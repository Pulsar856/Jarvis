"""Bounded voice coordination using an explicit provider and existing audio."""

import asyncio
import sys
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from jarvis.audio.control import StreamControl
from jarvis.audio.errors import AudioTimeout, StreamCancelled
from jarvis.audio.frames import PCMFrame
from jarvis.audio.session import StreamSession
from jarvis.providers.gemini.client import EventKind, GeminiClient, ProviderEvent
from jarvis.providers.gemini.config import GeminiConfig
from jarvis.providers.gemini.errors import (
    ProviderBackpressureError, ProviderNetworkError, ProviderProtocolError,
    ProviderTimeout,
)
from jarvis.providers.gemini.pcm import INPUT_PCM, OUTPUT_PCM, OutputFramer
from jarvis.providers.gemini.runtime import guarded


class ResponseBuffer:
    """Event-loop FIFO bounded by audio bytes and packet count, with epochs."""

    def __init__(self, max_bytes: int, max_items: int = 1024) -> None:
        if max_bytes <= 0 or max_items <= 0:
            raise ProviderProtocolError("Response buffer limits must be positive.")
        self.max_bytes = max_bytes
        self.max_items = max_items
        self.generation = 0
        self.bytes = 0
        self.high_water_bytes = 0
        self.discarded_bytes = 0
        self._items: deque[tuple[int, ProviderEvent]] = deque()
        self._ready = asyncio.Event()

    def put(self, event: ProviderEvent) -> None:
        size = len(event.data)
        if self.bytes + size > self.max_bytes or len(self._items) >= self.max_items:
            raise ProviderBackpressureError(
                "Gemini output exceeded the bounded playback queue. "
                "The session was stopped without silently dropping speech."
            )
        self._items.append((self.generation, event))
        self.bytes += size
        self.high_water_bytes = max(self.high_water_bytes, self.bytes)
        self._ready.set()

    async def get(self) -> tuple[int, ProviderEvent]:
        while not self._items:
            self._ready.clear()
            await self._ready.wait()
        generation, event = self._items.popleft()
        self.bytes -= len(event.data)
        return generation, event

    def discard_pending(self) -> None:
        """Release queued payloads when interrupted or when the owner exits."""
        self.discarded_bytes += self.bytes
        self.bytes = 0
        self._items.clear()
        self._ready.clear()

    def invalidate(self) -> None:
        self.discard_pending()
        self.generation += 1
        self.put(ProviderEvent(EventKind.INTERRUPTED))

    @property
    def has_audio(self) -> bool:
        return any(event.kind is EventKind.AUDIO for _, event in self._items)


@dataclass
class ConversationMetrics:
    connected: bool = False
    input_frames: int = 0
    input_bytes: int = 0
    capture_dropped_frames: int = 0
    capture_buffer_underflows: int = 0
    capture_buffer_wait_seconds: float = 0
    received_audio_bytes: int = 0
    playback_submitted_frames: int = 0
    playback_discarded_frames: int = 0
    playback_backpressure_events: int = 0
    playback_buffer_underflows: int = 0
    playback_buffer_wait_seconds: float = 0
    playback_pipe_wait_seconds: float = 0
    playback_queue_high_water_frames: int = 0
    playback_pipe_capacity_bytes: int = 0
    first_playback_pipe_seconds: float | None = None
    received_turns: int = 0
    drained_turns: int = 0
    interruptions: int = 0
    discarded_response_bytes: int = 0
    padding_bytes: int = 0
    queue_high_water_bytes: int = 0
    first_provider_audio_seconds: float | None = None
    network_send_wait_seconds: float = 0
    active_seconds: float = 0
    cleanup_seconds: float = 0
    cleanup_failures: int = 0
    stop_reason: str = "not_started"


