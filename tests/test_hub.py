"""Tests for the cross-harness hub: init, dry-run/apply sync, handoff notes."""

import contextlib
import io
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from ocdeck import hub
from ocdeck.harnesses import load_harness_settings
from ocdeck.hub import (
    CLAUDE_BEGIN, CLAUDE_END, CODEX_BEGIN, CODEX_END, INDEX_SERVER, main,
    plan_actions, parse_jsonc, strip_jsonc, write_handoff,
)
from ocdeck.models import SessionRecord

OPENCODE_CONFIG = """{
  // OpenCode config with comments
  "$schema": "https://opencode.ai/config.json",
  "model": "openai/gpt-5.6-sol",
  "mcp": {
    "signed_in_tabs": {
      "type": "local",
      "command": ["/usr/bin/playwright-mcp"],
      "environment": {"OPENCODE_AGENT_BROWSER_CONFIG": "/cfg/browser.json"},
      "enabled": true,
    },
    "docs": {
      "type": "local",
      "command": ["/usr/bin/docs-mcp"],
      "environment": {"DOCS_TOKEN_FILE": "/cfg/docs"},
    },
    "remote_thing": {"type": "remote", "url": "https://example.invalid/mcp"},
    "sleepy": {"type": "local", "command": ["sleep", "1"], "enabled": false},
  },
}
"""


def load_hub_json(hub_dir: Path) -> dict:
    return json.loads((hub_dir / "hub.json").read_text(encoding="utf-8"))


class FakeRunner:
    """Stand-in for ``subprocess.run`` that mimics ``claude mcp add-json``."""

    def __init__(self, settings_path: Path) -> None:
        self.settings_path = settings_path
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        argv = [str(part) for part in argv]
        self.calls.append(argv)
        if len(argv) > 2 and argv[1:3] == ["mcp", "add-json"]:
            payload = json.loads(argv[-1])
            try:
                data = json.loads(self.settings_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            data.setdefault("mcpServers", {})[argv[-2]] = payload
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            self.settings_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    @property
    def server_names(self) -> list[str]:
        return [call[-2] for call in self.calls if call[1:3] == ["mcp", "add-json"]]


class HubCase(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.home = self.base / "home"
        self.home.mkdir()
        self.hub_dir = self.base / "hub"
        self.xdg = self.base / "xdg"
        self.opencode_config = self.home / ".config" / "opencode" / "opencode.jsonc"
        self.claude_md = self.home / ".claude" / "CLAUDE.md"
        self.claude_json = self.home / ".claude.json"
        self.codex_dir = self.home / ".codex"
        self.runner = FakeRunner(self.claude_json)
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.xdg),
            "OCDECK_HUB_DIR": str(self.hub_dir),
            "OCDECK_OPENCODE_CONFIG": str(self.opencode_config),
        }
        self.enterContext(patch.dict(os.environ, environment, clear=True))
        self.enterContext(patch.object(hub, "RUNNER", self.runner))

    def write_opencode_config(self, text: str = OPENCODE_CONFIG) -> None:
        self.opencode_config.parent.mkdir(parents=True, exist_ok=True)
        self.opencode_config.write_text(text, encoding="utf-8")

    def enable(self, *harnesses: str) -> None:
        for name in ("opencode", "claude", "codex"):
            self.assertEqual(main(["harness", "on" if name in harnesses else "off", name]), 0)

    def init(self) -> None:
        self.assertEqual(main(["init"]), 0)

    def opencode_mcp_keys(self) -> set[str]:
        return set(parse_jsonc(self.opencode_config.read_text())["mcp"])


class JsoncTests(unittest.TestCase):
    def test_comments_trailing_commas_and_comment_like_strings(self):
        text = (
            "{\n"
            "  // line comment\n"
            "  \"url\": \"http://x//y\", /* block\n comment */\n"
            "  \"mcp\": {\n"
            '    "a": {"command": ["x"], "note": "trailing, }",},  // tail\n'
            "  },\n"
            "}\n"
        )
        self.assertEqual(strip_jsonc(text).count("//"), 2)  # only the ones inside "http://x//y"
        payload = parse_jsonc(text)
        self.assertEqual(payload["url"], "http://x//y")
        self.assertEqual(payload["mcp"]["a"]["command"], ["x"])
        self.assertEqual(payload["mcp"]["a"]["note"], "trailing, }")

    def test_escaped_quotes_do_not_end_the_string(self):
        payload = parse_jsonc('{"a": "say \\"hi\\" // not a comment", /* c */}')
        self.assertEqual(payload["a"], 'say "hi" // not a comment')


