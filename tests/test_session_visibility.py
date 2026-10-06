from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from textual.widgets import Checkbox, DataTable, Input, Static

from ocdeck.app import OCDeckApp, SUBAGENT_STYLE
from ocdeck.models import DashboardSnapshot, ProjectRecord, parse_sessions


class SessionOriginTests(unittest.TestCase):
    def test_only_explicit_automation_metadata_and_native_parents_classify_sessions(self):
        cases = (
            ({}, ""),
            ({"title": "Research (@general subagent)", "agent": "general"}, ""),
            ({"parentID": "parent"}, "Subagent"),
            ({"parentID": "session"}, ""),
            ({"metadata": {"homeAgent": {"kind": "project-worker"}}}, "Worker"),
            ({"metadata": {"homeAgent": {"kind": "portfolio-research"}}}, "Reporter"),
            ({"metadata": {"homeAgent": {"kind": "orchestrator", "role": "monitor"}}}, "Monitor"),
            ({"metadata": {"managedBy": "home_agent.py", "role": "home_agent_monitor"}}, "Monitor"),
            ({"metadata": {"managedBy": "home_agent.py", "role": "home_agent"}}, "Monitor"),
            ({"metadata": {"homeAgent": {"kind": "orchestrator", "role": "voice-orchestration"}}}, ""),
            ({"metadata": {"homeAgent": {"kind": "orchestrator"}}}, ""),
            ({"metadata": {"voiceIntake": {"version": 1, "targetAgent": "jarvis"}}}, ""),
            ({"metadata": []}, ""),
            ({"metadata": {"homeAgent": []}}, ""),
            ({"metadata": {"homeAgent": {"kind": [], "role": []}}}, ""),
            ({"metadata": {"created_by": "parent"}}, ""),
            ({"parentID": "parent", "metadata": {"homeAgent": {"kind": "project-worker"}}}, "Subagent"),
        )
        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                session = parse_sessions([{
                    "id": "session", "title": "Main", "directory": "/work/project",
                    **overrides,
                }])[0]
                self.assertEqual(session.agent_session_kind, expected)
                self.assertEqual(session.parent_id, overrides.get("parentID", ""))

        session = parse_sessions(
            [{"id": "session", "directory": "/work/project"}],
            agent_parent_ids={"session": "parent"},
        )[0]
        self.assertEqual(session.agent_session_kind, "Subagent")


class SessionListSource:
    opencode_bin = None

    async def collect(self):
        rows = [
            {"id": "main", "title": "ocdeck_maintenance", "directory": "/work/ocdeck", "projectId": "deck"},
            {"id": "game", "title": "agents_game", "directory": "/home/user", "projectId": "home"},
            {"id": "child", "title": "Research rendering", "directory": "/work/ocdeck", "projectId": "deck", "parentID": "main"},
            {"id": "worker", "title": "Inspect source", "directory": "/work/ocdeck", "projectId": "deck", "metadata": {"homeAgent": {"kind": "project-worker"}}},
        ]
        return DashboardSnapshot(
            sessions=parse_sessions(rows),
            projects=(
                ProjectRecord(id="deck", name="OC Deck", directory="/work/ocdeck", session_count=3),
                ProjectRecord(id="home", name="user", directory="/home/user", session_count=1),
            ),
        )


