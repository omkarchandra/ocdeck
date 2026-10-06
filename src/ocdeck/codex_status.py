"""Read Codex approval state from its existing local app-server daemon.

Rollout transcripts omit pending approvals. A bounded local WebSocket
client only initializes and reads thread metadata.
It never starts/resumes a thread, subscribes, or answers an approval request.
Protocol: https://learn.chatgpt.com/docs/app-server
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import struct
import stat
import time
from dataclasses import dataclass, replace
from pathlib import Path

from .models import SessionRecord

MAX_THREADS = 32
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
STATUS_TIMEOUT_SECONDS = 1.5


@dataclass(frozen=True)
class CodexRuntimeStatus:
    state: str
    flags: tuple[str, ...] = ()


def _parse_status(result: object, expected_id: str) -> CodexRuntimeStatus | None:
    if not isinstance(result, dict):
        return None
    thread = result.get("thread")
    if not isinstance(thread, dict) or thread.get("id") != expected_id:
        return None
    status = thread.get("status")
    if not isinstance(status, dict):
        return None
    state, flags = status.get("type"), status.get("activeFlags", [])
    if state not in ("active", "idle", "notLoaded", "systemError"):
        return None
    if not isinstance(flags, list) or any(not isinstance(flag, str) for flag in flags):
        return None
    return CodexRuntimeStatus(state, tuple(flags) if state == "active" else ())


class _LocalWebSocket:
    """Minimal RFC 6455 client for one bounded, local metadata exchange."""

    def __init__(self, connection, deadline):
        self.connection = connection
        self.deadline = deadline
        self.received = 0

    def timeout(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Codex status deadline exceeded")
        self.connection.settimeout(remaining)

    def read(self, length):
        if length > MAX_RESPONSE_BYTES or self.received + length > MAX_RESPONSE_BYTES:
            raise ValueError("Codex status response too large")
        chunks = bytearray()
        while len(chunks) < length:
            self.timeout()
            chunk = self.connection.recv(length - len(chunks))
            if not chunk:
                raise OSError("Codex status connection closed")
            chunks.extend(chunk)
        self.received += length
        return bytes(chunks)

    def write(self, data):
        self.timeout()
        self.connection.sendall(data)

    def handshake(self):
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            "GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
            f"Sec-WebSocket-Key: {key}\r\n\r\n"
        )
        self.write(request.encode())
        header = bytearray()
        while not header.endswith(b"\r\n\r\n"):
            if len(header) >= 8192:
                raise ValueError("Oversized WebSocket headers")
            header.extend(self.read(1))
        lines = header.decode("ascii").split("\r\n")
        fields = {}
        for line in lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                fields[name.lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
        ).digest()).decode()
        if (lines[0].split()[:2] != ["HTTP/1.1", "101"]
                or fields.get("sec-websocket-accept") != expected
                or fields.get("upgrade", "").lower() != "websocket"
                or "upgrade" not in {value.strip().lower() for value in fields.get("connection", "").split(",")}):
            raise ValueError("Invalid WebSocket upgrade")

    def frame(self, opcode, payload):
        mask = os.urandom(4)
        size = len(payload)
        header = bytes([0x80 | opcode])
        if size < 126:
            header += bytes([0x80 | size])
        elif size <= 65535:
            header += bytes([0x80 | 126]) + struct.pack("!H", size)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", size)
        self.write(header + mask + bytes(value ^ mask[i % 4] for i, value in enumerate(payload)))

    def send(self, message):
        self.frame(1, json.dumps(message, separators=(",", ":")).encode())

    def receive(self):
        message = bytearray()
        started = False
        while True:
            first, second = self.read(2)
            final, opcode = bool(first & 0x80), first & 15
            if first & 0x70 or second & 0x80:
                raise ValueError("Unsupported WebSocket frame")
            length = second & 127
            if opcode >= 8 and (not final or length > 125):
                raise ValueError("Invalid control frame")
            if length == 126:
                length = struct.unpack("!H", self.read(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self.read(8))[0]
            payload = self.read(length)
            if opcode == 8:
                raise OSError("Codex status connection closed")
            if opcode == 9:
                self.frame(10, payload)
                continue
            if opcode == 10:
                continue
            if opcode not in (0, 1) or (opcode == 0) != started:
                raise ValueError("Unexpected WebSocket message")
            started = True
            message.extend(payload)
            if final:
                return json.loads(message.decode("utf-8"))


def read_statuses(home: Path, thread_ids: list[str]) -> dict[str, CodexRuntimeStatus]:
    """Read only requested threads; never cache approval state across refreshes."""
    wanted = list(dict.fromkeys(
        native for native in thread_ids
        if isinstance(native, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", native)
    ))[:MAX_THREADS]
    if not wanted:
        return {}
    path = home / "app-server-control" / "app-server-control.sock"
    statuses: dict[str, CodexRuntimeStatus] = {}
    try:
        info = path.stat()  # Codex symlinks this to its /tmp socket.
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            return {}
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            wire = _LocalWebSocket(connection, time.monotonic() + STATUS_TIMEOUT_SECONDS)
            wire.timeout()
            connection.connect(str(path))
            wire.handshake()
            wire.send({"id": 0, "method": "initialize", "params": {
                "clientInfo": {"name": "ocdeck_status", "version": "0.1.0"},
            }})
            pending = {index: native for index, native in enumerate(wanted, 1)}
            initialized = False
            while pending:
                message = wire.receive()
                if not isinstance(message, dict) or "method" in message:
                    # Never answer server requests, including approvals.
                    continue
                request_id = message.get("id")
                if type(request_id) is not int:
                    continue
                if request_id == 0 and not initialized:
                    if "error" in message or not isinstance(message.get("result"), dict):
                        return {}
                    wire.send({"method": "initialized"})
                    for index, native in pending.items():
                        wire.send({"id": index, "method": "thread/read", "params": {
                            "threadId": native, "includeTurns": False,
                        }})
                    initialized = True
                elif initialized and request_id in pending:
                    native = pending.pop(request_id)
                    status = _parse_status(message.get("result"), native)
                    if status is not None and "error" not in message:
                        statuses[native] = status
    except (OSError, ValueError, RecursionError):
        pass
    return statuses


def apply_statuses(records: list[SessionRecord], statuses: dict[str, CodexRuntimeStatus]) -> list[SessionRecord]:
    """Overlay live signals while retaining native approval handling."""
    result = []
    for record in records:
        status = statuses.get(record.id.removeprefix("codex:")) if record.harness == "codex" else None
        if status is None or status.state in {"notLoaded", "systemError"}:
            result.append(record)
            continue
        permission = "Codex approval required — open the Codex terminal to respond" if "waitingOnApproval" in status.flags else ""
        question = "Codex needs input — open the Codex terminal to respond" if "waitingOnUserInput" in status.flags else ""
        busy = status.state == "active" and not permission and not question
        result.append(replace(record, status="busy" if busy else "idle",
                              assistant_active=busy, permission=permission, question=question))
    return result
