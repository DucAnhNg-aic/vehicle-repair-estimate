"""Giao diện chung mà mỗi module provider (``llm.claude``, ``llm.gemini``) cài đặt.

    create_client(api_key, timeout)   client SDK, có ``close()``
    list_models(client)               tên các model key hiện tại truy cập được
    Backend(client, *, timeout, ...)  bọc client:
        name                          tên provider, cũng là ``mapping.method`` của item do provider map
        endpoint                      API dùng cho OCR (ghi vào metadata)
        input_mode(media)             "markdown_text" | "inline" | "Files API"
        extract(...) -> Generation    OCR: instruction + tài liệu -> JSON theo schema
        select(...) -> Generation     mapping: instruction + context tĩnh (codebook, provider tự cache) + request
        mapping_cache_tag(effort)     phần riêng của provider trong khóa cache kết quả mapping
        settle_cost(audit)            cộng chi phí riêng của provider (nếu có) vào ``audit["cost"]``

OCR/mapping chỉ làm việc với ``Backend`` nên không phụ thuộc SDK của hãng nào.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

EFFORTS = ("low", "medium", "high")  # None = mặc định của model


@contextmanager
def measure(timings: dict, name: str):
    started = time.perf_counter()
    try:
        yield
    finally:
        timings[name] = round(time.perf_counter() - started, 3)


def normalize_effort(value: str | None) -> str | None:
    effort = (value or "").strip().lower() or None
    if effort not in {None, *EFFORTS}:
        raise ValueError(f"Mức suy luận phải là một trong {', '.join(EFFORTS)}: {value}")
    return effort


@dataclass(frozen=True)
class Media:
    """Tài liệu đầu vào của OCR: file ảnh/PDF (``path`` + ``mime``) hoặc Markdown đã đọc (``text``)."""
    path: Path | None = None
    mime: str | None = None
    text: str | None = None


@dataclass(frozen=True)
class Generation:
    """Kết quả một request: ``response`` (dict để lưu artifact), ``text`` (JSON model trả về).

    ``problem`` khác None khi response bị từ chối/cắt/rỗng; caller lưu response trước rồi mới
    gọi ``raise_if_incomplete`` để lần lỗi vẫn còn artifact điều tra.
    """
    response: dict
    text: str
    problem: str | None = None

    def raise_if_incomplete(self, what: str) -> None:
        if self.problem:
            raise RuntimeError(f"{what}: {self.problem}; xem response đã lưu.")
