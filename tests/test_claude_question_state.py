"""A Claude session blocked on AskUserQuestion / ExitPlanMode shows QUESTION, not RUN."""
from ocdeck.harnesses import ClaudeHarness, LiveProcess
from ocdeck.models import agent_state
from tests.test_claude_status import NOW, PROMPT, append

ASK = {
    "type": "assistant", "timestamp": "2026-09-26T10:01:00Z",
    "message": {"model": "claude-opus-5-5", "content": [
        {"type": "text", "text": "Three decisions are yours:"},
        {"type": "tool_use", "id": "toolu_ask1", "name": "AskUserQuestion",
         "input": {"questions": [{"question": "Which origins should be allowed?\nPick any.", "header": "Allowlist",
                                  "options": [], "multiSelect": True}]}},
    ]},
}
ANSWER = {
    "type": "user", "timestamp": "2026-09-26T10:01:30Z",
    "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_ask1", "content": "answered"}]},
}
PLAN = {
    "type": "assistant", "timestamp": "2026-09-26T10:01:00Z",
    "message": {"model": "claude-opus-5-5", "content": [
        {"type": "tool_use", "id": "toolu_plan1", "name": "ExitPlanMode", "input": {"plan": "do things"}},
    ]},
}
BASH = {
    "type": "assistant", "timestamp": "2026-09-26T10:01:00Z",
    "message": {"model": "claude-opus-5-5", "content": [
        {"type": "tool_use", "id": "toolu_bash1", "name": "Bash", "input": {"command": "sleep 100"}},
    ]},
}
INTERRUPTION = {
    "type": "user", "timestamp": "2026-09-26T10:01:40Z",
    "message": {"content": [{"type": "text", "text": "[Request interrupted by user for tool use]"}]},
}


def session_for(tmp_path, entries, live=True):
    append(tmp_path / "project" / "aaa.jsonl", entries)
    processes = [LiveProcess(1, "/project", ("claude", "--resume", "aaa"))] if live else []
    return ClaudeHarness(tmp_path, "/bin/claude").collect(processes=processes, tmux={}, now=NOW)[0]


def test_pending_ask_user_question_is_question_state(tmp_path):
    session = session_for(tmp_path, [PROMPT, ASK])
    assert session.question == "Which origins should be allowed? Pick any."
    assert not session.assistant_active
    assert agent_state(session, int(NOW * 1000)) == "question"


def test_answered_question_returns_to_running(tmp_path):
    session = session_for(tmp_path, [PROMPT, ASK, ANSWER])
    assert session.question == ""
    assert agent_state(session, int(NOW * 1000)) == "busy"


def test_pending_plan_approval_is_question_state(tmp_path):
    session = session_for(tmp_path, [PROMPT, PLAN])
    assert "plan" in session.question
    assert agent_state(session, int(NOW * 1000)) == "question"


def test_ordinary_running_tool_is_not_a_question(tmp_path):
    session = session_for(tmp_path, [PROMPT, BASH])
    assert session.question == ""
    assert agent_state(session, int(NOW * 1000)) == "busy"


def test_interrupted_question_is_cleared(tmp_path):
    session = session_for(tmp_path, [PROMPT, ASK, INTERRUPTION])
    assert session.question == ""
    assert agent_state(session, int(NOW * 1000)) != "question"


def test_question_in_a_dead_session_is_not_pending(tmp_path):
    session = session_for(tmp_path, [PROMPT, ASK], live=False)
    assert session.question == ""
    assert agent_state(session, int(NOW * 1000)) != "question"


def test_subagent_sidechain_question_is_ignored(tmp_path):
    sidechain = {**ASK, "isSidechain": True}
    session = session_for(tmp_path, [PROMPT, sidechain])
    assert session.question == ""
