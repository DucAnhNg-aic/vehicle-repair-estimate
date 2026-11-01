"""Lưu file đầu vào theo hash nội dung: cùng sha256 chỉ có một bản trên đĩa.

    <root>/<2 ký tự đầu sha256>/<sha256>

Tên file không kèm đuôi để hai lần upload cùng nội dung nhưng khác đuôi (.jpg/.jpeg) vẫn trùng một
bản; đuôi, MIME và tên gốc nằm ở collection ``input_files`` trong MongoDB. Run nào dùng file nào tra
ngược qua ``repair_estimates.input.sha256``.
"""
from __future__ import annotations

import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from llm_cache.keys import file_digest


@dataclass(frozen=True)
class StoredInputFile:
    sha256: str
    size: int
    relative_path: str  # so với root của store, để chuyển volume không phải sửa DB
    created: bool  # False: nội dung này đã có sẵn, không ghi thêm


class InputFileStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def path_for(self, sha256: str) -> Path:
        return self.root / sha256[:2] / sha256

    def put(self, source: str | Path) -> StoredInputFile:
        source = Path(source)
        sha256 = file_digest(source)
        target = self.path_for(sha256)
        relative = target.relative_to(self.root).as_posix()
        size = source.stat().st_size
        if target.exists():
            return StoredInputFile(sha256, size, relative, created=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Copy ra file tạm cùng thư mục rồi link sang tên thật: không bao giờ lộ file ghi dở,
        # và hai job cùng hash chạy song song thì chỉ một bên thắng, bên kia nhận FileExistsError.
        temp = target.with_name(f".{sha256}.{uuid.uuid4().hex}.tmp")
        try:
            shutil.copyfile(source, temp)
            os.link(temp, target)
            created = True
        except FileExistsError:
            created = False
        finally:
            temp.unlink(missing_ok=True)
        return StoredInputFile(sha256, size, relative, created=created)
