"""Who opened a session: the owner, or an agent program.

The Agents board must show sessions an agent opened differently from sessions
the owner opened. Only verified signals decide, never title text:

1. The owner launched or resumed it from OC Deck — recorded here, and the
   strongest signal: an owner-recorded launch renders normally even when an
   agent signal also matches (the owner took the session over).
2. An agent signal: OpenCode's own ``parentID``, OC Deck's lineage map, a
   headless Claude transcript ``entrypoint`` (``"sdk-cli"``, written by
   ``claude -p``; the interactive TUI writes ``"cli"``), or a headless Codex
   rollout ``source`` (``session_meta.payload.source``; seen locally:
   ``"vscode"``/``"codex-tui"`` for an interactive owner launch).
3. Anything else is UNKNOWN and renders like owner rows: most of the owner's
   older sessions carry no signal, and dimming them would dim the owner's own
   work. OpenCode V1/V2 storage records nothing that distinguishes
   ``opencode run`` from a TUI session, so those sessions stay unknown until
   the owner opens them from OC Deck.

The remembered state lives beside ``lineage.json`` under
``$XDG_STATE_HOME/ocdeck``: owner-only 0600, atomic replace writes, bounded.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .models import SessionRecord
from .recent_open import SESSION_ID_PATTERN

OWNER_VERDICT = "owner"
AGENT_VERDICT = "agent"
UNKNOWN_VERDICT = "unknown"
AGENT_MARKER = "[agent]"
AGENT_TITLE_STYLE = "dim #8ba4b5"

# Like LINEAGE_LIMIT in harnesses.py: a bounded, cosmetic memory.
OWNER_OPENED_LIMIT = 500
OWNER_PENDING_LIMIT = 64
# A launch whose session never appears is forgotten rather than kept forever.
OWNER_PENDING_EXPIRY_MS = 15 * 60 * 1000
# A slow CLI may need a while before its new session shows up in a snapshot.
OWNER_PENDING_MATCH_WINDOW_MS = 10 * 60 * 1000
OWNER_CREATED_SLOP_MS = 5_000

# Claude transcript entrypoints: "sdk-cli" is a headless `claude -p` run an
# agent started; "cli" is the interactive TUI.
CLAUDE_AGENT_ENTRYPOINTS = {"sdk-cli"}
CLAUDE_OWNER_ENTRYPOINTS = {"cli"}
# Codex rollout sources (session_meta.payload.source, camelCase in the file):
# exec/appServer/mcp/subAgent* are programmatic launches, cli/vscode are
# interactive ones an owner sits at.
CODEX_AGENT_SOURCES = {
    "exec", "appserver", "mcp", "subagentreview", "subagentcompact",
    "subagentthreadspawn", "subagentother",
}
CODEX_OWNER_SOURCES = {"cli", "vscode"}

TERMINAL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class SessionOrigin:
    """One session's verdict plus the signal that decided it."""

    verdict: str
    signal: str


def default_owner_opened_file() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")).expanduser()
    return base / "ocdeck/owner-opened.json"


