"""Explicit registration and synchronous dispatch of named Python handlers."""

from collections.abc import Callable

CommandHandler = Callable[[], object]


class DispatchError(RuntimeError):
    """Base class for command registration and execution errors."""


class RegistrationError(DispatchError):
    """A command name or handler cannot be registered."""


class UnknownCommandError(DispatchError):
    """No handler has been registered for the requested command."""


class HandlerError(DispatchError):
    """A registered handler raised an exception; the cause is preserved."""


class CommandDispatcher:
    """A local table of no-argument handlers returning results."""

    def __init__(self) -> None:
        self._handlers: dict[str, CommandHandler] = {}

    @property
    def command_names(self) -> tuple[str, ...]:
        """List the current commands in deterministic order."""
        return tuple(sorted(self._handlers))

    def register(self, name: str, handler: CommandHandler) -> None:
        """Register a callable under a unique, nonempty command name."""
        if not isinstance(name, str) or not name or any(
            char.isspace() or ord(char) < 32 or ord(char) == 127
            for char in name
        ):
            raise RegistrationError(
                "Command names must be nonempty and contain no whitespace "
                "or control characters."
            )
        if name in self._handlers:
            raise RegistrationError(f"Command {name!r} is already registered.")
        if not callable(handler):
            raise RegistrationError("A command handler must be callable.")
        self._handlers[name] = handler

    def dispatch(self, name: str) -> object:
        """Invoke a handler, retaining its original exception as the cause."""
        try:
            handler = self._handlers[name]
        except KeyError:
            raise UnknownCommandError(f"Unknown command: {name!r}.") from None
        try:
            return handler()
        except Exception as exc:
            raise HandlerError(
                f"Command {name!r} failed ({type(exc).__name__})."
            ) from exc