class SessionListTests(unittest.IsolatedAsyncioTestCase):
    async def test_main_list_hides_agent_sessions_and_toggle_restores_labeled_history(self):
        app = OCDeckApp(SessionListSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            await pilot.press("1")
            table = app.query_one("#sessions-table", DataTable)
            projects = app.query_one("#projects-table", DataTable)
            self.assertEqual({key.value for key in table.rows}, {"main", "game"})
            self.assertEqual(len(app.snapshot.sessions), 4)
            self.assertEqual(projects.get_cell("deck", "sessions"), "1")
            self.assertIn("MAIN SESSIONS", str(app.query_one("#sessions-title", Static).render()))

            table.focus()
            await pilot.press("b")
            await pilot.pause()
            self.assertTrue(app.query_one("#include-agent-sessions", Checkbox).value)
            self.assertEqual({key.value for key in table.rows}, {"main", "game", "child", "worker"})
            self.assertEqual(projects.get_cell("deck", "sessions"), "3")
            self.assertTrue(table.get_cell("child", "title").plain.startswith("[Subagent]"))
            self.assertTrue(table.get_cell("worker", "title").plain.startswith("[Worker]"))
            self.assertEqual(table.get_cell("worker", "title").style, SUBAGENT_STYLE)
            app._apply_snapshot(await app.source.collect())
            self.assertIn("child", table.rows)
            await pilot.click("#include-agent-sessions")
            await pilot.pause()
            self.assertEqual({key.value for key in table.rows}, {"main", "game"})

    async def test_agents_game_is_restored_by_clear_filters_on_desktop_and_small_windows(self):
        for size in ((140, 42), (84, 32), (58, 26)):
            with self.subTest(size=size):
                app = OCDeckApp(SessionListSource(), auto_refresh=False)
                async with app.run_test(size=size) as pilot:
                    await pilot.pause()
                    await pilot.press("1")
                    app.selected_project_id = "deck"
                    app.project_filter = True
                    search = app.query_one("#session-search", Input)
                    search.value = "ma"
                    await pilot.pause()
                    table = app.query_one("#sessions-table", DataTable)
                    self.assertNotIn("game", table.rows)
                    filters = app.query_one("#session-filters", Static)
                    self.assertTrue(filters.display)
                    self.assertIn('Search: "ma"', str(filters.render()))
                    self.assertIn("Project: OC Deck first", str(filters.render()))
                    self.assertTrue(search.has_class("filtered"))
                    await pilot.click("#clear-session-filters")
                    await pilot.pause()
                    self.assertIn("game", table.rows)
                    self.assertEqual(search.value, "")
                    self.assertFalse(app.project_filter)
                    self.assertFalse(filters.display)
                    self.assertFalse(app.show_agent_sessions)
                    self.assertNotIn("child", table.rows)

    async def test_hidden_child_permission_remains_on_agents_board_and_jump_reveals_it(self):
        app = OCDeckApp(SessionListSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            snapshot = await app.source.collect()
            snapshot = replace(snapshot, sessions=tuple(
                replace(session, permission="bash npm test", permission_id="perm-child")
                if session.id == "child" else session
                for session in snapshot.sessions
            ))
            app._apply_snapshot(snapshot)
            self.assertNotIn("child", app.query_one("#sessions-table", DataTable).rows)
            self.assertIn("bash npm test", str(app.query_one("#attention", Static).render()))
            self.assertIn("main", app.query_one("#agents-table", DataTable).rows)
            self.assertEqual(app.agent_display_state_by_id["main"], "permission")
            app.query_one("#session-search", Input).value = "ma"
            await pilot.pause()
            app.action_focus_permission()
            await pilot.pause()
            table = app.query_one("#sessions-table", DataTable)
            self.assertIn("child", table.rows)
            self.assertEqual(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value, "child")
            self.assertEqual(app.selected_session_id, "child")
            self.assertEqual(app.search_term, "")
            self.assertTrue(app.show_agent_sessions)

    async def test_mobile_live_list_retains_agent_sessions(self):
        with tempfile.TemporaryDirectory() as base:
            app = OCDeckApp(SessionListSource(), auto_refresh=False, inline_tmux=True,
                            mobile_target_file=Path(base) / "target.json")
            async with app.run_test(size=(48, 26)) as pilot:
                await pilot.pause()
                snapshot = await app.source.collect()
                app._apply_snapshot(replace(snapshot, sessions=tuple(
                    replace(session, instance_count=1, terminals=(f"oc-{session.id}",))
                    for session in snapshot.sessions
                )))
                app.action_show_tab("overview")
                await pilot.pause()
                self.assertIn("child", app.query_one("#sessions-table", DataTable).rows)
                self.assertIn("worker", app.query_one("#sessions-table", DataTable).rows)
                self.assertFalse(app.query_one("#session-controls").display)
