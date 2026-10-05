"""Run one quotation extraction using Gemini's Interactions API: python -m ocr [file]."""

from __future__ import annotations

import argparse
import mimetypes
import os
import sys
from pathlib import Path

from google import genai
from google.genai import types

from .extractor import (
    IMG_DIR, INLINE_LIMIT, OUTPUT_DIR, PROJECT_ROOT, ROOT, SUPPORTED,
    extract_document, load_env, load_prompt,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", nargs="?", type=Path, help="Ảnh/PDF/Markdown; mặc định dùng ảnh mẫu trong BGSC.")
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    parser.add_argument("--prompt", type=Path, default=ROOT / "prompt.py",
                        help="File .py khai báo PROMPT hoặc file text UTF-8; mặc định prompt.py.")
    parser.add_argument("--model", help="Ưu tiên hơn GEMINI_MODEL trong env/.env.")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--timeout", type=float, default=180, help="Timeout mỗi HTTP request và chờ upload (giây).")
    parser.add_argument("--list-models", action="store_true", help="Liệt kê model key hiện tại được truy cập.")
    parser.add_argument("--dry-run", action="store_true", help="Kiểm tra input/config, không gọi API.")
    args = parser.parse_args()
    load_env(args.env_file)
    model = args.model or os.getenv("GEMINI_MODEL") or "gemini-3.6-flash"
    if args.timeout <= 0:
        parser.error("--timeout phải lớn hơn 0")
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        parser.error("Thiếu GEMINI_API_KEY trong env hoặc --env-file.")

    if args.list_models:
        if args.dry_run:
            parser.error("Không kết hợp --list-models với --dry-run")
        with genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=int(args.timeout * 1000))) as client:
            for entry in client.models.list():
                print(entry.name)
        return 0

    file_path = args.file
    if file_path is None:
        samples = sorted(IMG_DIR.glob("*.jpg"))
        if len(samples) != 1:
            parser.error(f"Hãy chỉ định file khi {IMG_DIR} không có đúng một ảnh JPG mẫu.")
        file_path = samples[0]
    file_path = file_path.resolve()
    if not file_path.is_file() or file_path.stat().st_size == 0:
        parser.error(f"File không tồn tại hoặc rỗng: {file_path}")
    mime = mimetypes.guess_type(file_path)[0]
    is_markdown = file_path.suffix.lower() in {".md", ".markdown"}
    markdown_text = file_path.read_text(encoding="utf-8").strip() if is_markdown else None
    if is_markdown and not markdown_text:
        parser.error("Markdown rỗng.")
    if not is_markdown and mime not in SUPPORTED:
        parser.error("Chỉ hỗ trợ Markdown, PDF và ảnh JPG/PNG/WebP/HEIC/HEIF.")
    prompt = load_prompt(args.prompt)
    use_inline = file_path.stat().st_size <= INLINE_LIMIT
    input_mode = "markdown_text" if is_markdown else ("inline" if use_inline else "Files API")
    print(f"Model: {model}\nInput: {file_path}\nAPI: /v1beta/interactions\nMedia: {input_mode}", flush=True)
    if args.dry_run:
        return 0

    extract_document(file_path, model=model, api_key=api_key, prompt_path=args.prompt,
                     output_dir=args.output_dir, timeout=args.timeout)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Some upstream errors include request details; never expose the API key.
        message = str(exc)
        key = os.getenv("GEMINI_API_KEY")
        if key:
            message = message.replace(key, "<redacted>")
        print(f"Lỗi: {message}", file=sys.stderr)
        raise SystemExit(1)
