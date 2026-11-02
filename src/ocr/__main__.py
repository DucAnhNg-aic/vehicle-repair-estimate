"""Extract one Vehicle Repair Estimate (Claude hoặc Gemini theo tên model): python -m ocr [file]."""

from __future__ import annotations

import argparse
import mimetypes
import os
import sys
from pathlib import Path

from llm import PROVIDER_NAMES, LlmClients, Media, default_models, default_provider, get_provider, redact

from .extractor import (
    IMG_DIR, OUTPUT_DIR, PROJECT_ROOT, ROOT, SUPPORTED,
    extract_repair_estimate, load_env, load_prompt,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", nargs="?", type=Path, help="Ảnh/PDF/Markdown; mặc định dùng ảnh mẫu trong VRE.")
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    parser.add_argument("--prompt", type=Path, default=ROOT / "prompt.py",
                        help="File .py khai báo PROMPT hoặc file text UTF-8; mặc định prompt.py.")
    parser.add_argument("--provider", choices=sorted(PROVIDER_NAMES), help="Mặc định LLM_PROVIDER trong env/.env.")
    parser.add_argument("--model", help="claude-... hoặc gemini-...; mặc định <PROVIDER>_OCR_MODEL của provider.")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--timeout", type=float, default=180, help="Timeout mỗi HTTP request và chờ upload (giây).")
    parser.add_argument("--list-models", action="store_true", help="Liệt kê model API key của provider (theo --model) truy cập được.")
    parser.add_argument("--dry-run", action="store_true", help="Kiểm tra input/config, không gọi API.")
    args = parser.parse_args()
    load_env(args.env_file)
    if args.timeout <= 0:
        parser.error("--timeout phải lớn hơn 0")
    try:
        model = args.model or default_models(args.provider or default_provider())[0]
        provider = get_provider(model)
        LlmClients(args.timeout).require(model)
    except (RuntimeError, ValueError) as exc:
        parser.error(str(exc))

    if args.list_models:
        if args.dry_run:
            parser.error("Không kết hợp --list-models với --dry-run")
        module = provider.load()
        client = module.create_client(os.environ[provider.api_key_env], args.timeout)
        try:
            for name in module.list_models(client):
                print(name)
        finally:
            client.close()
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
    load_prompt(args.prompt)  # báo lỗi prompt hỏng trước khi gọi API
    backend_type = provider.load().Backend
    input_mode = backend_type.input_mode(Media(text=markdown_text) if is_markdown else Media(path=file_path, mime=mime))
    print(f"Model: {model} ({provider.name})\nInput: {file_path}\nAPI: {backend_type.endpoint}\nMedia: {input_mode}", flush=True)
    if args.dry_run:
        return 0

    extract_repair_estimate(file_path, model=model, prompt_path=args.prompt,
                            output_dir=args.output_dir, timeout=args.timeout)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Some upstream errors include request details; never expose the API key.
        print(f"Lỗi: {redact(str(exc))}", file=sys.stderr)
        raise SystemExit(1)
