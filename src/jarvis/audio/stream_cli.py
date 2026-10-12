"""Opt-in, bounded PCM diagnostics; speaker output is always explicit."""

import argparse
import json
import shutil
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict

from jarvis.audio.backend import AlsaBackend
from jarvis.audio.control import validate_timeout
from jarvis.audio.devices import select_device
from jarvis.audio.errors import AudioTimeout
from jarvis.audio.frames import PCMFormat, PCMFrame
from jarvis.audio.session import StreamSession
from jarvis.audio.streaming import PCM_PIPE_BYTES
from jarvis.signals import ShutdownRequest


def integer_argument(low: int, high: int) -> Callable[[str], int]:
    def parse(value: str) -> int:
        try:
            result = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("must be an integer") from exc
        if not low <= result <= high:
            raise argparse.ArgumentTypeError(f"must be {low}–{high}")
        return result
    return parse


def timeout_argument(value: str) -> float:
    try:
        result = float(value)
        validate_timeout(result)
        return result
    except (ValueError, RuntimeError) as exc:
        raise argparse.ArgumentTypeError(
            "timeout must be positive, finite, and at most 60 seconds"
        ) from exc


def add_stream_commands(
    audio: argparse._SubParsersAction, duration_type: Callable[[str], int]
) -> None:
    for name, help_text in (
        ("stream-info", "show PCM format, selectors, and limits without I/O"),
        ("stream-test", "bounded PCM capture; optional sequential replay"),
        ("stream-play-test", "send bounded silence to playback; show metrics"),
    ):
        parser = audio.add_parser(name, help=help_text)
        parser.add_argument(
            "--sample-rate", type=integer_argument(8000, 192000), default=16000
        )
        parser.add_argument("--channels", type=int, choices=(1, 2), default=1)
        parser.add_argument("--frame-ms", type=integer_argument(10, 100),
                            default=20)
        parser.add_argument("--buffer-frames", type=integer_argument(1, 256),
                            default=10)
        parser.add_argument("--io-timeout", type=timeout_argument, default=2)
        parser.add_argument("--output-device", help="playback ID or 'jabra'")
        if name != "stream-play-test":
            parser.add_argument("--input-device", help="capture ID or 'jabra'")
        if name != "stream-info":
            parser.add_argument("--duration", type=duration_type, default=5)
        if name == "stream-test":
            parser.add_argument(
                "--replay", action="store_true",
                help="retain bounded PCM in memory, then replay after capture",
            )


def _remaining(deadline: float, limit: float = 60) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise AudioTimeout("Streaming test exceeded its overall deadline.")
    return min(remaining, limit)


def _report(label: str, session: StreamSession) -> None:
    print(f"{label} metrics (process/pipe timings, not acoustic latency):")
    print(json.dumps(asdict(session.metrics), indent=2, sort_keys=True))


def _play_frames(
    frames: Iterable[PCMFrame], arguments: argparse.Namespace, pcm: PCMFormat,
    selector: str, signals: ShutdownRequest | None,
) -> None:
    deadline = time.monotonic() + arguments.duration + 5
    session = StreamSession(
        pcm, capture_selector=None, playback_selector=selector,
        capacity=arguments.buffer_frames, io_timeout=arguments.io_timeout,
        signals=signals,
    )
    try:
        with session:
            for frame in frames:
                session.write_frame(
                    frame, _remaining(deadline, arguments.io_timeout)
                )
            session.finish_playback(_remaining(deadline))
    finally:
        session.close()
        _report("Playback", session)


def run_stream_command(
    arguments: argparse.Namespace, signals: ShutdownRequest | None,
    input_selector: str, output_selector: str,
) -> None:
    pcm = PCMFormat(arguments.sample_rate, arguments.channels,
                    arguments.frame_ms)
    if arguments.audio_command == "stream-info":
        print(
            f"Logical PCM: {pcm.sample_format}, {pcm.sample_rate} Hz, "
            f"{pcm.channels} channel(s); {pcm.frame_ms} ms, "
            f"{pcm.samples_per_frame} sample frames, {pcm.frame_bytes} bytes"
        )
        print(f"Capture selector: {input_selector!r}")
        print(f"Playback selector: {output_selector!r}")
        print(
            f"Buffer: {arguments.buffer_frames} frames "
            f"({arguments.buffer_frames * pcm.frame_bytes} PCM bytes); "
            "capture drops oldest and counts loss; "
            "playback blocks or times out"
        )
        print(
            f"I/O timeout: {arguments.io_timeout:g}s; tests: 1–30s, "
            "whole frames rounded down, "
            "overall deadline duration + 5s per stage"
        )
        print(
            f"PCM pipe request: {PCM_PIPE_BYTES} bytes; ALSA buffer request: "
            f"{pcm.frame_ms * 4} ms. "
            "Actual pipe capacity is reported in metrics."
        )
        for utility in ("arecord", "aplay"):
            availability = "available" if shutil.which(utility) else "missing"
            print(f"{utility}: {availability}")
        print(
            "Jabra playback policy: ALSA resampling to 48000 Hz; "
            "plughw channel negotiation. Other outputs use named plughw."
        )
        print(
            "No devices opened. "
            "Pipe timings do not establish acoustic latency."
        )
        return
    count = arguments.duration * 1000 // pcm.frame_ms
    audio_seconds = count * pcm.duration
    if arguments.audio_command == "stream-play-test":
        print(
            f"PCM silence playback: {audio_seconds:g}s on {output_selector!r}",
            flush=True,
        )
        silence = bytes(pcm.frame_bytes)
        _play_frames(
            (PCMFrame(pcm, silence, sequence) for sequence in range(count)),
            arguments, pcm, output_selector, signals,
        )
        return
    if arguments.replay:
        # Resolve output before opening a microphone; never open both together.
        select_device(AlsaBackend().devices("playback", signals),
                      output_selector, "playback")
    print(
        f"PCM capture: {audio_seconds:g}s from {input_selector!r}", flush=True
    )
    deadline = time.monotonic() + arguments.duration + 5
    session = StreamSession(
        pcm, capture_selector=input_selector,
        capacity=arguments.buffer_frames, io_timeout=arguments.io_timeout,
        signals=signals,
    )
    retained: list[PCMFrame] = []
    try:
        session.start(capture_frames=count)
        with session:
            while True:
                frame = session.read_frame(
                    _remaining(deadline, arguments.io_timeout)
                )
                if frame is None:
                    break
                if arguments.replay:
                    retained.append(frame)
    finally:
        session.close()
        _report("Capture", session)
    if arguments.replay:
        print(
            "Capture closed; replaying retained PCM sequentially.", flush=True
        )
        _play_frames(retained, arguments, pcm, output_selector, signals)
