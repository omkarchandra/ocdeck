"""Claude Code helper agents (the Agent tool) are tracked as children of the session that spawned them.

Shapes come from a real run: ``<project>/<session>/subagents/agent-<id>.jsonl`` (sidechain
entries: user, attachment, assistant) plus ``agent-<id>.meta.json``.
"""
import json
from types import SimpleNamespace

from ocdeck.harnesses import ClaudeHarness, LiveProcess, is_transcript_subagent
from ocdeck.models import agent_state
from tests.test_claude_status import NOW, PROMPT, REPLY, append

PARENT = "aaa"
AGENT = "a494e0aae9f70bdcf"


def side(kind, stamp, **fields):
    return {"isSidechain": True, "agentId": AGENT, "sessionId": PARENT, "cwd": "/project",
            "type": kind, "timestamp": stamp, **fields}


TASK = side("user", "2026-09-26T10:01:30Z", message={"role": "user", "content": "Reply with the word OK"})
ATTACHMENT = side("attachment", "2026-09-26T10:01:30.100Z", attachment={"type": "skill_listing"})
WORKING = side("assistant", "2026-09-26T10:01:50Z", message={
    "model": "claude-haiku-4-5-20251001", "stop_reason": "tool_use",
    "content": [{"type": "tool_use", "id": "toolu_x", "name": "Bash", "input": {"command": "ls"}}]})
FINISHED = side("assistant", "2026-09-26T10:01:55Z", message={
    "model": "claude-haiku-4-5-20251001", "stop_reason": "end_turn",
    "content": [{"type": "text", "text": "OK"}]})


def add_subagent(root, entries, meta=None, *, project="project", agent=AGENT):
    base = root / project / PARENT / "subagents"
    append(base / f"agent-{agent}.jsonl", entries)
    if meta is not False:
        (base / f"agent-{agent}.meta.json").write_text(json.dumps(meta or {
            "agentType": "general-purpose", "description": "check the build", "toolUseId": "toolu_1",
            "spawnDepth": 1, "requestShape": "background", "requestNonInteractive": True}))


def collect(root, *, live=True, now=NOW):
    append(root / "project" / f"{PARENT}.jsonl", [PROMPT, REPLY])
    processes = [LiveProcess(1, "/project", ("claude", "--resume", PARENT))] if live else []
    return ClaudeHarness(root, "/bin/claude").collect(processes=processes, tmux={}, now=now)


def children(records):
    return [record for record in records if record.parent_id]


def test_finished_subagent_is_an_idle_child_of_its_parent(tmp_path):
    add_subagent(tmp_path, [TASK, ATTACHMENT, FINISHED, ATTACHMENT])
    records = collect(tmp_path)
    (child,) = children(records)
    assert child.id == f"claude:agent-{AGENT}"
    assert child.parent_id == f"claude:{PARENT}"
    assert child.agent_session_kind == "Subagent"
    assert (child.title, child.model, child.directory) == (
        "check the build", "claude-haiku-4-5-20251001", "/project")
    assert child.last_prompt == "Reply with the word OK"
    assert (child.status, child.instance_count, child.terminals) == ("idle", 0, ())
    assert agent_state(child, int(NOW * 1000)) == "idle"
    assert [record.id for record in records if not record.parent_id] == [f"claude:{PARENT}"]


def test_unfinished_recent_subagent_of_a_live_parent_is_busy(tmp_path):
    add_subagent(tmp_path, [TASK, ATTACHMENT, WORKING])
    (child,) = children(collect(tmp_path))
    assert (child.status, child.assistant_active) == ("busy", True)
    assert agent_state(child, int(NOW * 1000)) == "busy"


def test_a_tool_result_after_the_tool_call_keeps_the_turn_open(tmp_path):
    result = side("user", "2026-09-26T10:01:58Z", message={"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_x", "content": "file"}]})
    add_subagent(tmp_path, [TASK, WORKING, result])
    assert children(collect(tmp_path))[0].status == "busy"


def test_a_stale_unfinished_subagent_is_not_busy(tmp_path):
    add_subagent(tmp_path, [TASK, WORKING])  # last write 10:01:50; 4 minutes later nothing moved
    (child,) = children(collect(tmp_path, now=NOW + 240))
    assert child.status == "idle"


def test_without_a_live_parent_nothing_runs(tmp_path):
    add_subagent(tmp_path, [TASK, WORKING])
    (child,) = children(collect(tmp_path, live=False))
    assert (child.status, child.assistant_active) == ("idle", False)


def test_a_subagent_whose_parent_is_not_listed_is_dropped(tmp_path):
    add_subagent(tmp_path, [TASK, FINISHED])
    records = ClaudeHarness(tmp_path, "/bin/claude").collect(processes=[], tmux={}, now=NOW)
    assert records == []


def test_the_subagent_never_steals_a_fresh_process_from_its_parent(tmp_path):
    add_subagent(tmp_path, [TASK, FINISHED])
    # An id-less claude process in /project belongs to the newest MAIN transcript.
    records = collect(tmp_path)
    parent = next(record for record in records if not record.parent_id)
    assert parent.instance_count == 1
    assert children(records)[0].instance_count == 0


def test_a_missing_or_broken_meta_file_falls_back_to_the_task_text(tmp_path):
    add_subagent(tmp_path, [TASK, FINISHED], meta=False)
    assert children(collect(tmp_path))[0].title == "Reply with the word OK"
    (tmp_path / "project" / PARENT / "subagents" / f"agent-{AGENT}.meta.json").write_text("{not json")
    assert children(collect(tmp_path))[0].title == "Reply with the word OK"


def test_subagents_do_not_count_against_the_session_cap(tmp_path, monkeypatch):
    monkeypatch.setattr("ocdeck.harnesses.MAX_SESSIONS_PER_HARNESS", 1)
    for number in range(3):
        add_subagent(tmp_path, [TASK, FINISHED], agent=f"{number:017x}")
    records = collect(tmp_path)
    assert len(children(records)) == 3
    assert len([record for record in records if not record.parent_id]) == 1


def test_opening_a_helper_row_opens_its_parent_instead(tmp_path):
    from ocdeck.app import OCDeckApp  # heavy import only here

    add_subagent(tmp_path, [TASK, FINISHED])
    records = collect(tmp_path)
    child = children(records)[0]
    parent = next(record for record in records if not record.parent_id)
    assert is_transcript_subagent(child) and not is_transcript_subagent(parent)
    opened, notices = [], []

    class Fake:
        session_by_id = {record.id: record for record in records}
        notify = lambda self, *args, **kwargs: notices.append(args)

    fake = Fake()
    real = OCDeckApp._open_existing_session
    fake._open_existing_session = lambda session, *a, **k: (opened.append(session.id), True)[1]
    assert real(fake, child) is True
    assert opened == [parent.id]
    fake.session_by_id = {}
    assert real(fake, child) is False and notices
