"""The Claude Code status-line command that records the plan's 5h / 7d usage."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import ocdeck
from ocdeck import usage, usage_statusline as line
from ocdeck.harnesses import ClaudeHarness

SRC = str(Path(ocdeck.__file__).resolve().parents[1])
EVENT = {"session_id": "s", "model": {"display_name": "x"}, "cwd": "/private/place",
         "rate_limits": {"five_hour": {"used_percentage": 12.4, "resets_at": 1_791_511_200},
                         "seven_day": {"used_percentage": 41, "resets_at": 1_791_900_000.5},
                         "spend_limit": {"used_percentage": 3}}}


def test_only_the_limit_numbers_are_kept():
    kept = line.sanitized(EVENT)
    assert kept == {"five_hour": {"used_percentage": 12.4, "resets_at": 1_791_511_200.0},
                    "seven_day": {"used_percentage": 41.0, "resets_at": 1_791_900_000.5}}
    assert line.status_text(kept) == "5h 12% · 7d 41%"


@pytest.mark.parametrize("data", [None, [], {}, {"rate_limits": []}, {"rate_limits": {"five_hour": 3}},
                                  {"rate_limits": {"five_hour": {"used_percentage": "12"}}},
                                  {"rate_limits": {"five_hour": {"used_percentage": True}}}])
def test_input_without_usable_limits_records_nothing(data, tmp_path):
    assert line.sanitized(data) == {}
    assert line.run(json.dumps(data), tmp_path) == "" and list(tmp_path.iterdir()) == []


def test_a_reading_is_stored_owner_only_and_nothing_else_leaks_into_it(tmp_path):
    assert line.run(json.dumps(EVENT), tmp_path / "state") == "5h 12% · 7d 41%"
    path = tmp_path / "state" / usage.SNAPSHOT_NAME
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    stored = json.loads(path.read_text())
    assert set(stored) == {"captured", "rate_limits"}
    assert "/private/place" not in path.read_text() and "session" not in path.read_text()
    assert [item.name for item in path.parent.iterdir()] == [usage.SNAPSHOT_NAME]  # no temp file left
    (five, seven) = usage.claude_limits(1_791_500_000.0, path.parent)
    assert (five.label, five.used_percent, seven.label) == ("5h", 12.4, "7d")


def test_unwritable_state_never_breaks_the_status_line(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert line.run(json.dumps(EVENT), blocker / "inside") == "5h 12% · 7d 41%"
    assert line.run("not json", tmp_path) == ""


def test_the_command_runs_as_claude_code_runs_it(tmp_path):
    environment = {**os.environ, "OCDECK_USAGE_DIR": str(tmp_path), "PYTHONPATH": SRC}
    done = subprocess.run([sys.executable, "-m", "ocdeck.usage_statusline"], env=environment,
                          input=json.dumps(EVENT), text=True, capture_output=True, timeout=20)
    assert done.returncode == 0 and done.stdout == "5h 12% · 7d 41%\n" and done.stderr == ""
    assert (tmp_path / usage.SNAPSHOT_NAME).exists()
    silent = subprocess.run([sys.executable, "-m", "ocdeck.usage_statusline"], env=environment,
                            input="{}", text=True, capture_output=True, timeout=20)
    assert silent.returncode == 0 and silent.stdout == ""


# --- how a session gets it ---------------------------------------------------------

def settings_of(arguments: list[str]) -> dict:
    return json.loads(Path(arguments[arguments.index("--settings") + 1]).read_text())


def test_deck_launched_claude_sessions_carry_the_status_line(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OCDECK_CLAUDE_USAGE_STATUSLINE", "1")
    monkeypatch.setenv("OCDECK_CLAUDE_PERMISSION_HOOK", "1")
    settings = settings_of(ClaudeHarness(tmp_path / "p", "/bin/claude").launch_arguments(False))
    assert settings["statusLine"] == {"type": "command", "command": f"{sys.executable} -m ocdeck.usage_statusline"}
    assert "PermissionRequest" in settings["hooks"]  # the permission hook still rides along


def test_each_feature_has_its_own_off_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    harness = ClaudeHarness(tmp_path / "p", "/bin/claude")
    monkeypatch.setenv("OCDECK_CLAUDE_USAGE_STATUSLINE", "1")
    monkeypatch.setenv("OCDECK_CLAUDE_PERMISSION_HOOK", "0")
    only_line = settings_of(harness.launch_arguments(False))
    assert "statusLine" in only_line and "hooks" not in only_line
    monkeypatch.setenv("OCDECK_CLAUDE_USAGE_STATUSLINE", "0")
    monkeypatch.setenv("OCDECK_CLAUDE_PERMISSION_HOOK", "1")
    only_hook = settings_of(harness.launch_arguments(False))
    assert "hooks" in only_hook and "statusLine" not in only_hook
    monkeypatch.setenv("OCDECK_CLAUDE_PERMISSION_HOOK", "0")
    assert harness.launch_arguments(False) == ["--no-chrome"]
