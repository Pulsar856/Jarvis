"""Recording safety and PCM metadata using synthetic audio."""

import argparse
import signal
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

from jarvis.audio.backend import AlsaBackend
from jarvis.audio.cli import add_audio_commands
from jarvis.audio.devices import AudioDevice
from jarvis.audio.errors import (
    AudioCancelled, AudioOperationError, AudioTimeout, AudioValidationError,
)
from jarvis.audio.operations import AudioService, inspect_wav
from jarvis.signals import ShutdownRequest

CAPTURE = AudioDevice("capture", 7, "Jabra", "Jabra SPEAK 510", 0, "USB")


def write_wav(
    path: Path, seconds: int = 1, rate: int = 16000,
    channels: int = 1, width: int = 2,
) -> None:
    with wave.open(str(path), "wb") as destination:
        destination.setnchannels(channels)
        destination.setsampwidth(width)
        destination.setframerate(rate)
        destination.writeframes(b"\0" * seconds * rate * channels * width)


class RecordingTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.output = self.directory / "recording.wav"
        self.backend = Mock(spec=AlsaBackend)
        self.backend.devices.return_value = (CAPTURE,)

        def record(
            device: AudioDevice, path: Path, duration: int, signals: object
        ) -> None:
            write_wav(path, seconds=duration)

        self.backend.record.side_effect = record
        self.service = AudioService(self.backend)

    def test_recording_has_metadata_and_no_temporary_file(self) -> None:
        result = self.service.record(1, self.output)
        self.assertEqual(result.wav.sample_rate, 16000)
        self.assertEqual(result.wav.channels, 1)
        self.assertEqual(result.wav.sample_width, 2)
        self.assertEqual(result.wav.frames, 16000)
        self.assertEqual(result.wav.duration, 1)
        self.assertEqual(list(self.directory.iterdir()), [self.output])
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)

    def test_invalid_durations_fail_before_any_device_access(self) -> None:
        for value in (0, -1, 31, 1.5, True, "5", float("inf")):
            with self.subTest(value=value):
                with self.assertRaises(AudioValidationError):
                    self.service.record(value, self.output)
        self.backend.devices.assert_not_called()
        self.backend.record.assert_not_called()

    def test_invalid_output_paths_fail_before_capture(self) -> None:
        for path in (
            self.directory / "recording.mp3",
            self.directory / "absent" / "recording.wav",
            self.directory / "bad\x00name.wav",
        ):
            with self.subTest(path=path):
                with self.assertRaises(AudioValidationError):
                    self.service.record(1, path)
        self.backend.devices.assert_not_called()

    def test_existing_file_is_preserved(self) -> None:
        self.output.write_bytes(b"unrelated content")
        with self.assertRaisesRegex(AudioValidationError, "exists"):
            self.service.record(1, self.output)
        self.assertEqual(self.output.read_bytes(), b"unrelated content")
        self.backend.record.assert_not_called()

    def test_broken_symlink_is_not_replaced(self) -> None:
        self.output.symlink_to(self.directory / "absent.wav")
        with self.assertRaises(AudioValidationError):
            self.service.record(1, self.output)
        self.assertTrue(self.output.is_symlink())
        self.backend.record.assert_not_called()

    def test_file_created_during_recording_is_preserved(self) -> None:
        def record(
            device: object, path: Path, duration: int, signals: object
        ) -> None:
            write_wav(path)
            self.output.write_bytes(b"created by another writer")

        self.backend.record.side_effect = record
        with self.assertRaises(AudioValidationError):
            self.service.record(1, self.output)
        self.assertEqual(
            self.output.read_bytes(), b"created by another writer"
        )
        self.assertEqual(list(self.directory.iterdir()), [self.output])

    def test_failures_and_interrupts_remove_temporary_files(self) -> None:
        for error in (
            AudioOperationError("disconnected"), AudioTimeout("deadline"),
            AudioCancelled(signal.SIGTERM), KeyboardInterrupt(),
        ):
            with self.subTest(error=type(error).__name__):
                self.backend.record.side_effect = error
                with self.assertRaises(type(error)):
                    self.service.record(1, self.output)
                self.assertEqual(list(self.directory.iterdir()), [])

    def test_pending_signal_prevents_capture(self) -> None:
        service = AudioService(self.backend, ShutdownRequest(signal.SIGTERM))
        with self.assertRaises(AudioCancelled):
            service.record(1, self.output)
        self.backend.devices.assert_not_called()

    def test_incorrect_rate_or_short_capture_is_not_published(self) -> None:
        for rate, seconds in ((8000, 1), (16000, 0)):
            def record(
                device: object, path: Path, duration: int, signals: object
            ) -> None:
                write_wav(path, seconds=seconds, rate=rate)

            with self.subTest(rate=rate, seconds=seconds):
                self.backend.record.side_effect = record
                with self.assertRaises(AudioValidationError):
                    self.service.record(1, self.output)
                self.assertEqual(list(self.directory.iterdir()), [])

    def test_truncated_wav_is_rejected(self) -> None:
        write_wav(self.output)
        self.output.write_bytes(self.output.read_bytes()[:-20])
        with self.assertRaisesRegex(AudioValidationError, "truncated"):
            inspect_wav(self.output)

    def test_record_command_parses_device_duration_and_output(self) -> None:
        parser = argparse.ArgumentParser()
        add_audio_commands(parser.add_subparsers(dest="command"))
        args = parser.parse_args([
            "audio", "record", "--duration", "5", "--output", "test.wav",
            "--device", "Jabra:0",
        ])
        self.assertEqual(args.duration, 5)
        self.assertEqual(args.output, Path("test.wav"))
        self.assertEqual(args.device, "Jabra:0")

    def test_recording_subprocess_failure_reports_diagnostic(self) -> None:
        with (
            patch("jarvis.audio.backend.utility_path", return_value="arecord"),
            patch("jarvis.audio.backend.run_command") as run,
        ):
            run.return_value = subprocess.CompletedProcess(
                [], 1, "", "device disconnected"
            )
            with self.assertRaisesRegex(AudioOperationError, "disconnected"):
                AlsaBackend().record(CAPTURE, self.output, 1)
