"""Luồng chính: ảnh/PDF/Markdown → OCR (Gemini) → mapping codebook (exact + Gemini fallback).

Mỗi lần chạy tạo một thư mục riêng:

    <output_dir>/<timestamp>/
        summary.json        trạng thái, thời gian, kết quả và chi phí từng bước + tổng
        report.txt          báo cáo dạng text (giống log in ra terminal)
        ocr/                response.json, raw.txt, result.json, metadata.json của bước OCR
        mapping.json        các trường OCR + ``mapping`` cho từng item
        mapping.calls.json  từng lời gọi Gemini fallback (response, usage, cost, latency)

summary.json luôn được ghi, kể cả khi một bước lỗi (lỗi vẫn được raise cho caller).
"""
from __future__ import annotations

import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from cost import sum_costs
from llm_cache.keys import file_digest
from mapping import CodebookMapper, GeminiMatcher
from mapping.mapper import CODEBOOK_DIR
from ocr.extractor import OUTPUT_DIR, ROOT as OCR_ROOT, extract_repair_estimate, write_json

REPAIR_ESTIMATE_OUTPUT_DIR = OUTPUT_DIR / "repair_estimate_pipeline"


def run_repair_estimate_pipeline(
    file_path: str | Path, *, model: str, client, mapping_model: str | None = None,
    exact_only: bool = False, output_dir: str | Path = REPAIR_ESTIMATE_OUTPUT_DIR,
    codebook_dir: str | Path = CODEBOOK_DIR, prompt_path: str | Path = OCR_ROOT / "prompt.py",
    timeout: float = 180, verbose: bool = True, cache=None,
    ocr_thinking_level: str | None = None, mapping_thinking_level: str | None = None,
) -> dict:
    """Chạy OCR rồi mapping; trả về summary (cũng được ghi ra ``summary.json``)."""
    log = print if verbose else (lambda *args, **kwargs: None)
    file_path = Path(file_path).resolve()
    started_at = datetime.now(timezone.utc)
    run_dir = Path(output_dir) / started_at.strftime("%Y%m%dT%H%M%S_%fZ")
    run_dir.mkdir(parents=True)
    mapping_model = mapping_model or model
    summary = {
        "status": "running", "input": str(file_path), "input_sha256": None, "run_dir": str(run_dir),
        "started_at": started_at.isoformat(), "models": {"ocr": model, "mapping": None if exact_only else mapping_model},
        "steps": {"ocr": {"status": "pending"}, "mapping": {"status": "pending"}},
        "items": [], "total": {}, "error": None,
    }
    matcher = None
    started = time.perf_counter()
    try:
        summary["input_sha256"] = file_digest(file_path)
        # ---- Bước 1: OCR ----
        log(f"[1/2] OCR: {file_path.name} (model {model}) ...", flush=True)
        step_started = time.perf_counter()
        try:
            estimate = extract_repair_estimate(file_path, model=model, client=client, prompt_path=prompt_path,
                                               run_dir=run_dir / "ocr", timeout=timeout, verbose=False, cache=cache,
                                               thinking_level=ocr_thinking_level)
        finally:
            metadata_path = run_dir / "ocr" / "metadata.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.is_file() else {}
            summary["steps"]["ocr"] = {
                "status": metadata.get("status", "failed"),
                "seconds": round(time.perf_counter() - step_started, 3),
                "result": str(run_dir / "ocr" / "result.json"),
                "cost": metadata.get("cost"),
                "cache_hit": bool(metadata.get("cache_hit")),
            }
        summary["steps"]["ocr"].update(
            items=len(estimate.items), categories=dict(Counter(item.category for item in estimate.items)))

        # ---- Bước 2: Mapping ----
        log(f"[2/2] Mapping: {len(estimate.items)} hạng mục "
            f"({'exact-only' if exact_only else f'exact + Gemini fallback {mapping_model}'}) ...", flush=True)
        step_started = time.perf_counter()
        matcher = None if exact_only else GeminiMatcher(client, mapping_model, cache=cache, thinking_level=mapping_thinking_level,
            context_cache_dir=Path(output_dir) / ".gemini_context_cache")
        mapper = CodebookMapper(matcher=matcher, codebook_dir=codebook_dir)
        try:
            results = mapper.map_repair_estimate(estimate)
            write_json(run_dir / "mapping.json", {
                "document": estimate.document.model_dump(mode="json"),
                "items": [dict(item.model_dump(mode="json"), mapping=result.model_dump(mode="json"))
                          for item, result in zip(estimate.items, results)]})
            summary["steps"]["mapping"]["status"] = "success"
        finally:
            calls = matcher.calls if matcher else []
            write_json(run_dir / "mapping.calls.json", calls)
            summary["steps"]["mapping"].update(
                status=summary["steps"]["mapping"]["status"] if summary["steps"]["mapping"]["status"] == "success" else "failed",
                seconds=round(time.perf_counter() - step_started, 3),
                result=str(run_dir / "mapping.json"),
                gemini_calls=len(calls),
                invalid_selections=sum(len(call.get("invalid_selections", [])) for call in calls),
                context_cache=[call["context_cache"] for call in calls if call.get("context_cache")],
                cache_hits=matcher.cache_hits if matcher else 0,
                cache_misses=matcher.cache_misses if matcher else 0,
                cost=sum_costs(call["cost"] for call in calls),
            )
        summary["steps"]["mapping"]["methods"] = dict(Counter(result.method for result in results))
        summary["items"] = [
            {"raw_name": item.raw_name, "category": item.category, "method": result.method,
             "codebook": result.codebook, "codes": [row.code for row in result.matches],
             "matched_name": result.matches[0].name if result.matches else None}
            for item, result in zip(estimate.items, results)
        ]
        summary["status"] = "success"
        return summary
    except Exception as exc:
        summary["status"] = "failed"
        summary["error"] = f"{type(exc).__name__}: {exc}"
        for step in summary["steps"].values():
            if step["status"] == "pending":
                step["status"] = "skipped"
        raise
    finally:
        ocr_cost = summary["steps"]["ocr"].get("cost")
        call_costs = [call["cost"] for call in (matcher.calls if matcher else [])]
        summary["total"] = {
            "seconds": round(time.perf_counter() - started, 3),
            "cost": sum_costs(([ocr_cost] if ocr_cost else []) + call_costs),
        }
        report = format_report(summary)
        write_json(run_dir / "summary.json", summary)
        (run_dir / "report.txt").write_text(report + "\n", encoding="utf-8")
        log(report, flush=True)


