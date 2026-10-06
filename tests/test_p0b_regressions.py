"""Regression tests for council P0b component fixes.

G1: a project name from the catalog, registry, or a discovered directory
basename must never enter a tmux format string — tmux expands ``#(...)`` in
formats as a shell command on every status refresh (threat T13). The fix
stores the label in a ``@ocdeck_project`` session user option (whose value
tmux does not re-expand) and references it from ``status-left``.

G2: browser-enabled sessions on ANY backend take the browser launch path,
which never passes ``--auto`` (``--auto`` would auto-approve the ask rules
gating the signed-in browser).

G5: tmux targets use exact ``=name`` matching, and an existing managed
session whose panes do not match the expected terminal is refused rather
than attached (name squatting).
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from ocdeck.app import OCDeckApp
from ocdeck.models import SessionRecord
from ocdeck.tmux_header import apply_header

MALICIOUS_LABEL = "a#(touch /tmp/g1)#{pane_pid}"


def run_result(returncode: int = 0, stdout: bytes = b"") -> mock.Mock:
    result = mock.Mock()
    result.returncode = returncode
    result.stdout = stdout
    return result


class G1TmuxLabelTests(unittest.TestCase):
    def _launch(self, label):
        app = OCDeckApp(mock.Mock(), auto_refresh=False)
        app.notify = mock.Mock()
        app._request_refresh = mock.Mock()
        app._tmux_has_session = mock.Mock(return_value=False)
        app._tmux_attach = mock.Mock(return_value=True)
        with mock.patch("ocdeck.app.apply_header", apply_header):
            app._launch_tmux("oc2-ses_probe", Path("/tmp"), ["opencode"], accent="#ff5f00", label=label)

    def _capture(self) -> tuple[list[list[str]], object]:
        captured: list[list[str]] = []

        def fake_run(argv, *args, **kwargs):
            captured.append(list(argv))
            return run_result()

        runner = mock.Mock()
        runner.side_effect = fake_run
        return captured, runner

    def _option_value(self, captured: list[list[str]], option: str) -> str | None:
        for argv in captured:
            if argv[0:2] == ["tmux", "set-option"] and option in argv:
                return argv[argv.index(option) + 1]
        return None

    def test_label_never_enters_the_format_string(self) -> None:
        captured, runner = self._capture()
        with mock.patch("ocdeck.app.subprocess.run", runner):
            self._launch(MALICIOUS_LABEL)

        status_left = self._option_value(captured, "status-left")
        user_label = self._option_value(captured, "@ocdeck_project")
        self.assertIsNotNone(status_left, "status-left option was not set")
        self.assertIsNotNone(user_label, "@ocdeck_project option was not set")
        # The malicious label lives only in the user option, never in a format.
        self.assertNotIn(MALICIOUS_LABEL, status_left)
        self.assertNotIn("#(", status_left)
        self.assertEqual(user_label, MALICIOUS_LABEL)
        self.assertIn("#{@ocdeck_project}", status_left)

    def test_plain_labels_still_render(self) -> None:
        captured, runner = self._capture()
        with mock.patch("ocdeck.app.subprocess.run", runner):
            self._launch("energy sink")
        self.assertEqual(self._option_value(captured, "@ocdeck_project"), "energy sink")
        self.assertIn("#{@ocdeck_project}", self._option_value(captured, "status-left"))


class G2AutoOnBrowserSessionsTests(unittest.TestCase):
    def _app(self, backend: str, browser: bool) -> OCDeckApp:
        app = OCDeckApp.__new__(OCDeckApp)
        app.source = mock.Mock()
        app.source.backend = backend
        app._browser_terminal_ids = set()
        app._attach_live_terminal = mock.Mock(return_value=False)
        app._run_browser_session = mock.Mock(return_value=True)
        app._run_opencode = mock.Mock(return_value=True)
        app._session_directory = mock.Mock(return_value="/tmp/proj")
        app._session_tmux_name = mock.Mock(return_value="oc2-ses_x")
        app.notify = mock.Mock()
        session = SessionRecord(
            id="ses_x", title="t", project_id="p", directory="/tmp/proj",
            created_ms=0, updated_ms=0, browser_enabled=browser,
            instance_count=0, terminals=(),
        )
        return app, session

    def test_v2_browser_auto_needs_a_warning_and_a_second_press(self) -> None:
        # G2 as amended by the owner (2026-09-29): --auto on a browser session
        # auto-approves signed-in browser actions, so it is never silent.
        app, session = self._app("v2", browser=True)
        app.set_timer = mock.Mock()
        self.assertFalse(app._open_existing_session(session, auto=True))
        app._run_browser_session.assert_not_called()
        self.assertIn("signed-in browser tabs", app.notify.call_args.args[0])
        self.assertTrue(app._open_existing_session(session, auto=True))
        app._run_browser_session.assert_called_once()
        self.assertTrue(app._run_browser_session.call_args.kwargs["auto"])
        app._run_opencode.assert_not_called()

    def test_browser_open_without_auto_is_unchanged(self) -> None:
        for backend in ("v1", "v2"):
            app, session = self._app(backend, browser=True)
            self.assertTrue(app._open_existing_session(session))
            self.assertFalse(app._run_browser_session.call_args.kwargs["auto"])

    def test_plain_v2_session_still_allows_auto(self) -> None:
        app, session = self._app("v2", browser=False)
        self.assertTrue(app._open_existing_session(session, auto=True))
        app._run_opencode.assert_called_once()
        arguments = app._run_opencode.call_args[0][0]
        self.assertIn("--auto", arguments)


class G5TmuxTargetTests(unittest.TestCase):
    def test_has_session_uses_exact_target(self) -> None:
        captured: list[list[str]] = []

        def fake_run(argv, *args, **kwargs):
            captured.append(list(argv))
            return run_result(0)

        with mock.patch("ocdeck.app.subprocess.run", side_effect=fake_run):
            self.assertTrue(OCDeckApp._tmux_has_session(object(), "oc2-ses_x"))
        self.assertIn("=oc2-ses_x", captured[0])

    def test_session_matches_is_prefix_tolerant_but_refuses_others(self) -> None:
        def pane_runner(commands: bytes):
            def fake_run(argv, *args, **kwargs):
                self.assertEqual(argv[:4], ["tmux", "list-panes", "-t", "=s:"])
                result = run_result(0, commands)
                return result
            return fake_run

        app = object()
        with mock.patch("ocdeck.app.subprocess.run", side_effect=pane_runner(b"opencode")):
            self.assertTrue(OCDeckApp._tmux_session_matches(app, "s", ["opencode2", "run"]))
        with mock.patch("ocdeck.app.subprocess.run", side_effect=pane_runner(b"python3.14")):
            self.assertTrue(OCDeckApp._tmux_session_matches(app, "s", ["/x/.venv/bin/python", "-B"]))
        with mock.patch("ocdeck.app.subprocess.run", side_effect=pane_runner(b"bash")):
            self.assertFalse(OCDeckApp._tmux_session_matches(app, "s", ["opencode"]))
        # Uncertain results stay permissive (never brick opens).
        with mock.patch("ocdeck.app.subprocess.run", side_effect=pane_runner(b"")):
            self.assertTrue(OCDeckApp._tmux_session_matches(app, "s", ["opencode"]))

    def test_launch_refuses_to_attach_to_mismatched_existing_session(self) -> None:
        app = OCDeckApp.__new__(OCDeckApp)
        app.notify = mock.Mock()
        app._request_refresh = mock.Mock()
        app._tmux_attach = mock.Mock(return_value=True)
        app._tmux_has_session = mock.Mock(return_value=True)
        app._tmux_session_matches = mock.Mock(return_value=False)
        launched = app._launch_tmux(
            "oc2-ses_x", Path("/tmp"), ["opencode"], accent="#fff", label="p", title="t",
        )
        self.assertFalse(launched)
        app._tmux_attach.assert_not_called()

    def test_launch_attaches_when_identity_matches(self) -> None:
        app = OCDeckApp.__new__(OCDeckApp)
        app.notify = mock.Mock()
        app._request_refresh = mock.Mock()
        app._tmux_attach = mock.Mock(return_value=True)
        app.private = False
        app._tmux_has_session = mock.Mock(return_value=True)
        app._tmux_session_matches = mock.Mock(return_value=True)
        # No window shows this terminal yet (see test_window_reuse for the reuse path).
        app.inline_tmux = False
        app._raise_existing_window = mock.Mock(return_value=False)
        app._tmux_client_attached = mock.Mock(return_value=False)
        self.assertTrue(
            app._launch_tmux("oc2-ses_x", Path("/tmp"), ["opencode"], accent="#fff", label="p")
        )
        app._tmux_attach.assert_called_once()


if __name__ == "__main__":
    unittest.main()
