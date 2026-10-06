"""OpenCode V2 sentinel adapter tests (fake bridge, no live service)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ocdeck.sentinel.opencode import scan_opencode
from ocdeck.v2_read_api import ReadAPIError


def session(session_id: str, directory: str = "/home/user/proj") -> dict:
    return {"id": session_id, "location": {"directory": directory}}


def message(message_id: str, tool: str | None = None, tool_input: dict | None = None) -> dict:
    content = [{"type": "reasoning", "text": "thinking"}]
    if tool is not None:
        content.append({"type": "tool", "name": tool, "state": {"input": tool_input or {}, "status": "completed"}})
    return {"id": message_id, "time": {"created": 1790464862345}, "type": "assistant", "content": content}


class FakeBridge:
    def __init__(self, sessions, messages_by_session):
        self.sessions = sessions
        self.messages_by_session = messages_by_session
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, operation, *, params=None):
        self.calls.append((operation, dict(params or {})))
        if operation == "v2.session.list":
            return {"data": self.sessions}
        if operation == "v2.session.message.list":
            return {"data": self.messages_by_session.get(params["sessionID"], [])}
        raise ReadAPIError("unexpected operation")


class OpenCodeAdapterTests(unittest.TestCase):
    def _state(self) -> Path:
        directory = Path(tempfile.mkdtemp(prefix="sentinel-opencode-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        return directory

    def test_extracts_tool_events_with_cwd_and_input(self):
        bridge = FakeBridge([session("ses_a", "/home/user/proj")], {
            "ses_a": [message("msg_1", "read", {"filePath": "/home/user/proj/x.py"})],
        })
        events, notes, status = scan_opencode(self._state(), read=bridge)
        self.assertEqual(status, "OBSERVED")
        self.assertEqual(notes, [])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].harness, "opencode")
        self.assertEqual(events[0].session_id, "ses_a")
        self.assertEqual(events[0].cwd, "/home/user/proj")
        self.assertEqual(events[0].tool, "read")
        self.assertEqual(events[0].input, {"filePath": "/home/user/proj/x.py"})

    def test_cursor_deduplicates_and_picks_up_new_messages(self):
        bridge = FakeBridge([session("ses_a")], {
            "ses_a": [message("msg_1", "bash", {"command": "ls"})],
        })
        state = self._state()
        first, _, status = scan_opencode(state, read=bridge)
        self.assertEqual(status, "OBSERVED")
        self.assertEqual(len(first), 1)
        second, _, _ = scan_opencode(state, read=bridge)
        self.assertEqual(len(second), 0)
        bridge.messages_by_session["ses_a"].append(message("msg_2", "bash", {"command": "pwd"}))
        third, _, _ = scan_opencode(state, read=bridge)
        self.assertEqual([e.input.get("command") for e in third], ["pwd"])

    def test_session_list_failure_degrades(self):
        def failing(operation, *, params=None):
            raise ReadAPIError("service unavailable")

        events, notes, status = scan_opencode(self._state(), read=failing)
        self.assertEqual(status, "DEGRADED")
        self.assertEqual(events, [])
        self.assertTrue(any("unavailable" in note for note in notes))

    def test_all_message_lists_failing_degrades_with_partial_ok(self):
        bridge = FakeBridge([session("ses_a"), session("ses_b")], {
            "ses_a": [message("msg_1", "bash", {"command": "ls"})],
        })

        def partial(operation, *, params=None):
            if operation == "v2.session.list":
                return {"data": bridge.sessions}
            if (params or {}).get("sessionID") == "ses_b":
                raise ReadAPIError("unavailable")
            return {"data": bridge.messages_by_session["ses_a"]}

        events, notes, status = scan_opencode(self._state(), read=partial)
        self.assertEqual(status, "OBSERVED")
        self.assertEqual(len(events), 1)
        self.assertTrue(any("1/2" in note for note in notes))

    def test_unrecognized_shapes_are_rejected_not_guessed(self):
        def odd(operation, *, params=None):
            return {"unexpected": True}

        events, notes, status = scan_opencode(self._state(), read=odd)
        self.assertEqual(status, "DEGRADED")
        self.assertTrue(any("shape unrecognized" in note for note in notes))


if __name__ == "__main__":
    unittest.main()
