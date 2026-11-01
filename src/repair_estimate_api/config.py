"""Cấu hình phía server cho VRE API."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from mapping.mapper import CODEBOOK_DIR
from ocr.extractor import PROJECT_ROOT
from repair_estimate_pipeline.runner import REPAIR_ESTIMATE_OUTPUT_DIR


def _load_api_env(path: Path) -> None:
    allowed = {
        "REDIS_URL", "GEMINI_MODEL", "OCR_MODEL", "MAPPING_MODEL", "OCR_THINKING_LEVEL", "MAPPING_THINKING_LEVEL", "VRE_API_SHOW_AI_OPTIONS",
        "VRE_API_ALLOWED_MODELS",
        "VRE_API_QUEUE", "VRE_API_UPLOAD_DIR", "VRE_INPUT_STORE_DIR", "VRE_API_MAX_UPLOAD_BYTES",
        "VRE_API_RESULT_TTL_SECONDS", "VRE_API_JOB_TIMEOUT_SECONDS", "VRE_API_WORKERS",
        "VRE_PARTNER_API_KEYS", "VRE_PARTNER_MAX_WAIT_SECONDS",
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
        if value[:1] not in "\"'":  # giá trị không đặt trong dấu nháy: bỏ comment cuối dòng (" # ...")
            value = value.split(" #", 1)[0].rstrip()
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


def _partner_api_keys(raw: str) -> dict[str, str]:
    """``ten_khach:key,ten_khach_2:key2`` → {key: ten_khach}. Mỗi khách một key để thu hồi riêng."""
    keys: dict[str, str] = {}
    for entry in filter(None, (part.strip() for part in raw.split(","))):
        name, separator, key = entry.partition(":")
        name, key = name.strip(), key.strip()
        if not separator or not name or len(key) < 24:
            raise ValueError("VRE_PARTNER_API_KEYS phải có dạng ten:key (key >= 24 ký tự), cách nhau bởi dấu phẩy")
        if key in keys:
            raise ValueError(f"VRE_PARTNER_API_KEYS bị trùng key cho {name}")
        keys[key] = name
    return keys


@dataclass(frozen=True)
class ApiSettings:
    env_file: Path
    redis_url: str
    default_model: str
    allowed_models: frozenset[str]
    queue_name: str
    upload_dir: Path
    output_dir: Path
    codebook_dir: Path
    max_upload_bytes: int
    result_ttl_seconds: int
    job_timeout_seconds: int
    worker_count: int = 1  # số RQ worker chạy song song trong một tiến trình worker-pool
    default_mapping_model: str | None = None
    default_ocr_thinking_level: str | None = None
    default_mapping_thinking_level: str | None = None
    show_ai_options: bool = True  # False: demo UI ẩn dropdown model/reasoning
    partner_api_keys: dict[str, str] = field(default_factory=dict)  # key → tên khách; rỗng = tắt API đối tác
    partner_max_wait_seconds: int = 90  # < 100s để đi qua được Cloudflare/proxy có timeout mặc định
    input_store_dir: Path = PROJECT_ROOT / "input_files"  # file đầu vào lưu vĩnh viễn, mỗi sha256 một bản

    @classmethod
    def from_env(cls, env_file: str | Path = PROJECT_ROOT / ".env") -> "ApiSettings":
        env_file = Path(env_file).resolve()
        _load_api_env(env_file)
        redis_url = os.getenv("REDIS_URL")
        if not redis_url:
            raise ValueError("Thiếu REDIS_URL trong environment hoặc .env")
        default_model = os.getenv("OCR_MODEL") or os.getenv("GEMINI_MODEL") or "gemini-3.8-flash"
        default_mapping_model = os.getenv("MAPPING_MODEL") or default_model
        configured = os.getenv("VRE_API_ALLOWED_MODELS", f"{default_model},{default_mapping_model}")
        allowed_models = frozenset(model.strip() for model in configured.split(",") if model.strip())
        if not {default_model, default_mapping_model} <= allowed_models:
            raise ValueError("OCR_MODEL và MAPPING_MODEL phải nằm trong VRE_API_ALLOWED_MODELS")
        return cls(
            env_file=env_file,
            redis_url=redis_url,
            default_model=default_model,
            default_mapping_model=default_mapping_model,
            default_ocr_thinking_level=(os.getenv("OCR_THINKING_LEVEL") or "").strip().lower() or None,
            show_ai_options=os.getenv("VRE_API_SHOW_AI_OPTIONS", "true").strip().lower() in {"true", "1", "yes"},
            default_mapping_thinking_level=(os.getenv("MAPPING_THINKING_LEVEL") or "").strip().lower() or None,
            allowed_models=allowed_models,
            queue_name=os.getenv("VRE_API_QUEUE", "repair-estimate"),
            upload_dir=Path(os.getenv("VRE_API_UPLOAD_DIR", "/tmp/vre-api/uploads")).resolve(),
            output_dir=REPAIR_ESTIMATE_OUTPUT_DIR.resolve(),
            codebook_dir=CODEBOOK_DIR.resolve(),
            max_upload_bytes=_positive_int("VRE_API_MAX_UPLOAD_BYTES", 25 * 1024 * 1024),
            result_ttl_seconds=_positive_int("VRE_API_RESULT_TTL_SECONDS", 7 * 86400),
            job_timeout_seconds=_positive_int("VRE_API_JOB_TIMEOUT_SECONDS", 2100),
            worker_count=_positive_int("VRE_API_WORKERS", 1),
            partner_api_keys=_partner_api_keys(os.getenv("VRE_PARTNER_API_KEYS", "")),
            partner_max_wait_seconds=_positive_int("VRE_PARTNER_MAX_WAIT_SECONDS", 90),
            input_store_dir=Path(os.getenv("VRE_INPUT_STORE_DIR", PROJECT_ROOT / "input_files")).resolve(),
        )
