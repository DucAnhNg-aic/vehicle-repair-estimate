"""Cache hai tầng cho kết quả LLM: Redis (nhanh) rồi MongoDB (bền vững).

Fail-open: tầng nào lỗi thì cảnh báo một lần, tắt tầng đó trong phần còn lại của tiến trình
và coi như cache miss; pipeline vẫn gọi LLM bình thường.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

REDIS_PREFIX = "vre:llm:"
DEFAULT_TTL_DAYS = 30.0
_ALLOWED_ENV = {"REDIS_URL", "MONGO_URI", "MONGO_DATABASE", "MONGO_CACHE_COLLECTION", "CACHE_TTL_DAYS"}


def _warn(message: str) -> None:
    print(f"Cảnh báo cache: {message}", file=sys.stderr)


def _load_cache_env(path: Path) -> None:
    """Chỉ nạp biến liên quan tới cache; không thực thi hay in nội dung file."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or key not in _ALLOWED_ENV:
            continue
        value = value.strip()
        if value[:1] not in "\"'":  # giá trị không đặt trong dấu nháy: bỏ comment cuối dòng (" # ...")
            value = value.split(" #", 1)[0].rstrip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


class TwoTierCache:
    """``get``/``set`` giá trị JSON theo khóa; mọi lỗi hạ tầng đều bị nuốt (kèm cảnh báo)."""

    def __init__(self, redis=None, collection=None, *, ttl_seconds: float = DEFAULT_TTL_DAYS * 86400,
                 warn: Callable[[str], None] = _warn, client=None):
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds phải lớn hơn 0")
        self._redis, self._collection, self._client = redis, collection, client
        self.ttl_seconds = int(ttl_seconds)
        self._warn = warn

    # ---- tầng ----
    def _disable(self, tier: str, exc: Exception) -> None:
        self._warn(f"{tier} lỗi ({type(exc).__name__}); bỏ qua tầng này và gọi LLM bình thường.")
        if tier == "Redis":
            self._redis = None
        else:
            self._collection = None

    # ---- API ----
    def get(self, key: str) -> dict | None:
        if self._redis is not None:
            try:
                raw = self._redis.get(REDIS_PREFIX + key)
                if raw is not None:
                    value = json.loads(raw)
                    if isinstance(value, dict):
                        return value
            except Exception as exc:  # Redis tắt/timeout/dữ liệu hỏng
                self._disable("Redis", exc)
        if self._collection is not None:
            try:
                now = datetime.now(timezone.utc)
                doc = self._collection.find_one({"_id": key})
                expires_at = doc.get("expires_at") if doc else None
                if expires_at is not None and expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)  # pymongo trả datetime naive (UTC)
                # TTL index của Mongo chỉ dọn mỗi ~60s nên kiểm tra hạn thủ công.
                if doc and isinstance(doc.get("value"), dict) and expires_at and expires_at > now:
                    self._promote(key, doc["value"], int((expires_at - now).total_seconds()))
                    return doc["value"]
            except Exception as exc:
                self._disable("MongoDB", exc)
        return None

    def set(self, key: str, value: dict) -> None:
        payload = json.dumps(value, ensure_ascii=False)
        if self._redis is not None:
            try:
                self._redis.set(REDIS_PREFIX + key, payload, ex=self.ttl_seconds)
            except Exception as exc:
                self._disable("Redis", exc)
        if self._collection is not None:
            try:
                now = datetime.now(timezone.utc)
                self._collection.replace_one(
                    {"_id": key},
                    {"_id": key, "value": value, "created_at": now,
                     "expires_at": now + timedelta(seconds=self.ttl_seconds)},
                    upsert=True)
            except Exception as exc:
                self._disable("MongoDB", exc)

    def _promote(self, key: str, value: dict, remaining_seconds: int) -> None:
        """Đưa bản từ Mongo lên Redis để lần sau đọc nhanh."""
        if self._redis is None or remaining_seconds <= 0:
            return
        try:
            self._redis.set(REDIS_PREFIX + key, json.dumps(value, ensure_ascii=False), ex=remaining_seconds)
        except Exception as exc:
            self._disable("Redis", exc)

    def close(self) -> None:
        for resource in (self._redis, self._client):
            try:
                if resource is not None and hasattr(resource, "close"):
                    resource.close()
            except Exception:
                pass

    def __enter__(self) -> "TwoTierCache":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    @classmethod
    def from_env(cls, env_file: str | Path | None = None, *, warn: Callable[[str], None] = _warn) -> "TwoTierCache":
        """Dựng cache từ env. Thiếu cấu hình hoặc không kết nối được thì tầng đó bị bỏ qua."""
        if env_file is not None:
            _load_cache_env(Path(env_file))
        try:
            ttl_days = float(os.getenv("CACHE_TTL_DAYS", DEFAULT_TTL_DAYS))
            if ttl_days <= 0:
                raise ValueError
        except ValueError:
            warn(f"CACHE_TTL_DAYS không hợp lệ; dùng mặc định {DEFAULT_TTL_DAYS:g} ngày.")
            ttl_days = DEFAULT_TTL_DAYS

        redis_client = None
        url = os.getenv("REDIS_URL")
        if url:
            try:
                import redis
                redis_client = redis.Redis.from_url(
                    url, socket_connect_timeout=2, socket_timeout=2, decode_responses=True)
                redis_client.ping()
            except Exception as exc:
                warn(f"không kết nối được Redis ({type(exc).__name__}); bỏ qua tầng Redis.")
                redis_client = None

        collection = mongo_client = None
        uri = os.getenv("MONGO_URI")
        if uri:
            try:
                from pymongo import MongoClient
                mongo_client = MongoClient(uri, serverSelectionTimeoutMS=2000, appname="vre-cache")
                mongo_client.admin.command("ping")
                collection = mongo_client[os.getenv("MONGO_DATABASE", "vehicle_repair_estimate")][
                    os.getenv("MONGO_CACHE_COLLECTION", "llm_cache")]
                collection.create_index("expires_at", expireAfterSeconds=0, name="expires_at_ttl")
            except Exception as exc:
                warn(f"không kết nối được MongoDB ({type(exc).__name__}); bỏ qua tầng MongoDB.")
                collection = None
                if mongo_client is not None:
                    mongo_client.close()
                    mongo_client = None
        if redis_client is None and collection is None:
            warn("không có tầng cache nào hoạt động; mọi lời gọi sẽ đi thẳng tới LLM.")
        return cls(redis_client, collection, ttl_seconds=ttl_days * 86400, warn=warn, client=mongo_client)

