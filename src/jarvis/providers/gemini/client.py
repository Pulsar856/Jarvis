"""Lazy official SDK boundary; sanitized events and one explicit connection."""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from jarvis.audio.frames import PCMFormat, PCMFrame
from jarvis.providers.gemini.config import GeminiConfig
from jarvis.providers.gemini.errors import (
    ProviderDependencyError, ProviderProtocolError, classify_error,
)
from jarvis.providers.gemini.pcm import validate_audio_chunk

GOOGLE_BASE_URL = "https://generativelanguage.googleapis.com"


class EventKind(Enum):
    AUDIO = "audio"
    TURN_COMPLETE = "turn_complete"
    GENERATION_COMPLETE = "generation_complete"
    INTERRUPTED = "interrupted"
    GO_AWAY = "go_away"


@dataclass(frozen=True)
class ProviderEvent:
    kind: EventKind
    data: bytes = field(default=b"", repr=False)
    mime_type: str = ""


def parse_message(message: Any) -> tuple[ProviderEvent, ...]:
    """Keep SDK types, transcripts, tokens, tool calls, and text out of audio."""
    if getattr(message, "go_away", None) is not None:
        return (ProviderEvent(EventKind.GO_AWAY),)
    if getattr(message, "tool_call", None) is not None:
        raise ProviderProtocolError("Unexpected Gemini tool request.")
    content = getattr(message, "server_content", None)
    if content is None:
        return ()
    if getattr(content, "interrupted", False):
        # Any audio in the interrupted message belongs to invalidated output.
        return (ProviderEvent(EventKind.INTERRUPTED),)
    result = []
    total_bytes = 0
    turn = getattr(content, "model_turn", None)
    parts = getattr(turn, "parts", None) or ()
    if len(parts) > 64:
        raise ProviderProtocolError("Too many Gemini message parts.")
    for part in parts:
        blob = getattr(part, "inline_data", None)
        if blob is not None:
            data = getattr(blob, "data", None)
            mime = getattr(blob, "mime_type", None)
            if not isinstance(data, bytes) or not isinstance(mime, str):
                raise ProviderProtocolError("Invalid Gemini audio message.")
            validate_audio_chunk(data, mime)
            total_bytes += len(data)
            if total_bytes > 96000:
                raise ProviderProtocolError("Gemini message audio is too large.")
            result.append(ProviderEvent(EventKind.AUDIO, data, mime))
    if getattr(content, "generation_complete", False):
        result.append(ProviderEvent(EventKind.GENERATION_COMPLETE))
    status = getattr(content, "interaction_status", None)
    if status == "IDLE" or (
        status in (None, "INTERACTION_STATUS_UNSPECIFIED")
        and getattr(content, "turn_complete", False)
    ):
        result.append(ProviderEvent(EventKind.TURN_COMPLETE))
    return tuple(result)


@contextmanager
def quiet_sdk_logs():
    """Prevent SDK/network debug records from exposing headers or payloads."""
    prefixes = ("google_genai", "websockets", "httpx", "httpcore", "google.auth")
    names = set(prefixes) | {
        name for name in logging.Logger.manager.loggerDict
        if name.startswith(prefixes)
    }
    names.add("websockets.client")
    saved = []
    barriers = []
    # Logger filters do not apply to propagated child records. A null handler
    # and propagation barrier also catch SDK children created after entry.
    for prefix in prefixes:
        logger = logging.getLogger(prefix)
        handler = logging.NullHandler()
        previous_handlers = tuple(logger.handlers)
        barriers.append((logger, logger.propagate, previous_handlers, handler))
        for previous in previous_handlers:
            logger.removeHandler(previous)
        logger.addHandler(handler)
        logger.propagate = False
    for name in names:
        logger = logging.getLogger(name)
        saved.append((logger, logger.disabled))
        logger.disabled = True
    try:
        yield
    finally:
        for logger, disabled in saved:
            logger.disabled = disabled
        for logger, propagate, previous_handlers, handler in barriers:
            logger.removeHandler(handler)
            for previous in previous_handlers:
                logger.addHandler(previous)
            logger.propagate = propagate
            handler.close()


def create_sdk_client(config: GeminiConfig):
    """Use only Google's Developer API, with a supplied key and no retries."""
    config.require_key()
    try:
        from google import genai
    except ImportError:
        raise ProviderDependencyError(
            "Install the optional Jarvis gemini extra (google-genai==2.29.0)."
        ) from None
    with quiet_sdk_logs():
        return genai.Client(
            api_key=config.api_key, vertexai=False, enterprise=False,
            http_options={
                "base_url": GOOGLE_BASE_URL, "api_version": "v1beta",
                "timeout": int(config.connect_timeout * 1000),
                "retry_options": {"attempts": 1},
                "client_args": {"trust_env": False},
                "async_client_args": {"trust_env": False},
            },
        )