def _valid_session_ids(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    ordered: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not SESSION_ID_PATTERN.fullmatch(value):
            continue
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
        if len(ordered) >= OWNER_OPENED_LIMIT:
            break
    return ordered


def _valid_pending(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []
    pending: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, dict):
            continue
        terminal = value.get("terminal")
        directory = value.get("directory")
        recorded_ms = value.get("recorded_ms")
        if (
            not isinstance(terminal, str)
            or not TERMINAL_NAME_PATTERN.fullmatch(terminal)
            or terminal in seen
            or not isinstance(directory, str)
            or not directory
            or len(directory) > 4096
            or not isinstance(recorded_ms, int)
            or recorded_ms <= 0
        ):
            continue
        seen.add(terminal)
        pending.append(
            {"terminal": terminal, "directory": directory, "recorded_ms": recorded_ms}
        )
        if len(pending) >= OWNER_PENDING_LIMIT:
            break
    return pending


def _load(path: Path) -> tuple[list[str], list[dict[str, Any]]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], []
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return [], []
    return _valid_session_ids(payload.get("sessions")), _valid_pending(payload.get("pending"))


def _write(path: Path, session_ids: list[str], pending: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(
            {"version": 1, "sessions": session_ids, "pending": pending},
            handle,
            separators=(",", ":"),
        )
        handle.write("\n")
    os.replace(temporary, path)


def load_owner_sessions(path: Path | None = None) -> set[str]:
    """Session ids recorded as owner-opened; reading never writes."""
    return set(_load(path or default_owner_opened_file())[0])


def record_owner_session(session_id: str, path: Path | None = None) -> None:
    """Remember a session the owner opened from OC Deck."""
    if not isinstance(session_id, str) or not SESSION_ID_PATTERN.fullmatch(session_id):
        return
    path = path or default_owner_opened_file()
    session_ids, pending = _load(path)
    if session_id in session_ids:
        return
    session_ids.append(session_id)
    session_ids = session_ids[-OWNER_OPENED_LIMIT:]
    _write(path, session_ids, pending)


def record_owner_launch(
    terminal: str, directory: str, path: Path | None = None, *, now_ms: int | None = None
) -> None:
    """Remember an owner launch whose session id is not known yet.

    OpenCode v1 new sessions and Codex new sessions get their id from the CLI
    after it starts; the tmux terminal OC Deck launched (and the directory)
    identifies the session once a snapshot shows it.
    """
    if not isinstance(terminal, str) or not TERMINAL_NAME_PATTERN.fullmatch(terminal):
        return
    if not isinstance(directory, str) or not directory or len(directory) > 4096:
        return
    path = path or default_owner_opened_file()
    session_ids, pending = _load(path)
    entry = {
        "terminal": terminal,
        "directory": directory,
        "recorded_ms": now_ms if now_ms is not None else int(time.time() * 1000),
    }
    pending = [item for item in pending if item.get("terminal") != terminal]
    pending.append(entry)
    pending = pending[-OWNER_PENDING_LIMIT:]
    _write(path, session_ids, pending)


def _normalized(directory: str) -> str:
    return os.path.normpath(os.path.expanduser(directory)) if directory else ""


def _pending_match(
    entry: dict[str, Any], sessions: Iterable[SessionRecord], claimed: set[str]
) -> str:
    """The session an owner launch produced, if a snapshot shows it."""
    terminal = entry.get("terminal", "")
    directory = _normalized(entry.get("directory", ""))
    recorded_ms = entry.get("recorded_ms", 0)
    by_created = sorted(sessions, key=lambda session: session.created_ms, reverse=True)
    for session in by_created:
        if session.id in claimed:
            continue  # an already-claimed session is never a new launch's match
        if terminal in session.terminals:
            return session.id
    # An OpenCode launch without --session never names its terminal in a
    # snapshot; match the session the CLI created in the launch directory.
    for session in by_created:
        if session.id in claimed or session.harness != "opencode":
            continue
        if session.parent_id or session.agent_parent_id:
            continue  # an agent-opened session is never the owner's launch
        if (
            _normalized(session.directory) == directory
            and recorded_ms - OWNER_CREATED_SLOP_MS
            <= session.created_ms
            <= recorded_ms + OWNER_PENDING_MATCH_WINDOW_MS
        ):
            return session.id
    return ""


def refresh_owner_opened(
    sessions: Iterable[SessionRecord],
    path: Path | None = None,
    *,
    now_ms: int | None = None,
) -> set[str]:
    """Resolve pending owner launches against a snapshot; return owner ids.

    Reads only unless a pending launch resolved or expired.
    """
    path = path or default_owner_opened_file()
    session_ids, pending = _load(path)
    if not pending:
        return set(session_ids)
    sessions = tuple(sessions)
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    claimed = set(session_ids)
    kept: list[dict[str, Any]] = []
    for entry in pending:  # launch order: the oldest launch claims first
        if entry.get("recorded_ms", 0) + OWNER_PENDING_EXPIRY_MS <= now_ms:
            continue  # a launch whose session never appeared is forgotten, not matched
        match = _pending_match(entry, sessions, claimed)
        if match:
            claimed.add(match)
            session_ids.append(match)
        else:
            kept.append(entry)
    session_ids = _valid_session_ids(session_ids)[-OWNER_OPENED_LIMIT:]
    if kept != pending or len(session_ids) != len(claimed) and set(session_ids) != claimed:
        _write(path, session_ids, kept)
    return set(session_ids)


def classify_session(session: SessionRecord, owner_ids: Iterable[str]) -> SessionOrigin:
    """Verdict plus signal, by precedence: owner record, agent signals, none."""
    if session.id in owner_ids:
        return SessionOrigin(OWNER_VERDICT, "owner-recorded launch")
    if session.parent_id and session.parent_id != session.id:
        return SessionOrigin(AGENT_VERDICT, "opencode parentID")
    if session.agent_parent_id and session.agent_parent_id != session.id:
        return SessionOrigin(AGENT_VERDICT, "ocdeck lineage")
    if session.harness == "claude" and session.launch_source:
        if session.launch_source in CLAUDE_AGENT_ENTRYPOINTS:
            return SessionOrigin(AGENT_VERDICT, f'claude entrypoint "{session.launch_source}"')
        if session.launch_source in CLAUDE_OWNER_ENTRYPOINTS:
            return SessionOrigin(OWNER_VERDICT, f'claude entrypoint "{session.launch_source}"')
    if session.harness == "codex" and session.launch_source:
        source = re.sub(r"[-_]", "", session.launch_source.lower())
        if source in CODEX_AGENT_SOURCES:
            return SessionOrigin(AGENT_VERDICT, f'codex source "{session.launch_source}"')
        if source in CODEX_OWNER_SOURCES:
            return SessionOrigin(OWNER_VERDICT, f'codex source "{session.launch_source}"')
    return SessionOrigin(UNKNOWN_VERDICT, "no signal")
