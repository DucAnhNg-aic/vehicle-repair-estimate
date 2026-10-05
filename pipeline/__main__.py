"""Chạy cả luồng OCR → mapping: python -m pipeline <ảnh/PDF/Markdown>."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from google import genai
from google.genai import types

from mapping.mapper import CODEBOOK_DIR
from ocr.extractor import PROJECT_ROOT, load_env
from .runner import PIPELINE_OUTPUT_DIR, run_pipeline


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path, help="Ảnh/PDF/Markdown báo giá.")
    parser.add_argument("--model", help="Model OCR; ưu tiên hơn GEMINI_MODEL trong env/.env.")
    parser.add_argument("--mapping-model", help="Model cho Gemini fallback của mapping; mặc định giống --model.")
    parser.add_argument("--exact-only", action="store_true", help="Mapping chỉ exact match, không gọi Gemini fallback.")
    parser.add_argument("--output-dir", type=Path, default=PIPELINE_OUTPUT_DIR)
    parser.add_argument("--codebook-dir", type=Path, default=CODEBOOK_DIR)
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    parser.add_argument("--timeout", type=float, default=180, help="Timeout mỗi HTTP request (giây).")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout phải lớn hơn 0")
    load_env(args.env_file)
    model = args.model or os.getenv("GEMINI_MODEL") or "gemini-3.8-flash"
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        parser.error("Thiếu GEMINI_API_KEY trong env hoặc --env-file.")
    with genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=int(args.timeout * 1000))) as client:
        run_pipeline(args.file, model=model, client=client, mapping_model=args.mapping_model,
                     exact_only=args.exact_only, output_dir=args.output_dir,
                     codebook_dir=args.codebook_dir, timeout=args.timeout)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Report already printed by run_pipeline; never expose the API key.
        message = str(exc)
        key = os.getenv("GEMINI_API_KEY")
        if key:
            message = message.replace(key, "<redacted>")
        print(f"Lỗi: {message}", file=sys.stderr)
        raise SystemExit(1)