def _usd(cost: dict | None) -> str:
    if not cost:
        return "-"
    total = cost.get("total_usd")
    if "calls" in cost:  # kết quả sum_costs
        text = f"${total:.6f}"
        return text if cost["complete"] else f"{text} (+{cost['unpriced_calls']} lời gọi chưa định giá)"
    return f"${total:.6f}" if total is not None else f"chưa định giá ({cost.get('reason')})"


def _tokens(cost: dict | None) -> str:
    tokens = (cost or {}).get("tokens") or {}
    if not tokens:
        return ""
    return (f"in {tokens['total_input_tokens']:,} · out {tokens['total_output_tokens']:,}"
            f" · think {tokens['total_thought_tokens']:,} token")


def format_report(summary: dict) -> str:
    ocr, mapping, total = summary["steps"]["ocr"], summary["steps"]["mapping"], summary["total"]
    line = "-" * 78
    out = [f"=== VRE pipeline: {Path(summary['input']).name} — {summary['status'].upper()} ===",
           f"Run dir : {summary['run_dir']}",
           f"Models  : OCR {summary['models']['ocr']} · mapping {summary['models']['mapping'] or 'exact-only'}",
           line]
    detail = ", ".join(f"{k} {v}" for k, v in ocr.get("categories", {}).items())
    out.append(f"[1] OCR      {ocr['status']:<8} {ocr.get('seconds', 0):>7.1f}s  "
               f"{ocr.get('items', 0)} hạng mục ({detail})" if "items" in ocr else
               f"[1] OCR      {ocr['status']:<8} {ocr.get('seconds', 0):>7.1f}s")
    out.append("    cost - (trúng cache, không gọi Gemini)" if ocr.get("cache_hit") else
               f"    cost {_usd(ocr.get('cost'))}  {_tokens(ocr.get('cost'))}")
    methods = ", ".join(f"{k} {v}" for k, v in mapping.get("methods", {}).items())
    cache_note = f" · {mapping['cache_hits']} item từ cache" if mapping.get("cache_hits") else ""
    out.append(f"[2] Mapping  {mapping['status']:<8} {mapping.get('seconds', 0):>7.1f}s  "
               f"{methods or '-'}; {mapping.get('gemini_calls', 0)} lời gọi Gemini{cache_note}")
    out.append(f"    cost {_usd(mapping.get('cost'))}  {_tokens(mapping.get('cost'))}")
    out.append(line)
    out.append(f"TỔNG              {total.get('seconds', 0):>7.1f}s  cost {_usd(total.get('cost'))}  "
               f"({total.get('cost', {}).get('calls', 0)} lời gọi Gemini)")
    out.append(f"    {_tokens(total.get('cost'))}")
    if summary["items"]:
        out.append(line)
        for i, item in enumerate(summary["items"], 1):
            codes = ", ".join(item["codes"]) or "-"
            target = f" → {item['matched_name']}" if item["method"] == "gemini" else ""
            out.append(f"{i:>3}. [{item['category']:<8}] {item['method']:<9} {codes:<24} {item['raw_name']}{target}")
    if summary["error"]:
        out.append(line)
        out.append(f"LỖI: {summary['error']}")
    return "\n".join(out)
