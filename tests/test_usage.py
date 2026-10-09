"""Token usage and remaining quota: every reader runs against fixture files, never $HOME."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from ocdeck import usage
from ocdeck.usage import (Limit, ProviderUsage, UsageReport, collect_claude, collect_codex, collect_opencode,
                          collect_usage, epoch, read_budgets, window_label)

NOW = 1_791_500_000.0
HOUR = 3600


def iso(offset_seconds: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(NOW + offset_seconds, timezone.utc).isoformat().replace("+00:00", "Z")


@pytest.fixture(autouse=True)
def fresh_caches():
    usage._claude_cache.clear()
    usage._codex_cache.clear()
    usage._opencode_state.clear()
    yield


def write_lines(path: Path, entries: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
    os.utime(path, (NOW, NOW))
    return path


def claude_entry(message_id: str, ago_hours: float, *, inp=0, out=0, write=0, read=0, kind="assistant"):
    return {"type": kind, "timestamp": iso(-ago_hours * HOUR), "requestId": "req_" + message_id,
            "message": {"id": message_id, "usage": {
                "input_tokens": inp, "output_tokens": out,
                "cache_creation_input_tokens": write, "cache_read_input_tokens": read}}}


# --- Claude Code ---------------------------------------------------------------

def test_claude_tokens_are_split_by_window_and_cached_reads_stay_apart(tmp_path):
    write_lines(tmp_path / "proj" / "s1.jsonl", [
        claude_entry("m1", 1, inp=100, out=50, write=10, read=1000),      # inside 5h, 24h, 7d
        claude_entry("m2", 10, inp=200, out=20, read=500),                # inside 24h, 7d
        claude_entry("m3", 100, inp=300, out=30),                         # inside 7d only
        claude_entry("m4", 24 * 8, inp=999, out=999),                     # older than 7d: ignored
    ])
    item = collect_claude(NOW, tmp_path, tmp_path / "state")
    assert item.tallies["5h"].fresh == 160 and item.tallies["5h"].cached == 1000
    assert item.tallies["24h"].fresh == 160 + 220 and item.tallies["24h"].messages == 2
    assert item.tallies["7d"].fresh == 160 + 220 + 330 and item.tallies["7d"].cached == 1500
    assert item.tallies["7d"].cost is None  # transcripts keep no cost


def test_claude_streamed_and_resumed_messages_are_counted_once(tmp_path):
    # The same message is written several times while it streams (output grows) ...
    write_lines(tmp_path / "p" / "a.jsonl", [claude_entry("m1", 1, inp=10, out=1),
                                              claude_entry("m1", 1, inp=10, out=40)])
    # ... and copied into the transcript of a resumed session, and a helper's transcript.
    write_lines(tmp_path / "p" / "b.jsonl", [claude_entry("m1", 1, inp=10, out=40),
                                              claude_entry("m2", 2, inp=5, out=5)])
    write_lines(tmp_path / "p" / "b" / "subagents" / "agent-1.jsonl", [claude_entry("m3", 2, inp=1, out=1)])
    tally = collect_claude(NOW, tmp_path, tmp_path / "state").tallies["7d"]
    assert (tally.fresh_input, tally.output, tally.messages) == (10 + 5 + 1, 40 + 5 + 1, 3)


def test_claude_ignores_damage_and_non_assistant_lines(tmp_path):
    path = tmp_path / "p" / "a.jsonl"
    write_lines(path, [claude_entry("u", 1, inp=7, out=7, kind="user"), claude_entry("ok", 1, inp=1, out=2)])
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"usage": not json}\n{"type":"assistant","message":{"usage":"x"}}\n')
    os.utime(path, (NOW, NOW))
    assert collect_claude(NOW, tmp_path, tmp_path / "state").tallies["7d"].fresh == 3
    assert collect_claude(NOW, tmp_path / "missing", tmp_path / "state").tallies["7d"].fresh == 0


def test_claude_cache_rereads_only_a_changed_file(tmp_path):
    path = write_lines(tmp_path / "p" / "a.jsonl", [claude_entry("m1", 1, inp=1, out=1)])
    assert collect_claude(NOW, tmp_path, tmp_path / "s").tallies["7d"].fresh == 2
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(claude_entry("m2", 1, inp=10, out=10)) + "\n")
    os.utime(path, (NOW + 1, NOW + 1))
    assert collect_claude(NOW, tmp_path, tmp_path / "s").tallies["7d"].fresh == 22


def write_snapshot(directory: Path, limits: dict, captured: float = NOW - 60):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / usage.SNAPSHOT_NAME).write_text(json.dumps({"captured": captured, "rate_limits": limits}))


def test_claude_limits_come_from_the_status_line_snapshot(tmp_path):
    write_snapshot(tmp_path, {"five_hour": {"used_percentage": 12.5, "resets_at": NOW + 3 * HOUR},
                              "seven_day": {"used_percentage": 140, "resets_at": NOW + 86400}})
    five, seven = usage.claude_limits(NOW, tmp_path)
    assert (five.label, five.used_percent, five.resets_at) == ("5h", 12.5, NOW + 3 * HOUR)
    assert seven.used_percent == 100.0  # clamped
    assert five.source == "Claude Code status line" and five.as_of == NOW - 60


def test_a_claude_window_that_has_reset_shows_no_stale_percentage(tmp_path):
    write_snapshot(tmp_path, {"five_hour": {"used_percentage": 90, "resets_at": NOW - 5}})
    (limit,) = usage.claude_limits(NOW, tmp_path)
    assert limit.used_percent is None and limit.stale


@pytest.mark.parametrize("content", ["", "{not json", "[]", '{"rate_limits": []}',
                                      json.dumps({"rate_limits": {"five_hour": {"used_percentage": "x"}}})])
def test_claude_limits_survive_a_broken_snapshot(tmp_path, content):
    (tmp_path / usage.SNAPSHOT_NAME).write_text(content)
    assert usage.claude_limits(NOW, tmp_path) == ()
    assert usage.claude_limits(NOW, tmp_path / "nope") == ()


def test_without_any_reading_claude_explains_how_to_get_one(tmp_path):
    assert "start a Claude session from OC Deck" in collect_claude(NOW, tmp_path, tmp_path / "s").note


# --- Codex ---------------------------------------------------------------------

def codex_event(ago_hours: float, *, total: int, last_in=0, last_cached=0, last_out=0, limits=None, info=True):
    payload = {"type": "token_count", "rate_limits": limits}
    if info:
        payload["info"] = {"total_token_usage": {"total_tokens": total},
                           "last_token_usage": {"input_tokens": last_in, "cached_input_tokens": last_cached,
                                                "output_tokens": last_out}}
    return {"timestamp": iso(-ago_hours * HOUR), "type": "event_msg", "payload": payload}


def codex_file(root: Path, name: str, events: list[dict], age_days: float = 0) -> Path:
    path = write_lines(root / "2026" / "10" / "09" / f"rollout-{name}.jsonl", events)
    os.utime(path, (NOW - age_days * 86400, NOW - age_days * 86400))
    return path


def test_codex_tokens_use_each_request_once_and_split_cached_input(tmp_path):
    limits = {"primary": {"used_percent": 28.0, "window_minutes": 10080, "resets_at": NOW + 2 * 86400},
              "secondary": {"used_percent": 61.0, "window_minutes": 300, "resets_at": NOW + 2 * HOUR},
              "plan_type": "prolite"}
    codex_file(tmp_path, "a", [
        codex_event(1, total=1000, last_in=1000, last_cached=800, last_out=100, limits=limits),
        codex_event(1, total=1000, last_in=1000, last_cached=800, last_out=100, limits=limits),  # repeat
        codex_event(2, total=500, last_in=500, last_cached=0, last_out=50),
        codex_event(1, total=1, info=False, limits=limits),                                       # limits only
    ])
    item = collect_codex(NOW, tmp_path)
    assert (item.tallies["5h"].fresh_input, item.tallies["5h"].output, item.tallies["5h"].cached) == (700, 150, 800)
    five, seven = sorted(item.limits, key=lambda limit: limit.label)
    assert (five.label, five.used_percent) == ("5h", 61.0)
    assert (seven.label, seven.used_percent, seven.stale) == ("7d", 28.0, False)
    assert "plan prolite" in item.note


def test_codex_old_reading_is_kept_but_marked_when_its_window_has_reset(tmp_path):
    limits = {"primary": {"used_percent": 28.0, "window_minutes": 10080, "resets_at": NOW - 5 * 86400}}
    codex_file(tmp_path, "old", [codex_event(24 * 12, total=9, last_in=9, limits=limits)], age_days=12)
    item = collect_codex(NOW, tmp_path)
    (limit,) = item.limits
    assert limit.used_percent is None and limit.stale and limit.label == "7d"
    assert item.tallies["7d"].fresh == 0 and "old" in item.note


def test_codex_without_rollouts_says_so(tmp_path):
    item = collect_codex(NOW, tmp_path / "none")
    assert item.limits == () and "no Codex rate-limit reading" in item.note


def test_window_labels_and_epochs():
    assert (window_label(10080), window_label(300), window_label(45), window_label(None)) == ("7d", "5h", "45m", "limit")
    assert epoch(1_791_500_000) == 1_791_500_000 and epoch(1_791_500_000_000) == 1_791_500_000
    assert epoch("2026-10-09T02:00:00Z") == pytest.approx(1_791_511_200, abs=3600 * 24)
    assert epoch("garbage") is None and epoch(True) is None and epoch(float("nan")) is None


# --- OpenCode ------------------------------------------------------------------

def make_database(path: Path, rows: list[tuple]) -> Path:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE session_message (id TEXT, session_id TEXT, type TEXT, seq INTEGER,"
                       " time_created INTEGER, time_updated INTEGER, data TEXT)")
    for index, (kind, ago_hours, provider, tokens, cost) in enumerate(rows):
        body = {"model": {"id": "m", "providerID": provider}, "cost": cost, "tokens": tokens}
        connection.execute("INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
                           (f"id{index}", "ses", kind, index, int((NOW - ago_hours * HOUR) * 1000), 0, json.dumps(body)))
    connection.commit()
    connection.close()
    return path


def tokens(inp=0, out=0, reasoning=0, read=0, write=0):
    return {"input": inp, "output": out, "reasoning": reasoning, "cache": {"read": read, "write": write}}


def test_opencode_usage_is_summed_per_provider_and_window(tmp_path):
    database = make_database(tmp_path / "o.db", [
        ("assistant", 1, "openai", tokens(100, 20, 5, 900, 10), 0.0),
        ("assistant", 30, "openai", tokens(200, 30), 0.0),
        ("compaction", 2, "deepseek", tokens(50, 5, 0, 10), 1.25),
        ("assistant", 24 * 9, "openai", tokens(7777, 7777), 5.0),           # outside 7d
        ("user", 1, "openai", tokens(5000, 5000), 9.0),                      # not a model call
        ("assistant", 1, "ghost", tokens(0, 0), 0.0),                        # failed call: nothing spent
    ])
    items = {item.key: item for item in collect_opencode(NOW, database)}
    assert set(items) == {"opencode:openai", "opencode:deepseek"}
    openai = items["opencode:openai"]
    assert (openai.tallies["5h"].fresh_input, openai.tallies["5h"].output, openai.tallies["5h"].cached) == (110, 25, 900)
    assert openai.tallies["7d"].fresh == 110 + 25 + 200 + 30
    assert openai.tallies["24h"].messages == 1 and openai.tallies["7d"].messages == 2
    assert items["opencode:deepseek"].tallies["5h"].cost == pytest.approx(1.25)
    assert openai.name == "OpenAI · via OpenCode"
    assert [item.key for item in collect_opencode(NOW, database)][0] == "opencode:openai"  # biggest first


def test_opencode_refresh_reads_only_the_recent_past_but_stays_correct(tmp_path):
    database = make_database(tmp_path / "o.db", [
        ("assistant", 40, "openai", tokens(1000, 0), 0.0),      # old: kept from the first read
        ("assistant", 0.5, "openai", tokens(10, 0), 0.0),       # recent and still being written
    ])
    first = {item.key: item for item in collect_opencode(NOW, database)}["opencode:openai"]
    assert first.tallies["7d"].fresh == 1010
    connection = sqlite3.connect(database)
    # The message that was in progress finishes (its row is updated), a new one arrives, and an
    # old row is edited behind our back: only the recent past is re-read, so that edit is not seen.
    connection.execute("UPDATE session_message SET data = json_set(data, '$.tokens.output', 90) WHERE id = 'id1'")
    connection.execute("UPDATE session_message SET data = json_set(data, '$.tokens.input', 5) WHERE id = 'id0'")
    connection.execute("INSERT INTO session_message VALUES ('new','ses','assistant',9,?,0,?)",
                       (int((NOW - 0.2 * HOUR) * 1000), json.dumps({"model": {"providerID": "openai"}, "cost": 2.0,
                                                                  "tokens": tokens(100, 0)})))
    connection.commit()
    connection.close()
    second = {item.key: item for item in collect_opencode(NOW, database)}["opencode:openai"]
    assert second.tallies["5h"].fresh == 10 + 90 + 100 and second.tallies["5h"].messages == 2
    assert second.tallies["5h"].cost == pytest.approx(2.0)
    assert second.tallies["7d"].fresh == 1000 + 200       # the old row is still the cached value
    # Time moves on: the 5h-old rows leave that window without any re-read of the old past.
    later = {item.key: item for item in collect_opencode(NOW + 6 * HOUR, database)}["opencode:openai"]
    assert later.tallies["5h"].fresh == 0 and later.tallies["24h"].fresh == 200


def test_opencode_database_problems_become_a_note_not_an_error(tmp_path):
    (missing,) = collect_opencode(NOW, tmp_path / "missing.db")
    assert "unreadable" in missing.note and not (tmp_path / "missing.db").exists()  # never created
    empty = sqlite3.connect(tmp_path / "e.db")
    empty.execute("CREATE TABLE other (x)")
    empty.close()
    (broken,) = collect_opencode(NOW, tmp_path / "e.db")
    assert "query failed" in broken.note


# --- budgets and the whole report ---------------------------------------------

def test_budgets_turn_spend_into_a_percentage_left(tmp_path):
    database = make_database(tmp_path / "o.db", [("assistant", 1, "openrouter", tokens(1000, 0), 5.0)])
    budgets = tmp_path / "usage.json"
    budgets.write_text(json.dumps({"limits": {
        "opencode:openrouter": {"7d": {"usd": 20}, "5h": {"tokens": 4000}, "1y": {"usd": 1}, "24h": "bad"},
        "codex": {"7d": {"usd": -3}}}}))
    report = collect_usage(NOW, claude_root=tmp_path / "c", codex_root=tmp_path / "x", opencode_db=database,
                           directory=tmp_path / "s", budgets_file=budgets)
    (item,) = [item for item in report.providers if item.key == "opencode:openrouter"]
    by_label = {limit.label: limit for limit in item.limits}
    assert by_label["budget 7d"].used_percent == pytest.approx(25.0)
    assert by_label["budget 5h"].used_percent == pytest.approx(25.0)
    assert set(by_label) == {"budget 7d", "budget 5h"} and by_label["budget 7d"].source == "budget"
    assert read_budgets(tmp_path / "missing.json") == {}


def test_a_real_provider_limit_is_not_overridden_by_a_budget(tmp_path):
    write_snapshot(tmp_path / "s", {"seven_day": {"used_percentage": 10, "resets_at": NOW + HOUR}})
    budgets = tmp_path / "usage.json"
    budgets.write_text(json.dumps({"limits": {"claude-code": {"7d": {"tokens": 1}}}}))
    report = collect_usage(NOW, claude_root=tmp_path / "c", codex_root=tmp_path / "x",
                           opencode_db=tmp_path / "none.db", directory=tmp_path / "s", budgets_file=budgets)
    claude = next(item for item in report.providers if item.key == "claude-code")
    assert [limit.label for limit in claude.limits] == ["7d"]


def test_one_broken_reader_never_hides_the_others(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "collect_codex", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    report = collect_usage(NOW, claude_root=tmp_path / "c", opencode_db=tmp_path / "none.db",
                           directory=tmp_path / "s", budgets_file=tmp_path / "b.json")
    keys = [item.key for item in report.providers]
    assert "claude-code" in keys and "opencode" in keys
    codex = next(item for item in report.providers if item.key == "codex")
    assert codex.note == "could not read (RuntimeError)"


def test_text_helpers():
    assert usage.age_text(0) == "<1m" and usage.age_text(45 * 60) == "45m"
    assert usage.age_text(2 * HOUR + 600) == "2h 10m" and usage.age_text(3 * 86400 + 4 * HOUR) == "3d 4h"
    assert usage.short_count(999) == "999" and usage.short_count(1500) == "1.5K"
    assert usage.short_count(27_200_000) == "27.2M" and usage.short_count(1_900_000_000) == "1.9B"
    assert usage.money(None) == "-" and usage.money(3.456) == "$3.46"


def test_headline_lists_only_real_unexpired_limits():
    report = UsageReport(NOW, (
        ProviderUsage("claude-code", "A", (Limit("5h", 12.4, NOW + 1, "s"), Limit("7d", 41.0, NOW + 1, "s"))),
        ProviderUsage("codex", "B", (Limit("7d", None, NOW - 1, "s", stale=True),)),
        ProviderUsage("opencode:x", "C", (Limit("budget 7d", 80.0, None, "budget"),)),
    ))
    assert usage.headline(report) == "cc 5h 12% 7d 41%"


def test_the_text_report_shows_used_left_and_reset():
    report = UsageReport(NOW, (ProviderUsage(
        "claude-code", "Anthropic · Claude Code", (Limit("5h", 12.0, NOW + 3 * HOUR, "Claude Code status line"),),
        {label: usage.Tally(1500, 0, 0, None, 1) for label, _ in usage.WINDOWS}, note="hello"),))
    text = usage.format_report(report)
    assert "ANTHROPIC · CLAUDE CODE" in text and "12.0% used" in text and "88.0% left" in text
    assert "resets in 3h" in text and "5h 1.5K" in text and "note       hello" in text
