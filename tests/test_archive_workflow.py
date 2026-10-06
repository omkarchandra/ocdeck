"""The Shift+A archive workflow: OC Deck-only, reversible, never stopping anything."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from textual.widgets import DataTable

from ocdeck.app import OCDeckApp
from ocdeck.archive import default_archived_sessions_file, load_archived_sessions
from ocdeck.models import DashboardSnapshot, ProjectRecord, SessionRecord, SystemMetrics
from tests.test_harness_app import FakeHarnessSource


def record(session_id, title, *, parent="", instance=0, status="idle", updated=10):
    return SessionRecord(
        id=session_id,
        title=title,
        directory="/work/alpha",
        project_id="p1",
        created_ms=1,
        updated_ms=updated,
        instance_count=instance,
        parent_id=parent,
        status=status,
    )


def snapshot_with(sessions):
    return DashboardSnapshot(
        sessions=tuple(sessions),
        projects=(
            ProjectRecord(
                id="p1",
                directory="/work/alpha",
                name="alpha",
                session_count=len(sessions),
                instance_count=sum(session.instance_count for session in sessions),
                updated_ms=10,
                registered=True,
            ),
        ),
        metrics=SystemMetrics(memory_percent=41),
        connection="live",
    )


def row_ids(table: DataTable) -> list[str]:
    return [str(key.value) for key in table.rows]


def is_dimmed(cell) -> bool:
    return "dim" in cell.style or any("dim" in span.style for span in cell.spans)


class ArchiveWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # These tests write the default archive path: never the real one.
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        patcher = mock.patch.dict(os.environ, {"XDG_STATE_HOME": state.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.archived = default_archived_sessions_file()
        assert str(self.archived).startswith(state.name)

    def source(self, *sessions) -> FakeHarnessSource:
        source = FakeHarnessSource(Path("/nonexistent-fixture"))
        source.snap = snapshot_with(sessions)
        return source

    @staticmethod
    def select_session(app: OCDeckApp, session_id: str) -> None:
        table = app.query_one("#sessions-table", DataTable)
        table.focus()
        table.move_cursor(row=row_ids(table).index(session_id))

    async def test_first_press_alone_changes_nothing(self):
        app = OCDeckApp(
            self.source(record("ses_alpha", "Alpha planning", updated=20),
                        record("claude:beta", "Beta review")),
            auto_refresh=False,
        )
        async with app.run_test(size=(160, 42)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("1")
            await pilot.pause()
            self.select_session(app, "ses_alpha")
            with mock.patch.object(app, "notify") as notify:
                await pilot.press("A")
                await pilot.pause()
                notify.assert_called_once()
                self.assertEqual(
                    notify.call_args.args[0],
                    "Press Shift+A again to archive Alpha planning",
                )
                self.assertIn("timeout", notify.call_args.kwargs)
            self.assertFalse(self.archived.exists())
            self.assertEqual(
                row_ids(app.query_one("#sessions-table", DataTable)),
                ["ses_alpha", "claude:beta"],
            )

    async def test_archiving_keeps_the_cursor_in_place_not_at_the_top(self):
        app = OCDeckApp(
            self.source(record("ses_s1", "One", updated=40), record("ses_s2", "Two", updated=30),
                        record("ses_s3", "Three", updated=20), record("ses_s4", "Four", updated=10)),
            auto_refresh=False,
        )
        async with app.run_test(size=(160, 42)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("1")
            await pilot.pause()
            table = app.query_one("#sessions-table", DataTable)
            self.select_session(app, "ses_s3")
            with mock.patch.object(app, "notify"):
                await pilot.press("A", "A")
                await app.workers.wait_for_complete()
                await pilot.pause()
            self.assertEqual(row_ids(table), ["ses_s1", "ses_s2", "ses_s4"])
            # The row below moved up into the archived row's place.
            self.assertEqual(row_ids(table)[table.cursor_row], "ses_s4")
            self.select_session(app, "ses_s4")  # the last row: land on the new last row
            with mock.patch.object(app, "notify"):
                await pilot.press("A", "A")
                await app.workers.wait_for_complete()
                await pilot.pause()
            self.assertEqual(row_ids(table)[table.cursor_row], "ses_s2")

    async def test_shift_a_twice_archives_and_hides_the_row(self):
        app = OCDeckApp(
            self.source(record("ses_alpha", "Alpha planning", updated=20),
                        record("claude:beta", "Beta review")),
            auto_refresh=False,
        )
        async with app.run_test(size=(160, 42)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("1")
            await pilot.pause()
            table = app.query_one("#sessions-table", DataTable)
            self.select_session(app, "ses_alpha")
            with mock.patch.object(app, "notify") as notify:
                await pilot.press("A", "A")
                await app.workers.wait_for_complete()
                await pilot.pause()
                messages = [call.args[0] for call in notify.call_args_list]
                self.assertIn("Archived Alpha planning (Shift+U shows archived sessions)", messages)
            self.assertEqual(load_archived_sessions(self.archived), {"ses_alpha"})
            self.assertEqual(row_ids(table), ["claude:beta"])
            # The project counts drop with the hidden session.
            self.assertEqual(app.snapshot.projects[0].session_count, 1)
            self.assertEqual(app.snapshot.projects[0].instance_count, 0)
            # A refresh keeps it hidden.
            app.action_refresh_data()
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertEqual(row_ids(table), ["claude:beta"])
            self.assertEqual(load_archived_sessions(self.archived), {"ses_alpha"})

    async def test_archiving_a_parent_hides_its_nested_children(self):
        app = OCDeckApp(
            self.source(record("ses_parent", "Parent session", updated=30),
                        record("ses_child", "Helper child", parent="ses_parent", updated=20),
                        record("claude:solo", "Solo review")),
            auto_refresh=False,
        )
        async with app.run_test(size=(160, 42)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("1")
            await pilot.pause()
            table = app.query_one("#sessions-table", DataTable)
            self.select_session(app, "ses_parent")
            await pilot.press("A", "A")
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertEqual(load_archived_sessions(self.archived), {"ses_parent"})
            self.assertEqual(row_ids(table), ["claude:solo"])
            self.assertEqual(app.snapshot.projects[0].session_count, 1)

    async def test_archiving_a_live_session_keeps_it_visible_until_it_stops(self):
        app = OCDeckApp(
            self.source(record("ses_live", "Live build", instance=1, status="busy", updated=30),
                        record("claude:idle", "Idle review")),
            auto_refresh=False,
        )
        async with app.run_test(size=(160, 42)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("4")  # AGENTS: live rows
            await pilot.pause()
            table = app.query_one("#agents-table", DataTable)
            table.focus()
            table.move_cursor(row=row_ids(table).index("ses_live"))
            with mock.patch.object(app, "notify") as notify:
                await pilot.press("A")
                await pilot.pause()
                self.assertEqual(
                    notify.call_args.args[0],
                    "Press Shift+A again to archive Live build "
                    "(still running; it stays visible until it stops)",
                )
                await pilot.press("A")
                await app.workers.wait_for_complete()
                await pilot.pause()
                messages = [call.args[0] for call in notify.call_args_list]
                self.assertIn(
                    "Archived Live build; it is still running and stays visible until it "
                    "stops (Shift+U shows archived sessions)",
                    messages,
                )
            self.assertEqual(load_archived_sessions(self.archived), {"ses_live"})
            self.assertEqual(row_ids(table), ["ses_live"])  # the idle row was never listed here
            title = table.get_row("ses_live")[2]
            self.assertIn("[archived]", str(title))
            self.assertTrue(is_dimmed(title))

    async def test_shift_u_shows_archived_dimmed_and_shift_a_twice_unarchives(self):
        app = OCDeckApp(
            self.source(record("ses_alpha", "Alpha planning", updated=20),
                        record("claude:beta", "Beta review")),
            auto_refresh=False,
        )
        async with app.run_test(size=(160, 42)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("1")
            await pilot.pause()
            table = app.query_one("#sessions-table", DataTable)
            self.select_session(app, "ses_alpha")
            await pilot.press("A", "A")
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertEqual(row_ids(table), ["claude:beta"])
            # Shift+U lists archived rows again, dimmed with the marker.
            await pilot.press("U")
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertEqual(row_ids(table), ["ses_alpha", "claude:beta"])
            title = table.get_row("ses_alpha")[2]
            self.assertIn("[archived]", str(title))
            self.assertTrue(is_dimmed(title))
            self.assertEqual(app.snapshot.projects[0].session_count, 2)
            # Shift+A twice on the archived row unarchives it.
            self.select_session(app, "ses_alpha")
            with mock.patch.object(app, "notify") as notify:
                await pilot.press("A")
                await pilot.pause()
                notify.assert_called_once_with("Press Shift+A again to unarchive Alpha planning",
                                               timeout=6)
                await pilot.press("A")
                await app.workers.wait_for_complete()
                await pilot.pause()
                messages = [call.args[0] for call in notify.call_args_list]
                self.assertIn("Unarchived Alpha planning", messages)
            self.assertEqual(load_archived_sessions(self.archived), set())
            title = table.get_row("ses_alpha")[2]
            self.assertNotIn("[archived]", str(title))
            self.assertFalse(is_dimmed(title))
            # Hiding again: Shift+U toggles back, the row disappears once more.
            await pilot.press("U")
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertEqual(row_ids(table), ["ses_alpha", "claude:beta"])

    async def test_shift_a_in_alarms_still_routes_to_dismiss_all(self):
        app = OCDeckApp(self.source(record("ses_alpha", "Alpha planning")), auto_refresh=False)
        async with app.run_test(size=(160, 42)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("6")
            await pilot.pause()
            with mock.patch.object(app, "notify") as notify:
                await pilot.press("A")
                await pilot.pause()
                notify.assert_called_once_with("No alarms to dismiss")
            self.assertFalse(self.archived.exists())

    async def test_next_is_read_only_for_archiving(self):
        app = OCDeckApp(self.source(record("ses_alpha", "Alpha planning")), auto_refresh=False)
        async with app.run_test(size=(160, 42)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("5")
            await pilot.pause()
            with mock.patch.object(app, "notify") as notify:
                await pilot.press("A", "A")
                await pilot.pause()
                self.assertEqual(notify.call_count, 2)
                for call in notify.call_args_list:
                    self.assertEqual(call, mock.call("NEXT is read-only",
                                                     severity="warning", timeout=3))
            self.assertFalse(self.archived.exists())

    async def test_lowercase_a_still_opens_with_auto(self):
        app = OCDeckApp(
            self.source(record("ses_alpha", "Alpha planning", updated=20),
                        record("claude:beta", "Beta review")),
            auto_refresh=False,
        )
        async with app.run_test(size=(160, 42)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("1")
            await pilot.pause()
            self.select_session(app, "ses_alpha")
            with mock.patch.object(app, "_open_existing_session") as open_existing:
                await pilot.press("a")
                await pilot.press("a")  # a a would confirm a reopen, never archive
                await pilot.pause()
                self.assertEqual(open_existing.call_count, 2)
                for call in open_existing.call_args_list:
                    self.assertEqual(call.args[0].id, "ses_alpha")
                    self.assertTrue(call.kwargs.get("auto"))
            self.assertFalse(self.archived.exists())
            self.assertEqual(
                row_ids(app.query_one("#sessions-table", DataTable)),
                ["ses_alpha", "claude:beta"],
            )


if __name__ == "__main__":
    unittest.main()


class AgentsTabCursorTests(ArchiveWorkflowTests):
    async def test_archiving_in_the_agents_tab_keeps_the_cursor_in_place(self):
        app = OCDeckApp(
            self.source(record("ses_s1", "One", updated=40), record("ses_s2", "Two", updated=30),
                        record("ses_s3", "Three", updated=20), record("ses_s4", "Four", updated=10)),
            auto_refresh=False,
        )
        async with app.run_test(size=(160, 42)) as pilot:
            await app.workers.wait_for_complete()
            # Closed rows on the Agents tab are the remembered recently-open ones.
            remembered = mock.patch.object(type(app), "recent_open_sessions", new_callable=mock.PropertyMock,
                                           return_value=["ses_s1", "ses_s2", "ses_s3", "ses_s4"])
            remembered.start()
            self.addCleanup(remembered.stop)
            await pilot.press("4")
            await pilot.pause()
            app._render_agents()
            await pilot.pause()
            table = app.query_one("#agents-table", DataTable)
            table.focus()
            before = row_ids(table)
            index = before.index("ses_s3")
            table.move_cursor(row=index)
            await pilot.pause()
            with mock.patch.object(app, "notify"):
                await pilot.press("A", "A")
                await app.workers.wait_for_complete()
                await pilot.pause()
            after = row_ids(table)
            self.assertNotIn("ses_s3", after)
            self.assertEqual(table.cursor_row, min(index, len(after) - 1))
            self.assertNotEqual(table.cursor_row, 0)
