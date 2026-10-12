"""Validate local PCM inputs and output failures with a fake backend."""

import argparse
import os
import subprocess
import tempfile
import unittest
import wave
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from jarvis.audio.backend import AlsaBackend
from jarvis.audio.cli import add_audio_commands
from jarvis.audio.devices import AudioDevice
from jarvis.audio.errors import (
    AudioOperationError, AudioTimeout, AudioValidationError, DeviceNotFound,
)
from jarvis.audio.operations import AudioService

PLAYBACK = AudioDevice("playback", 7, "Jabra", "Jabra SPEAK 510", 0, "USB")


class PlaybackTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.path = self.directory / "sample.wav"
        self.backend = Mock(spec=AlsaBackend)
        self.backend.devices.return_value = (PLAYBACK,)
        self.service = AudioService(self.backend)

    def write_wav(
        self, seconds: int = 1, rate: int = 16000,
        channels: int = 1, width: int = 2,
    ) -> None:
        with wave.open(str(self.path), "wb") as output:
            output.setnchannels(channels)
            output.setsampwidth(width)
            output.setframerate(rate)
            output.writeframes(b"\0" * seconds * rate * channels * width)

    def test_pcm_playback_preserves_input_and_metadata(self) -> None:
        self.write_wav(rate=48000, channels=2)
        original = self.path.read_bytes()
        result = self.service.play(self.path)
        self.assertEqual(result.wav.sample_rate, 48000)
        self.assertEqual(result.wav.channels, 2)
        self.assertEqual(result.wav.duration, 1)
        self.backend.play.assert_called_once_with(PLAYBACK, self.path, 1, None)
        self.assertEqual(self.path.read_bytes(), original)

    def test_invalid_input_is_rejected_before_device_discovery(self) -> None:
        for path in (self.directory, self.directory / "absent.wav"):
            with self.subTest(path=path):
                with self.assertRaises(AudioValidationError):
                    self.service.play(path)
        self.path.write_text("not a wave file", encoding="utf-8")
        with self.assertRaises(AudioValidationError):
            self.service.play(self.path)
        self.backend.devices.assert_not_called()

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO validation")
    def test_fifo_is_rejected_without_blocking(self) -> None:
        os.mkfifo(self.path)
        with self.assertRaises(AudioValidationError):
            self.service.play(self.path)
        self.backend.play.assert_not_called()

    def test_unsupported_pcm_formats_and_lengths(self) -> None:
        cases = (
            {"width": 1}, {"channels": 3}, {"rate": 4000},
            {"seconds": 0}, {"seconds": 61},
        )
        for settings in cases:
            with self.subTest(settings=settings):
                self.write_wav(**settings)
                with self.assertRaises(AudioValidationError):
                    self.service.play(self.path)
        self.backend.play.assert_not_called()

    def test_missing_jabra_is_explicit(self) -> None:
        self.write_wav()
        self.backend.devices.return_value = ()
        with self.assertRaises(DeviceNotFound):
            self.service.play(self.path)
        self.backend.play.assert_not_called()

    def test_playback_failure_and_timeout_preserve_input(self) -> None:
        self.write_wav()
        original = self.path.read_bytes()
        for error in (
            AudioOperationError("disconnected"), AudioTimeout("deadline")
        ):
            with self.subTest(error=type(error).__name__):
                self.backend.play.side_effect = error
                with self.assertRaises(type(error)):
                    self.service.play(self.path)
                self.assertEqual(self.path.read_bytes(), original)

    def test_backend_playback_failure_reports_diagnostic(self) -> None:
        with (
            patch("jarvis.audio.backend.utility_path", return_value="aplay"),
            patch("jarvis.audio.backend.run_command") as run,
        ):
            run.return_value = subprocess.CompletedProcess(
                [], 1, "", "device busy"
            )
            with self.assertRaisesRegex(AudioOperationError, "device busy"):
                AlsaBackend().play(PLAYBACK, self.path, 1)

    def test_playback_timeout_propagates(self) -> None:
        with (
            patch("jarvis.audio.backend.utility_path", return_value="aplay"),
            patch(
                "jarvis.audio.backend.run_command",
                side_effect=AudioTimeout("deadline"),
            ),
        ):
            with self.assertRaises(AudioTimeout):
                AlsaBackend().play(PLAYBACK, self.path, 1)

    def test_jabra_playback_converts_without_relabeling_wav(self) -> None:
        with (
            patch("jarvis.audio.backend.utility_path", return_value="aplay"),
            patch("jarvis.audio.backend.run_command") as run,
        ):
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            AlsaBackend().play(PLAYBACK, self.path, 1)
        arguments = run.call_args.args[0]
        self.assertEqual(
            arguments[arguments.index("-D") + 1],
            'samplerate:SLAVE="plughw:CARD=Jabra,DEV=0",RATE=48000',
        )
        self.assertNotIn("-r", arguments)

    def test_other_playback_devices_keep_named_plughw(self) -> None:
        device = replace(PLAYBACK, card_id="HDMI", card_name="HDMI Output")
        with (
            patch("jarvis.audio.backend.utility_path", return_value="aplay"),
            patch("jarvis.audio.backend.run_command") as run,
        ):
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            AlsaBackend().play(device, self.path, 1)
        arguments = run.call_args.args[0]
        self.assertEqual(
            arguments[arguments.index("-D") + 1], "plughw:CARD=HDMI,DEV=0"
        )

    def test_play_cli_parses_input_and_explicit_device(self) -> None:
        parser = argparse.ArgumentParser()
        add_audio_commands(parser.add_subparsers(dest="command"))
        args = parser.parse_args([
            "audio", "play", "sample.wav", "--device", "Jabra:0"
        ])
        self.assertEqual(args.input, Path("sample.wav"))
        self.assertEqual(args.device, "Jabra:0")
