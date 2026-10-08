"""OC Deck answers a Claude Code permission prompt through a PermissionRequest hook.

The hook runs as a real subprocess (``python -m ocdeck.claude_permissions``) against a
throwaway state folder, the way Claude Code runs it; nothing here starts Claude itself.
"""
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import ocdeck
from ocdeck import claude_permissions as perms
from ocdeck.harnesses import ClaudeHarness, LiveProcess, MultiHarnessSource
from ocdeck.models import agent_state
from tests.test_claude_status import NOW, PROMPT, REPLY, append

SESSION = "9d2d9af9-e64c-4ad2-ae8e-c1ea5290ba2b"
SRC = str(Path(ocdeck.__file__).resolve().parents[1])
EVENT = {"session_id": SESSION, "hook_event_name": "PermissionRequest", "tool_name": "Bash",
         "tool_input": {"command": "touch  probe.txt", "description": "Create a file"}, "cwd": "/work"}


def start_hook(directory, event=EVENT, wait=20):
    environment = {**os.environ, "OCDECK_CLAUDE_PERMISSIONS_DIR": str(directory),
                   "OCDECK_PERMISSION_WAIT": str(wait), "PYTHONPATH": SRC}
    return subprocess.Popen([sys.executable, "-m", "ocdeck.claude_permissions"], env=environment,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)


def feed(process, event):
    process.stdin.write(json.dumps(event))
    process.stdin.close()


def run_clear(directory, event):
    environment = {**os.environ, "OCDECK_CLAUDE_PERMISSIONS_DIR": str(directory), "PYTHONPATH": SRC}
    return subprocess.run([sys.executable, "-m", "ocdeck.claude_permissions", "clear"], env=environment,
                          input=json.dumps(event), text=True, timeout=20, capture_output=True)


def wait_for(condition, seconds=10):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = condition()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not reached")


def pending(directory):
    return perms.pending_requests(directory)


