"""Cấu hình phía server cho VRE API."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from mapping.mapper import CODEBOOK_DIR
from ocr.extractor import PROJECT_ROOT
from repair_estimate_pipeline.runner import REPAIR_ESTIMATE_OUTPUT_DIR


def _load_api_env(path: Path) -> None:
    allowed = {
        "REDIS_URL", "GEMINI_MODEL", "VRE_API_KEY", "VRE_API_ALLOWED_MODELS",
        "VRE_API_QUEUE", "VRE_API_UPLOAD_DIR", "VRE_API_MAX_UPLOAD_BYTES",
        "VRE_API_RESULT_TTL_SECONDS", "VRE_API_JOB_TIMEOUT_SECONDS",
    }
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or key not in allowed:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _positive_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} phải là số nguyên") from exc
    if value <= 0:
        raise ValueError(f"{name} phải lớn hơn 0")
    return value


@dataclass(frozen=True)
class ApiSettings:
    env_file: Path
    redis_url: str
    default_model: str
    allowed_models: frozenset[str]
    api_key: str | None
    queue_name: str
    upload_dir: Path
    output_dir: Path
    codebook_dir: Path
    max_upload_bytes: int
    result_ttl_seconds: int
    job_timeout_seconds: int

    @classmethod
    def from_env(cls, env_file: str | Path = PROJECT_ROOT / ".env") -> "ApiSettings":
        env_file = Path(env_file).resolve()
        _load_api_env(env_file)
        redis_url = os.getenv("REDIS_URL")
        if not redis_url:
            raise ValueError("Thiếu REDIS_URL trong environment hoặc .env")
        default_model = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
        configured = os.getenv("VRE_API_ALLOWED_MODELS", default_model)
        allowed_models = frozenset(model.strip() for model in configured.split(",") if model.strip())
        if default_model not in allowed_models:
            raise ValueError("GEMINI_MODEL phải nằm trong VRE_API_ALLOWED_MODELS")
        return cls(
            env_file=env_file,
            redis_url=redis_url,
            default_model=default_model,
            allowed_models=allowed_models,
            api_key=os.getenv("VRE_API_KEY") or None,
            queue_name=os.getenv("VRE_API_QUEUE", "repair-estimate"),
            upload_dir=Path(os.getenv("VRE_API_UPLOAD_DIR", "/tmp/vre-api/uploads")).resolve(),
            output_dir=REPAIR_ESTIMATE_OUTPUT_DIR.resolve(),
            codebook_dir=CODEBOOK_DIR.resolve(),
            max_upload_bytes=_positive_int("VRE_API_MAX_UPLOAD_BYTES", 25 * 1024 * 1024),
            result_ttl_seconds=_positive_int("VRE_API_RESULT_TTL_SECONDS", 7 * 86400),
            job_timeout_seconds=_positive_int("VRE_API_JOB_TIMEOUT_SECONDS", 2100),
        )
