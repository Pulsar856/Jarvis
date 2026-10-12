"""Session lifecycle tests with synthetic PCM subprocesses."""

import signal
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

from jarvis.audio.backend import AlsaBackend, run_command
from jarvis.audio.devices import AudioDevice
from jarvis.audio.errors import (
    AudioCancelled, AudioOperationError, AudioTimeout, BackendUnavailable,
    DeviceNotFound, StreamCancelled,
)
from jarvis.audio.frames import PCMFormat, PCMFrame
from jarvis.audio.session import SessionState, StreamSession
from jarvis.signals import ShutdownRequest

CAPTURE = AudioDevice("capture", 7, "Jabra", "Jabra SPEAK 510", 0, "USB")
PLAYBACK = AudioDevice("playback", 7, "Jabra", "Jabra SPEAK 510", 0, "USB")


class SessionTests(unittest.TestCase):
    def create(self, capture_code=None, playback_code=None, **settings):
        backend = Mock(spec=AlsaBackend)
        backend.devices.side_effect = lambda direction, signals, cancel=None: (
            (CAPTURE,) if direction == "capture" else (PLAYBACK,)
        )
        capture_code = capture_code or (
            "import os,time; os.write(1,bytes(640*1000)); time.sleep(60)"
        )
        playback_code = playback_code or "import sys; sys.stdin.buffer.read()"

        def arguments(device, pcm):
            code = (
                capture_code if device.direction == "capture"
                else playback_code
            )
            return [sys.executable, "-c", code]

        patcher = patch("jarvis.audio.streaming.raw_arguments",
                        side_effect=arguments)
        patcher.start()
        self.addCleanup(patcher.stop)
        session = StreamSession(backend=backend, **settings)
        self.addCleanup(session.close)
        return session

    def assert_clean(self, session):
        self.assertEqual(session.state, SessionState.STOPPED)
        self.assertFalse(any(worker.is_alive() for worker in session._threads))
        for transport in (session.capture, session.playback):
            if transport is not None and transport.process is not None:
                self.assertIsNotNone(transport.process.poll())
                self.assertTrue(transport.process.stderr.closed)

    def test_start_stop_transitions_and_repeated_stop_requests(self):
        session = self.create()
        self.assertEqual(session.state, SessionState.INITIALIZED)
        with self.assertLogs("jarvis.audio.session", "INFO") as logs:
            session.start(capture_frames=10)
            self.assertEqual(session.state, SessionState.RUNNING)
            session.request_stop()
            session.request_stop()
            session.close()
            session.close()
        self.assertEqual(sum("-> stopping" in log for log in logs.output), 1)
        self.assertEqual(sum("-> stopped" in log for log in logs.output), 1)
        with self.assertRaises(AudioOperationError):
            session.start()
        self.assert_clean(session)

    def test_limited_capture_and_slow_consumer_keep_bounded_memory(self):
        session = self.create(capacity=3)
        session.start(capture_frames=1000)
        self.assertTrue(session._capture_done.wait(5))
        frames = []
        while (item := session.read_frame()) is not None:
            frames.append(item)
        self.assertEqual([item.sequence for item in frames], [997, 998, 999])
        metrics = session.metrics
        self.assertEqual(metrics.capture.frames, 1000)
        self.assertEqual(metrics.capture_buffer.dropped, 997)
        self.assertEqual(metrics.capture_buffer.high_water, 3)
        self.assertEqual(metrics.capture_buffer.consumed, 3)
        session.close()
        self.assertFalse(session.metrics.failed)
        self.assert_clean(session)

    def test_playback_drains_all_admitted_frames(self):
        session = self.create(capture_selector=None, playback_selector="jabra",
                              capacity=2)
        with session:
            for sequence in range(100):
                session.write_frame(
                    PCMFrame(PCMFormat(), bytes(640), sequence)
                )
            session.finish_playback()
            session.finish_playback()
        metrics = session.metrics
        self.assertEqual(metrics.playback.frames, 100)
        self.assertEqual(metrics.playback_buffer.accepted, 100)
        self.assertEqual(metrics.playback_buffer.dropped, 0)
        self.assertLessEqual(metrics.playback_buffer.high_water, 2)
        self.assert_clean(session)

    def test_missing_output_is_detected_before_capture_opens(self):
        session = self.create(playback_selector="missing")
        with self.assertRaises(DeviceNotFound):
            session.start()
        self.assertIsNone(session.capture)
        self.assertTrue(session.metrics.failed)
        self.assert_clean(session)

    def test_partial_process_startup_failure_reaps_capture(self):
        session = self.create(playback_selector="jabra")

        def arguments(device, pcm):
            return ([sys.executable, "-c", "import time; time.sleep(60)"]
                    if device.direction == "capture"
                    else ["/absent/playback-utility"])

        with patch(
            "jarvis.audio.streaming.raw_arguments", side_effect=arguments
        ):
            with self.assertRaises(BackendUnavailable):
                session.start()
        self.assertTrue(session.metrics.failed)
        self.assert_clean(session)

    def test_partial_worker_startup_failure_joins_started_worker(self):
        session = self.create(playback_selector="jabra")
        original = threading.Thread.start
        count = 0

        def start(worker):
            nonlocal count
            count += 1
            if count == 2:
                raise RuntimeError("synthetic thread startup failure")
            original(worker)

        with patch("jarvis.audio.session.Thread.start", side_effect=start,
                   autospec=True):
            with self.assertRaises(RuntimeError):
                session.start()
        self.assert_clean(session)

    def test_backend_failure_is_reported_and_other_worker_stops(self):
        session = self.create(
            capture_code="import sys; print('disconnected',file=sys.stderr); "
                         "sys.exit(3)",
            playback_selector="jabra",
        )
        session.start()
        with self.assertRaisesRegex(AudioOperationError, "disconnected"):
            session.read_frame(timeout=3)
        session.close()
        self.assertTrue(session.metrics.failed)
        self.assert_clean(session)

    def test_programmatic_stop_cancels_blocked_read_and_joins(self):
        session = self.create(capture_code="import time; time.sleep(60)")
        session.start()
        session.request_stop()
        with self.assertRaises(StreamCancelled):
            session.read_frame()
        began = time.monotonic()
        session.close()
        self.assertLess(time.monotonic() - began, 3)
        self.assertFalse(session.metrics.failed)
        self.assert_clean(session)

    def test_capture_timeout_fails_and_cleans_up(self):
        session = self.create(capture_code="import time; time.sleep(60)",
                              io_timeout=.1)
        session.start()
        self.assertTrue(session._capture_done.wait(3))
        with self.assertRaises(AudioTimeout):
            session.read_frame()
        session.close()
        self.assertTrue(session.metrics.failed)
        self.assert_clean(session)

    def test_existing_signal_request_cancels_without_lifecycle_failure(self):
        request = ShutdownRequest()
        session = self.create(capture_code="import time; time.sleep(60)",
                              signals=request)
        session.start()
        request.record(signal.SIGINT, None)
        with self.assertRaises(AudioCancelled):
            session.read_frame()
        session.close()
        self.assertFalse(session.metrics.failed)
        self.assert_clean(session)

    def test_context_reports_idle_playback_exit(self):
        session = self.create(capture_selector=None, playback_selector="jabra",
                              playback_code="import sys; sys.exit(4)")
        with self.assertRaises(AudioOperationError):
            with session:
                self.assertTrue(session._playback_done.wait(3))
        self.assert_clean(session)

    def test_operations_require_started_session(self):
        session = self.create(playback_selector="jabra")
        for operation in (
            session.read_frame,
            lambda: session.write_frame(PCMFrame(PCMFormat(), bytes(640))),
            session.finish_playback,
        ):
            with self.assertRaises(AudioOperationError):
                operation()
        session.close()
        self.assert_clean(session)

    def test_stop_unblocks_a_backpressured_playback_producer(self):
        session = self.create(capture_selector=None, playback_selector="jabra",
                              playback_code="import time; time.sleep(60)",
                              capacity=1, io_timeout=5)
        session.start()
        errors = []

        def produce():
            try:
                for sequence in range(1000):
                    session.write_frame(PCMFrame(PCMFormat(), bytes(640),
                                                 sequence), timeout=5)
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=produce)
        worker.start()
        try:
            deadline = time.monotonic() + 3
            while session.metrics.playback_buffer.overflows == 0:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.01)
            session.request_stop()
            session.close()
            worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertIsInstance(errors[0], StreamCancelled)
            self.assert_clean(session)
        finally:
            session.request_stop()
            worker.join(3)

    def test_programmatic_stop_interrupts_startup_discovery(self):
        session = self.create()
        entered = threading.Event()
        errors = []

        def discover(direction, signals, cancel=None):
            entered.set()
            run_command(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                5, signals, cancel,
            )
            return (CAPTURE,)

        session.backend.devices.side_effect = discover

        def start():
            try:
                session.start()
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=start)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            session.request_stop()
            worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertIsInstance(errors[0], StreamCancelled)
            self.assertFalse(session.metrics.failed)
            self.assert_clean(session)
        finally:
            session.request_stop()
            worker.join(6)
