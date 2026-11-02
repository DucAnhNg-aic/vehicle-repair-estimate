"""Extract one Vehicle Repair Estimate bằng provider LLM của model (Claude hoặc Gemini, xem ``llm``)."""

from __future__ import annotations

import json
import mimetypes
import os
import runpy
import time
from collections import Counter
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

from .schema import RepairEstimate
from cost import cost_from_response, estimate_cost
from llm import API_KEY_ENVS, MODEL_ENVS, LlmClients, Media, get_provider, normalize_effort
from llm_cache import ocr_key

ROOT = Path(__file__).resolve().parent  # thư mục ocr (.env, prompt.py)
PROJECT_ROOT = ROOT.parents[1]  # ocr -> src -> thư mục gốc dự án (.env, data/, outputs/)
IMG_DIR = PROJECT_ROOT / "data" / "img"
OUTPUT_DIR = PROJECT_ROOT / "outputs"
# mimetypes phụ thuộc /etc/mime.types của hệ điều hành; image slim không có nên .webp/.heic/.heif trả None
for _mime, _suffix in (("image/webp", ".webp"), ("image/heic", ".heic"), ("image/heif", ".heif")):
    mimetypes.add_type(_mime, _suffix)
SUPPORTED = {"application/pdf", "image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}
OCR_USER_TEXT = "Trích xuất toàn bộ báo giá theo hướng dẫn và schema."
ENV_KEYS = {*API_KEY_ENVS, *MODEL_ENVS, "OCR_THINKING_LEVEL", "MAPPING_THINKING_LEVEL",
            "MAPPING_CONTEXT_CACHE_ENABLED", "MAPPING_CONTEXT_CACHE_TTL_SECONDS"}


def load_env(path: Path) -> None:
    """Read simple KEY=value entries without executing shell or printing secrets."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if sep and key.strip() in ENV_KEYS:
            value = value.strip()
            if value[:1] not in "\"'":  # giá trị không đặt trong dấu nháy: bỏ comment cuối dòng (" # ...")
                value = value.split(" #", 1)[0].rstrip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key.strip(), value)


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


def _cached_estimate(cached) -> RepairEstimate | None:
    """Đọc lại kết quả OCR từ cache; dữ liệu hỏng/không còn hợp lệ thì coi như cache miss."""
    if not isinstance(cached, dict) or "result" not in cached:
        return None
    try:
        return RepairEstimate.model_validate_json(json.dumps(cached["result"], ensure_ascii=False))
    except ValueError:
        return None


def extract_repair_estimate(
    file_path: str | Path, *, model: str, backend=None,
    prompt_path: str | Path = ROOT / "prompt.py",
    output_dir: str | Path = OUTPUT_DIR, timeout: float = 180,
    run_dir: str | Path | None = None, verbose: bool = True, cache=None,
    thinking_level: str | None = None,
) -> RepairEstimate:
    """Extract and validate OCR; preserves run artifacts.

    Provider (Claude/Gemini) chọn theo tên ``model``. Truyền ``backend`` (``LlmClients.backend(model)``)
    để dùng chung kết nối (caller sở hữu); không truyền thì tự mở client bằng API key của provider trong env.
    Does not load .env or mutate process configuration.
    API/validation errors propagate; failed responses remain in the run directory.
    ``run_dir``: thư mục lưu artifacts cố định (mặc định tạo thư mục timestamp trong ``output_dir``).
    ``verbose=False``: không in log (pipeline tự in báo cáo).
    ``thinking_level``: low|medium|high (Claude: ``effort``, Gemini: ``thinking_level``); mặc định lấy từ
    OCR_THINKING_LEVEL (trống = mặc định của model).
    ``cache``: đối tượng có ``get(key)``/``set(key, value)``; trúng cache thì không gọi model
    (chỉ kết quả đã qua validate mới được ghi vào cache).
    """
    log = print if verbose else (lambda *args, **kwargs: None)
    if timeout <= 0:
        raise ValueError("timeout phải lớn hơn 0")
    file_path, prompt_path, output_dir = Path(file_path).resolve(), Path(prompt_path), Path(output_dir)
    if not file_path.is_file() or file_path.stat().st_size == 0:
        raise ValueError(f"File không tồn tại hoặc rỗng: {file_path}")
    mime = mimetypes.guess_type(file_path)[0]
    is_markdown = file_path.suffix.lower() in {".md", ".markdown"}
    markdown_text = file_path.read_text(encoding="utf-8").strip() if is_markdown else None
    if is_markdown and not markdown_text:
        raise ValueError("Markdown rỗng")
    if not is_markdown and mime not in SUPPORTED:
        raise ValueError("Định dạng tài liệu không hỗ trợ")
    thinking_level = normalize_effort(thinking_level or os.getenv("OCR_THINKING_LEVEL"))
    prompt = load_prompt(prompt_path)
    provider = get_provider(model)
    backend_type = type(backend) if backend is not None else provider.load().Backend
    media = Media(text=markdown_text) if is_markdown else Media(path=file_path, mime=mime)
    input_mode = backend_type.input_mode(media)
    # A unique run directory preserves earlier results, including failed validation.
    if run_dir is None:
        run_dir = output_dir / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True)
    cache_key = None
    if cache is not None:
        cache_started = time.perf_counter()
        cache_key = ocr_key(file_path, model=model, prompt=prompt, user_text=OCR_USER_TEXT,
                            schema=RepairEstimate.model_json_schema(), thinking_level=thinking_level)
        cached = cache.get(cache_key)
        parsed = _cached_estimate(cached)
        if parsed is not None:
            write_json(run_dir / "result.json", cached["result"])
            elapsed = round(time.perf_counter() - cache_started, 3)
            write_json(run_dir / "metadata.json", {
                "model": model, "provider": provider.name, "input": str(file_path), "input_mode": input_mode,
                "prompt_file": str(prompt_path.resolve()), "endpoint": backend_type.endpoint,
                "status": "success", "cache_hit": True, "elapsed_seconds": elapsed,
                "cost": None,  # không gọi model nên không tốn token
            })
            log(f"Trúng cache OCR: {len(parsed.items)} hạng mục, không gọi model ({elapsed:.3f}s)")
            log(f"Thư mục lần chạy: {run_dir}")
            return parsed
    started = time.perf_counter()
    timings = {}
    succeeded = False
    cost = estimate_cost(model, None)
    with ExitStack() as stack:
        if backend is None:
            backend = stack.enter_context(LlmClients(timeout)).backend(model)
        try:
            generation = backend.extract(model=model, instruction=prompt, user_text=OCR_USER_TEXT, media=media,
                                         schema=RepairEstimate, effort=thinking_level, timings=timings)
            write_json(run_dir / "response.json", generation.response)
            cost = cost_from_response(generation.response, model)
            raw = generation.text
            (run_dir / "raw.txt").write_text(raw, encoding="utf-8")
            generation.raise_if_incomplete("OCR")
            parsed = RepairEstimate.model_validate_json(raw)
            # Preserve original JSON numeric values; validation does not recalculate money.
            write_json(run_dir / "result.json", json.loads(raw))
            succeeded = True
            if cache is not None:
                cache.set(cache_key, {"result": json.loads(raw)})
            log(f"Đã trích xuất {len(parsed.items)} hạng mục: {dict(Counter(item.category for item in parsed.items))}")
            log(f"Kết quả: {run_dir / 'result.json'}")
        finally:
            elapsed = round(time.perf_counter() - started, 3)
            write_json(run_dir / "metadata.json", {
                "model": model,
                "provider": provider.name,
                "input": str(file_path),
                "input_mode": input_mode,
                "prompt_file": str(prompt_path.resolve()),
                "endpoint": backend_type.endpoint,
                "status": "success" if succeeded else "failed",
                "elapsed_seconds": elapsed,
                "cost": cost,
                "cache_hit": False,
                **timings,
            })
            log(f"Chuẩn bị file: {timings.get('prepare_media_seconds', 0):.3f}s", flush=True)
            if "llm_request_seconds" in timings:
                log(f"Gọi {provider.name}: {timings['llm_request_seconds']:.3f}s", flush=True)
            log(f"Tổng thời gian xử lý: {elapsed:.3f}s", flush=True)
            if cost["total_usd"] is not None:
                log(f"Chi phí ước tính: ${cost['total_usd']:.6f} USD", flush=True)
            else:
                log(f"Chi phí chưa xác định: {cost['reason']}", flush=True)
            log(f"Thư mục lần chạy: {run_dir}", flush=True)
    return parsed
