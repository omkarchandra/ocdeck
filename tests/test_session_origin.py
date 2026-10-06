"""Who opened a session: owner launches, verified agent signals, unknowns."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from textual.widgets import DataTable

from ocdeck.agents_layout import state_cell
from ocdeck.app import OCDeckApp
from ocdeck.harnesses import ClaudeHarness, CodexHarness
from ocdeck.models import DashboardSnapshot, ProjectRecord, SessionRecord, SystemMetrics
from ocdeck.recent_open import save_recent_open_sessions
from ocdeck.session_origin import (
    AGENT_MARKER,
    AGENT_VERDICT,
    OWNER_OPENED_LIMIT,
    OWNER_VERDICT,
    UNKNOWN_VERDICT,
    SessionOrigin,
    classify_session,
    default_owner_opened_file,
    load_owner_sessions,
    record_owner_launch,
    record_owner_session,
    refresh_owner_opened,
)
from tests.test_harnesses import claude_transcript, write_jsonl


def record(
    session_id: str,
    title: str,
    *,
    harness: str = "opencode",
    parent: str = "",
    launch_source: str = "",
    instance: int = 0,
    updated: int = 10,
    terminals: tuple[str, ...] = (),
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
        terminals=terminals,
        harness=harness,
        launch_source=launch_source,
    )


class ClassifyTests(unittest.TestCase):
    def test_verified_signals_and_owner_precedence(self):
        owner = record("ses_owner", "Owner work")
        self.assertEqual(
            classify_session(owner, set()),
            SessionOrigin(verdict=UNKNOWN_VERDICT, signal="no signal"),
        )
        subagent = record("ses_child", "Child", parent="ses_owner")
        self.assertEqual(classify_session(subagent, set()).verdict, AGENT_VERDICT)
        self.assertIn("parentID", classify_session(subagent, set()).signal)
        helper = record("ses_helper", "Helper", parent="", launch_source="")
        helper = replace(helper, agent_parent_id="ses_owner")
        self.assertEqual(classify_session(helper, set()).verdict, AGENT_VERDICT)
        self.assertIn("lineage", classify_session(helper, set()).signal)
        headless = record("claude:aaa", "Headless run", harness="claude", launch_source="sdk-cli")
        self.assertEqual(classify_session(headless, set()).verdict, AGENT_VERDICT)
        self.assertIn("sdk-cli", classify_session(headless, set()).signal)
        tui = record("claude:bbb", "TUI run", harness="claude", launch_source="cli")
        self.assertEqual(classify_session(tui, set()).verdict, OWNER_VERDICT)
        exec_codex = record("codex:aaa", "Exec run", harness="codex", launch_source="exec")
        self.assertEqual(classify_session(exec_codex, set()).verdict, AGENT_VERDICT)
        app_server = record("codex:bbb", "App server run", harness="codex", launch_source="appServer")
        self.assertEqual(classify_session(app_server, set()).verdict, AGENT_VERDICT)
        vscode_codex = record("codex:ccc", "VS Code run", harness="codex", launch_source="vscode")
        self.assertEqual(classify_session(vscode_codex, set()).verdict, OWNER_VERDICT)
        # An owner-recorded launch wins over every agent signal.
        self.assertEqual(
            classify_session(subagent, {"ses_child"}),
            SessionOrigin(verdict=OWNER_VERDICT, signal="owner-recorded launch"),
        )
        # A codex source OC Deck does not recognize stays unknown, not agent.
        self.assertEqual(
            classify_session(record("codex:ddd", "Odd run", harness="codex", launch_source="custom"), set()).verdict,
            UNKNOWN_VERDICT,
        )

    def test_owner_recorded_beats_a_headless_claude_entrypoint(self):
        headless = record("claude:aaa", "Headless run", harness="claude", launch_source="sdk-cli")
        self.assertEqual(classify_session(headless, {"claude:aaa"}).verdict, OWNER_VERDICT)


class OwnerOpenedStoreTests(unittest.TestCase):
    def test_record_load_bound_and_private_permissions(self):
        with tempfile.TemporaryDirectory() as base:
            path = Path(base) / "state" / "ocdeck" / "owner-opened.json"
            record_owner_session("ses_a", path)
            record_owner_session("claude:uuid-1", path)
            record_owner_session("ses_a", path)
            self.assertEqual(load_owner_sessions(path), {"ses_a", "claude:uuid-1"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["version"], 1)
            for index in range(OWNER_OPENED_LIMIT + 5):
                record_owner_session(f"ses_{index}", path)
            self.assertEqual(len(load_owner_sessions(path)), OWNER_OPENED_LIMIT)
            self.assertIn(f"ses_{OWNER_OPENED_LIMIT + 4}", load_owner_sessions(path))

    def test_corrupt_or_missing_state_loads_empty(self):
        with tempfile.TemporaryDirectory() as base:
            path = Path(base) / "owner.json"
            self.assertEqual(load_owner_sessions(path), set())
            path.write_text("not json", encoding="utf-8")
            self.assertEqual(load_owner_sessions(path), set())
            path.write_text(json.dumps({"version": 2, "sessions": ["ses_a"]}), encoding="utf-8")
            self.assertEqual(load_owner_sessions(path), set())
            path.write_text(json.dumps({"version": 1, "sessions": ["ses_a", 5, "nope!"]}), encoding="utf-8")
            self.assertEqual(load_owner_sessions(path), {"ses_a"})

    def test_pending_terminal_resolves_to_the_session_that_owns_it(self):
        with tempfile.TemporaryDirectory() as base:
            path = Path(base) / "owner.json"
            record_owner_launch("oc-new-1", "/work/alpha", path, now_ms=1_000)
            sessions = (
                record("ses_plain", "Plain", instance=1, terminals=("oc-plain",)),
                record("ses_new", "Owner new session", instance=1, terminals=("oc-new-1",)),
            )
            self.assertEqual(refresh_owner_opened(sessions, path, now_ms=2_000), {"ses_new"})
            self.assertEqual(load_owner_sessions(path), {"ses_new"})
            # Resolved launches never linger as pending work.
            self.assertEqual(refresh_owner_opened((), path, now_ms=3_000), {"ses_new"})

    def test_pending_launch_never_reclaims_an_already_claimed_session(self):
        with tempfile.TemporaryDirectory() as base:
            path = Path(base) / "owner.json"
            # An owner-recorded session is already claimed; a newer snapshot
            # entry for it must not swallow the pending launch's real session.
            record_owner_session("ses_done", path)
            record_owner_launch("oc-new-2", "/work/alpha", path, now_ms=1_000)
            claimed = replace(
                record("ses_done", "Already claimed", instance=1, terminals=("oc-new-1",)),
                created_ms=2_000,
            )
            fresh = replace(
                record("ses_new", "Owner new session", instance=1, terminals=("oc-new-2",)),
                created_ms=1_500,
            )
            sessions = (claimed, fresh)
            self.assertEqual(
                refresh_owner_opened(sessions, path, now_ms=2_500),
                {"ses_done", "ses_new"},
            )
            self.assertEqual(load_owner_sessions(path), {"ses_done", "ses_new"})

    def test_pending_opencode_launch_matches_by_directory_window(self):
        with tempfile.TemporaryDirectory() as base:
            path = Path(base) / "owner.json"
            record_owner_launch("oc-new-9", "/work/alpha", path, now_ms=1_000_000)
            sessions = (
                # A claude session in the folder is not the owner's OpenCode launch.
                replace(record("claude:aaa", "Foreign helper", harness="claude", updated=990_000),
                        created_ms=900_000),
                # An agent-opened OpenCode session is never claimed.
                replace(record("ses_sub", "Subagent", parent="ses_owner", updated=1_001_000),
                        created_ms=1_000_200),
                # The session the launched OpenCode CLI created, minutes later.
                replace(record("ses_new", "Owner new session", instance=1, updated=1_002_000),
                        created_ms=1_000_500),
                # An older OpenCode session in the folder predates the launch.
                replace(record("ses_old", "Older session", updated=999_000), created_ms=800_000),
            )
            self.assertEqual(refresh_owner_opened(sessions, path, now_ms=1_003_000), {"ses_new"})

    def test_expired_pending_launches_are_dropped(self):
        with tempfile.TemporaryDirectory() as base:
            path = Path(base) / "owner.json"
            record_owner_launch("oc-new-1", "/work/alpha", path, now_ms=1_000)
            sessions = (record("ses_new", "Owner new session", instance=1, terminals=("oc-new-1",)),)
            self.assertEqual(refresh_owner_opened(sessions, path, now_ms=1_000 + 10**9), set())
            self.assertEqual(refresh_owner_opened(sessions, path, now_ms=1_000 + 10**9), set())

    def test_refresh_without_pending_launches_never_writes(self):
        with tempfile.TemporaryDirectory() as base:
            path = Path(base) / "owner.json"
            record_owner_session("ses_a", path)
            before = path.read_bytes()
            stat_before = path.stat().st_mtime_ns
            time.sleep(0.01)
            sessions = (record("ses_a", "Owner work", instance=1),)
            self.assertEqual(refresh_owner_opened(sessions, path), {"ses_a"})
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(path.stat().st_mtime_ns, stat_before)

    def test_default_state_file_follows_xdg_state_home(self):
        base = os.environ.get("XDG_STATE_HOME", "")
        try:
            os.environ["XDG_STATE_HOME"] = "/tmp/ocdeck-origin-test-state"
            self.assertEqual(
                default_owner_opened_file(),
                Path("/tmp/ocdeck-origin-test-state/ocdeck/owner-opened.json"),
            )
        finally:
            if base:
                os.environ["XDG_STATE_HOME"] = base
            else:
                os.environ.pop("XDG_STATE_HOME", None)


class LaunchSourceTests(unittest.TestCase):
    def test_claude_transcript_entrypoint_reaches_the_session_record(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            tui = claude_transcript(root, "tui-1", "/work/alpha")
            headless = claude_transcript(root, "headless-1", "/work/alpha")
            with headless.open("r", encoding="utf-8") as handle:
                entries = handle.read().splitlines(True)
            # Real sdk-cli runs stamp every user entry, including the first.
            patched = [json.dumps({**json.loads(entry), "entrypoint": "sdk-cli"}) + "\n"
                       for entry in entries]
            headless.write_text("".join(patched), encoding="utf-8")
            with tui.open("r", encoding="utf-8") as handle:
                entries = handle.read().splitlines(True)
            patched = [json.dumps({**json.loads(entry), "entrypoint": "cli"}) + "\n"
                       for entry in entries[:1]] + entries[1:]
            tui.write_text("".join(patched), encoding="utf-8")
            sessions = {
                session.id: session
                for session in ClaudeHarness(root, "claude").collect(processes=[], tmux={})
            }
            self.assertEqual(sessions["claude:headless-1"].launch_source, "sdk-cli")
            self.assertEqual(sessions["claude:tui-1"].launch_source, "cli")

    def test_codex_rollout_source_reaches_the_session_record(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)

            def rollout(name: str, source: str) -> None:
                write_jsonl(root / "2026/09/26" / f"rollout-2026-09-26T10-00-00-{name}.jsonl", [
                    {"type": "session_meta", "timestamp": "2026-09-26T10:00:00Z",
                     "payload": {"id": name, "cwd": "/work/alpha", "source": source,
                                 "originator": "codex-tui", "timestamp": "2026-09-26T10:00:00Z"}},
                    {"type": "event_msg", "timestamp": "2026-09-26T10:00:02Z",
                     "payload": {"type": "user_message", "message": "port the parser"}},
                ])

            rollout("11111111-2222-3333-4444-555555555555", "appServer")
            rollout("99999999-8888-7777-6666-555555555555", "vscode")
            sessions = {
                session.id: session
                for session in CodexHarness(root, "codex").collect(processes=[], tmux={})
            }
            self.assertEqual(sessions["codex:11111111-2222-3333-4444-555555555555"].launch_source, "appServer")
            self.assertEqual(sessions["codex:99999999-8888-7777-6666-555555555555"].launch_source, "vscode")


class OriginSource:
    """ReopenSource-style feed with launch-source and parent signals."""

    opencode_bin = None

    def __init__(self) -> None:
        self.snap = DashboardSnapshot(
            sessions=(
                record("ses_owner", "Owner at the TUI", instance=1, updated=30),
                record("claude:tui", "Owner claude TUI", harness="claude",
                       launch_source="cli", instance=1, updated=28, terminals=("cc-tui",)),
                record("claude:helper", "Agent headless run", harness="claude",
                       launch_source="sdk-cli", instance=1, updated=26),
            ),
            projects=(ProjectRecord(id="p1", directory="/work/alpha", name="alpha",
                                    session_count=3, registered=True),),
            metrics=SystemMetrics(),
            connection="live",
            connection_detail="test",
        )

    async def collect(self) -> DashboardSnapshot:
        return self.snap


class AgentBoardTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_rows_are_marked_dimmed_and_ordered_after_owner_rows(self):
        app = OCDeckApp(OriginSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            app.action_show_tab("agents")
            await pilot.pause()
            table = app.query_one("#agents-table", DataTable)
            order = [str(key.value) for key in table.rows]
            self.assertEqual(order.index("claude:tui") < order.index("claude:helper"), True)
            self.assertEqual(order[-1], "claude:helper")
            helper_title = table.get_row("claude:helper")[2]
            self.assertIn(AGENT_MARKER, str(helper_title))
            self.assertEqual(helper_title.style, "dim #8ba4b5")
            owner_title = table.get_row("claude:tui")[2]
            self.assertNotIn(AGENT_MARKER, str(owner_title))
            self.assertEqual(owner_title.style, "")

    async def test_owner_recorded_session_renders_exactly_as_before(self):
        app = OCDeckApp(OriginSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            # The owner resumed this headless-launched session from OC Deck.
            record_owner_session("claude:helper", default_owner_opened_file())
            app._apply_snapshot(app.source.snap)
            await pilot.pause()
            app.action_show_tab("agents")
            await pilot.pause()
            table = app.query_one("#agents-table", DataTable)
            title = table.get_row("claude:helper")[2]
            self.assertNotIn(AGENT_MARKER, str(title))
            self.assertEqual(title.style, "")

    async def test_closed_agent_child_nests_under_its_visible_parent(self):
        source = OriginSource()
        parent = record("ses_parent", "Owner parent", instance=1, updated=40, terminals=("oc-parent",))
        parent = replace(parent, status="busy")
        child = record("claude:helper", "Finished helper", harness="claude",
                       launch_source="sdk-cli", updated=20)
        child = replace(child, agent_parent_id="ses_parent")
        source.snap = replace(source.snap, sessions=(parent, child))
        app = OCDeckApp(source, auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            app.action_show_tab("agents")
            await pilot.pause()
            table = app.query_one("#agents-table", DataTable)
            self.assertEqual([str(key.value) for key in table.rows], ["ses_parent"])
            self.assertIn("▸[1]", str(table.get_row("ses_parent")[2]))
            app.expanded_agent_ids.add("ses_parent")
            app._render_agents()
            await pilot.pause()
            self.assertEqual(
                [str(key.value) for key in table.rows], ["ses_parent", "claude:helper"]
            )
            child_title = table.get_row("claude:helper")[2]
            self.assertIn("└", str(child_title))
            self.assertIn(AGENT_MARKER, str(child_title))
            self.assertEqual(child_title.style, "dim #8ba4b5")
            self.assertIn(state_cell("closed"), str(table.get_row("claude:helper")[0]))
            self.assertTrue("claude:helper" in app.agent_closed_row_ids)

    async def test_closed_agent_row_without_a_parent_lists_after_owner_history(self):
        source = OriginSource()
        owner_closed = record("ses_history", "Owner closed", updated=30)
        agent_closed = record("claude:helper", "Agent closed", harness="claude",
                              launch_source="sdk-cli", updated=28)
        source.snap = replace(source.snap, sessions=(owner_closed, agent_closed))
        with tempfile.TemporaryDirectory() as base:
            recent = Path(base) / "recent.json"
            save_recent_open_sessions(recent, ["ses_history", "claude:helper"])
            app = OCDeckApp(source, auto_refresh=False, recent_open_file=recent)
            async with app.run_test(size=(140, 42)) as pilot:
                await pilot.pause()
                app.action_show_tab("agents")
                await pilot.pause()
                table = app.query_one("#agents-table", DataTable)
                order = [str(key.value) for key in table.rows]
                self.assertEqual(order, ["ses_history", "claude:helper"])
                title = table.get_row("claude:helper")[2]
                self.assertIn(AGENT_MARKER, str(title))
                self.assertEqual(title.style, "dim #8ba4b5")
                self.assertIn("previous session", str(table.get_row("claude:helper")[5]))


class OwnerRecordOnOpenTests(unittest.TestCase):
    """Peeking at a running agent session must not re-label it as the owner's."""

    def test_only_relaunching_a_closed_session_records_the_owner(self):
        from types import SimpleNamespace
        from ocdeck.app import OCDeckApp
        app = OCDeckApp.__new__(OCDeckApp)
        recorded = []
        app._record_owner_opened_session = recorded.append
        app._open_harness_session = lambda *args, **kwargs: True
        for instances, expected in ((1, []), (0, ["claude:x"])):
            recorded.clear()
            session = SimpleNamespace(id="claude:x", instance_count=instances, harness="claude")
            app._open_existing_session(session)
            self.assertEqual(recorded, expected, f"instance_count={instances}")