def test_the_deck_allowing_a_request_makes_the_hook_print_claudes_allow_decision(tmp_path):
    process = start_hook(tmp_path)
    feed(process, EVENT)
    request = wait_for(lambda: pending(tmp_path).get(SESSION))
    assert (request.tool, request.summary) == ("Bash", "Bash touch probe.txt")
    assert perms.approve(SESSION, request.request_id, tmp_path) == ""
    output, _ = process.communicate(timeout=15)
    assert json.loads(output) == {"hookSpecificOutput": {
        "hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}
    assert list(tmp_path.iterdir()) == []  # request and decision are both cleaned up


def test_with_no_answer_the_hook_prints_nothing_so_the_terminal_prompt_decides(tmp_path):
    process = start_hook(tmp_path, wait=1)
    feed(process, EVENT)
    output, _ = process.communicate(timeout=15)
    assert output == "" and process.returncode == 0
    assert list(tmp_path.iterdir()) == []


def test_a_request_is_never_answered_for_the_wrong_request_or_a_gone_hook(tmp_path):
    assert "no longer waiting" in perms.approve(SESSION, "0123456789ab", tmp_path)
    assert "invalid" in perms.approve("../x", "abc", tmp_path)
    assert "invalid" in perms.approve(SESSION, "a/b", tmp_path)
    # A request file whose hook process is gone is stale: not listed, not approvable.
    (tmp_path / f"{SESSION}__abc.json").write_text(json.dumps(
        {"id": "abc", "session": SESSION, "tool": "Bash", "pid": 2 ** 22 + 7, "summary": "x", "started": 1}))
    assert pending(tmp_path) == {}
    assert "no longer waiting" in perms.approve(SESSION, "abc", tmp_path)
    assert not (tmp_path / f"{SESSION}__abc.decision").exists()


def test_malformed_or_mismatched_request_files_are_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(perms, "_hook_alive", lambda pid: True)
    body = {"id": "abc", "session": SESSION, "tool": "Bash", "pid": 1, "summary": "ok", "started": 1}
    (tmp_path / f"{SESSION}__abc.json").write_text(json.dumps(body))
    (tmp_path / f"{SESSION}__zzz.json").write_text(json.dumps(body))  # name does not match its id
    (tmp_path / "bad.json").write_text("{not json")
    (tmp_path / f"{SESSION}__big.json").write_text(" " * (perms.MAX_REQUEST_BYTES + 1))
    assert list(pending(tmp_path)) == [SESSION]
    assert pending(tmp_path)[SESSION].request_id == "abc"


def test_a_post_tool_event_for_the_same_call_clears_the_request_at_once(tmp_path):
    process = start_hook(tmp_path)
    feed(process, EVENT)
    wait_for(lambda: pending(tmp_path))
    other = {**EVENT, "hook_event_name": "PostToolUse", "tool_input": {"command": "ls"}, "tool_use_id": "t1"}
    assert run_clear(tmp_path, other).returncode == 0
    assert pending(tmp_path), "a different tool call must not clear the request"
    same = {**EVENT, "hook_event_name": "PostToolUse", "tool_use_id": "t2"}
    run_clear(tmp_path, same)
    output, _ = process.communicate(timeout=10)  # the hook gives up as soon as its request is gone
    assert output == "" and pending(tmp_path) == {}


def test_stop_clears_every_request_of_that_session_only(tmp_path, monkeypatch):
    monkeypatch.setattr(perms, "_hook_alive", lambda pid: True)
    for session, request in ((SESSION, "aaa"), (SESSION, "bbb"), ("other-session", "ccc")):
        (tmp_path / f"{session}__{request}.json").write_text(json.dumps(
            {"id": request, "session": session, "tool": "Bash", "pid": 1, "summary": "s", "started": 1}))
    assert perms.clear_requests(json.dumps({"session_id": SESSION, "hook_event_name": "Stop"}), tmp_path) == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == ["other-session__ccc.json"]


def test_broken_hook_input_is_harmless(tmp_path):
    assert perms.run_hook("not json", tmp_path, wait=1) == ""
    assert perms.run_hook(json.dumps({"session_id": "../etc"}), tmp_path, wait=1) == ""
    assert perms.clear_requests("{", tmp_path) == 0
    assert list(tmp_path.iterdir()) == []


def test_summaries_are_short_single_lines():
    assert perms.summarize("Bash", {"command": "echo  a\nb"}) == "Bash echo a b"
    assert perms.summarize("Edit", {"file_path": "/x/y.py", "new_string": "SECRET"}) == "Edit /x/y.py"
    assert "SECRET" not in perms.summarize("Write", {"content": "SECRET", "other": 1})
    assert len(perms.summarize("Bash", {"command": "x" * 5000})) <= perms.SUMMARY_CHARS


# --- how the deck shows and answers it -------------------------------------------

def live_claude(tmp_path):
    append(tmp_path / "project" / f"{SESSION}.jsonl", [PROMPT, REPLY])
    process = [LiveProcess(1, "/project", ("claude", "--resume", SESSION))]
    return ClaudeHarness(tmp_path, "/bin/claude"), process


def test_a_live_session_with_a_pending_request_shows_as_permission(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("OCDECK_CLAUDE_PERMISSIONS_DIR", str(state))
    monkeypatch.setattr(perms, "_hook_alive", lambda pid: True)
    (state / f"{SESSION}__req1.json").write_text(json.dumps(
        {"id": "req1", "session": SESSION, "tool": "Bash", "pid": 1, "summary": "Bash touch probe.txt", "started": 1}))
    harness, processes = live_claude(tmp_path / "projects")
    (record,) = harness.collect(processes=processes, tmux={}, now=NOW)
    assert (record.permission, record.permission_id) == ("Bash touch probe.txt", "req1")
    assert agent_state(record, int(NOW * 1000)) == "permission"
    # A session that is not running cannot receive an answer, so it never shows one.
    (record,) = harness.collect(processes=[], tmux={}, now=NOW)
    assert (record.permission, record.permission_id) == ("", "")


def test_the_multi_harness_source_routes_claude_approval_to_the_hook(tmp_path, monkeypatch):
    monkeypatch.setenv("OCDECK_CLAUDE_PERMISSIONS_DIR", str(tmp_path))
    process = start_hook(tmp_path)
    feed(process, EVENT)
    request = wait_for(lambda: pending(tmp_path).get(SESSION))

    class OpenCode:
        calls = []

        async def approve_permission(self, session_id, permission_id):
            self.calls.append((session_id, permission_id))
            return ""

    opencode = OpenCode()
    source = MultiHarnessSource(opencode, [])
    assert asyncio.run(source.approve_permission(f"claude:{SESSION}", request.request_id)) == ""
    assert process.communicate(timeout=15)[0].strip() != ""
    assert asyncio.run(source.approve_permission("ses_abc", "perm1")) == ""
    assert opencode.calls == [("ses_abc", "perm1")]  # OpenCode still goes to OpenCode
    assert "no longer waiting" in asyncio.run(source.approve_permission(f"claude:{SESSION}", "gone00000000"))
    assert "unavailable" in asyncio.run(MultiHarnessSource(None, []).approve_permission("ses_abc", "p"))


# --- how a session gets the hook ---------------------------------------------------

def test_launch_arguments_attach_the_hook_through_a_settings_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OCDECK_CLAUDE_PERMISSION_HOOK", "1")
    harness = ClaudeHarness(tmp_path / "projects", "/bin/claude")
    arguments = harness.launch_arguments(False)
    assert arguments[0] == "--no-chrome" and arguments[1] == "--settings"
    settings = json.loads(Path(arguments[2]).read_text())
    assert set(settings["hooks"]) == {"PermissionRequest", "PostToolUse", "PostToolUseFailure",
                                      "PermissionDenied", "Stop"}
    command = settings["hooks"]["PermissionRequest"][0]["hooks"][0]["command"]
    assert command == f"{sys.executable} -m ocdeck.claude_permissions"
    assert settings["hooks"]["Stop"][0]["hooks"][0]["command"].endswith(" clear")
    assert harness.launch_arguments(False) == arguments  # stable, rewritten only when it changes
    assert harness.resume_command(SESSION, "/work")[:4] == ["/bin/claude", *arguments[:3]]


def test_the_hook_can_be_switched_off(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OCDECK_CLAUDE_PERMISSION_HOOK", "0")
    assert ClaudeHarness(tmp_path / "projects", "/bin/claude").launch_arguments(False) == ["--no-chrome"]
