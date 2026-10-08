"""OC Deck multi-harness behaviour: runtime labels, tmux routing and OpenCode-only guards."""

from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from unittest import mock

from textual.widgets import DataTable, Static

from ocdeck.app import OCDeckApp, build_source, parse_args
from ocdeck.harnesses import (
    ClaudeHarness,
    MultiHarnessSource,
    model_code,
    runtime_label,
    split_session_key,
)
from ocdeck.models import DashboardSnapshot, ProjectRecord, SessionRecord, SystemMetrics
from ocdeck.recent_open import save_recent_open_sessions

CLAUDE_BINARY = "/bin/claude"
CLAUDE_ID = "claude:aaa"
CLAUDE_NATIVE = "aaa"
OPENCODE_ID = "ses_astra"
OPUS_MODEL = "claude-opus-5-5"
ASTRA_MODEL = "openai/gpt-6-astra#max"
PROJECT_ID = "p1"
NARROW_SIZE = (120, 42)
WIDE_SIZE = (180, 42)


class FakeHarnessSource:
    """Small MultiHarnessSource stand-in: an OpenCode session plus a real Claude adapter."""

    backend = "v2"
    opencode_bin = "/bin/opencode2"

    def __init__(
        self,
        root: Path,
        *,
        enabled: tuple[str, ...] = ("opencode", "claude"),
        launch: str = "opencode",
        adapters: dict[str, ClaudeHarness] | None = None,
    ) -> None:
        self.enabled_harnesses = enabled
        self.launch_harness = launch
        self._adapters = (
            {"claude": ClaudeHarness(root, CLAUDE_BINARY)} if adapters is None else adapters
        )
        self.snap = DashboardSnapshot(connection="live")
        self.approvals: list[tuple[str, str]] = []
        self.browser_calls: list[str] = []

    def adapter(self, harness: str) -> ClaudeHarness | None:
        return self._adapters.get(harness)

    async def collect(self) -> DashboardSnapshot:
        return self.snap

    async def collect_activity(self) -> DashboardSnapshot:
        return self.snap

    async def approve_permission(self, session_id: str, permission_id: str, reply: str = "once") -> str:
        self.approvals.append((session_id, permission_id))
        return ""

    async def create_browser_session(self, project: ProjectRecord) -> tuple[str, str]:
        self.browser_calls.append(f"new:{project.id}")
        return "ses_browser", ""

    async def enable_session_browser(self, session_id: str, project_id: str) -> str:
        self.browser_calls.append(f"enable:{session_id}")
        return ""


def make_snapshot(
    directory: str,
    *,
    claude_status: str = "busy",
    claude_instance: int = 1,
    claude_terminals: tuple[str, ...] = (),
    opencode_status: str = "busy",
    opencode_instance: int = 1,
) -> DashboardSnapshot:
    return DashboardSnapshot(
        sessions=(
            SessionRecord(
                id=OPENCODE_ID,
                title="Astra refactor",
                directory=directory,
                project_id=PROJECT_ID,
                created_ms=1_000,
                updated_ms=9_000,
                last_interaction_ms=9_000,
                status=opencode_status,
                instance_count=opencode_instance,
                model=ASTRA_MODEL,
            ),
            SessionRecord(
                id=CLAUDE_ID,
                title="Claude work",
                directory=directory,
                project_id=PROJECT_ID,
                created_ms=1_000,
                updated_ms=8_000,
                last_interaction_ms=8_000,
                status=claude_status,
                instance_count=claude_instance,
                terminals=claude_terminals,
                model=OPUS_MODEL,
                harness="claude",
            ),
        ),
        projects=(
            ProjectRecord(
                id=PROJECT_ID,
                directory=directory,
                name="alpha",
                session_count=2,
                instance_count=claude_instance + opencode_instance,
                updated_ms=9_000,
                registered=True,
            ),
        ),
        metrics=SystemMetrics(memory_percent=41),
        connection="live",
        connection_detail="OpenCode + Claude Code",
    )


def runtime_cell(table: DataTable, row_id: str) -> str:
    return str(table.get_row(row_id)[-1])