class InitTests(HubCase):
    def test_init_imports_local_servers_and_never_overwrites(self):
        self.write_opencode_config()
        self.assertEqual(main(["init"]), 0)
        config = json.loads((self.hub_dir / "hub.json").read_text())
        self.assertEqual(config["instructions"], str(self.hub_dir / "AGENTS.md"))
        self.assertIn("# Shared instructions", (self.hub_dir / "AGENTS.md").read_text())
        # The signed-in agent browser is per-session only and is never imported.
        self.assertEqual(list(config["mcp"]), ["docs", INDEX_SERVER])
        self.assertEqual(config["mcp"]["docs"]["env"], {"DOCS_TOKEN_FILE": "/cfg/docs"})
        self.assertEqual(config["mcp"][INDEX_SERVER]["command"], [sys.executable, "-m", "ocdeck.index_server"])
        (self.hub_dir / "hub.json").write_text('{"instructions": "/keep/me", "mcp": {}}\n')
        self.assertEqual(main(["init"]), 0)
        self.assertIn("/keep/me", (self.hub_dir / "hub.json").read_text())

    def test_init_adopts_the_opencode_agents_symlink_target(self):
        shared = self.home / "config" / "AGENTS.md"
        shared.parent.mkdir(parents=True)
        shared.write_text("# Team rules\n", encoding="utf-8")
        link = self.home / ".config" / "opencode" / "AGENTS.md"
        link.parent.mkdir(parents=True)
        link.symlink_to(shared)
        self.write_opencode_config()
        self.init()
        config = json.loads((self.hub_dir / "hub.json").read_text())
        self.assertEqual(config["instructions"], str(shared.resolve()))
        self.assertFalse((self.hub_dir / "AGENTS.md").exists())

    def test_init_works_without_an_opencode_config(self):
        self.init()
        config = json.loads((self.hub_dir / "hub.json").read_text())
        self.assertEqual(list(config["mcp"]), [INDEX_SERVER])
        self.assertEqual(stat.S_IMODE((self.hub_dir / "hub.json").stat().st_mode), 0o600)


class StatusTests(HubCase):
    def test_status_lists_harnesses_hub_and_plan(self):
        self.write_opencode_config()
        self.init()
        self.enable("claude")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["status"]), 0)
        text = output.getvalue()
        self.assertIn("harnesses: Claude Code", text)
        self.assertIn(f"hub: {self.hub_dir}", text)
        self.assertIn(str(self.claude_md), text)
        self.assertIn("dry run", text)

    def test_unknown_harness_is_rejected(self):
        self.assertEqual(main(["harness", "on", "cursor"]), 2)
        self.assertEqual(load_harness_settings()["claude"], "auto")


