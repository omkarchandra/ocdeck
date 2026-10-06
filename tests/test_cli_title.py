import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ocdeck import cli


class TerminalTitleTests(unittest.TestCase):
    def test_title_escape_is_written_to_the_terminal(self):
        with tempfile.TemporaryDirectory() as base:
            fake_tty = Path(base) / "tty"
            fake_tty.write_bytes(b"")
            self.assertTrue(cli.set_terminal_title("OC Deck", str(fake_tty)))
            self.assertEqual(fake_tty.read_bytes(), b"\x1b]2;OC Deck\x07")

    def test_no_terminal_is_not_an_error(self):
        self.assertFalse(cli.set_terminal_title("OC Deck", "/nonexistent/tty"))

    def test_main_titles_the_tab_except_for_report_modes(self):
        for argv, titled in ((["--refresh", "5"], True), (["--once"], False), (["--destinations-json"], False)):
            with self.subTest(argv=argv), mock.patch.object(cli, "set_terminal_title") as title, \
                    mock.patch.object(cli, "acquire_instance_lock", return_value=(True, "")), \
                    mock.patch("ocdeck.app.main") as app_main:  # never the real deck lock
                cli.main(argv)
                self.assertEqual(title.called, titled)
                app_main.assert_called_once_with(argv)
                if titled:
                    title.assert_called_once_with("OC Deck")


if __name__ == "__main__":
    unittest.main()
