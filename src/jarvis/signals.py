"""Scoped process signals that defer shutdown work to normal execution."""

import signal
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from types import FrameType

SignalHandler = Callable[[int, FrameType | None], object] | int


class SignalHandlingError(RuntimeError):
    """Shutdown handlers could not be installed without changing ownership."""


@dataclass
class ShutdownRequest:
    """Record the first signal; repeated signals have the same stop intent."""

    signum: int | None = None

    def record(self, signum: int, frame: FrameType | None) -> None:
        """Record intent only: no logging, locks, exceptions, or cleanup."""
        if self.signum is None:
            self.signum = signum


@contextmanager
def capture_shutdown_signals() -> Iterator[ShutdownRequest]:
    """Handle SIGINT/SIGTERM temporarily and restore even after setup failure.

    Call from the main thread. Do not overwrite a foreign C-level handler that
    Python cannot restore. The caller owns the application and logging cleanup.
    """
    request = ShutdownRequest()
    previous: dict[signal.Signals, SignalHandler] = {}
    try:
        try:
            for signum in (signal.SIGINT, signal.SIGTERM):
                handler = signal.getsignal(signum)
                if handler is None:
                    raise SignalHandlingError(
                        "An existing shutdown signal handler cannot "
                        "be restored."
                    )
                signal.signal(signum, request.record)
                previous[signum] = handler
        except (ValueError, OSError) as exc:
            raise SignalHandlingError(
                "Cannot install shutdown handlers; run the CLI in the "
                "main thread."
            ) from exc
        yield request
    finally:
        for signum, handler in reversed(tuple(previous.items())):
            signal.signal(signum, handler)
