from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from textual.coordinate import Coordinate
from textual.widgets import DataTable, Input

from ocdeck.app import OCDeckApp
from ocdeck.models import DashboardSnapshot, ProjectRecord
from tests.test_app import MultiProjectSource


def cursor_id(table: DataTable) -> str:
    return str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)


class ProjectRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_pasted_nested_directory_reaches_registration_with_existing_projects(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            parent = Path(base)
            child = parent / "reverse_engineer_the_genome"
            child.mkdir()
            app = OCDeckApp(MultiProjectSource(), auto_refresh=False)
            app._register_project_worker = mock.Mock()
            async with app.run_test(size=(140, 42)) as pilot:
                await pilot.pause()
                app._apply_snapshot(DashboardSnapshot(projects=(ProjectRecord(
                    id="parent", name="Agents Start", directory=str(parent), registered=True,
                ),)))
                await pilot.press("d")
                app.query_one("#project-register", Input).value = str(child)
                await pilot.press("enter")
                app._register_project_worker.assert_called_once_with(child, child.name)

                app._register_project_worker.reset_mock()
                with mock.patch.object(app, "notify") as notify:
                    app._register_project(str(parent))
                app._register_project_worker.assert_not_called()
                self.assertIn("Already registered", notify.call_args.args[0])

    async def test_refresh_preserves_hover_and_horizontal_scroll(self) -> None:
        source = MultiProjectSource()
        app = OCDeckApp(source, auto_refresh=False)
        async with app.run_test(size=(96, 40)) as pilot:
            await pilot.pause()
            await pilot.press("1")
            projects = app.query_one("#projects-table", DataTable)
            projects.focus()
            await pilot.press("down")
            await pilot.hover(projects, offset=(3, 2))
            projects.scroll_to(x=3, animate=False, immediate=True)
            await pilot.pause()
            hover = projects.hover_coordinate
            scroll_x = projects.scroll_x
            self.assertEqual(hover.row, 1)
            self.assertGreater(scroll_x, 0)
            snapshot = await source.collect()
            for _ in range(3):
                app._apply_snapshot(snapshot)
                self.assertEqual(projects.hover_coordinate, hover)
                self.assertEqual(projects.scroll_x, scroll_x)
                await pilot.pause()
                self.assertEqual(cursor_id(projects), "p2")
                self.assertEqual(app.selected_project_id, "p2")

    async def test_back_to_back_refreshes_cannot_steal_selection_from_project_sessions(self) -> None:
        source = MultiProjectSource()
        app = OCDeckApp(source, auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            await pilot.press("1")
            projects = app.query_one("#projects-table", DataTable)
            sessions = app.query_one("#sessions-table", DataTable)
            projects.focus()
            await pilot.press("down")
            self.assertEqual(app.selected_project_id, "p2")
            self.assertEqual(app.selected_session_id, "s3")
            snapshot = await source.collect()
            for _ in range(3):
                app._apply_snapshot(snapshot)
                app._apply_snapshot(snapshot)
                await pilot.pause()
                self.assertEqual(app.selected_project_id, "p2")
                self.assertEqual(app.selected_session_id, "s3")
                self.assertEqual(cursor_id(sessions), "s3")
                self.assertTrue(app.project_filter)
            await pilot.press("up")
            self.assertEqual(app.selected_project_id, "p1")

    async def test_reordering_and_new_projects_preserve_selected_identity(self) -> None:
        source = MultiProjectSource()
        app = OCDeckApp(source, auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            await pilot.press("1")
            projects = app.query_one("#projects-table", DataTable)
            projects.focus()
            await pilot.press("down")
            projects.hover_coordinate = Coordinate(1, 0)
            original = await source.collect()
            new_project = ProjectRecord(
                id="nested", name="reverse-engineer-genome",
                directory="/work/alpha/reverse_engineer_the_genome",
            )
            updated = replace(original, projects=(new_project, *reversed(original.projects)))
            app._apply_snapshot(updated)
            app._apply_snapshot(updated)
            await pilot.pause()
            self.assertEqual(cursor_id(projects), "p2")
            self.assertEqual(app.selected_project_id, "p2")
            self.assertEqual(app.selected_session_id, "s3")
            self.assertIn("nested", projects.rows)
            self.assertEqual(projects.get_cell("nested", "sessions"), "0")

    async def test_focused_agents_selection_survives_background_operations_render(self) -> None:
        app = OCDeckApp(MultiProjectSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            original = await app.source.collect()
            snapshot = replace(original, sessions=tuple(
                replace(session, status="busy") for session in original.sessions
            ))
            app._apply_snapshot(snapshot)
            await pilot.pause()
            agents = app.query_one("#agents-table", DataTable)
            agents.focus()
            agents.move_cursor(row=1)
            await pilot.pause()
            selected = cursor_id(agents)
            app._apply_snapshot(snapshot)
            app._apply_snapshot(snapshot)
            await pilot.pause()
            self.assertEqual(cursor_id(agents), selected)
            self.assertEqual(app.selected_session_id, selected)
