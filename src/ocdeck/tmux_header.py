"""A uniform top header for every agent terminal (OpenCode, Claude Code, Codex, plugins).

    ⬢ Claude Code · claude-opus-5-5 │ Agents Start · Parser refactor      cc-1234… · 18:42

Text that comes from outside OC Deck (project names from the Drive-synced
catalog, session titles, model ids) is stored in tmux *user options* and the
status format only references them (``#{@ocdeck_title}``). tmux inserts such
values literally: ``#(...)`` commands, ``#[...]`` styles and nested formats in
them are never interpreted, so a crafted name cannot run shell commands
(security plan finding G1).
"""

from __future__ import annotations

import re
import subprocess
from typing import Callable, Sequence

from .harnesses import HARNESS_LABELS, HARNESS_STYLES, managed_tmux_prefixes, model_name

HEADER_BACKGROUND = "#0d1a25"
_HEX = re.compile(r"#[0-9A-Fa-f]{6}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩]")
FIELDS = ("harness", "model", "project", "title")


def _clean(value: str, limit: int) -> str:
    value = " ".join(_CONTROL.sub(" ", value or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _colour(value: str, fallback: str = "#8ba4b5") -> str:
    """Only literal #rrggbb colours reach the format string."""
    return value if _HEX.fullmatch(value or "") else fallback


def header_values(harness: str, model: str, project: str, title: str) -> dict[str, str]:
    return {
        "harness": _clean(HARNESS_LABELS.get(harness, harness), 24),
        "model": _clean(model_name(model) or "model not reported", 40),
        "project": _clean(project, 40),
        "title": _clean(title, 80),
    }


def status_left(accent: str, harness_colour: str) -> str:
    accent, harness_colour = _colour(accent), _colour(harness_colour)
    return (
        f"#[bold,fg={harness_colour}] ⬢ #{{@ocdeck_harness}} #[nobold,fg=#8ba4b5]· #{{@ocdeck_model}} "
        f"#[fg={accent}]│ #[bold]#{{@ocdeck_project}}#[nobold,fg=#e7f5fc] · #{{@ocdeck_title}} "
    )


def header_commands(
    name: str, *, harness: str, model: str = "", project: str = "", title: str = "", accent: str = "",
) -> list[list[str]]:
    """tmux argv lists that give session ``name`` the header."""
    values = header_values(harness, model, project, title)
    # set-option resolves a window/pane target even for session options.
    # The trailing colon makes '=name' an exact *session* component.
    target = f"={name}:"
    commands = [["tmux", "set-option", "-t", target, f"@ocdeck_{field}", values[field]] for field in FIELDS]
    harness_colour = HARNESS_STYLES.get(harness, "")
    options = (
        ("status", "on"),
        ("status-position", "top"),
        ("status-interval", "15"),
        ("status-style", f"bg={HEADER_BACKGROUND},fg={_colour(accent)}"),
        ("status-left-length", "220"),
        ("status-left", status_left(accent, harness_colour)),
        ("status-right", "#[fg=#668094]#{session_name} · %H:%M "),
        ("pane-border-style", f"fg={_colour(accent)}"),
        ("pane-active-border-style", f"fg={_colour(accent)},bold"),
    )
    commands += [["tmux", "set-option", "-t", target, option, value] for option, value in options]
    return commands


def apply_header(
    name: str,
    *,
    harness: str,
    model: str = "",
    project: str = "",
    title: str = "",
    accent: str = "",
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> bool:
    """Apply the header to one OC Deck-managed tmux session; never raises."""
    if not name.startswith(managed_tmux_prefixes()):
        return False  # never restyle a user's own tmux sessions
    for argv in header_commands(name, harness=harness, model=model, project=project, title=title, accent=accent):
        try:
            result = run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return False
        if result.returncode != 0:
            return False
    return True


def headers_for_sessions(sessions: Sequence, project_names: dict[str, str], accents: dict[str, str]):
    """(tmux name, header kwargs) for every managed terminal of every live session."""
    seen: set[str] = set()
    for session in sorted(sessions, key=lambda s: bool(getattr(s, "parent_id", "") or getattr(s, "agent_parent_id", ""))):
        if not getattr(session, "instance_count", 0):
            continue
        for terminal in getattr(session, "terminals", ()):
            if terminal.startswith(managed_tmux_prefixes()) and terminal not in seen:
                seen.add(terminal)
                yield terminal, {
                    "harness": getattr(session, "harness", "") or "opencode",
                    "model": session.model,
                    "project": project_names.get(session.project_id, ""),
                    "title": session.title,
                    "accent": accents.get(session.project_id, ""),
                }
