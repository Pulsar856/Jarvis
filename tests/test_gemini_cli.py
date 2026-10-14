"""CLI provider tests forbid network requests and use no real credentials."""

import asyncio
import builtins
import io
import os
import signal
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import AsyncMock, patch

from jarvis.cli import main
from jarvis.providers.gemini.cli import guarded
from jarvis.audio.control import StreamControl
from jarvis.audio.errors import AudioCancelled
from jarvis.providers.gemini.errors import ProviderTimeout


class GeminiCliTests(unittest.TestCase):
    def invoke(self, arguments, environment=None):
        output, errors = io.StringIO(), io.StringIO()
        with (
            patch.dict(os.environ, environment or {}, clear=True),
            redirect_stdout(output), redirect_stderr(errors),
        ):
            result = main(arguments)
        return result, output.getvalue(), errors.getvalue()

    def test_offline_models_and_core_do_not_construct_provider(self):
        with patch("jarvis.providers.gemini.cli.GeminiClient") as client:
            for arguments in (["ai", "models"], ["status"], ["demo"],
                              ["ai", "--help"]):
                with self.subTest(arguments=arguments):
                    if arguments[-1] == "--help":
                        with self.assertRaises(SystemExit) as raised:
                            self.invoke(arguments)
                        self.assertEqual(raised.exception.code, 0)
                    else:
                        result, output, errors = self.invoke(arguments)
                        self.assertEqual(result, 0, errors)
            client.assert_not_called()

    def test_missing_key_fails_before_provider_or_audio_start(self):
        with patch("jarvis.providers.gemini.cli.GeminiClient") as client:
            result, output, errors = self.invoke(["ai", "connect-test"])
        self.assertEqual(result, 1)
        self.assertIn("Set GEMINI_API_KEY locally", errors)
        self.assertNotIn("Traceback", errors)
        client.assert_not_called()

    def test_core_and_audio_metadata_ignore_invalid_provider_settings(self):
        environment = {
            "GEMINI_MODEL": "invalid://model",
            "GEMINI_API_KEY_FILE": "/unrelated/private/path",
            "JARVIS_GEMINI_MAX_SECONDS": "invalid",
        }
        with patch("jarvis.providers.gemini.cli.GeminiClient") as client:
            for arguments in (["status"], ["demo"], ["audio", "stream-info"]):
                result, output, errors = self.invoke(arguments, environment)
                self.assertEqual(result, 0, errors)
            client.assert_not_called()

    def test_missing_optional_sdk_is_safe_and_core_remains_usable(self):
        original_import = builtins.__import__

        def without_sdk(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "google" and "genai" in fromlist:
                raise ImportError("synthetic optional SDK absence")
            return original_import(name, globals, locals, fromlist, level)

        # Intercept even when another test has already cached google.genai.
        with patch("builtins.__import__", side_effect=without_sdk):
            result, output, errors = self.invoke(
                ["ai", "connect-test"], {"GEMINI_API_KEY": "synthetic-key"}
            )
            self.assertEqual(result, 1)
            self.assertIn("Install the optional Jarvis gemini extra", errors)
            self.assertNotIn("synthetic-key", output + errors)
            self.assertEqual(self.invoke(["status"])[0], 0)

    def test_connectivity_test_constructs_and_closes_fake_provider(self):
        client = AsyncMock()
        with patch("jarvis.providers.gemini.cli.GeminiClient", return_value=client):
            result, output, errors = self.invoke(
                ["ai", "connect-test"], {"GEMINI_API_KEY": "synthetic-key"}
            )
        self.assertEqual(result, 0, errors)
        self.assertIn("setup confirmed", output)
        client.connect.assert_awaited_once()
        client.close.assert_awaited_once()
        self.assertNotIn("synthetic-key", output + errors)

    @unittest.skipUnless(os.name == "posix", "POSIX AI CLI signals")
    def test_real_signals_during_fake_connection_restore_normal_shutdown(self):
        script = '''
import asyncio
import sys
import jarvis.providers.gemini.cli as provider_cli
from jarvis.cli import main
class FakeClient:
    def __init__(self, config): pass
    async def connect(self):
        print("FAKE_CONNECT_READY", flush=True)
        await asyncio.sleep(60)
    async def close(self):
        print("FAKE_CONNECT_CLOSED", flush=True)
provider_cli.GeminiClient = FakeClient
sys.exit(main(["ai", "connect-test"]))
'''
        for signum in (signal.SIGINT, signal.SIGTERM):
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith("JARVIS_") and key != "PYTHONPATH"}
            environment["GEMINI_API_KEY"] = "synthetic-key"
            process = subprocess.Popen(
                [sys.executable, "-c", script], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=environment,
            )
            try:
                import selectors
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    self.assertTrue(selector.select(3))
                    ready = os.read(process.stdout.fileno(), 4096)
                    self.assertIn(b"FAKE_CONNECT_READY", ready)
                process.send_signal(signum)
                output, errors = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 128 + signum, errors.decode())
                self.assertIn(b"FAKE_CONNECT_CLOSED", output)
                self.assertNotIn(b"-> failed", errors)
                self.assertNotIn(b"synthetic-key", output + errors)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=3)

    @unittest.skipUnless(os.name == "posix", "POSIX AI streaming signals")
    def test_real_signals_during_fake_capture_and_response_playback(self):
        script = '''
import sys
sys.path.insert(0, "tests")
from test_conversation import AudioFactory, FakeAudio, FakeProvider
import jarvis.conversation as conversation_module
import jarvis.providers.gemini.cli as provider_cli
from jarvis.audio.errors import StreamCancelled
from jarvis.cli import main
mode = sys.argv[1]
factory = AudioFactory()
provider = FakeProvider(factory, respond=mode == "speak-test")
original_voice = conversation_module.VoiceConversation
original_start = FakeAudio.start
def start(audio):
    original_start(audio)
    if audio.is_capture:
        print("FAKE_AUDIO_READY", flush=True)
def write(audio, frame, timeout=2):
    print("FAKE_AUDIO_READY", flush=True)
    audio.stopped.wait(3)
    raise StreamCancelled("synthetic stopped playback")
FakeAudio.start = start
FakeAudio.write_frame = write
original_close = provider.close
async def close():
    await original_close()
    assert all(audio.closed for audio in factory.instances)
    print("FAKE_PROVIDER_CLOSED", flush=True)
provider.close = close
provider_cli.GeminiClient = lambda config: provider
conversation_module.VoiceConversation = lambda *args, **kwargs: original_voice(
    *args, audio_factory=factory, **kwargs)
sys.exit(main(["ai", mode, "--duration", "10"]))
'''
        for mode in ("talk", "speak-test"):
            for signum in (signal.SIGINT, signal.SIGTERM):
                with self.subTest(mode=mode, signum=signum):
                    environment = {
                        key: value for key, value in os.environ.items()
                        if not key.startswith("JARVIS_")
                        and key != "PYTHONPATH"
                    }
                    environment["GEMINI_API_KEY"] = "synthetic-key"
                    process = subprocess.Popen(
                        [sys.executable, "-c", script, mode],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        env=environment,
                    )
                    ready = b""
                    try:
                        import selectors
                        import time
                        deadline = time.monotonic() + 3
                        with selectors.DefaultSelector() as selector:
                            selector.register(process.stdout, selectors.EVENT_READ)
                            while b"FAKE_AUDIO_READY" not in ready:
                                remaining = deadline - time.monotonic()
                                self.assertGreater(remaining, 0)
                                self.assertTrue(selector.select(remaining))
                                chunk = os.read(process.stdout.fileno(), 4096)
                                self.assertTrue(chunk)
                                ready += chunk
                        process.send_signal(signum)
                        output, errors = process.communicate(timeout=5)
                        self.assertEqual(process.returncode, 128 + signum,
                                         errors.decode())
                        self.assertIn(b"FAKE_PROVIDER_CLOSED", output)
                        self.assertNotIn(b"-> failed", errors)
                        self.assertNotIn(b"synthetic-key", ready + output + errors)
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.communicate(timeout=3)


class ProviderGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_deadline_cancels_and_waits_for_cleanup(self):
        cleaned = asyncio.Event()

        async def operation():
            try:
                await asyncio.sleep(60)
            finally:
                cleaned.set()

        with self.assertRaises(ProviderTimeout):
            await guarded(operation(), StreamControl(), .05)
        self.assertTrue(cleaned.is_set())

    async def test_programmatic_cancellation_is_cooperative(self):
        control = StreamControl()
        control.request_stop()
        from jarvis.audio.errors import StreamCancelled
        with self.assertRaises(StreamCancelled):
            await guarded(asyncio.sleep(60), control, 10)
