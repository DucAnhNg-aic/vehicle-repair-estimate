"""Run one quotation extraction using Gemini's Interactions API."""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import runpy
import sys
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from google import genai
from google.genai import types

from schema import Quotation
from cost import estimate_cost

ROOT = Path(__file__).resolve().parent
SUPPORTED = {"application/pdf", "image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}
INLINE_LIMIT = 15 * 1024 * 1024  # Leave room for base64 expansion and prompt.


def load_env(path: Path) -> None:
    """Read simple KEY=value entries without executing shell or printing secrets."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if sep and key.strip() in {"GEMINI_API_KEY", "GEMINI_MODEL"}:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key.strip(), value)


def wait_for_file(client, uploaded, timeout: float):
    deadline = time.monotonic() + timeout
    while True:
        state = getattr(uploaded.state, "name", uploaded.state)
        if state == "ACTIVE":
            return uploaded
        if state == "FAILED":
            raise RuntimeError("Gemini không xử lý được file upload.")
        if time.monotonic() >= deadline:
            raise TimeoutError("Hết thời gian chờ file upload sẵn sàng.")
        time.sleep(2)
        uploaded = client.files.get(name=uploaded.name)


def write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_prompt(path: Path) -> str:
    """Load PROMPT from a Python module or read a UTF-8 text prompt."""
    if path.suffix.lower() == ".py":
        prompt = runpy.run_path(str(path)).get("PROMPT")
        if not isinstance(prompt, str):
            raise ValueError(f"File prompt Python phải khai báo PROMPT là chuỗi: {path}")
    else:
        prompt = path.read_text(encoding="utf-8")
    prompt = prompt.strip()
    if not prompt:
        raise ValueError(f"Prompt rỗng: {path}")
    return prompt


@contextmanager
def measure(timings: dict, name: str):
    started = time.perf_counter()
    try:
        yield
    finally:
        timings[name] = round(time.perf_counter() - started, 3)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", nargs="?", type=Path, help="Ảnh/PDF/Markdown; mặc định dùng ảnh mẫu trong BGSC.")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--prompt", type=Path, default=ROOT / "prompt.py",
                        help="File .py khai báo PROMPT hoặc file text UTF-8; mặc định prompt.py.")
    parser.add_argument("--model", help="Ưu tiên hơn GEMINI_MODEL trong env/.env.")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "outputs")
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
        samples = sorted(ROOT.glob("*.jpg"))
        if len(samples) != 1:
            parser.error("Hãy chỉ định file khi BGSC không có đúng một ảnh JPG mẫu.")
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

    # A unique run directory preserves earlier results, including failed validation.
    args.output_dir.mkdir(parents=True, exist_ok=True)
    run_dir = args.output_dir / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    run_dir.mkdir()
    started = time.perf_counter()
    timings = {}
    succeeded = False
    uploaded = None
    cost = estimate_cost(model, None, datetime.now(timezone.utc).date())
    with genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=int(args.timeout * 1000))) as client:
        try:
            with measure(timings, "prepare_media_seconds"):
                media = {"type": "document" if mime == "application/pdf" else "image", "mime_type": mime}
                if is_markdown:
                    media = {"type": "text", "text": markdown_text}
                elif use_inline:
                    media["data"] = base64.b64encode(file_path.read_bytes()).decode("ascii")
                else:
                    uploaded = client.files.upload(file=str(file_path), config={"mime_type": mime})
                    ready = wait_for_file(client, uploaded, args.timeout)
                    media["uri"] = ready.uri
            with measure(timings, "gemini_request_seconds"):
                interaction = client.interactions.create(
                    model=model,
                    system_instruction=prompt,
                    input=[{"type": "text", "text": "Trích xuất toàn bộ báo giá theo hướng dẫn và schema."}, media],
                    response_format={"type": "text", "mime_type": "application/json", "schema": Quotation.model_json_schema()},
                    store=False,
                )
            response_data = interaction.model_dump(mode="json", exclude_none=True)
            write_json(run_dir / "response.json", response_data)
            cost = estimate_cost(
                response_data.get("model") or model, response_data.get("usage"),
                datetime.now(timezone.utc).date(),
            )
            raw = interaction.output_text or ""
            (run_dir / "raw.txt").write_text(raw, encoding="utf-8")
            if interaction.status != "completed":
                raise RuntimeError(f"Interaction chưa hoàn thành: {interaction.status}")
            if not raw:
                raise RuntimeError("Gemini trả nội dung rỗng; xem response.json.")
            parsed = Quotation.model_validate_json(raw)
            # Preserve original JSON numeric values; validation does not recalculate money.
            write_json(run_dir / "result.json", json.loads(raw))
            succeeded = True
            print(f"Đã trích xuất {len(parsed.items)} hạng mục: {dict(Counter(item.category for item in parsed.items))}")
            print(f"Kết quả: {run_dir / 'result.json'}")
        finally:
            if uploaded is not None:
                try:
                    client.files.delete(name=uploaded.name)
                except Exception:
                    print("Không xóa được file tạm trên Gemini; file sẽ tự hết hạn theo Files API.", file=sys.stderr)
            elapsed = round(time.perf_counter() - started, 3)
            write_json(run_dir / "metadata.json", {
                "model": model,
                "input": str(file_path),
                "input_mode": input_mode,
                "prompt_file": str(args.prompt.resolve()),
                "endpoint": "/v1beta/interactions",
                "status": "success" if succeeded else "failed",
                "elapsed_seconds": elapsed,
                "cost": cost,
                **timings,
            })
            print(f"Chuẩn bị file: {timings.get('prepare_media_seconds', 0):.3f}s", flush=True)
            if "gemini_request_seconds" in timings:
                print(f"Gọi Gemini: {timings['gemini_request_seconds']:.3f}s", flush=True)
            print(f"Tổng thời gian xử lý: {elapsed:.3f}s", flush=True)
            if cost["total_usd"] is not None:
                print(f"Chi phí ước tính (paid tier): ${cost['total_usd']:.6f} USD", flush=True)
            else:
                print(f"Chi phí chưa xác định: {cost['reason']}", flush=True)
            print(f"Thư mục lần chạy: {run_dir}", flush=True)
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
