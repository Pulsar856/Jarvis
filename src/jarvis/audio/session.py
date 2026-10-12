"""Subordinate stream ownership with bounded handoff and minimal workers."""

import logging
import time
from dataclasses import dataclass
from enum import Enum
from threading import Event, Lock, Thread
from types import TracebackType

from jarvis.audio.backend import AlsaBackend
from jarvis.audio.buffers import BufferMetrics, FrameBuffer, OverflowPolicy
from jarvis.audio.control import POLL_SECONDS, StreamControl, validate_timeout
from jarvis.audio.devices import select_device
from jarvis.audio.errors import (
    AudioCancelled, AudioOperationError, AudioTimeout, AudioValidationError,
    StreamCancelled,
)
from jarvis.audio.frames import PCMFormat, PCMFrame
from jarvis.audio.streaming import (
    CaptureStream, PlaybackStream, TransportMetrics,
)
from jarvis.signals import ShutdownRequest

logger = logging.getLogger(__name__)


class SessionState(Enum):
    INITIALIZED = "initialized"
    STARTING = "starting"
    RUNNING = "running"
    FAILED = "failed"
    STOPPING = "stopping"
    STOPPED = "stopped"


@dataclass(frozen=True)
class SessionMetrics:
    state: str
    failed: bool
    capture: TransportMetrics | None
    playback: TransportMetrics | None
    capture_buffer: BufferMetrics | None
    playback_buffer: BufferMetrics | None
    cleanup_seconds: float


