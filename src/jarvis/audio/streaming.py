"""Incremental raw ALSA pipe transport; each instance has one I/O owner."""

import logging
import os
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass, replace
from threading import Lock

from jarvis.audio.backend import _signal_process, playback_pcm, utility_path
from jarvis.audio.control import POLL_SECONDS, StreamControl, validate_timeout
from jarvis.audio.devices import AudioDevice
from jarvis.audio.errors import (
    AudioCancelled, AudioOperationError, AudioTimeout, AudioValidationError,
    BackendUnavailable, StreamCancelled,
)
from jarvis.audio.frames import PCMFormat, PCMFrame

logger = logging.getLogger(__name__)
PCM_PIPE_BYTES = 4096


@dataclass(frozen=True)
class TransportMetrics:
    """Process/pipe timings and complete frames; never acoustic latency."""

    frames: int = 0
    bytes: int = 0
    process_start_seconds: float = 0
    first_frame_seconds: float | None = None
    io_wait_seconds: float = 0
    active_seconds: float = 0
    cleanup_seconds: float = 0
    errors: int = 0
    pipe_capacity_bytes: int = 0


def raw_arguments(device: AudioDevice, pcm: PCMFormat) -> list[str]:
    """Declare logical format and request four ALSA periods of buffering.

    No filename means stdout capture or stdin playback. ALSA may negotiate
    period/buffer sizes; these flags are requests, not hardware guarantees.
    """
    capture = device.direction == "capture"
    name = "arecord" if capture else "aplay"
    return [
        utility_path(name), "-q", "-N", "--fatal-errors", "-D",
        device.pcm if capture else playback_pcm(device),
        "-t", "raw", "-f", pcm.sample_format,
        "-r", str(pcm.sample_rate), "-c", str(pcm.channels),
        "-F", str(pcm.frame_ms * 1000),
        "-B", str(pcm.frame_ms * 4000),
    ]


