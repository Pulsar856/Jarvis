"""Official-SDK contract fakes; tests perform no Gemini API requests."""

import asyncio
import io
import logging
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

from jarvis.audio.frames import PCMFormat, PCMFrame
from jarvis.providers.gemini.client import (
    EventKind, GeminiClient, parse_message, quiet_sdk_logs,
)
from jarvis.providers.gemini.config import GeminiConfig
from jarvis.providers.gemini.errors import (
    ProviderAuthenticationError, ProviderModelError, ProviderNetworkError,
    ProviderPermissionError, ProviderProtocolError, ProviderQuotaError,
    ProviderTimeout, classify_error,
)

KEY = "synthetic-private-key-for-offline-tests"


def message(data=None, **fields):
    content = NS(**fields)
    if data is not None:
        content.model_turn = NS(parts=[NS(inline_data=NS(
            data=data, mime_type="audio/pcm;rate=24000"
        ))])
    return NS(server_content=content)


class FakeContext:
    def __init__(self, session):
        self.session = session
        self.exited = False

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *args):
        self.exited = True


class GeminiClientTests(unittest.IsolatedAsyncioTestCase):
    def create(self, **settings):
        session = NS(setup_complete=NS(session_id="synthetic-token-not-printed"),
                     send_realtime_input=AsyncMock(),
                     send_client_content=AsyncMock())
        context = FakeContext(session)
        sdk = NS(aio=NS(live=NS(connect=Mock(return_value=context)),
                        aclose=AsyncMock()), close=Mock())
        client = GeminiClient(GeminiConfig(api_key=KEY, **settings),
                              factory=lambda config: sdk)
        self.addAsyncCleanup(client.close)
        return client, sdk, session, context

    async def test_explicit_connect_and_idempotent_cleanup(self):
        client, sdk, session, context = self.create()
        self.assertFalse(sdk.aio.live.connect.called)
        await client.connect()
        args = sdk.aio.live.connect.call_args.kwargs
        self.assertEqual(args["config"]["response_modalities"], ["AUDIO"])
        self.assertNotIn("session_resumption", args["config"])
        self.assertNotIn("output_audio_transcription", args["config"])
        await client.close()
        await client.close()
        self.assertTrue(context.exited)
        sdk.aio.aclose.assert_awaited_once()
        sdk.close.assert_called_once()

    async def test_input_format_sequence_and_stream_end(self):
        client, sdk, session, context = self.create()
        await client.connect()
        await client.send_audio(PCMFrame(PCMFormat(), bytes(640), 3))
        blob = session.send_realtime_input.call_args.kwargs["audio"]
        self.assertEqual(blob["mime_type"], "audio/pcm;rate=16000")
        self.assertEqual(blob["data"], bytes(640))
        for frame in (
            PCMFrame(PCMFormat(), bytes(640), 2),
            PCMFrame(PCMFormat(24000), bytes(960), 4),
        ):
            with self.assertRaises(ProviderProtocolError):
                await client.send_audio(frame)
        await client.end_audio()
        self.assertEqual(session.send_realtime_input.call_args.kwargs,
                         {"audio_stream_end": True})

    async def test_text_has_explicit_user_role_and_turn_completion(self):
        client, sdk, session, context = self.create()
        await client.connect()
        await client.send_text("Say exactly: Jarvis is ready.")
        args = session.send_client_content.call_args.kwargs
        self.assertEqual(args["turns"]["role"], "user")
        self.assertTrue(args["turn_complete"])

    async def test_receive_restarts_after_each_completed_turn(self):
        client, sdk, session, context = self.create()
        calls = 0

        async def receive():
            nonlocal calls
            calls += 1
            yield message(bytes(960), turn_complete=True)

        session.receive = receive
        await client.connect()
        stream = client.events()
        kinds = [(await anext(stream)).kind for _ in range(4)]
        await stream.aclose()
        self.assertEqual(kinds, [EventKind.AUDIO, EventKind.TURN_COMPLETE] * 2)
        self.assertEqual(calls, 2)

    async def test_partial_connection_failure_is_sanitized_and_closed(self):
        client, sdk, session, context = self.create()
        context.__class__ = FailingContext
        with self.assertRaises(ProviderAuthenticationError) as raised:
            await client.connect()
        self.assertNotIn(KEY, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(context.exited)
        sdk.aio.aclose.assert_awaited_once()

    async def test_stalled_send_times_out_without_retry(self):
        client, sdk, session, context = self.create(io_timeout=.05)
        await client.connect()

        async def stalled(**kwargs):
            await asyncio.sleep(60)

        session.send_realtime_input.side_effect = stalled
        with self.assertRaises(ProviderTimeout):
            await client.send_audio(PCMFrame(PCMFormat(), bytes(640)))
        self.assertEqual(session.send_realtime_input.await_count, 1)

    async def test_model_discovery_is_one_bounded_page_and_closes_client(self):
        client, sdk, session, context = self.create()
        sdk.aio.models = NS(list=AsyncMock(return_value=NS(page=[
            NS(name="models/gemini-3.8-live",
               supported_actions=["bidiGenerateContent"]),
            NS(name="models/gemini-text-only", supported_actions=["generateContent"]),
            NS(name="models/non-gemini", supported_actions=[]),
        ])))
        result = await client.models()
        sdk.aio.models.list.assert_awaited_once_with(config={"page_size": 50})
        self.assertEqual(result, (
            ("models/gemini-3.8-live", True), ("models/gemini-text-only", False),
        ))
        sdk.aio.live.connect.assert_not_called()
        sdk.aio.aclose.assert_awaited_once()

    async def test_async_cleanup_failure_still_closes_sync_sdk_resources(self):
        client, sdk, session, context = self.create()
        await client.connect()
        sdk.aio.aclose.side_effect = RuntimeError(KEY)
        with self.assertRaises(ProviderNetworkError) as raised:
            await client.close()
        sdk.close.assert_called_once()
        self.assertNotIn(KEY, str(raised.exception))

    def test_debug_records_with_headers_and_audio_never_reach_root(self):
        logger = logging.getLogger("websockets.client")
        original = logger.level
        logger.setLevel(logging.DEBUG)
        parent = logging.getLogger("google_genai")
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        parent.addHandler(handler)
        original_handlers = tuple(parent.handlers)
        original_propagate = parent.propagate
        try:
            with self.assertNoLogs(level=logging.DEBUG):
                with quiet_sdk_logs():
                    logger.debug("header %s; private transcript", KEY)
                    logging.getLogger("google_genai.live").warning(
                        "synthetic audio payload"
                    )
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(tuple(parent.handlers), original_handlers)
            self.assertEqual(parent.propagate, original_propagate)
        finally:
            logger.setLevel(original)
            parent.removeHandler(handler)
            handler.close()

    async def test_unconfirmed_setup_is_a_failure(self):
        client, sdk, session, context = self.create()
        session.setup_complete = None
        with self.assertRaises(ProviderProtocolError):
            await client.connect()
        self.assertTrue(context.exited)

    def test_multiple_parts_and_completion_are_preserved(self):
        value = message(bytes(960), generation_complete=True, turn_complete=True)
        value.server_content.model_turn.parts.append(NS(inline_data=NS(
            data=bytes(960), mime_type="audio/pcm;rate=24000"
        )))
        events = parse_message(value)
        self.assertEqual([event.kind for event in events], [
            EventKind.AUDIO, EventKind.AUDIO, EventKind.GENERATION_COMPLETE,
            EventKind.TURN_COMPLETE,
        ])

    def test_interruption_discards_audio_in_the_same_message(self):
        events = parse_message(message(bytes(960), interrupted=True,
                                       turn_complete=True))
        self.assertEqual([event.kind for event in events], [EventKind.INTERRUPTED])
        self.assertEqual(parse_message(NS(go_away=NS(time_left="1s")))[0].kind,
                         EventKind.GO_AWAY)

    def test_background_thinking_status_controls_completion(self):
        self.assertEqual(parse_message(message(turn_complete=True,
                                               interaction_status="IN_PROGRESS")), ())
        self.assertEqual(parse_message(message(interaction_status="IDLE"))[0].kind,
                         EventKind.TURN_COMPLETE)

    def test_invalid_audio_data_and_unexpected_tools_are_rejected(self):
        with self.assertRaises(ProviderProtocolError):
            parse_message(message("not binary PCM"))
        with self.assertRaises(ProviderProtocolError):
            parse_message(NS(tool_call=NS(function_calls=[])))

    def test_safe_error_categories_and_logging_restoration(self):
        for code, expected in (
            (401, ProviderAuthenticationError), (403, ProviderPermissionError),
            (404, ProviderModelError), (429, ProviderQuotaError),
            (1006, ProviderNetworkError),
        ):
            error = RuntimeError(KEY)
            error.code = code
            result = classify_error(error)
            self.assertIsInstance(result, expected)
            self.assertNotIn(KEY, str(result))
        logger = logging.getLogger("websockets.client")
        original = logger.disabled
        with quiet_sdk_logs():
            self.assertTrue(logger.disabled)
        self.assertEqual(logger.disabled, original)


class FailingContext(FakeContext):
    async def __aenter__(self):
        error = RuntimeError("invalid api key: " + KEY)
        error.code = 401
        raise error
