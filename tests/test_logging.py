"""Console formatting, level filtering, file output, and handler ownership."""

import io
import logging
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from jarvis.config import AppConfig
from jarvis.logging import close_logging, configure_logging


class LoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        close_logging()
        self.addCleanup(close_logging)

    def test_console_format_and_level_filtering(self) -> None:
        stream = io.StringIO()
        with redirect_stderr(stream):
            configure_logging(AppConfig(log_level="WARNING"))
            logger = logging.getLogger("jarvis.tests")
            logger.info("filtered message")
            logger.warning("visible message")
        self.assertNotIn("filtered message", stream.getvalue())
        self.assertRegex(
            stream.getvalue(),
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4} "
            r"WARNING jarvis.tests: visible message",
        )

    def test_file_logging_creates_directory_and_writes_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory) / "nested" / "logs"
            with redirect_stderr(io.StringIO()):
                configure_logging(AppConfig(log_dir=log_dir))
                logging.getLogger("jarvis.tests").info("Hello café")
                close_logging()
            self.assertIn(
                "INFO jarvis.tests: Hello café",
                (log_dir / "jarvis.log").read_text(encoding="utf-8"),
            )

    def test_reconfiguration_preserves_root_without_duplicates(self) -> None:
        stream = io.StringIO()
        root_handlers = logging.getLogger().handlers[:]
        with redirect_stderr(stream):
            configure_logging(AppConfig())
            configure_logging(AppConfig())
            logging.getLogger("jarvis.tests").info("one message")
        self.assertEqual(stream.getvalue().count("one message"), 1)
        self.assertEqual(logging.getLogger().handlers, root_handlers)

    def test_invalid_file_destination_does_not_leak_handlers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            existing_file = Path(directory) / "file"
            existing_file.write_text("occupied", encoding="utf-8")
            with self.assertRaises(OSError):
                configure_logging(AppConfig(log_dir=existing_file))
            self.assertEqual(logging.getLogger("jarvis").handlers, [])
