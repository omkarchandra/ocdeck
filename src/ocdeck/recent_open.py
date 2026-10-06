"""Persist the most recently open agent sessions for quick relaunch."""

from __future__ import annotations

import json
import fcntl
import hashlib
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Iterable

MAX_RECENT_OPEN_SESSIONS = 20
# OpenCode ids, plus other harnesses' sessions keyed as "<harness>:<id>".
SESSION_ID_PATTERN = re.compile(r"^(?:ses_|[a-z][a-z0-9_]{1,19}:)[A-Za-z0-9_-]{1,125}$")
MAX_HISTORY_BYTES = 16 * 1024


def default_recent_open_file(backend: str = "v1", server: str = "") -> Path:
    if backend not in {"v1", "v2"}:
        raise ValueError("Unsupported history backend")
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    server = server.rstrip("/")
    legacy = backend == "v1" and server in {"", "http://127.0.0.1:4096"}
    suffix = "" if legacy else f"-{backend}"
    if server and not legacy:
        suffix += "-" + hashlib.sha256(server.encode()).hexdigest()[:12]
    return state / f"ocdeck/recently-open-sessions{suffix}.json"


def _valid_session_ids(values: object) -> list[str]:
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
        if len(ordered) >= MAX_RECENT_OPEN_SESSIONS:
            break
    return ordered


def load_recent_open_sessions(path: Path) -> list[str]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_HISTORY_BYTES:
                return []
            payload = json.loads(handle.read(MAX_HISTORY_BYTES + 1))
    except (OSError, ValueError):
        return []
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return []
    return _valid_session_ids(payload.get("sessions"))


def save_recent_open_sessions(
    path: Path, session_ids: Iterable[str], *, remembered: Iterable[str] = ()
) -> list[str] | None:
    """Merge observations under a writer lock; never prune by a partial listing."""
    promoted = _valid_session_ids(list(session_ids))
    temporary = ""
    lock = -1
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = os.open(path.with_name(f".{path.name}.lock"),
                       os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        if not stat.S_ISREG(os.fstat(lock).st_mode):
            raise OSError("History lock is not a regular file")
        fcntl.flock(lock, fcntl.LOCK_EX)
        existing = load_recent_open_sessions(path)
        ordered = _valid_session_ids(promoted + existing + list(remembered))
        if ordered == existing and path.is_file():
            return ordered
        payload = json.dumps({"version": 1, "sessions": ordered}, separators=(",", ":")) + "\n"
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temporary = handle.name
            os.chmod(temporary, 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = ""
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return ordered
    except OSError:
        return None
    finally:
        if lock >= 0:
            os.close(lock)
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


class RecentOpenHistory:
    """Persistent observations, independent from the UI's filtered snapshots."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.ids = load_recent_open_sessions(path) if path is not None else []
        self.open_ids: set[str] = set()
        self.write_failed = False

    def observe(self, open_ids: Iterable[str]) -> None:
        current = _valid_session_ids(list(open_ids))
        newly_open = [session_id for session_id in current if session_id not in self.open_ids]
        self.open_ids = set(current)
        if not newly_open and not self.write_failed:
            return
        self.ids = _valid_session_ids(newly_open + self.ids)
        if self.path is None:
            return
        saved = save_recent_open_sessions(
            self.path, self.ids if self.write_failed else newly_open, remembered=self.ids
        )
        self.write_failed = saved is None
        if saved is not None:
            self.ids = saved