class SyncTests(HubCase):
    def test_dry_run_changes_nothing(self):
        self.write_opencode_config()
        self.init()
        self.enable("claude", "codex", "opencode")
        self.claude_md.parent.mkdir(parents=True)
        self.claude_md.write_text("# My notes\n", encoding="utf-8")
        plan = plan_actions()
        self.assertTrue(plan)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["sync"]), 0)
        self.assertIn("nothing was written", output.getvalue())
        self.assertEqual(self.claude_md.read_text(), "# My notes\n")
        self.assertFalse((self.home / ".codex" / "AGENTS.md").exists())
        self.assertEqual(self.runner.calls, [])

    def test_apply_writes_every_harness_and_is_idempotent(self):
        self.write_opencode_config()
        self.init()
        self.enable("claude", "codex", "opencode")
        self.claude_md.parent.mkdir(parents=True)
        self.claude_md.write_text("# My notes\n", encoding="utf-8")
        self.codex_dir.mkdir(parents=True)
        (self.codex_dir / "config.toml").write_text(
            'model = "gpt-5"\n'
            "\n"
            "[mcp_servers.hand_rolled]\n"
            'command = "uvx"\n'
            'args = ["hand"]\n',
            encoding="utf-8",
        )
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["sync", "--apply"]), 0)
        self.assertIn("applied", output.getvalue())

        claude_md = self.claude_md.read_text()
        self.assertIn("# My notes", claude_md)
        self.assertIn(f"{CLAUDE_BEGIN}\n@{self.hub_dir / 'AGENTS.md'}\n{CLAUDE_END}", claude_md)

        codex_md = (self.codex_dir / "AGENTS.md").read_text()
        self.assertIn(f"{CODEX_BEGIN}\n# Shared instructions", codex_md)
        self.assertIn(CODEX_END, codex_md)
        toml = (self.codex_dir / "config.toml").read_text()
        self.assertIn(CODEX_END, toml)
        self.assertIn("[mcp_servers.hand_rolled]", toml)
        self.assertEqual(toml.count("[mcp_servers.hand_rolled]"), 1)
        self.assertIn(f"[mcp_servers.{INDEX_SERVER}]", toml)
        self.assertIn('command = "%s"' % sys.executable, toml)
        self.assertIn('args = ["-m", "ocdeck.index_server"]', toml)
        self.assertIn("[mcp_servers.docs.env]", toml)
        self.assertNotIn("signed_in_tabs", toml)
        self.assertIn('DOCS_TOKEN_FILE = "/cfg/docs"', toml)
        self.assertNotIn("OPENCODE_AGENT_BROWSER_CONFIG", toml)
        self.assertEqual(self.runner.server_names, sorted([INDEX_SERVER, "docs"]))

        payload = parse_jsonc(self.opencode_config.read_text())
        self.assertIn(INDEX_SERVER, payload["mcp"])
        self.assertIn("docs", payload["mcp"])
        self.assertEqual(payload["mcp"][INDEX_SERVER]["type"], "local")
        self.assertTrue(payload["mcp"][INDEX_SERVER]["enabled"])
        self.assertEqual(
            payload["mcp"]["docs"]["environment"],
            {"DOCS_TOKEN_FILE": "/cfg/docs"},
        )
        self.assertEqual(load_hub_json(self.hub_dir)["instructions"], str(self.hub_dir / "AGENTS.md"))
        self.assertTrue((self.home / ".config" / "opencode" / "AGENTS.md").is_symlink())

        # A second pass has nothing left to do.
        self.assertEqual(plan_actions(), [])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["sync", "--apply"]), 0)
        self.assertEqual(plan_actions(), [])

    def test_backups_are_written_once_and_keep_the_original(self):
        self.write_opencode_config()
        self.init()
        self.enable("claude")
        self.claude_md.parent.mkdir(parents=True)
        self.claude_md.write_text("# My notes\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["sync", "--apply"]), 0)
        backup = self.claude_md.with_name("CLAUDE.md.ocdeck-bak")
        self.assertEqual(backup.read_text(), "# My notes\n")
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode) & 0o777, stat.S_IMODE(self.claude_md.stat().st_mode))
        self.claude_md.write_text("# Rewritten by hand\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["sync", "--apply"]), 0)
        self.assertEqual(backup.read_text(), "# My notes\n")
        self.assertFalse((self.claude_md.parent / "AGENTS.md").exists())

    def test_disabled_harnesses_are_left_alone(self):
        self.write_opencode_config()
        self.init()
        self.enable("claude")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["sync", "--apply"]), 0)
        self.assertTrue(self.claude_md.exists())
        self.assertFalse((self.home / ".codex").exists())
        self.assertFalse((self.home / ".config" / "opencode" / "AGENTS.md").exists())
        self.assertEqual(plan_actions(), [])
        self.assertNotIn(INDEX_SERVER, self.opencode_mcp_keys())

    def test_opencode_without_an_mcp_object_is_manual(self):
        self.write_opencode_config('{\n  "model": "openai/gpt-5.6-sol",\n}\n')
        self.init()
        self.enable("opencode")
        (self.home / ".config" / "opencode" / "AGENTS.md").write_text("# Mine\n", encoding="utf-8")
        original = self.opencode_config.read_text()
        plan = plan_actions()
        self.assertTrue(plan)
        for action in plan:
            self.assertTrue(action.description.startswith("MANUAL:"), action.description)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["sync", "--apply"]), 0)
        self.assertEqual(self.opencode_config.read_text(), original)
        self.assertTrue(all(action.manual for action in plan_actions()))
        self.assertEqual(len(plan_actions()), len(plan))

    def test_missing_opencode_config_stays_manual_and_never_creates_one(self):
        self.init()
        self.enable("opencode")
        shared = self.home / ".config" / "opencode" / "AGENTS.md"
        shared.parent.mkdir(parents=True, exist_ok=True)
        shared.write_text("# Mine\n", encoding="utf-8")
        plan = [action for action in plan_actions() if action.target_path == str(self.opencode_config)]
        self.assertTrue(plan)
        for action in plan:
            self.assertTrue(action.manual, action.description)
            action.apply_fn()
        self.assertFalse(self.opencode_config.exists())

    def test_codex_skips_names_toml_cannot_express(self):
        self.write_opencode_config()
        self.init()
        self.enable("codex")
        payload = json.loads((self.hub_dir / "hub.json").read_text())
        payload["mcp"]["not a name!"] = {"command": ["x"], "env": {}}
        (self.hub_dir / "hub.json").write_text(json.dumps(payload))
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            plan = plan_actions()
        self.assertIn('skipping MCP server "not a name!"', stderr.getvalue())
        toml_path = self.codex_dir / "config.toml"
        toml_path.parent.mkdir(parents=True)
        toml_path.write_text("", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            for action in plan:
                action.apply_fn()
        self.assertNotIn("not a name", toml_path.read_text())

    def test_opencode_config_that_is_not_an_object_is_manual(self):
        self.init()
        self.enable("opencode")
        broken = '{"mcp": true}\n'
        self.write_opencode_config(broken)
        plan = [action for action in plan_actions() if action.target_path == str(self.opencode_config)]
        self.assertTrue(plan)
        for action in plan:
            self.assertTrue(action.manual, action.description)
            action.apply_fn()
        self.assertEqual(self.opencode_config.read_text(), broken)

    def test_opencode_insert_handles_several_servers_and_empty_objects(self):
        self.write_opencode_config('{\n  "mcp": {},\n  "model": "m"\n}\n')
        (self.home / ".config" / "opencode" / "AGENTS.md").write_text("# Mine\n", encoding="utf-8")
        self.init()
        self.enable("opencode")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["sync", "--apply"]), 0)
        payload = parse_jsonc(self.opencode_config.read_text())
        self.assertEqual(list(payload["mcp"]), [INDEX_SERVER])
        self.assertEqual(payload["model"], "m")
        self.assertEqual(plan_actions(), [])

    def test_opencode_insert_restores_the_file_when_it_stays_broken(self):
        self.write_opencode_config('{\n  "mcp": {},\n}\n')
        before = self.opencode_config.read_text()
        # Force an insertion result that does not parse, to exercise the rollback.
        with patch.object(hub, "insert_opencode_server", return_value='{\n  "mcp": {\n'), \
                self.assertRaises(ValueError):
            hub._add_opencode_server("docs", {"command": ["x"], "env": {}})
        self.assertEqual(self.opencode_config.read_text(), before)
        self.assertTrue(hub.parse_jsonc(self.opencode_config.read_text()) == {"mcp": {}})

    def test_opencode_insert_keeps_the_rest_of_the_config(self):
        self.write_opencode_config()
        self.init()
        self.enable("opencode")
        (self.home / ".config" / "opencode" / "AGENTS.md").write_text("# Mine\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["sync", "--apply"]), 0)
        payload = parse_jsonc(self.opencode_config.read_text())
        self.assertEqual(payload["model"], "openai/gpt-5.6-sol")
        self.assertEqual(list(payload["mcp"]), [INDEX_SERVER, "signed_in_tabs", "docs", "remote_thing", "sleepy"])
        self.assertEqual(payload["mcp"][INDEX_SERVER]["command"], [sys.executable, "-m", "ocdeck.index_server"])
        self.assertEqual(plan_actions(), [])

    def test_claude_mcp_uses_add_json_with_a_stdio_payload(self):
        self.write_opencode_config()
        self.init()
        self.enable("claude")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["sync", "--apply"]), 0)
        add = [call for call in self.runner.calls if call[1:3] == ["mcp", "add-json"]]
        self.assertTrue(add)
        for call in add:
            self.assertTrue(call[0].endswith("claude"), call)
            self.assertEqual(call[3:5], ["--scope", "user"])
            self.assertEqual(call[5], call[-2])
            self.assertEqual(json.loads(call[-1])["type"], "stdio")
        self.assertNotIn("signed_in_tabs", [call[-2] for call in add])
        payload = json.loads(next(call for call in add if call[-2] == "docs")[-1])
        self.assertEqual(payload["command"], "/usr/bin/docs-mcp")
        self.assertEqual(payload["args"], [])
        self.assertEqual(payload["env"], {"DOCS_TOKEN_FILE": "/cfg/docs"})
        index = json.loads(next(call for call in add if call[-2] == INDEX_SERVER)[-1])
        self.assertEqual(index["command"], sys.executable)
        self.assertEqual(index["args"], ["-m", "ocdeck.index_server"])


