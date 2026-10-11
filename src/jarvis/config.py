"""Validated configuration; no files or external services are required."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from jarvis import __version__

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class ConfigurationError(ValueError):
    """Configuration cannot be used to start the application."""


@dataclass(frozen=True)
class AppConfig:
    """Shared application settings, validated on construction."""

    app_name: str = "Jarvis"
    app_version: str = field(default=__version__, init=False)
    log_level: str = "INFO"
    log_dir: Path | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.app_name, str) or not self.app_name.strip():
            raise ConfigurationError("JARVIS_APP_NAME must not be empty.")
        if any(ord(char) < 32 or ord(char) == 127 for char in self.app_name):
            raise ConfigurationError(
                "JARVIS_APP_NAME must not contain control characters."
            )
        if self.log_level not in LOG_LEVELS:
            raise ConfigurationError(
                "JARVIS_LOG_LEVEL must be one of: "
                + ", ".join(LOG_LEVELS)
                + "."
            )
        if self.log_dir is not None and not isinstance(self.log_dir, Path):
            raise ConfigurationError("log_dir must be a pathlib.Path or None.")
        if self.log_dir is not None and any(
            ord(char) < 32 or ord(char) == 127 for char in str(self.log_dir)
        ):
            raise ConfigurationError(
                "JARVIS_LOG_DIR must not contain control characters."
            )

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "AppConfig":
        """Load only documented settings from the supplied environment."""
        source = os.environ if environ is None else environ
        directory = source.get("JARVIS_LOG_DIR")
        if directory is not None and not directory.strip():
            raise ConfigurationError(
                "JARVIS_LOG_DIR must be a directory path when set."
            )
        try:
            log_dir = (
                Path(directory.strip()).expanduser() if directory else None
            )
        except RuntimeError as exc:
            raise ConfigurationError(
                "JARVIS_LOG_DIR cannot be expanded."
            ) from exc
        return cls(
            app_name=source.get("JARVIS_APP_NAME", "Jarvis").strip(),
            log_level=source.get("JARVIS_LOG_LEVEL", "INFO").strip().upper(),
            log_dir=log_dir,
        )
