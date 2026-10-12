"""Handler ownership and CLI cleanup; real OS signals live in test_cli."""

import io
import logging
import signal
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from jarvis.application import Application
from jarvis.cli import main
from jarvis.config import AppConfig
from jarvis.logging import close_logging, configure_logging
from jarvis.signals import SignalHandlingError, capture_shutdown_signals


class SignalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.originals = {
            signum: signal.getsignal(signum)
            for signum in (signal.SIGINT, signal.SIGTERM)
        }
        for signum, handler in self.originals.items():
            self.addCleanup(signal.signal, signum, handler)

    def assert_handlers_restored(self) -> None:
        for signum, handler in self.originals.items():
            self.assertEqual(signal.getsignal(signum), handler)

    def test_normal_scope_restores_custom_handlers(self) -> None:
        def previous(signum: int, frame: object) -> None:
            pass

        for signum in self.originals:
            signal.signal(signum, previous)
        with capture_shutdown_signals():
            for signum in self.originals:
                self.assertNotEqual(signal.getsignal(signum), previous)
        for signum in self.originals:
            self.assertIs(signal.getsignal(signum), previous)

    def test_exception_restores_handlers_and_preserves_cause(self) -> None:
        original = RuntimeError("controlled error")
        with self.assertRaises(RuntimeError) as raised:
            with capture_shutdown_signals():
                raise original
        self.assertIs(raised.exception, original)
        self.assert_handlers_restored()

    def test_partial_installation_failure_restores_handlers(self) -> None:
        real_signal = signal.signal

        def install(signum: int, handler: object) -> object:
            if signum == signal.SIGTERM:
                raise OSError("controlled installation failure")
            return real_signal(signum, handler)

        with patch("jarvis.signals.signal.signal", side_effect=install):
            with self.assertRaises(SignalHandlingError) as raised:
                with capture_shutdown_signals():
                    self.fail("installation should fail")
        self.assertIsInstance(raised.exception.__cause__, OSError)
        self.assert_handlers_restored()

    def test_foreign_handler_is_not_overwritten(self) -> None:
        real_getsignal = signal.getsignal

        def getsignal(signum: int) -> object:
            if signum == signal.SIGTERM:
                return None
            return real_getsignal(signum)

        with patch("jarvis.signals.signal.getsignal", side_effect=getsignal):
            with self.assertRaisesRegex(SignalHandlingError, "restored"):
                with capture_shutdown_signals():
                    self.fail("foreign handler must not be overwritten")
        self.assert_handlers_restored()

    def test_repeated_signals_record_one_shutdown_intent(self) -> None:
        with capture_shutdown_signals() as request:
            for signum in (signal.SIGTERM, signal.SIGTERM, signal.SIGINT):
                handler = signal.getsignal(signum)
                self.assertTrue(callable(handler))
                handler(signum, None)
            self.assertEqual(request.signum, signal.SIGTERM)
        self.assert_handlers_restored()

    def test_embedding_application_does_not_install_handlers(self) -> None:
        with Application(AppConfig()) as application:
            self.assert_handlers_restored()
            application.request_shutdown()
            application.run()
        self.assert_handlers_restored()

    def test_cli_closes_logs_before_restoring_handlers(self) -> None:
        output, errors = io.StringIO(), io.StringIO()
        owned_handlers: list[logging.Handler] = []
        wait_calls = 0

        def setup(config: AppConfig) -> None:
            configure_logging(config)
            owned_handlers.extend(logging.getLogger("jarvis").handlers)

        def interrupted_wait(event: object, timeout: float) -> bool:
            nonlocal wait_calls
            wait_calls += 1
            if wait_calls > 1:
                raise AssertionError("recorded signal did not stop the loop")
            for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGTERM):
                signal.getsignal(signum)(signum, None)
            return False

        def close_in_scope() -> None:
            for signum in self.originals:
                self.assertNotEqual(
                    signal.getsignal(signum), self.originals[signum]
                )
            close_logging()

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.dict(
                    "os.environ", {"JARVIS_LOG_DIR": directory}, clear=True
                ),
                patch("jarvis.cli.configure_logging", side_effect=setup),
                patch("jarvis.cli.close_logging", side_effect=close_in_scope),
                patch("jarvis.application.Event.wait", interrupted_wait),
                redirect_stdout(output),
                redirect_stderr(errors),
            ):
                result = main(["run"])
            file_logs = (Path(directory) / "jarvis.log").read_text(
                encoding="utf-8"
            )
        self.assertEqual(result, 0, errors.getvalue())
        self.assertEqual(errors.getvalue().count("Received SIGTERM"), 1)
        self.assertEqual(errors.getvalue().count("running -> stopped"), 1)
        self.assertIn("running -> stopped", file_logs)
        self.assertEqual(logging.getLogger("jarvis").handlers, [])
        file_handlers = [
            item for item in owned_handlers
            if isinstance(item, logging.FileHandler)
        ]
        self.assertEqual(len(file_handlers), 1)
        self.assertIsNone(file_handlers[0].stream)
        self.assert_handlers_restored()

    def test_runtime_error_keeps_nonzero_exit_and_cleanup(self) -> None:
        errors = io.StringIO()
        with (
            patch.dict("os.environ", {}, clear=True),
            patch(
                "jarvis.cli.Application.run",
                side_effect=OSError("controlled runtime failure"),
            ),
            redirect_stdout(io.StringIO()),
            redirect_stderr(errors),
        ):
            result = main(["run"])
        self.assertEqual(result, 1)
        self.assertIn("controlled runtime failure", errors.getvalue())
        self.assertIn("running -> failed", errors.getvalue())
        self.assertIn("failed -> stopped", errors.getvalue())
        self.assertEqual(logging.getLogger("jarvis").handlers, [])
        self.assert_handlers_restored()

    def test_unexpected_exception_propagates_after_cleanup(self) -> None:
        original = RuntimeError("controlled programming error")
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("jarvis.cli.Application.run", side_effect=original),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            with self.assertRaises(RuntimeError) as raised:
                main(["run"])
        self.assertIs(raised.exception, original)
        self.assertEqual(logging.getLogger("jarvis").handlers, [])
        self.assert_handlers_restored()
