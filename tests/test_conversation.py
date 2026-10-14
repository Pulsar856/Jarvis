"""Offline multi-turn simulations through the existing audio-session API."""

import asyncio
import sys
import threading
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from jarvis.audio.control import StreamControl
from jarvis.audio.errors import AudioOperationError, AudioTimeout, StreamCancelled
from jarvis.audio.frames import PCMFrame
from jarvis.audio.backend import AlsaBackend
from jarvis.audio.devices import AudioDevice
from jarvis.audio.session import SessionState, StreamSession
from jarvis.conversation import ResponseBuffer, VoiceConversation
from jarvis.providers.gemini.client import EventKind, ProviderEvent
from jarvis.providers.gemini.config import GeminiConfig
from jarvis.providers.gemini.errors import (
    ProviderAuthenticationError, ProviderBackpressureError, ProviderNetworkError,
)

MIME = "audio/pcm;rate=24000"


class FakeAudio:
    def __init__(self, factory, pcm, capture_selector=None,
                 playback_selector=None, **settings):
        self.factory, self.format = factory, pcm
        self.is_capture = capture_selector is not None
        self.started = False
        self.closed = False
        self.stopped = threading.Event()
        self.sequence = 0
        self.frames = []
        self.drained = False
        self.metrics = NS(
            failed=False,
            capture_buffer=NS(dropped=0) if self.is_capture else None,
            playback_buffer=None if self.is_capture else NS(discarded=0),
        )

    def start(self):
        if self.stopped.is_set():
            raise StreamCancelled("fake stopped startup")
        self.started = True
        if self.is_capture:
            self.factory.current_capture = self
        elif not self.factory.full_duplex:
            assert not any(audio.started and not audio.closed and audio.is_capture
                           for audio in self.factory.instances)
        if self.factory.fail_start == ("capture" if self.is_capture else "playback"):
            raise AudioOperationError("synthetic device startup failure")

    def read_frame(self, timeout=2):
        if self.stopped.wait(.005):
            raise StreamCancelled("fake stopped capture")
        result = PCMFrame(self.format, bytes(self.format.frame_bytes),
                          self.sequence, time.monotonic())
        self.sequence += 1
        return result

    def write_frame(self, frame, timeout=2):
        if self.stopped.is_set():
            raise StreamCancelled("fake stopped playback")
        if self.factory.backpressure:
            self.factory.backpressure -= 1
            raise AudioTimeout("fake full queue")
        if self.factory.interrupt_old and frame.payload.startswith(b"a"):
            self.factory.old_write.set()
            if self.stopped.wait(2):
                raise StreamCancelled("fake interrupted queued output")
            raise AudioTimeout("fake old output did not stop")
        assert frame.format == self.format
        self.frames.append(frame)

    def finish_playback(self, timeout=10):
        if self.stopped.is_set():
            raise StreamCancelled("fake stopped drain")
        self.drained = True

    def request_stop(self):
        self.stopped.set()

    def close(self):
        self.request_stop()
        self.closed = True


class AudioFactory:
    def __init__(self, full_duplex=False):
        self.instances = []
        self.current_capture = None
        self.full_duplex = full_duplex
        self.fail_start = None
        self.backpressure = 0
        self.interrupt_old = False
        self.old_write = threading.Event()

    def __call__(self, *args, **kwargs):
        audio = FakeAudio(self, *args, **kwargs)
        self.instances.append(audio)
        return audio


