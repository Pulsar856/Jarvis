"""Real synthetic pipe sources, never ALSA hardware or microphone capture."""

import os
import signal
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from jarvis.audio.control import StreamControl
from jarvis.audio.devices import AudioDevice
from jarvis.audio.errors import (
    AudioCancelled, AudioOperationError, AudioTimeout, BackendUnavailable,
    StreamCancelled,
)
from jarvis.audio.frames import PCMFormat
from jarvis.audio.streaming import CaptureStream, raw_arguments
from jarvis.signals import ShutdownRequest

DEVICE = AudioDevice("capture", 7, "Jabra", "Jabra SPEAK 510", 0, "USB")


class CaptureStreamTests(unittest.TestCase):
    def start(self, code: str, control: StreamControl | None = None):
        stream = CaptureStream(DEVICE, control=control)
        self.addCleanup(stream.close)
        with patch(
            "jarvis.audio.streaming.raw_arguments",
            return_value=[sys.executable, "-c", code],
        ):
            stream.start()
        return stream

    def assert_reaped(self, stream: CaptureStream) -> None:
        self.assertIsNotNone(stream.process.poll())
        self.assertTrue(stream.process.stdout.closed)
        self.assertTrue(stream.process.stderr.closed)

    def test_one_process_produces_ordered_frames_from_partial_reads(self):
        stream = self.start(
            "import os,time; "
            "parts=[b'a'*101,b'a'*539,b'b'*639,b'b',b'c'*640]; "
            "[(os.write(1,p),time.sleep(.01)) for p in parts]; "
            "time.sleep(60)"
        )
        frames = [stream.read_frame() for _ in range(3)]
        self.assertEqual([frame.sequence for frame in frames], [0, 1, 2])
        self.assertEqual(
            [frame.payload for frame in frames],
            [b'a' * 640, b'b' * 640, b'c' * 640],
        )
        self.assertEqual(stream.metrics.frames, 3)
        self.assertEqual(stream.metrics.bytes, 1920)
        self.assertEqual(stream.metrics.pipe_capacity_bytes,
                         max(4096, os.sysconf("SC_PAGESIZE")))
        self.assertGreaterEqual(stream.metrics.first_frame_seconds, 0)
        stream.close()
        stream.close()
        self.assert_reaped(stream)

    def test_eof_rejects_partial_frame_and_reaps_process(self):
        stream = self.start("import os; os.write(1,b'x'*319)")
        with self.assertRaisesRegex(AudioOperationError, "EOF"):
            stream.read_frame()
        self.assertEqual(stream.metrics.frames, 0)
        self.assert_reaped(stream)

    def test_empty_eof_and_device_failure_are_clear(self):
        for code, text in (
            ("pass", "EOF"),
            ("import sys; print('disconnected',file=sys.stderr); "
             "sys.exit(3)", "disconnected"),
        ):
            with self.subTest(code=code):
                stream = self.start(code)
                with self.assertRaisesRegex(AudioOperationError, text):
                    stream.read_frame()
                self.assert_reaped(stream)

    def test_stalled_pipe_timeout_is_bounded_and_reaped(self):
        stream = self.start("import time; time.sleep(60)")
        began = time.monotonic()
        with self.assertRaises(AudioTimeout):
            stream.read_frame(timeout=.15)
        self.assertLess(time.monotonic() - began, 3)
        self.assert_reaped(stream)

    def test_programmatic_stop_unblocks_a_reader(self):
        control = StreamControl()
        stream = self.start("import time; time.sleep(60)", control)
        stopped = threading.Event()
        errors = []

        def read():
            try:
                stream.read_frame(timeout=5)
            except BaseException as exc:
                errors.append(exc)
            finally:
                stopped.set()

        worker = threading.Thread(target=read)
        worker.start()
        control.request_stop()
        self.assertTrue(stopped.wait(3))
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(errors[0], StreamCancelled)
        self.assert_reaped(stream)

    def test_existing_signal_request_is_consumed_without_new_handlers(self):
        request = ShutdownRequest()
        stream = self.start(
            "import time; time.sleep(60)", StreamControl(request)
        )
        request.record(signal.SIGTERM, None)
        with self.assertRaises(AudioCancelled):
            stream.read_frame()
        stream.close()
        self.assert_reaped(stream)

    def test_missing_utility_and_partial_setup_do_not_leak(self):
        stream = CaptureStream(DEVICE)
        with patch(
            "jarvis.audio.streaming.raw_arguments",
            return_value=["/definitely/absent/audio-tool"],
        ):
            with self.assertRaises(BackendUnavailable):
                stream.start()
        stream = CaptureStream(DEVICE)
        with (
            patch("jarvis.audio.streaming.raw_arguments", return_value=[
                sys.executable, "-c", "import time; time.sleep(60)",
            ]),
            patch("jarvis.audio.streaming.os.set_blocking",
                  side_effect=OSError("cannot configure pipe")),
        ):
            with self.assertRaises(AudioOperationError):
                stream.start()
        self.assert_reaped(stream)

    def test_diagnostic_flood_is_drained_with_bounded_retention(self):
        stream = self.start(
            "import os,time; os.write(2,b'd'*200000); "
            "os.write(1,b'x'*640); time.sleep(60)"
        )
        self.assertEqual(stream.read_frame().payload, b'x' * 640)
        self.assertLessEqual(len(stream._diagnostic), 4096)

    def test_raw_arguments_declare_format_and_finite_alsa_buffer(self):
        with patch("jarvis.audio.streaming.utility_path", return_value="tool"):
            args = raw_arguments(DEVICE, PCMFormat())
        for flag, value in (
            ("-t", "raw"), ("-r", "16000"), ("-f", "S16_LE"),
            ("-c", "1"), ("-F", "20000"), ("-B", "80000"),
        ):
            self.assertEqual(args[args.index(flag) + 1], value)
        self.assertNotIn("-s", args)
        self.assertNotIn("-d", args)

    def test_cleanup_kills_and_reaps_a_child_ignoring_sigterm(self):
        stream = self.start(
            "import os,signal,time; signal.signal(signal.SIGTERM, "
            "signal.SIG_IGN); os.write(1,bytes(640)); time.sleep(60)"
        )
        stream.read_frame()
        stream.close()
        self.assertEqual(stream.process.returncode, -signal.SIGKILL)
        self.assert_reaped(stream)
