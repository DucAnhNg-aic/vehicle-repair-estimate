"""Unified codebook and prompt for batched repair-estimate mapping."""
from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from pathlib import Path

from google.genai import types, errors

from openpyxl import load_workbook

from cost import cost_from_response, estimate_cost, sum_costs
from llm_cache import mapping_context, mapping_item_payload, mapping_key
from ocr.schema import RepairEstimate, RepairEstimateItem
from .context_cache import ContextCache
from .prompt import MAPPING_INSTRUCTION
from .schema import BatchSelection, CodebookRow, MappingResult, WorkBatchSelection

Selection = str | tuple[str, str] | None
CandidateNames = list[str] | dict[str, list[str]]

CODEBOOK_DIR = Path(__file__).resolve().parents[2] / "data" / "codebook"


class Codebook:
    def __init__(self, path: str | Path, *, typed: bool = False):
        self.path = Path(path)
        self.by_name: dict[str, list[CodebookRow]] = defaultdict(list)
        self.by_type = {"part": defaultdict(list), "service": defaultdict(list)}
        workbook = load_workbook(self.path, read_only=True, data_only=True)
        try:
            for sheet in workbook:
                rows = iter(sheet.values)
                headers = next(rows, ())
                if "MA" not in headers or "TEN" not in headers:
                    raise ValueError(f"Thiếu cột MA/TEN: {self.path}, {sheet.title}")
                ci, ni = headers.index("MA"), headers.index("TEN")
                if typed and "item_type" not in headers:
                    raise ValueError(f"Thiếu cột item_type: {self.path}, {sheet.title}")
                ti = headers.index("item_type") if typed else None
                for number, values in enumerate(rows, 2):
                    code, name = values[ci], values[ni]
                    if code is None and name is None:
                        continue
                    if not isinstance(code, str) or not isinstance(name, str) or not code.strip() or not name.strip():
                        raise ValueError(f"MA/TEN không hợp lệ: {self.path}, {sheet.title}:{number}")
                    item_type = values[ti] if ti is not None else "part"
                    if item_type not in self.by_type:
                        raise ValueError(f"item_type không hợp lệ: {self.path}, {sheet.title}:{number}")
                    row = CodebookRow(code=code, name=name, row=number, item_type=item_type)
                    self.by_name[name].append(row)
                    self.by_type[item_type][name].append(row)
        finally:
            workbook.close()
        if not self.by_name:
            raise ValueError(f"Codebook rỗng: {self.path}")