class FakeProvider:
    def __init__(self, factory, respond=True):
        self.factory = factory
        self.respond = respond
        self.closed = False
        self.connected = False
        self.inputs = []
        self.prompt = None
        self.ends = 0
        self.queue = asyncio.Queue()
        self.last_capture = None
        self.error = None

    async def connect(self):
        if self.error:
            raise self.error
        self.connected = True

    def response(self):
        self.queue.put_nowait(ProviderEvent(EventKind.AUDIO, b"b" * 1920, MIME))
        self.queue.put_nowait(ProviderEvent(EventKind.TURN_COMPLETE))

    async def send_text(self, text):
        self.prompt = text
        if self.respond:
            self.response()

    async def send_audio(self, frame):
        self.inputs.append(frame)
        capture = self.factory.current_capture
        if self.respond and capture is not self.last_capture:
            self.last_capture = capture
            self.response()

    async def end_audio(self):
        self.ends += 1

    async def events(self):
        while True:
            event = await self.queue.get()
            if isinstance(event, Exception):
                raise event
            yield event

    async def close(self):
        self.closed = True


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    def create(self, *, respond=True, full_duplex=False, **settings):
        factory = AudioFactory(full_duplex)
        provider = FakeProvider(factory, respond)
        config = GeminiConfig(api_key="synthetic-key", max_seconds=1, **settings)
        conversation = VoiceConversation(provider, config, full_duplex=full_duplex,
                                         audio_factory=factory)
        return conversation, provider, factory

    def assert_clean(self, conversation, provider, factory):
        self.assertTrue(provider.closed)
        self.assertTrue(all(audio.closed for audio in factory.instances))
        self.assertEqual(conversation._audio_resources, set())
        self.assertFalse(conversation._io_tasks)
        self.assertEqual(conversation.buffer.bytes, 0)
        self.assertEqual(len(conversation.buffer._items), 0)

    async def test_generated_audio_uses_24khz_and_waits_for_native_drain(self):
        conversation, provider, factory = self.create()
        metrics = await asyncio.wait_for(conversation.run("Say exactly: ready."), 3)
        self.assertEqual(metrics.drained_turns, 1)
        self.assertEqual(metrics.received_audio_bytes, 1920)
        self.assertEqual(metrics.playback_submitted_frames, 2)
        self.assertEqual(len(factory.instances), 1)
        audio = factory.instances[0]
        self.assertFalse(audio.is_capture)
        self.assertEqual(audio.format.sample_rate, 24000)
        self.assertTrue(audio.drained)
        self.assert_clean(conversation, provider, factory)

    async def test_multiple_sequential_turns_resequence_capture_and_close_mic(self):
        conversation, provider, factory = self.create(max_turns=2)
        metrics = await asyncio.wait_for(conversation.run(), 3)
        self.assertEqual(metrics.drained_turns, 2)
        self.assertEqual(provider.ends, 2)
        self.assertEqual([frame.sequence for frame in provider.inputs],
                         list(range(len(provider.inputs))))
        self.assertTrue(all(frame.format.sample_rate == 16000
                            for frame in provider.inputs))
        self.assertEqual(sum(audio.is_capture for audio in factory.instances), 2)
        self.assert_clean(conversation, provider, factory)

    async def test_output_backpressure_retries_same_frame_without_loss(self):
        conversation, provider, factory = self.create()
        factory.backpressure = 1
        metrics = await conversation.run("Say ready.")
        self.assertEqual(metrics.playback_backpressure_events, 1)
        self.assertEqual(len(factory.instances[0].frames), 2)
        self.assertEqual(metrics.drained_turns, 1)
        self.assert_clean(conversation, provider, factory)

    async def test_connection_failure_never_opens_microphone(self):
        conversation, provider, factory = self.create()
        provider.error = ProviderAuthenticationError("synthetic rejected key")
        with self.assertRaises(ProviderAuthenticationError):
            await conversation.run()
        self.assertEqual(factory.instances, [])
        self.assertTrue(provider.closed)

    async def test_partial_audio_startup_failure_closes_all_resources(self):
        for direction in ("capture", "playback"):
            with self.subTest(direction=direction):
                conversation, provider, factory = self.create()
                factory.fail_start = direction
                with self.assertRaises(AudioOperationError):
                    await conversation.run(None if direction == "capture" else "ready")
                self.assert_clean(conversation, provider, factory)

    async def test_duration_limit_stops_microphone_and_connection(self):
        conversation, provider, factory = self.create(respond=False)
        metrics = await asyncio.wait_for(conversation.run(), 3)
        self.assertEqual(metrics.stop_reason, "duration_limit")
        self.assertLessEqual(metrics.input_frames, 50)
        self.assert_clean(conversation, provider, factory)

    async def test_programmatic_stop_and_repeated_requests_are_safe(self):
        conversation, provider, factory = self.create(respond=False)
        task = asyncio.create_task(conversation.run())
        while not factory.instances:
            await asyncio.sleep(.005)
        conversation.control.request_stop()
        conversation.control.request_stop()
        with self.assertRaises(StreamCancelled):
            await asyncio.wait_for(task, 3)
        self.assert_clean(conversation, provider, factory)

    async def test_interruption_invalidates_queued_and_inflight_old_audio(self):
        conversation, provider, factory = self.create(respond=False)
        factory.interrupt_old = True

        async def events():
            yield ProviderEvent(EventKind.AUDIO, b"a" * 9600, MIME)
            self.assertTrue(await asyncio.to_thread(factory.old_write.wait, 1))
            yield ProviderEvent(EventKind.INTERRUPTED)
            yield ProviderEvent(EventKind.AUDIO, b"b" * 1920, MIME)
            yield ProviderEvent(EventKind.TURN_COMPLETE)
            await asyncio.sleep(60)

        provider.events = events
        metrics = await asyncio.wait_for(conversation.run("ready"), 3)
        self.assertEqual(metrics.interruptions, 1)
        self.assertEqual(metrics.drained_turns, 1)
        self.assertGreaterEqual(metrics.discarded_response_bytes, 9600)
        frames = [frame for audio in factory.instances for frame in audio.frames]
        self.assertEqual(len(frames), 2)
        self.assertTrue(all(frame.payload == b"b" * 960 for frame in frames))
        self.assert_clean(conversation, provider, factory)

    async def test_network_disconnect_and_server_goaway_stop_audio(self):
        for event in (ProviderNetworkError("synthetic closure"),
                      ProviderEvent(EventKind.GO_AWAY)):
            with self.subTest(event=event):
                conversation, provider, factory = self.create(respond=False)
                provider.queue.put_nowait(event)
                with self.assertRaises(ProviderNetworkError):
                    await conversation.run()
                self.assert_clean(conversation, provider, factory)

    async def test_slow_network_uses_existing_capture_drop_policy(self):
        backend = Mock(spec=AlsaBackend)
        backend.devices.return_value = (
            AudioDevice("capture", 7, "Jabra", "Jabra SPEAK 510", 0, "USB"),
        )
        instances = []

        def audio_factory(*args, **kwargs):
            audio = StreamSession(*args, backend=backend, **kwargs)
            instances.append(audio)
            return audio

        provider = FakeProvider(None, respond=False)

        async def slow_send(frame):
            await asyncio.sleep(.12)
            provider.inputs.append(frame)

        provider.send_audio = slow_send
        config = GeminiConfig(api_key="synthetic-key", max_seconds=1)
        conversation = VoiceConversation(
            provider, config, audio_factory=audio_factory
        )
        code = (
            "import os,time\n"
            "while True:\n"
            " os.write(1,bytes(640)); time.sleep(.002)\n"
        )
        with patch("jarvis.audio.streaming.raw_arguments",
                   return_value=[sys.executable, "-c", code]):
            metrics = await asyncio.wait_for(conversation.run(), 4)
        self.assertGreater(metrics.capture_dropped_frames, 0)
        self.assertLessEqual(metrics.input_frames, 8)
        self.assertGreater(metrics.network_send_wait_seconds, .1)
        self.assertEqual(len(instances), 1)
        audio = instances[0]
        self.assertEqual(audio.state, SessionState.STOPPED)
        self.assertLessEqual(audio.metrics.capture_buffer.high_water, 10)
        self.assertFalse(any(worker.is_alive() for worker in audio._threads))
        self.assertIsNotNone(audio.capture.process.poll())
        self.assertTrue(provider.closed)

    async def test_interruption_during_playback_startup_releases_partial_audio(self):
        conversation, provider, factory = self.create(respond=False)
        startup = threading.Event()
        original = FakeAudio.start
        calls = 0

        def slow_first_start(audio):
            nonlocal calls
            calls += 1
            if calls == 1:
                startup.set()
                if audio.stopped.wait(2):
                    raise StreamCancelled("interrupted startup")
            original(audio)

        async def events():
            yield ProviderEvent(EventKind.AUDIO, b"a" * 960, MIME)
            self.assertTrue(await asyncio.to_thread(startup.wait, 1))
            yield ProviderEvent(EventKind.INTERRUPTED)
            yield ProviderEvent(EventKind.AUDIO, b"b" * 960, MIME)
            yield ProviderEvent(EventKind.TURN_COMPLETE)
            await asyncio.sleep(60)

        provider.events = events
        with patch.object(FakeAudio, "start", slow_first_start):
            metrics = await asyncio.wait_for(conversation.run("ready"), 3)
        self.assertEqual(metrics.drained_turns, 1)
        self.assertEqual(metrics.interruptions, 1)
        self.assertEqual(len(factory.instances), 2)
        self.assertEqual(factory.instances[0].frames, [])
        self.assert_clean(conversation, provider, factory)

    async def test_experimental_duplex_keeps_capture_open_across_two_turns(self):
        conversation, provider, factory = self.create(
            full_duplex=True, max_turns=2
        )
        provider.queue.put_nowait(ProviderEvent(EventKind.AUDIO, bytes(960), MIME))
        provider.queue.put_nowait(ProviderEvent(EventKind.TURN_COMPLETE))
        metrics = await asyncio.wait_for(conversation.run(), 3)
        self.assertEqual(metrics.drained_turns, 2)
        self.assertEqual(provider.ends, 0)
        self.assertEqual(sum(audio.is_capture for audio in factory.instances), 1)
        self.assert_clean(conversation, provider, factory)

    async def test_cleanup_failure_still_closes_provider_and_other_resources(self):
        conversation, provider, factory = self.create(
            full_duplex=True, respond=False
        )
        original = FakeAudio.close
        closed = []

        def failing_close(audio):
            original(audio)
            closed.append(audio)
            if audio.is_capture:
                raise AudioOperationError("synthetic cleanup failure")

        async def stop_after_playback_started():
            while len(factory.instances) < 2:
                await asyncio.sleep(.005)
            conversation.control.request_stop()

        provider.queue.put_nowait(ProviderEvent(EventKind.AUDIO, bytes(960), MIME))
        stop = asyncio.create_task(stop_after_playback_started())
        with patch.object(FakeAudio, "close", failing_close):
            with self.assertRaises(StreamCancelled):
                await asyncio.wait_for(conversation.run(), 3)
        await stop
        self.assertTrue(provider.closed)
        self.assertEqual(len(set(closed)), 2)
        self.assertTrue(all(audio.closed for audio in factory.instances))
        self.assertGreater(conversation.metrics.cleanup_failures, 0)


class ResponseBufferTests(unittest.IsolatedAsyncioTestCase):
    async def test_byte_and_packet_capacity_apply_without_silent_loss(self):
        buffer = ResponseBuffer(1920, max_items=2)
        event = ProviderEvent(EventKind.AUDIO, bytes(960), MIME)
        buffer.put(event)
        buffer.put(event)
        with self.assertRaises(ProviderBackpressureError):
            buffer.put(event)
        self.assertEqual(buffer.bytes, 1920)
        self.assertEqual(buffer.high_water_bytes, 1920)
        self.assertEqual((await buffer.get())[1], event)
        self.assertEqual(buffer.bytes, 960)

    async def test_interruption_clears_queue_and_changes_epoch(self):
        buffer = ResponseBuffer(1920)
        buffer.put(ProviderEvent(EventKind.AUDIO, bytes(960), MIME))
        buffer.invalidate()
        epoch, event = await buffer.get()
        self.assertEqual(epoch, 1)
        self.assertEqual(event.kind, EventKind.INTERRUPTED)
        self.assertEqual(buffer.discarded_bytes, 960)
        self.assertEqual(buffer.bytes, 0)