class StreamSession:
    """Own explicit streams; use a context manager for deterministic cleanup.

    One caller consumes capture and one caller produces playback. Workers alone
    own pipe I/O. request_stop() is safe from another ordinary thread; close()
    belongs to the owner and joins workers. No process handlers are installed.
    """

    def __init__(
        self, pcm: PCMFormat = PCMFormat(),
        capture_selector: str | None = "jabra",
        playback_selector: str | None = None,
        capacity: int = 10, io_timeout: float = 2,
        signals: ShutdownRequest | None = None,
        backend: AlsaBackend | None = None,
    ) -> None:
        if capture_selector is None and playback_selector is None:
            raise AudioValidationError("Enable capture, playback, or both.")
        validate_timeout(io_timeout)
        self.format = pcm
        self.capture_selector = capture_selector
        self.playback_selector = playback_selector
        self.io_timeout = io_timeout
        self.backend = AlsaBackend() if backend is None else backend
        self.control = StreamControl(signals)
        self.capture_buffer = (
            FrameBuffer(
                pcm, capacity, OverflowPolicy.DROP_OLDEST, self.control
            )
            if capture_selector is not None else None
        )
        self.playback_buffer = (
            FrameBuffer(pcm, capacity, OverflowPolicy.BLOCK, self.control)
            if playback_selector is not None else None
        )
        self.capture: CaptureStream | None = None
        self.playback: PlaybackStream | None = None
        self._state = SessionState.INITIALIZED
        self._lock = Lock()
        self._close_lock = Lock()
        self._failure: Exception | None = None
        self._threads: list[Thread] = []
        self._capture_done = Event()
        self._playback_done = Event()
        self._cleanup_seconds = 0.0
        self._last_submitted = -1
        self._closed = False

    @property
    def state(self) -> SessionState:
        with self._lock:
            return self._state

    def _transition(self, state: SessionState) -> None:
        with self._lock:
            logger.info(
                "PCM session: %s -> %s", self._state.value, state.value
            )
            self._state = state

    def _check(self) -> None:
        with self._lock:
            error = self._failure
        if error is not None:
            raise error
        self.control.check()

    def _require_running(self) -> None:
        self._check()
        if self.state is not SessionState.RUNNING:
            raise AudioOperationError("PCM session is not running.")

    def _fail(self, error: Exception) -> None:
        with self._lock:
            if self._failure is None:
                self._failure = error
                logger.error("PCM session failure (%s)", type(error).__name__)
                logger.info("PCM session: %s -> failed", self._state.value)
                self._state = SessionState.FAILED
        self.request_stop()

    def start(self, capture_frames: int | None = None) -> None:
        """Resolve endpoints before I/O; optional limit counts full frames."""
        if self.state is not SessionState.INITIALIZED:
            raise AudioOperationError("PCM session can only start once.")
        if capture_frames is not None and (
            type(capture_frames) is not int or capture_frames <= 0
            or self.capture_selector is None
        ):
            raise AudioValidationError("Capture frame limit must be positive.")
        self._transition(SessionState.STARTING)
        try:
            self._check()
            capture_device = (
                select_device(
                    self.backend.devices("capture", self.control.signals,
                                         cancel=self.control.check),
                    self.capture_selector, "capture",
                )
                if self.capture_selector is not None else None
            )
            playback_device = (
                select_device(
                    self.backend.devices("playback", self.control.signals,
                                         cancel=self.control.check),
                    self.playback_selector, "playback",
                )
                if self.playback_selector is not None else None
            )
            self._check()
            if capture_device is not None:
                self.capture = CaptureStream(
                    capture_device, self.format, self.control
                )
                self.capture.start()
            if playback_device is not None:
                self.playback = PlaybackStream(
                    playback_device, self.format, self.control
                )
                self.playback.start()
            self._transition(SessionState.RUNNING)
            for target, name, args in (
                (self._capture_loop, "capture", (capture_frames,)),
                (self._playback_loop, "playback", ()),
            ):
                if getattr(self, name) is not None:
                    worker = Thread(
                        target=target, args=args, name=f"jarvis-pcm-{name}"
                    )
                    worker.start()
                    self._threads.append(worker)
        except BaseException as exc:
            if isinstance(exc, Exception) and not isinstance(
                exc, (AudioCancelled, StreamCancelled)
            ):
                self._fail(exc)
            self.close()
            raise

    def _capture_loop(self, limit: int | None) -> None:
        try:
            count = 0
            while limit is None or count < limit:
                frame = self.capture.read_frame(self.io_timeout)
                self.capture_buffer.put(frame)
                count += 1
        except (AudioCancelled, StreamCancelled):
            pass
        except Exception as exc:
            self._fail(exc)
        finally:
            try:
                self.capture.close()
            except Exception as exc:
                self._fail(exc)
            self.capture_buffer.close()
            self._capture_done.set()

    def _playback_loop(self) -> None:
        try:
            while True:
                try:
                    frame = self.playback_buffer.get(min(.25, self.io_timeout))
                except AudioTimeout:
                    self.playback.check_health()
                    continue
                if frame is None:
                    self.playback.finish(min(60, self.io_timeout + 5))
                    break
                self.playback.write_frame(frame, self.io_timeout)
        except (AudioCancelled, StreamCancelled):
            pass
        except Exception as exc:
            self._fail(exc)
        finally:
            try:
                self.playback.close()
            except Exception as exc:
                self._fail(exc)
            self._playback_done.set()

    def read_frame(self, timeout: float = 2) -> PCMFrame | None:
        """Return captured data or None after a capture limit drains."""
        if self.capture_buffer is None:
            raise AudioOperationError("Session has no capture stream.")
        self._require_running()
        try:
            result = self.capture_buffer.get(timeout)
        except StreamCancelled:
            self._check()
            raise
        self._check()
        return result

    def write_frame(self, frame: PCMFrame, timeout: float = 2) -> None:
        """Admit playback; full queues time out without silent loss."""
        if self.playback_buffer is None:
            raise AudioOperationError("Session has no playback stream.")
        self._require_running()
        if not isinstance(frame, PCMFrame) or frame.format != self.format:
            raise AudioValidationError("Playback frame format does not match.")
        if frame.sequence <= self._last_submitted:
            raise AudioValidationError("Playback sequence must increase.")
        try:
            self.playback_buffer.put(frame, timeout)
        except StreamCancelled:
            self._check()
            raise
        self._last_submitted = frame.sequence

    def finish_playback(self, timeout: float = 10) -> None:
        """Drain admitted frames and hardware; explicit stop instead aborts."""
        validate_timeout(timeout)
        if self.playback_buffer is None:
            raise AudioOperationError("Session has no playback stream.")
        self._require_running()
        self.playback_buffer.close()
        deadline = time.monotonic() + timeout
        while not self._playback_done.wait(POLL_SECONDS):
            self._check()
            if time.monotonic() >= deadline:
                error = AudioTimeout(
                    "PCM playback drain exceeded its deadline."
                )
                self._fail(error)
                self.close()
                raise error
        self._check()

    def request_stop(self) -> None:
        """Request abort from any ordinary thread; close() owns joining."""
        self.control.request_stop()
        for buffer in (self.capture_buffer, self.playback_buffer):
            if buffer is not None:
                buffer.close(discard=True)

    def close(self) -> None:
        """Stop once, join both workers with a shared three-second budget."""
        with self._close_lock:
            if self._closed:
                return
            began = time.monotonic()
            self._transition(SessionState.STOPPING)
            self.request_stop()
            deadline = began + 3
            for worker in self._threads:
                worker.join(max(0, deadline - time.monotonic()))
            alive = [worker for worker in self._threads if worker.is_alive()]
            if alive:
                raise AudioTimeout(
                    "PCM worker did not stop within its budget."
                )
            for transport in (self.capture, self.playback):
                if transport is not None:
                    transport.close()
            self._cleanup_seconds = time.monotonic() - began
            self._closed = True
            self._transition(SessionState.STOPPED)

    @property
    def metrics(self) -> SessionMetrics:
        with self._lock:
            state = self._state.value
            failed = self._failure is not None
        return SessionMetrics(
            state, failed,
            self.capture.metrics if self.capture is not None else None,
            self.playback.metrics if self.playback is not None else None,
            self.capture_buffer.metrics
            if self.capture_buffer is not None else None,
            self.playback_buffer.metrics
            if self.playback_buffer is not None else None,
            self._cleanup_seconds,
        )

    def __enter__(self) -> "StreamSession":
        if self.state is SessionState.INITIALIZED:
            self.start()
        else:
            self._require_running()
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None,
        exc_value: BaseException | None, traceback: TracebackType | None,
    ) -> None:
        if isinstance(exc_value, Exception) and not isinstance(
            exc_value, (AudioCancelled, StreamCancelled)
        ):
            self._fail(exc_value)
        self.close()
        with self._lock:
            error = self._failure
        if exc_value is None and error is not None:
            raise error
