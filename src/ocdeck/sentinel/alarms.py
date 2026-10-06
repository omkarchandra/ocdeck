"""Bounded, redacted, hash-chained alarm artifact (C106/C108 semantics).

OC Deck never reads transcripts; it consumes only this artifact. Records are
plain sanitized facts: no file contents, no secrets, no URL query/fragment.
The chain is sha256(prev ‖ canonical(record)); tamper detection is only as
strong as where the head is anchored (C103 interim applies).
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import time
from datetime import datetime
from pathlib import Path

SCHEMA = 1
MAX_RECORDS = 50
MAX_ARTIFACT_BYTES = 64 * 1024
MAX_SUMMARY_CHARS = 200


def alarm_artifact_path(state_home: Path | None = None) -> Path:
    base = state_home or Path(
        os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")
    )
    return base / "ocdeck" / "sentinel-alarms.json"


def _canonical(record: dict) -> bytes:
    return json.dumps(record, sort_keys=True, separators=(",", ":")).encode()


def redact(text: str) -> str:
    """Strip URL queries/fragments and collapse $HOME for display (C106)."""
    import re

    text = re.sub(r"([?#])[^ ]*", r"\1…", text)
    home = str(Path.home())
    if home != "/" and home in text:
        text = text.replace(home, "~")
    return text


def finding_body(finding) -> dict:
    """Normalize a RuleFinding (or an already-built record body) for chaining."""
    if isinstance(finding, dict):
        return {k: v for k, v in finding.items() if k != "hash"}
    return {
        "schema": SCHEMA,
        "id": hashlib.sha256(
            (finding.rule + finding.session_id + finding.summary).encode()
        ).hexdigest()[:16],
        "firedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "harness": finding.harness,
        "sessionId": finding.session_id,
        "projectPath": finding.cwd,
        "origin": "transcript-intent",
        "rule": finding.rule,
        "severity": finding.severity,
        "outcome": finding.outcome,
        "summary": redact(finding.summary)[:MAX_SUMMARY_CHARS],
        "criteria": finding.criteria,
    }


def build_records(findings, *, overflow: int = 0) -> list[dict]:
    ordered = sorted(
        (finding_body(f) for f in findings),
        key=lambda b: (b.get("severity") != "CRITICAL", b.get("firedAt") or ""),
    )
    seen: set[str] = set()
    unique = [b for b in ordered if not (b["id"] in seen or seen.add(b["id"]))]
    kept, dropped = unique[:MAX_RECORDS], unique[MAX_RECORDS:]
    records: list[dict] = []
    previous = ""
    for base in kept:
        digest = hashlib.sha256(previous.encode() + _canonical(base)).hexdigest()
        records.append({**base, "hash": digest})
        previous = digest
    if dropped:
        overflow += len(dropped)
    return records


def write_alarms(path: Path, records: list[dict], meta: dict) -> dict:
    payload = {
        "schema": SCHEMA,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "meta": dict(meta),
        "records": list(records),
    }
    if not _valid_payload(payload):
        raise ValueError("invalid alarm artifact")
    encoded = _encode_payload(payload)
    while len(encoded) > MAX_ARTIFACT_BYTES and payload["records"]:
        payload["meta"]["overflow"] = payload["meta"].get("overflow", 0) + 1
        # Keep the highest-priority prefix. Dropping the head invalidates
        # every subsequent hash and discards CRITICAL findings first.
        payload["records"].pop()
        encoded = _encode_payload(payload)
    if len(encoded) > MAX_ARTIFACT_BYTES:
        raise ValueError("alarm metadata exceeds artifact byte limit")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent,
            prefix=f".{path.name}.", delete=False,
        ) as handle:
            temporary = handle.name
            os.chmod(temporary, 0o600)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = ""
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    return payload


def _encode_payload(payload: dict) -> bytes:
    return (json.dumps(payload, separators=(",", ":"), allow_nan=False) + "\n").encode()


def _valid_payload(payload: object) -> bool:
    """Check the consumer contract before callers use counters or records."""
    if not isinstance(payload, dict) or type(payload.get("schema")) is not int or payload["schema"] != SCHEMA:
        return False
    generated = payload.get("generatedAt")
    if not isinstance(generated, str):
        return False
    try:
        timestamp = datetime.fromisoformat(generated.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            return False
    except ValueError:
        return False
    meta, records = payload.get("meta"), payload.get("records")
    if not isinstance(meta, dict) or not isinstance(records, list) or len(records) > MAX_RECORDS:
        return False
    for key in ("overflow", "filesScanned", "events"):
        if key in meta and (type(meta[key]) is not int or meta[key] < 0):
            return False
    for key in ("rulesAvailable", "priorChainOk"):
        if key in meta and type(meta[key]) is not bool:
            return False
    for key in ("coverageNotes", "unsupportedSurfaces"):
        if key in meta and (not isinstance(meta[key], list) or any(not isinstance(v, str) for v in meta[key])):
            return False
    if "adapters" in meta and (
        not isinstance(meta["adapters"], dict)
        or any(not isinstance(v, str) for v in meta["adapters"].values())
    ):
        return False
    seen = set()
    for record in records:
        if not isinstance(record, dict) or type(record.get("schema")) is not int or record["schema"] != SCHEMA:
            return False
        for key in ("id", "firedAt", "harness", "sessionId", "projectPath", "origin", "rule", "severity", "outcome", "summary", "hash"):
            if not isinstance(record.get(key), str):
                return False
        if not record["id"] or record["id"] in seen or not isinstance(record.get("criteria"), dict):
            return False
        seen.add(record["id"])
    return True


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def load_alarms(path: Path) -> tuple[dict | None, bool]:
    """Bounded, nonblocking read; only a missing file is (None, True).

    A valid chain is self-consistency, not authenticity: its head is not
    yet anchored outside the writable artifact.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None, True
    except OSError:
        return None, False
    try:
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ARTIFACT_BYTES:
                return None, False
            raw = handle.read(MAX_ARTIFACT_BYTES + 1)
            if len(raw) > MAX_ARTIFACT_BYTES:
                return None, False
            payload = json.loads(raw, object_pairs_hook=_unique_object)
        if not _valid_payload(payload):
            return None, False
        # Reject nonfinite numbers in otherwise valid JSON too.
        _encode_payload(payload)
    except (OSError, ValueError, RecursionError):
        return None, False
    previous = ""
    for record in payload.get("records") or []:
        if not isinstance(record, dict):
            return payload, False
        expected = record.get("hash")
        body = {k: v for k, v in record.items() if k != "hash"}
        digest = hashlib.sha256(previous.encode() + _canonical(body)).hexdigest()
        if expected != digest:
            return payload, False
        previous = digest
    return payload, True
