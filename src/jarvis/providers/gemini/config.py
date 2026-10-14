"""Provider-local settings; core commands never require or load a Gemini key."""

import math
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from jarvis.providers.gemini.errors import ProviderConfigurationError

DEFAULT_MODEL = "gemini-3.8-live"
DOCUMENTED_MODELS = (
    (DEFAULT_MODEL, "current recommended Live model"),
    ("gemini-3.1-flash-live-preview", "legacy Live preview"),
    ("gemini-2.5-flash-native-audio-preview-12-2025", "native-audio preview"),
)


def read_private_key_file(path: str) -> str:
    """Read only the explicitly configured, private, owner-held regular file."""
    try:
        target = Path(path).expanduser().absolute()
        for parent in Path(__file__).resolve().parents:
            if (parent / ".git").exists():
                if target.resolve().is_relative_to(parent):
                    raise ProviderConfigurationError(
                        "Keep the Gemini key file outside the repository."
                    )
                break
        descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 4096
            ):
                raise ProviderConfigurationError(
                    "Gemini key file must be owner-held, regular, "
                    "at most 4096 bytes, and private (mode 0600)."
                )
            return os.read(descriptor, 4097).decode("utf-8").strip()
        finally:
            os.close(descriptor)
    except (OSError, ValueError, UnicodeError, RuntimeError) as exc:
        if isinstance(exc, ProviderConfigurationError):
            raise
        raise ProviderConfigurationError(
            "Cannot read the explicitly configured private Gemini key file."
        ) from None


@dataclass(frozen=True)
class GeminiConfig:
    api_key: str = field(default="", repr=False)
    model: str = DEFAULT_MODEL
    max_seconds: int = 60
    max_turns: int = 3
    connect_timeout: float = 10
    io_timeout: float = 5
    response_seconds: int = 15

    def __post_init__(self) -> None:
        if not isinstance(self.api_key, str) or (
            self.api_key and (
                len(self.api_key) > 4096
                or any(char.isspace() or ord(char) < 33
                       for char in self.api_key)
                or self.api_key.startswith("auth_tokens/")
            )
        ):
            raise ProviderConfigurationError(
                "Use a normal Gemini API key without whitespace."
            )
        if not isinstance(self.model, str) or not re.fullmatch(
            r"gemini-[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", self.model
        ):
            raise ProviderConfigurationError("GEMINI_MODEL is invalid.")
        for value, maximum, label in (
            (self.max_seconds, 120, "session seconds"),
            (self.max_turns, 5, "turn limit"),
            (self.response_seconds, 30, "response audio seconds"),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ProviderConfigurationError(
                    f"Gemini {label} must be an integer from 1 to {maximum}."
                )
        for value in (self.connect_timeout, self.io_timeout):
            if (
                type(value) not in (int, float) or not math.isfinite(value)
                or not 0 < value <= 30
            ):
                raise ProviderConfigurationError(
                    "Gemini timeouts must be positive, finite, and <=30s."
                )

    def require_key(self) -> None:
        if not self.api_key:
            raise ProviderConfigurationError(
                "Set GEMINI_API_KEY locally or configure GEMINI_API_KEY_FILE. "
                "Never paste the key into chat or supply it as a CLI argument."
            )

    @classmethod
    def from_env(
        cls, environ: Mapping[str, str] | None = None,
        need_key: bool = True, model: str | None = None,
    ) -> "GeminiConfig":
        source = os.environ if environ is None else environ
        key = source.get("GEMINI_API_KEY", "") if need_key else ""
        if need_key and not key and source.get("GEMINI_API_KEY_FILE"):
            key = read_private_key_file(source["GEMINI_API_KEY_FILE"])
        try:
            result = cls(
                api_key=key,
                model=source.get("GEMINI_MODEL", DEFAULT_MODEL)
                if model is None else model,
                max_seconds=int(source.get("JARVIS_GEMINI_MAX_SECONDS", "60")),
            )
        except (ValueError, TypeError):
            raise ProviderConfigurationError(
                "JARVIS_GEMINI_MAX_SECONDS must be an integer from 1 to 120."
            ) from None
        if need_key:
            result.require_key()
        return result
