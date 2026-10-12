"""Lifecycle boundaries, central dispatch, and controlled shutdown."""

import signal
import unittest
from unittest.mock import patch

from jarvis.application import Application, ApplicationError, ApplicationState
from jarvis.config import AppConfig
from jarvis.dispatch import HandlerError
from jarvis.signals import ShutdownRequest


class ApplicationTests(unittest.TestCase):
    def test_initialization_registers_demo_and_retains_config(self) -> None:
        config = AppConfig()
        application = Application(config)
        self.assertIs(application.config, config)
        self.assertIs(application.state, ApplicationState.INITIALIZED)
        self.assertEqual(application.dispatcher.command_names, ("hello",))

    def test_context_starts_and_stops_with_transition_logs(self) -> None:
        with self.assertLogs("jarvis.application", level="INFO") as logs:
            with Application(AppConfig()) as application:
                self.assertIs(application.state, ApplicationState.RUNNING)
        self.assertIs(application.state, ApplicationState.STOPPED)
        self.assertTrue(
            any("initialized -> running" in line for line in logs.output)
        )
        self.assertTrue(
            any("running -> stopped" in line for line in logs.output)
        )

    def test_demo_execution(self) -> None:
        with Application(AppConfig()) as application:
            self.assertEqual(
                application.dispatch("hello"),
                "Hello. Jarvis core is operational.",
            )

    def test_application_dispatch_uses_central_dispatcher(self) -> None:
        marker = object()
        with Application(AppConfig()) as application:
            with patch.object(
                application.dispatcher, "dispatch", return_value=marker
            ) as handler:
                self.assertIs(application.dispatch("hello"), marker)
                handler.assert_called_once_with("hello")

    def test_operations_require_running_state(self) -> None:
        application = Application(AppConfig())
        operations = (lambda: application.dispatch("hello"), application.run)
        for operation in operations:
            with self.assertRaises(ApplicationError):
                operation()
        application.shutdown()
        with self.assertRaises(ApplicationError):
            application.dispatch("hello")

    def test_start_is_single_use_and_shutdown_is_idempotent(self) -> None:
        application = Application(AppConfig())
        application.start()
        with self.assertRaises(ApplicationError):
            application.start()
        application.shutdown()
        application.shutdown()
        self.assertIs(application.state, ApplicationState.STOPPED)
        with self.assertRaises(ApplicationError):
            application.start()

    def test_cooperative_shutdown_does_not_block(self) -> None:
        with Application(AppConfig()) as application:
            application.request_shutdown()
            application.run()
        self.assertIs(application.state, ApplicationState.STOPPED)

    def test_run_handles_keyboard_interrupt(self) -> None:
        with Application(AppConfig()) as application:
            with patch.object(
                application._shutdown_requested,
                "wait",
                side_effect=KeyboardInterrupt,
            ):
                application.run()
        self.assertIs(application.state, ApplicationState.STOPPED)

    def test_uncaught_failure_logs_failed_state_then_cleans_up(self) -> None:
        original = RuntimeError("example failure")
        application = Application(AppConfig())
        with self.assertLogs("jarvis.application", level="INFO") as logs:
            with self.assertRaises(RuntimeError) as raised:
                with application:
                    raise original
        self.assertIs(raised.exception, original)
        self.assertIs(application.state, ApplicationState.STOPPED)
        self.assertTrue(
            any("running -> failed" in line for line in logs.output)
        )
        self.assertTrue(
            any("failed -> stopped" in line for line in logs.output)
        )

    def test_caught_handler_failure_keeps_application_usable(self) -> None:
        def fail() -> object:
            raise ValueError("example failure")

        with Application(AppConfig()) as application:
            application.dispatcher.register("fail", fail)
            with self.assertRaises(HandlerError):
                application.dispatch("fail")
            self.assertIs(application.state, ApplicationState.RUNNING)
            self.assertEqual(
                application.dispatch("hello"),
                "Hello. Jarvis core is operational.",
            )

    def test_repeated_shutdown_requests_have_one_stop_transition(self) -> None:
        with self.assertLogs("jarvis.application", level="INFO") as logs:
            with Application(AppConfig()) as application:
                application.request_shutdown()
                application.request_shutdown()
                application.run()
                application.shutdown()
                application.shutdown()
        self.assertIs(application.state, ApplicationState.STOPPED)
        self.assertEqual(
            sum("running -> stopped" in line for line in logs.output), 1
        )

    def test_signal_before_run_is_honored_without_waiting(self) -> None:
        request = ShutdownRequest(signum=signal.SIGTERM)
        with self.assertLogs("jarvis.application", level="INFO") as logs:
            with Application(AppConfig()) as application:
                with patch.object(
                    application._shutdown_requested,
                    "wait",
                    side_effect=AssertionError("should not wait after signal"),
                ):
                    application.run(request)
        self.assertIs(application.state, ApplicationState.STOPPED)
        self.assertTrue(
            any("Received SIGTERM" in line for line in logs.output)
        )