class GeminiMatcher:
    """Shared-client fallback; one request maps a whole batch of items against one codebook.

    Records usage, cost and duration for each request in ``calls``.
    """
    INSTRUCTION = MAPPING_INSTRUCTION

    def __init__(self, client, model: str, cache=None, *, context_cache_dir=None, context_cache_enabled=None,
                 thinking_level: str | None = None):
        self.client, self.model, self.cache = client, model, cache
        enabled = (os.getenv("MAPPING_CONTEXT_CACHE_ENABLED", "true").lower() in {"true", "1", "yes"}
                   if context_cache_enabled is None else context_cache_enabled)
        self.context_cache = ContextCache(
            client, model, context_cache_dir or CODEBOOK_DIR.parents[1] / "outputs" / ".gemini_context_cache",
            ttl_seconds=int(os.getenv("MAPPING_CONTEXT_CACHE_TTL_SECONDS", "1800")),
        ) if enabled else None
        self.thinking_level = (thinking_level or os.getenv("MAPPING_THINKING_LEVEL") or "LOW").strip().upper()
        if self.thinking_level not in {"MINIMAL", "LOW", "MEDIUM", "HIGH"}:
            raise ValueError("MAPPING_THINKING_LEVEL phải là MINIMAL, LOW, MEDIUM hoặc HIGH")
        self.calls: list[dict] = []
        self.cache_hits = self.cache_misses = 0  # số item được/không được phục vụ từ cache

    def select_many(self, items: list[RepairEstimateItem], names: CandidateNames) -> list[Selection]:
        """Một prompt chung cho mọi category; names chứa tên và item_type.
        Trả tên cho vật tư, (item_type, name) cho công việc, hoặc None; giữ thứ tự.

        Có ``cache``: item đã có kết quả được lấy từ cache, chỉ item còn lại (bỏ trùng) mới gửi Gemini;
        kết quả mới chỉ được ghi cache sau khi qua mọi bước kiểm tra.
        """
        typed = isinstance(names, dict)
        valid = {(group, name) for group, entries in names.items() for name in entries} if typed else set(names)
        context_names = [{"item_type": group, "name": name}
                         for group, entries in names.items() for name in entries] if typed else names
        if self.cache is None:
            return self._request(items, names)
        schema = WorkBatchSelection if typed else BatchSelection
        context = mapping_context(model=self.model,
            instructions=self.INSTRUCTION + "\ngenerateContent:v1;thinking=" + self.thinking_level + "\n" + json.dumps(schema.model_json_schema(), sort_keys=True),
            names=context_names)
        keys = [mapping_key(context, item.model_dump(mode="json")) for item in items]
        selected: list[Selection] = [None] * len(items)
        missing: dict[str, list[int]] = {}  # khóa -> vị trí; item trùng khóa chỉ gửi một lần
        for pos, key in enumerate(keys):
            cached = self.cache.get(key)
            name = cached.get("name") if isinstance(cached, dict) and "name" in cached else False
            if typed and isinstance(name, (list, tuple)) and len(name) == 2 and all(isinstance(v, str) for v in name):
                name = tuple(name)
            valid_shape = isinstance(name, str) if not typed else (isinstance(name, tuple) and len(name) == 2 and all(isinstance(v, str) for v in name))
            if name is None or (valid_shape and name in valid and
                                (not typed or items[pos].category != "material" or name[0] == "part")):
                selected[pos] = name
                self.cache_hits += 1
            else:  # miss, hoặc giá trị cache không còn thuộc codebook
                missing.setdefault(key, []).append(pos)
        if missing:
            fresh = self._request([items[positions[0]] for positions in missing.values()], names)
            for (key, positions), name in zip(missing.items(), fresh):
                self.cache.set(key, {"name": name})
                for pos in positions:
                    selected[pos] = name
                self.cache_misses += len(positions)
        return selected

    def _request(self, items: list[RepairEstimateItem], names: CandidateNames) -> list[Selection]:
        """Gửi một request Gemini cho ``items`` bằng prompt chung."""
        started = time.perf_counter()
        audit = {"items": [item.raw_name for item in items], "categories": sorted({item.category for item in items}),
                 "prompt": "mapping", "model": self.model, "status": "failed"}
        try:
            payload = {"items": [dict(mapping_item_payload(item.model_dump(mode="json")), index=i)
                                 for i, item in enumerate(items)],
                       "names": ([{"name": name, "item_type": group}
                                  for group, entries in names.items() for name in entries]
                                 if isinstance(names, dict) else names)}
            schema = WorkBatchSelection if isinstance(names, dict) else BatchSelection
            schema_json = schema.model_json_schema()
            context = self.context_cache.acquire(self.INSTRUCTION, payload["names"], schema_json) if self.context_cache else None
            audit["context_cache"] = context
            audit["endpoint"] = "generateContent"
            audit["thinking_level"] = self.thinking_level

            def generate(cached):
                config = {"response_mime_type": "application/json", "response_json_schema": schema_json,
                          "http_options": types.HttpOptions(extra_body={"store": False}),
                          "thinking_config": types.ThinkingConfig(thinking_level=self.thinking_level)}
                if cached:
                    config["cached_content"] = cached["name"]
                    contents = {"items": payload["items"]}
                else:
                    config["system_instruction"] = self.INSTRUCTION
                    contents = {"names": payload["names"], "items": payload["items"]}
                return self.client.models.generate_content(
                    model=self.model, contents=json.dumps(contents, ensure_ascii=False, separators=(",", ":")),
                    config=types.GenerateContentConfig(**config),
                )

            try:
                response = generate(context)
            except errors.ClientError as exc:
                if context is None or exc.code != 404:
                    raise
                # A cachedContent may be deleted before its recorded expiry. Retry once with full context.
                self.context_cache.invalidate(self.INSTRUCTION, payload["names"], schema_json)
                audit["context_cache_fallback"] = "not_found"
                response = generate(None)
            audit["response"] = response.model_dump(mode="json", exclude_none=True)
            audit["cost"] = cost_from_response(audit["response"], self.model)
            raw = response.text
            candidates = getattr(response, "candidates", None)
            if not candidates or str(getattr(candidates[0].finish_reason, "value", candidates[0].finish_reason)) != "STOP" or not raw:
                raise RuntimeError("Gemini mapping chưa hoàn thành, bị chặn hoặc trả rỗng")
            selections = schema.model_validate_json(raw).selections
            indexes = [s.index for s in selections]
            if sorted(indexes) != list(range(len(items))):
                raise ValueError(f"Gemini trả thiếu/thừa/trùng index (cần 0..{len(items) - 1}): {indexes}")
            if isinstance(names, dict):
                if any((s.name is None) != (s.item_type is None) for s in selections):
                    raise ValueError("name và item_type phải cùng null hoặc cùng có giá trị")
                by_index = {s.index: (s.item_type, s.name) if s.name is not None else None for s in selections}
                valid = {(group, name) for group, entries in names.items() for name in entries}
            else:
                by_index = {s.index: s.name for s in selections}
                valid = set(names)
            if any(name is not None and name not in valid for name in by_index.values()):
                raise ValueError("Gemini trả tên ngoài codebook")
            if isinstance(names, dict) and any(
                item.category == "material" and by_index[i] is not None and by_index[i][0] != "part"
                for i, item in enumerate(items)
            ):
                raise ValueError("Vật tư chỉ được map với item_type part")
            audit["status"] = "success"
            return [by_index[i] for i in range(len(items))]
        finally:
            audit.setdefault("cost", estimate_cost(self.model, None))
            context = audit.get("context_cache")
            if context and context.get("created"):
                storage = context.get("storage_estimated_usd")
                audit["cost"]["context_cache_storage_complete"] = storage is not None
                audit["cost"]["context_cache_storage_usd"] = storage or 0.0
                if audit["cost"].get("total_usd") is not None:
                    audit["cost"]["total_usd"] += storage or 0.0
            audit["elapsed_seconds"] = round(time.perf_counter() - started, 3)
            self.calls.append(audit)

    def select(self, item: RepairEstimateItem, names: CandidateNames) -> Selection:
        """Map một item (một request)."""
        return self.select_many([item], names)[0]

    def cost_summary(self) -> dict:
        """Tổng chi phí mọi lời gọi fallback đã thực hiện."""
        return sum_costs(call["cost"] for call in self.calls)


