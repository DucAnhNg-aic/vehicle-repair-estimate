"""Schemas for codebook mapping and LLM batch fallback."""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from ocr.schema import StrictModel


class CodebookRow(StrictModel):
    code: str
    name: str
    row: int
    item_type: Literal["part", "service"] | None = None


class MappingResult(StrictModel):
    raw_name: str
    category: Literal["material", "labor", "paint"]
    codebook: str
    method: Literal["exact", "claude", "gemini", "unmatched"]  # claude/gemini: provider đã map item
    matches: list[CodebookRow]


class WorkItemSelection(StrictModel):
    index: int = Field(description="Chỉ số của hạng mục trong danh sách `items` đầu vào (bắt đầu từ 0).")
    name: str | None = Field(description="Một tên nguyên văn trong danh sách `names`, hoặc null nếu không có tên tương ứng.")
    item_type: Literal["part", "service"] | None = Field(
        description="Nhóm chứa name đã chọn; null khi name null.")


class WorkBatchSelection(StrictModel):
    selections: list[WorkItemSelection]
