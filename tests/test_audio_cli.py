"""Run installed CLI against synthetic utilities, never physical audio."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

FAKE_TOOL = '''
import json
import os
import sys
import time
import wave
from pathlib import Path

if "-l" in sys.argv:
    print("card 7: Fixture [Jabra Synthetic], device 0: USB Audio [USB Audio]")
    sys.exit(0)
name = Path(sys.argv[0]).name
Path(os.environ["FAKE_AUDIO_DIR"], name + ".json").write_text(
    json.dumps(sys.argv)
)
if os.environ.get("FAKE_AUDIO_SLEEP"):
    Path(os.environ["FAKE_AUDIO_DIR"], "child.pid").write_text(
        str(os.getpid())
    )
    time.sleep(60)
if name == "arecord":
    frames = int(sys.argv[sys.argv.index("-s") + 1])
    with wave.open(sys.argv[-1], "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\\0\\0" * frames)
'''


class AudioCliTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        for name in ("arecord", "aplay"):
            path = self.bin / name
            path.write_text(
                f"#!{sys.executable}\n" + FAKE_TOOL, encoding="utf-8"
            )
            path.chmod(0o700)
        self.environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith("JARVIS_") and key != "PYTHONPATH"
        }
        self.environment.update(
            PATH=str(self.bin), FAKE_AUDIO_DIR=str(self.directory),
            PYTHONIOENCODING="utf-8",
        )

    def invoke(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "jarvis", *arguments],
            capture_output=True, text=True, env=self.environment, timeout=5,
        )

    def test_record_play_and_loopback_without_hardware(self) -> None:
        path = self.directory / "sample.wav"
        for arguments in (
            ("audio", "record", "--duration", "1", "--output", str(path)),
            ("audio", "play", str(path)),
            ("audio", "loopback", "--duration", "1"),
        ):
            with self.subTest(arguments=arguments):
                result = self.invoke(*arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads((self.directory / "aplay.json").read_text())
        loopback_path = Path(args[-1])
        self.assertFalse(loopback_path.exists())
        self.assertFalse(loopback_path.parent.exists())

    def test_environment_selection_and_cli_override(self) -> None:
        self.environment["JARVIS_AUDIO_INPUT"] = "missing"
        output = self.directory / "sample.wav"
        result = self.invoke("audio", "record", "--output", str(output))
        self.assertEqual(result.returncode, 1)
        self.assertFalse(output.exists())
        result = self.invoke(
            "audio", "record", "--output", str(output),
            "--duration", "1", "--device", "Fixture:0",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads((self.directory / "arecord.json").read_text())
        self.assertEqual(
            args[args.index("-D") + 1], "plughw:CARD=Fixture,DEV=0"
        )

    def test_invalid_audio_settings_do_not_break_core_or_help(self) -> None:
        self.environment.update(
            JARVIS_AUDIO_INPUT="missing", JARVIS_AUDIO_OUTPUT="missing"
        )
        for arguments in (("status",), ("demo",), ("audio", "--help")):
            result = self.invoke(*arguments)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_utilities_do_not_break_core(self) -> None:
        self.environment["PATH"] = str(self.directory / "absent")
        for command in ("status", "demo"):
            self.assertEqual(self.invoke(command).returncode, 0)
        result = self.invoke("audio", "devices")
        self.assertEqual(result.returncode, 1)
        self.assertIn("alsa-utils", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_invalid_duration_is_usage_error_without_capture(self) -> None:
        for value in ("0", "31", "-1", "1.5", "nan"):
            with self.subTest(value=value):
                result = self.invoke(
                    "audio", "record", "--duration", value,
                    "--output", str(self.directory / "sample.wav"),
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn("duration", result.stderr)
        self.assertFalse((self.directory / "arecord.json").exists())

    @unittest.skipUnless(os.name == "posix", "POSIX audio cancellation")
    def test_real_signals_cancel_audio_and_reap_utility(self) -> None:
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signum=signum):
                self.assert_cancelled(signum)

    def assert_cancelled(self, signum: signal.Signals) -> None:
        self.environment["FAKE_AUDIO_SLEEP"] = "1"
        pid_file = self.directory / "child.pid"
        pid_file.unlink(missing_ok=True)
        output = self.directory / "cancelled.wav"
        process = subprocess.Popen(
            [sys.executable, "-m", "jarvis", "audio", "record",
             "--duration", "1", "--output", str(output)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=self.environment,
        )
        child_pid = None
        try:
            deadline = time.monotonic() + 5
            while not pid_file.exists() and time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                time.sleep(0.01)
            self.assertTrue(
                pid_file.exists(), "synthetic utility did not start"
            )

            child_pid = int(pid_file.read_text())
            process.send_signal(signum)
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 128 + signum, stderr.decode())
            self.assertEqual(stderr.count(b"running -> stopped"), 1)
            self.assertNotIn(b"-> failed", stderr)
            self.assertNotIn(b"Traceback", stdout + stderr)
            self.assertFalse(output.exists())
            self.assertEqual(list(self.directory.glob(".jarvis-audio-*")), [])
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
            if child_pid is not None:
                try:
                    os.killpg(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
