"""Progressive playback to synthetic pipe sinks, never physical speakers."""

import os
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from jarvis.audio.control import StreamControl
from jarvis.audio.devices import AudioDevice
from jarvis.audio.errors import (
    AudioOperationError, AudioTimeout, AudioValidationError, StreamCancelled,
)
from jarvis.audio.frames import PCMFormat, PCMFrame
from jarvis.audio.streaming import PlaybackStream, raw_arguments

DEVICE = AudioDevice("playback", 7, "Jabra", "Jabra SPEAK 510", 0, "USB")


class PlaybackStreamTests(unittest.TestCase):
    def start(self, code: str, control: StreamControl | None = None):
        stream = PlaybackStream(DEVICE, control=control)
        self.addCleanup(stream.close)
        with patch(
            "jarvis.audio.streaming.raw_arguments",
            return_value=[sys.executable, "-c", code],
        ):
            stream.start()
        return stream

    def test_partial_writes_preserve_progressive_pcm_without_headers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sink.bin"
            stream = self.start(
                "import sys; from pathlib import Path; "
                f"Path({str(path)!r}).write_bytes(sys.stdin.buffer.read())"
            )
            original = os.write

            def small_write(fd, data):
                return original(fd, data[:31])

            with patch("jarvis.audio.streaming.os.write",
                       side_effect=small_write):
                for sequence in range(3):
                    stream.write_frame(PCMFrame(
                        PCMFormat(), bytes([sequence]) * 640, sequence
                    ))
            stream.finish()
            stream.finish()
            self.assertEqual(
                path.read_bytes(), b'\0' * 640 + b'\1' * 640 + b'\2' * 640
            )
        self.assertEqual(stream.metrics.frames, 3)
        self.assertEqual(stream.metrics.bytes, 1920)
        self.assertIsNotNone(stream.metrics.first_frame_seconds)
        self.assertIsNotNone(stream.process.poll())
        self.assertTrue(stream.process.stdin.closed)
        self.assertTrue(stream.process.stderr.closed)

    def test_rejects_unsupported_format_and_reordered_frames(self):
        stream = self.start("import sys; sys.stdin.buffer.read()")
        with self.assertRaises(AudioValidationError):
            stream.write_frame(PCMFrame(PCMFormat(channels=2), bytes(1280)))
        stream.write_frame(PCMFrame(PCMFormat(), bytes(640), 3))
        for sequence in (2, 3):
            with self.assertRaises(AudioValidationError):
                stream.write_frame(PCMFrame(PCMFormat(), bytes(640), sequence))
        stream.write_frame(PCMFrame(PCMFormat(), bytes(640), 5))
        stream.finish()
        self.assertEqual(stream.metrics.frames, 2)

    def test_broken_pipe_and_unexpected_exit_reap_the_child(self):
        stream = self.start("import sys; sys.stdin.buffer.read()")
        with patch("jarvis.audio.streaming.os.write",
                   side_effect=BrokenPipeError()):
            with self.assertRaisesRegex(AudioOperationError, "pipe closed"):
                stream.write_frame(PCMFrame(PCMFormat(), bytes(640)))
        self.assertIsNotNone(stream.process.poll())
        stream = self.start(
            "import sys; print('device busy', file=sys.stderr); sys.exit(2)"
        )
        with self.assertRaisesRegex(AudioOperationError, "device busy"):
            stream.finish()
        self.assertIsNotNone(stream.process.poll())

    def test_nonreading_sink_times_out_without_blocking_forever(self):
        stream = self.start("import time; time.sleep(60)")
        began = time.monotonic()
        with self.assertRaises(AudioTimeout):
            for sequence in range(4096):
                stream.write_frame(
                    PCMFrame(PCMFormat(), bytes(640), sequence), timeout=.1
                )
        self.assertLess(time.monotonic() - began, 3)
        self.assertIsNotNone(stream.process.poll())

    def test_finish_timeout_aborts_and_reaps(self):
        stream = self.start(
            "import sys,time; sys.stdin.buffer.read(); time.sleep(60)"
        )
        stream.write_frame(PCMFrame(PCMFormat(), bytes(640)))
        with self.assertRaises(AudioTimeout):
            stream.finish(timeout=.15)
        self.assertIsNotNone(stream.process.poll())

    def test_programmatic_cancellation_cleans_up(self):
        control = StreamControl()
        stream = self.start("import time; time.sleep(60)", control)
        control.request_stop()
        with self.assertRaises(StreamCancelled):
            stream.write_frame(PCMFrame(PCMFormat(), bytes(640)))
        self.assertIsNotNone(stream.process.poll())

    def test_zero_write_progress_is_a_failure(self):
        stream = self.start("import sys; sys.stdin.buffer.read()")
        with patch("jarvis.audio.streaming.os.write", return_value=0):
            with self.assertRaisesRegex(AudioOperationError, "progress"):
                stream.write_frame(PCMFrame(PCMFormat(), bytes(640)))
        self.assertIsNotNone(stream.process.poll())

    def test_jabra_raw_format_keeps_explicit_conversion(self):
        with patch("jarvis.audio.streaming.utility_path", return_value="tool"):
            args = raw_arguments(DEVICE, PCMFormat())
            other = raw_arguments(
                replace(DEVICE, card_name="HDMI", card_id="HDMI"), PCMFormat()
            )
        self.assertEqual(
            args[args.index("-D") + 1],
            'samplerate:SLAVE="plughw:CARD=Jabra,DEV=0",RATE=48000',
        )
        self.assertEqual(args[args.index("-r") + 1], "16000")
        self.assertEqual(args[args.index("-c") + 1], "1")
        self.assertEqual(
            other[other.index("-D") + 1], "plughw:CARD=HDMI,DEV=0"
        )
