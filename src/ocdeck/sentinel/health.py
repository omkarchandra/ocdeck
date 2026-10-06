"""Read-only Sentinel freshness and coverage status for the dashboard.

No process launches, transcript reads, notifications, or enforcement. A
recent artifact proves only that a scan reported; it cannot authenticate
the scanner or replace protected audit checkpoints. Operator dismissals
(acks) are receipt-only (C106/C108): they never change the artifact, they
only exclude a record from the live counts while it stays listed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .acks import ack_path, acks_sibling, dismissed_pairs, load_ack_times
from .alarms import alarm_artifact_path, load_alarms

STALE_AFTER_SECONDS = 60
FUTURE_TOLERANCE_SECONDS = 5


@dataclass(frozen=True)
class SentinelHealth:
    status: str  # OFFLINE, UNAVAILABLE, DEGRADED, OBSERVED
    reasons: tuple[str, ...]
    age_seconds: float | None = None
    alarm_count: int = 0
    critical_count: int = 0
    high_count: int = 0
    overflow: int = 0
    acked_count: int = 0

    @property
    def label(self) -> str:
        return f"SENTINEL-{self.status}"


def _acks_file(path: Path | None, acks_path: Path | None) -> Path:
    """Dismissal log beside the alarm artifact (or an explicit override)."""
    if acks_path is not None:
        return acks_path
    if path is not None:
        return acks_sibling(path)
    return ack_path()


def read_health(path: Path | None = None, *, now: datetime | None = None,
                acks_path: Path | None = None) -> SentinelHealth:
    """Fail visibly on missing, stale, malformed or incomplete scan evidence.

    Reasons are fixed text, safe to show without exposing alarm contents.
    Call on each dashboard refresh so monitor failure remains observable
    when the scanner itself has stopped. Existing alarms and overflow counts
    remain visible when the artifact is stale. Acked (dismissed) records are
    excluded from the live counts but stay listed; an ack log that fails
    validation dismisses nothing and says so.
    """
    payload, chain_ok, acked, acks_ok = _load(path, acks_path, now)
    return _health(payload, chain_ok, acked=acked, acks_ok=acks_ok, now=now)


def _load(path: Path | None, acks_path: Path | None, now: datetime | None):
    """One artifact read plus the dismissals that apply to its records."""
    payload, chain_ok = load_alarms(path if path is not None else alarm_artifact_path())
    times, acks_ok = load_ack_times(_acks_file(path, acks_path))
    records = payload["records"] if payload and chain_ok else []
    acked = dismissed_pairs(records, times, now or datetime.now(timezone.utc)) if acks_ok else frozenset()
    return payload, chain_ok, acked, acks_ok


@dataclass(frozen=True)
class SentinelReport:
    health: SentinelHealth
    records: tuple[dict, ...] = ()
    acked: frozenset[tuple[str, str]] = frozenset()


def read_report(path: Path | None = None, *, now: datetime | None = None,
                acks_path: Path | None = None) -> SentinelReport:
    """Health and display records from the same bounded artifact read."""
    payload, chain_ok, acked, acks_ok = _load(path, acks_path, now)
    health = _health(payload, chain_ok, acked=acked, acks_ok=acks_ok, now=now)
    records = tuple(payload["records"]) if payload and chain_ok and health.status != "UNAVAILABLE" else ()
    return SentinelReport(health, records, acked if acks_ok else frozenset())


def _health(payload: dict | None, chain_ok: bool, *, acked: frozenset = frozenset(),
            acks_ok: bool = True, now: datetime | None) -> SentinelHealth:
    ack_reason = () if acks_ok else ("Dismissal log failed validation.",)
    if not chain_ok:
        return SentinelHealth("UNAVAILABLE", ("Alarm artifact failed validation.",) + ack_reason)
    if payload is None:
        return SentinelHealth("OFFLINE", ("No completed scan artifact.",) + ack_reason)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    generated = datetime.fromisoformat(payload["generatedAt"].replace("Z", "+00:00"))
    age = (current - generated).total_seconds()
    records, meta = payload["records"], payload["meta"]
    if not acks_ok:
        # A broken dismissal log must fail toward MORE visibility: every
        # alarm is counted as live again (C106/C108).
        acked = frozenset()
    visible = [record for record in records if (record["id"], record["hash"]) not in acked]
    counts = {
        "age_seconds": age,
        "alarm_count": len(visible),
        "critical_count": sum(r["severity"] == "CRITICAL" for r in visible),
        "high_count": sum(r["severity"] == "HIGH" for r in visible),
        "overflow": meta.get("overflow", 0),
        "acked_count": len(records) - len(visible),
    }
    if age < -FUTURE_TOLERANCE_SECONDS:
        return SentinelHealth("UNAVAILABLE", ("Scan timestamp is in the future.",) + ack_reason, **counts)
    if age >= STALE_AFTER_SECONDS:
        return SentinelHealth("OFFLINE", ("No completed scan within 60 seconds.",) + ack_reason, **counts)
    reasons = list(ack_reason)
    if meta.get("rulesAvailable") is not True:
        reasons.append("Rules are unavailable or unreported.")
    if meta.get("priorChainOk") is not True:
        reasons.append("Prior artifact integrity is broken or unreported.")
    adapters = meta.get("adapters", {})
    if any(adapters.get(harness) not in {"OBSERVED", "INACTIVE"} for harness in ("claude", "codex", "opencode")):
        reasons.append("Harness coverage is incomplete or unreported.")
    if meta.get("coverageNotes"):
        reasons.append("The scan reported coverage gaps.")
    if meta.get("unsupportedSurfaces") or "unsupportedSurfaces" not in meta:
        reasons.append("Some monitoring surfaces are unsupported or unreported.")
    if counts["overflow"]:
        reasons.append("Some alarms are omitted from the bounded artifact.")
    return SentinelHealth("DEGRADED" if reasons else "OBSERVED", tuple(reasons), **counts)