class SafetyTests(HubCase):
    def test_browser_servers_are_never_synced_even_if_added_to_hub_json(self):
        self.init()
        config = load_hub_json(self.hub_dir)
        config["mcp"]["sneaky"] = {"command": ["/x/playwright-mcp-v1"], "env": {}}
        config["mcp"]["by_env"] = {"command": ["/x/other"], "env": {"OPENCODE_AGENT_BROWSER_CONFIG": "/c"}}
        (self.hub_dir / "hub.json").write_text(json.dumps(config))
        self.enable("claude")
        self.enable("codex")
        names = " ".join(action.description for action in plan_actions())
        self.assertNotIn("sneaky", names)
        self.assertNotIn("by_env", names)

    def test_opencode_v2_protected_config_is_manual_only(self):
        managed = self.home / "opt" / "opencode.jsonc"
        managed.parent.mkdir(parents=True)
        managed.write_text('{"mcp": {}}\n')
        before = managed.read_text()
        self.init()
        self.enable("opencode")
        environment = {key: value for key, value in os.environ.items() if key != "OCDECK_OPENCODE_CONFIG"}
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(hub, "opencode_backend", return_value="v2"), \
                patch.object(hub, "managed_v2_config", return_value=managed):
            plan = [action for action in plan_actions() if action.harness == "opencode"]
            self.assertTrue(plan)
            self.assertTrue(all(action.manual for action in plan))
            self.assertTrue(all("protected" in action.description for action in plan))
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["sync", "--apply"]), 0)
        self.assertEqual(managed.read_text(), before)
        self.assertFalse((self.home / ".config" / "opencode" / "AGENTS.md").exists())


