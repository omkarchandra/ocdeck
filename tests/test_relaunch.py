from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from textual.widgets import DataTable

from ocdeck.agents_layout import state_cell
from ocdeck.app import OCDeckApp
from ocdeck.models import (
    DashboardSnapshot,
    ProjectRecord,
    SessionRecord,
    SystemMetrics,
)
from ocdeck.recent_open import (
    MAX_RECENT_OPEN_SESSIONS,
    load_recent_open_sessions,
    save_recent_open_sessions,
)


class ReopenSource:
    opencode_bin = None

    def __init__(self) -> None:
        self.open_ids: set[str] = set()

    def snapshot(self) -> DashboardSnapshot:
        def record(
            session_id: str,
            title: str,
            *,
            parent: str = "",
            instance: int = 0,
            updated: int = 10,
        ) -> SessionRecord:
            return SessionRecord(
                id=session_id,
                title=title,
                directory="/work/alpha",
                project_id="p1",
                created_ms=1,
                updated_ms=updated,
                instance_count=instance,
                parent_id=parent,
            )

        return DashboardSnapshot(
            sessions=(
                record("ses_alpha", "Agent Alpha", instance=1 if "ses_alpha" in self.open_ids else 0),
                record(
                    "ses_beta",
                    "Agent Beta",
                    instance=1 if "ses_beta" in self.open_ids else 0,
                    updated=9,
                ),
                record("ses_child", "Alpha child", parent="ses_alpha", updated=8),
            ),
            projects=(
                ProjectRecord(
                    id="p1",
                    directory="/work/alpha",
                    name="alpha",
                    session_count=3,
                    registered=True,
                ),
            ),
            metrics=SystemMetrics(),
            connection="live",
            connection_detail="test",
        )

    async def collect(self) -> DashboardSnapshot:
        return self.snapshot()


