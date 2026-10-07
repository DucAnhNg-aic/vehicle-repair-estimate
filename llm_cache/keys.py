"""Khóa cache cho các lời gọi LLM.

Mỗi khóa là sha256 của mọi thứ có thể làm đổi kết quả: nội dung input, model, prompt, schema
(và danh sách tên codebook với mapping). Đổi bất kỳ thứ nào thì khóa đổi, cache cũ tự mất hiệu lực.
"""
from __future__ import annotations

import hashlib
import json
import unicodedata
from pathlib import Path
from typing import Any, Iterable

KEY_VERSION = "v1"  # tăng khi đổi cấu trúc khóa/value để vô hiệu hóa toàn bộ cache cũ

# Các trường của item mà quyết định mapping phụ thuộc vào. Cố ý bỏ mã dòng, số lượng, tiền
# và đoạn trích gốc: chúng khác nhau giữa các báo giá nhưng không đổi tên codebook được chọn.
MAPPING_ITEM_FIELDS = (
    "raw_section", "section_type", "raw_name", "operation", "vehicle_part", "item_name", "category",
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _normalize(value: Any) -> Any:
    """NFC + gộp khoảng trắng; không đổi hoa/thường để không gộp nhầm hai tên khác nhau."""
    if isinstance(value, str):
        return " ".join(unicodedata.normalize("NFC", value).split())
    return value


def file_digest(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ocr_key(file_path: str | Path, *, model: str, prompt: str, user_text: str, schema: dict) -> str:
    """Khóa cache OCR: nội dung file + model + prompt + câu lệnh người dùng + schema."""
    payload = {
        "kind": "ocr", "version": KEY_VERSION, "file": file_digest(file_path), "model": model,
        "prompt": _sha256(prompt.encode("utf-8")), "user_text": user_text,
        "schema": _sha256(_canonical(schema)),
    }
    return "ocr:" + _sha256(_canonical(payload))


def mapping_context(*, model: str, instructions: str, names: Iterable[str]) -> str:
    """Phần dùng chung cho cả batch mapping; tính một lần rồi truyền cho ``mapping_key``."""
    payload = {
        "version": KEY_VERSION, "model": model, "instructions": _sha256(instructions.encode("utf-8")),
        "names": _sha256(_canonical(list(names))),  # đúng thứ tự gửi cho model
    }
    return _sha256(_canonical(payload))


def mapping_key(context: str, item_fields: dict) -> str:
    """Khóa cache mapping cho một item trong ngữ cảnh ``context``."""
    item = {name: _normalize(item_fields.get(name)) for name in MAPPING_ITEM_FIELDS}
    return "mapping:" + _sha256(_canonical({"context": context, "item": item}))