class _PCMProcess:
    """Concrete shared pipe ownership for the two ALSA stream directions."""

    def __init__(
        self, device: AudioDevice, pcm: PCMFormat,
        control: StreamControl | None = None,
    ) -> None:
        if not isinstance(pcm, PCMFormat):
            raise AudioValidationError("Transport requires a PCMFormat.")
        self.device = device
        self.format = pcm
        self.control = StreamControl() if control is None else control
        self.process: subprocess.Popen[bytes] | None = None
        self._selector: selectors.BaseSelector | None = None
        self._diagnostic = bytearray()
        self._closed = False
        self._started_at: float | None = None
        self._metrics = TransportMetrics()
        self._metrics_lock = Lock()

    @property
    def metrics(self) -> TransportMetrics:
        with self._metrics_lock:
            result = self._metrics
        if self._started_at is not None and not self._closed:
            result = replace(
                result, active_seconds=time.monotonic() - self._started_at
            )
        return result

    def start(self) -> None:
        """Acquire once; partial acquisition is always rolled back."""
        if self.process is not None or self._closed:
            raise AudioOperationError("PCM transport can only start once.")
        self.control.check()
        arguments = raw_arguments(self.device, self.format)
        capture = self.device.direction == "capture"
        self._started_at = time.monotonic()
        try:
            self.process = subprocess.Popen(
                arguments,
                stdin=subprocess.DEVNULL if capture else subprocess.PIPE,
                stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                stderr=subprocess.PIPE, bufsize=0, start_new_session=True,
                pipesize=PCM_PIPE_BYTES,
                env=dict(os.environ, LC_ALL="C"),
            )
            self._selector = selectors.DefaultSelector()
            os.set_blocking(self.process.stderr.fileno(), False)
            self._selector.register(
                self.process.stderr, selectors.EVENT_READ, "diagnostic"
            )
            pipe = self.process.stdout if capture else self.process.stdin
            # Linux rounds to a page; query the actual capacity for metrics.
            from fcntl import F_GETPIPE_SZ, fcntl
            pipe_capacity = fcntl(pipe.fileno(), F_GETPIPE_SZ)
            os.set_blocking(pipe.fileno(), False)
            self._selector.register(
                pipe, selectors.EVENT_READ if capture
                else selectors.EVENT_WRITE, "pcm",
            )
            with self._metrics_lock:
                self._metrics = replace(
                    self._metrics,
                    process_start_seconds=time.monotonic() - self._started_at,
                    pipe_capacity_bytes=pipe_capacity,
                )
            logger.info(
                "PCM %s opened on %s", self.device.direction,
                self.device.identifier,
            )
        except BaseException as exc:
            self.close()
            if isinstance(exc, FileNotFoundError):
                raise BackendUnavailable("Audio utility disappeared.") from exc
            if isinstance(exc, OSError):
                raise AudioOperationError("Cannot open PCM pipes.") from exc
            raise

    def _require_open(self) -> None:
        self.control.check()
        if self.process is None or self._closed:
            raise AudioOperationError("PCM transport is not open.")

    def check_health(self) -> None:
        """Detect an exited backend even when playback has no queued input."""
        self._require_open()
        if self.process.poll() is not None:
            self._drain_diagnostic()
            self._backend_error("Audio process exited unexpectedly")

    def _drain_diagnostic(self) -> None:
        pipe = self.process.stderr
        try:
            chunk = os.read(pipe.fileno(), 4096)
        except BlockingIOError:
            return
        if chunk:
            self._diagnostic.extend(chunk)
            del self._diagnostic[:-4096]
        else:
            try:
                self._selector.unregister(pipe)
            except KeyError:
                pass

    def _wait(self, deadline: float) -> bool:
        """Drain diagnostics while waiting for data or pipe capacity."""
        self.control.check()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AudioTimeout("PCM pipe operation exceeded its deadline.")
        began = time.monotonic()
        events = self._selector.select(min(POLL_SECONDS, remaining))
        with self._metrics_lock:
            self._metrics = replace(
                self._metrics, io_wait_seconds=self._metrics.io_wait_seconds
                + time.monotonic() - began,
            )
        ready = False
        for key, mask in events:
            if key.data == "diagnostic":
                self._drain_diagnostic()
            else:
                ready = True
        self.control.check()
        if self.process.poll() not in (None, 0):
            self._drain_diagnostic()
            self._backend_error("Audio process exited unexpectedly")
        return ready

    def _backend_error(self, message: str) -> None:
        detail = self._diagnostic.decode("utf-8", errors="replace").strip()
        raise AudioOperationError(
            f"{message} (exit {self.process.poll()}): "
            f"{detail or 'no utility diagnostic'}"
        )

    def _completed_frame(self) -> None:
        with self._metrics_lock:
            now = time.monotonic()
            self._metrics = replace(
                self._metrics, frames=self._metrics.frames + 1,
                bytes=self._metrics.bytes + self.format.frame_bytes,
                first_frame_seconds=(
                    now - self._started_at
                    if self._metrics.first_frame_seconds is None
                    else self._metrics.first_frame_seconds
                ),
            )

    def _failed_operation(self, exc: BaseException) -> None:
        if not isinstance(exc, (AudioCancelled, StreamCancelled)):
            with self._metrics_lock:
                self._metrics = replace(
                    self._metrics, errors=self._metrics.errors + 1
                )
        self.close()

    def close(self) -> None:
        """Abort idempotently; terminate/kill the owned group and reap it."""
        if self._closed:
            return
        began = time.monotonic()
        try:
            if self.process is not None:
                _signal_process(self.process, signal.SIGTERM)
                try:
                    self.process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    _signal_process(self.process, signal.SIGKILL)
                    try:
                        self.process.wait(timeout=1)
                    except subprocess.TimeoutExpired as exc:
                        raise AudioTimeout(
                            "Audio process did not exit after SIGKILL."
                        ) from exc
                # Also kill any residual descendants in the private group.
                _signal_process(self.process, signal.SIGKILL)
        finally:
            if self._selector is not None:
                self._selector.close()
            if self.process is not None:
                for pipe in (
                    self.process.stdin, self.process.stdout,
                    self.process.stderr,
                ):
                    if pipe is not None:
                        pipe.close()
            with self._metrics_lock:
                self._metrics = replace(
                    self._metrics,
                    active_seconds=(
                        began - self._started_at
                        if self._started_at is not None else 0
                    ),
                    cleanup_seconds=time.monotonic() - began,
                )
            self._closed = True


