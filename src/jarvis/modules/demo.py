"""A deterministic, offline example of the module registration convention."""

from jarvis.dispatch import CommandDispatcher


def hello() -> str:
    """Return a fixed result without requiring any external resources."""
    return "Hello. Jarvis core is operational."


def register(dispatcher: CommandDispatcher) -> None:
    """Make this module's concrete command available to the application."""
    dispatcher.register("hello", hello)
