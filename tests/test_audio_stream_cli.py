"""Installed streaming CLI with synthetic tools and real OS cancellation."""

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
from pathlib import Path

directory = Path(os.environ["FAKE_STREAM_DIR"])
name = Path(sys.argv[0]).name
if "-l" in sys.argv:
    print("card 7: Fixture [Jabra Synthetic], device 0: USB Audio [USB Audio]")
    sys.exit(0)
(directory / (name + ".pid")).write_text(str(os.getpid()))
(directory / (name + ".args")).write_text(json.dumps(sys.argv))
mode = os.environ.get("FAKE_STREAM_MODE", "")
if mode == "fail":
    print("synthetic device disconnected", file=sys.stderr)
    sys.exit(3)
if mode == "stall":
    time.sleep(60)
if name == "arecord":
    rate = int(sys.argv[sys.argv.index("-r") + 1])
    channels = int(sys.argv[sys.argv.index("-c") + 1])
    period_us = int(sys.argv[sys.argv.index("-F") + 1])
    size = rate * channels * 2 * period_us // 1000000
    while True:
        os.write(1, bytes(17))
        os.write(1, bytes(size - 17))
        time.sleep(.002)
else:
    count = 0
    while True:
        data = os.read(0, 333)
        if not data:
            break
        count += len(data)
    (directory / "played-bytes").write_text(str(count))
'''


class StreamingCliTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.bin = self.directory / "bin"
        self.bin.mkdir()
        for name in ("arecord", "aplay"):
            path = self.bin / name
            path.write_text(f"#!{sys.executable}\n" + FAKE_TOOL,
                            encoding="utf-8")
            path.chmod(0o700)
        self.environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith("JARVIS_") and key != "PYTHONPATH"
        }
        self.environment.update(PATH=str(self.bin),
                                FAKE_STREAM_DIR=str(self.directory))

    def invoke(self, *arguments):
        return subprocess.run(
            [sys.executable, "-m", "jarvis", "audio", *arguments],
            capture_output=True, text=True, env=self.environment, timeout=5,
        )

    def test_info_is_hardware_free_and_preserves_selector_precedence(self):
        self.environment.update(JARVIS_AUDIO_INPUT="unattached",
                                JARVIS_AUDIO_OUTPUT="other")
        result = self.invoke("stream-info", "--input-device", "Fixture:0")
        self.assertEqual(result.returncode, 0, result.stderr)
        for text in ("640 bytes", "320 sample frames", "Fixture:0", "other",
                     "No devices opened", "acoustic latency"):
            self.assertIn(text, result.stdout)
        self.assertEqual(list(self.directory.glob("*.pid")), [])
        self.environment["PATH"] = str(self.directory / "absent")
        result = self.invoke("stream-info")
        self.assertEqual(result.returncode, 0)
        self.assertIn("missing", result.stdout)

    def test_capture_is_bounded_without_implicit_speaker_output(self):
        result = self.invoke("stream-test", "--duration", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        metrics = json.loads(result.stdout[result.stdout.index('{'):])
        self.assertEqual(metrics["capture"]["frames"], 50)
        self.assertEqual(metrics["capture"]["bytes"], 32000)
        buffer = metrics["capture_buffer"]
        self.assertEqual(buffer["consumed"] + buffer["dropped"], 50)
        self.assertLessEqual(buffer["high_water"], 10)
        self.assertEqual(metrics["state"], "stopped")
        self.assertIsNone(metrics["playback"])
        self.assertFalse((self.directory / "aplay.pid").exists())
        self.assertFalse(list(self.directory.rglob("*.wav")))

    def test_sequential_replay_and_silent_playback_are_explicit(self):
        result = self.invoke("stream-test", "--duration", "1", "--replay")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Capture closed; replaying", result.stdout)
        metrics, _ = json.JSONDecoder().raw_decode(
            result.stdout[result.stdout.index('{'):]
        )
        expected_bytes = metrics["capture_buffer"]["consumed"] * 640
        self.assertEqual(int((self.directory / "played-bytes").read_text()),
                         expected_bytes)
        args = json.loads((self.directory / "aplay.args").read_text())
        self.assertEqual(args[args.index("-t") + 1], "raw")
        self.assertIn("RATE=48000", args[args.index("-D") + 1])
        for pid_file in self.directory.glob("*.pid"):
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pid_file.read_text()), 0)
        (self.directory / "arecord.pid").unlink()
        result = self.invoke("stream-play-test", "--duration", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.directory / "arecord.pid").exists())

    def test_invalid_limits_and_missing_device_fail_without_capture(self):
        for arguments in (
            ("stream-test", "--duration", "31"),
            ("stream-test", "--buffer-frames", "0"),
            ("stream-test", "--frame-ms", "9"),
            ("stream-test", "--io-timeout", "nan"),
        ):
            with self.subTest(arguments=arguments):
                result = self.invoke(*arguments)
                self.assertEqual(result.returncode, 2)
        self.environment["JARVIS_AUDIO_OUTPUT"] = "missing"
        result = self.invoke("stream-test", "--duration", "1", "--replay")
        self.assertEqual(result.returncode, 1)
        self.assertIn("No playback device", result.stderr)
        self.assertFalse(list(self.directory.glob("*.pid")))

    def test_backend_failure_and_timeout_have_nonzero_exit_and_cleanup(self):
        for mode in ("fail", "stall"):
            self.environment["FAKE_STREAM_MODE"] = mode
            with self.subTest(mode=mode):
                result = self.invoke("stream-test", "--duration", "1",
                                     "--io-timeout", ".1")
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertIn('"failed": true', result.stdout)
                with self.assertRaises(ProcessLookupError):
                    child_pid = int(
                        (self.directory / "arecord.pid").read_text()
                    )
                    os.kill(child_pid, 0)

    @unittest.skipUnless(os.name == "posix", "POSIX streaming signals")
    def test_real_sigint_and_sigterm_cancel_capture_and_playback(self):
        self.environment["FAKE_STREAM_MODE"] = "stall"
        for command, utility in (("stream-test", "arecord"),
                                 ("stream-play-test", "aplay")):
            for signum in (signal.SIGINT, signal.SIGTERM):
                with self.subTest(command=command, signum=signum):
                    pid_file = self.directory / (utility + ".pid")
                    pid_file.unlink(missing_ok=True)
                    process = subprocess.Popen(
                        [sys.executable, "-m", "jarvis", "audio", command,
                         "--duration", "5"], stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, env=self.environment,
                    )
                    child_pid = None
                    try:
                        deadline = time.monotonic() + 3
                        while time.monotonic() < deadline:
                            if pid_file.exists() and pid_file.read_text():
                                child_pid = int(pid_file.read_text())
                                break
                            if process.poll() is not None:
                                break
                            time.sleep(.01)
                        self.assertIsNotNone(
                            child_pid, "fake utility not ready"
                        )
                        process.send_signal(signum)
                        stdout, stderr = process.communicate(timeout=5)
                        self.assertEqual(process.returncode, 128 + signum,
                                         stderr.decode())
                        self.assertEqual(
                            stderr.count(b"running -> stopped"), 1
                        )
                        self.assertNotIn(b"-> failed", stderr)
                        self.assertNotIn(b"Traceback", stdout + stderr)
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
