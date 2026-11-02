"""Metrics chung cho API và MongoDB, tách theo từng bước pipeline."""

from llm import PROVIDER_NAMES


def build_metrics(summary: dict, *, item_count: int, methods: dict) -> dict:
    def timing_cost(step: dict) -> dict:
        return {"duration_seconds": step.get("seconds"), "cost": step.get("cost")}

    steps = summary.get("steps") or {}
    ocr = steps.get("ocr") or {}
    mapping = steps.get("mapping") or {}
    return {
        "total": timing_cost(summary.get("total") or {}),
        "ocr": {**timing_cost(ocr), "cache_hit": bool(ocr.get("cache_hit"))},
        "mapping": {
            **timing_cost(mapping),
            "item_count": item_count,
            "exact_count": methods.get("exact", 0),
            # Tổng item map bằng AI, cộng mọi provider ("claude", "gemini").
            "llm_count": sum(methods.get(name, 0) for name in PROVIDER_NAMES),
            "unmatched_count": methods.get("unmatched", 0),
            "cache_hits": mapping.get("cache_hits", 0),
            "cache_misses": mapping.get("cache_misses", 0),
            "llm_calls": mapping.get("llm_calls", 0),
            "context_cache": mapping.get("context_cache", []),  # Gemini context cache; Claude không có
        },
    }
