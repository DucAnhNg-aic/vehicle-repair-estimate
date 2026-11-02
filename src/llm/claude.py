"""Provider Claude (Messages API): OCR và mapping trả JSON đúng schema."""
from __future__ import annotations

import base64
import io
import json
import sys

import anthropic
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener
from pydantic import BaseModel

from .base import Generation, Media, measure

NAME = "claude"  # khai báo provider (tiền tố model, API key) nằm trong llm.PROVIDERS
# Model có server-side fallback: bị safety classifier từ chối thì API tự chạy lại trên model khác
# trong cùng request. Haiku 5.5 không có fallback nên không gửi tham số này.
FALLBACK_MODELS = frozenset({"claude-sonnet-5-5"})
FALLBACK_BETA = "server-side-fallback-2026-07-01"

register_heif_opener()  # cho Pillow mở được HEIC/HEIF để chuyển sang JPEG
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}  # Claude không nhận HEIC/HEIF
INLINE_LIMIT = 15 * 1024 * 1024  # PDF lớn hơn thì upload qua Files API.
IMAGE_INLINE_LIMIT = 7 * 1024 * 1024  # Claude nhận tối đa 10 MB/ảnh sau base64.
IMAGE_MAX_SIDE = 8000  # Kích thước tối đa mỗi chiều Claude chấp nhận.
IMAGE_LONG_EDGE = 2576  # Claude tự thu ảnh lớn hơn về cạnh dài này, thu trước không mất chi tiết.
OCR_MAX_TOKENS = 64000  # gồm thinking + JSON; streaming nên không lo timeout
MAPPING_MAX_TOKENS = 32000
# Cache 1 giờ: job đến thưa vẫn trúng cache; mỗi lần trúng cache lại được gia hạn TTL.
PROMPT_CACHE_TTL = "1h"


def create_client(api_key: str | None, timeout: float) -> anthropic.Anthropic:
    """``timeout`` (giây) áp cho từng HTTP request; streaming nên chỉ tính thời gian giữa các chunk."""
    return anthropic.Anthropic(api_key=api_key, timeout=timeout)


def list_models(client) -> list[str]:
    return [entry.id for entry in client.models.list()]


def image_for_claude(data: bytes, mime: str) -> tuple[bytes, str]:
    """Ảnh Claude nhận được thì gửi nguyên; HEIC/HEIF hoặc ảnh vượt giới hạn thì chuyển JPEG
    với cạnh dài tối đa ``IMAGE_LONG_EDGE``."""
    with Image.open(io.BytesIO(data)) as image:
        if mime in IMAGE_TYPES and len(data) <= IMAGE_INLINE_LIMIT and max(image.size) <= IMAGE_MAX_SIDE:
            return data, mime
        converted = ImageOps.exif_transpose(image).convert("RGB")
    converted.thumbnail((IMAGE_LONG_EDGE, IMAGE_LONG_EDGE))
    buffer = io.BytesIO()
    converted.save(buffer, "JPEG", quality=90)
    return buffer.getvalue(), "image/jpeg"


class Backend:
    name = NAME
    endpoint = "/v1/messages"

    def __init__(self, client, *, timeout: float = 180):  # timeout: giao diện chung, Claude không cần
        self.client = client

    @staticmethod
    def input_mode(media: Media) -> str:
        if media.text is not None:
            return "markdown_text"
        return "Files API" if media.mime == "application/pdf" and media.path.stat().st_size > INLINE_LIMIT else "inline"

    def mapping_cache_tag(self, effort: str | None) -> str:
        """Phần riêng của provider trong khóa cache kết quả mapping."""
        return f"messages:v1;effort={effort}"

    def extract(self, *, model: str, instruction: str, user_text: str, media: Media,
                schema: type[BaseModel], effort: str | None, timings: dict) -> Generation:
        uploaded = None
        try:
            with measure(timings, "prepare_media_seconds"):
                if media.text is not None:
                    part = {"type": "text", "text": media.text}
                elif media.mime != "application/pdf":
                    data, media_type = image_for_claude(media.path.read_bytes(), media.mime)
                    part = {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                                        "data": base64.b64encode(data).decode("ascii")}}
                elif self.input_mode(media) == "inline":
                    part = {"type": "document", "source": {"type": "base64", "media_type": media.mime,
                                                           "data": base64.b64encode(media.path.read_bytes()).decode("ascii")}}
                else:
                    uploaded = self.client.files.upload(file=media.path)
                    part = {"type": "document", "source": {"type": "file", "file_id": uploaded.id}}
            with measure(timings, "llm_request_seconds"):
                return self._request(model=model, system=instruction, schema=schema, effort=effort,
                                     max_tokens=OCR_MAX_TOKENS, content=[part, {"type": "text", "text": user_text}])
        finally:
            if uploaded is not None:
                try:
                    self.client.files.delete(uploaded.id)
                except Exception:
                    print(f"Không xóa được file tạm {uploaded.id} trên Files API của Claude.", file=sys.stderr)

    def select(self, *, model: str, instruction: str, context: dict, request: dict,
               schema: type[BaseModel], effort: str | None, audit: dict) -> Generation:
        """``context`` (codebook) nằm ở system prompt có ``cache_control``: request sau có cùng prefix,
        kể cả từ job/worker khác, đọc lại từ prompt cache; chỉ ``request`` (items) là gửi mới."""
        audit["endpoint"] = "messages"
        system = [{"type": "text", "text": instruction},
                  {"type": "text", "text": json.dumps(context, ensure_ascii=False, separators=(",", ":")),
                   "cache_control": {"type": "ephemeral", "ttl": PROMPT_CACHE_TTL}}]
        content = [{"type": "text", "text": json.dumps(request, ensure_ascii=False, separators=(",", ":"))}]
        return self._request(model=model, system=system, content=content, schema=schema, effort=effort,
                             max_tokens=MAPPING_MAX_TOKENS)

    def settle_cost(self, audit: dict) -> None:
        """Prompt cache của Claude đã tính trong token usage; không có phí riêng."""

    def _request(self, *, model, system, content, schema, effort, max_tokens) -> Generation:
        output_config: dict = {"format": {"type": "json_schema", "schema": anthropic.transform_schema(schema)}}
        if effort:
            output_config["effort"] = effort
        params = dict(model=model, max_tokens=max_tokens, system=system, output_config=output_config,
                      messages=[{"role": "user", "content": content}])
        if model in FALLBACK_MODELS:
            params.update(betas=[FALLBACK_BETA], fallbacks="default")
        # Streaming: request dài (thinking + JSON lớn) không bị cắt bởi read timeout của một response duy nhất.
        with self.client.beta.messages.stream(**params) as stream:
            message = stream.get_final_message()
        text = "".join(block.text for block in message.content if block.type == "text")
        response = message.model_dump(mode="json", exclude_none=True)
        return Generation(response=response, text=text, problem=_problem(response, text))


def _problem(response: dict, text: str) -> str | None:
    stop_reason = response.get("stop_reason")
    if stop_reason == "refusal":
        category = (response.get("stop_details") or {}).get("category")
        return f"Claude từ chối (category: {category})"
    if stop_reason != "end_turn" or not text:
        return f"Claude chưa hoàn thành (stop_reason: {stop_reason}) hoặc trả rỗng"
    return None
