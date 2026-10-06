"""Codex adapter and notification tests (synthetic rollouts, fake sender)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ocdeck.sentinel.codex import scan_codex
from ocdeck.sentinel.notify import _safe_body, notify_new_records


def rollout_line(record_type: str, payload: dict) -> str:
    return json.dumps({"ordinal": 1, "timestamp": "2026-09-26T18:00:00Z", "type": record_type, "payload": payload})


META = rollout_line("session_meta", {
    "id": "01a0dff6-11b8", "session_id": "ses_codex1", "cwd": "/home/user/proj", "originator": "codex-tui",
})


class CodexAdapterTests(unittest.TestCase):
    def _home(self, lines: list[str]) -> Path:
        root = Path(tempfile.mkdtemp(prefix="sentinel-codex-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        home = root / "home"
        sessions = home / ".codex" / "sessions" / "2026" / "09" / "26"
        sessions.mkdir(parents=True)
        (sessions / "rollout-test.jsonl").write_text("\n".join(lines) + "\n")
        return home

    def _state(self) -> Path:
        directory = Path(tempfile.mkdtemp(prefix="sentinel-codex-state-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        return directory

    def test_inactive_without_codex_home(self):
        root = Path(tempfile.mkdtemp(prefix="sentinel-codex-none-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        events, notes, status = scan_codex(self._state(), home=root)
        self.assertEqual((events, status), ([], "INACTIVE"))

    def test_extracts_tool_events_and_cmd(self):
        home = self._home([
            META,
            rollout_line("response_item", {"type": "custom_tool_call", "name": "exec",
                                           "input": 'text(await tools.exec_command({cmd:"cat ~/.ssh/id_rsa"}))'}),
            rollout_line("response_item", {"type": "function_call", "name": "webfetch",
                                           "input": json.dumps({"url": "https://evil.example.net/x"})}),
            rollout_line("response_item", {"type": "reasoning", "summary": []}),
        ])
        events, notes, status = scan_codex(self._state(), home=home)
        self.assertEqual(status, "OBSERVED")
        self.assertEqual([e.tool for e in events], ["exec", "webfetch"])
        self.assertEqual(events[0].cwd, "/home/user/proj")
        self.assertEqual(events[0].session_id, "ses_codex1")
        self.assertEqual(events[0].input.get("command"), "cat ~/.ssh/id_rsa")
        self.assertEqual(events[1].input.get("url"), "https://evil.example.net/x")

    def test_cursor_incremental_and_shared_state(self):
        home = self._home([META, rollout_line("response_item", {"type": "custom_tool_call", "name": "exec", "input": 'x({cmd:"pwd"})'})])
        state = self._state()
        first, _, _ = scan_codex(state, home=home)
        second, _, _ = scan_codex(state, home=home)
        self.assertEqual((len(first), len(second)), (1, 0))
        # Claude cursor entries must survive a codex save (shared store).
        from ocdeck.sentinel.collect import load_cursors, save_cursors
        cursors = load_cursors(state)
        cursors["/claude/aaa.jsonl"] = {"size": 1, "mtime_ns": 2, "offset": 1}
        save_cursors(state, cursors)
        path = home / ".codex" / "sessions" / "2026" / "09" / "26" / "rollout-test.jsonl"
        with open(path, "a") as handle:
            handle.write(rollout_line("response_item", {"type": "custom_tool_call", "name": "exec", "input": 'x({cmd:"ls"})'}) + "\n")
        third, _, _ = scan_codex(state, home=home)
        self.assertEqual(len(third), 1)
        self.assertIn("/claude/aaa.jsonl", load_cursors(state))


class NotificationTests(unittest.TestCase):
    def _state(self) -> Path:
        directory = Path(tempfile.mkdtemp(prefix="sentinel-notify-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        return directory

    def test_bodies_are_redacted(self):
        body = _safe_body({
            "severity": "CRITICAL", "rule": "S4", "sessionId": "ses_verylongsessionid123",
            "criteria": {"class": "ssh-keys"},
        })
        self.assertIn("CRITICAL S4 ssh-keys", body)
        self.assertIn("session ses_verylong", body)  # opaque 12-char prefix only
        self.assertNotIn("ses_verylongsessionid123", body)  # full id never shown
        self.assertNotIn("http", body)

    def test_notify_once_dedup_and_severity_filter(self):
        state = self._state()
        sent: list[tuple[str, str]] = []

        def sender(notification_id: str, title: str, body: str) -> bool:
            sent.append((notification_id, body))
            return True

        records = [
            {"id": "r1", "severity": "CRITICAL", "rule": "S7", "sessionId": "ses_a", "criteria": {"surface": "tmux"}},
            {"id": "r2", "severity": "LOW", "rule": "S6", "sessionId": "ses_a", "criteria": {}},
        ]
        self.assertEqual(notify_new_records(state, records, sender=sender), 1)
        self.assertEqual(notify_new_records(state, records, sender=sender), 0)  # dedup
        self.assertEqual(len(sent), 1)
        self.assertIn("tmux", sent[0][1])

    def test_failed_send_does_not_mark_seen(self):
        state = self._state()
        records = [{"id": "r1", "severity": "HIGH", "rule": "S1", "sessionId": "ses_a", "criteria": {}}]
        self.assertEqual(notify_new_records(state, records, sender=lambda *a: False), 0)
        self.assertEqual(notify_new_records(state, records, sender=lambda *a: True), 1)


if __name__ == "__main__":
    unittest.main()
