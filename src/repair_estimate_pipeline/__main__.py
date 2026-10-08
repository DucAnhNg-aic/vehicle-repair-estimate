"""Chạy cả luồng VRE OCR → mapping: python -m repair_estimate_pipeline <file>."""
from __future__ import annotations

import argparse
import os
import sys
from contextlib import ExitStack
from pathlib import Path

from google import genai
from google.genai import types

from mapping.mapper import CODEBOOK_DIR
from ocr.extractor import PROJECT_ROOT, load_env
from .runner import REPAIR_ESTIMATE_OUTPUT_DIR, run_repair_estimate_pipeline


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path, help="Ảnh/PDF/Markdown báo giá.")
    parser.add_argument("--model", help="Model OCR; ưu tiên hơn OCR_MODEL/GEMINI_MODEL trong env/.env.")
    parser.add_argument("--mapping-model", help="Model cho Gemini fallback của mapping; mặc định MAPPING_MODEL trong env/.env, nếu thiếu thì giống model OCR.")
    parser.add_argument("--exact-only", action="store_true", help="Mapping chỉ exact match, không gọi Gemini fallback.")
    parser.add_argument("--save-mongodb", action="store_true", help="Lưu kết quả vào MongoDB sau khi ghi JSON.")
    parser.add_argument("--no-cache", action="store_true",
                        help="Bỏ qua cache Redis/MongoDB: luôn gọi Gemini và không ghi cache.")
    parser.add_argument("--output-dir", type=Path, default=REPAIR_ESTIMATE_OUTPUT_DIR)
    parser.add_argument("--codebook-dir", type=Path, default=CODEBOOK_DIR)
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    parser.add_argument("--timeout", type=float, default=180, help="Timeout mỗi HTTP request (giây).")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout phải lớn hơn 0")
    load_env(args.env_file)
    model = args.model or os.getenv("OCR_MODEL") or os.getenv("GEMINI_MODEL") or "gemini-3.8-flash"
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        parser.error("Thiếu GEMINI_API_KEY trong env hoặc --env-file.")
    with ExitStack() as stack:
        repository = None
        if args.save_mongodb:
            from storage import MongoSettings, RepairEstimateRepository
            repository = stack.enter_context(RepairEstimateRepository(MongoSettings.from_env(args.env_file)))
            # Check storage before spending tokens on OCR.
            repository.ping()
            repository.ensure_indexes()
        cache = None
        if not args.no_cache:
            from llm_cache import TwoTierCache
            cache = stack.enter_context(TwoTierCache.from_env(args.env_file))
        client = stack.enter_context(genai.Client(
            api_key=api_key, http_options=types.HttpOptions(timeout=int(args.timeout * 1000))))
        summary = run_repair_estimate_pipeline(
            args.file, model=model, client=client, mapping_model=args.mapping_model or os.getenv("MAPPING_MODEL"),
            exact_only=args.exact_only, output_dir=args.output_dir,
            codebook_dir=args.codebook_dir, timeout=args.timeout, cache=cache,
        )
        if repository is not None:
            try:
                saved = repository.save_run(summary["run_dir"])
            except Exception as exc:
                print(f"Lưu MongoDB thất bại. JSON đã lưu tại: {summary['run_dir']}", file=sys.stderr)
                print(f"Import lại bằng: python -m storage import '{summary['run_dir']}'", file=sys.stderr)
                raise RuntimeError("Không lưu được MongoDB; dùng output JSON để import lại.") from exc
            print(f"Đã lưu MongoDB: {saved['run_id']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Report already printed by the pipeline; never expose the API key.
        message = str(exc)
        key = os.getenv("GEMINI_API_KEY")
        if key:
            message = message.replace(key, "<redacted>")
        print(f"Lỗi: {message}", file=sys.stderr)
        raise SystemExit(1)
