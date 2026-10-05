"""Classify Codebook using Gemini, with validated batch checkpoints."""
from __future__ import annotations

import hashlib
import json

from openpyxl import load_workbook

from .schema import ClassificationBatch


def write_json(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_rows(path, sheet):
    wb = load_workbook(path, read_only=True, data_only=False)
    try:
        ws = wb[sheet]
        if [ws.cell(1, i).value for i in (1, 2)] != ["MA", "TEN"]:
            raise ValueError("Cột A/B phải có header MA/TEN")
        rows, seen = [], set()
        for row, cells in enumerate(ws.iter_rows(min_row=2, max_col=2), 2):
            code, name = [c.value for c in cells]
            if code is None and name is None:
                continue
            if any(c.data_type == "f" for c in cells):
                raise ValueError(f"Mã/tên dạng công thức tại dòng {row}")
            if not all(isinstance(v, str) and v.strip() for v in (code, name)) or code in seen:
                raise ValueError(f"Mã/tên thiếu, không phải text hoặc mã trùng tại dòng {row}")
            seen.add(code)
            rows.append({"row": row, "code": code, "name": name})
        if not rows:
            raise ValueError("Codebook rỗng")
        return rows
    finally:
        wb.close()


def unique_names(rows):
    # Exact strings only: retain spelling and position qualifiers.
    return list(dict.fromkeys(r["name"] for r in rows))


def validate_batch(payload, names):
    parsed = ClassificationBatch.model_validate(payload)
    returned = [item.name for item in parsed.items]
    if len(returned) != len(names) or set(returned) != set(names):
        raise ValueError("Gemini trả thiếu/thừa/trùng tên hoặc sửa tên đầu vào")
    return parsed.model_dump()


def export_xlsx(source, sheet, destination, rows, results):
    wb = load_workbook(source)
    try:
        ws = wb[sheet]
        keys = ["item_type"]
        names = ["item_type"]
        existing = [c.value for c in ws[1]]
        if any(n in existing for n in names):
            raise ValueError("Input đã có cột classification; hãy dùng Excel gốc")
        # Insert after MA/TEN/Group, preserving any existing columns to the right.
        ws.insert_cols(4)
        start = 4
        for offset, name in enumerate(names):
            ws.cell(1, start + offset, name)
        for row in rows:
            item = results.get(row["name"])
            if item:
                for offset, key in enumerate(keys):
                    value = item[key]
                    cell = ws.cell(row["row"], start + offset, value)
                    if isinstance(value, str):
                        cell.data_type = "s"
        tmp = destination.with_suffix(".tmp.xlsx")
        wb.save(tmp)
        tmp.replace(destination)
    finally:
        wb.close()