class VoiceConversation:
    """Concrete MVP orchestration; provider types never enter the PCM engine.

    Async network tasks use a small executor bridge to the existing synchronous
    audio API. Every bridge call is retained and awaited during owned cleanup.
    Sequential mode stops capture until native playback has actually drained.
    """

    def __init__(
        self, provider: GeminiClient, config: GeminiConfig,
        control: StreamControl | None = None,
        input_selector: str = "jabra", output_selector: str = "jabra",
        full_duplex: bool = False,
        audio_factory: Callable[..., StreamSession] = StreamSession,
    ) -> None:
        self.provider = provider
        self.config = config
        self.control = StreamControl() if control is None else control
        self.input_selector = input_selector
        self.output_selector = output_selector
        self.full_duplex = full_duplex
        self.audio_factory = audio_factory
        self.metrics = ConversationMetrics()
        self.buffer = ResponseBuffer(config.response_seconds * 24000 * 2)
        self.capture: StreamSession | None = None
        self.playback: StreamSession | None = None
        self._audio_resources: set[StreamSession] = set()
        self._audio_locks: dict[StreamSession, asyncio.Lock] = {}
        self._audio_acquisitions = 0
        self._io_tasks: set[asyncio.Task] = set()
        self._listening = asyncio.Event()
        self._talk = False
        self._input_sequence = 0
        self._response_bytes = 0
        self._started = 0.0

    async def _io(self, function, *args, **kwargs):
        async def operation():
            owner = getattr(function, "__self__", None)
            lock = self._audio_locks.get(owner)
            if lock is None:
                return await asyncio.to_thread(function, *args, **kwargs)
            async with lock:
                return await asyncio.to_thread(function, *args, **kwargs)

        task = asyncio.create_task(operation())
        self._io_tasks.add(task)

        def finished(completed):
            self._io_tasks.discard(completed)
            if not completed.cancelled():
                completed.exception()  # Consume errors even after cancellation.

        task.add_done_callback(finished)
        return await asyncio.shield(task)

    def _own_audio(self, audio: StreamSession) -> None:
        self._audio_acquisitions += 1
        if self._audio_acquisitions > 32:
            raise ProviderProtocolError("Audio restart limit reached; session stopped.")
        self._audio_resources.add(audio)
        self._audio_locks[audio] = asyncio.Lock()

    async def _close_audio(self, audio: StreamSession) -> None:
        audio.request_stop()
        await self._io(audio.close)
        if audio in self._audio_resources:
            snapshot = audio.metrics
            if snapshot.capture_buffer is not None:
                self.metrics.capture_dropped_frames += snapshot.capture_buffer.dropped
                self.metrics.capture_buffer_underflows += getattr(
                    snapshot.capture_buffer, "underflows", 0
                )
                self.metrics.capture_buffer_wait_seconds += getattr(
                    snapshot.capture_buffer, "wait_seconds", 0
                )
            if snapshot.playback_buffer is not None:
                self.metrics.playback_discarded_frames += (
                    snapshot.playback_buffer.discarded
                )
                self.metrics.playback_buffer_underflows += getattr(
                    snapshot.playback_buffer, "underflows", 0
                )
                self.metrics.playback_buffer_wait_seconds += getattr(
                    snapshot.playback_buffer, "wait_seconds", 0
                )
                self.metrics.playback_queue_high_water_frames = max(
                    self.metrics.playback_queue_high_water_frames,
                    getattr(snapshot.playback_buffer, "high_water", 0),
                )
            transport = getattr(snapshot, "playback", None)
            if transport is not None:
                self.metrics.playback_pipe_wait_seconds += transport.io_wait_seconds
                self.metrics.playback_pipe_capacity_bytes = max(
                    self.metrics.playback_pipe_capacity_bytes,
                    transport.pipe_capacity_bytes,
                )
                if self.metrics.first_playback_pipe_seconds is None:
                    self.metrics.first_playback_pipe_seconds = (
                        transport.first_frame_seconds
                    )
            self._audio_resources.remove(audio)

    async def _open_capture(self) -> None:
        self.control.check()
        audio = self.audio_factory(
            INPUT_PCM, capture_selector=self.input_selector,
            capacity=10, io_timeout=min(2, self.config.io_timeout),
            signals=self.control.signals,
        )
        self._own_audio(audio)
        self.capture = audio
        try:
            await self._io(audio.start)
        except StreamCancelled:
            if audio is self.capture:
                raise
            return  # Receiver paused this startup before playback.
        if audio is self.capture:
            self._listening.set()

    async def _pause_capture(self) -> None:
        self._listening.clear()
        audio, self.capture = self.capture, None
        if audio is not None:
            await self._close_audio(audio)
            await self.provider.end_audio()

    async def _resume_capture(self) -> None:
        if self._talk and not self.full_duplex and self.capture is None:
            await self._open_capture()

    async def _sender(self) -> None:
        next_send = asyncio.get_running_loop().time()
        while True:
            await self._listening.wait()
            self.control.check()
            audio = self.capture
            if audio is None:
                continue
            try:
                frame = await self._io(audio.read_frame, timeout=.1)
            except AudioTimeout:
                if audio.metrics.failed:
                    raise
                continue
            except StreamCancelled:
                if audio is not self.capture:
                    continue  # Sequential handoff, not a failed microphone.
                raise
            if frame is None:
                raise ProviderProtocolError("Microphone stream ended unexpectedly.")
            if audio is not self.capture or not self._listening.is_set():
                continue
            loop = asyncio.get_running_loop()
            await asyncio.sleep(max(0, next_send - loop.time()))
            self.control.check()
            if audio is not self.capture:
                continue
            frame = PCMFrame(INPUT_PCM, frame.payload, self._input_sequence,
                             frame.captured_at)
            began = loop.time()
            await self.provider.send_audio(frame)
            self.metrics.network_send_wait_seconds += loop.time() - began
            self._input_sequence += 1
            self.metrics.input_frames += 1
            self.metrics.input_bytes += len(frame.payload)
            next_send = loop.time() + INPUT_PCM.duration

    async def _receiver(self) -> None:
        async for event in self.provider.events():
            self.control.check()
            if event.kind is EventKind.GO_AWAY:
                raise ProviderNetworkError(
                    "Gemini requested session termination; no resumption attempted."
                )
            if event.kind is EventKind.INTERRUPTED:
                self.metrics.interruptions += 1
                self._response_bytes = 0
                self.buffer.invalidate()
                if self.playback is not None:
                    self.playback.request_stop()
                continue
            if event.kind is EventKind.AUDIO:
                if self.metrics.first_provider_audio_seconds is None:
                    self.metrics.first_provider_audio_seconds = (
                        asyncio.get_running_loop().time() - self._started
                    )
                self._response_bytes += len(event.data)
                if self._response_bytes > self.buffer.max_bytes:
                    raise ProviderBackpressureError(
                        "Gemini response exceeded the configured audio-duration limit."
                    )
                self.metrics.received_audio_bytes += len(event.data)
                if self._talk and not self.full_duplex and self.capture is not None:
                    await self._pause_capture()
                self.buffer.put(event)
            elif event.kind is EventKind.TURN_COMPLETE:
                self.metrics.received_turns += 1
                self._response_bytes = 0
                self.buffer.put(event)
            # Generation complete does not imply that the device has drained.
        raise ProviderNetworkError("Gemini stream closed unexpectedly.")

    async def _open_playback(self) -> StreamSession:
        self.control.check()
        audio = self.audio_factory(
            OUTPUT_PCM, capture_selector=None,
            playback_selector=self.output_selector,
            capacity=10, io_timeout=min(2, self.config.io_timeout),
            signals=self.control.signals,
        )
        self.playback = audio
        self._own_audio(audio)
        await self._io(audio.start)
        return audio

    async def _write(self, audio: StreamSession, frame: PCMFrame, epoch: int):
        began = asyncio.get_running_loop().time()
        while epoch == self.buffer.generation:
            self.control.check()
            try:
                await self._io(audio.write_frame, frame, timeout=.1)
                self.metrics.playback_submitted_frames += 1
                return True
            except AudioTimeout:
                self.metrics.playback_backpressure_events += 1
                if audio.metrics.failed:
                    raise
                if asyncio.get_running_loop().time() - began >= self.config.io_timeout:
                    raise ProviderBackpressureError(
                        "Playback admission exceeded its bounded backpressure wait."
                    ) from None
            except StreamCancelled:
                if epoch != self.buffer.generation:
                    return False
                raise
        return False

    async def _speaker(self) -> str:
        framer = OutputFramer()
        current_epoch = self.buffer.generation
        audio = None
        try:
            while True:
                epoch, event = await self.buffer.get()
                if epoch != current_epoch:
                    if audio is not None:
                        await self._close_audio(audio)
                        audio = self.playback = None
                    self.metrics.discarded_response_bytes += framer.discard()
                    current_epoch = epoch
                if event.kind is EventKind.INTERRUPTED:
                    if audio is not None:
                        await self._close_audio(audio)
                        audio = self.playback = None
                    self.metrics.discarded_response_bytes += framer.discard()
                    if not self.buffer.has_audio:
                        await self._resume_capture()
                    continue
                if event.kind is EventKind.AUDIO:
                    if audio is None:
                        try:
                            audio = await self._open_playback()
                        except StreamCancelled:
                            if epoch == self.buffer.generation:
                                raise
                            await self._close_audio(self.playback)
                            self.playback = None
                            self.metrics.discarded_response_bytes += len(event.data)
                            continue
                    for frame in framer.feed(event.data, event.mime_type):
                        if not await self._write(audio, frame, epoch):
                            self.metrics.discarded_response_bytes += (
                                len(frame.payload) + len(event.data)
                                - framer.chunk_consumed_bytes
                            )
                            break
                elif event.kind is EventKind.TURN_COMPLETE:
                    if audio is None:
                        if not self._talk:
                            raise ProviderProtocolError(
                                "Gemini completed the test without audio."
                            )
                        await self._resume_capture()
                        continue
                    final = framer.finish()
                    if final is not None:
                        if not await self._write(audio, final, epoch):
                            continue
                    self.metrics.padding_bytes = framer.padding_bytes
                    try:
                        await self._io(audio.finish_playback, timeout=10)
                    except StreamCancelled:
                        if epoch != self.buffer.generation:
                            continue
                        raise
                    await self._close_audio(audio)
                    audio = self.playback = None
                    self.metrics.drained_turns += 1
                    limit = self.config.max_turns if self._talk else 1
                    if self.metrics.drained_turns >= limit:
                        return "turn_limit"
                    await self._resume_capture()
        finally:
            if audio is not None:
                await self._close_audio(audio)

    async def _duration(self) -> str:
        await asyncio.sleep(max(
            0, self.config.max_seconds
            - (asyncio.get_running_loop().time() - self._started)
        ))
        if not self._talk:
            raise ProviderTimeout("Gemini speak-test did not finish before its limit.")
        return "duration_limit"

    async def run(self, prompt: str | None = None) -> ConversationMetrics:
        return await guarded(
            self._run(prompt), self.control,
            self.config.connect_timeout + self.config.max_seconds + 1,
        )

    async def _run(self, prompt: str | None = None) -> ConversationMetrics:
        """Connect before capture; all tasks and resources belong to this call."""
        self._talk = prompt is None
        tasks = set()
        try:
            self.control.check()
            await self.provider.connect()
            self.metrics.connected = True
            self._started = asyncio.get_running_loop().time()
            if self._talk:
                await self._open_capture()
            else:
                await self.provider.send_text(prompt)
            tasks.add(asyncio.create_task(self._receiver()))
            tasks.add(asyncio.create_task(self._speaker()))
            tasks.add(asyncio.create_task(self._duration()))
            if self._talk:
                tasks.add(asyncio.create_task(self._sender()))
            done, _ = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                if task.exception() is not None:
                    raise task.exception()
            self.metrics.stop_reason = next(iter(done)).result()
            return self.metrics
        except asyncio.CancelledError:
            self.metrics.stop_reason = "cancelled"
            raise
        except Exception:
            self.metrics.stop_reason = "failed"
            raise
        finally:
            primary_failure = sys.exc_info()[0] is not None
            cleanup_errors = []
            began = time.monotonic()
            self._listening.set()
            for audio in tuple(self._audio_resources):
                audio.request_stop()
            cancelled_tasks = {task for task in tasks if not task.done()}
            for task in tasks:
                task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            cleanup_errors.extend(
                result for task, result in zip(tasks, results)
                if task in cancelled_tasks and isinstance(result, Exception)
            )
            try:
                for audio in tuple(self._audio_resources):
                    try:
                        await self._close_audio(audio)
                    except TimeoutError:
                        cleanup_errors.append(ProviderTimeout(
                            "Audio bridge cleanup exceeded its bounded wait."
                        ))
                    except Exception as exc:
                        cleanup_errors.append(exc)
                if self._io_tasks:
                    try:
                        await asyncio.wait_for(asyncio.gather(
                            *tuple(self._io_tasks), return_exceptions=True
                        ), 10)
                    except TimeoutError:
                        cleanup_errors.append(ProviderTimeout(
                            "Audio bridge cleanup exceeded its bounded wait."
                        ))
                    except Exception as exc:
                        cleanup_errors.append(exc)
            finally:
                try:
                    await self.provider.close()
                except Exception as exc:
                    cleanup_errors.append(exc)
                finally:
                    self.buffer.discard_pending()
                    self.metrics.discarded_response_bytes += self.buffer.discarded_bytes
                    self.metrics.queue_high_water_bytes = self.buffer.high_water_bytes
                    self.metrics.active_seconds = (
                        asyncio.get_running_loop().time() - self._started
                        if self._started else 0
                    )
                    self.metrics.cleanup_seconds = time.monotonic() - began
                    self.metrics.cleanup_failures = len(cleanup_errors)
                    if not self._io_tasks:
                        self._audio_locks.clear()
                    self.capture = self.playback = None
            if cleanup_errors and not primary_failure:
                self.metrics.stop_reason = "failed"
                raise cleanup_errors[0]