class HandoffTests(HubCase):
    def git_repo(self) -> Path:
        project = self.base / "project"
        project.mkdir(parents=True, exist_ok=True)
        (project / "README.md").write_text("# deck\n", encoding="utf-8")
        for command in (
            ["git", "init", "-b", "main"],
            ["git", "config", "user.email", "test@example.invalid"],
            ["git", "config", "user.name", "Test"],
            ["git", "add", "."],
            ["git", "commit", "-m", "first commit"],
        ):
            subprocess.run(command, cwd=project, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        (project / "dirty.txt").write_text("scratch\n", encoding="utf-8")
        return project

    def claude_transcript(self, project: Path, session_id: str) -> None:
        entries = [
            {"type": "user", "cwd": str(project), "sessionId": session_id, "timestamp": "2026-09-26T10:00:00Z",
             "message": {"role": "user", "content": "<command-name>/login</command-name>"}},
            {"type": "user", "cwd": str(project), "sessionId": session_id, "timestamp": "2026-09-26T10:00:01Z",
             "message": {"role": "user", "content": [{"type": "text", "text": "wire the hub sync"}]}},
            {"type": "assistant", "cwd": str(project), "timestamp": "2026-09-26T10:05:00Z",
             "message": {"model": "claude-opus-5-5", "content": [{"type": "text", "text": "the plan looks right"}]}},
        ]
        path = self.home / ".claude" / "projects" / "work" / f"{session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")

    def test_trivial_continuations_are_skipped_for_an_earlier_real_message(self):
        """REAL BUG: a long session's last human turns are often just "go on",
        so the handoff quoted a continuation instead of the real request. The
        filter must be an exact match: "Continue the migration" survives."""
        project = self.base / "plain"
        project.mkdir()
        session_id = "12121212-3434-5656-7878-909090909090"
        entries = [
            {"type": "user", "cwd": str(project), "sessionId": session_id, "timestamp": "2026-09-26T12:00:00Z",
             "message": {"role": "user", "content": "ship the release notes pipeline"}},
            {"type": "user", "cwd": str(project), "sessionId": session_id, "timestamp": "2026-09-26T12:01:00Z",
             "message": {"role": "user", "content": "Continue the migration in stages"}},
            {"type": "assistant", "cwd": str(project), "timestamp": "2026-09-26T12:05:00Z",
             "message": {"model": "claude-opus-5-5", "content": [{"type": "text", "text": "shipping it"}]}},
            {"type": "user", "cwd": str(project), "sessionId": session_id, "timestamp": "2026-09-26T12:06:00Z",
             "message": {"role": "user", "content": "go on"}},
            {"type": "user", "cwd": str(project), "sessionId": session_id, "timestamp": "2026-09-26T12:07:00Z",
             "message": {"role": "user", "content": "  Carry On  "}},
            {"type": "assistant", "cwd": str(project), "timestamp": "2026-09-26T12:08:00Z",
             "message": {"model": "claude-opus-5-5", "content": [{"type": "text", "text": "done shipping"}]}},
        ]
        path = self.home / ".claude" / "projects" / "work" / f"{session_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
        session = SessionRecord(
            id=f"claude:{session_id}", title="Ship the notes", directory=str(project),
            project_id="", created_ms=0, updated_ms=0, model="anthropic/claude-opus-5-5", harness="claude",
        )
        path, _prompt = write_handoff(session, "codex", self.hub_dir, now=datetime(2026, 9, 26, 12, 9, 0))
        body = path.read_text()
        self.assertIn("ship the release notes pipeline", body)
        self.assertIn("Continue the migration in stages", body)  # never a substring match
        self.assertIn("done shipping", body)
        self.assertNotIn("go on", body)
        self.assertNotIn("Carry On", body)

    def test_handoff_captures_prompts_reply_and_git_state(self):
        project = self.git_repo()
        session_id = "11111111-2222-3333-4444-555555555555"
        self.claude_transcript(project, session_id)
        session = SessionRecord(
            id=f"claude:{session_id}", title="Wire the hub sync", directory=str(project),
            project_id="", created_ms=0, updated_ms=0, model="anthropic/claude-opus-5-5", harness="claude",
        )
        path, prompt = write_handoff(session, "codex", self.hub_dir, now=datetime(2026, 9, 26, 10, 6, 7))
        self.assertEqual(
            path,
            self.hub_dir / "handoffs" / "project" / "20260926-100607-claude-to-codex.md",
        )
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        body = path.read_text()
        self.assertIn("# Handoff: Wire the hub sync", body)
        self.assertIn("Claude Code (`claude`)", body)
        self.assertIn(session_id, body)
        self.assertIn("claude-opus-5-5", body)
        self.assertIn(str(project), body)
        self.assertIn("wire the hub sync", body)
        self.assertNotIn("/login", body)
        self.assertIn("the plan looks right", body)
        self.assertIn("## Git state", body)
        self.assertIn("Branch: `main`", body)
        self.assertIn("first commit", body)
        self.assertIn("dirty.txt", body)
        self.assertIn(
            f'Continue the work handed off from Claude Code session "Wire the hub sync". '
            f"First read the handoff notes at {path}, check the git state, then carry on.",
            prompt,
        )

    def test_handoff_outside_a_repo_omits_git_and_rejects_bad_targets(self):
        plain = self.base / "plain"
        plain.mkdir()
        session = SessionRecord(
            id="ses_opencode", title="Sketch notes", directory=str(plain), project_id="",
            created_ms=0, updated_ms=0, last_prompt="draft the plan", harness="opencode",
        )
        path, prompt = write_handoff(session, "claude", self.hub_dir, now=datetime(2026, 9, 26, 11, 0, 0))
        body = path.read_text()
        self.assertNotIn("## Git state", body)
        self.assertIn("draft the plan", body)
        self.assertIn("OpenCode session", prompt)
        self.assertEqual(path.name, "20260926-110000-opencode-to-claude.md")
        with self.assertRaises(ValueError):
            write_handoff(session, "cursor", self.hub_dir)

    def test_cli_handoff_prints_path_and_prompt(self):
        project = self.git_repo()
        session_id = "66666666-7777-8888-9999-000000000000"
        self.claude_transcript(project, session_id)
        self.assertEqual(main(["harness", "on", "codex"]), 0)
        self.assertEqual(main(["harness", "off", "opencode"]), 0)
        self.assertEqual(main(["harness", "off", "claude"]), 0)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["handoff", f"claude:{session_id}", "--hub", str(self.hub_dir), "--to", "codex"]), 0)
        text = output.getvalue()
        self.assertIn("claude-to-codex.md", text)
        self.assertIn("Continue the work handed off from Claude Code", text)
        self.assertTrue((self.hub_dir / "handoffs" / "project").is_dir())


