"""Category-routed exact matching with an injectable Gemini fallback."""
from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path

from openpyxl import load_workbook

from cost import cost_from_response, estimate_cost, sum_costs
from ocr.schema import Quotation, QuotationItem
from .prompt import SYSTEM_INSTRUCTION
from .schema import BatchSelection, CodebookRow, ItemSelection, MappingResult

CODEBOOK_DIR = Path(__file__).resolve().parents[1] / "codebook"


class Codebook:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.by_name: dict[str, list[CodebookRow]] = defaultdict(list)
        workbook = load_workbook(self.path, read_only=True, data_only=True)
        try:
            for sheet in workbook:
                rows = iter(sheet.values)
                headers = next(rows, ())
                if "MA" not in headers or "TEN" not in headers:
                    raise ValueError(f"Thiếu cột MA/TEN: {self.path}, {sheet.title}")
                ci, ni = headers.index("MA"), headers.index("TEN")
                for number, values in enumerate(rows, 2):
                    code, name = values[ci], values[ni]
                    if code is None and name is None:
                        continue
                    if not isinstance(code, str) or not isinstance(name, str) or not code.strip() or not name.strip():
                        raise ValueError(f"MA/TEN không hợp lệ: {self.path}, {sheet.title}:{number}")
                    self.by_name[name].append(CodebookRow(code=code, name=name, row=number))
        finally:
            workbook.close()
        if not self.by_name:
            raise ValueError(f"Codebook rỗng: {self.path}")


class GeminiMatcher:
    """Shared-client fallback; one request maps a whole batch of items against one codebook.

    Records usage, cost and duration for each request in ``calls``.
    """
    SYSTEM_INSTRUCTION = SYSTEM_INSTRUCTION

    def __init__(self, client, model: str):
        self.client, self.model = client, model
        self.calls: list[dict] = []

    def select_many(self, items: list[QuotationItem], names: list[str]) -> list[str | None]:
        """Một request cho cả ``items``; trả về tên (hoặc None) theo đúng thứ tự ``items``."""
        started = time.perf_counter()
        audit = {"items": [item.raw_name for item in items], "categories": sorted({item.category for item in items}),
                 "model": self.model, "status": "failed"}
        try:
            payload = {"items": [dict(item.model_dump(mode="json"), index=i) for i, item in enumerate(items)],
                       "names": names}
            response = self.client.interactions.create(
                model=self.model, system_instruction=self.SYSTEM_INSTRUCTION,
                input=json.dumps(payload, ensure_ascii=False),
                response_format={"type": "text", "mime_type": "application/json", "schema": BatchSelection.model_json_schema()},
                store=False,
            )
            audit["response"] = response.model_dump(mode="json", exclude_none=True)
            audit["cost"] = cost_from_response(audit["response"], self.model)
            if response.status != "completed" or not response.output_text:
                raise RuntimeError("Gemini mapping chưa hoàn thành hoặc trả rỗng")
            selections = BatchSelection.model_validate_json(response.output_text).selections
            indexes = [s.index for s in selections]
            if sorted(indexes) != list(range(len(items))):
                raise ValueError(f"Gemini trả thiếu/thừa/trùng index (cần 0..{len(items) - 1}): {indexes}")
            by_index = {s.index: s.name for s in selections}
            if any(name is not None and name not in names for name in by_index.values()):
                raise ValueError("Gemini trả tên ngoài codebook")
            audit["status"] = "success"
            return [by_index[i] for i in range(len(items))]
        finally:
            audit.setdefault("cost", estimate_cost(self.model, None))
            audit["elapsed_seconds"] = round(time.perf_counter() - started, 3)
            self.calls.append(audit)

    def select(self, item: QuotationItem, names: list[str]) -> str | None:
        """Map một item (một request)."""
        return self.select_many([item], names)[0]

    def cost_summary(self) -> dict:
        """Tổng chi phí mọi lời gọi fallback đã thực hiện."""
        return sum_costs(call["cost"] for call in self.calls)


class CodebookMapper:
    BATCH_SIZE = 50  # số item tối đa mỗi request Gemini

    def __init__(self, *, matcher=None, codebook_dir: str | Path = CODEBOOK_DIR):
        root = Path(codebook_dir)
        self.service = Codebook(root / "service_codebook.xlsx")
        self.part = Codebook(root / "part_codebook.xlsx")
        self.matcher = matcher

    def book_for(self, item: QuotationItem) -> Codebook:
        return self.part if item.category == "material" else self.service

    def _result(self, item: QuotationItem, matches: list[CodebookRow], method: str) -> MappingResult:
        return MappingResult(raw_name=item.raw_name, category=item.category,
                             codebook=self.book_for(item).path.name, method=method, matches=matches)

    def map_item(self, item: QuotationItem) -> MappingResult:
        """Map một item (exact, rồi một request Gemini nếu không khớp)."""
        return self.map_items([item])[0]

    def map_items(self, items: list[QuotationItem]) -> list[MappingResult]:
        """Exact match trước; các item còn lại gửi Gemini theo batch, mỗi codebook một request
        (tối đa ``BATCH_SIZE`` item/request). Trả một kết quả cho mỗi item, đúng thứ tự."""
        results: list[MappingResult | None] = [None] * len(items)
        pending: dict[int, list[int]] = {}  # id(codebook) -> vị trí các item chưa khớp
        for pos, item in enumerate(items):
            matches = self.book_for(item).by_name.get(item.raw_name, [])
            if matches:
                results[pos] = self._result(item, matches, "exact")
            elif self.matcher is None:
                results[pos] = self._result(item, [], "unmatched")
            else:
                pending.setdefault(id(self.book_for(item)), []).append(pos)
        for book in (self.service, self.part):  # thứ tự cố định
            positions = pending.get(id(book), [])
            names = list(book.by_name)
            for start in range(0, len(positions), self.BATCH_SIZE):
                chunk = positions[start:start + self.BATCH_SIZE]
                selected = self.matcher.select_many([items[pos] for pos in chunk], names)
                if len(selected) != len(chunk):
                    raise ValueError("Matcher trả sai số lượng kết quả")
                for pos, name in zip(chunk, selected):
                    if name is None:
                        results[pos] = self._result(items[pos], [], "unmatched")
                    elif name not in book.by_name:
                        raise ValueError(f"Tên trả về không thuộc codebook: {name}")
                    else:
                        results[pos] = self._result(items[pos], book.by_name[name], "gemini")
        return results  # type: ignore[return-value]

    def map_quotation(self, quotation: Quotation) -> list[MappingResult]:
        """One result per OCR item, in original order (including unmatched items)."""
        return self.map_items(quotation.items)
