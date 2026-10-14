"""Explicit provider CLI; documented model information stays offline."""

import argparse
import asyncio
import json
from dataclasses import asdict, replace

from jarvis.audio.control import StreamControl
from jarvis.providers.gemini.client import GeminiClient
from jarvis.providers.gemini.config import DOCUMENTED_MODELS, GeminiConfig
from jarvis.providers.gemini.runtime import guarded
from jarvis.signals import ShutdownRequest


def add_ai_commands(commands: argparse._SubParsersAction) -> None:
    parser = commands.add_parser("ai", help="explicit Gemini Live operations")
    ai = parser.add_subparsers(dest="ai_command", required=True)
    models = ai.add_parser("models", help="documented models; offline by default")
    models.add_argument("--remote", action="store_true",
                        help="explicitly query one Google API model-list page")
    models.add_argument("--model", help="override GEMINI_MODEL")
    connect = ai.add_parser("connect-test", help="bounded Live setup and close")
    connect.add_argument("--model", help="override GEMINI_MODEL")
    for name, description in (
        ("speak-test", "request a brief spoken test response and play it"),
        ("talk", "explicit bounded microphone-to-Gemini conversation"),
    ):
        command = ai.add_parser(name, help=description)
        command.add_argument("--model", help="override GEMINI_MODEL")
        command.add_argument("--duration", type=duration_argument,
                             help="maximum connected session seconds (1–120)")
        command.add_argument("--output-device", help="playback ID or 'jabra'")
        if name == "talk":
            command.add_argument("--input-device", help="capture ID or 'jabra'")
            command.add_argument("--max-turns", type=turn_argument, default=3)
            command.add_argument(
                "--full-duplex", action="store_true",
                help="experimental simultaneous input/output with server interruption",
            )


def duration_argument(value: str) -> int:
    try:
        duration = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("duration must be an integer") from None
    if not 1 <= duration <= 120:
        raise argparse.ArgumentTypeError("duration must be 1–120 seconds")
    return duration


def turn_argument(value: str) -> int:
    try:
        result = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("turns must be an integer") from None
    if not 1 <= result <= 5:
        raise argparse.ArgumentTypeError("turns must be 1–5")
    return result


async def connect_test(config: GeminiConfig) -> None:
    client = GeminiClient(config)
    try:
        await client.connect()
    finally:
        await client.close()
    print(f"Gemini Live setup confirmed for {config.model}; connection closed.")


async def remote_models(config: GeminiConfig) -> None:
    client = GeminiClient(config)
    models = await client.models()
    for name, supports_bidi in models:
        action = "bidiGenerateContent advertised" if supports_bidi else (
            "Live capability not advertised in listing"
        )
        print(f"{name}: {action}")
    print("One page listed; this does not certify Live access or free-tier billing.")


async def voice_operation(arguments, config: GeminiConfig, control: StreamControl):
    from jarvis.audio.cli import configured_device
    from jarvis.conversation import VoiceConversation

    config = replace(
        config,
        max_seconds=arguments.duration if arguments.duration is not None else (
            min(30, config.max_seconds) if arguments.ai_command == "speak-test"
            else config.max_seconds
        ),
        max_turns=getattr(arguments, "max_turns", 1),
    )
    full_duplex = getattr(arguments, "full_duplex", False)
    conversation = VoiceConversation(
        GeminiClient(config), config, control=control,
        input_selector=configured_device(
            "capture", getattr(arguments, "input_device", None)
        ),
        output_selector=configured_device("playback", arguments.output_device),
        full_duplex=full_duplex,
    )
    prompt = (
        "Please say exactly: Jarvis is ready."
        if arguments.ai_command == "speak-test" else None
    )
    print(
        f"Gemini Live {arguments.ai_command}: {config.model}, "
        f"maximum {config.max_seconds}s. Ctrl+C stops. "
        + ("Experimental full-duplex." if full_duplex else "Sequential turn-taking."),
        flush=True,
    )
    try:
        await conversation.run(prompt)
    finally:
        print("Conversation metrics (host timings; audible success unverified):")
        print(json.dumps(asdict(conversation.metrics), indent=2, sort_keys=True))


def run_ai(arguments: argparse.Namespace, signals: ShutdownRequest | None):
    offline = arguments.ai_command == "models" and not arguments.remote
    config = GeminiConfig.from_env(need_key=not offline, model=arguments.model)
    if offline:
        print("Documented Live candidates, reviewed 2026-10-12:")
        for name, description in DOCUMENTED_MODELS:
            selected = " [configured]" if name == config.model else ""
            print(f"  {name}: {description}{selected}")
        print(
            "Google pricing lists free-tier usage; project access, quotas, "
            "and billing are unverified. No API request made."
        )
        return
    control = StreamControl(signals)
    if arguments.ai_command in ("speak-test", "talk"):
        asyncio.run(voice_operation(arguments, config, control))
        return
    operation = (
        remote_models(config) if arguments.ai_command == "models"
        else connect_test(config)
    )
    asyncio.run(guarded(operation, control, config.connect_timeout + 1))
