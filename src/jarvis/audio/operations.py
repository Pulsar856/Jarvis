"""Safe bounded WAV operations, independent of the generic application core."""

import logging
import os
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

from jarvis.audio.backend import (
    CAPTURE_CHANNELS, CAPTURE_RATE, CAPTURE_WIDTH,
    AlsaBackend, check_cancelled,
)
from jarvis.audio.devices import AudioDevice, select_device
from jarvis.audio.errors import AudioOperationError, AudioValidationError
from jarvis.signals import ShutdownRequest

logger = logging.getLogger(__name__)
MAX_RECORD_SECONDS = 30
MAX_PLAY_SECONDS = 60


@dataclass(frozen=True)
class WavInfo:
    """Validated PCM metadata; no captured samples are exposed in results."""

    channels: int
    sample_width: int
    sample_rate: int
    frames: int

    @property
    def duration(self) -> float:
        return self.frames / self.sample_rate


@dataclass(frozen=True)
class Recording:
    path: Path
    device: AudioDevice
    wav: WavInfo


@dataclass(frozen=True)
class Playback:
    path: Path
    device: AudioDevice
    wav: WavInfo


@dataclass(frozen=True)
class Loopback:
    capture: AudioDevice
    playback: AudioDevice
    wav: WavInfo


def validate_duration(duration: int) -> None:
    """Reject infinite, fractional, boolean, and excessive capture requests."""
    if type(duration) is not int or not 1 <= duration <= MAX_RECORD_SECONDS:
        raise AudioValidationError(
            f"Recording duration must be an integer from 1 to "
            f"{MAX_RECORD_SECONDS} seconds."
        )


def validate_output(output: Path) -> Path:
    """Require a new WAV in an existing directory, including symlink checks."""
    if any(ord(char) < 32 or ord(char) == 127 for char in str(output)):
        raise AudioValidationError(
            "Audio paths cannot contain control characters."
        )
    try:
        path = output.expanduser().absolute()
    except (OSError, ValueError, RuntimeError) as exc:
        raise AudioValidationError(
            "Cannot use the requested output path."
        ) from exc
    if path.suffix.casefold() != ".wav":
        raise AudioValidationError("Recording output must have a .wav suffix.")
    if not path.parent.is_dir():
        raise AudioValidationError("The output directory must already exist.")
    if os.path.lexists(path):
        raise AudioValidationError("Output already exists; choose a new path.")
    return path


def inspect_wav(path: Path) -> WavInfo:
    """Validate complete 16-bit PCM mono/stereo WAV up to sixty seconds."""
    try:
        if not path.is_file():
            raise AudioValidationError("Input must be a regular WAV file.")
        with wave.open(str(path), "rb") as source:
            info = WavInfo(
                channels=source.getnchannels(),
                sample_width=source.getsampwidth(),
                sample_rate=source.getframerate(),
                frames=source.getnframes(),
            )
            if source.getcomptype() != "NONE" or info.sample_width != 2:
                raise AudioValidationError(
                    "Only uncompressed 16-bit PCM WAV is supported."
                )
            if info.channels not in (1, 2):
                raise AudioValidationError(
                    "WAV must contain one or two channels."
                )
            if not 8000 <= info.sample_rate <= 192000:
                raise AudioValidationError(
                    "WAV sample rate must be 8000–192000 Hz."
                )
            if not 0 < info.duration <= MAX_PLAY_SECONDS:
                raise AudioValidationError(
                    "WAV duration must be greater than zero and "
                    "at most 60 seconds."
                )
            remaining = info.frames * info.channels * info.sample_width
            while remaining:
                chunk = source.readframes(min(4096, info.frames))
                if not chunk:
                    raise AudioValidationError("WAV audio data is truncated.")
                remaining -= len(chunk)
                if remaining < 0:
                    raise AudioValidationError(
                        "WAV audio data has an invalid length."
                    )
            return info
    except (OSError, wave.Error, EOFError, ValueError) as exc:
        raise AudioValidationError(
            "Cannot read a supported PCM WAV file."
        ) from exc


class AudioService:
    """Compose device resolution and file safety around a concrete backend."""

    def __init__(
        self, backend: AlsaBackend | None = None,
        signals: ShutdownRequest | None = None,
    ) -> None:
        self.backend = AlsaBackend() if backend is None else backend
        self.signals = signals

    def record(
        self, duration: int, output: Path, selector: str = "jabra"
    ) -> Recording:
        """Capture only on explicit invocation; never overwrite any output."""
        validate_duration(duration)
        path = validate_output(output)
        check_cancelled(self.signals)
        device = select_device(
            self.backend.devices("capture", self.signals), selector, "capture"
        )
        return self._record(device, duration, path)

    def _record(
        self, device: AudioDevice, duration: int, output: Path
    ) -> Recording:
        check_cancelled(self.signals)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=output.parent, prefix=".jarvis-audio-", suffix=".wav",
                delete=False,
            ) as destination:
                temporary = Path(destination.name)
            logger.info(
                "Recording %s seconds from %s", duration, device.identifier
            )
            self.backend.record(device, temporary, duration, self.signals)
            check_cancelled(self.signals)
            info = inspect_wav(temporary)
            if (
                info.channels != CAPTURE_CHANNELS
                or info.sample_width != CAPTURE_WIDTH
                or info.sample_rate != CAPTURE_RATE
                or info.frames != duration * CAPTURE_RATE
            ):
                raise AudioValidationError(
                    "Recording does not match the requested PCM "
                    "format and duration."
                )
            check_cancelled(self.signals)
            os.link(temporary, output)
            logger.info("Recording saved: %s", output)
            return Recording(output, device, info)
        except FileExistsError as exc:
            raise AudioValidationError(
                "Output already exists; choose a new path."
            ) from exc
        except OSError as exc:
            raise AudioOperationError(
                "Cannot create the recording output."
            ) from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def play(self, source: Path, selector: str = "jabra") -> Playback:
        """Validate first and leave the caller's file unchanged."""
        if any(ord(char) < 32 or ord(char) == 127 for char in str(source)):
            raise AudioValidationError(
                "Audio paths cannot contain control characters."
            )
        try:
            path = source.expanduser().absolute()
        except (OSError, RuntimeError, ValueError) as exc:
            raise AudioValidationError(
                "Cannot use the requested input path."
            ) from exc
        info = inspect_wav(path)
        check_cancelled(self.signals)
        device = select_device(
            self.backend.devices("playback", self.signals),
            selector, "playback",
        )
        return self._play(device, path, info)

    def _play(
        self, device: AudioDevice, path: Path, info: WavInfo
    ) -> Playback:
        logger.info(
            "Playing %.2f seconds on %s", info.duration, device.identifier
        )
        self.backend.play(device, path, info.duration, self.signals)
        check_cancelled(self.signals)
        return Playback(path, device, info)

    def loopback(
        self, duration: int, capture_selector: str = "jabra",
        playback_selector: str = "jabra",
    ) -> Loopback:
        """Resolve both endpoints first and always remove the temporary WAV."""
        validate_duration(duration)
        check_cancelled(self.signals)
        capture = select_device(
            self.backend.devices("capture", self.signals),
            capture_selector, "capture",
        )
        playback = select_device(
            self.backend.devices("playback", self.signals),
            playback_selector, "playback",
        )
        with tempfile.TemporaryDirectory(prefix="jarvis-audio-") as directory:
            path = Path(directory) / "loopback.wav"
            recording = self._record(capture, duration, path)
            self._play(playback, path, recording.wav)
            return Loopback(capture, playback, recording.wav)
