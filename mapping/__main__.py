"""Map an existing OCR result: python -m mapping result.json."""
import argparse
import os
from contextlib import nullcontext
from pathlib import Path

from google import genai
from google.genai import types

from ocr.extractor import PROJECT_ROOT, load_env, write_json
from ocr.schema import Quotation
from .mapper import CODEBOOK_DIR, CodebookMapper, GeminiMatcher


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result", type=Path)
    parser.add_argument("--output", type=Path, default=Path("outputs/mapping.json"))
    parser.add_argument("--codebook-dir", type=Path, default=CODEBOOK_DIR)
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    parser.add_argument("--model")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--exact-only", action="store_true", help="Test exact match offline, không gọi Gemini")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout phải lớn hơn 0")
    quotation = Quotation.model_validate_json(args.result.read_text(encoding="utf-8"))
    load_env(args.env_file)
    model = args.model or os.getenv("GEMINI_MODEL") or "gemini-3.6-flash"
    if not args.exact_only and not os.getenv("GEMINI_API_KEY"):
        parser.error("Thiếu GEMINI_API_KEY; dùng --exact-only để test offline")
    with (nullcontext(None) if args.exact_only else genai.Client(
        api_key=os.getenv("GEMINI_API_KEY"), http_options=types.HttpOptions(timeout=int(args.timeout * 1000))
    )) as client:
        matcher = GeminiMatcher(client, model) if client else None
        mapper = CodebookMapper(matcher=matcher, codebook_dir=args.codebook_dir)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        try:
            results = mapper.map_quotation(quotation)
            write_json(args.output, {"document": quotation.document.model_dump(mode="json"),
                                    "items": [dict(item.model_dump(mode="json"), mapping=result.model_dump(mode="json"))
                                              for item, result in zip(quotation.items, results)]})
        finally:
            write_json(args.output.with_suffix(".calls.json"), matcher.calls if matcher else [])
    print(f"Đã map {len(results)} dòng: {args.output}")
    if matcher:
        total = matcher.cost_summary()
        print(f"Gemini fallback: {total['calls']} lời gọi; chi phí ước tính ${total['total_usd']:.6f} USD"
              + ("" if total["complete"] else f" (thiếu {total['unpriced_calls']} lời gọi chưa định giá)"))


if __name__ == "__main__":
    main()
