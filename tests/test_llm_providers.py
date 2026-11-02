import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import get_args
from unittest.mock import MagicMock, patch

from llm import (PROVIDER_NAMES, PROVIDERS, LlmClients, ProviderName, default_models, default_provider, engine_alias,
                 find_provider, get_provider, provider_by_alias, provider_name, redact)
from repair_estimate_api.config import ApiSettings, ModelChoiceError


class RegistryTests(unittest.TestCase):
    def test_engine_aliases_are_unique_and_do_not_contain_vendor_names(self):
        self.assertEqual(provider_by_alias("5").name, "claude")
        self.assertEqual(provider_by_alias(" 1 ").name, "gemini")
        self.assertEqual((engine_alias("claude"), engine_alias("gemini"), engine_alias(None)), ("5", "1", None))
        for wrong in ("claude", "gemini", "", None, "2"):
            self.assertIsNone(provider_by_alias(wrong))
        aliases = [p.alias for p in PROVIDERS]
        self.assertEqual(len(set(aliases)), len(aliases))

    def test_provider_literal_matches_registry(self):
        self.assertEqual(set(get_args(ProviderName)), PROVIDER_NAMES)

    def test_default_provider_from_llm_provider_then_legacy_ocr_model(self):
        self.assertEqual(default_provider({}), "gemini")
        self.assertEqual(default_provider({"OCR_MODEL": "claude-haiku-5-5"}), "claude")  # .env cũ
        self.assertEqual(default_provider({"LLM_PROVIDER": " Claude ", "OCR_MODEL": "gemini-3.8-flash"}), "claude")
        with self.assertRaisesRegex(ValueError, "LLM_PROVIDER"):
            default_provider({"LLM_PROVIDER": "openai"})

    def test_default_models_per_provider_with_legacy_fallback(self):
        env = {"OCR_MODEL": "gemini-3.5-flash-lite", "MAPPING_MODEL": "gemini-3.6-flash",
               "CLAUDE_MAPPING_MODEL": "claude-sonnet-5-5"}
        self.assertEqual(default_models("gemini", env), ("gemini-3.5-flash-lite", "gemini-3.6-flash"))
        # OCR_MODEL/MAPPING_MODEL cũ là Gemini nên không áp cho Claude
        self.assertEqual(default_models("claude", env), ("claude-haiku-5-5", "claude-sonnet-5-5"))
        self.assertEqual(default_models("gemini", {"GEMINI_OCR_MODEL": "gemini-3.6-flash", **env}),
                         ("gemini-3.6-flash", "gemini-3.6-flash"))
        self.assertEqual(default_models("claude", {}), ("claude-haiku-5-5", "claude-haiku-5-5"))
        with self.assertRaisesRegex(ValueError, "CLAUDE_OCR_MODEL"):
            default_models("claude", {"CLAUDE_OCR_MODEL": "gemini-3.8-flash"})

    def test_provider_is_chosen_by_model_prefix(self):
        self.assertEqual(provider_name("claude-haiku-5-5"), "claude")
        self.assertEqual(provider_name("gemini-3.5-flash-lite"), "gemini")
        self.assertEqual(provider_name("models/gemini-3.8-flash"), "gemini")
        self.assertIsNone(find_provider("gpt-5"))
        with self.assertRaisesRegex(ValueError, "claude-... hoặc gemini-..."):
            get_provider("gpt-5")

    def test_clients_open_lazily_once_per_provider_and_close_only_owned(self):
        owned, given = MagicMock(), MagicMock()
        env = {"ANTHROPIC_API_KEY": "secret-claude"}
        with patch("llm.claude.create_client", return_value=owned) as create:
            with LlmClients(30, env=env, clients={"gemini": given}) as llm:
                first = llm.backend("claude-haiku-5-5")
                self.assertIs(llm.backend("claude-sonnet-5-5"), first)  # cùng provider: dùng chung client
                self.assertEqual(llm.backend("gemini-3.8-flash").name, "gemini")
            create.assert_called_once_with("secret-claude", 30)
        owned.close.assert_called_once()
        given.close.assert_not_called()

    def test_redact_hides_every_provider_key(self):
        env = {"ANTHROPIC_API_KEY": "sk-ant-x", "GEMINI_API_KEY": "AIza-y"}
        self.assertEqual(redact("bad sk-ant-x / AIza-y", env), "bad <redacted> / <redacted>")


