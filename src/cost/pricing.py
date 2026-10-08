"""Ước tính chi phí Gemini (Standard paid tier) — không phải hóa đơn.

Nơi duy nhất chứa bảng giá và logic tính tiền. Mọi chỗ gọi Gemini dùng:

    from cost import cost_from_response, sum_costs

    cost = cost_from_response(response.model_dump(mode="json"), fallback_model=model)
    total = sum_costs([cost_a, cost_b, ...])

Thêm model mới: thêm một dòng vào ``PRICES`` sau khi kiểm tra trang giá.
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
    "gemini-3.8-flash": {"promo": ("0.75", "0.075", "3.75"), "regular": ("1.50", "0.15", "7.50")},
    # Giá thường từ 2027-01-01 của Flash-Lite chưa được Google công bố rõ nên để None, không đoán.
    "gemini-3.5-flash-lite": {"promo": ("0.30", "0.03", "2.50"), "regular": None},
}
TOKEN_FIELDS = ("total_input_tokens", "total_cached_tokens", "total_output_tokens", "total_thought_tokens")
_USD_FIELDS = ("input_usd", "cached_input_usd", "output_usd", "thinking_usd", "context_cache_storage_usd", "total_usd")
_MILLION = Decimal(1_000_000)


def today_utc() -> date:
    return datetime.now(timezone.utc).date()


def estimate_cost(model: str, usage: dict | None, pricing_date: date | None = None) -> dict:
    """Chi phí của một response. ``total_usd`` là None (kèm ``reason``) nếu không tính được."""
    pricing_date = pricing_date or today_utc()
    model = (model or "").removeprefix("models/")
    result = {
        "currency": "USD", "basis": "standard_paid_tier_estimate", "model": model,
        "pricing_source": PRICING_SOURCE, "pricing_checked_date": PRICING_CHECKED,
        "pricing_date": pricing_date.isoformat(), "total_usd": None,
    }
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


def cost_from_response(response_data: dict | None, fallback_model: str, pricing_date: date | None = None) -> dict:
    """Chi phí từ ``response.model_dump(mode="json")``; ưu tiên model ghi trong response."""
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
    """Estimate full configured TTL once at cache creation, separate from request tokens."""
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
    unpriced_storage = sum(Decimal(str(c.get("context_cache_storage_usd", 0))) for c in costs if c not in priced)
    usd["context_cache_storage_usd"] += float(unpriced_storage)
    usd["total_usd"] += float(unpriced_storage)
    return {
        "currency": "USD", "basis": "standard_paid_tier_estimate",
        "calls": len(costs), "priced_calls": len(priced), "unpriced_calls": len(costs) - len(priced),
        "complete": len(priced) == len(costs) and all(c.get("context_cache_storage_complete", True) for c in costs),
        "unpriced_reasons": sorted(({c.get("reason") for c in costs if c.get("total_usd") is None} - {None}) |
                                  ({"Phí lưu context cache chưa định giá."} if any(not c.get("context_cache_storage_complete", True) for c in costs) else set())),
        "tokens": {key: sum(c["tokens"][key] for c in priced) for key in TOKEN_FIELDS},
        **usd,
    }