class CodebookMapper:
    BATCH_SIZE = 50  # số item tối đa mỗi request Gemini

    def __init__(self, *, matcher=None, codebook_dir: str | Path = CODEBOOK_DIR):
        root = Path(codebook_dir)
        self.work = Codebook(root / "codebook.xlsx", typed=True)
        self.matcher = matcher

    def book_for(self, item: RepairEstimateItem) -> Codebook:
        return self.work

    def _result(self, item: RepairEstimateItem, matches: list[CodebookRow], method: str) -> MappingResult:
        return MappingResult(raw_name=item.raw_name, category=item.category,
                             codebook=self.book_for(item).path.name, method=method, matches=matches)

    def map_item(self, item: RepairEstimateItem) -> MappingResult:
        """Map vật tư bằng exact/fallback, công việc bằng Gemini."""
        return self.map_items([item])[0]

    def map_items(self, items: list[RepairEstimateItem]) -> list[MappingResult]:
        """Chỉ exact vật tư; công việc và vật tư chưa khớp gửi Gemini theo batch
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
                if items[pos].category == "material" and group != "part":
                    raise ValueError("Vật tư chỉ được map với item_type part")
                matches = self.work.by_type.get(group, {}).get(selected_name, [])
                if not matches:
                    raise ValueError(f"Tên/loại trả về không thuộc codebook: {name}")
                results[pos] = self._result(items[pos], matches, "gemini")
        return results  # type: ignore[return-value]

    def map_repair_estimate(self, estimate: RepairEstimate) -> list[MappingResult]:
        """One result per OCR item, in original order (including unmatched items)."""
        return self.map_items(estimate.items)
