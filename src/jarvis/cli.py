"""Terminal commands and their user-facing output."""

import argparse
import logging
import platform
import sys
from collections.abc import Sequence
from contextlib import ExitStack

from jarvis import __version__
from jarvis.audio.cli import add_audio_commands, run_audio
from jarvis.audio.errors import AudioCancelled, AudioError
from jarvis.application import Application, ApplicationError
from jarvis.config import AppConfig, ConfigurationError
from jarvis.dispatch import DispatchError
from jarvis.logging import close_logging, configure_logging
from jarvis.signals import SignalHandlingError, capture_shutdown_signals


def build_parser() -> argparse.ArgumentParser:
    """Describe the supported terminal interface."""
    parser = argparse.ArgumentParser(
        prog="jarvis",
        description=f"Jarvis {__version__} — personal assistant core.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", title="commands")
    commands.add_parser(
        "status", help="show configuration and runtime information"
    )
    commands.add_parser("demo", help="execute the built-in hello command")
    commands.add_parser("run", help="run until Ctrl+C or SIGTERM")
    add_audio_commands(commands)
    return parser


def show_status(application: Application) -> None:
    """Print an explicit, non-secret configuration and runtime summary."""
    config = application.config
    print(f"{config.app_name} {config.app_version}")
    print(f"State: {application.state.value}")
    names = ", ".join(application.dispatcher.command_names)
    print(f"Registered commands: {names}")
    print(f"Log level: {config.log_level}")
    print(f"Log directory: {config.log_dir or 'disabled (console only)'}")
    print(f"Python: {platform.python_version()}")
    print(
        f"Platform: {platform.system()} {platform.release()} "
        f"({platform.machine()})"
    )
    print(f"Interpreter: {sys.executable}")
    print("External services: none required")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    with ExitStack() as process_scope:
        try:
            signals = (
                process_scope.enter_context(capture_shutdown_signals())
                if args.command in ("run", "audio") else None
            )
            config = AppConfig.from_env()
            configure_logging(config)
            with Application(config) as application:
                if args.command == "status":
                    show_status(application)
                elif args.command == "demo":
                    print(application.dispatch("hello"))
                elif args.command == "audio":
                    try:
                        run_audio(args, signals)
                    except AudioCancelled as exc:
                        logging.getLogger(__name__).info("%s", exc)
                        return 128 + exc.signum
                else:
                    print(
                        f"{config.app_name} {config.app_version} running. "
                        "Press Ctrl+C to stop.",
                        flush=True,
                    )
                    application.run(signals)
            return 0
        except KeyboardInterrupt:
            logging.getLogger(__name__).info(
                "Interrupt received; shutting down"
            )
            return 0
        except (
            ConfigurationError, ApplicationError, DispatchError,
            SignalHandlingError, AudioError, OSError,
        ) as exc:
            print(f"jarvis: {exc}", file=sys.stderr)
            return 1
        finally:
            close_logging()
