"""Codex adapter: rollout JSONL tailing (schema verified 2026-09-26).

Rollout files: ``~/.codex/sessions/**/rollout-<uuid>.jsonl``. Records are
``{ordinal, timestamp, type, payload}``; ``session_meta`` carries ``id`` /
``session_id`` / ``cwd``; tool *intent* lives in ``response_item`` payloads
of type ``custom_tool_call`` / ``function_call`` with ``name`` plus a string
``input`` (often a JS-style wrapper containing ``cmd:"..."``). Outputs
(``*_tool_call_output``) are ignored — intent only (C105).

``turn_context`` records carry ``approval_policy`` and
``file_system_sandbox_policy``; they are collected as policy evidence for
the future S11 rule (C3-r2) but do not raise alarms yet.
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

MAX_ROLLOUT_FILES = 200
MAX_LINE_BYTES = 1024 * 1024
MAX_EVENTS_PER_FILE = 200
CMD_PATTERN = re.compile(r'cmd\s*:\s*"((?:[^"\\]|\\.)*)"')

TOOL_CALL_TYPES = {"custom_tool_call", "function_call"}


class ToolEvent:
    __slots__ = ("harness", "session_id", "timestamp", "cwd", "tool", "input", "sidechain")

    def __init__(self, session_id: str, timestamp: str, cwd: str, tool: str, tool_input: dict):
        self.harness = "codex"
        self.session_id = session_id
        self.timestamp = timestamp
        self.cwd = cwd
        self.tool = tool
        self.input = tool_input
        self.sidechain = False


def rollout_files(home: Path | None = None) -> list[Path]:
    home = home or Path.home()
    root = home / ".codex" / "sessions"
    if not root.is_dir():
        return []
    files = [p for p in root.rglob("rollout-*.jsonl") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:MAX_ROLLOUT_FILES]


def _safe_open(path: Path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            return None
    except OSError:
        os.close(descriptor)
        return None
    return os.fdopen(descriptor, "rb")


def _read_meta(path: Path) -> tuple[str, str]:
    """Session id + cwd from the first session_meta record (cheap prefix read)."""
    handle = _safe_open(path)
    if handle is None:
        return "", ""
    with handle:
        prefix = handle.read(64 * 1024)
    for raw in prefix.split(b"\n"):
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("type") == "session_meta":
            payload = record.get("payload") or {}
            if isinstance(payload, dict):
                session_id = str(payload.get("session_id") or payload.get("id") or path.stem)
                cwd = str(payload.get("cwd") or "")
                return session_id, cwd
        break  # first non-blank record that is not session_meta: stop
    return path.stem, ""


def _tool_input(payload: dict) -> dict:
    raw = payload.get("input")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        # function_call inputs are JSON-encoded strings; custom_tool_call
        # inputs are JS-style wrappers containing cmd:"...".
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            pass
        match = CMD_PATTERN.search(raw)
        if match:
            try:
                return {"command": json.loads('"' + match.group(1) + '"')}
            except ValueError:
                return {"command": match.group(1)}
        return {"input": raw[:2000]}
    return {}


def scan_codex(state_dir: Path, *, home: Path | None = None):
    """Return (events, notes, status) for the Codex adapter.

    Cursors share ``collect``'s store (``cursors.json``); codex entries are
    merged into the loaded state so Claude's entries are never dropped.
    """
    from .collect import load_cursors, save_cursors

    home = home or Path.home()
    events: list[ToolEvent] = []
    notes: list[str] = []
    files = rollout_files(home)
    if not files:
        return [], [], "INACTIVE"

    cursors = load_cursors(state_dir)
    for path in files:
        key = str(path)
        try:
            metadata = path.stat()
        except OSError:
            continue
        cursor = cursors.get(key) or {}
        offset = cursor.get("offset", 0) if isinstance(cursor.get("offset"), int) else 0
        if cursor and metadata.st_size < offset:
            notes.append(f"rollout shrank or was replaced: {key}")
            offset = 0
        session_id, cwd = _read_meta(path)
        handle = _safe_open(path)
        if handle is None:
            continue
        with handle:
            handle.seek(offset)
            data = handle.read(MAX_LINE_BYTES * 64)
        consumed = offset
        produced = 0
        for raw in data.split(b"\n")[:-1]:
            consumed += len(raw) + 1
            if produced >= MAX_EVENTS_PER_FILE or len(raw) > MAX_LINE_BYTES:
                continue
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(record, dict) or record.get("type") != "response_item":
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict) or payload.get("type") not in TOOL_CALL_TYPES:
                continue
            name = payload.get("name")
            if not isinstance(name, str) or not name:
                continue
            events.append(ToolEvent(
                session_id=session_id or key,
                timestamp=str(record.get("timestamp") or ""),
                cwd=cwd,
                tool=name,
                tool_input=_tool_input(payload),
            ))
            produced += 1
        if data and not data.endswith(b"\n"):
            consumed = offset + len(data) - len(data.split(b"\n")[-1])
        cursors[key] = {
            "size": metadata.st_size, "mtime_ns": metadata.st_mtime_ns, "offset": consumed,
        }
    save_cursors(state_dir, cursors)
    return events, notes, "OBSERVED"
