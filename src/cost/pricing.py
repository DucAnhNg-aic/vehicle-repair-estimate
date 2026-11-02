"""Ước tính chi phí gọi LLM theo giá niêm yết — không phải hóa đơn.

Nơi duy nhất chứa bảng giá và logic tính tiền cho mọi provider (Claude, Gemini); model được
nhận ra theo tiền tố tên. Mọi chỗ gọi model dùng:

    from cost import cost_from_response, sum_costs

    cost = cost_from_response(response.model_dump(mode="json"), fallback_model=model)
    total = sum_costs([cost_a, cost_b, ...])

Thêm model mới: thêm một dòng vào ``CLAUDE_PRICES``/``PRICES`` sau khi kiểm tra trang giá.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Iterable

PRICING_SOURCE = "https://ai.google.dev/gemini-api/docs/pricing"
PRICING_CHECKED = "2026-10-08"
# Google công bố giá khuyến mãi đến 2026-12-31 và giá thường từ 2027-01-01.
REGULAR_FROM = date(2027, 1, 1)
# USD / 1M token: (input, cached_input, output gồm cả thinking).
PRICES: dict[str, dict[str, tuple[str, str, str] | None]] = {
    "gemini-3.6-flash": {"promo": ("0.75", "0.075", "3.75"), "regular": ("1.50", "0.15", "7.50")},
    "gemini-3.7-flash": {"promo": ("0.75", "0.075", "3.75"), "regular": ("1.50", "0.15", "7.50")},
    "gemini-3.8-flash": {"promo": ("0.75", "0.075", "3.75"), "regular": ("1.50", "0.15", "7.50")},
    # Giá thường từ 2027-01-01 của Flash-Lite chưa được Google công bố rõ nên để None, không đoán.
    "gemini-3.5-flash-lite": {"promo": ("0.30", "0.03", "2.50"), "regular": None},
}

CLAUDE_PRICING_SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing"
CLAUDE_PRICING_CHECKED = "2026-11-01"
# USD / 1M token: (input, ghi cache 5 phút, ghi cache 1 giờ, đọc cache, output gồm cả thinking).
# Opus 5 / Opus 4.8 là đích của server-side fallback khi Opus 5.5 từ chối.
CLAUDE_PRICES: dict[str, tuple[str, str, str, str, str]] = {
    "claude-opus-5-5": ("4", "5", "8", "0.20", "20"),
    "claude-opus-5": ("5", "6.25", "10", "0.50", "25"),
    "claude-opus-4-8": ("5", "6.25", "10", "0.50", "25"),
    "claude-sonnet-5-5": ("2", "2.50", "4", "0.10", "10"),
    "claude-haiku-5-5": ("0.10", "0.125", "0.20", "0.01", "0.50"),
}
# Prompt (gồm cả token đọc/ghi cache) vượt ngưỡng thì cả request tính theo bảng giá cao hơn.
CLAUDE_LONG_PROMPT: dict[str, tuple[int, tuple[str, str, str, str, str]]] = {
    "claude-haiku-5-5": (100_000, ("0.50", "0.625", "1", "0.05", "2.50")),
}

TOKEN_FIELDS = ("total_input_tokens", "total_cached_tokens", "total_output_tokens", "total_thought_tokens")
# Claude: total_thought_tokens luôn 0 vì thinking đã nằm trong output_tokens; ghi cache tính riêng.
SUMMARY_TOKEN_FIELDS = TOKEN_FIELDS + ("total_cache_write_tokens",)
_USD_FIELDS = ("input_usd", "cached_input_usd", "cache_write_usd", "output_usd", "thinking_usd",
               "context_cache_storage_usd", "total_usd")
_MILLION = Decimal(1_000_000)


def today_utc() -> date:
    return datetime.now(timezone.utc).date()


def _base(model: str, source: str, checked: str, pricing_date: date) -> dict:
    return {
        "currency": "USD", "basis": "standard_paid_tier_estimate", "model": model,
        "pricing_source": source, "pricing_checked_date": checked,
        "pricing_date": pricing_date.isoformat(), "total_usd": None,
    }


def estimate_cost(model: str, usage: dict | None, pricing_date: date | None = None) -> dict:
    """Chi phí của một response. ``total_usd`` là None (kèm ``reason``) nếu không tính được."""
    pricing_date = pricing_date or today_utc()
    model = (model or "").removeprefix("models/")
    if model.startswith("claude-"):
        return _estimate_claude(model, usage, pricing_date)
    result = _base(model, PRICING_SOURCE, PRICING_CHECKED, pricing_date)
    if model not in PRICES:
        return {**result, "reason": "Model chưa có bảng giá được xác minh."}
    if not usage:
        return {**result, "reason": "Response không có token usage."}
    counts = [usage.get(name) for name in TOKEN_FIELDS]
    if any(type(value) is not int or value < 0 for value in counts):
        return {**result, "reason": "Token usage thiếu hoặc không hợp lệ."}
    input_tokens, cached_tokens, output_tokens, thought_tokens = counts
    if cached_tokens > input_tokens:
        return {**result, "reason": "Cached tokens lớn hơn input tokens."}
    tier = "promo" if pricing_date < REGULAR_FROM else "regular"
    if PRICES[model][tier] is None:
        return {**result, "reason": f"Model chưa có giá {tier} được xác minh."}
    input_rate, cached_rate, output_rate = map(Decimal, PRICES[model][tier])
    input_cost = Decimal(input_tokens - cached_tokens) * input_rate / _MILLION
    cached_cost = Decimal(cached_tokens) * cached_rate / _MILLION
    output_cost = Decimal(output_tokens) * output_rate / _MILLION
    thought_cost = Decimal(thought_tokens) * output_rate / _MILLION
    return {
        **result,
        "rates_usd_per_million_tokens": {
            "input": float(input_rate), "cached_input": float(cached_rate), "output_including_thinking": float(output_rate),
        },
        "tokens": dict(zip(TOKEN_FIELDS, counts)),
        "input_usd": float(input_cost), "cached_input_usd": float(cached_cost),
        "output_usd": float(output_cost), "thinking_usd": float(thought_cost),
        "total_usd": float(input_cost + cached_cost + output_cost + thought_cost),
    }


def _estimate_claude(model: str, usage: dict | None, pricing_date: date) -> dict:
    """``usage`` là ``Message.usage`` của Claude: input_tokens chỉ gồm phần không đọc/ghi cache."""
    result = _base(model, CLAUDE_PRICING_SOURCE, CLAUDE_PRICING_CHECKED, pricing_date)
    if model not in CLAUDE_PRICES:
        return {**result, "reason": "Model chưa có bảng giá được xác minh."}
    if not usage:
        return {**result, "reason": "Response không có token usage."}
    counts = [usage.get("input_tokens"), usage.get("cache_creation_input_tokens") or 0,
              usage.get("cache_read_input_tokens") or 0, usage.get("output_tokens")]
    written_1h = (usage.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0
    if any(type(value) is not int or value < 0 for value in [*counts, written_1h]) or written_1h > counts[1]:
        return {**result, "reason": "Token usage thiếu hoặc không hợp lệ."}
    uncached, written, read, output_tokens = counts
    prompt_tokens = uncached + written + read
    rates = CLAUDE_PRICES[model]
    threshold, long_rates = CLAUDE_LONG_PROMPT.get(model, (None, None))
    if threshold is not None and prompt_tokens > threshold:
        rates = long_rates
    input_rate, write_5m_rate, write_1h_rate, read_rate, output_rate = map(Decimal, rates)
    input_cost = Decimal(uncached) * input_rate / _MILLION
    write_cost = (Decimal(written - written_1h) * write_5m_rate + Decimal(written_1h) * write_1h_rate) / _MILLION
    read_cost = Decimal(read) * read_rate / _MILLION
    output_cost = Decimal(output_tokens) * output_rate / _MILLION
    return {
        **result,
        "rates_usd_per_million_tokens": {
            "input": float(input_rate), "cache_write_5m": float(write_5m_rate), "cache_write_1h": float(write_1h_rate),
            "cached_input": float(read_rate), "output_including_thinking": float(output_rate),
        },
        "tokens": {"total_input_tokens": prompt_tokens, "total_cached_tokens": read, "total_output_tokens": output_tokens,
                   "total_thought_tokens": 0, "total_cache_write_tokens": written},
        "input_usd": float(input_cost), "cached_input_usd": float(read_cost), "cache_write_usd": float(write_cost),
        "output_usd": float(output_cost), "thinking_usd": 0.0,
        "total_usd": float(input_cost + write_cost + read_cost + output_cost),
    }


def cost_from_response(response_data: dict | None, fallback_model: str, pricing_date: date | None = None) -> dict:
    """Chi phí từ ``response.model_dump(mode="json")``; ưu tiên model ghi trong response
    (Claude: model thực sự phục vụ, kể cả khi server-side fallback chuyển sang model khác)."""
    data = response_data or {}
    usage = data.get("usage")
    if usage is None:
        meta = data.get("usage_metadata") or data.get("usageMetadata")
        if isinstance(meta, dict):
            def field(snake, camel, default=None):
                return meta.get(snake, meta.get(camel, default))
            usage = {
                "total_input_tokens": field("prompt_token_count", "promptTokenCount"),
                "total_cached_tokens": field("cached_content_token_count", "cachedContentTokenCount", 0),
                "total_output_tokens": field("candidates_token_count", "candidatesTokenCount", 0),
                "total_thought_tokens": field("thoughts_token_count", "thoughtsTokenCount", 0),
            }
    return estimate_cost(data.get("model") or fallback_model, usage, pricing_date)


def estimate_cache_storage(model: str, token_count: int | None, ttl_seconds: int,
                           pricing_date: date | None = None) -> float | None:
    """Phí lưu context cache của Gemini cho trọn TTL, tính một lần lúc tạo cache, tách khỏi token request."""
    if model.removeprefix("models/") not in PRICES or type(token_count) is not int or token_count < 0:
        return None
    started_at = datetime.now(timezone.utc) if pricing_date is None else datetime.combine(pricing_date, datetime.min.time(), tzinfo=timezone.utc)
    # Account for a TTL crossing the published price-change date.
    boundary = datetime.combine(REGULAR_FROM, datetime.min.time(), tzinfo=timezone.utc)
    promo_seconds = min(ttl_seconds, max(0, int((boundary - started_at).total_seconds())))
    regular_seconds = ttl_seconds - promo_seconds
    return float(Decimal(token_count) / _MILLION *
                 (Decimal(promo_seconds) * Decimal("0.50") + Decimal(regular_seconds)) / Decimal(3600))


def sum_costs(costs: Iterable[dict | None]) -> dict:
    """Cộng nhiều kết quả ``estimate_cost``. ``total_usd`` chỉ gồm phần đã định giá;
    ``complete`` = False nếu có lời gọi chưa định giá được (xem ``unpriced_reasons``)."""
    costs = [c for c in costs if c is not None]
    priced = [c for c in costs if c.get("total_usd") is not None]
    usd = {key: float(sum((Decimal(str(c.get(key, 0))) for c in priced), Decimal(0))) for key in _USD_FIELDS}
    # Gemini: phí lưu context cache vẫn phát sinh khi request sau đó lỗi (lời gọi chưa định giá).
    unpriced_storage = sum(Decimal(str(c.get("context_cache_storage_usd", 0))) for c in costs if c not in priced)
    usd["context_cache_storage_usd"] += float(unpriced_storage)
    usd["total_usd"] += float(unpriced_storage)
    storage_unpriced = any(not c.get("context_cache_storage_complete", True) for c in costs)
    return {
        "currency": "USD", "basis": "standard_paid_tier_estimate",
        "calls": len(costs), "priced_calls": len(priced), "unpriced_calls": len(costs) - len(priced),
        "complete": len(priced) == len(costs) and not storage_unpriced,
        "unpriced_reasons": sorted(({c.get("reason") for c in costs if c.get("total_usd") is None} - {None}) |
                                   ({"Phí lưu context cache chưa định giá."} if storage_unpriced else set())),
        "tokens": {key: sum(c["tokens"].get(key, 0) for c in priced) for key in SUMMARY_TOKEN_FIELDS},
        **usd,
    }
