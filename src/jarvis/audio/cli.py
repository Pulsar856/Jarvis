"""Audio argument parsing and display; imported modules do no hardware I/O."""

import argparse
import os
from pathlib import Path

from jarvis.audio.backend import AlsaBackend
from jarvis.audio.devices import Direction, select_device
from jarvis.audio.errors import AudioValidationError, DeviceNotFound
from jarvis.audio.operations import AudioService, validate_duration
from jarvis.signals import ShutdownRequest


def add_audio_commands(commands: argparse._SubParsersAction) -> None:
    """Attach explicitly requested local audio operations."""
    parser = commands.add_parser(
        "audio", help="explicit local audio operations"
    )
    audio = parser.add_subparsers(dest="audio_command", required=True)
    audio.add_parser("devices", help="list capture and playback interfaces")
    record = audio.add_parser("record", help="record a bounded PCM WAV")
    record.add_argument("--duration", type=duration_argument, default=5)
    record.add_argument("--output", type=Path, required=True)
    record.add_argument("--device", help="capture ID or 'jabra'")
    play = audio.add_parser("play", help="play a supported PCM WAV")
    play.add_argument("input", type=Path)
    play.add_argument("--device", help="playback ID or 'jabra'")
    loopback = audio.add_parser(
        "loopback", help="record then play; delete the WAV"
    )
    loopback.add_argument("--duration", type=duration_argument, default=5)
    loopback.add_argument("--input-device", help="capture ID or 'jabra'")
    loopback.add_argument("--output-device", help="playback ID or 'jabra'")


def configured_device(
    direction: Direction, override: str | None = None
) -> str:
    """CLI selection wins; only audio commands read optional audio settings."""
    key = (
        "JARVIS_AUDIO_INPUT" if direction == "capture"
        else "JARVIS_AUDIO_OUTPUT"
    )
    return override if override is not None else os.environ.get(key, "jabra")


def duration_argument(value: str) -> int:
    """Report invalid duration as a usage failure before hardware I/O."""
    try:
        duration = int(value)
        validate_duration(duration)
        return duration
    except (ValueError, AudioValidationError) as exc:
        raise argparse.ArgumentTypeError(
            "duration must be an integer from 1 to 30 seconds"
        ) from exc


def run_audio(
    arguments: argparse.Namespace, signals: ShutdownRequest | None
) -> None:
    """Display discovery separately from configured selection and health."""
    backend = AlsaBackend()
    if arguments.audio_command == "record":
        selector = configured_device("capture", arguments.device)
        result = AudioService(backend, signals).record(
            arguments.duration, arguments.output, selector
        )
        print(
            f"Recorded {result.wav.duration:g}s, {result.wav.sample_rate} Hz, "
            f"16-bit mono from {result.device.identifier}: {result.path}"
        )
        return
    if arguments.audio_command == "play":
        selector = configured_device("playback", arguments.device)
        result = AudioService(backend, signals).play(arguments.input, selector)
        channels = "mono" if result.wav.channels == 1 else "stereo"
        print(
            f"Played {result.wav.duration:g}s, {result.wav.sample_rate} Hz, "
            f"16-bit {channels} on {result.device.identifier}: {result.path}"
        )
        if result.device.is_jabra:
            print("Jabra output: 48000 Hz via ALSA sample-rate conversion.")
        return
    if arguments.audio_command == "loopback":
        result = AudioService(backend, signals).loopback(
            arguments.duration,
            configured_device("capture", arguments.input_device),
            configured_device("playback", arguments.output_device),
        )
        print(
            f"Loopback completed: {result.wav.duration:g}s, "
            f"{result.wav.sample_rate} Hz, 16-bit mono; "
            f"{result.capture.identifier} -> {result.playback.identifier}. "
            "Temporary WAV removed."
        )
        if result.playback.is_jabra:
            print("Jabra output: 48000 Hz via ALSA sample-rate conversion.")
        return
    print("Detected hardware interfaces (enumeration is not an access test):")
    for direction, key in (
        ("capture", "JARVIS_AUDIO_INPUT"),
        ("playback", "JARVIS_AUDIO_OUTPUT"),
    ):
        devices = backend.devices(direction, signals)
        configured = os.environ.get(key, "jabra")
        try:
            selected = select_device(devices, configured, direction).identifier
        except DeviceNotFound:
            selected = None
        print(f"{direction.capitalize()} interfaces:")
        for device in devices:
            tags = []
            if device.is_jabra:
                tags.append("Jabra")
            if device.identifier == selected:
                tags.append("configured")
            suffix = f" [{', '.join(tags)}]" if tags else ""
            print(
                f"  {device.identifier} — {device.card_name}, "
                f"{device.device_name}{suffix}"
            )
            if device.persistent_id is not None:
                print(f"    ALSA alias: {device.alsa_identifier}")
            if device.native_capabilities:
                print("    Native: " + "; ".join(device.native_capabilities))
        if not devices:
            print("  none detected")
        status = selected or "not detected or ambiguous"
        print(f"  Configured selector: {configured!r} -> {status}")
