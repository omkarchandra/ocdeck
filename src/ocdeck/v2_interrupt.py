"""Interrupt a running OpenCode V2 turn the way the owner would: Esc, then Esc.

A V2 agent runs inside the OpenCode service; closing or killing its terminal
does not stop the turn. OpenCode's own interrupt is Esc pressed twice in the
client. This sends exactly those two keys into the OC Deck-managed ``oc2-``
tmux pane, and only after checking that the pane shows OpenCode and that a
turn is running. The second Esc is sent only once OpenCode asks for it.
"""
from __future__ import annotations

import subprocess
import time
from typing import Callable

RUNNING = "esc interrupt"
CONFIRM = "esc again to interrupt"

INTERRUPTED = "interrupted"
IDLE = "idle"
NOT_OPENCODE = "not-opencode"
FAILED = "failed"


def _footer(run: Callable[..., subprocess.CompletedProcess], target: str) -> str | None:
    """The pane's last non-empty line: OpenCode's status footer.

    Only the footer decides. Conversation text above it can quote the same
    words ("esc interrupt") and must never trigger a key press.
    """
    try:
        result = run(["tmux", "capture-pane", "-p", "-t", target],
                     capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    return lines[-1].casefold() if lines else ""


def _wait_for(run, target, sleep, predicate, *, seconds: float) -> bool:
    waited = 0.0
    while True:
        footer = _footer(run, target)
        if footer is not None and predicate(footer):
            return True
        if waited >= seconds:
            return False
        sleep(0.2)
        waited += 0.2


def interrupt_v2_turn(
    name: str,
    *,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Return INTERRUPTED, IDLE, NOT_OPENCODE or FAILED; never raises."""
    if not name.startswith("oc2-"):
        return NOT_OPENCODE
    target = f"={name}:"  # exact session (G5), its current pane
    try:
        current = run(["tmux", "display-message", "-p", "-t", target, "#{pane_current_command}"],
                      capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return FAILED
    if current.returncode != 0 or not current.stdout.strip().startswith("opencode2"):
        return NOT_OPENCODE
    footer = _footer(run, target)
    if footer is None:
        return FAILED
    if RUNNING not in footer and CONFIRM not in footer:
        return IDLE

    def escape() -> bool:
        try:
            return run(["tmux", "send-keys", "-t", target, "Escape"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=3, check=False).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    if CONFIRM not in footer:
        if not escape():
            return FAILED
        sleep(0.3)
        if not _wait_for(run, target, sleep, lambda text: CONFIRM in text, seconds=2.0):
            return FAILED  # never send a second Esc OpenCode did not ask for
    if not escape():
        return FAILED
    stopped = _wait_for(run, target, sleep,
                        lambda text: RUNNING not in text and CONFIRM not in text, seconds=6.0)
    return INTERRUPTED if stopped else FAILED
