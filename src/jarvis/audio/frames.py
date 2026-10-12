"""Immutable, header-free PCM frames for transports and future adapters."""

import math
from dataclasses import dataclass, field

from jarvis.audio.errors import AudioValidationError


@dataclass(frozen=True)
class PCMFormat:
    """Logical interleaved PCM format; sample frames include every channel."""

    sample_rate: int = 16000
    channels: int = 1
    frame_ms: int = 20
    sample_format: str = "S16_LE"

    def __post_init__(self) -> None:
        if self.sample_format != "S16_LE":
            raise AudioValidationError("Streaming supports S16_LE PCM only.")
        if (
            type(self.sample_rate) is not int
            or not 8000 <= self.sample_rate <= 192000
            or type(self.channels) is not int or self.channels not in (1, 2)
            or type(self.frame_ms) is not int or not 10 <= self.frame_ms <= 100
        ):
            raise AudioValidationError(
                "PCM requires 8000–192000 Hz, one or two channels, "
                "and integer frames of 10–100 ms."
            )
        if self.sample_rate * self.frame_ms % 1000:
            raise AudioValidationError(
                "Frame duration must contain an integral number of samples."
            )

    @property
    def samples_per_frame(self) -> int:
        return self.sample_rate * self.frame_ms // 1000

    @property
    def bytes_per_sample_frame(self) -> int:
        return self.channels * 2

    @property
    def frame_bytes(self) -> int:
        return self.samples_per_frame * self.bytes_per_sample_frame

    @property
    def duration(self) -> float:
        return self.frame_ms / 1000


@dataclass(frozen=True)
class PCMFrame:
    """One exact logical frame; sequence gaps may represent reported drops.

    The optional timestamp describes when Python assembled the frame, not an
    acoustic timestamp. Payloads are excluded from repr to avoid voice logs.
    """

    format: PCMFormat
    payload: bytes = field(repr=False)
    sequence: int = 0
    captured_at: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.format, PCMFormat):
            raise AudioValidationError("Frame requires a validated PCMFormat.")
        if not isinstance(self.payload, bytes):
            raise AudioValidationError("PCM payload must be immutable bytes.")
        if len(self.payload) != self.format.frame_bytes:
            raise AudioValidationError(
                f"PCM frame requires exactly {self.format.frame_bytes} bytes."
            )
        if type(self.sequence) is not int or self.sequence < 0:
            raise AudioValidationError("Frame sequence must be nonnegative.")
        if self.captured_at is not None and (
            type(self.captured_at) not in (int, float)
            or not math.isfinite(self.captured_at) or self.captured_at < 0
        ):
            raise AudioValidationError("Frame timestamp must be finite.")
