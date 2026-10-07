import asyncio
import json
import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from ocdeck.harnesses import (
    ClaudeHarness, CodexHarness, GitProbe, LiveProcess, MultiHarnessSource,
    load_harness_settings, merge_harness_sessions, resolve_enabled_harnesses,
    save_harness_settings, split_session_key,
)
from unittest import mock

from ocdeck.harnesses import load_browser_grants, save_browser_grants
from ocdeck.models import DashboardSnapshot, ProjectRecord, SessionRecord


def write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")


def claude_transcript(root: Path, session_id: str, cwd: str, *, title: str = "") -> Path:
    entries = [
        {"type": "user", "cwd": cwd, "sessionId": session_id, "timestamp": "2026-09-26T10:00:00Z",
         "gitBranch": "main", "message": {"role": "user", "content": "<command-name>/login</command-name>"}},
        {"type": "user", "cwd": cwd, "sessionId": session_id, "timestamp": "2026-09-26T10:00:01Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "fix the deck bugs"}]}},
        {"type": "assistant", "cwd": cwd, "timestamp": "2026-09-26T10:05:00Z",
         "message": {"model": "claude-opus-5-5", "content": [{"type": "text", "text": "done"}]}},
        {"type": "last-prompt", "lastPrompt": "now add harnesses", "sessionId": session_id},
    ]
    if title:
        entries.append({"type": "ai-title", "aiTitle": "generated"})
        entries.append({"type": "custom-title", "customTitle": title})
    path = root / cwd.replace("/", "-") / f"{session_id}.jsonl"
    write_jsonl(path, entries)
    return path


class SettingsTests(unittest.TestCase):
    def test_defaults_are_auto_and_invalid_values_are_ignored(self):
        with tempfile.TemporaryDirectory() as base:
            path = Path(base) / "harnesses.json"
            self.assertEqual(load_harness_settings(path), {"opencode": "auto", "claude": "auto", "codex": "auto"})
            path.write_text(json.dumps({"opencode": False, "claude": "sometimes", "bogus": "on"}))
            self.assertEqual(load_harness_settings(path)["opencode"], "off")
            self.assertEqual(load_harness_settings(path)["claude"], "auto")
            path.write_text("{not json")
            self.assertEqual(load_harness_settings(path)["codex"], "auto")

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as base:
            path = Path(base) / "nested/harnesses.json"
            save_harness_settings({"opencode": "off", "claude": "on"}, path)
            self.assertEqual(load_harness_settings(path), {"opencode": "off", "claude": "on", "codex": "auto"})

    def test_auto_follows_installed_binaries_and_override_wins(self):
        installed = {"claude"}
        which = lambda harness: f"/bin/{harness}" if harness in installed else None
        settings = {"opencode": "auto", "claude": "auto", "codex": "on"}
        self.assertEqual(resolve_enabled_harnesses(settings, which=which), ("claude", "codex"))
        settings["claude"] = "off"
        self.assertEqual(resolve_enabled_harnesses(settings, which=which), ("codex",))
        self.assertEqual(resolve_enabled_harnesses(settings, ["claude", "OpenCode"], which=which),
                         ("opencode", "claude"))
        with self.assertRaises(ValueError):
            resolve_enabled_harnesses(settings, ["cursor"], which=which)

    def test_session_keys(self):
        self.assertEqual(split_session_key("claude:abc"), ("claude", "abc"))
        self.assertEqual(split_session_key("ses_abc"), ("opencode", "ses_abc"))
        self.assertEqual(split_session_key("weird:abc"), ("opencode", "weird:abc"))


class ClaudeAdapterTests(unittest.TestCase):
    def test_transcript_metadata_and_title_precedence(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            claude_transcript(root, "aaa", "/work/alpha", title="My rename")
            claude_transcript(root, "bbb", "/work/beta")
            write_jsonl(root / "-work-alpha/aaa/subagents/agent-1.jsonl", [{"type": "user", "cwd": "/x"}])
            (root / "-work-alpha/broken.jsonl").write_text("not json\n")
            sessions = {s.id: s for s in ClaudeHarness(root, "claude").collect(processes=[], tmux={})}
            # A helper agent's transcript is never a top-level session; it nests under its parent.
            self.assertEqual({k for k, v in sessions.items() if not v.parent_id}, {"claude:aaa", "claude:bbb"})
            self.assertEqual(sessions["claude:agent-1"].parent_id, "claude:aaa")
            alpha = sessions["claude:aaa"]
            self.assertEqual(alpha.title, "My rename")
            self.assertEqual(sessions["claude:bbb"].title, "fix the deck bugs")
            self.assertEqual(alpha.last_prompt, "now add harnesses")
            self.assertEqual(alpha.directory, "/work/alpha")
            self.assertEqual(alpha.model, "claude-opus-5-5")
            self.assertEqual(alpha.harness, "claude")
            self.assertEqual((alpha.status, alpha.instance_count), ("idle", 0))

    def test_live_process_mapping_by_flag_cwd_and_tmux(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            claude_transcript(root, "aaa", "/work/alpha")
            fresh = claude_transcript(root, "bbb", "/work/beta")
            now = time.time()
            os.utime(fresh, (now, now))
            adapter = ClaudeHarness(root, "claude")
            processes = [
                LiveProcess(1, "/elsewhere", ("claude", "--resume", "aaa"), "/dev/pts/3"),
                LiveProcess(2, "/work/beta", ("claude",), ""),
            ]
            tmux = {"shell": (True, ("/dev/pts/3",)), "cc-bbb": (False, ("/dev/pts/9",))}
            sessions = {s.id: s for s in adapter.collect(processes=processes, tmux=tmux, now=now + 60)}
            self.assertEqual(sessions["claude:aaa"].terminals, ("shell",))
            self.assertTrue(sessions["claude:aaa"].terminal_attached)
            self.assertEqual(sessions["claude:bbb"].terminals, ("cc-bbb",))
            self.assertEqual(sessions["claude:bbb"].instance_count, 1)

    def test_turn_boundaries_drive_running_and_waiting_states(self):
        from ocdeck.models import agent_state
        with tempfile.TemporaryDirectory() as base:
            path = claude_transcript(Path(base), "aaa", "/work/alpha")
            adapter = ClaudeHarness(Path(base), "claude")
            live = [LiveProcess(1, "/work/alpha", ("claude", "-r", "aaa"))]
            # Prompt + assistant reply, no turn end yet: the turn is running.
            running = adapter.collect(processes=live, tmux={})[0]
            self.assertEqual((running.status, agent_state(running)), ("busy", "busy"))
            with path.open("a") as handle:
                handle.write(json.dumps({"type": "system", "subtype": "turn_duration",
                                         "timestamp": "2026-09-26T10:06:00Z"}) + "\n")
            waiting = adapter.collect(processes=live, tmux={})[0]
            self.assertEqual(waiting.status, "idle")
            self.assertGreaterEqual(waiting.assistant_done_ms, waiting.last_interaction_ms)
            self.assertEqual(agent_state(waiting, now_ms=waiting.assistant_done_ms + 1000), "review")
            # A background notification restarts work without a typed prompt.
            with path.open("a") as handle:
                handle.write(json.dumps({"type": "user", "timestamp": "2026-09-26T10:07:00Z",
                                         "message": {"content": "<task-notification>done</task-notification>"}}) + "\n")
            self.assertEqual(adapter.collect(processes=live, tmux={})[0].status, "busy")
            # Nothing runs without a live process, whatever the transcript says.
            self.assertEqual(adapter.collect(processes=[], tmux={})[0].status, "idle")

    def test_commands(self):
        adapter = ClaudeHarness(Path("/nonexistent"), "/bin/claude")
        # Never your own Chrome: every OC Deck launch disables Claude in Chrome.
        self.assertEqual(adapter.resume_command("aaa", "/w"), ["/bin/claude", "--no-chrome", "--resume", "aaa"])
        command, session_id = adapter.new_command("/w", "continue from handoff")
        self.assertEqual(command, ["/bin/claude", "--no-chrome", "--session-id", session_id, "continue from handoff"])
        self.assertEqual(len(session_id), 36)
        self.assertEqual(adapter.collect(processes=[], tmux={}), [])


class CodexAdapterTests(unittest.TestCase):
    def test_modern_and_legacy_rollouts(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            write_jsonl(root / "2026/09/26/rollout-2026-09-26T10-00-00-11111111-2222-3333-4444-555555555555.jsonl", [
                {"type": "session_meta", "timestamp": "2026-09-26T10:00:00Z",
                 "payload": {"id": "11111111-2222-3333-4444-555555555555", "cwd": "/work/alpha",
                             "timestamp": "2026-09-26T10:00:00Z"}},
                {"type": "response_item", "timestamp": "2026-09-26T10:00:01Z",
                 "payload": {"type": "message", "role": "user",
                             "content": [{"type": "input_text", "text": "<environment_context>x</environment_context>"}]}},
                {"type": "event_msg", "timestamp": "2026-09-26T10:00:02Z",
                 "payload": {"type": "user_message", "message": "port the parser"}},
                {"type": "turn_context", "timestamp": "2026-09-26T10:00:03Z",
                 "payload": {"cwd": "/work/alpha", "model": "gpt-6-codex"}},
                {"type": "event_msg", "timestamp": "2026-09-26T10:09:00Z",
                 "payload": {"type": "user_message", "message": "now test it"}},
            ])
            write_jsonl(root / "2025/01/01/rollout-2025-01-01T00-00-00-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl", [
                {"id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "timestamp": "2025-01-01T00:00:00Z"},
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "no cwd here"}]},
            ])
            adapter = CodexHarness(root, "/bin/codex")
            sessions = adapter.collect(processes=[
                LiveProcess(5, "/work/alpha", ("codex", "resume", "11111111-2222-3333-4444-555555555555")),
            ], tmux={})
            self.assertEqual(len(sessions), 1)  # the legacy rollout has no cwd
            session = sessions[0]
            self.assertEqual(session.id, "codex:11111111-2222-3333-4444-555555555555")
            self.assertEqual((session.title, session.last_prompt), ("port the parser", "now test it"))
            self.assertEqual(session.model, "gpt-6-codex")
            self.assertEqual(session.instance_count, 1)
            self.assertEqual(adapter.resume_command("x", "/w"), ["/bin/codex", "resume", "x"])


class AgentBrowserTests(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.TemporaryDirectory()
        home = Path(self.base.name)
        patcher = mock.patch.dict(os.environ, {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.base.cleanup)
        self.launcher = home / ".local/lib/opencode/playwright-mcp-v1"
        self.launcher.parent.mkdir(parents=True)
        self.launcher.write_text("#!/bin/sh\n")
        self.launcher.chmod(0o755)
        (home / ".config/opencode").mkdir(parents=True)
        self.browser_config = home / ".config/opencode/agent-browser.json"
        self.browser_config.write_text("{}")

    def test_claude_gets_a_per_session_mcp_config_before_variadic_boundary(self):
        adapter = ClaudeHarness(Path(self.base.name), "/bin/claude")
        self.assertEqual(adapter.resume_command("aaa", "/w"), ["/bin/claude", "--no-chrome", "--resume", "aaa"])
        command = adapter.resume_command("aaa", "/w", browser=True)
        self.assertEqual(command[1:3], ["--no-chrome", "--mcp-config"])
        self.assertEqual(command[4:], ["--resume", "aaa"])
        config_file = Path(command[3])
        self.assertEqual(config_file.stat().st_mode & 0o777, 0o600)
        server = json.loads(config_file.read_text())["mcpServers"]["agent_browser"]
        self.assertEqual(server["command"], str(self.launcher))
        self.assertEqual(server["env"], {"OPENCODE_AGENT_BROWSER_CONFIG": str(self.browser_config)})
        new, session_id = adapter.new_command("/w", "hello", browser=True)
        self.assertEqual(new[-3:], ["--session-id", session_id, "hello"])

    def test_codex_uses_config_overrides(self):
        command = CodexHarness(Path(self.base.name), "/bin/codex").resume_command("x", "/w", browser=True)
        self.assertEqual(command[0], "/bin/codex")
        self.assertEqual(command[-2:], ["resume", "x"])
        self.assertIn(f'mcp_servers.agent_browser.command="{self.launcher}"', command)
        self.assertIn(
            f'mcp_servers.agent_browser.env.OPENCODE_AGENT_BROWSER_CONFIG="{self.browser_config}"', command
        )

    def test_missing_browser_install_adds_nothing(self):
        self.launcher.unlink()
        self.assertEqual(ClaudeHarness(Path(self.base.name), "c").resume_command("a", "/w", browser=True),
                         ["c", "--no-chrome", "--resume", "a"])

    def test_grants_mark_sessions_browser_enabled(self):
        root = Path(self.base.name) / "projects"
        claude_transcript(root, "aaa", "/work/alpha")
        claude_transcript(root, "bbb", "/work/beta")
        save_browser_grants({"claude:aaa"})
        self.assertEqual(load_browser_grants(), {"claude:aaa"})
        sessions = {s.id: s for s in ClaudeHarness(root, "c").collect(processes=[], tmux={})}
        self.assertTrue(sessions["claude:aaa"].browser_enabled)
        self.assertFalse(sessions["claude:bbb"].browser_enabled)


class MergeTests(unittest.TestCase):
    def test_sessions_join_matching_projects_or_create_one(self):
        snapshot = DashboardSnapshot(projects=(
            ProjectRecord("p1", "/work", "work"),
            ProjectRecord("p2", "/work/alpha", "alpha", session_count=2),
        ))
        sessions = [
            SessionRecord("claude:a", "A", "/work/alpha/src", "", 1, 50, status="busy", harness="claude"),
            SessionRecord("claude:b", "B", "/elsewhere/proj", "", 1, 99, harness="claude"),
            SessionRecord("claude:c", "C", "/elsewhere/proj", "", 1, 10, harness="claude"),
        ]
        git = GitProbe()
        git._cache.update({  # keep the test hermetic
            path: (time.monotonic(), "", "", -1)
            for path in ("/work", "/work/alpha", "/work/alpha/src", "/elsewhere/proj")
        })
        merged = merge_harness_sessions(snapshot, sessions, git)
        by_id = {s.id: s for s in merged.sessions}
        self.assertEqual(by_id["claude:a"].project_id, "p2")
        self.assertEqual(by_id["claude:b"].project_id, by_id["claude:c"].project_id)
        projects = {p.id: p for p in merged.projects}
        self.assertEqual((projects["p2"].session_count, projects["p2"].active_count), (3, 1))
        created = projects[by_id["claude:b"].project_id]
        self.assertEqual((created.directory, created.name, created.session_count), ("/elsewhere/proj", "proj", 2))
        # The source's project order is kept; harness-only projects are appended.
        self.assertEqual([p.id for p in merged.projects][:2], ["p1", "p2"])
        self.assertEqual(merged.projects[-1].id, created.id)


class FakeAdapter:
    def __init__(self, harness, sessions=None, error=None):
        self.harness, self.sessions, self.error = harness, sessions or [], error

    def collect(self):
        if self.error:
            raise self.error
        return list(self.sessions)


class FakeOpenCode:
    backend = "v2"
    opencode_bin = "/bin/opencode2"
    api_url = "http://x"

    def __init__(self):
        self.calls = 0

    async def collect(self):
        self.calls += 1
        return DashboardSnapshot(connection="offline", connection_detail="OpenCode API unavailable")


class MultiSourceTests(unittest.TestCase):
    def session(self, harness="claude"):
        return SessionRecord(f"{harness}:1", "t", "/tmp", "", 1, 2, harness=harness)

    def test_opencode_disabled_is_never_called_and_deck_stays_live(self):
        opencode = FakeOpenCode()
        source = MultiHarnessSource(opencode, [FakeAdapter("claude", [self.session()])], opencode_enabled=False)
        snapshot = asyncio.run(source.collect())
        self.assertEqual(opencode.calls, 0)
        self.assertEqual(snapshot.connection, "live")
        self.assertEqual([s.id for s in snapshot.sessions], ["claude:1"])
        self.assertIsNone(source.opencode_bin)
        self.assertEqual(source.enabled_harnesses, ("claude",))
        self.assertEqual(source.api_url, "http://x")  # falls through for OpenCode-only code paths

    def test_failing_adapter_degrades_to_warning(self):
        source = MultiHarnessSource(
            None, [FakeAdapter("claude", error=OSError("boom")), FakeAdapter("codex", [self.session("codex")])],
            opencode_enabled=False,
        )
        snapshot = asyncio.run(source.collect())
        self.assertEqual(snapshot.connection, "live")
        self.assertIn("Claude Code unavailable", snapshot.warning)
        self.assertEqual([s.id for s in snapshot.sessions], ["codex:1"])
        everything_down = MultiHarnessSource(None, [FakeAdapter("claude", error=OSError())], opencode_enabled=False)
        self.assertEqual(asyncio.run(everything_down.collect()).connection, "offline")

    def test_opencode_enabled_keeps_its_connection_state(self):
        source = MultiHarnessSource(FakeOpenCode(), [FakeAdapter("claude", [self.session()])])
        snapshot = asyncio.run(source.collect())
        self.assertEqual(snapshot.connection, "offline")
        self.assertIn("Claude Code", snapshot.connection_detail)
        self.assertEqual(source.enabled_harnesses, ("opencode", "claude"))


if __name__ == "__main__":
    unittest.main()


class LineageTests(unittest.TestCase):
    def test_env_stamp_and_process_tree_nest_headless_children(self):
        from ocdeck.harnesses import ProcessInfo, apply_lineage, load_lineage, observe_lineage
        parent = SessionRecord("claude:p1", "parent", "/w", "", 1, 2, harness="claude")
        stamped = SessionRecord("ses_a", "stamped child", "/w/a", "x", 10_000, 11, harness="opencode")
        treed = SessionRecord("ses_b", "tree child", "/w/b", "x", 20_000, 21, harness="opencode")
        stranger = SessionRecord("ses_c", "human run", "/w/c", "x", 30_000, 31, harness="opencode")
        table = {
            50: ProcessInfo(50, 1, 0, "/w", ("claude", "--resume", "p1")),
            60: ProcessInfo(60, 1, 9_000, "/w/a", ("/opt/x/opencode2", "run", "-m", "m", "go")),
            70: ProcessInfo(70, 50, 19_000, "/w/b", ("opencode2", "run", "go")),
            80: ProcessInfo(80, 1, 29_000, "/w/c", ("opencode2", "run", "go")),
            90: ProcessInfo(90, 50, 0, "/w/c", ("opencode2", "/w/c")),  # a TUI, not headless
        }
        stamps = {60: "claude:p1"}
        found = observe_lineage([parent, stamped, treed, stranger], {50: "claude:p1"}, table,
                                stamp=lambda pid: stamps.get(pid, ""))
        self.assertEqual(found, {"ses_a": "claude:p1", "ses_b": "claude:p1"})
        with tempfile.TemporaryDirectory() as base:
            path = Path(base) / "lineage.json"
            snapshot = DashboardSnapshot(sessions=(parent, stamped, treed, stranger))
            with mock.patch("ocdeck.harnesses.parent_stamp", side_effect=lambda pid: stamps.get(pid, "")):
                nested = apply_lineage(snapshot, {50: "claude:p1"}, path, table)
            by_id = {s.id: s for s in nested.sessions}
            self.assertEqual(by_id["ses_a"].agent_parent_id, "claude:p1")
            self.assertEqual(by_id["ses_c"].agent_parent_id, "")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            # Remembered after the helper exits (no processes at all).
            later = apply_lineage(snapshot, {}, path, {})
            self.assertEqual({s.id: s.agent_parent_id for s in later.sessions}["ses_b"], "claude:p1")
            self.assertEqual(load_lineage(path), {"ses_a": "claude:p1", "ses_b": "claude:p1"})

    def test_stamp_reads_only_the_session_key_and_validates_it(self):
        from ocdeck.harnesses import parent_stamp
        with tempfile.TemporaryDirectory() as base:
            proc = Path(base)
            (proc / "5").mkdir()
            (proc / "5" / "environ").write_bytes(
                b"SECRET_TOKEN=abc\0CLAUDE_CODE_SESSION_ID=5906cf70-e7fd\0")
            self.assertEqual(parent_stamp(5, proc), "claude:5906cf70-e7fd")
            (proc / "6").mkdir()
            (proc / "6" / "environ").write_bytes(b"CLAUDE_CODE_SESSION_ID=../../etc\0")
            self.assertEqual(parent_stamp(6, proc), "")
            self.assertEqual(parent_stamp(7, proc), "")

    def test_existing_parents_are_never_overridden(self):
        from ocdeck.harnesses import ProcessInfo, observe_lineage
        child = SessionRecord("ses_a", "c", "/w", "x", 10_000, 11, parent_id="ses_real", harness="opencode")
        table = {60: ProcessInfo(60, 1, 9_000, "/w", ("opencode2", "run"))}
        self.assertEqual(observe_lineage([child], {}, table, stamp=lambda pid: "claude:p1"), {})


class CodexTitleTests(unittest.TestCase):
    def test_thread_names_from_the_session_index_win_over_the_first_prompt(self):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base) / "sessions"
            write_jsonl(root / "2026/09/26/rollout-2026-09-26T10-00-00-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl", [
                {"type": "session_meta", "payload": {"id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "cwd": "/w"}},
                {"type": "event_msg", "payload": {"type": "user_message", "message": "a long pasted first prompt"}},
            ])
            adapter = CodexHarness(root, "/bin/codex")
            self.assertEqual(adapter.collect(processes=[], tmux={})[0].title, "a long pasted first prompt")
            write_jsonl(Path(base) / "session_index.jsonl", [
                {"id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "thread_name": "Old name"},
                {"id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "thread_name": "Continue OC Deck security work"},
            ])
            self.assertEqual(adapter.collect(processes=[], tmux={})[0].title, "Continue OC Deck security work")
            (Path(base) / "session_index.jsonl").write_text("not json\n")
            self.assertEqual(adapter.collect(processes=[], tmux={})[0].title, "a long pasted first prompt")



class ProjectRoutingTests(unittest.TestCase):
    def snapshot(self):
        from ocdeck.models import DashboardSnapshot, ProjectRecord
        projects = (
            ProjectRecord("deck", "/home/u/agents_start/dashboard", "OC Deck"),
            ProjectRecord("start", "/home/u/agents_start", "Agents Start"),
            ProjectRecord("wt1", "/tmp/scratch/wt1", "wt1"),
        )
        sessions = (
            SessionRecord("claude:me", "me", "/home/u/agents_start", "start", 1, 2, harness="claude"),
            SessionRecord("ses_helper", "helper", "/tmp/scratch/wt1", "wt1", 1, 2, agent_parent_id="claude:me"),
            SessionRecord("ses_orphan", "orphan", "/tmp/scratch/wt2", "wt2", 1, 2),
            SessionRecord("ses_other", "other", "/home/u/agents_start", "start", 1, 2),
        )
        return DashboardSnapshot(sessions=sessions, projects=projects)

    def test_routes_parents_and_scratch(self):
        from ocdeck.harnesses import SCRATCH_PROJECT_ID, route_projects
        routed = route_projects(self.snapshot(), {"claude:me": "OC Deck"})
        by_id = {s.id: s.project_id for s in routed.sessions}
        self.assertEqual(by_id["claude:me"], "deck")              # explicit route
        self.assertEqual(by_id["ses_helper"], "deck")             # follows its parent
        self.assertEqual(by_id["ses_orphan"], SCRATCH_PROJECT_ID)  # temp folder, no parent
        self.assertEqual(by_id["ses_other"], "start")             # untouched
        names = [p.name for p in routed.projects]
        self.assertNotIn("wt1", names)  # empty temp-folder project disappears
        self.assertIn("Scratch", names)
        self.assertEqual({s.directory for s in routed.sessions if s.id == "claude:me"},
                         {"/home/u/agents_start"})  # grouping never changes the folder

    def test_unknown_route_targets_and_cycles_are_harmless(self):
        from ocdeck.harnesses import route_projects
        snapshot = self.snapshot()
        looped = tuple(replace(s, agent_parent_id="ses_other") if s.id == "claude:me" else
                       replace(s, agent_parent_id="claude:me") if s.id == "ses_other" else s
                       for s in snapshot.sessions)
        routed = route_projects(replace(snapshot, sessions=looped), {"claude:me": "No Such Project"})
        self.assertEqual({s.id: s.project_id for s in routed.sessions}["claude:me"], "start")

    def test_routes_file_round_trip_is_private(self):
        from ocdeck.harnesses import load_routes, save_route
        with tempfile.TemporaryDirectory() as base:
            path = Path(base) / "routes.json"
            save_route("claude:me", "OC Deck", path)
            save_route("codex:x", "OC Deck", path)
            save_route("codex:x", "", path)  # clearing a route
            self.assertEqual(load_routes(path), {"claude:me": "OC Deck"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            path.write_text("[1, 2]")
            self.assertEqual(load_routes(path), {})

    def test_resume_runs_in_the_original_folder_when_shown_elsewhere(self):
        with tempfile.TemporaryDirectory() as base:
            original = Path(base) / "agents_start"
            original.mkdir()
            claude_transcript(Path(base) / "projects", "aaa", str(original))
            adapter = ClaudeHarness(Path(base) / "projects", "/bin/claude")
            adapter.collect(processes=[], tmux={})
            self.assertEqual(adapter.resume_command("aaa", str(original)), ["/bin/claude", "--no-chrome", "--resume", "aaa"])
            self.assertEqual(adapter.resume_command("aaa", "/elsewhere"),
                             ["/usr/bin/env", "-C", str(original), "/bin/claude", "--no-chrome", "--resume", "aaa"])
