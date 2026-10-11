"""Named dispatch and failure behavior without external services."""

import unittest

from jarvis.dispatch import (
    CommandDispatcher,
    HandlerError,
    RegistrationError,
    UnknownCommandError,
)


class DispatchTests(unittest.TestCase):
    def test_dispatch_returns_the_handler_result(self) -> None:
        dispatcher = CommandDispatcher()
        result = {"operational": True}
        dispatcher.register("check", lambda: result)
        self.assertIs(dispatcher.dispatch("check"), result)

    def test_command_names_are_sorted(self) -> None:
        dispatcher = CommandDispatcher()
        dispatcher.register("zeta", lambda: None)
        dispatcher.register("alpha", lambda: None)
        self.assertEqual(dispatcher.command_names, ("alpha", "zeta"))

    def test_duplicate_registration_preserves_original_handler(self) -> None:
        dispatcher = CommandDispatcher()
        dispatcher.register("hello", lambda: "original")
        with self.assertRaisesRegex(RegistrationError, "already registered"):
            dispatcher.register("hello", lambda: "replacement")
        self.assertEqual(dispatcher.dispatch("hello"), "original")

    def test_invalid_registration(self) -> None:
        dispatcher = CommandDispatcher()
        for name in ("", "two words", "new\nline", "control\x00character"):
            with self.subTest(name=name):
                with self.assertRaises(RegistrationError):
                    dispatcher.register(name, lambda: None)
        with self.assertRaisesRegex(RegistrationError, "callable"):
            dispatcher.register("invalid", None)  # type: ignore[arg-type]

    def test_unknown_command_is_clear(self) -> None:
        with self.assertRaisesRegex(
            UnknownCommandError, "Unknown command: 'missing'"
        ):
            CommandDispatcher().dispatch("missing")

    def test_handler_failure_preserves_cause_and_allows_recovery(self) -> None:
        dispatcher = CommandDispatcher()
        original = ValueError("example cause")

        def fail() -> object:
            raise original

        dispatcher.register("fail", fail)
        dispatcher.register("healthy", lambda: "working")
        with self.assertRaisesRegex(
            HandlerError, "'fail'.*ValueError"
        ) as raised:
            dispatcher.dispatch("fail")
        self.assertIs(raised.exception.__cause__, original)
        self.assertEqual(dispatcher.dispatch("healthy"), "working")

    def test_keyboard_interrupt_is_not_wrapped(self) -> None:
        dispatcher = CommandDispatcher()

        def interrupt() -> object:
            raise KeyboardInterrupt

        dispatcher.register("interrupt", interrupt)
        with self.assertRaises(KeyboardInterrupt):
            dispatcher.dispatch("interrupt")