if __name__ == "__main__":
    unittest.main()


class SyncOnlyTests(HubCase):
    def test_only_mcp_registers_servers_without_touching_instruction_files(self):
        self.write_opencode_config()
        self.init()
        self.enable("claude", "codex")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["sync", "--only", "mcp", "--apply"]), 0)
        self.assertFalse((self.home / ".claude" / "CLAUDE.md").exists())
        self.assertFalse((self.codex_dir / "AGENTS.md").exists())
        self.assertIn(INDEX_SERVER, (self.codex_dir / "config.toml").read_text())
        self.assertIn(INDEX_SERVER, self.runner.server_names)
        self.assertNotIn("instructions", out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as again:
            self.assertEqual(main(["sync", "--only", "mcp"]), 0)
        self.assertIn("0 action(s) planned", again.getvalue())


def make_opencode_db(path: Path, session_id: str, messages: list[dict]) -> Path:
    """A V2-shaped session DB: ``session_message`` rows plus ``part`` rows.

    Message dicts are chronological; ``data`` holds the inline payload and
    ``parts`` the separate part rows, exactly like the real OpenCode DB.
    """
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, "
            "type TEXT NOT NULL, seq INTEGER NOT NULL, time_created INTEGER NOT NULL, "
            "time_updated INTEGER NOT NULL, data TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT NOT NULL, "
            "session_id TEXT NOT NULL, time_created INTEGER NOT NULL, "
            "time_updated INTEGER NOT NULL, data TEXT NOT NULL)"
        )
        for index, message in enumerate(messages):
            message_id = f"msg_{index:04d}"
            connection.execute(
                "INSERT INTO session_message VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    message_id,
                    message.get("session", session_id),
                    message["type"],
                    message.get("seq", index),
                    message.get("time", 1_000_000 + index),
                    1_000_000 + index,
                    json.dumps(message.get("data", {})),
                ),
            )
            for part_index, part in enumerate(message.get("parts", ())):
                connection.execute(
                    "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        f"prt_{index:04d}_{part_index:03d}",
                        message_id,
                        message.get("session", session_id),
                        1_000_000 + index * 100 + part_index,
                        1_000_000 + index * 100 + part_index,
                        json.dumps(part),
                    ),
                )
        connection.commit()
    finally:
        connection.close()
    return path


