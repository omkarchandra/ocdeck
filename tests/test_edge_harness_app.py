"""Edge coverage for OC Deck harness/session keys: Shift+H, Shift+C, Shift+B,
Shift+N, n, o, x, y / rename, privacy badges, the RUNTIME resize flip and keys
typed while a search Input is focused.

Reuses the fake-source pattern from ``tests/test_harness_app.py``. Every tmux,
subprocess and launch path is mocked: OC Deck never talks to a real tmux, agent
binary or hub in these tests, and HOME / XDG_CONFIG_HOME / OCDECK_HUB_DIR /
CODEX_HOME point at throwaway directories.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
import uuid
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from textual.widgets import DataTable, Input, Static

from ocdeck.app import OCDeckApp
from ocdeck.harnesses import ClaudeHarness, CodexHarness
from ocdeck.models import DashboardSnapshot

from tests.test_harness_app import (
    CLAUDE_BINARY,
    CLAUDE_ID,
    CLAUDE_NATIVE,
    NARROW_SIZE,
    OPENCODE_ID,
    PROJECT_ID,
    WIDE_SIZE,
    FakeHarnessSource,
    focus_row,
    make_snapshot,
    row_index,
    runtime_cell,
)


class EdgeHarnessAppTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.workspace = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.environment = {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "OCDECK_HUB_DIR": str(self.home / "hub"),
            "CODEX_HOME": str(self.home / "codex"),
            "OCDECK_HARNESSES": "",
        }
        self.env_patcher = mock.patch.dict(os.environ, self.environment, clear=False)
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)
        self.recent = self.workspace / "recent.json"
        self.source = FakeHarnessSource(self.home / "claude-projects")

    def set_snapshot(self, **kwargs: object) -> DashboardSnapshot:
        self.source.snap = make_snapshot(str(self.workspace), **kwargs)  # type: ignore[arg-type]
        return self.source.snap

    @asynccontextmanager
    async def running(
        self,
        size: tuple[int, int] = NARROW_SIZE,
        snapshot: DashboardSnapshot | None = None,
        **kwargs: object,
    ):
        """Boot the deck against the fake source with a known snapshot applied."""
        self.source.snap = (
            snapshot if snapshot is not None else self.set_snapshot(**kwargs)
        )
        app = OCDeckApp(self.source, auto_refresh=False, recent_open_file=self.recent)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            app._apply_snapshot(self.source.snap)
            await pilot.pause()
            yield app, pilot

    @asynccontextmanager
    async def launching(self, app: OCDeckApp):
        """Patch every side-effectful shell/launch helper; none may run for real."""
        with ExitStack() as stack:
            yield SimpleNamespace(
                launch=stack.enter_context(
                    mock.patch.object(app, "_launch_tmux", return_value=True)
                ),
                run_opencode=stack.enter_context(
                    mock.patch.object(app, "_run_opencode", return_value=True)
                ),
                browser=stack.enter_context(
                    mock.patch.object(app, "_run_browser_session", return_value=True)
                ),
                has_session=stack.enter_context(
                    mock.patch.object(app, "_tmux_has_session", return_value=False)
                ),
                stop=stack.enter_context(mock.patch.object(app, "_stop_job_worker")),
                attach=stack.enter_context(
                    mock.patch.object(app, "_attach_live_terminal", return_value=False)
                ),
                focus=stack.enter_context(
                    mock.patch.object(app, "_focus_process_via_ptyxis", return_value=False)
                ),
                renderers=stack.enter_context(
                    mock.patch("ocdeck.app.direct_renderers", return_value=())
                ),
            )

    @staticmethod
    def notify_texts(notify) -> list[str]:
        return [str(call.args[0]) for call in notify.call_args_list if call.args]

    @staticmethod
    def grant_store():
        grants: set[str] = set()

        def store(value: set[str]) -> None:
            grants.clear()
            grants.update(value)

        return grants, store

    def focus_selected(self, app, *, row: str = CLAUDE_ID, agents: bool = False) -> None:
        table = app.query_one("#agents-table" if agents else "#sessions-table", DataTable)
        focus_row(app, table, row)

    # --- 1. Shift+H cycles the launch harness -------------------------------
    async def test_cycle_harness_with_zero_enabled_harnesses_warns_and_keeps_state(self) -> None:
        self.source.enabled_harnesses = ()
        self.source.launch_harness = "claude"
        async with self.running() as (app, pilot):
            app.query_one("#agents-table", DataTable).focus()
            await pilot.pause()
            with mock.patch.object(app, "notify") as notify:
                await pilot.press("H")
                await pilot.pause()
            self.assertIn("No harness is enabled", self.notify_texts(notify)[0])
            self.assertEqual(self.source.launch_harness, "claude")

    async def test_cycle_harness_with_a_single_enabled_harness_stays_put(self) -> None:
        self.source.enabled_harnesses = ("claude",)
        self.source.launch_harness = "claude"
        async with self.running() as (app, pilot):
            app.query_one("#agents-table", DataTable).focus()
            await pilot.pause()
            with mock.patch.object(app, "notify") as notify:
                await pilot.press("H")
                await pilot.press("H")
                await pilot.press("H")
                await pilot.pause()
            self.assertEqual(self.source.launch_harness, "claude")
            self.assertTrue(
                all("Claude Code" in text for text in self.notify_texts(notify)),
                self.notify_texts(notify),
            )

    async def test_cycle_harness_through_two_enabled_harnesses(self) -> None:
        async with self.running() as (app, pilot):
            app.query_one("#agents-table", DataTable).focus()
            await pilot.pause()
            with mock.patch.object(app, "notify") as notify:
                self.assertEqual(self.source.launch_harness, "opencode")
                await pilot.press("H")
                await pilot.pause()
                self.assertEqual(self.source.launch_harness, "claude")
                await pilot.press("H")
                await pilot.pause()
                self.assertEqual(self.source.launch_harness, "opencode")
            self.assertTrue(
                any(
                    "New sessions launch with Claude Code" in text
                    for text in self.notify_texts(notify)
                ),
                self.notify_texts(notify),
            )

    async def test_cycle_harness_through_three_enabled_harnesses(self) -> None:
        self.source._adapters = {
            "claude": ClaudeHarness(self.home / "claude-projects", CLAUDE_BINARY),
            "codex": CodexHarness(self.home / "codex" / "sessions", "/bin/codex"),
        }
        self.source.enabled_harnesses = ("claude", "opencode", "codex")
        self.source.launch_harness = "claude"
        async with self.running() as (app, pilot):
            app.query_one("#agents-table", DataTable).focus()
            await pilot.pause()
            seen = []
            all_texts: list[str] = []
            for _ in range(3):
                with mock.patch.object(app, "notify") as notify:
                    await pilot.press("H")
                    await pilot.pause()
                    seen.append(self.source.launch_harness)
                    all_texts.extend(self.notify_texts(notify))
            self.assertEqual(seen, ["opencode", "codex", "claude"], seen)
            self.assertTrue(any("Codex" in text for text in all_texts), all_texts)

    async def test_cycle_harness_when_launch_harness_is_disabled_jumps_to_first_enabled(self) -> None:
        self.source.launch_harness = "codex"  # not part of the enabled harnesses
        async with self.running() as (app, pilot):
            app.query_one("#agents-table", DataTable).focus()
            await pilot.pause()
            with mock.patch.object(app, "notify") as notify:
                await pilot.press("H")
                await pilot.pause()
            self.assertEqual(self.source.launch_harness, "opencode")
            self.assertIn(
                "New sessions launch with OpenCode", self.notify_texts(notify)[-1]
            )

    # --- 2. Shift+C handoff --------------------------------------------------
    async def test_handoff_to_the_same_harness_is_refused(self) -> None:
        self.source.launch_harness = "claude"
        async with self.running() as (app, pilot):
            with (
                mock.patch.object(app, "_handoff_worker") as worker,
                mock.patch.object(app, "notify") as notify,
                mock.patch("ocdeck.hub.write_handoff") as write_handoff,
            ):
                await pilot.press("1")
                await pilot.pause()
                table = app.query_one("#sessions-table", DataTable)
                table.focus()
                table.move_cursor(row=row_index(table, CLAUDE_ID))
                await pilot.pause()
                await pilot.press("C")
                await pilot.pause()
                self.assertIn(
                    "Pick a different harness with Shift+H (currently Claude Code)",
                    self.notify_texts(notify)[0],
                )
                worker.assert_not_called()
                write_handoff.assert_not_called()

    async def test_handoff_without_a_selected_session_reports_one(self) -> None:
        empty = DashboardSnapshot(connection="live", connection_detail="")
        async with self.running(snapshot=empty) as (app, pilot):
            with (
                mock.patch.object(app, "_handoff_worker") as worker,
                mock.patch.object(app, "notify") as notify,
            ):
                await pilot.press("C")
                await pilot.pause()
            self.assertIn("Select a session first", self.notify_texts(notify)[0])
            worker.assert_not_called()

    async def test_handoff_worker_reports_hub_write_failure(self) -> None:
        async with self.running() as (app, pilot):
            with (
                mock.patch(
                    "ocdeck.hub.write_handoff", side_effect=RuntimeError("hub broke")
                ) as write_handoff,
                mock.patch.object(app, "notify") as notify,
            ):
                async with self.launching(app) as mocks:
                    await pilot.press("1")
                    await pilot.pause()
                    self.focus_selected(app)
                    await pilot.pause()
                    await pilot.press("C")
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                    write_handoff.assert_called_once()
                    self.assertIn(
                        "Handoff failed: RuntimeError: hub broke",
                        self.notify_texts(notify),
                    )
                    mocks.launch.assert_not_called()
                    mocks.run_opencode.assert_not_called()

    async def test_handoff_to_opencode_runs_opencode_with_the_continuation_prompt(self) -> None:
        async with self.running() as (app, pilot):
            with (
                mock.patch(
                    "ocdeck.hub.write_handoff",
                    return_value=(self.workspace / "handoff.md", "carry on here"),
                ) as write_handoff,
                mock.patch.object(app, "notify") as notify,
            ):
                async with self.launching(app) as mocks:
                    await pilot.press("1")
                    await pilot.pause()
                    self.focus_selected(app)
                    await pilot.pause()
                    self.assertEqual(self.source.launch_harness, "opencode")
                    await pilot.press("C")
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                    write_handoff.assert_called_once()
                    mocks.run_opencode.assert_called_once()
                    arguments = mocks.run_opencode.call_args.args
                    options = mocks.run_opencode.call_args.kwargs
    
                    self.assertTrue(str(options["tmux_name"]).startswith("oc-handoff-"))
                    self.assertEqual(options["project_id"], PROJECT_ID)
                    self.assertEqual(options["title"], "handoff: Claude work")
                    self.assertIn(
                        "Handed off to OpenCode", self.notify_texts(notify)[-1]
                    )

    async def test_handoff_to_claude_starts_a_new_claude_session_with_a_uuid(self) -> None:
        self.source.launch_harness = "claude"
        async with self.running() as (app, pilot):
            with (
                mock.patch(
                    "ocdeck.hub.write_handoff",
                    return_value=(self.workspace / "handoff.md", "continue from OpenCode"),
                ) as write_handoff,
                mock.patch.object(app, "notify") as notify,
            ):
                async with self.launching(app) as mocks:
                    await pilot.press("1")
                    await pilot.pause()
                    self.focus_selected(app, row=OPENCODE_ID)
                    await pilot.pause()
                    await pilot.press("C")
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                    write_handoff.assert_called_once()
                    mocks.launch.assert_called_once()
                    name, directory, command = mocks.launch.call_args.args[:3]
                    self.assertTrue(str(name).startswith("cc-"), name)
                    self.assertEqual(Path(directory), self.workspace)
                    self.assertEqual(command[0], CLAUDE_BINARY)
                    self.assertIn("--session-id", command)
                    uuid.UUID(command[3])
                    self.assertEqual(command[4], "continue from OpenCode")
                    mocks.run_opencode.assert_not_called()
                    self.assertIn(
                        "Handed off to Claude Code", self.notify_texts(notify)[-1]
                    )

    # --- 3. Shift+B agent-browser grants -------------------------------------
    async def test_browser_grant_toggle_grants_and_revokes_on_the_agents_board(self) -> None:
        grants, store = self.grant_store()
        with (
            mock.patch("ocdeck.app.agent_browser_server", return_value=("/bin/agent-browser", {})),
            mock.patch("ocdeck.app.load_browser_grants", side_effect=lambda: set(grants)),
            mock.patch("ocdeck.app.save_browser_grants", side_effect=store) as save_grants,
        ):
            async with self.running() as (app, pilot):
                with mock.patch.object(app, "notify") as notify:
                    await pilot.press("4")
                    await pilot.pause()
                    self.focus_selected(app, agents=True)
                    await pilot.pause()
                    await pilot.press("B")
                    await pilot.pause()
                    self.assertIn(
                        "Agent browser enabled", self.notify_texts(notify)[-1]
                    )
                    self.assertEqual(grants, {CLAUDE_ID})
                    self.focus_selected(app, agents=True)
                    await pilot.pause()
                    await pilot.press("B")
                    await pilot.pause()
                    self.assertIn(
                        "Agent browser revoked", self.notify_texts(notify)[-1]
                    )
                self.assertEqual(grants, set())
                self.assertEqual(save_grants.call_count, 2)

    async def test_browser_grant_toggle_survives_a_failed_save(self) -> None:
        async with self.running() as (app, pilot):
            with (
                mock.patch("ocdeck.app.agent_browser_server", return_value=("/bin/agent-browser", {})),
                mock.patch("ocdeck.app.load_browser_grants", return_value=set()),
                mock.patch("ocdeck.app.save_browser_grants", side_effect=OSError("disk full")),
                mock.patch.object(app, "notify") as notify,
            ):
                await pilot.press("1")
                await pilot.pause()
                self.focus_selected(app)
                await pilot.pause()
                await pilot.press("B")
                await pilot.pause()
                self.assertIn(
                    "Could not save the browser grant", self.notify_texts(notify)[-1]
                )

    async def test_browser_grant_toggle_works_in_private_mode_without_leaking_names(self) -> None:
        grants, store = self.grant_store()
        with (
            mock.patch("ocdeck.app.agent_browser_server", return_value=("/bin/agent-browser", {})),
            mock.patch("ocdeck.app.load_browser_grants", side_effect=lambda: set(grants)),
            mock.patch("ocdeck.app.save_browser_grants", side_effect=store),
        ):
            async with self.running() as (app, pilot):
                with mock.patch.object(app, "notify") as notify:
                    await pilot.press("1")
                    await pilot.pause()
                    self.focus_selected(app)
                    await pilot.pause()
                    await pilot.press("p")
                    await pilot.pause()
                    self.assertTrue(app.private)
                    self.assertIn("Privacy mode on", self.notify_texts(notify))
                    await pilot.press("B")
                    await pilot.pause()
                self.assertEqual(grants, {CLAUDE_ID})
                texts = self.notify_texts(notify)
                self.assertIn("Agent browser enabled", texts[-1])
                # The confirmation must never name the private session or project.
                joined = " ".join(texts)
                self.assertNotIn("Claude work", joined)
                self.assertNotIn(self.workspace.name, joined)

    async def test_browser_grant_toggle_is_read_only_on_the_next_tab(self) -> None:
        grants, store = self.grant_store()
        with (
            mock.patch("ocdeck.app.agent_browser_server", return_value=("/bin/agent-browser", {})),
            mock.patch(
                "ocdeck.app.load_browser_grants", side_effect=lambda: set(grants)
            ) as load_grants,
            mock.patch("ocdeck.app.save_browser_grants", side_effect=store),
        ):
            async with self.running() as (app, pilot):
                await pilot.press("5")
                await pilot.pause()
                await pilot.pause()
                with mock.patch.object(app, "notify") as notify:
                    # The NEXT pane itself swallows Shift+B with its read-only noop.
                    await pilot.press("B")
                    await pilot.pause()
                    self.assertEqual(app.query_one("#tabs").active, "next")
                    # With no widget focused the app-level Shift+B binding runs
                    # the read-only guard instead of touching the grant store.
                    app.set_focus(None)
                    await pilot.pause()
                    await pilot.press("B")
                    await pilot.pause()
                    self.assertIn(
                        "NEXT is read-only", self.notify_texts(notify)[-1]
                    )
                self.assertEqual(grants, set())
                load_grants.assert_not_called()

    # --- 4. Shift+N browser session with a foreign launch harness ------------
    async def test_new_browser_session_with_the_claude_harness_grants_the_fresh_id(self) -> None:
        self.source.launch_harness = "claude"
        grants, store = self.grant_store()
        with (
            mock.patch("ocdeck.app.agent_browser_server", return_value=("/bin/agent-browser", {})),
            mock.patch("ocdeck.harnesses.agent_browser_server", return_value=None),
            mock.patch("ocdeck.app.load_browser_grants", side_effect=lambda: set(grants)),
            mock.patch("ocdeck.app.save_browser_grants", side_effect=store),
        ):
            async with self.running() as (app, pilot):
                async with self.launching(app) as mocks:
                    with mock.patch.object(app, "notify"):
                        app.query_one("#agents-table", DataTable).focus()
                        await pilot.pause()
                        await pilot.press("N")
                        await pilot.pause()
                        mocks.launch.assert_called_once()
                        name, directory, command = mocks.launch.call_args.args[:3]
                        self.assertTrue(str(name).startswith("cc-"), name)
                        self.assertEqual(Path(directory), self.workspace)
                        self.assertEqual(command[:3], [CLAUDE_BINARY, "--no-chrome", "--session-id"])
                        self.assertEqual(
                            grants,
                            {f"claude:{command[3]}"},
                            "the fresh Claude session id must be pre-granted the agent browser",
                        )
                        mocks.run_opencode.assert_not_called()

    async def test_new_browser_session_with_the_codex_harness_never_pregrants(self) -> None:
        self.source._adapters = {
            "codex": CodexHarness(self.home / "codex" / "sessions", "/bin/codex"),
        }
        self.source.enabled_harnesses = ("opencode", "codex")
        self.source.launch_harness = "codex"
        with (
            mock.patch("ocdeck.app.agent_browser_server", return_value=("/bin/agent-browser", {})),
            mock.patch("ocdeck.harnesses.agent_browser_server", return_value=None),
            mock.patch("ocdeck.app.load_browser_grants") as load_grants,
            mock.patch("ocdeck.app.save_browser_grants") as save_grants,
        ):
            async with self.running() as (app, pilot):
                with mock.patch.object(app, "notify") as notify:
                    async with self.launching(app) as mocks:
                        app.query_one("#agents-table", DataTable).focus()
                        await pilot.pause()
                        await pilot.press("N")
                        await pilot.pause()
                        mocks.launch.assert_called_once()
                        name, directory, command = mocks.launch.call_args.args[:3]
                        self.assertTrue(str(name).startswith("cx-new-"), name)
                        self.assertEqual(Path(directory), self.workspace)
                        self.assertEqual(command[0], "/bin/codex")
                        # Codex gets no session id until its first rollout is
                        # written, so no grant can be stored yet and the deck
                        # instead asks the operator to press Shift+B on the row.
                        save_grants.assert_not_called()
                        load_grants.assert_not_called()
                        self.assertIn(
                            "Shift+B on the new session",
                            self.notify_texts(notify)[-1],
                        )

    # --- 5. n with a disabled launch harness ---------------------------------
    async def test_new_session_is_refused_when_launch_harness_is_disabled(self) -> None:
        self.source.enabled_harnesses = ("opencode", "claude")
        self.source.launch_harness = "codex"  # not enabled
        async with self.running() as (app, pilot):
            with mock.patch.object(app, "notify") as notify:
                async with self.launching(app) as mocks:
                    await pilot.press("1")
                    await pilot.pause()
                    await pilot.press("n")
                    await pilot.pause()
                    # N offers only enabled harnesses: the disabled default
                    # (codex) cannot be chosen, and nothing launches unasked.
                    from textual.widgets import Select
                    from ocdeck.launch_picker import LaunchPicker
                    self.assertIsInstance(app.screen, LaunchPicker)
                    offered = [value for _label, value in app.screen.query_one("#launch-harness", Select)._options]
                    self.assertNotIn("codex", offered)
                    mocks.launch.assert_not_called()
                    mocks.run_opencode.assert_not_called()
                    await pilot.press("escape")
                    await pilot.pause()
                    # The direct path still refuses a disabled default harness.
                    app.action_new_session()
                    self.assertIn("No enabled harness can start sessions", self.notify_texts(notify)[-1])
                    mocks.launch.assert_not_called()

    # --- 6. o on a Claude session -------------------------------------------
    async def test_o_on_a_closed_claude_session_resumes_and_records_a_launch_window(self) -> None:
        async with self.running(claude_status="idle", claude_instance=0) as (app, pilot):
            with mock.patch.object(app, "notify") as notify:
                async with self.launching(app) as mocks:
                    await pilot.press("1")
                    await pilot.pause()
                    self.focus_selected(app)
                    await pilot.pause()
                    await pilot.press("o")
                    await pilot.pause()
                    mocks.launch.assert_called_once()
                    name, directory, command = mocks.launch.call_args.args[:3]
                    self.assertEqual(name, "cc-aaa")
                    self.assertEqual(Path(directory), self.workspace)
                    self.assertEqual(command, [CLAUDE_BINARY, "--no-chrome", "--resume", CLAUDE_NATIVE])
                    pending = app._harness_launch_pending[CLAUDE_ID]
                    self.assertGreater(pending, time.monotonic())
                    self.assertLessEqual(pending, time.monotonic() + 15)
                self.assertFalse(
                    [text for text in self.notify_texts(notify) if "not found" in text]
                )

    async def test_o_on_a_live_direct_claude_session_focusses_the_window(self) -> None:
        async with self.running(claude_status="idle", claude_instance=1) as (app, pilot):
            self.source.adapter("claude").live_pids = {CLAUDE_NATIVE: (4243,)}
            with mock.patch.object(app, "notify") as notify:
                async with self.launching(app) as mocks:
                    mocks.focus.return_value = True
                    await pilot.press("4")
                    await pilot.pause()
                    self.focus_selected(app, agents=True)
                    await pilot.pause()
                    await pilot.press("o")
                    await pilot.pause()
                mocks.launch.assert_not_called()
                mocks.run_opencode.assert_not_called()
                mocks.focus.assert_called_once_with(4243)
                self.assertIn(
                    "Focused the Claude Code session's window",
                    self.notify_texts(notify)[-1],
                )

    async def test_o_on_a_live_managed_tmux_claude_session_attaches_without_needing_a_binary(self) -> None:
        async with self.running(
            claude_status="idle", claude_instance=1, claude_terminals=("cc-aaa",)
        ) as (app, pilot):
            self.source.adapter("claude").binary = None
            with mock.patch.object(app, "notify") as notify:
                async with self.launching(app) as mocks:
                    mocks.attach.return_value = True
                    await pilot.press("1")
                    await pilot.pause()
                    self.focus_selected(app)
                    await pilot.pause()
                    await pilot.press("o")
                    await pilot.pause()
                    mocks.launch.assert_not_called()
                    mocks.run_opencode.assert_not_called()
                mocks.attach.assert_called_once()
                self.assertEqual(mocks.attach.call_args.args[0].id, CLAUDE_ID)
                self.assertEqual(
                    [text for text in self.notify_texts(notify) if "not found" in text],
                    [],
                    "attaching a live terminal must not require the harness binary",
                )

    async def test_o_within_15_s_of_a_relaunch_is_refused_then_allowed_after_expiry(self) -> None:
        async with self.running(claude_status="idle", claude_instance=0) as (app, pilot):
            with mock.patch.object(app, "notify") as notify:
                async with self.launching(app) as mocks:
                    await pilot.press("1")
                    await pilot.pause()
                    self.focus_selected(app)
                    await pilot.pause()
                    await pilot.press("o")
                    await pilot.pause()
                    self.assertEqual(mocks.launch.call_count, 1)
                    await pilot.press("o")
                    await pilot.pause()
                    self.assertEqual(
                        mocks.launch.call_count, 1, "a pending relaunch must be refused"
                    )
                    self.assertIn(
                        "already starting or running", self.notify_texts(notify)[-1]
                    )
                    self.assertIn("Claude Code", self.notify_texts(notify)[-1])
                    app._harness_launch_pending[CLAUDE_ID] = time.monotonic() - 1
                    await pilot.press("o")
                    await pilot.pause()
                    self.assertEqual(
                        mocks.launch.call_count, 2,
                        "an expired relaunch window must allow a relaunch",
                    )

    async def test_o_without_a_binary_notifies_an_error_and_launches_nothing(self) -> None:
        missing = ClaudeHarness(self.home / "claude-projects", CLAUDE_BINARY)
        missing.binary = None
        self.source._adapters = {"claude": missing}
        async with self.running(claude_status="idle", claude_instance=0) as (app, pilot):
            with mock.patch.object(app, "notify") as notify:
                async with self.launching(app) as mocks:
                    await pilot.press("1")
                    await pilot.pause()
                    self.focus_selected(app)
                    await pilot.pause()
                    await pilot.press("o")
                    await pilot.pause()
                    texts = self.notify_texts(notify)
                    self.assertEqual(
                        texts[-1], "Claude Code executable not found", texts
                    )
                    mocks.launch.assert_not_called()
                    mocks.run_opencode.assert_not_called()

    # --- 7. x must never touch a foreign tmux name ---------------------------
    async def test_x_only_interrupts_the_managed_terminal_and_never_the_foreign_one(self) -> None:
        async with self.running(
            claude_status="idle", claude_instance=1, claude_terminals=("main", "cc-aaa")
        ) as (app, pilot):
            with mock.patch.object(app, "notify") as notify, \
                    mock.patch.object(
                        app,
                        "_tmux_has_session",
                        side_effect=lambda name: name in {"main", "cc-aaa"},
                    ) as has_session, \
                    mock.patch.object(app, "_tmux_kill_session", return_value=True) as kill, \
                    mock.patch.object(app, "_interrupt_claude_worker") as interrupt, \
                    mock.patch.object(app, "_stop_job_worker") as stop_worker:
                await pilot.press("1")
                await pilot.pause()
                self.focus_selected(app)
                await pilot.pause()
                await pilot.press("x")
                await pilot.pause()
                self.assertIn(
                    "Press x again to interrupt the agent in cc-aaa (Esc, as in Claude Code",
                    self.notify_texts(notify)[-1],
                )
                await pilot.press("x")
                await pilot.pause()
                interrupt.assert_called_once_with("cc-aaa")
                stop_worker.assert_not_called()  # a Claude terminal is interrupted, not killed
                self.assertEqual(
                    [call for call in kill.call_args_list if "main" in str(call)],
                    [],
                    "the user's own tmux session must never be a stop target",
                )

    async def test_x_stops_a_claude_process_that_runs_outside_tmux(self) -> None:
        # e.g. an agent's `claude -p`, or claude started in a plain terminal.
        from ocdeck.process_stop import ProcessTarget
        target = ProcessTarget(4242, 99, ("claude", "--resume", "aaa"))
        async with self.running(claude_status="busy", claude_instance=1) as (app, pilot):
            app._harness_adapter("claude").stoppable_pids = {"aaa": (4242,)}
            with mock.patch.object(app, "notify") as notify, \
                    mock.patch("ocdeck.app.identify", return_value=target), \
                    mock.patch("ocdeck.app.stop_processes", return_value=(1, 0)) as stop:
                async with self.launching(app) as mocks:
                    await pilot.press("1")
                    await pilot.pause()
                    self.focus_selected(app)
                    await pilot.pause()
                    await pilot.press("x")
                    await pilot.pause()
                    stop.assert_not_called()  # the first press only asks
                    self.assertEqual(
                        self.notify_texts(notify)[-1],
                        "Press x again to stop this Claude Code session (process 4242); "
                        "the conversation is saved and o reopens it",
                    )
                    await pilot.press("x")
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                    stop.assert_called_once_with((target,))
                    mocks.stop.assert_not_called()  # no tmux session is killed
                    self.assertEqual(self.notify_texts(notify)[-1],
                                     "Stopped the Claude Code session; press o to reopen it")

    async def test_x_with_only_a_foreign_terminal_never_kills_it(self) -> None:
        async with self.running(
            claude_status="idle", claude_instance=1, claude_terminals=("main",)
        ) as (app, pilot):
            with mock.patch.object(app, "notify") as notify:
                async with self.launching(app) as mocks:
                    await pilot.press("1")
                    await pilot.pause()
                    self.focus_selected(app)
                    await pilot.pause()
                    # Neither "main" (foreign) nor "cc-aaa" (canonical) is running.
                    await pilot.press("x")
                    await pilot.pause()
                    await pilot.press("x")
                    await pilot.pause()
                    mocks.stop.assert_not_called()
                    self.assertIn(
                        "No live terminal is attached to this session",
                        self.notify_texts(notify)[-1],
                    )
                    # Now pretend every tmux session exists, foreign "main" too.
                    mocks.has_session.side_effect = lambda name: True
                    with mock.patch.object(app, "_interrupt_claude_worker") as interrupt:
                        await pilot.press("x")
                        await pilot.pause()
                        await pilot.press("x")
                        await pilot.pause()
                        interrupt.assert_called_once_with("cc-aaa")
                        for call in interrupt.call_args_list:
                            self.assertNotIn("main", str(call))

    # --- 8. y and rename are OpenCode-only on foreign harnesses --------------
    async def test_permission_y_and_rename_on_a_claude_session(self) -> None:
        async with self.running() as (app, pilot):
            with mock.patch.object(app, "notify") as notify:
                await pilot.press("1")
                await pilot.pause()
                self.focus_selected(app)
                await pilot.pause()
                await pilot.press("y")
                await pilot.pause()
                self.assertIn("No pending permission for this session", self.notify_texts(notify)[-1])
                self.assertEqual(app._permission_replies_in_flight, set())
                app._begin_rename(CLAUDE_ID)
                self.assertIn(
                    "Renaming is OpenCode-only; use the Claude Code terminal",
                    self.notify_texts(notify)[-1],
                )
                self.assertEqual(app.renaming_session_id, "")
                self.assertFalse(app.query_one("#session-rename").display)

    # --- 9. privacy mode hides badges; RUNTIME flips on resize ---------------
    async def test_privacy_mode_hides_harness_badges_on_the_agents_board(self) -> None:
        async with self.running() as (app, pilot):
            await pilot.press("4")
            await pilot.pause()
            table = app.query_one("#agents-table", DataTable)
            self.assertEqual(str(table.get_row(CLAUDE_ID)[2]), "CC Claude work")
            await pilot.press("p")
            await pilot.pause()
            await pilot.pause()
            title = str(table.get_row(CLAUDE_ID)[2])
            self.assertEqual(title, "Hidden session")
            self.assertNotIn("CC", title)
            self.assertNotIn("Claude work", title)

    async def test_runtime_column_switches_between_codes_and_full_names_on_resize(self) -> None:
        async with self.running() as (app, pilot):
            await pilot.press("4")
            await pilot.pause()
            table = app.query_one("#agents-table", DataTable)
            self.assertFalse(app.agent_runtime_full)
            self.assertEqual(runtime_cell(table, CLAUDE_ID), "CC OP5")
            self.assertEqual(runtime_cell(table, OPENCODE_ID), "OC AST")

            await pilot.resize_terminal(*WIDE_SIZE)
            await pilot.pause()
            await pilot.pause()
            await pilot.pause()
            self.assertTrue(app.agent_runtime_full)
            self.assertEqual(
                runtime_cell(table, CLAUDE_ID), "Claude Code · claude-opus-5-5"
            )
            self.assertEqual(runtime_cell(table, OPENCODE_ID), "OpenCode · gpt-6-astra")

            await pilot.resize_terminal(*NARROW_SIZE)
            await pilot.pause()
            await pilot.pause()
            await pilot.pause()
            self.assertFalse(app.agent_runtime_full)
            self.assertEqual(runtime_cell(table, CLAUDE_ID), "CC OP5")

    async def test_runtime_column_is_correct_at_each_boot_size(self) -> None:
        async with self.running(size=WIDE_SIZE) as (app, pilot):
            await pilot.press("4")
            await pilot.pause()
            table = app.query_one("#agents-table", DataTable)
            self.assertTrue(app.agent_runtime_full)
            self.assertEqual(
                runtime_cell(table, CLAUDE_ID), "Claude Code · claude-opus-5-5"
            )

    # --- 10. typed keys while the search Input is focused --------------------
    async def test_keys_typed_into_the_session_search_never_trigger_actions(self) -> None:
        async with self.running() as (app, pilot):
            search = app.query_one("#session-search", Input)
            with (
                mock.patch.object(app, "notify") as notify,
                mock.patch.object(app, "_handoff_worker") as worker,
            ):
                async with self.launching(app) as mocks:
                    await pilot.press("1")
                    await pilot.pause()
                    search.focus()
                    await pilot.pause()
                    await pilot.press("H")
                    await pilot.pause()
                    self.assertEqual(self.source.launch_harness, "opencode")
                    await pilot.press("C")
                    await pilot.pause()
                    await pilot.press("o")
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                self.assertEqual(search.value, "HCo")
                worker.assert_not_called()
                mocks.launch.assert_not_called()
                mocks.run_opencode.assert_not_called()
                self.assertEqual(
                    [
                        text
                        for text in self.notify_texts(notify)
                        if "handoff" in text.lower()
                    ],
                    [],
                )


class PrivateRuntimeVisibilityTests(unittest.IsolatedAsyncioTestCase):
    """Privacy mode must hide harness identifiers, not only session names.

    ``_render_agents`` already skips the CC badge while ``self.private`` is on
    (src/ocdeck/app.py:1828-1831). The RUNTIME column does not: ``_runtime_text``
    (src/ocdeck/app.py:2868) renders "CC OP5" / "Claude Code · <model>" for every
    row with no ``self.private`` check, and the detail pane's ``_runtime_block``
    (src/ocdeck/app.py:2850) still prints "RUNTIME\nOpenCode · <model>" under
    privacy mode. Suggested fix: honour ``self.private`` in ``_runtime_text`` and
    ``_runtime_block`` (return an empty/hidden Text or block).
    """

    def setUp(self) -> None:
        super().setUp()
        self.workspace = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.environment = {
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "OCDECK_HUB_DIR": str(self.home / "hub"),
            "CODEX_HOME": str(self.home / "codex"),
            "OCDECK_HARNESSES": "",
        }
        self.env_patcher = mock.patch.dict(os.environ, self.environment, clear=False)
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)
        self.source = FakeHarnessSource(self.home / "claude-projects")

    @asynccontextmanager
    async def private_app(self):
        self.source.snap = make_snapshot(str(self.workspace))
        app = OCDeckApp(
            self.source, auto_refresh=False, recent_open_file=self.workspace / "recent.json"
        )
        async with app.run_test(size=NARROW_SIZE) as pilot:
            await pilot.pause()
            app._apply_snapshot(self.source.snap)
            await pilot.press("p")
            await pilot.pause()
            await pilot.pause()
            yield app

    async def test_private_mode_hides_the_runtime_column_on_the_agents_board(self) -> None:
        async with self.private_app() as app:
            table = app.query_one("#agents-table", DataTable)
            for session_id in (CLAUDE_ID, OPENCODE_ID):
                label = str(table.get_row(session_id)[-1])
                self.assertNotIn(
                    "CC", label, "RUNTIME codes must be hidden in privacy mode"
                )
                self.assertNotIn(
                    "OP5", label, "RUNTIME codes must be hidden in privacy mode"
                )

    async def test_private_mode_hides_the_runtime_block_in_the_detail_pane(self) -> None:
        async with self.private_app() as app:
            block = app._runtime_block(app.session_by_id[OPENCODE_ID])
            self.assertNotIn(
                "OpenCode",
                block,
                "the RUNTIME block must not leak the model in privacy mode",
            )
            self.assertNotIn(
                "gpt-6-astra",
                block,
                "the RUNTIME block must not leak the model in privacy mode",
            )


if __name__ == "__main__":
    unittest.main()
