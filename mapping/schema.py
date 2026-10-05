"""Schemas for codebook mapping and Gemini batch fallback."""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from ocr.schema import StrictModel


class CodebookRow(StrictModel):
    code: str
    name: str
    row: int


class MappingResult(StrictModel):
    raw_name: str
    category: Literal["material", "labor", "paint"]
    codebook: str
    method: Literal["exact", "gemini", "unmatched"]
    matches: list[CodebookRow]


class ItemSelection(StrictModel):
    index: int = Field(description="Chỉ số của hạng mục trong danh sách `items` đầu vào (bắt đầu từ 0).")
    name: str | None = Field(description="Một tên nguyên văn trong danh sách `names`, hoặc null nếu không có tên tương ứng.")


class BatchSelection(StrictModel):
    selections: list[ItemSelection] = Field(description="Đúng một kết quả cho mỗi hạng mục đầu vào.")
