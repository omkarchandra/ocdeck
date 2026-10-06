"""Claude's local CLI messages must not masquerade as model activity."""
import json
from datetime import datetime, timezone
from unittest import mock

import pytest

from ocdeck.harnesses import ClaudeHarness, LiveProcess, runtime_label
from ocdeck.models import agent_state


PROMPT = {
    "type": "user", "cwd": "/project", "timestamp": "2026-09-26T10:00:00Z",
    "message": {"content": "Fix the UI"},
}
REPLY = {
    "type": "assistant", "timestamp": "2026-09-26T10:01:00Z",
    "message": {"model": "claude-sonnet-5", "content": [{"type": "text", "text": "Done"}]},
}
DONE = {"type": "system", "subtype": "turn_duration", "timestamp": "2026-09-26T10:01:01Z"}
LOCAL_COMMANDS = [
    {"type": "user", "isMeta": True, "timestamp": "2026-09-26T10:02:00.001Z",
     "message": {"content": "<local-command-caveat>Local CLI output.</local-command-caveat>"}},
    {"type": "user", "timestamp": "2026-09-26T10:02:00Z",
     "message": {"content": "<command-name>/rename</command-name><command-args>new name</command-args>"}},
    {"type": "user", "timestamp": "2026-09-26T10:02:00Z",
     "message": {"content": [{"type": "text", "text": "<local-command-stdout>Renamed</local-command-stdout>"}]}},
]
API_ERROR = {
    "type": "assistant", "timestamp": "2026-09-26T10:01:02Z",
    "isApiErrorMessage": True, "error": "rate_limit",
    "message": {"model": "<synthetic>", "stop_reason": "stop_sequence",
                "content": [{"type": "text", "text": "Quota reached"}]},
}
INTERRUPTION = {
    "type": "user", "timestamp": "2026-09-26T10:01:02Z",
    "message": {"content": [{"type": "text", "text": "[Request interrupted by user]"}]},
}
NOW = datetime(2026, 9, 26, 10, 2, 1, tzinfo=timezone.utc).timestamp()


def append(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        for entry in entries:
            stream.write(json.dumps(entry) + "\n")


def collect(adapter):
    return adapter.collect(
        processes=[LiveProcess(1, "/project", ("claude", "--resume", "aaa"))],
        tmux={}, now=NOW,
    )[0]


def test_local_commands_do_not_reopen_a_completed_turn_or_replace_its_model(tmp_path):
    path = tmp_path / "project" / "aaa.jsonl"
    append(path, [PROMPT, REPLY, API_ERROR, DONE])
    adapter = ClaudeHarness(tmp_path, "/bin/claude")
    assert collect(adapter).status == "idle"
    # Exercise cache invalidation and the slight out-of-order timestamps seen
    # in the actual transcript. All these entries are written by an idle CLI.
    for entry in LOCAL_COMMANDS:
        append(path, [entry])
        session = collect(adapter)
        assert session.status == "idle"
        assert not session.assistant_active
        assert agent_state(session, int(NOW * 1000)) != "busy"
        assert session.model == "claude-sonnet-5"
        assert runtime_label("claude", session.model, full=False) == "CC SN5"
        assert session.last_prompt == "Fix the UI"


def test_local_commands_during_a_real_turn_do_not_cancel_it(tmp_path):
    path = tmp_path / "project" / "aaa.jsonl"
    append(path, [PROMPT, REPLY, *LOCAL_COMMANDS])
    session = collect(ClaudeHarness(tmp_path, "/bin/claude"))
    assert session.status == "busy"
    assert session.last_interaction_ms == int(datetime.fromisoformat(PROMPT["timestamp"]).timestamp() * 1000)


def test_local_only_tail_does_not_trigger_freshness_fallback(tmp_path):
    path = tmp_path / "project" / "aaa.jsonl"
    # Only local entries survive the bounded tail; the head still supplies
    # identity and a real model after an earlier synthetic CLI notice.
    append(path, [PROMPT, API_ERROR, REPLY, DONE, *LOCAL_COMMANDS])
    with mock.patch("ocdeck.harnesses._read_tail", return_value=LOCAL_COMMANDS):
        session = collect(ClaudeHarness(tmp_path, "/bin/claude"))
    assert session.status == "idle"
    assert session.model == "claude-sonnet-5"
    assert agent_state(session, int(NOW * 1000)) == "open"


def test_context_output_injected_for_the_model_does_not_reopen_the_turn(tmp_path):
    # Shape copied from a real transcript: /context writes two local_command
    # system entries, then a hidden user entry (isMeta, plain-string content)
    # parented to them, carrying the report for the model. Later the CLI adds
    # an away_summary recap. None of it is model work; the row showed RUN.
    context = [
        {"type": "system", "subtype": "local_command", "uuid": "lc1", "isMeta": False,
         "timestamp": "2026-09-26T10:02:00Z", "content": "<command-name>/context</command-name>"},
        {"type": "system", "subtype": "local_command", "uuid": "lc2", "parentUuid": "lc1", "isMeta": False,
         "commandRun": {"command": "context", "args": ""},
         "timestamp": "2026-09-26T10:02:00Z", "content": "<local-command-stdout>Context Usage</local-command-stdout>"},
        {"type": "user", "isMeta": True, "uuid": "m1", "parentUuid": "lc2", "promptId": "p1",
         "timestamp": "2026-09-26T10:02:00Z", "message": {"content": "## Context Usage\n\n**Tokens:** 193.5k"}},
        {"type": "system", "subtype": "away_summary", "timestamp": "2026-09-26T10:02:01Z",
         "content": "Recap of the session"},
    ]
    path = tmp_path / "project" / "aaa.jsonl"
    append(path, [PROMPT, REPLY, DONE, *context, *context])
    session = collect(ClaudeHarness(tmp_path, "/bin/claude"))
    assert (session.status, session.assistant_active) == ("idle", False)
    assert agent_state(session, int(NOW * 1000)) != "busy"
    assert session.last_prompt == "Fix the UI"


@pytest.mark.parametrize("terminal_entry", [API_ERROR, INTERRUPTION], ids=["quota-error", "interrupted"])
def test_terminal_notices_end_the_turn_without_turn_duration(tmp_path, terminal_entry):
    path = tmp_path / "project" / "aaa.jsonl"
    append(path, [PROMPT, REPLY])
    adapter = ClaudeHarness(tmp_path, "/bin/claude")
    assert collect(adapter).status == "busy"
    append(path, [terminal_entry, *LOCAL_COMMANDS])
    session = collect(adapter)
    assert session.status == "idle"
    assert session.model == "claude-sonnet-5"
    assert session.assistant_done_ms > session.last_interaction_ms


@pytest.mark.parametrize("message", [
    {"type": "user", "message": {"content": "Continue"}},
    {"type": "user", "message": {"content": "<task-notification>finished</task-notification>"}},
    {"type": "user", "isMeta": True, "message": {"content": "<cross-session-message>ready</cross-session-message>"}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "Continuing"}]}},
], ids=["prompt", "background-task", "cross-session", "tool-result", "assistant-without-model"])
def test_real_activity_after_local_commands_reopens_the_turn(tmp_path, message):
    path = tmp_path / "project" / "aaa.jsonl"
    append(path, [PROMPT, REPLY, DONE, *LOCAL_COMMANDS, {**message, "timestamp": "2026-09-26T10:03:00Z"}])
    session = collect(ClaudeHarness(tmp_path, "/bin/claude"))
    assert (session.status, session.assistant_active) == ("busy", True)
    assert session.model == "claude-sonnet-5"
