"""Provider settings and private credential sources, never real credentials."""

import os
import tempfile
import unittest
from pathlib import Path

from jarvis.providers.gemini.config import (
    DEFAULT_MODEL, GeminiConfig, read_private_key_file,
)
from jarvis.providers.gemini.errors import ProviderConfigurationError

KEY = "synthetic-private-key-for-offline-tests"


class GeminiConfigTests(unittest.TestCase):
    def test_missing_key_is_optional_for_offline_model_information(self):
        config = GeminiConfig.from_env({}, need_key=False)
        self.assertEqual(config.model, DEFAULT_MODEL)
        with self.assertRaises(ProviderConfigurationError):
            config.require_key()
        with self.assertRaises(ProviderConfigurationError):
            GeminiConfig.from_env({})

    def test_key_is_excluded_from_repr_and_invalid_settings(self):
        config = GeminiConfig(api_key=KEY)
        self.assertNotIn(KEY, repr(config))
        for settings in (
            {"api_key": KEY + "\n"}, {"model": "https://other.example"},
            {"max_seconds": 121}, {"max_seconds": True},
            {"max_turns": 0}, {"connect_timeout": float("inf")},
            {"io_timeout": 0}, {"response_seconds": 31},
        ):
            with self.subTest(settings=settings):
                with self.assertRaises(ProviderConfigurationError) as raised:
                    GeminiConfig(**settings)
                self.assertNotIn(KEY, str(raised.exception))

    def test_model_and_duration_environment_with_cli_override(self):
        source = {"GEMINI_API_KEY": KEY, "GEMINI_MODEL": "gemini-test-live",
                  "JARVIS_GEMINI_MAX_SECONDS": "20"}
        config = GeminiConfig.from_env(source)
        self.assertEqual(config.model, "gemini-test-live")
        self.assertEqual(config.max_seconds, 20)
        self.assertEqual(GeminiConfig.from_env(source, model=DEFAULT_MODEL).model,
                         DEFAULT_MODEL)
        with self.assertRaises(ProviderConfigurationError):
            GeminiConfig.from_env({**source, "JARVIS_GEMINI_MAX_SECONDS": "nan"})

    def test_explicit_private_key_file_and_environment_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gemini-key"
            path.write_text(KEY, encoding="utf-8")
            path.chmod(0o600)
            self.assertEqual(read_private_key_file(str(path)), KEY)
            config = GeminiConfig.from_env({"GEMINI_API_KEY_FILE": str(path)})
            self.assertEqual(config.api_key, KEY)
            config = GeminiConfig.from_env({"GEMINI_API_KEY": "env-test-key",
                                           "GEMINI_API_KEY_FILE": "/absent"})
            self.assertEqual(config.api_key, "env-test-key")
            # Offline models do not even read an explicitly configured file.
            self.assertEqual(GeminiConfig.from_env(
                {"GEMINI_API_KEY_FILE": "/absent"}, need_key=False
            ).api_key, "")

    def test_insecure_symlink_fifo_and_large_key_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "key"
            path.write_text(KEY)
            path.chmod(0o644)
            with self.assertRaises(ProviderConfigurationError):
                read_private_key_file(str(path))
            path.chmod(0o600)
            link = Path(directory) / "link"
            link.symlink_to(path)
            with self.assertRaises(ProviderConfigurationError):
                read_private_key_file(str(link))
            path.write_bytes(b"x" * 4097)
            with self.assertRaises(ProviderConfigurationError):
                read_private_key_file(str(path))
            fifo = Path(directory) / "fifo"
            os.mkfifo(fifo, 0o600)
            with self.assertRaises(ProviderConfigurationError):
                read_private_key_file(str(fifo))
