"""Metrics chung cho API và MongoDB, tách theo từng bước pipeline."""


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
            "gemini_count": methods.get("gemini", 0),
            "unmatched_count": methods.get("unmatched", 0),
            "cache_hits": mapping.get("cache_hits", 0),
            "cache_misses": mapping.get("cache_misses", 0),
            "gemini_calls": mapping.get("gemini_calls", 0),
            "context_cache": mapping.get("context_cache", []),
        },
    }