class ApiSettingsProviderTests(unittest.TestCase):
    def settings(self, env_text="", **env):
        with TemporaryDirectory() as temp, patch.dict("os.environ", {"REDIS_URL": "redis://x", **env}, clear=True):
            env_file = Path(temp) / ".env"
            env_file.write_text(env_text, encoding="utf-8")
            settings = ApiSettings.from_env(env_file)
            self.loaded_env = dict(os.environ)
            return settings

    def test_legacy_env_keeps_single_gemini_provider(self):
        # .env đang chạy production: chỉ có cấu hình Gemini cũ
        settings = self.settings(OCR_MODEL="gemini-3.5-flash-lite", VRE_API_ALLOWED_MODELS="gemini-3.5-flash-lite,gemini-3.8-flash")
        self.assertEqual(settings.providers, {"gemini": ("gemini-3.5-flash-lite", "gemini-3.5-flash-lite")})

    def test_second_provider_needs_api_key_and_allowed_models(self):
        allowed = "gemini-3.5-flash-lite,claude-haiku-5-5,claude-sonnet-5-5"
        base = dict(LLM_PROVIDER="gemini", GEMINI_OCR_MODEL="gemini-3.5-flash-lite", VRE_API_ALLOWED_MODELS=allowed)
        self.assertEqual(list(self.settings(**base).providers), ["gemini"])  # chưa có ANTHROPIC_API_KEY
        # Key nằm trong .env: API không nạp secret vào env nhưng vẫn biết provider đã cấu hình
        settings = self.settings("ANTHROPIC_API_KEY=sk-ant-x\n", CLAUDE_MAPPING_MODEL="claude-sonnet-5-5", **base)
        self.assertEqual(settings.providers, {"gemini": ("gemini-3.5-flash-lite", "gemini-3.5-flash-lite"),
                                                "claude": ("claude-haiku-5-5", "claude-sonnet-5-5")})
        self.assertNotIn("ANTHROPIC_API_KEY", self.loaded_env)
        no_claude_models = dict(base, VRE_API_ALLOWED_MODELS="gemini-3.5-flash-lite")
        self.assertEqual(list(self.settings(ANTHROPIC_API_KEY="k", **no_claude_models).providers), ["gemini"])

    def test_allowlist_defaults_to_models_of_every_configured_provider(self):
        settings = self.settings(LLM_PROVIDER="claude", GEMINI_API_KEY="g")
        self.assertEqual(settings.allowed_models, {"claude-haiku-5-5", "gemini-3.8-flash"})
        self.assertEqual(list(settings.providers), ["claude", "gemini"])

    def test_enabled_provider_default_model_must_be_allowed(self):
        with self.assertRaisesRegex(ValueError, "Model mặc định của claude"):
            self.settings(ANTHROPIC_API_KEY="k", VRE_API_ALLOWED_MODELS="gemini-3.8-flash,claude-sonnet-5-5")

    def test_unknown_model_name_is_rejected_at_startup(self):
        with self.assertRaisesRegex(ValueError, "gpt-5"):
            self.settings(LLM_PROVIDER="claude", VRE_API_ALLOWED_MODELS="claude-haiku-5-5,gpt-5")

    def test_resolve_models_errors_carry_api_codes(self):
        settings = self.settings(LLM_PROVIDER="claude", VRE_API_ALLOWED_MODELS="claude-haiku-5-5,gemini-3.8-flash")
        for args, code in [(("claude", "gemini-3.8-flash", None), "MODEL_PROVIDER_MISMATCH"),
                           ((None, "gpt-5", None), "MODEL_NOT_ALLOWED"),
                           (("gemini", None, None), "PROVIDER_DISABLED")]:
            with self.subTest(args=args), self.assertRaises(ModelChoiceError) as caught:
                settings.resolve_models(*args)
            self.assertEqual(caught.exception.code, code)


if __name__ == "__main__":
    unittest.main()