class OpenCodeHandoffTests(HubCase):
    """OpenCode-origin handoffs read the session DB like Claude/Codex read JSONL."""

    def setUp(self):
        super().setUp()
        self.project = self.base / "project"
        self.project.mkdir()

    def session(self, **kwargs) -> SessionRecord:
        data = {
            "id": "ses_OPENCODE",
            "title": "Wire the hub sync",
            "directory": str(self.project),
            "project_id": "",
            "created_ms": 0,
            "updated_ms": 0,
            "harness": "opencode",
        }
        data.update(kwargs)
        return SessionRecord(**data)

    def test_handoff_reads_prompts_and_reply_from_the_session_db(self):
        """REAL BUG: an OpenCode-origin handoff quoted only the single
        ``last_prompt`` ("go on") because the hub had no OpenCode transcript
        path at all. The session DB has the real exchange: non-trivial prompts
        plus a last assistant reply section."""
        db = make_opencode_db(self.base / "opencode.db", "ses_OPENCODE", [
            {"type": "user", "data": {"text": "wire the hub sync for the agents board"}},
            {"type": "assistant", "data": {"content": [
                {"type": "reasoning", "text": "quiet thinking"},
                {"type": "text", "text": "the plan looks right"},
            ]}},
            {"type": "user", "data": {"text": "Continue the migration in stages"}},
            {"type": "user", "parts": [{"type": "text", "text": "go on"}]},  # trivial, from parts
            {"type": "user", "data": {"text": "carry on"}},                 # trivial, inline
            {"type": "assistant", "parts": [{"type": "text", "text": "all tests green"}]},
            {"type": "user", "session": "ses_OTHER", "data": {"text": "SECRET OTHER SESSION"}},
            {"type": "system", "data": {"text": "SECRET SYSTEM ROW"}},
        ])
        with patch.dict(os.environ, {"OCDECK_SESSION_DB_FILE": str(db)}):
            path, _prompt = write_handoff(
                self.session(last_prompt="go on"), "claude", self.hub_dir,
                now=datetime(2026, 9, 26, 10, 0, 0),
            )
        body = path.read_text()
        self.assertEqual(path.name, "20260926-100000-opencode-to-claude.md")
        self.assertIn("## Recent requests", body)
        self.assertIn("wire the hub sync for the agents board", body)
        self.assertIn("Continue the migration in stages", body)  # exact match only
        self.assertIn("## Last assistant reply", body)  # the section OpenCode handoffs never had
        self.assertIn("all tests green", body)
        self.assertNotIn("the plan looks right", body)  # an older reply is replaced
        self.assertNotIn("quiet thinking", body)        # reasoning is not the reply
        self.assertNotIn("go on", body)                 # trivial continuations are dropped
        self.assertNotIn("carry on", body)
        self.assertNotIn("SECRET", body)                 # other sessions and system rows stay out

    def test_a_malformed_or_missing_db_falls_back_without_raising(self):
        broken = self.base / "broken.db"
        broken.write_bytes(b"this is not a sqlite database" * 8)
        for db_path in (broken, self.base / "missing.db"):
            with self.subTest(db_path=db_path.name), \
                    patch.dict(os.environ, {"OCDECK_SESSION_DB_FILE": str(db_path)}):
                path, _prompt = write_handoff(
                    self.session(last_prompt="draft the parser fix"), "claude", self.hub_dir,
                    now=datetime(2026, 9, 26, 10, 1, 0),
                )
            body = path.read_text()
            self.assertIn("draft the parser fix", body)  # the last-prompt fallback
            self.assertNotIn("## Last assistant reply", body)


