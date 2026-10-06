"""Project launch choices and real key routing, with all launches mocked."""
from __future__ import annotations

from contextlib import asynccontextmanager
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from textual.widgets import Button, DataTable, Input, Select

from ocdeck.app import OCDeckApp
from ocdeck.harnesses import ClaudeHarness, CodexHarness, SCRATCH_PROJECT_ID, route_projects
from ocdeck.launch_picker import LaunchChoice, LaunchPicker, agent_arguments, discover_agents
from ocdeck.models import ProjectRecord
from tests.test_harness_app import FakeHarnessSource, make_snapshot, focus_row, CLAUDE_ID, PROJECT_ID


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.project = self.root / "project"
        self.project.mkdir()
        self.enterContext(mock.patch.dict(os.environ, {
            "HOME": str(self.root), "XDG_CONFIG_HOME": str(self.root / "config"),
            "CLAUDE_CONFIG_DIR": str(self.root / "claude"), "CODEX_HOME": str(self.root / "codex"),
        }))

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        return target

    def test_discovery_uses_native_names_and_ignores_hidden_subagents(self):
        self.write("project/.opencode/agents/team/reviewer.md", "---\nmode: primary\n---\n")
        self.write("project/.opencode/agents/worker.md", "---\nmode: subagent\n---\n")
        self.write("project/.opencode/agents/hidden.md", "---\nhidden: true\n---\n")
        self.write("project/.claude/agents/file-name.md", "---\nname: actual-agent\ndescription: Review\n---\n")
        self.write("project/.claude/agents/docs.md", "This is documentation, not an agent.")
        self.write("codex/review.config.toml", "contents are not read by discovery")
        with mock.patch("subprocess.Popen", side_effect=AssertionError("no subprocess")):
            choices = discover_agents(self.project, ("configured-agent", "--bad"))
        self.assertEqual(choices["opencode"], ("build", "configured-agent", "plan", "team/reviewer"))
        self.assertEqual(choices["claude"], ("actual-agent",))
        self.assertEqual(choices["codex"], ("review",))

    def test_discovery_skips_symlinks_fifo_and_unterminated_frontmatter(self):
        agent = self.write("project/.claude/agents/good.md", "---\nname: good\ndescription: Review\n---\n")
        agent.with_name("link.md").symlink_to(agent)
        os.mkfifo(agent.with_name("pipe.md"))
        self.write("project/.claude/agents/broken.md", "---\nname: broken\ndescription: Review\n")
        self.assertEqual(discover_agents(self.project)["claude"], ("good",))

    def test_arguments_remain_literal_and_cannot_inject_options(self):
        self.assertEqual(agent_arguments("opencode", "team/reviewer"), ["--agent", "team/reviewer"])
        self.assertEqual(agent_arguments("claude", "plugin:reviewer"), ["--agent", "plugin:reviewer"])
        self.assertEqual(agent_arguments("codex", "review"), ["--profile", "review"])
        self.assertEqual(agent_arguments("plugin-harness", ""), [])
        for harness, name in (("codex", "../review"), ("claude", "--agent"),
                              ("opencode", "a;id"), ("opencode", "a\nb"),
                              ("codex", "x" * 129), ("plugin-harness", "review")):
            with self.subTest(harness=harness, name=name), self.assertRaises(ValueError):
                agent_arguments(harness, name)


class LaunchPickerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.project = self.root / "project"
        self.project.mkdir()
        self.source = FakeHarnessSource(self.root, enabled=("opencode", "claude", "codex"), adapters={
            "claude": ClaudeHarness(self.root / "claude", "/bin/claude"),
            "codex": CodexHarness(self.root / "codex", "/bin/codex"),
        })
        self.source.snap = make_snapshot(str(self.project))
        self.enterContext(mock.patch("ocdeck.app.discover_agents", return_value={
            "opencode": ("build", "plan"), "claude": ("reviewer",), "codex": ("review",),
        }))

    @asynccontextmanager
    async def running(self, size=(120, 42)):
        app = OCDeckApp(self.source, auto_refresh=False, recent_open_file=self.root / "recent.json")
        with mock.patch.object(app, "_launch_tmux", return_value=True) as launch, \
                mock.patch.object(app, "_run_opencode", return_value=True) as opencode:
            async with app.run_test(size=size) as pilot:
                await pilot.pause()
                app._apply_snapshot(self.source.snap)
                focus_row(app, app.query_one("#agents-table", DataTable), CLAUDE_ID)
                await pilot.pause()
                yield app, pilot, launch, opencode

    async def open_picker(self, app, pilot):
        await pilot.press("S")
        await pilot.pause()
        self.assertIsInstance(app.screen, LaunchPicker)

    async def test_shift_s_launches_each_harness_in_selected_sessions_project(self):
        for harness, agent, flag in (("opencode", "plan", "--agent"),
                                     ("claude", "reviewer", "--agent"), ("codex", "review", "--profile")):
            with self.subTest(harness=harness):
                async with self.running() as (app, pilot, launch, opencode):
                    other = ProjectRecord(id="other", directory=str(self.root), name="Other")
                    app.project_by_id[other.id] = other
                    app.selected_project_id = other.id
                    await self.open_picker(app, pilot)
                    app.screen.query_one("#launch-harness", Select).value = harness
                    await pilot.pause()
                    app.screen.query_one("#launch-agent", Select).value = agent
                    await pilot.pause()
                    await pilot.click("#launch-new")
                    await app.workers.wait_for_complete()
                    if harness == "opencode":
                        self.assertEqual(opencode.call_args.args[0], [str(self.project), flag, agent])
                        self.assertEqual(opencode.call_args.kwargs["project_id"], PROJECT_ID)
                    else:
                        self.assertEqual(launch.call_args.args[1], self.project)
                        self.assertEqual(launch.call_args.args[2][0:3], [f"/bin/{harness}", flag, agent])
                    # Selecting another runtime does not relabel the original session.
                    self.assertEqual(app.session_by_id[CLAUDE_ID].harness, "claude")

    async def test_cancel_launches_nothing_and_preserves_quick_launch_harness(self):
        async with self.running() as (app, pilot, launch, opencode):
            await self.open_picker(app, pilot)
            app.screen.query_one("#launch-harness", Select).value = "codex"
            await pilot.pause()
            await pilot.press("escape")
            await app.workers.wait_for_complete()
            self.assertNotIsInstance(app.screen, LaunchPicker)
            launch.assert_not_called()
            opencode.assert_not_called()
            self.assertEqual(self.source.launch_harness, "opencode")

    async def test_scratch_picker_discovers_and_launches_in_selected_sessions_folder(self):
        self.source.snap = route_projects(self.source.snap, {})
        async with self.running() as (app, pilot, launch, opencode):
            self.assertEqual(app.session_by_id[CLAUDE_ID].project_id, SCRATCH_PROJECT_ID)
            await self.open_picker(app, pilot)
            from ocdeck.app import discover_agents
            self.assertEqual(discover_agents.call_args.args[0], self.project)
            app.screen.query_one("#launch-harness", Select).value = "claude"
            await pilot.pause()
            app.screen.query_one("#launch-agent", Select).value = "reviewer"
            await pilot.click("#launch-new")
            await app.workers.wait_for_complete()
            self.assertEqual(launch.call_args.args[1], self.project)
            self.assertEqual(launch.call_args.args[2][:3], ["/bin/claude", "--agent", "reviewer"])

    async def test_scratch_quick_launch_and_handoff_keep_the_actual_folder(self):
        self.source.snap = route_projects(self.source.snap, {})
        self.source.launch_harness = "codex"
        async with self.running() as (app, pilot, launch, opencode):
            app.selected_project_id = SCRATCH_PROJECT_ID
            app.action_new_session()
            self.assertEqual(launch.call_args.args[1], self.project)
            with mock.patch.object(app, "_handoff_worker") as handoff:
                app.action_handoff_session()
            self.assertEqual(handoff.call_args.args[2].directory, str(self.project))

    async def test_continue_transfers_notes_and_selected_agent(self):
        async with self.running() as (app, pilot, launch, opencode):
            with mock.patch("ocdeck.hub.write_handoff", return_value=(self.root / "notes.md", "fixture continuation")) as handoff:
                await self.open_picker(app, pilot)
                app.screen.query_one("#launch-harness", Select).value = "codex"
                await pilot.pause()
                app.screen.query_one("#launch-agent", Select).value = "review"
                await pilot.click("#launch-continue")
                await app.workers.wait_for_complete()
            self.assertEqual(handoff.call_args.args[0].id, CLAUDE_ID)
            self.assertEqual(handoff.call_args.args[1], "codex")
            self.assertEqual(launch.call_args.args[1], self.project)
            self.assertEqual(launch.call_args.args[2], ["/bin/codex", "--profile", "review", "fixture continuation"])

    async def test_switching_harness_clears_stale_agent_and_custom_input(self):
        async with self.running() as (app, pilot, launch, opencode):
            await self.open_picker(app, pilot)
            app.screen.query_one("#launch-agent", Select).value = "__custom__"
            await pilot.pause()
            custom = app.screen.query_one("#launch-custom", Input)
            custom.value = "--unsafe"
            await pilot.click("#launch-new")
            self.assertIsInstance(app.screen, LaunchPicker)
            launch.assert_not_called()
            opencode.assert_not_called()
            app.screen.query_one("#launch-harness", Select).value = "codex"
            await pilot.pause()
            self.assertEqual(app.screen.query_one("#launch-agent", Select).value, "")
            self.assertEqual(custom.value, "")
            self.assertFalse(custom.display)
            await pilot.press("escape")

    async def test_picker_blocks_underlying_shortcuts_and_works_on_narrow_screen(self):
        async with self.running(size=(80, 32)) as (app, pilot, launch, opencode):
            await self.open_picker(app, pilot)
            app.screen.query_one("#launch-new", Button).focus()
            await pilot.press("n", "a", "o", "N", "B", "H")
            launch.assert_not_called()
            opencode.assert_not_called()
            self.assertEqual(self.source.launch_harness, "opencode")
            await pilot.press("escape")

    async def test_disabled_harness_is_rechecked_when_picker_is_submitted(self):
        async with self.running() as (app, pilot, launch, opencode):
            await self.open_picker(app, pilot)
            self.source.enabled_harnesses = ()
            await pilot.click("#launch-new")
            await app.workers.wait_for_complete()
            launch.assert_not_called()
            opencode.assert_not_called()

    async def test_project_row_disables_continue_and_uses_that_project(self):
        async with self.running() as (app, pilot, launch, opencode):
            app.action_show_tab("overview")
            await pilot.pause()
            table = app.query_one("#projects-table", DataTable)
            table.focus()
            await pilot.pause()
            await self.open_picker(app, pilot)
            self.assertTrue(app.screen.query_one("#launch-continue", Button).disabled)
            await pilot.click("#launch-new")
            await app.workers.wait_for_complete()
            self.assertEqual(opencode.call_args.args[0][0], str(self.project))

    async def test_new_session_key_and_button_offer_the_harness_choice(self):
        # Owner request: starting a session from a project lets you pick the harness.
        for trigger in ("key", "button"):
            with self.subTest(trigger=trigger):
                async with self.running() as (app, pilot, launch, opencode):
                    app.action_show_tab("overview")
                    await pilot.pause()
                    app.query_one("#projects-table", DataTable).focus()
                    await pilot.pause()
                    if trigger == "key":
                        await pilot.press("n")
                    else:
                        app.query_one("#new-session", Button).press()
                    await pilot.pause()
                    self.assertIsInstance(app.screen, LaunchPicker)
                    self.assertTrue(app.screen.query_one("#launch-continue", Button).disabled)
                    launch.assert_not_called()
                    opencode.assert_not_called()
                    app.screen.query_one("#launch-harness", Select).value = "claude"
                    await pilot.pause()
                    await pilot.click("#launch-new")
                    await app.workers.wait_for_complete()
                    self.assertEqual(launch.call_args.args[2][0], "/bin/claude")

    async def test_new_session_launches_directly_when_only_one_harness_is_enabled(self):
        self.source = FakeHarnessSource(self.root, enabled=("opencode",), adapters={})
        self.source.snap = make_snapshot(str(self.project))
        async with self.running() as (app, pilot, launch, opencode):
            app.action_show_tab("overview")
            await pilot.pause()
            app.query_one("#projects-table", DataTable).focus()
            await pilot.pause()
            await pilot.press("n")
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertNotIsInstance(app.screen, LaunchPicker)
            self.assertEqual(opencode.call_args.args[0][0], str(self.project))

    async def test_typing_shift_s_in_search_does_not_open_picker(self):
        async with self.running() as (app, pilot, launch, opencode):
            app.action_search()
            await pilot.press("S")
            await pilot.pause()
            self.assertIsInstance(app.focused, Input)
            self.assertIn("S", app.focused.value)
            self.assertNotIsInstance(app.screen, LaunchPicker)
            launch.assert_not_called()
            opencode.assert_not_called()

    async def test_button_opens_picker_and_privacy_hides_project_name(self):
        async with self.running() as (app, pilot, launch, opencode):
            app.private = True
            await pilot.click("#choose-launch")
            await pilot.pause()
            self.assertIsInstance(app.screen, LaunchPicker)
            self.assertEqual(app.screen.project_label, "Selected project")
            await pilot.press("escape")
            launch.assert_not_called()
            opencode.assert_not_called()

    async def test_next_tab_and_empty_harness_selection_do_not_open_picker(self):
        async with self.running() as (app, pilot, launch, opencode):
            await pilot.press("5", "S")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, LaunchPicker)
            await pilot.press("4")
            self.source.enabled_harnesses = ()
            await pilot.press("S")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, LaunchPicker)
            launch.assert_not_called()
            opencode.assert_not_called()

    async def test_v2_creation_and_rollback_keep_agent_selection(self):
        self.source.create_session = mock.AsyncMock(return_value=("ses_fixture", ""))
        self.source.remove_session = mock.AsyncMock(return_value="")
        async with self.running() as (app, pilot, launch, opencode):
            opencode.return_value = False
            await self.open_picker(app, pilot)
            app.screen.query_one("#launch-agent", Select).value = "plan"
            await pilot.click("#launch-new")
            await app.workers.wait_for_complete()
            self.source.create_session.assert_awaited_once_with(self.project)
            self.assertEqual(opencode.call_args.args[0], [str(self.project), "--session", "ses_fixture", "--agent", "plan"])
            self.source.remove_session.assert_awaited_once_with("ses_fixture")


if __name__ == "__main__":
    unittest.main()
