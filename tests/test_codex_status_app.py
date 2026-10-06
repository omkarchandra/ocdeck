"""Codex approvals are visible and focusable; replies stay in Codex."""
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from textual.widgets import DataTable

from ocdeck.app import OCDeckApp
from ocdeck.codex_status import CodexRuntimeStatus, apply_statuses
from test_harness_app import FakeHarnessSource, make_snapshot


class CodexApprovalDisplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_approval_visible_focusable_and_cleared(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = FakeHarnessSource(root, enabled=("opencode", "codex"))
            snapshot = make_snapshot(directory)
            codex = replace(snapshot.sessions[1], id="codex:abc", harness="codex", model="gpt-6-astra")
            waiting = apply_statuses([codex], {"abc": CodexRuntimeStatus("active", ("waitingOnApproval",))})[0]
            source.snap = replace(snapshot, sessions=(snapshot.sessions[0], waiting))
            app = OCDeckApp(source, auto_refresh=False, recent_open_file=root / "recent.json")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                app._apply_snapshot(source.snap)
                app.action_show_tab("agents")
                await pilot.pause()
                table = app.query_one("#agents-table", DataTable)
                self.assertIn("PERM", str(table.get_row("codex:abc")[0]))
                self.assertEqual(app._agent_display_state(app.session_by_id["codex:abc"]), "permission")
                await pilot.press("g")
                await pilot.pause()
                self.assertEqual(app.selected_session_id, "codex:abc")
                with mock.patch.object(app, "notify") as notify:
                    await pilot.press("y")
                    await pilot.pause()
                self.assertIn("OpenCode-only", notify.call_args.args[0])
                self.assertEqual(source.approvals, [])
                running = apply_statuses([waiting], {"abc": CodexRuntimeStatus("active")})[0]
                source.snap = replace(snapshot, sessions=(snapshot.sessions[0], running))
                app._apply_snapshot(source.snap)
                await pilot.pause()
                self.assertEqual(app._agent_display_state(app.session_by_id["codex:abc"]), "busy")
                self.assertNotIn("PERM", str(table.get_row("codex:abc")[0]))