class GeminiClient:
    """One lazy SDK client and Live context; no hardware or automatic retry."""

    def __init__(
        self, config: GeminiConfig,
        factory: Callable[[GeminiConfig], Any] | None = None,
    ) -> None:
        self.config = config
        self._factory = create_sdk_client if factory is None else factory
        self._client = None
        self._context = None
        self._session = None
        self._quiet = None
        self._send_lock = asyncio.Lock()
        self._last_input_sequence = -1
        self._closed = False

    def _create(self) -> None:
        self.config.require_key()
        if self._client is None:
            self._client = self._factory(self.config)
            self._quiet = quiet_sdk_logs()
            self._quiet.__enter__()

    async def connect(self) -> None:
        if self._closed or self._session is not None:
            raise ProviderProtocolError("Gemini client can only connect once.")
        try:
            self._create()
            self._context = self._client.aio.live.connect(
                model=self.config.model,
                config={
                    "response_modalities": ["AUDIO"],
                    "max_output_tokens": 512,
                    "system_instruction": (
                        "You are Jarvis. Reply briefly in the user's language, "
                        "usually one short sentence. Do not call tools."
                    ),
                    "realtime_input_config": {
                        "automatic_activity_detection": {"disabled": False},
                        "activity_handling": "START_OF_ACTIVITY_INTERRUPTS",
                    },
                },
            )
            self._session = await asyncio.wait_for(
                self._context.__aenter__(), self.config.connect_timeout
            )
            if not getattr(self._session, "setup_complete", None):
                raise ProviderProtocolError("Gemini setup was not confirmed.")
        except BaseException as exc:
            try:
                await self.close()
            except Exception:
                pass  # Preserve the primary, sanitized connection failure.
            if isinstance(exc, Exception):
                raise classify_error(exc) from None
            raise

    async def _send(self, method: str, **arguments) -> None:
        if self._session is None or self._closed:
            raise ProviderProtocolError("Gemini session is not connected.")
        try:
            async with self._send_lock:
                await asyncio.wait_for(
                    getattr(self._session, method)(**arguments),
                    self.config.io_timeout,
                )
        except Exception as exc:
            raise classify_error(exc) from None

    async def send_audio(self, frame: PCMFrame) -> None:
        if not isinstance(frame, PCMFrame) or frame.format != PCMFormat():
            raise ProviderProtocolError(
                "Gemini input requires 16 kHz mono S16_LE, 20 ms PCM frames."
            )
        if frame.sequence <= self._last_input_sequence:
            raise ProviderProtocolError("Gemini input sequence must increase.")
        await self._send("send_realtime_input", audio={
            "data": frame.payload, "mime_type": "audio/pcm;rate=16000",
        })
        self._last_input_sequence = frame.sequence

    async def end_audio(self) -> None:
        await self._send("send_realtime_input", audio_stream_end=True)

    async def send_text(self, text: str) -> None:
        if not isinstance(text, str) or not 1 <= len(text) <= 256:
            raise ProviderProtocolError("Gemini test text must be bounded.")
        await self._send("send_client_content", turns={
            "role": "user", "parts": [{"text": text}],
        }, turn_complete=True)

    async def events(self) -> AsyncIterator[ProviderEvent]:
        if self._session is None or self._closed:
            raise ProviderProtocolError("Gemini session is not connected.")
        try:
            # SDK receive() ends per completed interaction, not per connection.
            while not self._closed:
                received = False
                async for message in self._session.receive():
                    received = True
                    for event in parse_message(message):
                        yield event
                if not received:
                    raise ProviderProtocolError(
                        "Gemini stream ended unexpectedly."
                    )
        except Exception as exc:
            raise classify_error(exc) from None

    async def models(self) -> tuple[tuple[str, bool], ...]:
        """One bounded page; listing is not proof of Live/free-tier access."""
        try:
            self._create()
            pager = await asyncio.wait_for(
                self._client.aio.models.list(config={"page_size": 50}),
                self.config.connect_timeout,
            )
            result = []
            for model in pager.page:
                name = getattr(model, "name", "")
                actions = getattr(model, "supported_actions", ()) or ()
                if isinstance(name, str) and name.startswith("models/gemini-"):
                    result.append((name[:160], any(
                        action.casefold() == "bidigeneratecontent"
                        for action in actions if isinstance(action, str)
                    )))
            return tuple(result[:50])
        except Exception as exc:
            raise classify_error(exc) from None
        finally:
            await self.close()

    async def close(self) -> None:
        """Close partial setup and SDK resources once, with bounded waits."""
        if self._closed:
            return
        self._closed = True
        failure = None
        try:
            if self._context is not None:
                try:
                    await asyncio.wait_for(
                        self._context.__aexit__(None, None, None), 3
                    )
                except Exception as exc:
                    failure = classify_error(exc)
            if self._client is not None:
                try:
                    await asyncio.wait_for(self._client.aio.aclose(), 3)
                except Exception as exc:
                    failure = failure or classify_error(exc)
                try:
                    self._client.close()
                except Exception as exc:
                    failure = failure or classify_error(exc)
        finally:
            if self._quiet is not None:
                self._quiet.__exit__(None, None, None)
        if failure is not None:
            raise failure from None
