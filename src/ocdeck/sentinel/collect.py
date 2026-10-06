"""Bounded, incremental transcript discovery and tailing (C102/C104 semantics).

Coverage is anchored to on-disk transcript roots, not launch records. Every
adapter declares an honest status; unknown shapes are skipped, never guessed.
Only complete newline-terminated records are consumed; a shrunk or replaced
file resets its cursor and raises a coverage note (S14 semantics).
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

MAX_TRANSCRIPT_FILES = 500
MAX_LINE_BYTES = 1024 * 1024
MAX_CURSOR_STATE_BYTES = 256 * 1024
BUSY_WINDOW = 0  # intent parsing needs no freshness window

ADAPTER_STATUS = {
    "claude": "OBSERVED",
    "opencode": "UNSUPPORTED pending verified API/event adapter",
    "codex": "INACTIVE while ~/.codex is absent",
}


@dataclass(slots=True)
class ToolEvent:
    """Intent evidence: one tool invocation seen in a transcript (C105)."""

    harness: str
    session_id: str
    timestamp: str
    cwd: str
    tool: str
    input: dict
    sidechain: bool = False


@dataclass(slots=True)
class ScanResult:
    events: list[ToolEvent] = field(default_factory=list)
    coverage_notes: list[str] = field(default_factory=list)
    files_scanned: int = 0
    adapter_status: dict[str, str] = field(default_factory=lambda: dict(ADAPTER_STATUS))


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


def claude_transcript_files(home: Path) -> list[Path]:
    root = home / ".claude" / "projects"
    if not root.is_dir():
        return []
    files = [p for p in root.rglob("*.jsonl") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:MAX_TRANSCRIPT_FILES]


def codex_rollout_files(home: Path) -> list[Path]:
    root = home / ".codex"
    if not root.is_dir():
        return []
    return [p for p in root.rglob("rollout-*.jsonl") if p.is_file()][:MAX_TRANSCRIPT_FILES]


def discover_roots(home: Path | None = None) -> dict[str, list[Path]]:
    home = home or Path.home()
    return {"claude": claude_transcript_files(home), "codex": codex_rollout_files(home)}


def state_file_path(state_dir: Path) -> Path:
    return state_dir / "cursors.json"


def load_cursors(state_dir: Path) -> dict[str, dict]:
    path = state_file_path(state_dir)
    try:
        handle = _safe_open(path)
        if handle is None:
            return {}
        with handle:
            if os.fstat(handle.fileno()).st_size > MAX_CURSOR_STATE_BYTES:
                return {}
            payload = json.loads(handle.read(MAX_CURSOR_STATE_BYTES + 1))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return {}
    cursors = payload.get("cursors")
    if not isinstance(cursors, dict):
        return {}
    return {k: v for k, v in cursors.items() if isinstance(v, dict)} if len(cursors) < MAX_TRANSCRIPT_FILES * 2 else {}


def save_cursors(state_dir: Path, cursors: dict[str, dict]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = json.dumps({"version": 1, "cursors": cursors}, separators=(",", ":")) + "\n"
    temporary = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=state_dir,
            prefix=".cursors.", delete=False,
        ) as handle:
            temporary = handle.name
            os.chmod(temporary, 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, state_file_path(state_dir))
        temporary = ""
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _parse_claude_line(line: bytes, fallback_session: str) -> ToolEvent | None:
    if len(line) > MAX_LINE_BYTES:
        return None
    try:
        record = json.loads(line)
    except ValueError:
        return None
    if not isinstance(record, dict) or record.get("type") != "assistant":
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, list):
        return None
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name")
        tool_input = block.get("input")
        if not isinstance(name, str) or not isinstance(tool_input, dict):
            continue
        return ToolEvent(
            harness="claude",
            session_id=str(record.get("sessionId") or fallback_session),
            timestamp=str(record.get("timestamp") or ""),
            cwd=str(record.get("cwd") or ""),
            tool=name,
            input=tool_input,
            sidechain=bool(record.get("isSidechain")),
        )
    return None


def scan_transcripts(
    state_dir: Path,
    *,
    home: Path | None = None,
) -> ScanResult:
    """Tail every discovered transcript once; return intent events + notes."""
    home = home or Path.home()
    result = ScanResult()
    cursors = load_cursors(state_dir)
    next_cursors: dict[str, dict] = {}

    for path in claude_transcript_files(home):
        key = str(path)
        try:
            metadata = path.stat()
        except OSError:
            continue
        result.files_scanned += 1
        cursor = cursors.get(key) or {}
        offset = int(cursor.get("offset") or 0) if isinstance(cursor.get("offset"), int) else 0
        if cursor and (
            int(cursor.get("size") or -1) != metadata.st_size
            or int(cursor.get("mtime_ns") or -1) != metadata.st_mtime_ns
        ):
            if metadata.st_size < offset:
                result.coverage_notes.append(f"transcript shrank or was replaced: {key}")
                offset = 0
        handle = _safe_open(path)
        if handle is None:
            continue
        with handle:
            handle.seek(offset)
            data = handle.read(MAX_LINE_BYTES * 64)
        consumed = offset
        for raw in data.split(b"\n")[:-1]:  # only complete lines
            consumed += len(raw) + 1
            event = _parse_claude_line(raw, path.stem)
            if event is not None:
                result.events.append(event)
        if data and not data.endswith(b"\n"):
            # Hold back the partial tail until the writer completes the line.
            consumed = offset + len(data) - len(data.split(b"\n")[-1])
        next_cursors[key] = {
            "size": metadata.st_size, "mtime_ns": metadata.st_mtime_ns, "offset": consumed,
        }

    for path in codex_rollout_files(home):
        result.adapter_status["codex"] = "UNSUPPORTED: rollout schema not verified locally"
        break
    save_cursors(state_dir, next_cursors)
    return result
