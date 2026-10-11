"""Configuration defaults, overrides, and startup validation."""

import unittest
from pathlib import Path
from unittest.mock import patch

from jarvis import __version__
from jarvis.config import AppConfig, ConfigurationError


class ConfigTests(unittest.TestCase):
    def test_defaults_require_no_environment(self) -> None:
        config = AppConfig.from_env({})
        self.assertEqual(config.app_name, "Jarvis")
        self.assertEqual(config.app_version, __version__)
        self.assertEqual(config.log_level, "INFO")
        self.assertIsNone(config.log_dir)

    def test_environment_overrides(self) -> None:
        config = AppConfig.from_env(
            {
                "JARVIS_APP_NAME": " My Jarvis ",
                "JARVIS_LOG_LEVEL": " debug ",
                "JARVIS_LOG_DIR": " ~/jarvis-logs ",
                "JARVIS_APP_VERSION": "untrusted-version",
            }
        )
        self.assertEqual(config.app_name, "My Jarvis")
        self.assertEqual(config.log_level, "DEBUG")
        self.assertEqual(config.log_dir, Path.home() / "jarvis-logs")
        self.assertEqual(config.app_version, __version__)

    def test_process_environment_is_used_by_default(self) -> None:
        with patch.dict(
            "os.environ", {"JARVIS_LOG_LEVEL": "warning"}, clear=True
        ):
            self.assertEqual(AppConfig.from_env().log_level, "WARNING")

    def test_invalid_values_produce_setting_specific_errors(self) -> None:
        cases = (
            ("JARVIS_APP_NAME", ""),
            ("JARVIS_APP_NAME", "   "),
            ("JARVIS_APP_NAME", "Jarvis\nforged log"),
            ("JARVIS_LOG_LEVEL", "verbose"),
            ("JARVIS_LOG_LEVEL", ""),
            ("JARVIS_LOG_DIR", "   "),
            ("JARVIS_LOG_DIR", "invalid\x00directory"),
        )
        for name, value in cases:
            with self.subTest(name=name, value=value):
                with self.assertRaisesRegex(ConfigurationError, name):
                    AppConfig.from_env({name: value})

    def test_direct_construction_is_validated(self) -> None:
        with self.assertRaises(ConfigurationError):
            AppConfig(log_level="verbose")
        with self.assertRaises(ConfigurationError):
            AppConfig(app_name="")
        with self.assertRaises(ConfigurationError):
            AppConfig(log_dir="logs")  # type: ignore[arg-type]

    def test_invalid_value_is_not_echoed(self) -> None:
        sensitive = "example-sensitive-value"
        with self.assertRaises(ConfigurationError) as raised:
            AppConfig.from_env({"JARVIS_LOG_LEVEL": sensitive})
        self.assertNotIn(sensitive, str(raised.exception))
