"""Real child-process cleanup tests without audio tools or audio hardware."""

import signal
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from jarvis.audio.backend import run_command
from jarvis.audio.errors import AudioCancelled, AudioTimeout
from jarvis.signals import ShutdownRequest


class AudioProcessTests(unittest.TestCase):
    def test_command_returns_result_without_a_shell(self) -> None:
        result = run_command(
            [sys.executable, "-c", "print('synthetic result')"], 3
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "synthetic result")

    def test_timeout_terminates_and_reaps_real_child(self) -> None:
        original = subprocess.Popen
        children = []

        def spawn(*args: object, **kwargs: object) -> subprocess.Popen:
            child = original(*args, **kwargs)
            children.append(child)
            return child

        with patch("jarvis.audio.backend.subprocess.Popen", side_effect=spawn):
            with self.assertRaises(AudioTimeout):
                run_command(
                    [sys.executable, "-c", "import time; time.sleep(60)"], 0.2
                )
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())
        self.assertTrue(children[0].stdout.closed)
        self.assertTrue(children[0].stderr.closed)

    def test_pending_cancellation_does_not_spawn_a_child(self) -> None:
        with patch("jarvis.audio.backend.subprocess.Popen") as spawn:
            with self.assertRaises(AudioCancelled):
                run_command(["unused"], 3, ShutdownRequest(signal.SIGINT))
        spawn.assert_not_called()

    def test_cancellation_after_spawn_terminates_and_reaps_child(self) -> None:
        request = ShutdownRequest()
        child = Mock()
        child.pid = 12345
        child.poll.return_value = None

        def communicate(timeout: float) -> tuple[str, str]:
            if request.signum is None:
                request.record(signal.SIGTERM, None)
                raise subprocess.TimeoutExpired(["fake"], timeout)
            return "", ""

        child.communicate.side_effect = communicate
        with (
            patch("jarvis.audio.backend.subprocess.Popen", return_value=child),
            patch("jarvis.audio.backend._signal_process") as stop,
        ):
            with self.assertRaises(AudioCancelled) as raised:
                run_command(["fake"], 3, request)
        self.assertEqual(raised.exception.signum, signal.SIGTERM)
        stop.assert_called_once_with(child, signal.SIGTERM)
        self.assertEqual(child.communicate.call_count, 2)

    def test_cleanup_escalates_to_kill_when_terminate_is_ignored(self) -> None:
        child = Mock()
        child.pid = 12345
        child.poll.return_value = None
        child.communicate.side_effect = [
            KeyboardInterrupt(), subprocess.TimeoutExpired(["fake"], 1),
            ("", ""),
        ]
        with (
            patch("jarvis.audio.backend.subprocess.Popen", return_value=child),
            patch("jarvis.audio.backend._signal_process") as stop,
        ):
            with self.assertRaises(KeyboardInterrupt):
                run_command(["fake"], 3)
        self.assertEqual(
            [call.args[1] for call in stop.call_args_list],
            [signal.SIGTERM, signal.SIGKILL],
        )
        self.assertEqual(child.communicate.call_count, 3)
