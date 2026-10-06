"""OpenCode V2 adapter: session messages via the sanctioned read bridge.

Discovered contract (verified live against OpenCode 2.0.14 on 2026-09-26):
``v2.session.list`` → ``{data: [{id, location: {directory}, ...}]}``;
``v2.session.message.list`` → ``{data: [{id, time: {created}, type,
content: [{type: "tool", name, state: {input, ...}}, ...]}]}``.

Only tool-call *intent* is extracted (name + input); text/reasoning content
is never read (C105/C106 minimization). Cursors are bounded seen-id sets per
session. Any bridge failure degrades this adapter to a coverage note while
leaving other adapters running (C102 truthful status, S14 semantics).
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from ..v2_read_api import ReadAPIError, read_api

MAX_SESSIONS = 50
MAX_MESSAGES_PER_SESSION = 50
MAX_SEEN_IDS_PER_SESSION = 200
MAX_CURSOR_FILE_BYTES = 512 * 1024


class ToolEvent:
    __slots__ = ("harness", "session_id", "timestamp", "cwd", "tool", "input", "sidechain")

    def __init__(self, session_id: str, timestamp: str, cwd: str, tool: str, tool_input: dict):
        self.harness = "opencode"
        self.session_id = session_id
        self.timestamp = timestamp
        self.cwd = cwd
        self.tool = tool
        self.input = tool_input if isinstance(tool_input, dict) else {}
        self.sidechain = False


def _cursor_path(state_dir: Path) -> Path:
    return state_dir / "opencode-cursors.json"


def _load_seen(state_dir: Path) -> dict[str, list[str]]:
    try:
        descriptor = os.open(_cursor_path(state_dir), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as handle:
            if os.fstat(handle.fileno()).st_size > MAX_CURSOR_FILE_BYTES:
                return {}
            payload = json.loads(handle.read(MAX_CURSOR_FILE_BYTES + 1))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return {}
    seen = payload.get("seen")
    if not isinstance(seen, dict):
        return {}
    return {
        session: ids[-MAX_SEEN_IDS_PER_SESSION:]
        for session, ids in seen.items()
        if isinstance(session, str) and isinstance(ids, list)
        and all(isinstance(item, str) for item in ids)
    }


def _save_seen(state_dir: Path, seen: dict[str, list[str]]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    trimmed = {session: ids[-MAX_SEEN_IDS_PER_SESSION:] for session, ids in seen.items()}
    payload = json.dumps({"version": 1, "seen": trimmed}, separators=(",", ":")) + "\n"
    temporary = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=state_dir, prefix=".opencode-cursors.", delete=False
        ) as handle:
            temporary = handle.name
            os.chmod(temporary, 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, _cursor_path(state_dir))
        temporary = ""
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _iso_timestamp(milliseconds) -> str:
    try:
        seconds = int(milliseconds) / 1000
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))
    except (TypeError, ValueError):
        return ""


def scan_opencode(state_dir: Path, *, read=read_api):
    """Return (events, notes, status) for the OpenCode V2 adapter."""
    seen = _load_seen(state_dir)
    events: list[ToolEvent] = []
    notes: list[str] = []
    try:
        listing = read("v2.session.list", params={"limit": MAX_SESSIONS})
    except ReadAPIError as error:
        return [], [f"opencode session list unavailable: {error}"], "DEGRADED"
    sessions = listing.get("data") if isinstance(listing, dict) else None
    if not isinstance(sessions, list):
        return [], ["opencode session list shape unrecognized"], "DEGRADED"

    failures = 0
    for session in sessions:
        if not isinstance(session, dict):
            continue
        session_id = session.get("id")
        if not isinstance(session_id, str) or not session_id:
            continue
        location = session.get("location")
        directory = ""
        if isinstance(location, dict) and isinstance(location.get("directory"), str):
            directory = location["directory"]
        try:
            payload = read(
                "v2.session.message.list",
                params={"sessionID": session_id, "limit": MAX_MESSAGES_PER_SESSION},
            )
        except ReadAPIError:
            failures += 1
            continue
        messages = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(messages, list):
            failures += 1
            continue
        known = set(seen.get(session_id) or [])
        fresh_ids: list[str] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            message_id = message.get("id")
            if not isinstance(message_id, str):
                continue
            if message_id in known and message_id in (seen.get(session_id) or []):
                continue
            fresh_ids.append(message_id)
            timestamp = _iso_timestamp((message.get("time") or {}).get("created"))
            for block in message.get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool":
                    continue
                name = block.get("name")
                if not isinstance(name, str) or not name:
                    continue
                state = block.get("state")
                tool_input = state.get("input") if isinstance(state, dict) else None
                events.append(ToolEvent(
                    session_id=session_id, timestamp=timestamp, cwd=directory,
                    tool=name, tool_input=tool_input or {},
                ))
        if fresh_ids or session_id in seen:
            merged = (seen.get(session_id) or []) + [i for i in fresh_ids if i not in (seen.get(session_id) or [])]
            seen[session_id] = merged[-MAX_SEEN_IDS_PER_SESSION:]
    if failures and failures >= len(sessions):
        notes.append("opencode message lists unavailable for every session")
        _save_seen(state_dir, seen)
        return events, notes, "DEGRADED"
    if failures:
        notes.append(f"opencode message lists failed for {failures}/{len(sessions)} sessions")
    _save_seen(state_dir, seen)
    return events, notes, "OBSERVED"
