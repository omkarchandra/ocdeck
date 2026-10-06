"""Dismissal (ack) log, origin display, and the Shift+D dismiss workflow.

Council constraints (C106/C108): a dismissal records receipt only and grants
no authority; it never edits the alarm artifact; dismissed rows stay listed;
a broken ack log fails toward MORE visibility (nothing treated as dismissed).
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest
from textual.widgets import DataTable, Static

from ocdeck.alarm_view import alarm_cells, alarm_detail, health_text
from ocdeck.app import OCDeckApp
from ocdeck.dismiss_all import DismissAllScreen
from ocdeck.sentinel.acks import (
    MAX_ACKS_BYTES,
    ack_path,
    acks_sibling,
    append_ack,
    load_acks,
)
from ocdeck.sentinel.alarms import alarm_artifact_path, build_records, finding_body, load_alarms, write_alarms
from ocdeck.sentinel.health import read_health, read_report
from ocdeck.sentinel.rules import RuleFinding
from tests.test_harness_app import FakeHarnessSource, make_snapshot

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
META = {
    "rulesAvailable": True, "priorChainOk": True, "overflow": 0,
    "adapters": {"claude": "OBSERVED", "codex": "INACTIVE", "opencode": "OBSERVED"},
    "coverageNotes": [], "unsupportedSurfaces": [],
}


def findings() -> list[RuleFinding]:
    return [
        RuleFinding(rule="S3", severity="HIGH", harness="claude", session_id="ses_a",
                    cwd="/project", summary="webfetch attempted egress to non-allowlisted origin",
                    criteria={"origin": "https://opencode.ai", "tool": "webfetch"}),
        RuleFinding(rule="S7", severity="CRITICAL", harness="claude", session_id="ses_b",
                    cwd="/other", summary="tmux kill-session attempted",
                    criteria={"class": "tmux", "tool": "Bash", "outcome": "attempted"}),
        RuleFinding(rule="S1", severity="HIGH", harness="claude", session_id="ses_c",
                    cwd="/project", summary="Read touched env-secrets", criteria={}),
    ]


def write_artifact(path: Path, records=None, *, meta: dict | None = None) -> Path:
    """Fresh OBSERVED artifact; the envelope stamp is not part of the chain."""
    payload = write_alarms(path, records if records is not None else build_records(findings()),
                           dict(META, **(meta or {})))
    payload["generatedAt"] = NOW.isoformat()
    path.write_text(json.dumps(payload))
    return path


def display_record(**overrides) -> dict:
    base = {
        "schema": 1, "id": "id0", "firedAt": "2026-09-27T00:00:00Z",
        "harness": "claude", "sessionId": "ses_a", "projectPath": "/project",
        "origin": "transcript-intent", "rule": "S3", "severity": "HIGH",
        "outcome": "attempted",
        "summary": "webfetch attempted egress to non-allowlisted origin",
        "criteria": {"origin": "https://opencode.ai", "tool": "webfetch"},
        "hash": "hash0",
    }
    base.update(overrides)
    return base


def rewrite_log(path: Path, entries: list[dict]) -> None:
    path.write_text("".join(
        json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n"
        for entry in entries
    ))
    path.chmod(0o600)


# --- ack log: append/load --------------------------------------------------

def test_append_load_round_trip_and_chain(tmp_path):
    path = tmp_path / "sentinel-acks.jsonl"
    assert load_acks(path) == (frozenset(), True)  # missing file is not an error
    line = append_ack(path, "alarm1", "hash1", now=NOW)
    assert line is not None
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    entry = json.loads(path.read_text().splitlines()[0])
    assert entry == line
    assert entry["schema"] == 1 and entry["disposition"] == "dismissed"
    assert entry["via"] == "deck-ui" and entry["ackedAt"] == "2026-09-27T12:00:00Z"
    assert entry["prev"] == ""
    body = {key: value for key, value in entry.items() if key != "hash"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    assert entry["hash"] == hashlib.sha256(canonical).hexdigest()
    second = append_ack(path, "alarm2", "hash2", now=NOW)
    assert second["prev"] == line["hash"]
    assert acks_sibling(alarm_artifact_path()) == ack_path().parent / "sentinel-acks.jsonl"
    pairs, ok = load_acks(path)
    assert ok
    assert pairs == frozenset({("alarm1", "hash1"), ("alarm2", "hash2")})


def test_re_ack_of_the_same_pair_is_idempotent(tmp_path):
    path = tmp_path / "sentinel-acks.jsonl"
    append_ack(path, "alarm1", "hash1", now=NOW)
    before = path.read_bytes()
    assert append_ack(path, "alarm1", "hash1", now=NOW) is None
    assert path.read_bytes() == before
    assert load_acks(path)[0] == frozenset({("alarm1", "hash1")})


def test_same_id_with_a_different_hash_is_not_acked(tmp_path):
    path = tmp_path / "sentinel-acks.jsonl"
    append_ack(path, "alarm1", "hash1", now=NOW)
    pairs, ok = load_acks(path)
    assert ok and ("alarm1", "hash2") not in pairs
    # A re-fired alarm (same id, new record hash) is a new dismissal.
    assert append_ack(path, "alarm1", "hash2", now=NOW) is not None
    pairs, _ = load_acks(path)
    assert ("alarm1", "hash1") in pairs and ("alarm1", "hash2") in pairs


def test_append_refuses_to_extend_an_invalid_log(tmp_path):
    path = tmp_path / "sentinel-acks.jsonl"
    rewrite_log(path, [{"schema": 1}])
    before = path.read_bytes()
    with pytest.raises(ValueError):
        append_ack(path, "alarm1", "hash1", now=NOW)
    assert path.read_bytes() == before


@pytest.mark.parametrize("corrupt", [
    "tamper", "reorder", "symlink", "oversize", "bad-json", "duplicate-key",
    "broken-prev", "group-readable", "missing-disposition",
])
def test_invalid_logs_arent_trusted_and_ack_nothing(tmp_path, corrupt):
    path = tmp_path / "sentinel-acks.jsonl"
    first = append_ack(path, "alarm1", "hash1", now=NOW)
    second = append_ack(path, "alarm2", "hash2", now=NOW)
    entries = [first, second]
    if corrupt == "tamper":
        entries[1] = {**second, "alarmId": "alarm9"}
    elif corrupt == "reorder":
        entries = [second, first]
    elif corrupt == "bad-json":
        path.write_text("{not json\n")
        path.chmod(0o600)
    elif corrupt == "duplicate-key":
        raw = json.dumps(first, sort_keys=True, separators=(",", ":"))
        path.write_text(raw[:-1] + ',"alarmId":"alarm1"}\n')
        path.chmod(0o600)
    elif corrupt == "broken-prev":
        entries = [{**first, "prev": "deadbeef"}]
    elif corrupt == "group-readable":
        path.chmod(0o644)
    elif corrupt == "missing-disposition":
        entries = [{key: value for key, value in first.items() if key != "disposition"}]
    if corrupt not in {"symlink", "oversize", "bad-json", "duplicate-key", "group-readable"}:
        rewrite_log(path, entries)
    if corrupt == "symlink":
        real = tmp_path / "real.jsonl"
        real.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(real)
    elif corrupt == "oversize":
        path.write_bytes(b"x" * (MAX_ACKS_BYTES + 1))
        path.chmod(0o600)
    assert load_acks(path) == (frozenset(), False)


# --- health: counts, acked exposure, fail-visible breakage -----------------

def test_health_counts_exclude_acked_and_expose_acked_count(tmp_path):
    path = tmp_path / "alarms.json"
    acks = tmp_path / "sentinel-acks.jsonl"
    records = build_records(pinned(findings()))  # CRITICAL S7 first after the sort
    write_artifact(path, records)
    critical = next(r for r in records if r["severity"] == "CRITICAL")
    append_ack(acks, critical["id"], critical["hash"], now=NOW)
    health = read_health(path, now=NOW, acks_path=acks)
    assert health.status == "OBSERVED"
    assert health.alarm_count == 2
    assert health.critical_count == 0
    assert health.high_count == 2
    assert health.acked_count == 1
    report = read_report(path, now=NOW, acks_path=acks)
    # Dismissed rows stay listed; the pair is exposed for row dimming.
    assert len(report.records) == 3
    assert report.acked == {(critical["id"], critical["hash"])}
    badge = health_text(report, details=False).plain
    assert "ALARMS(2)" in badge and "1 dismissed" in badge
    undismissed = read_report(path, now=NOW, acks_path=tmp_path / "absent.jsonl")
    plain = health_text(undismissed, details=False).plain
    assert "ALARMS(3)" in plain and "dismissed" not in plain


def test_report_reads_the_sibling_ack_log_by_default(tmp_path):
    path = tmp_path / "alarms.json"
    records = build_records(pinned(findings()))
    write_artifact(path, records)
    acks = acks_sibling(path)
    append_ack(acks, records[0]["id"], records[0]["hash"], now=NOW)
    report = read_report(path, now=NOW)
    assert report.health.acked_count == 1
    assert report.health.alarm_count == 2
    assert report.acked == {(records[0]["id"], records[0]["hash"])}


@pytest.mark.parametrize("meta_patch", [{}, {"priorChainOk": False}])
def test_broken_ack_log_degrades_and_counts_everything(tmp_path, meta_patch):
    path = tmp_path / "alarms.json"
    acks = tmp_path / "sentinel-acks.jsonl"
    records = build_records(findings())
    write_artifact(path, records, meta=meta_patch)
    append_ack(acks, records[0]["id"], records[0]["hash"], now=NOW)
    tampered = json.loads(acks.read_text())
    tampered["alarmId"] = "forged"
    acks.write_text(json.dumps(tampered, sort_keys=True, separators=(",", ":")) + "\n")
    acks.chmod(0o600)
    health = read_health(path, now=NOW, acks_path=acks)
    assert health.status == "DEGRADED"
    assert "Dismissal log failed validation." in health.reasons
    assert health.alarm_count == 3 and health.critical_count == 1
    assert health.acked_count == 0
    report = read_report(path, now=NOW, acks_path=acks)
    assert report.acked == frozenset()
    assert len(report.records) == 3
    # A stale artifact with a broken ack log keeps both reasons.
    stale = read_health(path, now=NOW + timedelta(seconds=120), acks_path=acks)
    assert stale.status == "OFFLINE"
    assert "Dismissal log failed validation." in stale.reasons


# --- binding: survives re-chaining, never precedes the alarm ---------------

FIRED = "2026-09-27T11:00:00Z"


def pinned(finding_list, fired=FIRED) -> list[dict]:
    """Record bodies with a fixed firedAt (the scanner stamps real time)."""
    return [{**finding_body(f), "firedAt": fired} for f in finding_list]


def bodies_of(records) -> list[dict]:
    """What the scanner carries forward: prior records without their hash."""
    return [{k: v for k, v in r.items() if k != "hash"} for r in records]


def test_dismissal_survives_a_new_critical_rechaining_the_artifact(tmp_path):
    # Every scan re-sorts (CRITICAL first) and re-chains record hashes. A new
    # CRITICAL used to change every HIGH record's hash, so dismissals bound to
    # (id, hash) silently came back on the next scan.
    path, acks = tmp_path / "alarms.json", tmp_path / "sentinel-acks.jsonl"
    first = build_records(pinned(findings()))
    high = next(r for r in first if r["rule"] == "S3")
    append_ack(acks, high["id"], high["hash"], now=NOW)
    new_critical = RuleFinding(rule="S7", severity="CRITICAL", harness="opencode", session_id="ses_new",
                               cwd="/p", summary="tmux kill-server attempted", criteria={})
    second = build_records(bodies_of(first) + pinned([new_critical], "2026-09-27T11:30:00Z"))
    rechained = next(r for r in second if r["id"] == high["id"])
    assert rechained["hash"] != high["hash"]  # the condition that used to lose the dismissal
    write_artifact(path, second)
    report = read_report(path, now=NOW, acks_path=acks)
    assert (rechained["id"], rechained["hash"]) in report.acked
    assert report.health.acked_count == 1
    assert report.health.critical_count == 2


@pytest.mark.parametrize("acked_at", [
    datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc),  # before the alarm fired
    NOW + timedelta(days=365),                           # dated in the future
], ids=["before-it-fired", "future-dated"])
def test_a_dismissal_cannot_precede_its_alarm_or_come_from_the_future(tmp_path, acked_at):
    path, acks = tmp_path / "alarms.json", tmp_path / "sentinel-acks.jsonl"
    records = build_records(pinned(findings()))
    append_ack(acks, records[0]["id"], records[0]["hash"], now=acked_at)
    write_artifact(path, records)
    report = read_report(path, now=NOW, acks_path=acks)
    assert report.acked == frozenset()
    assert report.health.acked_count == 0


def test_an_alarm_that_fires_again_after_its_dismissal_is_live_again(tmp_path):
    path, acks = tmp_path / "alarms.json", tmp_path / "sentinel-acks.jsonl"
    old = build_records(pinned(findings(), "2026-09-27T11:00:00Z"))
    append_ack(acks, old[0]["id"], old[0]["hash"], now=datetime(2026, 9, 27, 11, 10, tzinfo=timezone.utc))
    again = build_records(pinned(findings(), "2026-09-27T11:20:00Z"))  # same ids, later firing
    write_artifact(path, again)
    assert read_report(path, now=NOW, acks_path=acks).health.acked_count == 0


# --- origin display --------------------------------------------------------

def test_s3_summary_shows_the_destination_origin():
    cells = alarm_cells(display_record(), {}, {}, private=False)
    assert str(cells[5]) == "webfetch → https://opencode.ai"
    ported = display_record(criteria={"origin": "https://host.local:8080", "tool": "WebFetch"})
    assert str(alarm_cells(ported, {}, {}, private=False)[5]) == "WebFetch → https://host.local:8080"


@pytest.mark.parametrize("origin", [
    "https://opencode.ai/repo", "https://opencode.ai/?q=1", "javascript:alert(1)",
    "https://evil.example.net and http://second", "not-a-url", "",
])
def test_invalid_or_unsafe_origin_falls_back_to_the_plain_summary(origin):
    record = display_record(criteria={"origin": origin, "tool": "webfetch"})
    cells = alarm_cells(record, {}, {}, private=False)
    assert str(cells[5]) == "webfetch attempted egress to non-allowlisted origin"


def test_non_s3_rule_keeps_its_plain_summary():
    record = display_record(rule="S1", criteria={"origin": "https://opencode.ai", "tool": "Read"})
    assert str(alarm_cells(record, {}, {}, private=False)[5]).startswith("webfetch attempted")


def test_private_mode_hides_the_origin():
    record = display_record()
    assert str(alarm_cells(record, {}, {}, private=True)[5]) == "[hidden]"
    assert "opencode.ai" not in str(alarm_cells(record, {}, {}, private=True)[5])


def test_alarm_detail_shows_origin_and_dismissed_status():
    record = display_record()
    plain = alarm_detail(record, private=False).plain
    assert "Origin: https://opencode.ai" in plain
    assert "Status" not in plain
    dismissed = alarm_detail(record, private=False, acked=True).plain
    assert "Origin: https://opencode.ai" in dismissed
    assert "Status: dismissed" in dismissed
    invalid = alarm_detail(
        display_record(criteria={"origin": "javascript:alert(1)", "tool": "webfetch"}),
        private=False,
    ).plain
    assert "Origin:" not in invalid
    private = alarm_detail(record, private=True, acked=True).plain
    assert "Origin:" not in private and "dismissed" not in private


def test_dismissed_row_keeps_its_text_but_is_dimmed():
    record = display_record()
    plain = alarm_cells(record, {}, {}, private=False)
    dimmed = alarm_cells(record, {}, {}, private=False, dismissed=True)
    assert [cell.plain for cell in plain] == [cell.plain for cell in dimmed]
    assert all(any(span.style == "dim" for span in cell.spans) for cell in dimmed)
    assert all(not any(span.style == "dim" for span in cell.spans) for cell in plain)


# --- app pilot: the Shift+D dismiss workflow -------------------------------

class DismissWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # These tests write the default artifact and ack paths: never the real ones.
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        patcher = mock.patch.dict(os.environ, {"XDG_STATE_HOME": state.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        assert str(alarm_artifact_path()).startswith(state.name)

    def source(self):
        source = FakeHarnessSource(Path("/nonexistent-fixture"))
        source.snap = make_snapshot("/project")
        return source

    async def test_shift_d_confirms_then_records_receipt_only(self):
        records = build_records(findings())
        path = write_artifact(alarm_artifact_path(), records)
        acks = ack_path()
        original = path.read_bytes()
        app = OCDeckApp(self.source(), auto_refresh=False)
        async with app.run_test(size=(160, 42)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("6")
            await pilot.pause()
            table = app.query_one("#alarms-table", DataTable)
            self.assertEqual(app.query_one("#tabs").active, "alarms")
            self.assertEqual(table.row_count, 3)
            self.assertIn("ALARMS(3)", str(app.query_one("#sentinel-status", Static).visual))
            selected = app.alarm_by_id[app.selected_alarm_id]
            with mock.patch.object(app, "notify") as notify:
                await pilot.press("D")  # first press: confirmation only
                await pilot.pause()
                notify.assert_called_once()
                self.assertEqual(
                    notify.call_args.args[0],
                    f"Press Shift+D again to dismiss this {selected['rule']} alarm",
                )
                self.assertIn("timeout", notify.call_args.kwargs)
                self.assertFalse(acks.exists())
                await pilot.press("D")  # second press: record the dismissal
            await app.workers.wait_for_complete()
            await pilot.pause()
            pairs, ok = load_acks(acks)
            self.assertTrue(ok)
            self.assertEqual(pairs, {(selected["id"], selected["hash"])})
            # The badge drops, the dismissed row stays listed (dimmed).
            badge = str(app.query_one("#sentinel-status", Static).visual)
            self.assertIn("ALARMS(2)", badge)
            self.assertIn("1 dismissed", badge)
            self.assertEqual(table.row_count, 3)
            row = table.get_row(selected["id"])
            self.assertTrue(any(span.style == "dim" for span in row[0].spans))
            self.assertIn("Status: dismissed",
                          str(app.query_one("#alarm-detail", Static).visual))
            # A refresh keeps the dismissal; the artifact is never modified.
            app.action_refresh_data()
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertIn("ALARMS(2)", str(app.query_one("#sentinel-status", Static).visual))
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(load_alarms(path)[1], True)
            # Re-pressing both keys on the already-dismissed alarm is a no-op.
            with mock.patch.object(app, "notify") as notify:
                await pilot.press("D", "D")
                await pilot.pause()
                messages = [call.args[0] for call in notify.call_args_list]
                self.assertIn("Alarm already dismissed", messages)
            again, ok_again = load_acks(acks)
            self.assertTrue(ok_again)
            self.assertEqual(again, pairs)

    async def test_shift_a_dismisses_every_listed_alarm_only_after_the_typed_count(self):
        records = build_records(findings())
        path = write_artifact(alarm_artifact_path(), records)
        original = path.read_bytes()
        append_ack(ack_path(), records[0]["id"], records[0]["hash"])  # one already dismissed
        app = OCDeckApp(self.source(), auto_refresh=False)
        async with app.run_test(size=(160, 42)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("6")
            await pilot.pause()
            self.assertIn("ALARMS(2)", str(app.query_one("#sentinel-status", Static).visual))
            # A wrong count dismisses nothing.
            await pilot.press("A")
            await pilot.pause()
            self.assertIsInstance(app.screen, DismissAllScreen)
            self.assertEqual(app.screen.count, 2)
            await pilot.press("3", "enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertNotIsInstance(app.screen, DismissAllScreen)
            self.assertEqual(len(load_acks(ack_path())[0]), 1)
            # Esc dismisses nothing either.
            await pilot.press("A")
            await pilot.pause()
            await pilot.press("escape")
            await app.workers.wait_for_complete()
            self.assertEqual(len(load_acks(ack_path())[0]), 1)
            # The exact count records one receipt per remaining alarm.
            await pilot.press("A")
            await pilot.pause()
            await pilot.press("2", "enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            pairs, ok = load_acks(ack_path())
            self.assertTrue(ok)
            self.assertEqual(pairs, {(r["id"], r["hash"]) for r in records})
            badge = str(app.query_one("#sentinel-status", Static).visual)
            self.assertIn("ALARMS(0)", badge)
            self.assertIn("3 dismissed", badge)
            self.assertEqual(app.query_one("#alarms-table", DataTable).row_count, 3)
            self.assertEqual(path.read_bytes(), original)  # the artifact is never edited
            with mock.patch.object(app, "notify") as notify:
                await pilot.press("A")
                await pilot.pause()
                notify.assert_called_once_with("No alarms to dismiss")

    async def test_shift_a_records_nothing_if_the_list_changed_before_confirming(self):
        records = build_records(findings())
        write_artifact(alarm_artifact_path(), records)
        app = OCDeckApp(self.source(), auto_refresh=False)
        async with app.run_test(size=(160, 42)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("6")
            await pilot.pause()
            await pilot.press("A")
            await pilot.pause()
            # A new alarm arrives while the owner is typing the count.
            new = RuleFinding(rule="S7", severity="CRITICAL", harness="codex", session_id="ses_new",
                              cwd="/p", summary="tmux kill-server attempted", criteria={})
            write_artifact(alarm_artifact_path(), build_records(bodies_of(records) + [finding_body(new)]))
            app._sentinel_refresh_worker()
            await app.workers.wait_for_complete()
            with mock.patch.object(app, "notify") as notify:
                await pilot.press("3", "enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertIn("The alarm list changed", notify.call_args.args[0])
            self.assertFalse(ack_path().exists())

    async def test_shift_a_outside_alarms_starts_an_archive_confirmation(self):
        write_artifact(alarm_artifact_path(), build_records(findings()))
        app = OCDeckApp(self.source(), auto_refresh=False)
        async with app.run_test(size=(160, 42)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("4")  # AGENTS
            await pilot.pause()
            with mock.patch.object(app, "notify") as notify:
                await pilot.press("A")  # first press: archive confirmation only
                await pilot.pause()
                notify.assert_called_once()
                self.assertEqual(
                    notify.call_args.args[0],
                    "Press Shift+A again to archive Astra refactor "
                    "(still running; it stays visible until it stops)",
                )
            self.assertNotIsInstance(app.screen, DismissAllScreen)
            self.assertFalse(ack_path().exists())

    async def test_shift_d_outside_alarms_writes_nothing(self):
        records = build_records(findings())
        write_artifact(alarm_artifact_path(), records)
        app = OCDeckApp(self.source(), auto_refresh=False)
        async with app.run_test(size=(160, 42)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("4")  # AGENTS
            await pilot.pause()
            with mock.patch.object(app, "notify") as notify:
                await pilot.press("D", "D")
                await pilot.pause()
                self.assertEqual(notify.call_count, 2)
                for call in notify.call_args_list:
                    self.assertEqual(call, mock.call("Dismiss works in ALARMS (6)",
                                                     severity="warning", timeout=4))
            self.assertFalse(ack_path().exists())
            self.assertIsNone(app.alarm_by_id.get(""))


if __name__ == "__main__":
    unittest.main()
