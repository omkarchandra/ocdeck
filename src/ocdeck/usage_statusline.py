"""Claude Code status-line command: records the plan's 5-hour / 7-day usage for OC Deck.

OC Deck launches its Claude sessions with this as ``statusLine`` (see
``ClaudeHarness.permission_settings_arguments``). Claude Code hands the command a JSON
document on stdin each time the status line refreshes; for a Claude.ai subscription it
carries ``rate_limits.five_hour`` / ``seven_day`` with ``used_percentage`` and ``resets_at``.
Only those numbers are kept (``usage/claude-rate-limits.json``, owner-only, replaced
atomically); the status line itself becomes one short line such as ``5h 12% · 7d 41%``.
It never raises and never prints anything it did not build from numbers.
Standard library only: it runs on every refresh and must start fast.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from .usage import SNAPSHOT_NAME, _number, epoch, state_dir

KEYS = (("five_hour", "5h"), ("seven_day", "7d"))


def statusline_settings(python: str | None = None) -> dict[str, object]:
    """The ``--settings`` payload that makes Claude Code report its plan usage to the deck."""
    return {"statusLine": {"type": "command", "command": f"{python or sys.executable} -m ocdeck.usage_statusline"}}


def sanitized(data: object) -> dict[str, dict[str, float]]:
    """``{"five_hour": {"used_percentage": 12.0, "resets_at": 1.79e9}, ...}``, numbers only."""
    limits = data.get("rate_limits") if isinstance(data, dict) else None
    result: dict[str, dict[str, float]] = {}
    if not isinstance(limits, dict):
        return result
    for key, _ in KEYS:
        item = limits.get(key)
        if not isinstance(item, dict):
            continue
        used = _number(item.get("used_percentage"))
        if used is None:
            continue
        entry = {"used_percentage": max(0.0, min(100.0, used))}
        resets = epoch(item.get("resets_at"))
        if resets is not None:
            entry["resets_at"] = resets
        result[key] = entry
    return result


def status_text(limits: dict[str, dict[str, float]]) -> str:
    return " · ".join(f"{label} {limits[key]['used_percentage']:.0f}%" for key, label in KEYS if key in limits)


def record(limits: dict[str, dict[str, float]], directory: Path | None = None) -> bool:
    """Replace the snapshot; False when it could not be written."""
    directory = directory or state_dir()
    path = directory / SNAPSHOT_NAME
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"captured": time.time(), "rate_limits": limits}, handle)
        os.replace(temporary, path)
    except OSError:
        return False
    return True


def run(stdin_text: str, directory: Path | None = None) -> str:
    """Return the status-line text; record the limits as a side effect."""
    try:
        limits = sanitized(json.loads(stdin_text))
    except ValueError:
        return ""
    if limits:
        record(limits, directory)
    return status_text(limits)


def main() -> int:
    try:
        text = run(sys.stdin.read())
    except Exception:
        text = ""
    if text:
        sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
