"""Explicit record/play orchestration and temporary-file cleanup."""

import argparse
import signal
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock

from jarvis.audio.backend import AlsaBackend
from jarvis.audio.cli import add_audio_commands
from jarvis.audio.devices import AudioDevice
from jarvis.audio.errors import (
    AudioCancelled, AudioOperationError, DeviceNotFound,
)
from jarvis.audio.operations import AudioService

CAPTURE = AudioDevice("capture", 7, "Jabra", "Jabra SPEAK 510", 0, "USB")
PLAYBACK = AudioDevice("playback", 7, "Jabra", "Jabra SPEAK 510", 0, "USB")


class LoopbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = Mock(spec=AlsaBackend)
        self.backend.devices.side_effect = lambda direction, signals: (
            (CAPTURE,) if direction == "capture" else (PLAYBACK,)
        )
        self.paths = []

        def record(
            device: object, path: Path, seconds: int, signals: object
        ) -> None:
            self.paths.append(path)
            with wave.open(str(path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16000)
                output.writeframes(b"\0\0" * seconds * 16000)

        self.backend.record.side_effect = record
        self.service = AudioService(self.backend)

    def test_loopback_resolves_both_devices_before_recording(self) -> None:
        result = self.service.loopback(1)
        self.assertEqual(result.wav.duration, 1)
        self.assertEqual(result.capture, CAPTURE)
        self.assertEqual(result.playback, PLAYBACK)
        self.assertEqual(
            [call[0] for call in self.backend.mock_calls],
            ["devices", "devices", "record", "play"],
        )
        played = self.backend.play.call_args.args[1]
        self.assertFalse(played.exists())
        self.assertFalse(played.parent.exists())
        self.assertFalse(self.paths[0].exists())

    def test_missing_output_prevents_any_recording(self) -> None:
        self.backend.devices.side_effect = [(CAPTURE,), ()]
        with self.assertRaises(DeviceNotFound):
            self.service.loopback(1)
        self.backend.record.assert_not_called()

    def test_failure_or_cancellation_removes_temporary_wav(self) -> None:
        for error in (
            AudioOperationError("disconnected"), AudioCancelled(signal.SIGINT)
        ):
            with self.subTest(error=type(error).__name__):
                self.backend.play.side_effect = error
                with self.assertRaises(type(error)):
                    self.service.loopback(1)
                self.assertFalse(self.paths[-1].parent.exists())

    def test_loopback_cli_parses_both_device_selectors(self) -> None:
        parser = argparse.ArgumentParser()
        add_audio_commands(parser.add_subparsers(dest="command"))
        args = parser.parse_args([
            "audio", "loopback", "--duration", "5",
            "--input-device", "Jabra:0", "--output-device", "Jabra:0",
        ])
        self.assertEqual(args.duration, 5)
        self.assertEqual(args.input_device, "Jabra:0")
        self.assertEqual(args.output_device, "Jabra:0")
