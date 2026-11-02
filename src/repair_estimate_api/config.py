"""Cấu hình phía server cho VRE API."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from llm import MODEL_ENVS, PROVIDERS, default_models, default_provider, find_provider, provider_name
from mapping.mapper import CODEBOOK_DIR
from ocr.extractor import PROJECT_ROOT
from repair_estimate_pipeline.runner import REPAIR_ESTIMATE_OUTPUT_DIR


def _load_api_env(path: Path) -> set[str]:
    """Nạp cấu hình không bí mật vào env; trả tên mọi biến có giá trị trong file.

    API không cần API key của provider nên không nạp, chỉ cần biết key có được cấu hình hay không.
    """
    allowed = {
        "REDIS_URL", *MODEL_ENVS, "OCR_THINKING_LEVEL", "MAPPING_THINKING_LEVEL", "VRE_API_SHOW_AI_OPTIONS",
        "VRE_API_ALLOWED_MODELS",
        "VRE_API_QUEUE", "VRE_API_UPLOAD_DIR", "VRE_INPUT_STORE_DIR", "VRE_API_MAX_UPLOAD_BYTES",
        "VRE_API_RESULT_TTL_SECONDS", "VRE_API_JOB_TIMEOUT_SECONDS", "VRE_API_WORKERS",
        "VRE_PARTNER_API_KEYS", "VRE_PARTNER_MAX_WAIT_SECONDS",
    }
    present: set[str] = set()
    if not path.is_file():
        return present
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator:
            continue
        value = value.strip()
        if value[:1] not in "\"'":  # giá trị không đặt trong dấu nháy: bỏ comment cuối dòng (" # ...")
            value = value.split(" #", 1)[0].rstrip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value:
            present.add(key)
        if key in allowed:
            os.environ.setdefault(key, value)
    return present


class ModelChoiceError(ValueError):
    """Lựa chọn provider/model của job không hợp lệ; ``code`` là mã lỗi trả cho client."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


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
    # Provider được bật -> (model OCR, model mapping) mặc định; phần tử đầu là provider mặc định.
    providers: dict[str, tuple[str, str]]
    allowed_models: frozenset[str]  # mọi provider; job chỉ được chọn model trong đây
    queue_name: str
    upload_dir: Path
    output_dir: Path
    codebook_dir: Path
    max_upload_bytes: int
    result_ttl_seconds: int
    job_timeout_seconds: int
    worker_count: int = 1  # số RQ worker chạy song song trong một tiến trình worker-pool
    default_ocr_thinking_level: str | None = None
    default_mapping_thinking_level: str | None = None
    show_ai_options: bool = True  # False: demo UI ẩn dropdown model/reasoning
    partner_api_keys: dict[str, str] = field(default_factory=dict)  # key → tên khách; rỗng = tắt API đối tác
    partner_max_wait_seconds: int = 90  # < 100s để đi qua được Cloudflare/proxy có timeout mặc định
    input_store_dir: Path = PROJECT_ROOT / "input_files"  # file đầu vào lưu vĩnh viễn, mỗi sha256 một bản
    def resolve_models(self, provider: str | None, ocr_model: str | None,
                       mapping_model: str | None) -> tuple[str, str, str]:
        """(provider, model OCR, model mapping) của một job; mỗi job chỉ dùng một provider.

        Không chọn provider thì suy ra từ model đã chọn; không chọn gì thì dùng provider mặc định.
        Model bỏ trống lấy model mặc định của provider. Lỗi là ``ModelChoiceError``.
        """
        enabled = self.providers
        chosen = [model for model in (ocr_model, mapping_model) if model]
        if set(chosen) - self.allowed_models:
            raise ModelChoiceError("MODEL_NOT_ALLOWED", "Model không nằm trong allowlist")
        owners = {provider_name(model) for model in chosen}
        if len(owners) > 1 or (provider and owners - {provider}):
            raise ModelChoiceError("MODEL_PROVIDER_MISMATCH",
                                   "Model OCR và mapping phải cùng thuộc provider của job (mỗi job một provider)")
        provider = provider or next(iter(owners), None) or next(iter(enabled))
        if provider not in enabled:
            raise ModelChoiceError("PROVIDER_DISABLED", f"Provider {provider} chưa được bật trên server")
        default_ocr, default_mapping = enabled[provider]
        return provider, ocr_model or default_ocr, mapping_model or default_mapping

    @classmethod
    def from_env(cls, env_file: str | Path = PROJECT_ROOT / ".env") -> "ApiSettings":
        env_file = Path(env_file).resolve()
        file_keys = _load_api_env(env_file)
        redis_url = os.getenv("REDIS_URL")
        if not redis_url:
            raise ValueError("Thiếu REDIS_URL trong environment hoặc .env")
        default = default_provider()
        defaults = {default: default_models(default)}
        # Provider khác chỉ được cân nhắc khi có API key (trong env hoặc .env).
        for spec in PROVIDERS:
            if spec.name != default and (os.getenv(spec.api_key_env) or spec.api_key_env in file_keys):
                defaults[spec.name] = default_models(spec.name)
        configured = os.getenv("VRE_API_ALLOWED_MODELS")
        allowed_models = (frozenset(model.strip() for model in configured.split(",") if model.strip()) if configured
                          else frozenset(model for models in defaults.values() for model in models))
        unknown = sorted(model for model in allowed_models if find_provider(model) is None)
        if unknown:
            # Provider xét theo tiền tố tên model; báo sớm thay vì để job lỗi lúc chạy.
            raise ValueError(f"Model không thuộc provider nào (cần claude-... hoặc gemini-...): {', '.join(unknown)}")
        providers = {}
        for name, models in defaults.items():
            if name != default and not any(provider_name(model) == name for model in allowed_models):
                continue  # có key nhưng allowlist không mở model nào của provider này: không bật
            if not set(models) <= allowed_models:
                raise ValueError(f"Model mặc định của {name} ({', '.join(dict.fromkeys(models))}) "
                                 "phải nằm trong VRE_API_ALLOWED_MODELS")
            providers[name] = models
        return cls(
            env_file=env_file,
            redis_url=redis_url,
            providers=providers,
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
