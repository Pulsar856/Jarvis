"""Explicit lifecycle for a single, synchronous application process."""

import logging
from enum import Enum
from threading import Event
from types import TracebackType

from jarvis.config import AppConfig
from jarvis.dispatch import CommandDispatcher
from jarvis.modules.demo import register as register_demo

logger = logging.getLogger(__name__)


class ApplicationError(RuntimeError):
    """An operation is incompatible with the application lifecycle."""


class ApplicationState(Enum):
    """Observable lifecycle states."""

    INITIALIZED = "initialized"
    RUNNING = "running"
    FAILED = "failed"
    STOPPED = "stopped"


class Application:
    """Own settings, lifecycle, and shutdown without worker threads."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._state = ApplicationState.INITIALIZED
        self._shutdown_requested = Event()
        self.dispatcher = CommandDispatcher()
        register_demo(self.dispatcher)
        logger.info("%s %s initialized", config.app_name, config.app_version)

    @property
    def state(self) -> ApplicationState:
        """Expose lifecycle state without permitting outside mutation."""
        return self._state

    def _transition(self, state: ApplicationState) -> None:
        logger.info(
            "Application state: %s -> %s", self._state.value, state.value
        )
        self._state = state

    def _require_running(self) -> None:
        if self.state is not ApplicationState.RUNNING:
            raise ApplicationError(
                "This operation requires a running application."
            )

    def start(self) -> None:
        """Start once; create a new application for a subsequent lifecycle."""
        if self.state is not ApplicationState.INITIALIZED:
            raise ApplicationError(
                "Only an initialized application can start."
            )
        self._transition(ApplicationState.RUNNING)

    def request_shutdown(self) -> None:
        """Wake the persistent loop through a cooperative shutdown signal."""
        self._shutdown_requested.set()

    def dispatch(self, name: str) -> object:
        """Execute a registered command while the application is running."""
        self._require_running()
        logger.debug("Dispatching command %r", name)
        return self.dispatcher.dispatch(name)

    def run(self) -> None:
        """Wait until shutdown is requested or Ctrl+C is received."""
        self._require_running()
        try:
            while not self._shutdown_requested.wait(timeout=1.0):
                pass
        except KeyboardInterrupt:
            logger.info("Interrupt received; shutting down")
            self.request_shutdown()

    def shutdown(self) -> None:
        """Stop idempotently, including after an application failure."""
        self.request_shutdown()
        if self.state is not ApplicationState.STOPPED:
            self._transition(ApplicationState.STOPPED)

    def __enter__(self) -> "Application":
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is not None and issubclass(exc_type, Exception):
            logger.error("Application failure (%s)", exc_type.__name__)
            self._transition(ApplicationState.FAILED)
        self.shutdown()
