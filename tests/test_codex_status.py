"""Read-only Codex status protocol, approval transitions, and rollout events."""
import base64
import hashlib
import json
import os
import stat
import struct
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from ocdeck import codex_status as status_api
from ocdeck.codex_status import CodexRuntimeStatus, _LocalWebSocket, apply_statuses, read_statuses
from ocdeck.harnesses import CodexHarness, LiveProcess
from ocdeck.models import SessionRecord, agent_state


def frame(payload, opcode=1, final=True):
    if not isinstance(payload, bytes):
        payload = json.dumps(payload).encode()
    size = len(payload)
    prefix = bytes([(0x80 if final else 0) | opcode])
    prefix += bytes([size]) if size < 126 else bytes([126]) + struct.pack("!H", size)
    return prefix + payload


class FakeSocket:
    def __init__(self, responses=None):
        self.incoming = bytearray()
        self.sent = []
        self.closed = False
        self.responses = responses or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def settimeout(self, timeout):
        assert 0 < timeout <= status_api.STATUS_TIMEOUT_SECONDS

    def connect(self, path):
        assert path.endswith("app-server-control/app-server-control.sock")

    def sendall(self, data):
        if data.startswith(b"GET "):
            key = data.split(b"Sec-WebSocket-Key: ")[1].split(b"\r\n")[0]
            accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
            self.incoming.extend(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
            return
        assert data[1] & 0x80  # Client frames must be masked.
        size, offset = data[1] & 127, 2
        if size == 126:
            size, offset = struct.unpack("!H", data[2:4])[0], 4
        mask = data[offset:offset + 4]
        payload = bytes(value ^ mask[i % 4] for i, value in enumerate(data[offset + 4:]))
        assert len(payload) == size
        if data[0] & 15 == 10:
            self.sent.append(("pong", payload))
            return
        message = json.loads(payload)
        self.sent.append(message)
        if message.get("method") == "initialize":
            self.incoming.extend(frame({"id": 0, "result": {}}))
        elif message.get("method") == "thread/read":
            native = message["params"]["threadId"]
            response = self.responses.get(native, {"type": "active", "activeFlags": ["waitingOnApproval"]})
            self.incoming.extend(frame({"id": message["id"], "result": {"thread": {"id": native, "status": response}}}))

    def recv(self, length):
        if not self.incoming:
            raise TimeoutError("no response")
        length = min(length, 7)  # Every response arrives in partial chunks.
        data = bytes(self.incoming[:length])
        del self.incoming[:length]
        return data


def connect(monkeypatch, responses=None):
    connection = FakeSocket(responses)
    monkeypatch.setattr(Path, "stat", lambda path: SimpleNamespace(st_mode=stat.S_IFSOCK, st_uid=os.getuid()))
    monkeypatch.setattr(status_api.socket, "socket", lambda *args: connection)
    return connection


def session():
    return SessionRecord(id="codex:abc", title="Codex", directory="/p", project_id="p",
                         created_ms=1, updated_ms=2, instance_count=1, harness="codex",
                         status="busy", assistant_active=True)


def test_read_only_protocol_correlates_and_closes(monkeypatch, tmp_path):
    connection = connect(monkeypatch, {"def": {"type": "idle"}})
    result = read_statuses(tmp_path, ["abc", "abc", "def", "bad\nname"])
    assert result == {"abc": CodexRuntimeStatus("active", ("waitingOnApproval",)), "def": CodexRuntimeStatus("idle")}
    assert [m["method"] for m in connection.sent] == ["initialize", "initialized", "thread/read", "thread/read"]
    assert all(m["params"]["includeTurns"] is False for m in connection.sent if m["method"] == "thread/read")
    assert connection.closed


def test_foreign_server_requests_never_get_answered(monkeypatch, tmp_path):
    connection = connect(monkeypatch)
    original = connection.sendall
    def send(data):
        original(data)
        if data.startswith(b"GET "):
            connection.incoming.extend(frame({"method": "item/commandExecution/requestApproval", "id": 777, "params": {}}))
    connection.sendall = send
    assert "abc" in read_statuses(tmp_path, ["abc"])
    assert all(m.get("id") != 777 for m in connection.sent)


def test_thread_count_bound_and_no_socket_when_missing(tmp_path, monkeypatch):
    assert read_statuses(tmp_path, ["abc"]) == {}
    connection = connect(monkeypatch)
    assert len(read_statuses(tmp_path, [f"t{i}" for i in range(100)])) == status_api.MAX_THREADS
    assert len(connection.sent) == 2 + status_api.MAX_THREADS


def test_wrong_owner_or_regular_file_never_connected(monkeypatch, tmp_path):
    with mock.patch.object(status_api.socket, "socket", side_effect=AssertionError("must not connect")):
        for mode, uid in ((stat.S_IFREG, os.getuid()), (stat.S_IFSOCK, os.getuid() + 1)):
            monkeypatch.setattr(Path, "stat", lambda path: SimpleNamespace(st_mode=mode, st_uid=uid))
            assert read_statuses(tmp_path, ["abc"]) == {}


@pytest.mark.parametrize("response", [{}, {"type": "unknown"}, {"type": "active", "activeFlags": "waitingOnApproval"},
                                     {"type": "active", "activeFlags": [None]}])
def test_malformed_runtime_status_is_not_a_permission(monkeypatch, tmp_path, response):
    connect(monkeypatch, {"abc": response})
    assert read_statuses(tmp_path, ["abc"]) == {}


def test_mismatched_thread_response_is_rejected():
    assert status_api._parse_status({"thread": {"id": "different", "status": {"type": "active"}}}, "abc") is None


def test_timeout_and_oversized_frames_fail_bounded(monkeypatch, tmp_path):
    connection = connect(monkeypatch)
    connection.sendall = lambda data: None
    assert read_statuses(tmp_path, ["abc"]) == {}
    assert connection.closed
    connection = connect(monkeypatch)
    connection.sendall = lambda data: None
    connection.incoming.extend(b"HTTP/1.1 101 " + b"x" * 8192)
    assert read_statuses(tmp_path, ["abc"]) == {}
    connection = FakeSocket()
    connection.incoming.extend(b"\x81\x7f" + struct.pack("!Q", status_api.MAX_RESPONSE_BYTES + 1))
    wire = _LocalWebSocket(connection, status_api.time.monotonic() + 1)
    with pytest.raises(ValueError, match="too large"):
        wire.receive()


def test_fragmentation_ping_and_control_frames():
    connection = FakeSocket()
    connection.incoming.extend(frame(b'{"id":', final=False) + frame(b"hello", opcode=9) + frame(b'1}', opcode=0))
    wire = _LocalWebSocket(connection, status_api.time.monotonic() + 1)
    assert wire.receive() == {"id": 1}
    assert connection.sent == [("pong", b"hello")]


@pytest.mark.parametrize("data", [b"\x80\x00", b"\x82\x00", b"\xc1\x00", b"\x81\x80", b"\x09\x00", b"\x89\x7e"])
def test_invalid_websocket_frames_fail_closed(data):
    connection = FakeSocket()
    connection.incoming.extend(data)
    wire = _LocalWebSocket(connection, status_api.time.monotonic() + 1)
    with pytest.raises(ValueError):
        wire.receive()


def test_permission_then_running_then_idle_without_stale_state():
    original = session()
    waiting = apply_statuses([original], {"abc": CodexRuntimeStatus("active", ("waitingOnApproval",))})[0]
    assert agent_state(waiting) == "permission"
    assert waiting.permission_id == ""  # No OpenCode reply capability fabricated.
    running = apply_statuses([waiting], {"abc": CodexRuntimeStatus("active")})[0]
    assert agent_state(running) == "busy" and not running.permission
    idle = apply_statuses([running], {"abc": CodexRuntimeStatus("idle")})[0]
    assert not idle.assistant_active and not idle.permission
    assert apply_statuses([original], {}) == [original]


def test_user_input_and_other_harnesses():
    original = session()
    question = apply_statuses([original], {"abc": CodexRuntimeStatus("active", ("waitingOnUserInput",))})[0]
    assert agent_state(question) == "question"
    foreign = replace(original, harness="claude")
    assert apply_statuses([foreign], {"abc": CodexRuntimeStatus("active", ("waitingOnApproval",))}) == [foreign]


def rollout(tmp_path, entries):
    path = tmp_path / "rollout-abc.jsonl"
    items = [{"type": "session_meta", "timestamp": "2026-09-26T10:00:00Z", "payload": {"id": "abc", "cwd": "/p"}}]
    items.extend({"type": "event_msg", "timestamp": f"2026-09-26T10:00:{i + 1:02}Z", "payload": {"type": kind}} for i, kind in enumerate(entries))
    path.write_text("\n".join(map(json.dumps, items)) + "\n")
    return path


def test_task_started_reopens_turn_after_task_complete(tmp_path):
    path = rollout(tmp_path, ["task_started", "task_complete", "task_started"])
    adapter = CodexHarness(tmp_path, "/bin/codex")
    info = adapter.parse_transcript(path, path.stat().st_size)
    assert info.turn_open is True and info.prompt_ms > info.done_ms
    with mock.patch("ocdeck.harnesses.read_codex_statuses", return_value={}):
        records = adapter.collect(processes=[LiveProcess(42, "/p", ("codex", "resume", "abc"))], tmux={}, now=2_000_000_000)
    assert agent_state(records[0]) == "busy"  # A long turn stays active.


def test_response_item_user_message_and_abort_boundaries(tmp_path):
    path = rollout(tmp_path, ["task_complete"])
    with path.open("a") as file:
        file.write(json.dumps({"type": "response_item", "timestamp": "2026-09-26T10:01:00Z", "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": "continue"}]}}) + "\n")
    adapter = CodexHarness(tmp_path, "/bin/codex")
    info = adapter.parse_transcript(path, path.stat().st_size)
    assert info.turn_open is True and info.last_prompt == "continue"
    path = rollout(tmp_path, ["task_started", "turn_aborted"])
    assert adapter.parse_transcript(path, path.stat().st_size).turn_open is False


