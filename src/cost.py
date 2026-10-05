"""Estimate standard paid-tier token cost; this is not an invoice."""

from datetime import date
from decimal import Decimal

PRICING_SOURCE = "https://ai.google.dev/gemini-api/docs/pricing"
PRICING_CHECKED = "2026-10-01"


def estimate_cost(model: str, usage: dict | None, pricing_date: date) -> dict:
    result = {
        "currency": "USD", "basis": "standard_paid_tier_estimate",
        "pricing_source": PRICING_SOURCE, "pricing_checked_date": PRICING_CHECKED,
        "pricing_date": pricing_date.isoformat(), "total_usd": None,
    }
    if model.removeprefix("models/") != "gemini-3.6-flash":
        return {**result, "reason": "Model chưa có bảng giá được xác minh."}
    if not usage:
        return {**result, "reason": "Response không có token usage."}
    names = ("total_input_tokens", "total_cached_tokens", "total_output_tokens", "total_thought_tokens")
    counts = [usage.get(name) for name in names]
    if any(type(value) is not int or value < 0 for value in counts):
        return {**result, "reason": "Token usage thiếu hoặc không hợp lệ."}
    input_tokens, cached_tokens, output_tokens, thought_tokens = counts
    if cached_tokens > input_tokens:
        return {**result, "reason": "Cached tokens lớn hơn input tokens."}
    # Google publishes promotional rates through 2026-12-31 and regular rates from 2027-01-01.
    rates = ("0.75", "0.075", "3.75") if pricing_date < date(2027, 1, 1) else ("1.50", "0.15", "7.50")
    input_rate, cached_rate, output_rate = map(Decimal, rates)
    million = Decimal(1_000_000)
    input_cost = Decimal(input_tokens - cached_tokens) * input_rate / million
    cached_cost = Decimal(cached_tokens) * cached_rate / million
    output_cost = Decimal(output_tokens) * output_rate / million
    thought_cost = Decimal(thought_tokens) * output_rate / million
    return {
        **result,
        "rates_usd_per_million_tokens": {
            "input": float(input_rate), "cached_input": float(cached_rate), "output_including_thinking": float(output_rate),
        },
        "tokens": dict(zip(names, counts)),
        "input_usd": float(input_cost), "cached_input_usd": float(cached_cost),
        "output_usd": float(output_cost), "thinking_usd": float(thought_cost),
        "total_usd": float(input_cost + cached_cost + output_cost + thought_cost),
    }
