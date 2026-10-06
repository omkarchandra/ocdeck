"""Rendered layout, live headers, and read-only Sentinel navigation regressions."""
from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from textual.widgets import DataTable, Static

from ocdeck.app import OCDeckApp
from ocdeck.sentinel.alarms import alarm_artifact_path, build_records, write_alarms
from ocdeck.sentinel.health import read_report
from ocdeck.sentinel.rules import RuleFinding
from tests.test_harness_app import FakeHarnessSource, make_snapshot


def write_report(*, directory="/project", session_id="aaa", summary="Fixture finding", overflow=0):
    path = alarm_artifact_path()
    write_alarms(path, build_records([
        RuleFinding(rule="S7", severity="CRITICAL", harness="claude", session_id=session_id,
                    cwd=directory, summary=summary, criteria={}),
    ]), {
        "rulesAvailable": True, "priorChainOk": True, "overflow": overflow,
        "adapters": {"claude": "OBSERVED", "codex": "INACTIVE", "opencode": "OBSERVED"},
        "coverageNotes": [], "unsupportedSurfaces": [],
    })
    return path


class ExpansionUITests(unittest.IsolatedAsyncioTestCase):
    def source(self):
        source = FakeHarnessSource(Path("/nonexistent-fixture"))
        source.snap = make_snapshot("/project")
        return source

    async def test_runtime_really_fits_and_selection_survives_resizes(self):
        source = self.source()
        prompt = "The full prompt must survive even when the DETAIL column is hidden."
        source.snap = replace(source.snap, sessions=tuple(
            replace(session, last_prompt=prompt, title=session.title + "x" * 150)
            for session in source.snap.sessions
        ))
        app = OCDeckApp(source, auto_refresh=False)
        async with app.run_test(size=(180, 42)) as pilot:
            await pilot.pause()
            table = app.query_one("#agents-table", DataTable)
            app._restore_table_cursor(table, "claude:aaa")
            for width in (120, 80, 60, 50, 40, 180):
                await pilot.resize_terminal(width, 42)
                await pilot.pause()
                with self.subTest(width=width):
                    self.assertEqual(app._selected_row_id(table, ""), "claude:aaa")
                    self.assertLessEqual(table.virtual_size.width, table.scrollable_content_region.width)
                    self.assertEqual(table.max_scroll_x, 0)
                    self.assertTrue(all(not column.auto_width for column in table.columns.values()))
                    self.assertEqual(str(table.get_row("claude:aaa")[-1]),
                                     "CC OP5" if width < 160 else "Claude Code · claude-opus-5-5")
                    self.assertIn(prompt, str(app.query_one("#agent-focus", Static).visual))
                    self.assertEqual("DETAIL" in table.columns, app.agent_widths.detail > 0)

    async def test_refresh_headers_uses_live_metadata_and_private_labels(self):
        source = self.source()
        source.snap = replace(source.snap, sessions=(replace(
            source.snap.sessions[1], terminals=("cc-aaa", "personal"),
        ),))
        app = OCDeckApp(source, auto_refresh=False)
        with mock.patch("ocdeck.app.apply_header", return_value=True) as apply:
            async with app.run_test(size=(120, 42)) as pilot:
                await app.workers.wait_for_complete()
                self.assertEqual(apply.call_args.args, ("cc-aaa",))
                self.assertEqual(apply.call_args.kwargs["model"], "claude-opus-5-5")
                self.assertEqual(apply.call_args.kwargs["project"], "alpha")
                source.snap = replace(source.snap, sessions=(replace(source.snap.sessions[0], title="Renamed"),))
                app.action_refresh_data()
                await app.workers.wait_for_complete()
                self.assertEqual(apply.call_args.kwargs["title"], "Renamed")
                await pilot.press("p")
                await app.workers.wait_for_complete()
                self.assertEqual(apply.call_args.kwargs["title"], "Hidden session")
                self.assertEqual(apply.call_args.kwargs["project"], "Hidden project")
                self.assertEqual(apply.call_args.kwargs["model"], "")
                self.assertTrue(all(call.args == ("cc-aaa",) for call in apply.call_args_list))

    async def test_helper_activity_is_distinct_from_the_idle_parents_own_state(self):
        source = self.source()
        parent = replace(source.snap.sessions[1], status="idle", model="claude-sonnet-5")
        helper = replace(source.snap.sessions[0], agent_parent_id=parent.id, title="Helper [bold]name[/bold]\x1b[31m")
        source.snap = replace(source.snap, sessions=(parent, helper))
        app = OCDeckApp(source, auto_refresh=False)
        async with app.run_test(size=(120, 42)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("4")
            await pilot.pause()
            table = app.query_one("#agents-table", DataTable)
            focus = app.query_one("#agent-focus", Static)
            self.assertEqual(table.row_count, 1)
            self.assertTrue(str(table.get_row(parent.id)[0]).startswith("↳ RUN"))
            self.assertEqual(str(table.get_row(parent.id)[-1]), "CC SN5")
            self.assertIn("helper RUNNING:", str(table.get_row(parent.id)[5]))
            self.assertIn("IDLE (idle", str(focus.visual))
            self.assertIn("helper RUNNING:", str(focus.visual))
            self.assertIn("OpenCode · gpt-6-astra", str(focus.visual))
            self.assertNotIn("\x1b", str(focus.visual))
            # The distinction must survive when DETAIL is hidden.
            await pilot.resize_terminal(60, 42)
            await pilot.pause()
            self.assertNotIn("DETAIL", table.columns)
            self.assertTrue(str(table.get_row(parent.id)[0]).startswith("↳ RUN"))
            self.assertIn("helper RUNNING:", str(focus.visual))
            await pilot.press("p")
            self.assertNotIn("Helper", str(focus.visual))
            self.assertNotIn("Helper", " ".join(str(cell) for cell in table.get_row(parent.id)))
            await pilot.press("p")
            # Once the parent itself is active, the ordinary dot returns.
            source.snap = replace(source.snap, sessions=(replace(parent, status="busy"), helper))
            app.action_refresh_data()
            await app.workers.wait_for_complete()
            self.assertTrue(str(table.get_row(parent.id)[0]).startswith("● RUN"))
            self.assertNotIn("helper RUNNING:", str(focus.visual))

    async def test_alarms_global_literal_private_and_read_only(self):
        secret = "[bold]alarm-secret[/bold]\x1b[31m"
        path = write_report(directory="/unknown-project", summary=secret, overflow=2)
        original = path.read_bytes()
        app = OCDeckApp(self.source(), auto_refresh=False)
        async with app.run_test(size=(160, 42)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("6")
            await pilot.pause()
            table = app.query_one("#alarms-table", DataTable)
            self.assertEqual(app.query_one("#tabs").active, "alarms")
            self.assertIs(app.focused, table)
            self.assertEqual(table.row_count, 1)
            self.assertIn("Unassigned", str(table.get_row_at(0)[4]))
            summary = table.get_row_at(0)[5]
            self.assertIn("[bold]alarm-secret[/bold]", summary.plain)
            self.assertNotIn("\x1b", summary.plain)
            self.assertEqual(summary.spans, [])
            self.assertIn("2 omitted", str(app.query_one("#sentinel-status", Static).visual))
            with mock.patch.object(app, "_launch_tmux") as launch, \
                    mock.patch.object(app, "_stop_job_worker") as stop:
                await pilot.press("enter", "o", "a", "n", "N", "B", "x", "x", "y", "L", "L", "z", "z", "t")
                await pilot.pause()
                launch.assert_not_called()
                stop.assert_not_called()
                self.assertEqual(app.source.approvals, [])
                self.assertEqual(app.source.browser_calls, [])
                self.assertEqual(app.query_one("#tabs").active, "alarms")
            await pilot.press("p")
            for cell in table.get_row_at(0):
                self.assertNotIn("alarm-secret", str(cell))
                self.assertNotIn("unknown-project", str(cell))
            self.assertNotIn("alarm-secret", str(app.query_one("#alarm-detail", Static).visual))
            self.assertEqual(path.read_bytes(), original)

    async def test_enter_locates_exact_session_without_launching(self):
        write_report(directory="/project")
        app = OCDeckApp(self.source(), auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("6")
            await pilot.pause()
            with mock.patch.object(app, "_open_existing_session") as launch:
                await pilot.press("enter")
                await pilot.pause()
                launch.assert_not_called()
                self.assertEqual(app.query_one("#tabs").active, "overview")
                self.assertEqual(app._current_session().id, "claude:aaa")

    async def test_health_refresh_clears_bad_records_despite_busy_backend(self):
        path = write_report()
        app = OCDeckApp(self.source(), auto_refresh=False)
        async with app.run_test(size=(120, 42)) as pilot:
            await app.workers.wait_for_complete()
            app.refresh_in_progress = True
            payload = json.loads(path.read_text())
            payload["generatedAt"] = (datetime.now(timezone.utc) - timedelta(seconds=65)).isoformat()
            path.write_text(json.dumps(payload))
            await app._sentinel_refresh_worker().wait()
            self.assertEqual(app.sentinel_report.health.status, "OFFLINE")
            self.assertEqual(len(app.sentinel_report.records), 1)
            payload["records"][0]["summary"] = "altered without rechaining"
            path.write_text(json.dumps(payload))
            await app._sentinel_refresh_worker().wait()
            self.assertEqual(app.sentinel_report.health.status, "UNAVAILABLE")
            self.assertEqual(app.query_one("#alarms-table", DataTable).row_count, 0)
            self.assertNotIn("Fixture finding", str(app.query_one("#alarm-detail", Static).visual))
            path.unlink()
            await app._sentinel_refresh_worker().wait()
            self.assertEqual(app.sentinel_report.health.status, "OFFLINE")
            self.assertEqual(app.sentinel_report.health.alarm_count, 0)


def test_report_reads_health_and_records_from_one_artifact():
    path = write_report()
    from ocdeck.sentinel.alarms import load_alarms
    with mock.patch("ocdeck.sentinel.health.load_alarms", wraps=load_alarms) as load:
        report = read_report(path)
    load.assert_called_once_with(path)
    assert report.health.alarm_count == len(report.records) == 1
