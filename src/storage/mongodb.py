"""Store and query completed Vehicle Repair Estimate pipeline runs."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.collection import Collection
from repair_estimate_pipeline.metrics import build_metrics


@dataclass(frozen=True)
class MongoSettings:
    uri: str
    database: str = "vehicle_repair_estimate"
    collection: str = "repair_estimates"

    @classmethod
    def from_env(cls, env_file: str | Path | None = None) -> "MongoSettings":
        if env_file is not None:
            _load_mongo_env(Path(env_file))
        uri = os.getenv("MONGO_URI")
        if not uri:
            raise ValueError("Thiếu MONGO_URI trong environment hoặc .env")
        return cls(
            uri=uri,
            database=os.getenv("MONGO_DATABASE", cls.database),
            collection=os.getenv("MONGO_COLLECTION", cls.collection),
        )


def _load_mongo_env(path: Path) -> None:
    """Load only MongoDB variables; never execute or print the env file."""
    if not path.is_file():
        return
    allowed = {"MONGO_URI", "MONGO_DATABASE", "MONGO_COLLECTION"}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or key not in allowed:
            continue
        value = value.strip()
        if value[:1] not in "\"'":  # giá trị không đặt trong dấu nháy: bỏ comment cuối dòng (" # ...")
            value = value.split(" #", 1)[0].rstrip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Thiếu file bắt buộc: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON không hợp lệ: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"JSON phải là object: {path}")
    return payload


def build_run_document(run_dir: str | Path) -> dict[str, Any]:
    """Build one MongoDB document from summary.json and mapping.json."""
    run_dir = Path(run_dir).resolve()
    summary = _read_json(run_dir / "summary.json")
    mapping = _read_json(run_dir / "mapping.json")
    items = mapping.get("items")
    document = mapping.get("document")
    if not isinstance(items, list) or not isinstance(document, dict):
        raise ValueError(f"mapping.json thiếu document/items hợp lệ: {run_dir}")
    document = dict(document)
    if "quotation_number" in document and "estimate_number" not in document:
        document["estimate_number"] = document.pop("quotation_number")

    methods = {"exact": 0, "gemini": 0, "unmatched": 0}
    stored_items = []
    for index, item in enumerate(items):
        if not isinstance(item, dict) or not isinstance(item.get("mapping"), dict):
            raise ValueError(f"Item {index} thiếu mapping hợp lệ: {run_dir}")
        method = item["mapping"].get("method")
        if method not in methods:
            raise ValueError(f"Item {index} có mapping.method không hợp lệ: {method}")
        methods[method] += 1
        stored_items.append({**item, "index": index})

    started_at = summary.get("started_at")
    try:
        created_at = datetime.fromisoformat(started_at) if started_at else datetime.now(timezone.utc)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"started_at không hợp lệ: {started_at}") from exc
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)

    source_path = Path(summary.get("input") or "")
    return {
        "run_id": run_dir.name,
        "created_at": created_at,
        "input": {"file_name": source_path.name, "file_path": str(source_path),
                  "sha256": summary.get("input_sha256")},
        "status": summary.get("status", "unknown"),
        "updated_by": "gemini",
        "models": summary.get("models") or {},
        "document": document,
        "items": stored_items,
        "metrics": build_metrics(summary, item_count=len(items), methods=methods),
        "artifacts_path": str(run_dir),
        "error": summary.get("error"),
    }


class RepairEstimateRepository:
    def __init__(self, settings: MongoSettings, *, client=None):
        self._owns_client = client is None
        self.client = client or MongoClient(
            settings.uri, serverSelectionTimeoutMS=5000, appname="vre-pipeline"
        )
        self.collection: Collection = self.client[settings.database][settings.collection]

    def ping(self) -> None:
        self.client.admin.command("ping")

    def ensure_indexes(self) -> None:
        self.collection.create_index([("run_id", ASCENDING)], unique=True, name="run_id_unique")
        self.collection.create_index([("input.sha256", ASCENDING)], name="input_sha256")
        self.collection.create_index([("document.vehicle_plate", ASCENDING)], name="vehicle_plate")
        self.collection.create_index([("document.estimate_number", ASCENDING)], name="estimate_number")
        self.collection.create_index([("created_at", DESCENDING)], name="created_at_desc")

    def save_run(self, run_dir: str | Path) -> dict[str, Any]:
        document = build_run_document(run_dir)
        result = self.collection.replace_one(
            {"run_id": document["run_id"]}, document, upsert=True
        )
        return {
            "run_id": document["run_id"],
            "created": result.upserted_id is not None,
            "matched_count": result.matched_count,
            "modified_count": result.modified_count,
        }

    def list_results(
        self, *, limit: int = 10, vehicle_plate: str | None = None,
        include_items: bool = False,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            raise ValueError("limit phải lớn hơn 0")
        query = {"document.vehicle_plate": vehicle_plate} if vehicle_plate else {}
        projection = {
            "_id": 0,
            "run_id": 1,
            "created_at": 1,
            "status": 1,
            "updated_by": 1,
            "document": 1,
            "metrics": 1,
        }
        if include_items:
            projection["items"] = 1
        return list(self.collection.find(query, projection).sort("created_at", DESCENDING).limit(limit))

    def list_history(self, *, limit: int = 20, before: datetime | None = None,
                     search: str | None = None) -> list[dict[str, Any]]:
        """Tóm tắt các lần chạy (không kèm items), mới nhất trước; ``before`` là con trỏ ``created_at``."""
        if limit <= 0:
            raise ValueError("limit phải lớn hơn 0")
        query: dict[str, Any] = {}
        if before is not None:
            query["created_at"] = {"$lt": before}
        if search:
            pattern = {"$regex": re.escape(search.strip()), "$options": "i"}
            query["$or"] = [{"document.vehicle_plate": pattern}, {"document.estimate_number": pattern},
                            {"input.file_name": pattern}]
        projection = {
            "_id": 0, "run_id": 1, "created_at": 1, "status": 1, "models": 1,
            "input.file_name": 1, "document.vehicle_plate": 1, "document.estimate_number": 1,
            "metrics": 1,
        }
        return list(self.collection.find(query, projection).sort("created_at", DESCENDING).limit(limit))

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        """Một lần chạy đầy đủ; bỏ đường dẫn nội bộ của server."""
        return self.collection.find_one(
            {"run_id": run_id}, {"_id": 0, "artifacts_path": 0, "input.file_path": 0})

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "RepairEstimateRepository":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()