class ProjectMemoryTests(HubCase):
    """The handoff quotes ocdeck-index's shared memory for the session's project."""

    def setUp(self):
        super().setUp()
        self.project = self.base / "memo"
        self.project.mkdir()

    def session(self, **kwargs) -> SessionRecord:
        data = {
            "id": "ses_MEMORY",
            "title": "Write the notes",
            "directory": str(self.project),
            "project_id": "",
            "created_ms": 0,
            "updated_ms": 0,
            "harness": "opencode",
        }
        data.update(kwargs)
        return SessionRecord(**data)

    def test_project_memory_is_listed_with_its_writer(self):
        from ocdeck.index_server import IndexServer

        with patch.dict(os.environ, {"OCDECK_INDEX_HARNESS": "codex"}):
            IndexServer(cwd=self.project).memory_write(
                "run pytest -q before committing", tags=["convention"]
            )
        path, _prompt = write_handoff(self.session(), "claude", self.hub_dir,
                                      now=datetime(2026, 9, 26, 11, 0, 0))
        body = path.read_text()
        self.assertIn("## PROJECT MEMORY (ocdeck-index, if any)", body)
        self.assertIn("(codex) run pytest -q before committing", body)

    def test_no_memory_renders_the_document_without_the_section(self):
        path, _prompt = write_handoff(self.session(), "claude", self.hub_dir,
                                      now=datetime(2026, 9, 26, 11, 0, 0))
        body = path.read_text()
        self.assertNotIn("PROJECT MEMORY", body)
        self.assertNotIn("no memory", body.lower())  # no placeholder noise

    def test_a_failing_index_import_omits_the_section(self):
        with patch.dict(sys.modules, {"ocdeck.index_server": None}):
            path, _prompt = write_handoff(self.session(), "claude", self.hub_dir,
                                          now=datetime(2026, 9, 26, 11, 0, 0))
        self.assertNotIn("PROJECT MEMORY", path.read_text())
