"""Bounded buffer policies, cancellation, and accounting without hardware."""

import threading
import unittest

from jarvis.audio.buffers import FrameBuffer, OverflowPolicy
from jarvis.audio.control import StreamControl
from jarvis.audio.errors import (
    AudioOperationError, AudioTimeout, AudioValidationError, StreamCancelled,
)
from jarvis.audio.frames import PCMFormat, PCMFrame


def frame(sequence: int) -> PCMFrame:
    return PCMFrame(PCMFormat(), bytes(640), sequence)


class BufferTests(unittest.TestCase):
    def test_capacity_validation(self):
        for capacity in (0, -1, 257, True, 2.0):
            with self.subTest(capacity=capacity):
                with self.assertRaises(AudioValidationError):
                    FrameBuffer(PCMFormat(), capacity)

    def test_sustained_capture_is_bounded_and_keeps_freshest_frames(self):
        buffer = FrameBuffer(PCMFormat(), 5, OverflowPolicy.DROP_OLDEST)
        for sequence in range(10000):
            buffer.put(frame(sequence))
        self.assertEqual(buffer.metrics.occupancy, 5)
        self.assertEqual(buffer.metrics.high_water, 5)
        self.assertEqual(buffer.metrics.dropped, 9995)
        self.assertEqual(buffer.metrics.overflows, 9995)
        self.assertEqual(
            [buffer.get().sequence for _ in range(5)], list(range(9995, 10000))
        )
        metrics = buffer.metrics
        self.assertEqual(
            metrics.accepted,
            metrics.consumed + metrics.dropped + metrics.occupancy,
        )

    def test_full_playback_applies_backpressure_without_silent_loss(self):
        buffer = FrameBuffer(PCMFormat(), 1)
        buffer.put(frame(0))
        with self.assertRaises(AudioTimeout):
            buffer.put(frame(1), timeout=0)
        self.assertEqual(buffer.metrics.accepted, 1)
        self.assertEqual(buffer.metrics.dropped, 0)
        self.assertEqual(buffer.get().sequence, 0)
        buffer.put(frame(1), timeout=0)
        self.assertEqual(buffer.get().sequence, 1)

    def test_waiting_producer_resumes_when_consumer_makes_room(self):
        buffer = FrameBuffer(PCMFormat(), 1)
        buffer.put(frame(0))
        entered = threading.Event()
        errors = []

        def produce():
            entered.set()
            try:
                buffer.put(frame(1), timeout=2)
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=produce)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertEqual(buffer.get().sequence, 0)
            worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(buffer.get().sequence, 1)
        finally:
            buffer.control.request_stop()
            worker.join(3)

    def test_cancellation_unblocks_producer_and_consumer(self):
        for produce in (True, False):
            with self.subTest(produce=produce):
                control = StreamControl()
                buffer = FrameBuffer(PCMFormat(), 1, control=control)
                if produce:
                    buffer.put(frame(0))
                entered = threading.Event()
                errors = []

                def wait():
                    entered.set()
                    try:
                        if produce:
                            buffer.put(frame(1), timeout=5)
                        else:
                            buffer.get(timeout=5)
                    except BaseException as exc:
                        errors.append(exc)

                worker = threading.Thread(target=wait)
                worker.start()
                self.assertTrue(entered.wait(1))
                control.request_stop()
                buffer.close(discard=True)
                worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertIsInstance(errors[0], StreamCancelled)

    def test_close_drains_and_discard_is_accounted_once(self):
        buffer = FrameBuffer(PCMFormat(), 2)
        buffer.put(frame(0))
        buffer.close()
        self.assertEqual(buffer.get().sequence, 0)
        self.assertIsNone(buffer.get())
        with self.assertRaises(AudioOperationError):
            buffer.put(frame(1))
        other = FrameBuffer(PCMFormat(), 2)
        other.put(frame(0))
        other.close(discard=True)
        other.close(discard=True)
        self.assertEqual(other.metrics.discarded, 1)
        self.assertIsNone(other.get())

    def test_empty_buffer_counts_starvation_and_validates_format(self):
        buffer = FrameBuffer(PCMFormat())
        with self.assertRaises(AudioTimeout):
            buffer.get(timeout=0)
        self.assertEqual(buffer.metrics.underflows, 1)
        with self.assertRaises(AudioOperationError):
            buffer.put(PCMFrame(PCMFormat(channels=2), bytes(1280)))
        self.assertEqual(buffer.metrics.accepted, 0)
