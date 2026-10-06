"""Sessions the owner archived from OC Deck.

Archiving is OC Deck-only and reversible: nothing in Claude, Codex or
OpenCode changes and nothing is stopped — the session is only hidden from
this board until it is unarchived (or the state file is removed). Keys are
OC Deck's own session ids (``"opencode:ses_…"``, ``"claude:…"``, …).

The remembered state lives beside ``lineage.json`` under
``$XDG_STATE_HOME/ocdeck``: owner-only 0600, atomic replace writes, bounded
like LINEAGE_LIMIT in harnesses.py. A corrupt or missing file means nothing
is archived; reading and writing never crash the board.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .recent_open import SESSION_ID_PATTERN

# Like LINEAGE_LIMIT in harnesses.py: a bounded, cosmetic memory.
ARCHIVED_SESSIONS_LIMIT = 500


def default_archived_sessions_file() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")).expanduser()
    return base / "ocdeck/archived-sessions.json"


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
        if len(ordered) >= ARCHIVED_SESSIONS_LIMIT:
            break
    return ordered


def _load(path: Path) -> list[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return []
    return _valid_session_ids(payload.get("sessions"))


def _write(path: Path, session_ids: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(
            {"version": 1, "sessions": session_ids},
            handle,
            separators=(",", ":"),
        )
        handle.write("\n")
    os.replace(temporary, path)


def load_archived_sessions(path: Path | None = None) -> set[str]:
    """Session ids archived from OC Deck; reading never writes."""
    return set(_load(path or default_archived_sessions_file()))


def archive_session(session_id: str, path: Path | None = None) -> None:
    """Hide a session from OC Deck (reversible; nothing is ever stopped)."""
    if not isinstance(session_id, str) or not SESSION_ID_PATTERN.fullmatch(session_id):
        return
    path = path or default_archived_sessions_file()
    session_ids = _load(path)
    if session_id in session_ids:
        return
    session_ids.append(session_id)
    _write(path, session_ids[-ARCHIVED_SESSIONS_LIMIT:])


def unarchive_session(session_id: str, path: Path | None = None) -> None:
    """Show a previously archived session on the board again."""
    path = path or default_archived_sessions_file()
    session_ids = _load(path)
    if session_id not in session_ids:
        return
    session_ids.remove(session_id)
    _write(path, session_ids)
