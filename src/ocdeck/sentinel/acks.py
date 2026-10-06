"""Append-only dismissal (acknowledgement) log beside the alarm artifact.

A dismissal records receipt only (C106/C108): it never edits, deletes, or
rewrites the alarm artifact and it grants no authority. Dismissed alarms
stay listed, dimmed, until the bounded artifact itself rotates them out.
The log is a separate hash-chained JSONL file using the same safe-file
conventions as the alarm artifact (O_NOFOLLOW, regular file only,
owner-only 0600, size caps, canonical JSON, sha256 chain). A log that is
missing means "nothing dismissed yet"; a log that fails validation means
"nothing is dismissed" (fail toward MORE visibility, never less).
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = 1
ACK_LOG_NAME = "sentinel-acks.jsonl"
MAX_ACKS_BYTES = 1024 * 1024  # 1 MiB append-only log cap
FUTURE_TOLERANCE_SECONDS = 5


class AckLogUnavailable(ValueError):
    """The dismissal log cannot be trusted; nothing may be appended."""


def ack_path(state_home: Path | None = None) -> Path:
    base = state_home or Path(
        os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")
    )
    return base / "ocdeck" / ACK_LOG_NAME


def acks_sibling(artifact: Path) -> Path:
    """The dismissal log that belongs to a given alarm artifact."""
    return artifact.parent / ACK_LOG_NAME


def _canonical(entry: dict) -> bytes:
    return json.dumps(entry, sort_keys=True, separators=(",", ":")).encode()


def _timestamp(now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return current.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _valid_entry(entry: object) -> bool:
    if not isinstance(entry, dict) or type(entry.get("schema")) is not int or entry["schema"] != SCHEMA:
        return False
    for key in ("alarmId", "alarmHash", "disposition", "ackedAt", "via", "prev", "hash"):
        if not isinstance(entry.get(key), str):
            return False
    if not entry["alarmId"] or not entry["alarmHash"] or not entry["hash"]:
        return False
    if entry["disposition"] != "dismissed" or entry["via"] != "deck-ui":
        return False
    try:
        stamp = datetime.fromisoformat(entry["ackedAt"].replace("Z", "+00:00"))
    except ValueError:
        return False
    return stamp.tzinfo is not None


def _read_log(path: Path) -> tuple[frozenset[tuple[str, str]], str, bool, dict[str, tuple[str, ...]]]:
    """acked pairs, the head hash ("" when empty), whether to trust them, and
    every dismissal time per alarm id."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return frozenset(), "", True, {}
    except OSError:
        return frozenset(), "", False, {}
    try:
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                return frozenset(), "", False, {}
            if stat.S_IMODE(info.st_mode) != 0o600:
                return frozenset(), "", False, {}
            if info.st_size > MAX_ACKS_BYTES:
                return frozenset(), "", False, {}
            raw = handle.read(MAX_ACKS_BYTES + 1)
            if len(raw) > MAX_ACKS_BYTES or not raw.strip():
                return frozenset(), "", False, {}
            try:
                lines = [json.loads(line, object_pairs_hook=_unique_object)
                         for line in raw.splitlines() if line.strip()]
            except ValueError:
                return frozenset(), "", False, {}
    except (OSError, ValueError, RecursionError):
        return frozenset(), "", False, {}
    acked: set[tuple[str, str]] = set()
    times: dict[str, tuple[str, ...]] = {}
    previous = ""
    for entry in lines:
        if not _valid_entry(entry) or entry["prev"] != previous:
            return frozenset(), "", False, {}
        body = {k: v for k, v in entry.items() if k != "hash"}
        if entry["hash"] != hashlib.sha256(_canonical(body)).hexdigest():
            return frozenset(), "", False, {}
        acked.add((entry["alarmId"], entry["alarmHash"]))
        times[entry["alarmId"]] = times.get(entry["alarmId"], ()) + (entry["ackedAt"],)
        previous = entry["hash"]
    return frozenset(acked), previous, True, times


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def load_acks(path: Path) -> tuple[frozenset[tuple[str, str]], bool]:
    """Bounded, nonblocking read; only a missing file is (empty, True).

    A broken or tampered log fails toward MORE visibility: every alarm is
    treated as not dismissed. An empty-but-present file is never produced
    by append_ack, so it is treated as a truncation and rejected.
    """
    acked, _head, ok, _times = _read_log(path)
    return acked, ok


def load_ack_times(path: Path) -> tuple[dict[str, tuple[str, ...]], bool]:
    """Every dismissal time per alarm id, with the same trust rules as load_acks."""
    _acked, _head, ok, times = _read_log(path)
    return times, ok


def _utc(stamp: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def dismissed_pairs(records, times: dict[str, tuple[str, ...]], now: datetime) -> frozenset[tuple[str, str]]:
    """(id, hash) of the current records that a dismissal applies to.

    A dismissal applies to the record with the same alarm id when it was made
    at or after that record fired and is not dated in the future. Binding by
    id and time, rather than by the chained record hash, survives the scanner
    re-chaining the artifact (a new CRITICAL sorts ahead of every HIGH and
    changes their hashes). A dismissal still can never precede the alarm it
    dismisses, and an alarm that fires again later is live again.
    """
    limit = now + timedelta(seconds=FUTURE_TOLERANCE_SECONDS)
    pairs: set[tuple[str, str]] = set()
    for record in records:
        fired = _utc(record.get("firedAt"))
        if fired is None:
            continue
        for stamp in times.get(record.get("id"), ()):
            acked_at = _utc(stamp)
            if acked_at is not None and fired <= acked_at <= limit:
                pairs.add((record["id"], record["hash"]))
                break
    return frozenset(pairs)


def append_ack(path: Path, alarm_id: str, alarm_hash: str, *, now: datetime | None = None) -> dict | None:
    """Append one dismissal receipt; idempotent per (alarmId, alarmHash).

    Returns the written entry, or None when the pair was already
    acknowledged. Refuses (raises AckLogUnavailable) to extend a
    log that fails validation. Never touches the alarm artifact.
    """
    if not isinstance(alarm_id, str) or not alarm_id or not isinstance(alarm_hash, str) or not alarm_hash:
        raise ValueError("alarm_id and alarm_hash must be non-empty strings")
    acked, previous, ok, _times = _read_log(path)
    if not ok:
        raise AckLogUnavailable("dismissal log failed validation")
    if (alarm_id, alarm_hash) in acked:
        return None
    body = {
        "schema": SCHEMA,
        "alarmId": alarm_id,
        "alarmHash": alarm_hash,
        "disposition": "dismissed",
        "ackedAt": _timestamp(now),
        "via": "deck-ui",
        "prev": previous,
    }
    entry = {**body, "hash": hashlib.sha256(_canonical(body)).hexdigest()}
    line = _canonical(entry) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            os.fchmod(descriptor, 0o600)  # exact owner-only perms, umask-proof
    except OSError as error:
        raise AckLogUnavailable(f"dismissal log unwritable: {error}") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise AckLogUnavailable("dismissal log must be an owner-only 0600 regular file")
        if info.st_size + len(line) > MAX_ACKS_BYTES:
            raise AckLogUnavailable("dismissal log exceeds its byte limit")
        view = memoryview(line)
        while view:
            view = view[os.write(descriptor, view):]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return entry