def test_current_codex_completed_user_item_updates_last_prompt(tmp_path):
    path = rollout(tmp_path, ["task_complete"])
    with path.open("a") as file:
        file.write(json.dumps({"type": "event_msg", "timestamp": "2026-09-26T10:01:00Z", "payload": {
            "type": "item_completed", "item": {"type": "UserMessage", "content": [
                {"type": "local_image", "path": "/tmp/example.png"},
                {"type": "text", "text": "Why does this say idle?", "text_elements": []},
            ]}}}) + "\n")
    adapter = CodexHarness(tmp_path, "/bin/codex")
    info = adapter.parse_transcript(path, path.stat().st_size)
    assert info.last_prompt == "Why does this say idle?"
    assert info.turn_open is True and info.prompt_ms > info.done_ms


def test_adapter_queries_only_live_sessions_and_refresh_clears_permission(tmp_path):
    rollout(tmp_path, ["task_started"])
    adapter = CodexHarness(tmp_path, "/bin/codex")
    with mock.patch("ocdeck.harnesses.read_codex_statuses", side_effect=[
        {"abc": CodexRuntimeStatus("active", ("waitingOnApproval",))}, {},
    ]) as read:
        first = adapter.collect(processes=[], tmux={"cx-abc": (True, ())})
        second = adapter.collect(processes=[], tmux={"cx-abc": (True, ())})
    assert agent_state(first[0]) == "permission"
    assert agent_state(second[0]) == "busy" and not second[0].permission
    read.assert_called_with(tmp_path.parent, ["abc"])
