"""Validate the version-controlled unit without installing or starting it."""

import shutil
import subprocess
import unittest
from pathlib import Path

UNIT = Path(__file__).resolve().parents[1] / "deploy/systemd/jarvis.service"
TARGET_PYTHON = Path("/home/user/voice-assistant/.venv/bin/python")


class SystemdTests(unittest.TestCase):
    @unittest.skipUnless(
        shutil.which("systemd-analyze") and TARGET_PYTHON.is_file(),
        "systemd-analyze and the target Pi interpreter are required",
    )
    def test_unit_syntax(self) -> None:
        result = subprocess.run(
            ["systemd-analyze", "verify", str(UNIT)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
