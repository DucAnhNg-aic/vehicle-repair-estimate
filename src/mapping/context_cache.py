"""Lazy Google context cache registry shared by workers through the outputs volume."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google.genai import types

from cost import estimate_cache_storage


class ContextCache:
    def __init__(self, client, model: str, directory: Path, *, ttl_seconds: int = 1800):
        if ttl_seconds <= 0:
            raise ValueError("MAPPING_CONTEXT_CACHE_TTL_SECONDS phải lớn hơn 0")
        self.client, self.model, self.directory = client, model, Path(directory)
        self.ttl_seconds = ttl_seconds
        self.disabled = False

    def _path(self, instruction: str, names, schema: dict) -> Path:
        # Hash credentials to isolate registries across API-key/project changes, never store secrets.
        scope = hashlib.sha256(os.getenv("GEMINI_API_KEY", "").encode()).hexdigest()
        payload = {"version": 1, "model": self.model, "instruction": instruction,
                   "names": names, "schema": schema, "scope": scope}
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                          separators=(",", ":")).encode()).hexdigest()
        return self.directory / f"{digest}.json"

    def acquire(self, instruction: str, names, schema: dict) -> dict | None:
        if self.disabled:
            return None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self._path(instruction, names, schema)
            with path.with_suffix(".lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    record = json.loads(path.read_text())
                    expires = datetime.fromisoformat(record["expire_time"])
                    if (isinstance(record.get("name"), str) and record["name"].startswith("cachedContents/")
                            and expires > datetime.now(timezone.utc) + timedelta(seconds=30)):
                        return {**record, "created": False, "storage_estimated_usd": 0.0}
                except (FileNotFoundError, ValueError, KeyError, TypeError):
                    pass
                cache = self.client.caches.create(
                    model=self.model,
                    config=types.CreateCachedContentConfig(
                        display_name=f"vre-mapping-{path.stem[:16]}",
                        system_instruction=instruction,
                        contents=json.dumps({"names": names}, ensure_ascii=False, separators=(",", ":")),
                        ttl=f"{self.ttl_seconds}s",
                    ),
                )
                expires = cache.expire_time
                if not isinstance(cache.name, str) or not isinstance(expires, datetime):
                    raise ValueError("Google cache metadata thiếu name/expire_time")
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                tokens = getattr(cache.usage_metadata, "total_token_count", None)
                record = {"name": cache.name, "model": self.model, "expire_time": expires.isoformat(),
                          "token_count": tokens, "ttl_seconds": self.ttl_seconds,
                          "storage_estimated_usd": estimate_cache_storage(self.model, tokens, self.ttl_seconds)}
                try:
                    temp = path.with_suffix(".tmp")
                    temp.write_text(json.dumps(record))
                    temp.replace(path)
                except OSError:
                    warnings.warn("Không lưu được registry Google cache; cache đã tạo vẫn được tính phí.", RuntimeWarning)
                return {**record, "created": True}
        except Exception as exc:
            self.disabled = True
            warnings.warn(f"Google context cache unavailable ({type(exc).__name__}); gửi codebook đầy đủ.", RuntimeWarning)
            return None

    def invalidate(self, instruction: str, names, schema: dict) -> None:
        path = self._path(instruction, names, schema)
        with path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            path.unlink(missing_ok=True)