class RecentOpenStoreTests(unittest.TestCase):
    def test_round_trip_validation_and_cap(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            path = Path(base) / "state" / "recent.json"
            self.assertEqual(load_recent_open_sessions(path), [])

            save_recent_open_sessions(path, ["ses_a", "not a session", "ses_a", "ses_b"])
            self.assertEqual(load_recent_open_sessions(path), ["ses_a", "ses_b"])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

            save_recent_open_sessions(
                path, [f"ses_{index}" for index in range(MAX_RECENT_OPEN_SESSIONS + 5)]
            )
            loaded = load_recent_open_sessions(path)
            self.assertEqual(len(loaded), MAX_RECENT_OPEN_SESSIONS)
            self.assertEqual(loaded[0], "ses_0")

            path.write_text("not json", encoding="utf-8")
            self.assertEqual(load_recent_open_sessions(path), [])
            path.write_text('{"version":2,"sessions":["ses_x"]}', encoding="utf-8")
            self.assertEqual(load_recent_open_sessions(path), [])


class RelaunchTests(unittest.IsolatedAsyncioTestCase):
    async def test_closed_rows_are_remembered_and_relaunch_the_same_session(self) -> None:
        source = ReopenSource()
        source.open_ids = {"ses_alpha"}
        with tempfile.TemporaryDirectory() as base:
            recent = Path(base) / "recent.json"
            app = OCDeckApp(source, auto_refresh=False, recent_open_file=recent)
            async with app.run_test(size=(140, 42)) as pilot:
                await pilot.pause()
                app._apply_snapshot(source.snapshot())
                await pilot.pause()
                self.assertEqual(load_recent_open_sessions(recent), ["ses_alpha"])

                agents = app.query_one("#agents-table", DataTable)
                self.assertIn("ses_alpha", agents.rows)
                self.assertNotIn("ses_child", load_recent_open_sessions(recent))

                source.open_ids = set()
                app._apply_snapshot(source.snapshot())
                await pilot.pause()
                self.assertIn(state_cell("closed"), str(agents.get_row("ses_alpha")[0]))
                self.assertIn("ses_alpha", load_recent_open_sessions(recent))

                agents.focus()
                agents.move_cursor(row=agents.get_row_index("ses_alpha"))
                await pilot.pause()
                launched: list[tuple[list[str], dict[str, object]]] = []
                app._run_opencode = lambda arguments, **kwargs: (
                    launched.append((list(arguments), kwargs)) or True
                )
                app.action_open_session()

        self.assertEqual(
            launched,
            [
                (
                    ["/work/alpha", "--session", "ses_alpha"],
                    {
                        "tmux_name": "oc-ses_alpha",
                        "project_id": "p1",
                        "title": "Agent Alpha",
                    },
                )
            ],
        )

    async def test_unlisted_history_is_retained_but_not_rendered(self) -> None:
        source = ReopenSource()
        with tempfile.TemporaryDirectory() as base:
            recent = Path(base) / "recent.json"
            save_recent_open_sessions(recent, ["ses_alpha", "ses_beta", "ses_gone"])
            app = OCDeckApp(source, auto_refresh=False, recent_open_file=recent)
            async with app.run_test(size=(140, 42)) as pilot:
                await pilot.pause()
                source.open_ids = {"ses_alpha"}
                app._apply_snapshot(source.snapshot())
                await pilot.pause()
                self.assertEqual(load_recent_open_sessions(recent), ["ses_alpha", "ses_beta", "ses_gone"])
                agents = app.query_one("#agents-table", DataTable)
                self.assertNotIn("ses_gone", agents.rows)
                self.assertIn("ses_beta", agents.rows)
                self.assertIn(state_cell("closed"), str(agents.get_row("ses_beta")[0]))
                self.assertNotIn(state_cell("closed"), str(agents.get_row("ses_alpha")[0]))

    async def test_shift_l_relaunches_all_previous_after_confirmation(self) -> None:
        source = ReopenSource()
        with tempfile.TemporaryDirectory() as base:
            recent = Path(base) / "recent.json"
            save_recent_open_sessions(recent, ["ses_beta", "ses_alpha"])
            app = OCDeckApp(source, auto_refresh=False, recent_open_file=recent)
            async with app.run_test(size=(140, 42)) as pilot:
                await pilot.pause()
                app._apply_snapshot(source.snapshot())
                app.action_show_tab("agents")
                await pilot.pause()
                launched: list[list[str]] = []
                app._run_opencode = lambda arguments, **kwargs: (
                    launched.append(list(arguments)) or True
                )
                with mock.patch.object(app, "notify") as notify:
                    app.action_relaunch_previous_sessions()
                self.assertEqual(launched, [])
                self.assertIn("Shift+L again", notify.call_args.args[0])

                app.action_relaunch_previous_sessions()
                self.assertEqual(
                    launched,
                    [
                        ["/work/alpha", "--session", "ses_beta"],
                        ["/work/alpha", "--session", "ses_alpha"],
                    ],
                )

    async def test_running_agents_are_not_relaunched(self) -> None:
        source = ReopenSource()
        source.open_ids = {"ses_alpha", "ses_beta"}
        with tempfile.TemporaryDirectory() as base:
            recent = Path(base) / "recent.json"
            save_recent_open_sessions(recent, ["ses_beta", "ses_alpha"])
            app = OCDeckApp(source, auto_refresh=False, recent_open_file=recent)
            async with app.run_test(size=(140, 42)) as pilot:
                await pilot.pause()
                app._apply_snapshot(source.snapshot())
                app.action_show_tab("agents")
                await pilot.pause()
                app._run_opencode = mock.Mock(return_value=True)
                with mock.patch.object(app, "notify") as notify:
                    app.action_relaunch_previous_sessions()
                app._run_opencode.assert_not_called()
                self.assertIn("No previous agent sessions", notify.call_args.args[0])

    async def test_shift_l_requires_the_agents_view(self) -> None:
        source = ReopenSource()
        with tempfile.TemporaryDirectory() as base:
            recent = Path(base) / "recent.json"
            save_recent_open_sessions(recent, ["ses_alpha"])
            app = OCDeckApp(source, auto_refresh=False, recent_open_file=recent)
            async with app.run_test(size=(140, 42)) as pilot:
                await pilot.pause()
                app._apply_snapshot(source.snapshot())
                app.action_show_tab("overview")
                await pilot.pause()
                app._run_opencode = mock.Mock(return_value=True)
                with mock.patch.object(app, "notify") as notify:
                    app.action_relaunch_previous_sessions()
                app._run_opencode.assert_not_called()
                self.assertIn("Open AGENTS (4)", notify.call_args.args[0])

    def test_shortcut_binding_is_registered(self) -> None:
        app = OCDeckApp(ReopenSource(), auto_refresh=False)
        self.assertTrue(
            any(
                binding.action == "relaunch_previous_sessions" for binding in app.BINDINGS
            )
        )

    async def test_keyboard_shortcut_fires_on_the_agents_view(self) -> None:
        source = ReopenSource()
        with tempfile.TemporaryDirectory() as base:
            recent = Path(base) / "recent.json"
            save_recent_open_sessions(recent, ["ses_alpha"])
            app = OCDeckApp(source, auto_refresh=False, recent_open_file=recent)
            async with app.run_test(size=(140, 42)) as pilot:
                await pilot.pause()
                app._apply_snapshot(source.snapshot())
                await pilot.pause()
                with mock.patch.object(app, "notify") as notify:
                    await pilot.press("L")
                self.assertIn("Shift+L again", notify.call_args.args[0])