def row_index(table: DataTable, row_id: str) -> int:
    keys = [str(key.value) for key in table.rows]
    assert row_id in keys, f"{row_id} is not in the table: {keys}"
    return keys.index(row_id)


def focus_row(app: OCDeckApp, table: DataTable, row_id: str) -> None:
    table.focus()
    table.move_cursor(row=row_index(table, row_id))
    app.selected_session_id = row_id


class HarnessAppTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.workspace = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.recent = self.workspace / "recent.json"
        self.source = FakeHarnessSource(self.workspace / "claude-projects")

    def set_snapshot(self, **kwargs: object) -> DashboardSnapshot:
        self.source.snap = make_snapshot(str(self.workspace), **kwargs)  # type: ignore[arg-type]
        return self.source.snap

    @asynccontextmanager
    async def running(self, size: tuple[int, int] = NARROW_SIZE, **kwargs: object):
        """Boot the deck against the fake source with a known snapshot applied."""
        self.set_snapshot(**kwargs)
        app = OCDeckApp(self.source, auto_refresh=False, recent_open_file=self.recent)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            app._apply_snapshot(self.source.snap)
            await pilot.pause()
            yield app, pilot

    # --- 1. runtime column -------------------------------------------------
    async def test_runtime_column_shows_short_codes_at_120_columns(self) -> None:
        async with self.running(NARROW_SIZE) as (app, pilot):
            await pilot.press("4")
            await pilot.pause()
            self.assertEqual(app.query_one("#tabs").active, "agents")
            table = app.query_one("#agents-table", DataTable)
            self.assertEqual(str(list(table.columns.values())[-1].label), "RUNTIME")
            self.assertFalse(app.agent_runtime_full)
            self.assertEqual(runtime_cell(table, CLAUDE_ID), "CC OP5")
            self.assertEqual(runtime_cell(table, OPENCODE_ID), "OC AST")

    async def test_runtime_column_shows_full_names_at_180_columns(self) -> None:
        async with self.running(WIDE_SIZE) as (app, pilot):
            await pilot.press("4")
            await pilot.pause()
            self.assertEqual(app.query_one("#tabs").active, "agents")
            table = app.query_one("#agents-table", DataTable)
            self.assertTrue(app.agent_runtime_full)
            self.assertEqual(runtime_cell(table, CLAUDE_ID), "Claude Code · claude-opus-5-5")
            self.assertEqual(runtime_cell(table, OPENCODE_ID), "OpenCode · gpt-6-astra")

    async def test_detail_pane_names_the_harness_and_model(self) -> None:
        async with self.running() as (app, pilot):
            await pilot.press("1")
            await pilot.pause()
            table = app.query_one("#sessions-table", DataTable)
            focus_row(app, table, CLAUDE_ID)
            await pilot.pause()
            detail = str(app.query_one("#session-detail", Static).visual)
            self.assertIn("RUNTIME", detail)
            self.assertIn("Claude Code · claude-opus-5-5", detail)

    def test_runtime_block_sanitizes_what_a_transcript_reports(self) -> None:
        app = OCDeckApp(self.source, auto_refresh=False, recent_open_file=self.recent)
        sessions = make_snapshot(str(self.workspace)).sessions
        self.assertIn("Claude Code · claude-opus-5-5", app._runtime_block(sessions[1]))
        self.assertIn("OpenCode · gpt-6-astra", app._runtime_block(sessions[0]))
        markup = app._runtime_block(replace(sessions[1], model="[bold]claude-opus-5-5"))
        self.assertIn("Claude Code", markup)
        self.assertIn("\\[bold]", markup)  # markup from a transcript is never interpreted
        control = app._runtime_block(replace(sessions[1], model="claude\x1b]52;c;payload\x07opus"))
        self.assertNotIn("\x1b", control)
        self.assertIn("model not reported", app._runtime_block(replace(sessions[1], model="")))

    def test_runtime_label_and_model_code_helpers(self) -> None:
        self.assertEqual(model_code(OPUS_MODEL), "OP5")
        self.assertEqual(model_code(ASTRA_MODEL), "AST")
        self.assertEqual(runtime_label("claude", OPUS_MODEL, full=False), "CC OP5")
        self.assertEqual(runtime_label("opencode", ASTRA_MODEL, full=False), "OC AST")
        self.assertEqual(runtime_label("claude", OPUS_MODEL, full=True), "Claude Code · claude-opus-5-5")
        self.assertEqual(runtime_label("opencode", ASTRA_MODEL, full=True), "OpenCode · gpt-6-astra")
        self.assertEqual(split_session_key(CLAUDE_ID), ("claude", CLAUDE_NATIVE))
        self.assertEqual(split_session_key(OPENCODE_ID), ("opencode", OPENCODE_ID))

    # --- 2. reopen a closed Claude session ---------------------------------
    async def test_closed_claude_session_reopens_in_its_own_tmux_terminal(self) -> None:
        async with self.running(claude_status="idle", claude_instance=0) as (app, pilot):
            await pilot.press("1")
            await pilot.pause()
            table = app.query_one("#sessions-table", DataTable)
            focus_row(app, table, CLAUDE_ID)
            await pilot.pause()
            with (
                mock.patch.object(app, "_launch_tmux", return_value=True) as launch,
                mock.patch.object(app, "_run_opencode", return_value=True) as run_opencode,
            ):
                await pilot.press("o")
                await pilot.pause()
            launch.assert_called_once()
            run_opencode.assert_not_called()
            name, directory, command = launch.call_args.args[:3]
            self.assertEqual(name, "cc-aaa")
            self.assertEqual(Path(directory), self.workspace)
            self.assertEqual(command, [CLAUDE_BINARY, "--no-chrome", "--resume", CLAUDE_NATIVE])
            self.assertEqual(launch.call_args.kwargs["title"], "Claude work")

    # --- 3. never double-start a live transcript ---------------------------
    async def test_live_claude_transcript_is_never_relaunched(self) -> None:
        async with self.running(claude_status="idle", claude_instance=1) as (app, pilot):
            await pilot.press("4")
            await pilot.pause()
            table = app.query_one("#agents-table", DataTable)
            focus_row(app, table, CLAUDE_ID)
            await pilot.pause()
            with (
                mock.patch.object(app, "_launch_tmux", return_value=True) as launch,
                mock.patch.object(app, "_run_opencode", return_value=True) as run_opencode,
                mock.patch.object(app, "notify") as notify,
            ):
                await pilot.press("o")
                await pilot.pause()
            launch.assert_not_called()
            run_opencode.assert_not_called()
            self.assertIn("already running", notify.call_args.args[0])

    # --- 4. reattach a Claude session that already has a terminal ----------
    async def test_claude_session_with_tmux_terminal_reattaches_instead_of_launching(self) -> None:
        async with self.running(
            claude_status="idle", claude_instance=1, claude_terminals=("cc-aaa",)
        ) as (app, pilot):
            await pilot.press("4")
            await pilot.pause()
            table = app.query_one("#agents-table", DataTable)
            focus_row(app, table, CLAUDE_ID)
            await pilot.pause()
            with (
                mock.patch.object(app, "_attach_live_terminal", return_value=True) as attach,
                mock.patch.object(app, "_launch_tmux", return_value=True) as launch,
                mock.patch.object(app, "_run_opencode", return_value=True) as run_opencode,
            ):
                await pilot.press("o")
                await pilot.pause()
            attach.assert_called_once()
            self.assertEqual(attach.call_args.args[0].id, CLAUDE_ID)
            launch.assert_not_called()
            run_opencode.assert_not_called()

    # --- 5. cycle the launch harness ---------------------------------------
    async def test_shift_h_cycles_the_launch_harness(self) -> None:
        async with self.running() as (app, pilot):
            table = app.query_one("#agents-table", DataTable)
            table.focus()
            await pilot.pause()
            with mock.patch.object(app, "notify") as notify:
                self.assertEqual(self.source.launch_harness, "opencode")
                await pilot.press("H")
                self.assertEqual(self.source.launch_harness, "claude")
                self.assertEqual(
                    app._launch_harness(), "claude", "the deck must read the switched harness"
                )
                await pilot.press("H")
                self.assertEqual(self.source.launch_harness, "opencode")
            self.assertEqual(app._enabled_harnesses(), ("opencode", "claude"))
            self.assertTrue(
                any("Claude Code" in str(call.args[0]) for call in notify.call_args_list),
                [call.args for call in notify.call_args_list],
            )

    async def test_shift_h_wraps_around_a_single_harness(self) -> None:
        self.source.enabled_harnesses = ("claude",)
        self.source.launch_harness = "claude"
        async with self.running() as (app, pilot):
            app.query_one("#agents-table", DataTable).focus()
            await pilot.pause()
            await pilot.press("H")
            self.assertEqual(self.source.launch_harness, "claude")

    # --- 6. new session with the Claude harness ---------------------------
    async def test_new_session_uses_the_claude_harness_with_a_fresh_uuid(self) -> None:
        self.source.launch_harness = "claude"
        async with self.running() as (app, pilot):
            await pilot.press("1")
            await pilot.pause()
            projects = app.query_one("#projects-table", DataTable)
            projects.focus()
            projects.move_cursor(row=row_index(projects, PROJECT_ID))
            await pilot.pause()
            with (
                mock.patch.object(app, "_launch_tmux", return_value=True) as launch,
                mock.patch.object(app, "_run_opencode", return_value=True) as run_opencode,
            ):
                await pilot.press("n")
                await pilot.pause()
                # Several harnesses are enabled, so N offers the choice first;
                # the default (Claude here) is pre-selected.
                from ocdeck.launch_picker import LaunchPicker
                self.assertIsInstance(app.screen, LaunchPicker)
                await pilot.click("#launch-new")
                await app.workers.wait_for_complete()
                await pilot.pause()
            launch.assert_called_once()
            run_opencode.assert_not_called()
            name, directory, command = launch.call_args.args[:3]
            self.assertEqual(Path(directory), self.workspace)
            self.assertEqual(command[:3], [CLAUDE_BINARY, "--no-chrome", "--session-id"])
            session_id = command[3]
            uuid.UUID(session_id)  # a real, pre-assigned session id
            self.assertEqual(name, f"cc-{session_id}")

    # --- 7. OpenCode-only guards ------------------------------------------
    async def test_claude_y_without_a_pending_permission_does_nothing(self) -> None:
        async with self.running() as (app, pilot):
            await pilot.press("1")
            await pilot.pause()
            table = app.query_one("#sessions-table", DataTable)
            focus_row(app, table, CLAUDE_ID)
            await pilot.pause()
            with mock.patch.object(app, "notify") as notify:
                await pilot.press("y")
                await pilot.pause()
            self.assertIn("No pending permission for this session", notify.call_args.args[0])
            self.assertEqual(self.source.approvals, [])
            self.assertEqual(app._permission_replies_in_flight, set())

    async def test_claude_y_allows_a_pending_permission_once(self) -> None:
        async with self.running() as (app, pilot):
            await pilot.press("1")
            await pilot.pause()
            self.source.snap = replace(self.source.snap, sessions=tuple(
                replace(item, permission="Bash touch probe.txt", permission_id="req123")
                if item.id == CLAUDE_ID else item
                for item in self.source.snap.sessions))
            app._apply_snapshot(self.source.snap)
            await pilot.pause()
            table = app.query_one("#sessions-table", DataTable)
            focus_row(app, table, CLAUDE_ID)
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()
            self.assertEqual(self.source.approvals, [(CLAUDE_ID, "req123")])

    async def test_rename_is_opencode_only(self) -> None:
        async with self.running() as (app, _pilot):
            with mock.patch.object(app, "notify") as notify:
                app._begin_rename(CLAUDE_ID)
            self.assertIn("Renaming is OpenCode-only", notify.call_args.args[0])
            self.assertEqual(app.renaming_session_id, "")
            self.assertFalse(app.query_one("#session-rename").display)

    async def test_browser_enable_never_reaches_the_opencode_browser_api(self) -> None:
        """Shift+B on a Claude session toggles the harness agent browser, in isolation.

        The grant store is patched so the test never writes ~/.config/ocdeck.
        """
        grants: set[str] = set()

        def store(value: set[str]) -> None:
            grants.clear()
            grants.update(value)

        with (
            mock.patch("ocdeck.app.agent_browser_server", return_value=(["/bin/agent-browser"], {})),
            mock.patch("ocdeck.app.load_browser_grants", side_effect=lambda: set(grants)),
            mock.patch("ocdeck.app.save_browser_grants", side_effect=store),
        ):
            async with self.running() as (app, pilot):
                await pilot.press("1")
                await pilot.pause()
                table = app.query_one("#sessions-table", DataTable)
                focus_row(app, table, CLAUDE_ID)
                await pilot.pause()
                with mock.patch.object(app, "notify") as notify:
                    await pilot.press("B")
                    await pilot.pause()
                    self.assertIn("Agent browser enabled", notify.call_args.args[0])
                    self.assertEqual(grants, {CLAUDE_ID})
                    focus_row(app, app.query_one("#sessions-table", DataTable), CLAUDE_ID)
                    await pilot.press("B")
                    await pilot.pause()
                    self.assertIn("Agent browser revoked", notify.call_args.args[0])
                self.assertEqual(grants, set())
                self.assertEqual(self.source.browser_calls, [])
                self.assertEqual(app._browser_operations, set())
                self.assertEqual(app._browser_terminal_ids, set())

    async def test_sessions_list_badges_only_foreign_harness_rows(self) -> None:
        async with self.running() as (app, pilot):
            await pilot.press("1")
            await pilot.pause()
            table = app.query_one("#sessions-table", DataTable)
            title_index = app.session_title_index
            self.assertEqual(str(table.get_row(CLAUDE_ID)[title_index]), "CC Claude work")
            self.assertEqual(str(table.get_row(OPENCODE_ID)[title_index]), "Astra refactor")

    # --- 9. tmux names -----------------------------------------------------
    def test_tmux_names_follow_the_session_harness(self) -> None:
        snapshot = make_snapshot(str(self.workspace))
        app = OCDeckApp(self.source, auto_refresh=False, recent_open_file=self.recent)
        self.assertEqual(app._session_tmux_name_for_id(CLAUDE_ID), "cc-aaa")
        self.assertEqual(app._session_tmux_name_for_id(OPENCODE_ID), "oc2-ses_astra")
        names = {session.id: app._session_tmux_name(session) for session in snapshot.sessions}
        self.assertEqual(names, {CLAUDE_ID: "cc-aaa", OPENCODE_ID: "oc2-ses_astra"})

    def test_tmux_name_falls_back_to_opencode_prefix_without_an_adapter(self) -> None:
        source = FakeHarnessSource(
            self.workspace / "claude-projects", enabled=("opencode",), adapters={}
        )
        app = OCDeckApp(source, auto_refresh=False, recent_open_file=self.recent)
        self.assertEqual(app._session_tmux_name_for_id(CLAUDE_ID), "oc2-claude:aaa")

    # --- 10. build_source ---------------------------------------------------
    def test_build_source_enables_only_the_requested_harness(self) -> None:
        home = self.enterContext(tempfile.TemporaryDirectory())
        environment = {
            "HOME": home,
            "XDG_CONFIG_HOME": str(Path(home) / ".config"),
            "OCDECK_HARNESSES": "",
        }
        with (
            mock.patch.dict(os.environ, environment),
            mock.patch("ocdeck.harnesses.find_binary", return_value=CLAUDE_BINARY) as find_binary,
            mock.patch("ocdeck.app.DashboardSource") as dashboard_source,
        ):
            args = parse_args(["--harness", "claude"])
            source = build_source(args)
        self.assertIsInstance(source, MultiHarnessSource)
        self.assertTrue(source._refresh_harnesses)
        self.assertEqual(source._harness_override, ("claude",))
        self.assertFalse(source.opencode_enabled)
        self.assertEqual(source.enabled_harnesses, ("claude",))
        self.assertIsNone(source.opencode_bin)
        adapter = source.adapter("claude")
        self.assertIsInstance(adapter, ClaudeHarness)
        self.assertEqual(adapter.binary, CLAUDE_BINARY)
        self.assertEqual(adapter.root, Path(home) / ".claude" / "projects")
        self.assertIsNone(source.adapter("codex"))
        # The OpenCode source is still constructed, but never enabled or read.
        dashboard_source.assert_called_once()
        self.assertEqual(args.harness, "claude")
        # The adapter's executable comes from binary detection, not a hardcoded path.
        self.assertEqual(find_binary.call_args_list, [mock.call("claude")])

    def test_build_source_rejects_an_unknown_harness(self) -> None:
        home = self.enterContext(tempfile.TemporaryDirectory())
        environment = {
            "HOME": home,
            "XDG_CONFIG_HOME": str(Path(home) / ".config"),
            "OCDECK_HARNESSES": "",
        }
        with (
            mock.patch.dict(os.environ, environment),
            mock.patch("ocdeck.harnesses.find_binary", return_value=CLAUDE_BINARY),
            mock.patch("ocdeck.app.DashboardSource"),
            self.assertRaises(SystemExit),
        ):
            build_source(parse_args(["--harness", "bogus"]))

    def test_build_source_follows_the_harness_settings_file(self) -> None:
        home = self.enterContext(tempfile.TemporaryDirectory())
        environment = {
            "HOME": home,
            "XDG_CONFIG_HOME": str(Path(home) / ".config"),
            "OCDECK_HARNESSES": "",
        }
        settings = Path(home) / ".config" / "ocdeck" / "harnesses.json"
        settings.parent.mkdir(parents=True)
        settings.write_text('{"opencode": "on", "claude": "on", "codex": "on"}', encoding="utf-8")
        binaries = {"opencode2": "/bin/opencode2", "claude": CLAUDE_BINARY, "codex": "/bin/codex"}
        with (
            mock.patch.dict(os.environ, environment),
            mock.patch("ocdeck.harnesses.find_binary", side_effect=lambda harness: binaries.get(harness)),
            mock.patch("ocdeck.app.DashboardSource"),
        ):
            source = build_source(parse_args([]))
        self.assertTrue(source.opencode_enabled)
        self.assertEqual(source.enabled_harnesses, ("opencode", "claude", "codex"))
        self.assertEqual(source.launch_harness, "opencode")
        self.assertEqual(source.adapter("codex").binary, "/bin/codex")


