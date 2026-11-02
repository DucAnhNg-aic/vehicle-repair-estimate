"""Các provider LLM dùng cho OCR và mapping; model thuộc provider nào xét theo tiền tố tên.

    claude-...  -> llm.claude  (ANTHROPIC_API_KEY)
    gemini-...  -> llm.gemini  (GEMINI_API_KEY)

Provider mặc định: ``LLM_PROVIDER`` (thiếu thì theo provider của ``OCR_MODEL`` cũ, cuối cùng là gemini).
Model mặc định từng provider: ``<PROVIDER>_OCR_MODEL`` / ``<PROVIDER>_MAPPING_MODEL``, xem ``default_models``.

Thêm provider: viết module theo giao diện ở ``llm.base`` rồi khai báo một dòng trong ``PROVIDERS``.
Module provider chỉ được import khi có model dùng tới.
"""
from __future__ import annotations

import importlib
import os
from dataclasses import dataclass
from types import ModuleType
from typing import Literal

from .base import Media, normalize_effort


@dataclass(frozen=True)
class Provider:
    name: str  # cũng là ``mapping.method`` của item do provider này map
    model_prefix: str
    api_key_env: str
    module: str
    default_model: str  # khi không cấu hình model nào cho provider
    alias: str  # tên che ra ngoài (``engine`` của API đối tác), không lộ hãng

    @property
    def env_prefix(self) -> str:
        """Tiền tố biến môi trường riêng: CLAUDE_OCR_MODEL, GEMINI_MAPPING_MODEL..."""
        return self.name.upper()

    def load(self) -> ModuleType:
        return importlib.import_module(self.module)


PROVIDERS = (
    Provider("claude", "claude-", "ANTHROPIC_API_KEY", "llm.claude", "claude-haiku-5-5", "5"),
    Provider("gemini", "gemini-", "GEMINI_API_KEY", "llm.gemini", "gemini-3.8-flash", "1"),
)
ProviderName = Literal["claude", "gemini"]  # cho schema API; phải khớp PROVIDERS (có test kiểm tra)
PROVIDER_NAMES = frozenset(provider.name for provider in PROVIDERS)
API_KEY_ENVS = tuple(provider.api_key_env for provider in PROVIDERS)
DEFAULT_PROVIDER = "gemini"  # khi không cấu hình LLM_PROVIDER lẫn OCR_MODEL
# Biến môi trường chọn provider/model (không có secret); các nơi đọc .env dùng chung danh sách này.
MODEL_ENVS = ("LLM_PROVIDER", "OCR_MODEL", "MAPPING_MODEL",
              *(f"{p.env_prefix}_{kind}_MODEL" for p in PROVIDERS for kind in ("OCR", "MAPPING")))


def find_provider(model: str | None) -> Provider | None:
    """Provider của ``model``; None nếu tên model không thuộc provider nào."""
    model = (model or "").removeprefix("models/")
    return next((provider for provider in PROVIDERS if model.startswith(provider.model_prefix)), None)


def get_provider(model: str) -> Provider:
    provider = find_provider(model)
    if provider is None:
        raise ValueError(f"Model {model!r} không thuộc provider nào; tên phải bắt đầu bằng "
                         + " hoặc ".join(f"{p.model_prefix}..." for p in PROVIDERS))
    return provider


def provider_name(model: str | None) -> str | None:
    provider = find_provider(model)
    return provider.name if provider else None


def provider_by_alias(alias: str | None) -> Provider | None:
    """Provider ứng với ``engine`` của API đối tác; None nếu bí danh lạ."""
    return next((provider for provider in PROVIDERS if provider.alias == (alias or "").strip()), None)


def engine_alias(name: str | None) -> str | None:
    """Bí danh công khai của provider ``name`` (``None`` nếu không có)."""
    return next((provider.alias for provider in PROVIDERS if provider.name == name), None)


