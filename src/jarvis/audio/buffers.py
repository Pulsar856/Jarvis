"""Finite PCM handoff with freshness and lossless playback admission."""

import time
from collections import deque
from dataclasses import dataclass, replace
from enum import Enum
from threading import Condition

from jarvis.audio.control import POLL_SECONDS, StreamControl, validate_timeout
from jarvis.audio.errors import (
    AudioOperationError, AudioTimeout, AudioValidationError,
)
from jarvis.audio.frames import PCMFormat, PCMFrame

MAX_BUFFER_FRAMES = 256


class OverflowPolicy(Enum):
    DROP_OLDEST = "drop_oldest"
    BLOCK = "block"


@dataclass(frozen=True)
class BufferMetrics:
    accepted: int = 0
    consumed: int = 0
    dropped: int = 0
    discarded: int = 0
    overflows: int = 0
    underflows: int = 0
    high_water: int = 0
    occupancy: int = 0
    wait_seconds: float = 0


class FrameBuffer:
    """A bounded FIFO; closed buffers drain to None, cancellation raises."""

    def __init__(
        self, pcm: PCMFormat, capacity: int = 10,
        policy: OverflowPolicy = OverflowPolicy.BLOCK,
        control: StreamControl | None = None,
    ) -> None:
        if not isinstance(pcm, PCMFormat):
            raise AudioValidationError("Buffer requires a PCMFormat.")
        if type(capacity) is not int or not 1 <= capacity <= MAX_BUFFER_FRAMES:
            raise AudioValidationError("Buffer capacity must be 1–256 frames.")
        if not isinstance(policy, OverflowPolicy):
            raise AudioOperationError("Unknown buffer overflow policy.")
        self.format = pcm
        self.capacity = capacity
        self.policy = policy
        self.control = StreamControl() if control is None else control
        self._frames: deque[PCMFrame] = deque()
        self._condition = Condition()
        self._closed = False
        self._metrics = BufferMetrics()

    @property
    def metrics(self) -> BufferMetrics:
        with self._condition:
            return replace(self._metrics, occupancy=len(self._frames))

    def _wait(self, remaining: float) -> None:
        began = time.monotonic()
        self._condition.wait(min(POLL_SECONDS, remaining))
        self._metrics = replace(
            self._metrics, wait_seconds=self._metrics.wait_seconds
            + time.monotonic() - began,
        )

    def put(self, frame: PCMFrame, timeout: float = 2) -> None:
        """Capture can drop oldest; playback waits or explicitly times out."""
        validate_timeout(timeout, allow_zero=True)
        if not isinstance(frame, PCMFrame) or frame.format != self.format:
            raise AudioOperationError("Buffer frame format does not match.")
        deadline = time.monotonic() + timeout
        with self._condition:
            self.control.check()
            if self._closed:
                raise AudioOperationError("PCM buffer is closed.")
            if len(self._frames) == self.capacity:
                self._metrics = replace(
                    self._metrics, overflows=self._metrics.overflows + 1
                )
                if self.policy is OverflowPolicy.DROP_OLDEST:
                    self._frames.popleft()
                    self._metrics = replace(
                        self._metrics, dropped=self._metrics.dropped + 1
                    )
            while len(self._frames) == self.capacity:
                self.control.check()
                if self._closed:
                    raise AudioOperationError("PCM buffer is closed.")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AudioTimeout(
                        "Playback buffer is full; retry or stop."
                    )
                self._wait(remaining)
            self.control.check()
            if self._closed:
                raise AudioOperationError("PCM buffer is closed.")
            self._frames.append(frame)
            self._metrics = replace(
                self._metrics, accepted=self._metrics.accepted + 1,
                high_water=max(self._metrics.high_water, len(self._frames)),
            )
            self._condition.notify_all()

    def get(self, timeout: float = 2) -> PCMFrame | None:
        """Return FIFO data, a drain marker, or a bounded starvation error."""
        validate_timeout(timeout, allow_zero=True)
        deadline = time.monotonic() + timeout
        with self._condition:
            while not self._frames:
                self.control.check()
                if self._closed:
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._metrics = replace(
                        self._metrics, underflows=self._metrics.underflows + 1
                    )
                    raise AudioTimeout("PCM buffer has no frame available.")
                self._wait(remaining)
            self.control.check()
            frame = self._frames.popleft()
            self._metrics = replace(
                self._metrics, consumed=self._metrics.consumed + 1
            )
            self._condition.notify_all()
            return frame

    def close(self, discard: bool = False) -> None:
        """Wake waiters; optionally account for and discard queued frames."""
        with self._condition:
            self._closed = True
            if discard:
                self._metrics = replace(
                    self._metrics,
                    discarded=self._metrics.discarded + len(self._frames),
                )
                self._frames.clear()
            self._condition.notify_all()
