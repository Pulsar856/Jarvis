"""Offline discovery fixtures; enumeration never opens a microphone."""

import argparse
import io
import subprocess
import unittest
from dataclasses import replace
from contextlib import redirect_stdout
from unittest.mock import patch

from jarvis.audio.backend import AlsaBackend, utility_path
from jarvis.audio.cli import add_audio_commands, run_audio
from jarvis.audio.devices import (
    native_capabilities, parse_devices, select_device,
)
from jarvis.audio.errors import (
    AudioOperationError, BackendUnavailable, DeviceNotFound,
)

CAPTURE = (
    "**** List of CAPTURE Hardware Devices ****\n"
    "card 7: Jabra [Jabra SPEAK 510 USB], device 0: USB Audio [USB Audio]\n"
)
PLAYBACK = (
    "card 0: HDMI [HDMI Output], device 0: HDMI Audio [HDMI Audio]\n"
    "card 7: Jabra [Jabra SPEAK 510 USB], device 0: USB Audio [USB Audio]\n"
)
NATIVE = (
    "Playback:\n  Format: S16_LE\n  Channels: 2\n  Rates: 8000, 16000\n"
    "Capture:\n  Format: S16_LE\n  Channels: 1\n  Rates: 16000\n"
)


class DeviceTests(unittest.TestCase):
    def test_capture_listing_returns_named_hardware(self) -> None:
        devices = parse_devices(CAPTURE, "capture")
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0].direction, "capture")
        self.assertEqual(devices[0].identifier, "Jabra:0")
        self.assertEqual(devices[0].pcm, "plughw:CARD=Jabra,DEV=0")
        self.assertTrue(devices[0].is_jabra)

    def test_playback_listing_distinguishes_outputs(self) -> None:
        devices = parse_devices(PLAYBACK, "playback")
        self.assertEqual(len(devices), 2)
        self.assertEqual(devices[0].identifier, "HDMI:0")
        self.assertFalse(devices[0].is_jabra)
        self.assertEqual(
            select_device(devices, "jabra", "playback").identifier, "Jabra:0"
        )

    def test_numeric_card_changes_do_not_change_identifier(self) -> None:
        first = parse_devices(CAPTURE, "capture")[0]
        moved = parse_devices(
            CAPTURE.replace("card 7:", "card 3:"), "capture"
        )[0]
        self.assertEqual(first.identifier, moved.identifier)
        self.assertEqual(first.pcm, moved.pcm)

    def test_explicit_selection(self) -> None:
        device = select_device(
            parse_devices(PLAYBACK, "playback"), "HDMI:0", "playback"
        )
        self.assertEqual(device.card_name, "HDMI Output")

    def test_missing_jabra_does_not_fall_back(self) -> None:
        devices = parse_devices(CAPTURE.replace("Jabra", "Other"), "capture")
        with self.assertRaises(DeviceNotFound):
            select_device(devices, "jabra", "capture")

    def test_ambiguous_jabra_requires_explicit_selection(self) -> None:
        text = CAPTURE + CAPTURE.replace("7:", "8:").replace(
            "Jabra [", "Jabra2 ["
        )
        with self.assertRaisesRegex(DeviceNotFound, "Multiple"):
            select_device(parse_devices(text, "capture"), "jabra", "capture")

    def test_native_metadata_keeps_directions_separate(self) -> None:
        capture = native_capabilities(NATIVE, "capture")
        self.assertIn("Channels: 1", capture)
        self.assertNotIn("Channels: 2", capture)
        self.assertIn(
            "Rates: 8000, 16000", native_capabilities(NATIVE, "playback")
        )

    def test_persistent_usb_id_and_alsa_alias_are_selectable(self) -> None:
        device = replace(
            parse_devices(CAPTURE, "capture")[0],
            persistent_id="usb-jabra-serial",
        )
        self.assertEqual(device.identifier, "usb-jabra-serial:0")
        self.assertEqual(device.pcm, "plughw:CARD=Jabra,DEV=0")
        for selector in ("usb-jabra-serial:0", "Jabra:0", "jabra"):
            self.assertIs(
                select_device((device,), selector, "capture"), device
            )

    def test_backend_parses_listings_without_accessing_hardware(self) -> None:
        with (
            patch("jarvis.audio.backend.utility_path", return_value="arecord"),
            patch("jarvis.audio.backend.run_command") as run,
            patch("pathlib.Path.read_text", return_value=NATIVE),
        ):
            run.return_value = subprocess.CompletedProcess([], 0, CAPTURE, "")
            devices = AlsaBackend().devices("capture")
        self.assertEqual(len(devices), 1)
        self.assertIn("Rates: 16000", devices[0].native_capabilities)
        self.assertEqual(run.call_args.args[:2], (["arecord", "-l"], 5))

    def test_no_soundcards_returns_empty_inventory(self) -> None:
        with (
            patch("jarvis.audio.backend.utility_path", return_value="arecord"),
            patch("jarvis.audio.backend.run_command") as run,
        ):
            run.return_value = subprocess.CompletedProcess(
                [], 1, "", "arecord: no soundcards found..."
            )
            self.assertEqual(AlsaBackend().devices("capture"), ())

    def test_discovery_permission_error_is_not_reported_as_empty(self) -> None:
        with (
            patch("jarvis.audio.backend.utility_path", return_value="arecord"),
            patch("jarvis.audio.backend.run_command") as run,
        ):
            run.return_value = subprocess.CompletedProcess(
                [], 1, "", "Permission denied"
            )
            with self.assertRaisesRegex(AudioOperationError, "Permission"):
                AlsaBackend().devices("capture")

    def test_missing_backend_utility_has_clear_error(self) -> None:
        with patch("jarvis.audio.backend.shutil.which", return_value=None):
            with self.assertRaisesRegex(BackendUnavailable, "alsa-utils"):
                utility_path("arecord")

    def test_audio_devices_parsing_and_display_without_hardware(self) -> None:
        parser = argparse.ArgumentParser()
        add_audio_commands(parser.add_subparsers(dest="command"))
        args = parser.parse_args(["audio", "devices"])
        output = io.StringIO()
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("jarvis.audio.cli.AlsaBackend") as backend,
            redirect_stdout(output),
        ):
            backend.return_value.devices.side_effect = (
                parse_devices(CAPTURE, "capture"),
                parse_devices(PLAYBACK, "playback"),
            )
            run_audio(args, None)
        for text in ("Capture", "Playback", "Jabra", "configured"):
            self.assertIn(text, output.getvalue())
