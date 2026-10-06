from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from ocdeck.app import main, parse_args
from ocdeck.backend import desktop_config_home


class BackendSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = Path(self.temporary.name)
        environment = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.config)}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def save_backend(self, value: str) -> Path:
        directory = self.config / "ocdeck"
        directory.mkdir(mode=0o700, exist_ok=True)
        lock = directory / ".backend.lock"
        lock.write_text("")
        lock.chmod(0o600)
        selector = directory / "backend"
        selector.write_text(value)
        selector.chmod(0o600)
        return selector

    def test_plain_cli_uses_saved_selection_without_probing_installed_backends(self) -> None:
        for backend in ("v1", "v2"):
            with self.subTest(backend=backend):
                self.save_backend(f"{backend}\n")
                with mock.patch("ocdeck.app.DashboardSource") as source:
                    self.assertEqual(parse_args([]).backend, backend)
                source.assert_not_called()

    def test_explicit_cli_then_environment_override_saved_selection(self) -> None:
        self.save_backend("v1\n")
        with mock.patch.dict(os.environ, {"OCDECK_OPENCODE_BACKEND": "v2"}):
            self.assertEqual(parse_args([]).backend, "v2")
            self.assertEqual(parse_args(["--backend", "v1"]).backend, "v1")
        self.assertEqual(parse_args(["--backend", "v2"]).backend, "v2")

    def test_no_saved_selection_preserves_v2_default(self) -> None:
        self.assertEqual(parse_args([]).backend, "v2")

    def test_managed_opencode_profile_keeps_the_desktop_selection(self) -> None:
        home = self.config / "home"
        self.config = home / ".config"
        self.config.mkdir(parents=True)
        self.save_backend("v1\n")
        private = self.config / "ocdeck-v2-runtime"
        with mock.patch("pathlib.Path.home", return_value=home), mock.patch.dict(
            os.environ, {"XDG_CONFIG_HOME": str(private)}
        ):
            self.assertEqual(desktop_config_home(), self.config)
            self.assertEqual(parse_args([]).backend, "v1")
        custom = home / "custom-config"
        with mock.patch("pathlib.Path.home", return_value=home), mock.patch.dict(
            os.environ, {"XDG_CONFIG_HOME": str(custom)}
        ):
            self.assertEqual(desktop_config_home(), custom)

    def test_explicit_selector_path_is_honored_and_missing_override_is_an_error(self) -> None:
        selector = self.save_backend("v1\n")
        with mock.patch.dict(os.environ, {"OCDECK_BACKEND_FILE": str(selector)}):
            self.assertEqual(parse_args([]).backend, "v1")
        for override in ("", str(self.config / "missing")):
            with self.subTest(override=override), mock.patch.dict(
                os.environ, {"OCDECK_BACKEND_FILE": override}
            ), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                parse_args([])
            self.assertEqual(error.exception.code, 2)

    def test_invalid_selector_fails_before_backend_initialization(self) -> None:
        self.save_backend("xx\n")
        stderr = io.StringIO()
        with mock.patch("ocdeck.app.DashboardSource") as source:
            with redirect_stderr(stderr), self.assertRaises(SystemExit) as error:
                main(["--once"])
        self.assertEqual(error.exception.code, 2)
        self.assertIn("backend selection", stderr.getvalue())
        source.assert_not_called()
        self.assertEqual(parse_args(["--backend", "v1"]).backend, "v1")

    def test_public_or_symlinked_selection_is_not_silently_used(self) -> None:
        selector = self.save_backend("v1\n")
        selector.chmod(0o644)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_args([])
        selector.chmod(0o600)
        target = selector.with_name("real-backend")
        selector.rename(target)
        selector.symlink_to(target)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_args([])

    def test_saved_backend_reaches_source_in_plain_cli(self) -> None:
        self.save_backend("v1\n")
        with mock.patch("ocdeck.app.DashboardSource") as source, mock.patch(
            "ocdeck.app.read_live_opencode_panes", return_value=()
        ) as panes:
            with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as result:
                main(["--destinations-json"])
        self.assertEqual(result.exception.code, 0)
        self.assertEqual(source.call_args.kwargs["backend"], "v1")
        source.return_value.collect.assert_not_called()
        panes.assert_called_once()
