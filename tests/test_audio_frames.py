"""PCM contracts do not depend on hardware, files, or provider formats."""

import unittest
from dataclasses import FrozenInstanceError

from jarvis.audio.errors import AudioValidationError
from jarvis.audio.frames import PCMFormat, PCMFrame


class FrameTests(unittest.TestCase):
    def test_default_frame_size_distinguishes_samples_and_bytes(self) -> None:
        pcm = PCMFormat()
        self.assertEqual(pcm.samples_per_frame, 320)
        self.assertEqual(pcm.frame_bytes, 640)
        self.assertEqual(pcm.duration, 0.02)
        self.assertEqual(pcm.sample_format, "S16_LE")

    def test_stereo_frame_has_interleaved_sample_alignment(self) -> None:
        pcm = PCMFormat(sample_rate=48000, channels=2, frame_ms=10)
        self.assertEqual(pcm.samples_per_frame, 480)
        self.assertEqual(pcm.bytes_per_sample_frame, 4)
        self.assertEqual(pcm.frame_bytes, 1920)
        frame = PCMFrame(pcm, bytes(1920), 3, 1.25)
        self.assertEqual(frame.sequence, 3)
        self.assertEqual(frame.captured_at, 1.25)

    def test_invalid_format_and_fractional_samples_are_rejected(self) -> None:
        for settings in (
            {"sample_format": "FLOAT_LE"}, {"sample_rate": 0},
            {"sample_rate": 192001}, {"sample_rate": True},
            {"channels": 3}, {"channels": True},
            {"frame_ms": 9}, {"frame_ms": 101}, {"frame_ms": 20.0},
            {"sample_rate": 44100, "frame_ms": 13},
        ):
            with self.subTest(settings=settings):
                with self.assertRaises(AudioValidationError):
                    PCMFormat(**settings)

    def test_wrong_byte_counts_and_mutable_payloads_are_rejected(self) -> None:
        for payload in (b"", bytes(639), bytes(642), bytearray(640)):
            with self.subTest(length=len(payload)):
                with self.assertRaises(AudioValidationError):
                    PCMFrame(PCMFormat(), payload)

    def test_sequence_and_timestamp_validation(self) -> None:
        for settings in (
            {"sequence": -1}, {"sequence": True}, {"sequence": 1.5},
            {"captured_at": float("nan")}, {"captured_at": float("inf")},
            {"captured_at": -1}, {"captured_at": "now"},
        ):
            with self.subTest(settings=settings):
                with self.assertRaises(AudioValidationError):
                    PCMFrame(PCMFormat(), bytes(640), **settings)

    def test_metadata_is_immutable_and_repr_excludes_audio(self) -> None:
        payload = (b"private-voice" * 64)[:640]
        self.assertEqual(len(payload), 640)
        frame = PCMFrame(PCMFormat(), payload)
        with self.assertRaises(FrozenInstanceError):
            frame.sequence = 1
        with self.assertRaises(FrozenInstanceError):
            frame.format.channels = 2
        self.assertNotIn("private", repr(frame))
