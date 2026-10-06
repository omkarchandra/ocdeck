"""Hostile artifacts and independent freshness checks; no live services."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

from ocdeck.sentinel.alarms import MAX_ARTIFACT_BYTES, build_records, load_alarms, write_alarms
from ocdeck.sentinel.health import read_health
from ocdeck.sentinel.rules import RuleFinding

NOW = datetime(2026, 9, 26, 23, 0, tzinfo=timezone.utc)


def finding(number=0, *, severity="HIGH", criteria=None):
    return RuleFinding(rule="S1", severity=severity, harness="claude", session_id=f"s{number}",
                       cwd="/project", summary=f"finding {number}", criteria=criteria or {})


def artifact(path):
    payload = write_alarms(path, build_records([finding()]), {
        "overflow": 0, "rulesAvailable": True, "priorChainOk": True,
        "adapters": {"claude": "OBSERVED", "codex": "INACTIVE", "opencode": "OBSERVED"},
        "coverageNotes": [], "unsupportedSurfaces": [],
    })
    payload["generatedAt"] = NOW.isoformat()
    path.write_text(json.dumps(payload))
    return payload


@pytest.mark.parametrize("field,value", [
    ("schema", True), ("records", {}), ("records", "bad"), ("records", None),
    ("meta", []), ("generatedAt", "tomorrow"), ("generatedAt", "2026-09-26"),
])
def test_bad_envelopes_fail_closed(tmp_path, field, value):
    path = tmp_path / "alarms.json"
    payload = artifact(path)
    payload[field] = value
    path.write_text(json.dumps(payload))
    assert load_alarms(path) == (None, False)
    assert read_health(path, now=NOW).status == "UNAVAILABLE"


@pytest.mark.parametrize("key,value", [
    ("overflow", "3"), ("overflow", -1), ("overflow", True),
    ("adapters", []), ("adapters", {"claude": []}), ("coverageNotes", "gap"),
    ("rulesAvailable", "false"), ("priorChainOk", 1), ("unsupportedSurfaces", [42]),
])
def test_bad_metadata_fails_closed(tmp_path, key, value):
    path = tmp_path / "alarms.json"
    payload = artifact(path)
    payload["meta"][key] = value
    path.write_text(json.dumps(payload))
    assert load_alarms(path) == (None, False)


def test_record_shape_and_duplicate_ids_checked_even_with_valid_hash(tmp_path):
    path = tmp_path / "alarms.json"
    payload = artifact(path)
    body = dict(payload["records"][0], severity=[])
    payload["records"] = build_records([body])
    path.write_text(json.dumps(payload))
    assert load_alarms(path) == (None, False)
    payload = artifact(path)
    payload["records"] *= 2
    path.write_text(json.dumps(payload))
    assert load_alarms(path) == (None, False)


@pytest.mark.parametrize("raw", [
    b"{" * 2000, b"[" * 2000 + b"]" * 2000, b"\xff",
    b'{"schema":1,"schema":1}',
])
def test_invalid_json_is_unavailable(tmp_path, raw):
    path = tmp_path / "alarms.json"
    path.write_bytes(raw)
    assert load_alarms(path) == (None, False)


def test_missing_unreadable_symlink_and_directory_are_distinct(tmp_path):
    path = tmp_path / "alarms.json"
    assert load_alarms(path) == (None, True)
    assert read_health(path, now=NOW).label == "SENTINEL-OFFLINE"
    with mock.patch("ocdeck.sentinel.alarms.os.open", side_effect=PermissionError):
        assert load_alarms(path) == (None, False)
    path.symlink_to(tmp_path / "missing")
    assert load_alarms(path) == (None, False)
    assert load_alarms(tmp_path) == (None, False)


def test_fifo_does_not_block_and_descriptor_closes(tmp_path):
    path = tmp_path / "fifo"
    os.mkfifo(path)
    # Isolate the probe: a regression must time out, never hang the suite.
    result = subprocess.run([sys.executable, "-B", "-c",
        "import os,sys; from pathlib import Path; from ocdeck.sentinel.alarms import load_alarms; "
        "before=len(os.listdir('/proc/self/fd')); "
        "assert load_alarms(Path(sys.argv[1])) == (None,False); "
        "assert len(os.listdir('/proc/self/fd')) == before", str(path)],
        timeout=5, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_valid_json_over_size_limit_rejected(tmp_path):
    path = tmp_path / "alarms.json"
    artifact(path)
    path.write_bytes(path.read_bytes() + b" " * MAX_ARTIFACT_BYTES)
    assert load_alarms(path) == (None, False)


def test_byte_trimming_preserves_critical_chain_and_input(tmp_path):
    path = tmp_path / "alarms.json"
    records = build_records([finding(i, criteria={"padding": "a" * 2500}) for i in range(40)]
                            + [finding(99, severity="CRITICAL")])
    original = list(records)
    meta = {"overflow": 3}
    payload = write_alarms(path, records, meta)
    assert path.stat().st_size <= MAX_ARTIFACT_BYTES
    assert load_alarms(path) == (payload, True)
    assert payload["records"][0]["severity"] == "CRITICAL"
    assert 0 < len(payload["records"]) < len(records)
    assert payload["meta"]["overflow"] == 3 + len(records) - len(payload["records"])
    assert meta == {"overflow": 3} and records == original


def test_metadata_overflow_keeps_previous_artifact(tmp_path):
    path = tmp_path / "alarms.json"
    artifact(path)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="metadata"):
        write_alarms(path, [], {"coverageNotes": ["x" * MAX_ARTIFACT_BYTES]})
    assert path.read_bytes() == before


@pytest.mark.parametrize("age,status", [(0, "OBSERVED"), (59.9, "OBSERVED"),
    (60, "OFFLINE"), (600, "OFFLINE"), (-4, "OBSERVED"), (-6, "UNAVAILABLE")])
def test_health_uses_scan_timestamp_and_retains_alarm_counts(tmp_path, age, status):
    path = tmp_path / "alarms.json"
    artifact(path)
    health = read_health(path, now=NOW + timedelta(seconds=age))
    assert health.status == status
    assert health.age_seconds == age
    assert health.alarm_count == 1 and health.high_count == 1


@pytest.mark.parametrize("patch", [
    {"rulesAvailable": False}, {"priorChainOk": False},
    {"adapters": {"claude": "OBSERVED"}}, {"coverageNotes": ["private payload"]},
    {"unsupportedSurfaces": ["S10"]}, {"overflow": 12},
])
def test_coverage_gaps_are_degraded_without_disclosing_details(tmp_path, patch):
    path = tmp_path / "alarms.json"
    payload = artifact(path)
    payload["meta"].update(patch)
    path.write_text(json.dumps(payload))
    health = read_health(path, now=NOW)
    assert health.status == "DEGRADED" and health.reasons
    assert "private payload" not in repr(health)


def test_tampered_artifact_never_reports_observed(tmp_path):
    path = tmp_path / "alarms.json"
    payload = artifact(path)
    payload["records"][0]["summary"] = "changed"
    path.write_text(json.dumps(payload))
    assert read_health(path, now=NOW).status == "UNAVAILABLE"


def test_broken_chain_evidence_survives_quiet_scans(tmp_path, monkeypatch):
    from ocdeck.sentinel import __main__ as scanner
    from ocdeck.sentinel.collect import ScanResult

    path = tmp_path / "ocdeck" / "sentinel-alarms.json"
    payload = artifact(path)
    payload["records"][0]["summary"] = "tampered"
    path.write_text(json.dumps(payload))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"version": 1, "allow_origins": []}))
    rules.chmod(0o600)
    with mock.patch.object(scanner, "scan_transcripts", return_value=ScanResult()), \
         mock.patch.object(scanner, "scan_codex", return_value=([], [], "INACTIVE")), \
         mock.patch.object(scanner, "scan_opencode", return_value=([], [], "INACTIVE")), \
         mock.patch.object(scanner, "notify_new_records", return_value=0):
        for _ in range(2):
            assert scanner.once(rules_path=rules, home=tmp_path) == 0
            persisted, valid = load_alarms(path)
            assert valid and persisted["meta"]["priorChainOk"] is False
            assert read_health(path).status == "DEGRADED"
