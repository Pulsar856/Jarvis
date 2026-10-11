"""Exercise installed entry points; every subprocess has a bounded lifetime."""

import os
import selectors
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from jarvis import __version__


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environ = {
            key: value for key, value in os.environ.items()
            if not key.startswith("JARVIS_") and key != "PYTHONPATH"
        }
        self.environ["PYTHONIOENCODING"] = "utf-8"

    def invoke(
        self, *arguments: str, cwd: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "jarvis", *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=self.environ,
            cwd=cwd,
            timeout=5,
        )

    def test_help_works_even_with_invalid_configuration(self) -> None:
        self.environ["JARVIS_LOG_LEVEL"] = "invalid"
        result = self.invoke("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        for text in (f"Jarvis {__version__}", "status", "demo", "run"):
            self.assertIn(text, result.stdout)

    def test_no_arguments_display_identity_and_commands(self) -> None:
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Jarvis {__version__}", result.stdout)
        self.assertIn("{status,demo,run}", result.stdout)

    def test_version_matches_package(self) -> None:
        result = self.invoke("--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), __version__)

    def test_status_honors_config_without_printing_secrets(self) -> None:
        self.environ.update(
            JARVIS_APP_NAME="Offline Jarvis",
            JARVIS_LOG_LEVEL="debug",
            OPENAI_API_KEY="example-never-print-me",
        )
        result = self.invoke("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        for text in (
            f"Offline Jarvis {__version__}", "Log level: DEBUG", "Python:",
            "Platform:", "State: running", "Registered commands: hello",
            "Log directory: disabled", "External services: none required",
        ):
            self.assertIn(text, result.stdout)
        self.assertNotIn(
            "example-never-print-me", result.stdout + result.stderr
        )

    def test_demo_returns_deterministic_result(self) -> None:
        result = self.invoke("demo")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(), "Hello. Jarvis core is operational."
        )
        self.assertIn("running -> stopped", result.stderr)

    def test_editable_package_works_outside_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self.invoke("demo", cwd=directory)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("core is operational", result.stdout)

    def test_invalid_cli_command_is_usage_error(self) -> None:
        result = self.invoke("missing")
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid choice", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_invalid_configuration_exits_cleanly(self) -> None:
        self.environ["JARVIS_LOG_LEVEL"] = "example-sensitive-value"
        result = self.invoke("demo")
        self.assertEqual(result.returncode, 1)
        self.assertIn("JARVIS_LOG_LEVEL must be", result.stderr)
        self.assertNotIn("example-sensitive-value", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_invalid_log_destination_exits_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "file"
            path.write_text("occupied", encoding="utf-8")
            self.environ["JARVIS_LOG_DIR"] = str(path)
            result = self.invoke("demo")
        self.assertEqual(result.returncode, 1)
        self.assertIn("jarvis:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    @unittest.skipUnless(os.name == "posix", "SIGINT pipe test targets POSIX")
    def test_run_starts_and_ctrl_c_stops_cleanly(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-m", "jarvis", "run"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.environ,
        )
        try:
            self.assertIsNotNone(process.stdout)
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                self.assertTrue(
                    selector.select(timeout=5), "run did not become ready"
                )
                ready = os.read(process.stdout.fileno(), 4096)
            self.assertIn(b"Press Ctrl+C to stop.", ready)
            process.send_signal(signal.SIGINT)
            output, errors = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, errors.decode("utf-8"))
            self.assertIn(b"running -> stopped", errors)
            self.assertNotIn(b"Traceback", ready + output + errors)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