class CaptureStream(_PCMProcess):
    """Read fixed logical frames from one continuous arecord process."""

    def __init__(
        self, device: AudioDevice, pcm: PCMFormat = PCMFormat(),
        control: StreamControl | None = None,
    ) -> None:
        if device.direction != "capture":
            raise AudioValidationError("Capture requires a capture device.")
        super().__init__(device, pcm, control)
        self._partial = bytearray()
        self._sequence = 0

    def read_frame(self, timeout: float = 2) -> PCMFrame:
        """Assemble partial reads; EOF never publishes an incomplete frame."""
        validate_timeout(timeout)
        deadline = time.monotonic() + timeout
        try:
            self._require_open()
            while len(self._partial) < self.format.frame_bytes:
                if not self._wait(deadline):
                    continue
                try:
                    chunk = os.read(
                        self.process.stdout.fileno(),
                        self.format.frame_bytes - len(self._partial),
                    )
                except BlockingIOError:
                    continue
                if not chunk:
                    self._backend_error(
                        "Capture EOF with incomplete PCM frame"
                        if self._partial else "Unexpected capture EOF"
                    )
                self._partial.extend(chunk)
            frame = PCMFrame(
                self.format, bytes(self._partial), self._sequence,
                time.monotonic(),
            )
            self._partial.clear()
            self._sequence += 1
            self._completed_frame()
            return frame
        except BaseException as exc:
            self._partial.clear()
            self._failed_operation(exc)
            raise


class PlaybackStream(_PCMProcess):
    """Progressively write logical frames; finish drains, close aborts."""

    def __init__(
        self, device: AudioDevice, pcm: PCMFormat = PCMFormat(),
        control: StreamControl | None = None,
    ) -> None:
        if device.direction != "playback":
            raise AudioValidationError("Playback requires a playback device.")
        super().__init__(device, pcm, control)
        self._last_sequence = -1
        self._finished = False

    def write_frame(self, frame: PCMFrame, timeout: float = 2) -> None:
        """Write one frame, with bounded backpressure on partial writes."""
        validate_timeout(timeout)
        if not isinstance(frame, PCMFrame) or frame.format != self.format:
            raise AudioValidationError("Playback frame format does not match.")
        if frame.sequence <= self._last_sequence:
            raise AudioValidationError(
                "Playback frame sequences must increase; gaps are allowed."
            )
        deadline = time.monotonic() + timeout
        try:
            self._require_open()
            pending = memoryview(frame.payload)
            while pending:
                if not self._wait(deadline):
                    continue
                try:
                    written = os.write(self.process.stdin.fileno(), pending)
                except BlockingIOError:
                    continue
                except BrokenPipeError:
                    self._drain_diagnostic()
                    self._backend_error("Playback pipe closed unexpectedly")
                if written <= 0:
                    raise AudioOperationError(
                        "Playback made no write progress."
                    )
                pending = pending[written:]
            self._last_sequence = frame.sequence
            self._completed_frame()
        except BaseException as exc:
            self._failed_operation(exc)
            raise

    def finish(self, timeout: float = 5) -> None:
        """Send EOF and wait for successful hardware drain, without a WAV."""
        validate_timeout(timeout)
        if self._finished:
            return
        deadline = time.monotonic() + timeout
        try:
            self._require_open()
            self._selector.unregister(self.process.stdin)
            self.process.stdin.close()
            while self.process.poll() is None:
                self._wait(deadline)
            self._drain_diagnostic()
            if self.process.returncode != 0:
                self._backend_error("Playback drain failed")
            self._finished = True
            self.close()
        except BaseException as exc:
            self._failed_operation(exc)
            raise
