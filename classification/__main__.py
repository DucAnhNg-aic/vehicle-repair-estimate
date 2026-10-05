"""Classify Codebook using Gemini, with validated batch checkpoints."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from google import genai
from google.genai import types
from tqdm import tqdm

# Reuse cost accounting from the OCR package.
from ocr.cost import estimate_cost
from .classifier import digest, export_xlsx, read_rows, unique_names, validate_batch, write_json
from .prompt import PROMPT
from .schema import ClassificationBatch

ROOT = Path(__file__).resolve().parents[1]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=Path, default=ROOT / "codebook/codebook.xlsx")
    p.add_argument("--sheet", default="Codebook phụ tùng")
    p.add_argument("--env-file", type=Path, default=ROOT / "ocr/.env")
    p.add_argument("--model")
    p.add_argument("--batch-size", type=int, default=100)
    p.add_argument("--timeout", type=float, default=180)
    p.add_argument("--run-dir", type=Path)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--max-batches", type=int, help="Giới hạn số batch mới để chạy thử; resume để tiếp tục.")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    if a.batch_size <= 0 or a.timeout <= 0 or (a.max_batches is not None and a.max_batches <= 0):
        p.error("batch-size, timeout, max-batches phải dương")
    if a.resume and not a.run_dir:
        p.error("--resume yêu cầu --run-dir")
    if a.env_file.is_file():
        for line in a.env_file.read_text().splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key in {"GEMINI_API_KEY", "GEMINI_MODEL"}:
                os.environ.setdefault(key, value.strip().strip("\"'"))
    model = a.model or os.getenv("GEMINI_MODEL") or "gemini-3.6-flash"
    source_hash = digest(a.input.read_bytes())
    rows = read_rows(a.input, a.sheet)
    names = unique_names(rows)
    batches = [names[i:i + a.batch_size] for i in range(0, len(names), a.batch_size)]
    manifest = {"source_sha256": source_hash, "sheet": a.sheet, "model": model,
                "batch_size": a.batch_size, "rows": len(rows), "unique_names": len(names), "pipeline_version": 3,
                "contract_sha256": digest((PROMPT + json.dumps(ClassificationBatch.model_json_schema(), sort_keys=True)).encode())}
    print(f"{len(rows)} dòng; {len(names)} tên unique; {len(batches)} batch; model={model}", flush=True)
    if a.dry_run:
        print(json.dumps({"names": batches[0][:3]}, ensure_ascii=False))
        return
    if not os.getenv("GEMINI_API_KEY"):
        p.error("Thiếu GEMINI_API_KEY")
    run = a.run_dir or ROOT / "classification/outputs" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    if a.resume:
        if json.loads((run / "manifest.json").read_text()) != manifest:
            raise ValueError("Không resume: input/model/batch size/prompt/schema đã thay đổi")
    else:
        run.mkdir(parents=True, exist_ok=False)
        write_json(run / "manifest.json", manifest)
    print(f"Run: {run}", flush=True)
    results, executed = {}, 0
    try:
        with genai.Client(api_key=os.environ["GEMINI_API_KEY"], http_options=types.HttpOptions(timeout=int(a.timeout * 1000))) as client:
            for index, batch in enumerate(tqdm(batches, desc="Classification", unit="batch"), 1):
                checkpoint = run / f"batch_{index:05d}.json"
                if checkpoint.exists():
                    valid = validate_batch(json.loads(checkpoint.read_text()), batch)
                else:
                    if a.max_batches is not None and executed >= a.max_batches:
                        break
                    payload = {"names": batch}
                    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
                    prefix = run / f"batch_{index:05d}_{stamp}"
                    write_json(prefix.with_suffix(".input.json"), payload)
                    started = time.perf_counter()
                    response = client.interactions.create(
                        model=model, system_instruction=PROMPT,
                        input=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                        response_format={"type": "text", "mime_type": "application/json", "schema": ClassificationBatch.model_json_schema()},
                        store=False,
                    )
                    elapsed = time.perf_counter() - started
                    data = response.model_dump(mode="json", exclude_none=True)
                    write_json(prefix.with_suffix(".response.json"), data)
                    cost = estimate_cost(data.get("model") or model, data.get("usage"), datetime.now(timezone.utc).date())
                    write_json(prefix.with_suffix(".metrics.json"), {"seconds": elapsed, "usage": data.get("usage"), "cost": cost})
                    if response.status != "completed":
                        raise ValueError(f"Batch {index}: status={response.status}")
                    valid = validate_batch(json.loads(response.output_text or ""), batch)
                    write_json(checkpoint, valid)
                    executed += 1
                    tqdm.write(f"Batch {index}: {elapsed:.2f}s; estimated USD={cost['total_usd']}")
                for item in valid["items"]:
                    results[item["name"]] = item
    finally:
        if digest(a.input.read_bytes()) != source_hash:
            raise ValueError("Input thay đổi trong khi chạy; không export")
        complete = len(results) == len(names)
        export_xlsx(a.input, a.sheet, run / ("classified.xlsx" if complete else "classified.partial.xlsx"), rows, results)
        write_json(run / "unique_results.json", [results[n] for n in names if n in results])
        write_json(run / "results.json", [{**r, **results[r["name"]]} for r in rows if r["name"] in results])
        metrics = [json.loads(f.read_text()) for f in run.glob("*.metrics.json")]
        known = [m["cost"]["total_usd"] for m in metrics if m["cost"]["total_usd"] is not None]
        summary = {"complete": complete, "classified_rows": sum(r["name"] in results for r in rows), "total_rows": len(rows),
                   "classified_unique_names": len(results), "total_unique_names": len(names),
                   "item_types": dict(Counter(i["item_type"] for i in results.values())),
                   "request_seconds": sum(m["seconds"] for m in metrics),
                   "known_estimated_cost_usd": sum(known), "unpriced_responses": len(metrics) - len(known)}
        write_json(run / "summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        message = str(exc)
        key = os.getenv("GEMINI_API_KEY")
        if key:
            message = message.replace(key, "<redacted>")
        print(f"Lỗi: {message}", file=sys.stderr)
        sys.exit(1)
