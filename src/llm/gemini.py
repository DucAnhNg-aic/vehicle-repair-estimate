"""Provider Gemini: OCR qua Interactions API, mapping qua generateContent + context cache."""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path

from google import genai
from google.genai import errors, types
from pydantic import BaseModel

from .base import Generation, Media, measure
from .gemini_context_cache import ContextCache

NAME = "gemini"  # khai báo provider (tiền tố model, API key) nằm trong llm.PROVIDERS
INLINE_LIMIT = 15 * 1024 * 1024  # Leave room for base64 expansion and prompt.
CONTEXT_CACHE_DIR = Path(__file__).resolve().parents[2] / "outputs" / ".gemini_context_cache"


def create_client(api_key: str | None, timeout: float) -> genai.Client:
    return genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=int(timeout * 1000)))


def list_models(client) -> list[str]:
    return [entry.name for entry in client.models.list()]


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


class Backend:
    name = NAME
    endpoint = "/v1beta/interactions"

    def __init__(self, client, *, timeout: float = 180, context_cache_dir: str | Path | None = None,
                 context_cache_enabled: bool | None = None):
        self.client, self.timeout = client, timeout
        if context_cache_enabled is None:
            context_cache_enabled = os.getenv("MAPPING_CONTEXT_CACHE_ENABLED", "true").lower() in {"true", "1", "yes"}
        self.context_cache_enabled = context_cache_enabled
        self.context_cache_dir = Path(context_cache_dir or CONTEXT_CACHE_DIR)
        self._context_caches: dict[str, ContextCache] = {}  # theo model; tự tắt khi Google cache lỗi

    @staticmethod
    def input_mode(media: Media) -> str:
        if media.text is not None:
            return "markdown_text"
        return "inline" if media.path.stat().st_size <= INLINE_LIMIT else "Files API"

    def mapping_cache_tag(self, effort: str | None) -> str:
        """Phần riêng của provider trong khóa cache kết quả mapping (giữ nguyên khóa cũ của Gemini)."""
        return f"generateContent:v1;thinking={(effort or 'low').upper()}"

    def extract(self, *, model: str, instruction: str, user_text: str, media: Media,
                schema: type[BaseModel], effort: str | None, timings: dict) -> Generation:
        uploaded = None
        try:
            with measure(timings, "prepare_media_seconds"):
                if media.text is not None:
                    part = {"type": "text", "text": media.text}
                else:
                    part = {"type": "document" if media.mime == "application/pdf" else "image", "mime_type": media.mime}
                    if self.input_mode(media) == "inline":
                        part["data"] = base64.b64encode(media.path.read_bytes()).decode("ascii")
                    else:
                        uploaded = self.client.files.upload(file=str(media.path), config={"mime_type": media.mime})
                        part["uri"] = wait_for_file(self.client, uploaded, self.timeout).uri
            with measure(timings, "llm_request_seconds"):
                interaction = self.client.interactions.create(
                    model=model,
                    system_instruction=instruction,
                    input=[{"type": "text", "text": user_text}, part],
                    response_format={"type": "text", "mime_type": "application/json", "schema": schema.model_json_schema()},
                    store=False,
                    **({"generation_config": {"thinking_level": effort}} if effort else {}),
                )
            text = interaction.output_text or ""
            problem = None
            if interaction.status != "completed":
                problem = f"Gemini interaction chưa hoàn thành ({interaction.status})"
            elif not text:
                problem = "Gemini trả nội dung rỗng"
            return Generation(response=interaction.model_dump(mode="json", exclude_none=True), text=text, problem=problem)
        finally:
            if uploaded is not None:
                try:
                    self.client.files.delete(name=uploaded.name)
                except Exception:
                    print("Không xóa được file tạm trên Gemini; file sẽ tự hết hạn theo Files API.", file=sys.stderr)

    def _context_cache(self, model: str) -> ContextCache | None:
        if not self.context_cache_enabled:
            return None
        if model not in self._context_caches:
            self._context_caches[model] = ContextCache(
                self.client, model, self.context_cache_dir,
                ttl_seconds=int(os.getenv("MAPPING_CONTEXT_CACHE_TTL_SECONDS", "1800")))
        return self._context_caches[model]

    def select(self, *, model: str, instruction: str, context: dict, request: dict,
               schema: type[BaseModel], effort: str | None, audit: dict) -> Generation:
        """``context`` (codebook) nằm trong Google context cache dùng chung giữa các job; lỗi cache thì
        gửi kèm đầy đủ trong request."""
        schema_json = schema.model_json_schema()
        context_cache = self._context_cache(model)
        cached = context_cache.acquire(instruction, context, schema_json) if context_cache else None
        audit["context_cache"] = cached
        audit["endpoint"] = "generateContent"

        def generate(cached):
            config = {"response_mime_type": "application/json", "response_json_schema": schema_json,
                      "http_options": types.HttpOptions(extra_body={"store": False})}
            if effort:
                config["thinking_config"] = types.ThinkingConfig(thinking_level=effort.upper())
            if cached:
                config["cached_content"] = cached["name"]
                contents = request
            else:
                config["system_instruction"] = instruction
                contents = {**context, **request}
            return self.client.models.generate_content(
                model=model, contents=json.dumps(contents, ensure_ascii=False, separators=(",", ":")),
                config=types.GenerateContentConfig(**config),
            )

        try:
            response = generate(cached)
        except errors.ClientError as exc:
            if cached is None or exc.code != 404:
                raise
            # A cachedContent may be deleted before its recorded expiry. Retry once with full context.
            context_cache.invalidate(instruction, context, schema_json)
            audit["context_cache_fallback"] = "not_found"
            response = generate(None)
        text = response.text
        candidates = getattr(response, "candidates", None)
        finish = str(getattr(candidates[0].finish_reason, "value", candidates[0].finish_reason)) if candidates else None
        problem = None if finish == "STOP" and text else f"Gemini chưa hoàn thành, bị chặn hoặc trả rỗng (finish_reason: {finish})"
        return Generation(response=response.model_dump(mode="json", exclude_none=True), text=text or "", problem=problem)

    def settle_cost(self, audit: dict) -> None:
        """Cộng phí lưu context cache (tính một lần lúc tạo) vào chi phí lời gọi, kể cả khi request lỗi."""
        context = audit.get("context_cache")
        if not (context and context.get("created")):
            return
        storage = context.get("storage_estimated_usd")
        audit["cost"]["context_cache_storage_complete"] = storage is not None
        audit["cost"]["context_cache_storage_usd"] = storage or 0.0
        if audit["cost"].get("total_usd") is not None:
            audit["cost"]["total_usd"] += storage or 0.0