def default_provider(env=None) -> str:
    """``LLM_PROVIDER``; thiếu thì theo provider của ``OCR_MODEL`` (cấu hình cũ), cuối cùng là gemini."""
    env = os.environ if env is None else env
    name = (env.get("LLM_PROVIDER") or "").strip().lower()
    if name and name not in PROVIDER_NAMES:
        raise ValueError(f"LLM_PROVIDER phải là một trong {', '.join(sorted(PROVIDER_NAMES))}: {name}")
    return name or provider_name((env.get("OCR_MODEL") or "").strip()) or DEFAULT_PROVIDER


def default_models(provider: str, env=None) -> tuple[str, str]:
    """(model OCR, model mapping) mặc định của ``provider``.

    Thứ tự: ``<PROVIDER>_OCR_MODEL``, rồi ``OCR_MODEL`` cũ nếu cùng provider, rồi model mặc định của
    provider; mapping tương tự với ``*_MAPPING_MODEL`` và cuối cùng lấy theo model OCR.
    """
    env = os.environ if env is None else env
    spec = next((p for p in PROVIDERS if p.name == provider), None)
    if spec is None:
        raise ValueError(f"Provider không tồn tại: {provider}")

    def configured(kind: str) -> str | None:
        own = (env.get(f"{spec.env_prefix}_{kind}_MODEL") or "").strip()
        if own and provider_name(own) != provider:
            raise ValueError(f"{spec.env_prefix}_{kind}_MODEL phải là model {spec.model_prefix}...: {own}")
        legacy = (env.get(f"{kind}_MODEL") or "").strip()
        return own or (legacy if provider_name(legacy) == provider else None)

    ocr = configured("OCR") or spec.default_model
    return ocr, configured("MAPPING") or ocr


class LlmClients:
    """Mỗi provider một client SDK, mở khi lần đầu cần; ``with`` đóng các client do nó mở.

    ``clients``: client có sẵn theo tên provider (test, hoặc caller tự quản lý) — không bị đóng.
    ``backend_options``: tham số thêm cho ``Backend`` của provider, vd. ``{"gemini": {"context_cache_dir": ...}}``.
    """

    def __init__(self, timeout: float = 180, *, env=None, clients: dict | None = None,
                 backend_options: dict[str, dict] | None = None):
        self.timeout = timeout
        self.env = os.environ if env is None else env
        self._clients = dict(clients or {})
        self._owned: list = []
        self._backends: dict = {}
        self._options = backend_options or {}

    def require(self, *models: str | None) -> None:
        """Báo lỗi sớm (trước khi gọi API) nếu model lạ hoặc thiếu API key của provider cần dùng."""
        for model in filter(None, models):
            provider = get_provider(model)
            if provider.name not in self._clients and not self.env.get(provider.api_key_env):
                raise RuntimeError(f"Chưa cấu hình {provider.api_key_env} cho model {model}")

    def backend(self, model: str):
        provider = get_provider(model)
        if provider.name not in self._backends:
            module = provider.load()
            client = self._clients.get(provider.name)
            if client is None:
                self.require(model)
                client = module.create_client(self.env[provider.api_key_env], self.timeout)
                self._clients[provider.name] = client
                self._owned.append(client)
            self._backends[provider.name] = module.Backend(
                client, timeout=self.timeout, **self._options.get(provider.name, {}))
        return self._backends[provider.name]

    def close(self) -> None:
        while self._owned:
            self._owned.pop().close()

    def __enter__(self) -> "LlmClients":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def redact(message: str, env=None) -> str:
    """Che mọi API key provider khỏi thông báo lỗi (lỗi upstream có thể chứa chi tiết request)."""
    env = os.environ if env is None else env
    for key in API_KEY_ENVS:
        if env.get(key):
            message = message.replace(env[key], "<redacted>")
    return message


__all__ = [
    "API_KEY_ENVS", "MODEL_ENVS", "PROVIDERS", "PROVIDER_NAMES", "LlmClients", "Media", "ProviderName",
    "default_models", "default_provider", "engine_alias", "find_provider", "get_provider", "normalize_effort",
    "provider_by_alias", "provider_name", "redact",
]
