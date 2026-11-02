"""Unified codebook and prompt for batched repair-estimate mapping."""
from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from pathlib import Path

from openpyxl import load_workbook

from cost import cost_from_response, estimate_cost, sum_costs
from llm import normalize_effort
from llm_cache import mapping_context, mapping_item_payload, mapping_key
from ocr.schema import RepairEstimate, RepairEstimateItem
from .prompt import MAPPING_INSTRUCTION
from .schema import CodebookRow, MappingResult, WorkBatchSelection

Selection = tuple[str, str] | None  # (item_type, name) trong codebook, hoặc None = không khớp
CandidateNames = dict[str, list[str]]  # item_type -> tên trong codebook

CODEBOOK_DIR = Path(__file__).resolve().parents[2] / "data" / "codebook"


class Codebook:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.by_type = {"part": defaultdict(list), "service": defaultdict(list)}
        workbook = load_workbook(self.path, read_only=True, data_only=True)
        try:
            for sheet in workbook:
                rows = iter(sheet.values)
                headers = next(rows, ())
                if "MA" not in headers or "TEN" not in headers:
                    raise ValueError(f"Thiếu cột MA/TEN: {self.path}, {sheet.title}")
                ci, ni = headers.index("MA"), headers.index("TEN")
                if "item_type" not in headers:
                    raise ValueError(f"Thiếu cột item_type: {self.path}, {sheet.title}")
                ti = headers.index("item_type")
                for number, values in enumerate(rows, 2):
                    code, name = values[ci], values[ni]
                    if code is None and name is None:
                        continue
                    if not isinstance(code, str) or not isinstance(name, str) or not code.strip() or not name.strip():
                        raise ValueError(f"MA/TEN không hợp lệ: {self.path}, {sheet.title}:{number}")
                    item_type = values[ti]
                    if item_type not in self.by_type:
                        raise ValueError(f"item_type không hợp lệ: {self.path}, {sheet.title}:{number}")
                    self.by_type[item_type][name].append(
                        CodebookRow(code=code, name=name, row=number, item_type=item_type))
        finally:
            workbook.close()
        if not any(self.by_type.values()):
            raise ValueError(f"Codebook rỗng: {self.path}")


class LlmMatcher:
    """Shared-client fallback; one request maps a whole batch of items against one codebook.

    Không phụ thuộc hãng: mọi lời gọi đi qua ``backend`` (``LlmClients.backend(model)``) của provider.
    Lời dặn + danh sách tên codebook là phần tĩnh để provider tự cache (Claude: prompt caching,
    Gemini: context cache); mỗi request chỉ gửi mới ``items``.
    Records usage, cost and duration for each request in ``calls``.
    """
    INSTRUCTION = MAPPING_INSTRUCTION

    def __init__(self, backend, model: str, cache=None, *, thinking_level: str | None = None):
        self.backend, self.model, self.cache = backend, model, cache
        self.method = backend.name  # ghi vào ``mapping.method`` của item được map
        self.thinking_level = normalize_effort(thinking_level or os.getenv("MAPPING_THINKING_LEVEL") or "low")
        self.calls: list[dict] = []
        self.cache_hits = self.cache_misses = 0  # số item được/không được phục vụ từ cache

    def select_many(self, items: list[RepairEstimateItem], names: CandidateNames) -> list[Selection]:
        """Một prompt chung cho mọi category; ``names``: item_type -> tên trong codebook.
        Trả (item_type, name) hoặc None cho mỗi item, giữ thứ tự.

        Có ``cache``: item đã có kết quả được lấy từ cache, chỉ item còn lại (bỏ trùng) mới gửi model;
        kết quả mới chỉ được ghi cache sau khi qua mọi bước kiểm tra.
        """
        valid = {(group, name) for group, entries in names.items() for name in entries}
        context_names = [{"item_type": group, "name": name} for group, entries in names.items() for name in entries]
        if self.cache is None:
            return self._request(items, names)[0]
        context = mapping_context(model=self.model,
            instructions=self.INSTRUCTION + "\n" + self.backend.mapping_cache_tag(self.thinking_level) + "\n"
            + json.dumps(WorkBatchSelection.model_json_schema(), sort_keys=True),
            names=context_names)
        keys = [mapping_key(context, item.model_dump(mode="json")) for item in items]
        selected: list[Selection] = [None] * len(items)
        missing: dict[str, list[int]] = {}  # khóa -> vị trí; item trùng khóa chỉ gửi một lần
        for pos, key in enumerate(keys):
            cached = self.cache.get(key)
            name = cached.get("name") if isinstance(cached, dict) and "name" in cached else False
            if isinstance(name, list) and all(isinstance(v, str) for v in name):  # Redis/Mongo lưu tuple thành list
                name = tuple(name)
            if name is None or (isinstance(name, tuple) and name in valid
                                and (items[pos].category != "material" or name[0] == "part")):
                selected[pos] = name
                self.cache_hits += 1
            else:  # miss, hoặc giá trị cache không còn thuộc codebook
                missing.setdefault(key, []).append(pos)
        if missing:
            fresh, invalid = self._request([items[positions[0]] for positions in missing.values()], names)
            for index, ((key, positions), name) in enumerate(zip(missing.items(), fresh)):
                if index not in invalid:  # tên model trả sai thành null chỉ là tạm thời, không ghi cache
                    self.cache.set(key, {"name": name})
                for pos in positions:
                    selected[pos] = name
                self.cache_misses += len(positions)
        return selected

    def _request(self, items: list[RepairEstimateItem], names: CandidateNames) -> tuple[list[Selection], set[int]]:
        """Gửi một request cho ``items`` bằng prompt chung.

        Trả (kết quả, vị trí các item mà model trả tên ngoài codebook); các item đó được đặt null.
        """
        started = time.perf_counter()
        audit = {"items": [item.raw_name for item in items], "categories": sorted({item.category for item in items}),
                 "prompt": "mapping", "provider": self.backend.name, "model": self.model, "status": "failed"}
        try:
            request = {"items": [dict(mapping_item_payload(item.model_dump(mode="json")), index=i)
                                 for i, item in enumerate(items)]}
            context = {"names": [{"name": name, "item_type": group} for group, entries in names.items()
                                 for name in entries]}
            audit["thinking_level"] = self.thinking_level
            generation = self.backend.select(
                model=self.model, instruction=self.INSTRUCTION, context=context,
                request=request, schema=WorkBatchSelection, effort=self.thinking_level, audit=audit)
            audit["response"] = generation.response
            audit["cost"] = cost_from_response(generation.response, self.model)
            generation.raise_if_incomplete("mapping")
            raw = generation.text
            selections = WorkBatchSelection.model_validate_json(raw).selections
            indexes = [s.index for s in selections]
            if sorted(indexes) != list(range(len(items))):
                raise ValueError(f"Model trả thiếu/thừa/trùng index (cần 0..{len(items) - 1}): {indexes}")
            if any((s.name is None) != (s.item_type is None) for s in selections):
                raise ValueError("name và item_type phải cùng null hoặc cùng có giá trị")
            by_index = {s.index: (s.item_type, s.name) if s.name is not None else None for s in selections}
            valid = {(group, name) for group, entries in names.items() for name in entries}
            # Lựa chọn không dùng được -> null cho riêng item đó, không làm hỏng cả batch:
            # tên ngoài codebook (model tự diễn đạt lại) hoặc vật tư bị map sang loại khác ngoài "part".
            reasons = {i: "ngoài codebook" for i, name in by_index.items() if name is not None and name not in valid}
            reasons.update({i: "vật tư chỉ được map với item_type part" for i, item in enumerate(items)
                            if i not in reasons and item.category == "material"
                            and by_index[i] is not None and by_index[i][0] != "part"})
            invalid = set(reasons)
            if invalid:
                audit["invalid_selections"] = [
                    {"index": i, "item": items[i].raw_name, "returned": by_index[i], "reason": reasons[i]}
                    for i in sorted(invalid)]
                by_index = {i: (None if i in invalid else name) for i, name in by_index.items()}
            audit["status"] = "success"
            return [by_index[i] for i in range(len(items))], invalid
        finally:
            audit.setdefault("cost", estimate_cost(self.model, None))
            self.backend.settle_cost(audit)
            audit["elapsed_seconds"] = round(time.perf_counter() - started, 3)
            self.calls.append(audit)

    def cost_summary(self) -> dict:
        """Tổng chi phí mọi lời gọi fallback đã thực hiện."""
        return sum_costs(call["cost"] for call in self.calls)


