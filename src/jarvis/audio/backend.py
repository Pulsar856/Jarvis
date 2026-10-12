"""ALSA utility boundary: bounded child processes and deterministic cleanup."""

import os
import shutil
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from jarvis.audio.devices import (
    AudioDevice, Direction, native_capabilities, parse_devices,
)
from jarvis.audio.errors import (
    AudioCancelled, AudioOperationError, AudioTimeout, BackendUnavailable,
)
from jarvis.signals import ShutdownRequest

CAPTURE_RATE = 16000
CAPTURE_CHANNELS = 1
CAPTURE_WIDTH = 2
JABRA_PLAYBACK_RATE = 48000


def playback_pcm(device: AudioDevice) -> str:
    """Keep the verified Jabra conversion policy shared by WAV and raw I/O."""
    if device.is_jabra:
        return (
            f'samplerate:SLAVE="{device.pcm}",RATE={JABRA_PLAYBACK_RATE}'
        )
    return device.pcm


def check_cancelled(signals: ShutdownRequest | None) -> None:
    """Consume recorded signal intent outside the signal callback."""
    if signals is not None and signals.signum is not None:
        raise AudioCancelled(signals.signum)


def _signal_process(process: subprocess.Popen[str], signum: int) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signum)
        else:
            process.send_signal(signum)
    except ProcessLookupError:
        pass


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        _signal_process(process, signal.SIGTERM)
    try:
        process.communicate(timeout=1)
    except subprocess.TimeoutExpired:
        _signal_process(process, signal.SIGKILL)
        process.communicate(timeout=1)


def run_command(
    arguments: list[str], timeout: float,
    signals: ShutdownRequest | None = None,
    cancel: Callable[[], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run without a shell; kill and reap the private group on interruption."""
    check_cancelled(signals)
    if cancel is not None:
        cancel()
    environment = dict(os.environ, LC_ALL="C")
    try:
        process = subprocess.Popen(
            arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace", env=environment, start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise BackendUnavailable(
            "The selected audio utility is missing."
        ) from exc
    except OSError as exc:
        raise AudioOperationError("Cannot start the audio utility.") from exc
    deadline = time.monotonic() + timeout
    try:
        while True:
            check_cancelled(signals)
            if cancel is not None:
                cancel()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AudioTimeout("Audio utility exceeded its deadline.")
            try:
                output, errors = process.communicate(
                    timeout=min(0.1, remaining)
                )
            except subprocess.TimeoutExpired:
                continue
            check_cancelled(signals)
            if cancel is not None:
                cancel()
            return subprocess.CompletedProcess(
                arguments, process.returncode, output, errors
            )
    except BaseException:
        _stop_process(process)
        raise


def utility_path(name: str) -> str:
    """Resolve a fixed utility name only when an audio command requests it."""
    executable = shutil.which(name)
    if executable is None:
        raise BackendUnavailable(
            f"{name} is unavailable; the Debian package is alsa-utils."
        )
    return executable


def require_success(
    result: subprocess.CompletedProcess[str], operation: str
) -> None:
    """Report diagnostics without audio payloads or environment dumps."""
    if result.returncode != 0:
        detail = result.stderr.strip()[:2000] or "no utility diagnostic"
        raise AudioOperationError(
            f"{operation} failed (exit {result.returncode}): {detail}"
        )


def persistent_identifier(card_index: int) -> str | None:
    """Resolve the optional by-id control symlink using the current index."""
    try:
        for entry in sorted(Path("/dev/snd/by-id").iterdir()):
            try:
                target = entry.resolve(strict=True)
                if target.name == f"controlC{card_index}":
                    return entry.name
            except (OSError, RuntimeError):
                continue
    except OSError:
        pass
    return None


class AlsaBackend:
    """Use installed arecord/aplay and named plughw devices."""

    def devices(
        self, direction: Direction, signals: ShutdownRequest | None = None,
        cancel: Callable[[], None] | None = None,
    ) -> tuple[AudioDevice, ...]:
        name = "arecord" if direction == "capture" else "aplay"
        result = run_command(
            [utility_path(name), "-l"], 5, signals, cancel
        )
        if "no soundcards found" in result.stderr.casefold():
            return ()
        require_success(result, f"{direction.capitalize()} discovery")
        devices = []
        for device in parse_devices(result.stdout, direction):
            path = (
                Path("/proc/asound") / f"card{device.card_index}"
                / f"stream{device.device_index}"
            )
            try:
                capabilities = native_capabilities(
                    path.read_text(encoding="utf-8", errors="replace"),
                    direction,
                )
            except OSError:
                capabilities = ()
            devices.append(replace(
                device, native_capabilities=capabilities,
                persistent_id=persistent_identifier(device.card_index),
            ))
        return tuple(devices)

    def record(
        self, device: AudioDevice, output: Path, duration: int,
        signals: ShutdownRequest | None = None,
    ) -> None:
        """Capture exact mono S16_LE frames into a private file."""
        arguments = [
            utility_path("arecord"), "-q", "-N", "--fatal-errors",
            "-D", device.pcm, "-t", "wav", "-f", "S16_LE",
            "-r", str(CAPTURE_RATE), "-c", str(CAPTURE_CHANNELS),
            "-s", str(duration * CAPTURE_RATE), str(output),
        ]
        require_success(
            run_command(arguments, duration + 5, signals), "Recording"
        )

    def play(
        self, device: AudioDevice, path: Path, duration: float,
        signals: ShutdownRequest | None = None,
    ) -> None:
        """Read WAV format; use verified 48 kHz output for Jabra playback."""
        arguments = [
            utility_path("aplay"), "-q", "-N", "--fatal-errors",
            "-D", playback_pcm(device), "-t", "wav", str(path),
        ]
        require_success(
            run_command(arguments, duration + 5, signals), "Playback"
        )
