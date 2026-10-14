"""Validate the pinned SDK's real serialization against a fake WebSocket."""

import base64
import importlib.util
import json
import os
import unittest
from unittest.mock import patch

from jarvis.audio.frames import PCMFormat, PCMFrame
from jarvis.providers.gemini.client import EventKind, GeminiClient
from jarvis.providers.gemini.config import GeminiConfig


def sdk_available():
    try:
        return importlib.util.find_spec("google.genai") is not None
    except ModuleNotFoundError:
        return False


@unittest.skipUnless(sdk_available(), "optional google-genai SDK not installed")
class SDKContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_sdk_header_setup_pcm_and_turn_fields_without_network(self):
        key = "synthetic-private-sdk-contract-key"

        class Socket:
            def __init__(self):
                self.sent = []
                self.received = [
                    {"setupComplete": {}},
                    {"serverContent": {"modelTurn": {"parts": [{"inlineData": {
                        "mimeType": "audio/pcm;rate=24000",
                        "data": base64.b64encode(bytes(960)).decode(),
                    }}]}, "turnComplete": True}},
                ]
                self.closed = False

            async def send(self, value):
                self.sent.append(json.loads(value))

            async def recv(self, **kwargs):
                return json.dumps(self.received.pop(0)).encode()

        socket = Socket()
        calls = []

        class Context:
            async def __aenter__(self):
                return socket

            async def __aexit__(self, *args):
                socket.closed = True

        def connection(uri, **kwargs):
            calls.append((uri, kwargs))
            return Context()

        client = GeminiClient(GeminiConfig(api_key=key))
        environment = {
            "GOOGLE_GENAI_USE_VERTEXAI": "true",
            "GOOGLE_GENAI_USE_ENTERPRISE": "true",
            "GOOGLE_GEMINI_BASE_URL": "https://wrong.example",
        }
        with patch.dict(os.environ, environment):
            with patch("google.genai.live.ws_connect", side_effect=connection):
                try:
                    await client.connect()
                    await client.send_audio(PCMFrame(PCMFormat(), bytes(640)))
                    await client.end_audio()
                    await client.send_text("Say ready.")
                    events = client.events()
                    self.assertEqual((await anext(events)).kind, EventKind.AUDIO)
                    self.assertEqual(
                        (await anext(events)).kind, EventKind.TURN_COMPLETE
                    )
                    await events.aclose()
                finally:
                    await client.close()
        self.assertTrue(socket.closed)
        self.assertEqual(len(calls), 1)
        uri, options = calls[0]
        self.assertTrue(uri.startswith("wss://generativelanguage.googleapis.com/"))
        self.assertNotIn(key, uri)
        self.assertEqual(options["additional_headers"]["x-goog-api-key"], key)
        setup = socket.sent[0]["setup"]
        self.assertEqual(setup["model"], "models/gemini-3.8-live")
        self.assertEqual(setup["generationConfig"]["responseModalities"], ["AUDIO"])
        self.assertNotIn("tools", setup)
        audio = socket.sent[1]["realtime_input"]["audio"]
        self.assertEqual(audio["mime_type"], "audio/pcm;rate=16000")
        self.assertEqual(base64.b64decode(audio["data"]), bytes(640))
        self.assertTrue(socket.sent[2]["realtime_input"]["audioStreamEnd"])