class CodebookMapper:
    BATCH_SIZE = 50  # số item tối đa mỗi request LLM

    def __init__(self, *, matcher=None, codebook_dir: str | Path = CODEBOOK_DIR):
        root = Path(codebook_dir)
        self.work = Codebook(root / "codebook.xlsx")
        self.matcher = matcher

    def _result(self, item: RepairEstimateItem, matches: list[CodebookRow], method: str) -> MappingResult:
        return MappingResult(raw_name=item.raw_name, category=item.category,
                             codebook=self.work.path.name, method=method, matches=matches)

    def map_items(self, items: list[RepairEstimateItem]) -> list[MappingResult]:
        """Chỉ exact vật tư; công việc và vật tư chưa khớp gửi LLM theo batch
        (tối đa ``BATCH_SIZE`` item/request). Trả một kết quả cho mỗi item, đúng thứ tự."""
        results: list[MappingResult | None] = [None] * len(items)
        pending: list[int] = []
        for pos, item in enumerate(items):
            matches = self.work.by_type["part"].get(item.raw_name, []) if item.category == "material" else []
            if matches:
                results[pos] = self._result(item, matches, "exact")
            elif self.matcher is None:
                results[pos] = self._result(item, [], "unmatched")
            else:
                pending.append(pos)
        names = {kind: list(rows) for kind, rows in self.work.by_type.items()}
        for start in range(0, len(pending), self.BATCH_SIZE):
            chunk = pending[start:start + self.BATCH_SIZE]
            selected = self.matcher.select_many([items[pos] for pos in chunk], names)
            if len(selected) != len(chunk):
                raise ValueError("Matcher trả sai số lượng kết quả")
            for pos, name in zip(chunk, selected):
                if name is None:
                    results[pos] = self._result(items[pos], [], "unmatched")
                    continue
                if not isinstance(name, tuple) or len(name) != 2:
                    raise ValueError("Kết quả mapping thiếu item_type")
                group, selected_name = name
                if items[pos].category == "material" and group != "part":  # matcher tùy biến trả sai loại
                    results[pos] = self._result(items[pos], [], "unmatched")
                    continue
                matches = self.work.by_type.get(group, {}).get(selected_name, [])
                if not matches:
                    raise ValueError(f"Tên/loại trả về không thuộc codebook: {name}")
                results[pos] = self._result(items[pos], matches, self.matcher.method)
        return results  # type: ignore[return-value]

    def map_repair_estimate(self, estimate: RepairEstimate) -> list[MappingResult]:
        """One result per OCR item, in original order (including unmatched items)."""
        return self.map_items(estimate.items)
