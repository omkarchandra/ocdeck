import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from textual.widgets import Button, DataTable, Static

from ocdeck.agents_layout import state_cell
from ocdeck.app import OCDeckApp
from ocdeck.recent_open import (
    RecentOpenHistory, default_recent_open_file, load_recent_open_sessions,
    save_recent_open_sessions,
)
from tests.test_relaunch import ReopenSource


class HistoryTests(unittest.TestCase):
    def test_partial_observations_and_restarts_preserve_history(self):
        with tempfile.TemporaryDirectory() as base:
            path = Path(base) / "recent.json"
            first = RecentOpenHistory(path)
            first.observe(["ses_alpha", "ses_beta"])
            first.observe([])
            first.observe(["ses_alpha"])
            restarted = RecentOpenHistory(path)
            self.assertEqual(restarted.ids, ["ses_alpha", "ses_beta"])
            restarted.observe([])
            self.assertEqual(load_recent_open_sessions(path), ["ses_alpha", "ses_beta"])

    def test_two_viewers_merge_their_observations(self):
        with tempfile.TemporaryDirectory() as base:
            path = Path(base) / "recent.json"
            first, second = RecentOpenHistory(path), RecentOpenHistory(path)
            first.observe(["ses_alpha"])
            second.observe(["ses_beta"])
            self.assertEqual(load_recent_open_sessions(path), ["ses_beta", "ses_alpha"])
            first.observe([])
            first.observe(["ses_alpha"])
            self.assertEqual(load_recent_open_sessions(path), ["ses_alpha", "ses_beta"])

    def test_history_scope_separates_backends_and_remote_servers(self):
        self.assertEqual(default_recent_open_file(), default_recent_open_file("v1", "http://127.0.0.1:4096"))
        self.assertNotEqual(default_recent_open_file("v1"), default_recent_open_file("v2"))
        self.assertNotEqual(default_recent_open_file("v2", "https://one.test"),
                            default_recent_open_file("v2", "https://two.test"))

    def test_failed_save_keeps_memory_and_retries_on_next_observation(self):
        with tempfile.TemporaryDirectory() as base:
            path = Path(base) / "recent.json"
            history = RecentOpenHistory(path)
            with mock.patch("ocdeck.recent_open.save_recent_open_sessions", return_value=None):
                history.observe(["ses_alpha"])
            self.assertEqual(history.ids, ["ses_alpha"])
            self.assertTrue(history.write_failed)
            history.observe(["ses_alpha"])
            self.assertFalse(history.write_failed)
            self.assertEqual(load_recent_open_sessions(path), ["ses_alpha"])


class RelaunchWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_from_agents_controls_uses_agents_selection(self):
        source = ReopenSource()
        source.open_ids = {"ses_alpha"}
        with tempfile.TemporaryDirectory() as base:
            path = Path(base) / "recent.json"
            save_recent_open_sessions(path, ["ses_beta"])
            app = OCDeckApp(source, auto_refresh=False, recent_open_file=path)
            app._run_opencode = mock.Mock(return_value=True)
            async with app.run_test(size=(110, 42)) as pilot:
                await pilot.pause()
                agents = app.query_one("#agents-table", DataTable)
                agents.focus()
                agents.move_cursor(row=agents.get_row_index("ses_beta"))
                await pilot.pause()
                app.query_one("#relaunch-agents", Button).focus()
                await pilot.press("o")
                app._run_opencode.assert_called_once_with(
                    ["/work/alpha", "--session", "ses_beta"], tmux_name="oc-ses_beta",
                    project_id="p1", title="Agent Beta",
                )

    async def test_activity_render_failure_is_visible_and_recovers_without_toast_spam(self):
        source = ReopenSource()
        source.collect_activity = mock.AsyncMock(side_effect=source.collect)
        app = OCDeckApp(source, auto_refresh=False)
        async with app.run_test(size=(110, 42)) as pilot:
            await pilot.pause()
            with mock.patch.object(app, "_render_agents", side_effect=ValueError("bad rows")), \
                    mock.patch.object(app, "notify") as notify:
                for _ in range(2):
                    app._request_activity_refresh()
                    await app.workers.wait_for_complete()
                    self.assertIn("DEGRADED", str(app.query_one("#brand", Static).render()))
                    self.assertIn("DEGRADED", str(app.query_one("#metric-connection", Static).render()))
                self.assertEqual(notify.call_count, 1)
            app._request_activity_refresh()
            await app.workers.wait_for_complete()
            self.assertIn("LIVE", str(app.query_one("#brand", Static).render()))

    async def test_busy_without_tui_and_idle_parent_of_busy_child_are_never_closed(self):
        source = ReopenSource()
        with tempfile.TemporaryDirectory() as base:
            path = Path(base) / "recent.json"
            save_recent_open_sessions(path, ["ses_alpha", "ses_beta"])
            app = OCDeckApp(source, auto_refresh=False, recent_open_file=path)
            async with app.run_test(size=(110, 42)) as pilot:
                await pilot.pause()
                snapshot = source.snapshot()
                sessions = tuple(replace(item, status="busy")
                                 if item.id in {"ses_beta", "ses_child"} else item
                                 for item in snapshot.sessions)
                app._apply_snapshot(replace(snapshot, sessions=sessions))
                await pilot.pause()
                table = app.query_one("#agents-table", DataTable)
                self.assertEqual(set(key.value for key in table.rows), {"ses_alpha", "ses_beta"})
                self.assertFalse(app._closed_agent_sessions())
                self.assertIn(state_cell("busy", inherited=True), str(table.get_row("ses_alpha")[0]))
                self.assertIn(state_cell("busy"), str(table.get_row("ses_beta")[0]))
                self.assertTrue(app.query_one("#relaunch-agents", Button).disabled)

    async def test_confirmation_is_bound_to_the_actual_sessions_and_launches_once(self):
        source = ReopenSource()
        with tempfile.TemporaryDirectory() as base:
            path = Path(base) / "recent.json"
            save_recent_open_sessions(path, ["ses_alpha", "ses_beta"])
            app = OCDeckApp(source, auto_refresh=False, recent_open_file=path)
            app._run_opencode = mock.Mock(return_value=True)
            async with app.run_test(size=(110, 42)) as pilot:
                await pilot.pause()
                await pilot.press("L")
                source.open_ids = {"ses_alpha"}
                app._apply_snapshot(source.snapshot())
                await pilot.press("L")
                app._run_opencode.assert_not_called()
                await pilot.press("L")
                app._run_opencode.assert_called_once()
                self.assertEqual(app._run_opencode.call_args.args[0], ["/work/alpha", "--session", "ses_beta"])
                await pilot.press("L", "L")
                self.assertEqual(app._run_opencode.call_count, 1)

    async def test_bulk_restore_uses_browser_connection_without_granting_or_forking(self):
        source = ReopenSource()
        with tempfile.TemporaryDirectory() as base:
            path = Path(base) / "recent.json"
            save_recent_open_sessions(path, ["ses_alpha"])
            app = OCDeckApp(source, auto_refresh=False, recent_open_file=path)
            app._run_browser_session = mock.Mock(return_value=True)
            app._run_opencode = mock.Mock(return_value=True)
            async with app.run_test(size=(110, 42)) as pilot:
                await pilot.pause()
                snapshot = source.snapshot()
                app._apply_snapshot(replace(snapshot, sessions=tuple(
                    replace(item, browser_enabled=True) if item.id == "ses_alpha" else item
                    for item in snapshot.sessions
                )))
                await pilot.click("#relaunch-agents")
                await pilot.click("#relaunch-agents")
                app._run_browser_session.assert_called_once_with(Path("/work/alpha"), "ses_alpha", "p1", "Agent Alpha", auto=False)
                app._run_opencode.assert_not_called()

    async def test_open_does_not_duplicate_a_known_live_unlinked_terminal(self):
        source = ReopenSource()
        source.open_ids = {"ses_alpha"}
        app = OCDeckApp(source, auto_refresh=False)
        app._run_opencode = mock.Mock(return_value=True)
        async with app.run_test(size=(110, 42)) as pilot:
            await pilot.pause()
            table = app.query_one("#agents-table", DataTable)
            table.focus()
            table.move_cursor(row=table.get_row_index("ses_alpha"))
            await pilot.press("o", "a")
            app._run_opencode.assert_not_called()

    async def test_expired_confirmation_requires_a_new_confirmed_press(self):
        source = ReopenSource()
        with tempfile.TemporaryDirectory() as base:
            path = Path(base) / "recent.json"
            save_recent_open_sessions(path, ["ses_alpha"])
            app = OCDeckApp(source, auto_refresh=False, recent_open_file=path)
            app._run_opencode = mock.Mock(return_value=True)
            async with app.run_test(size=(110, 42)) as pilot:
                await pilot.pause()
                await pilot.press("L")
                app._relaunch_confirm_until = 0
                await pilot.press("L")
                app._run_opencode.assert_not_called()
                await pilot.press("L")
                app._run_opencode.assert_called_once()

    async def test_v2_relaunch_preserves_backend_server_and_session_id(self):
        source = ReopenSource()
        source.backend = "v2"
        source.opencode_bin = "/usr/bin/opencode2"
        source.api_url = "https://server.example"
        with tempfile.TemporaryDirectory() as base:
            path = Path(base) / "recent.json"
            save_recent_open_sessions(path, ["ses_alpha"])
            app = OCDeckApp(source, auto_refresh=False, recent_open_file=path)
            app._ensure_launch_directory = lambda path: path
            app._launch_tmux = mock.Mock(return_value=True)
            async with app.run_test(size=(110, 42)) as pilot:
                await pilot.pause()
                await pilot.press("L", "L")
                self.assertEqual(app._launch_tmux.call_args.args, (
                    "oc2-ses_alpha", Path("/work/alpha"),
                    ["/usr/bin/opencode2", "--server", "https://server.example", "/work/alpha", "--session", "ses_alpha"],
                ))
