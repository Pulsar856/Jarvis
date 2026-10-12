"""Cooperative stream cancellation without installing process handlers."""

import math
from threading import Event

from jarvis.audio.errors import (
    AudioCancelled, AudioValidationError, StreamCancelled,
)
from jarvis.signals import ShutdownRequest

POLL_SECONDS = 0.05


def validate_timeout(seconds: float, allow_zero: bool = False) -> None:
    """Keep per-operation waits finite and bounded to at most sixty seconds."""
    if (
        type(seconds) not in (int, float) or not math.isfinite(seconds)
        or seconds > 60 or seconds < 0 or (seconds == 0 and not allow_zero)
    ):
        raise AudioValidationError(
            "Stream timeout must be finite, positive, and at most 60 seconds."
        )


class StreamControl:
    """An Event for owner requests; existing signal callbacks record intent."""

    def __init__(self, signals: ShutdownRequest | None = None) -> None:
        self.signals = signals
        self.stopped = Event()

    def request_stop(self) -> None:
        """Call in ordinary execution, including from an owning thread."""
        self.stopped.set()

    def check(self) -> None:
        if self.signals is not None and self.signals.signum is not None:
            raise AudioCancelled(self.signals.signum)
        if self.stopped.is_set():
            raise StreamCancelled("PCM stream stop requested.")