class ClosedHarnessSessionTests(unittest.IsolatedAsyncioTestCase):
    """A closed Claude session should still be relaunchable from the AGENTS board.

    ``_closed_agent_sessions`` (src/ocdeck/app.py:1958) is fed by the recent-open store,
    whose ``SESSION_ID_PATTERN`` (src/ocdeck/recent_open.py:16) only accepts
    ``ses_*`` ids, so every ``claude:``/``codex:`` id is dropped on the way in and a
    closed foreign-harness session never renders as a CLOSED row (nor in Shift+L's
    relaunch set). Suggested fix: validate ids as ``<native>`` or ``<harness>:<native>``
    using ``HARNESS_IDS`` instead of an OpenCode-only ``ses_`` prefix.
    """

    async def test_closed_claude_session_is_listed_and_relaunchable_from_the_agents_board(self) -> None:
        workspace = Path(self.enterContext(tempfile.TemporaryDirectory()))
        recent = workspace / "recent.json"
        source = FakeHarnessSource(workspace / "claude-projects")
        source.snap = make_snapshot(str(workspace), claude_status="idle", claude_instance=0)
        # The deck remembers open ids through the recent-open store.
        self.assertEqual(save_recent_open_sessions(recent, [CLAUDE_ID]), [CLAUDE_ID])
        app = OCDeckApp(source, auto_refresh=False, recent_open_file=recent)
        async with app.run_test(size=NARROW_SIZE) as pilot:
            await pilot.pause()
            app._apply_snapshot(source.snap)
            await pilot.press("4")
            await pilot.pause()
            table = app.query_one("#agents-table", DataTable)
            self.assertIn(CLAUDE_ID, [str(key.value) for key in table.rows])
            focus_row(app, table, CLAUDE_ID)
            with mock.patch.object(app, "_launch_tmux", return_value=True) as launch:
                await pilot.press("o")
                await pilot.pause()
            launch.assert_called_once()
            self.assertEqual(launch.call_args.args[0], "cc-aaa")


if __name__ == "__main__":
    unittest.main()
