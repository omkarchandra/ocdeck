"""Desktop notifications for new sentinel alarms (C106 semantics).

Bodies carry only severity, rule, an opaque session prefix and a safe
resource/destination class — never URLs with queries, commands, paths, or
summaries. Each record is notified at most once (bounded seen-id store), so
re-scans and persistent unresolved records do not re-notify.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path

GDBUS = "/usr/bin/gdbus"
DESTINATION = "org.gtk.Notifications"
OBJECT_PATH = "/org/gtk/Notifications"
APP_ID = "org.local.OCDeckSwitch"
MAX_SEEN = 500
NOTIFY_SEVERITIES = ("CRITICAL", "HIGH")


def _gvariant_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def notified_path(state_dir: Path) -> Path:
    return state_dir / "notified.json"


def load_notified(state_dir: Path) -> set[str]:
    try:
        descriptor = os.open(notified_path(state_dir), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as handle:
            if os.fstat(handle.fileno()).st_size > 64 * 1024:
                return set()
            payload = json.loads(handle.read(64 * 1024 + 1))
    except (OSError, ValueError):
        return set()
    ids = payload.get("notified") if isinstance(payload, dict) else None
    if not isinstance(ids, list):
        return set()
    return {item for item in ids if isinstance(item, str)}


def save_notified(state_dir: Path, ids: set[str]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = json.dumps(
        {"version": 1, "notified": sorted(ids)[-MAX_SEEN:]}, separators=(",", ":")
    ) + "\n"
    temporary = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=state_dir, prefix=".notified.", delete=False
        ) as handle:
            temporary = handle.name
            os.chmod(temporary, 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, notified_path(state_dir))
        temporary = ""
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _safe_body(record: dict) -> str:
    severity = str(record.get("severity") or "")
    rule = str(record.get("rule") or "")
    session = str(record.get("sessionId") or "")[:12]
    criteria = record.get("criteria") if isinstance(record.get("criteria"), dict) else {}
    detail = ""
    for key in ("class", "surface"):
        if isinstance(criteria.get(key), str):
            detail = f" {criteria[key]}"
            break
    return f"{severity} {rule}{detail} · session {session}"


def send_notification(notification_id: str, title: str, body: str) -> bool:
    payload = f"{{'title': <{_gvariant_string(title)}>, 'body': <{_gvariant_string(body)}>}}"
    try:
        result = subprocess.run(
            [
                GDBUS, "call", "--session", "--dest", DESTINATION,
                "--object-path", OBJECT_PATH, "--method",
                "org.gtk.Notifications.AddNotification",
                APP_ID, notification_id, payload,
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def notify_new_records(state_dir: Path, records: list[dict], *, sender=send_notification) -> int:
    """Notify CRITICAL/HIGH records not yet announced; returns count sent."""
    seen = load_notified(state_dir)
    sent = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        if str(record.get("severity")) not in NOTIFY_SEVERITIES:
            continue
        record_id = str(record.get("id") or "")
        if not record_id or record_id in seen:
            continue
        rule = str(record.get("rule") or "sentinel")
        if sender(f"ocdeck-sentinel-{record_id}", f"OC Deck sentinel: {rule}", _safe_body(record)):
            sent += 1
            seen.add(record_id)
    if sent:
        save_notified(state_dir, seen)
    return sent
