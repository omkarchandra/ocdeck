"""Interrupt a running Claude Code turn the way the owner would: one Esc.

Closing or killing a Claude terminal throws away the live session. Claude Code's own
interrupt is Esc in its client; the conversation stays open. This sends exactly that one
key into the OC Deck-managed ``cc-`` tmux pane, and only after checking that the pane is
Claude and that its status footer says a turn is running ("esc to interrupt"). A
permission or question dialog has a different footer ("Esc to cancel"), so a pending
prompt is never answered by this: Esc there would mean "No".
"""
from __future__ import annotations

import subprocess
import time
from typing import Callable

from .v2_interrupt import FAILED, IDLE, INTERRUPTED, _footer, _wait_for

RUNNING = "esc to interrupt"
NOT_CLAUDE = "not-claude"
PANE_COMMANDS = {"claude", "node"}  # how tmux names the process of a native or npm install


def interrupt_claude_turn(
    name: str,
    *,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Return INTERRUPTED, IDLE, NOT_CLAUDE or FAILED; never raises, sends at most one Esc."""
    if not name.startswith("cc-"):
        return NOT_CLAUDE
    target = f"={name}:"  # exact session (G5), its current pane
    try:
        current = run(["tmux", "display-message", "-p", "-t", target, "#{pane_current_command}"],
                      capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return FAILED
    if current.returncode != 0 or current.stdout.strip() not in PANE_COMMANDS:
        return NOT_CLAUDE
    footer = _footer(run, target)
    if footer is None:
        return FAILED
    if RUNNING not in footer:
        return IDLE
    try:
        sent = run(["tmux", "send-keys", "-t", target, "Escape"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=3, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return FAILED
    if not sent:
        return FAILED
    stopped = _wait_for(run, target, sleep, lambda text: RUNNING not in text, seconds=6.0)
    return INTERRUPTED if stopped else FAILED
