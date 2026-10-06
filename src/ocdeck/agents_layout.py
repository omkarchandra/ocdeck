"""Compact Agents-board cells and column widths.

Every column up to RUNTIME stays visible down to narrow windows: long words
become short codes, SESSION and PROJECT shrink first, and DETAIL is the only
column that hides (the focus strip under the table always shows full text).

    STATE      TERM  SESSION               PROJECT   AGE  DETAIL        RUNTIME
    ● RUN 2m   ●OPN  CC Parser refactor    alpha     2m   refactor the  CC OP5
"""

from __future__ import annotations

from dataclasses import dataclass

# Column order on the board.
COLUMNS = ("STATE", "TERM", "SESSION", "PROJECT", "AGE", "DETAIL", "RUNTIME")

# Agent state -> (symbol, short code). Matches OC Deck's state names.
STATE_CODES = {
    "busy": ("●", "RUN"),
    "permission": ("!", "PERM"),
    "question": ("?", "ASK"),
    "retry": ("◆", "RTRY"),
    "stalled": ("!", "STAL"),
    "review": ("◑", "REV"),
    "open": ("●", "IDLE"),
    "idle": ("○", "IDLE"),
    "closed": ("○", "CLSD"),
    "job": ("◆", "JOB"),
}
STATE_WORDS = {
    "RUN": "running", "PERM": "needs permission", "ASK": "has a question", "RTRY": "retrying",
    "STAL": "stalled", "REV": "waiting for you", "IDLE": "idle", "CLSD": "closed",
    "JOB": "background job",
}
TERM_CODES = {"attached": "●OPN", "tmux": "○TMX", "direct": "◆DIR", "server": "○SRV", "saved": "○SAV"}
TERM_WORDS = {
    "●OPN": "open in a window", "○TMX": "background tmux", "◆DIR": "direct terminal tab",
    "○SRV": "server session", "○SAV": "saved (closed)",
}


def state_cell(state: str, age: str = "", *, inherited: bool = False) -> str:
    """'● RUN 2m' — at most 9 characters."""
    symbol, code = STATE_CODES.get(state, ("○", state[:4].upper() or "?"))
    if inherited:
        symbol = "↳"  # the state comes from a nested helper, not this session
    text = f"{symbol} {code}"
    return f"{text} {age}"[:9] if age and len(text) + 1 + len(age) <= 9 else text


def term_kind(*, attached: bool, tmux: bool, live: bool, closed: bool = False) -> str:
    if closed:
        return "saved"
    if attached:
        return "attached"
    if tmux:
        return "tmux"
    return "direct" if live else "server"


def term_cell(kind: str) -> str:
    return TERM_CODES.get(kind, "○SRV")


def legend(state: str, term: str) -> str:
    """Full words for the focus strip: 'running · background tmux'."""
    _symbol, code = STATE_CODES.get(state, ("", state.upper()))
    return f"{STATE_WORDS.get(code, state)} · {TERM_WORDS.get(term, term)}"


@dataclass(frozen=True, slots=True)
class Widths:
    state: int
    term: int
    session: int
    project: int
    age: int
    detail: int
    runtime: int

    def as_tuple(self) -> tuple[int, ...]:
        return (self.state, self.term, self.session, self.project, self.age, self.detail, self.runtime)

    @property
    def full_runtime(self) -> bool:
        return self.runtime >= 20

    def used(self) -> int:
        """Characters the visible columns need, including 1-cell padding each side."""
        visible = [width for width in self.as_tuple() if width]
        return sum(visible) + 2 * len(visible)


# Minimum / preferred widths. STATE, TERM, AGE and RUNTIME codes never shrink.
STATE_W, TERM_W, AGE_W, RUNTIME_CODE_W, RUNTIME_FULL_W = 9, 4, 4, 6, 30
SESSION_MIN, SESSION_MAX = 10, 34
PROJECT_MIN, PROJECT_MAX = 6, 16
DETAIL_MIN, DETAIL_MAX = 10, 80
CHROME = 6  # tabs' horizontal margins and table scrollbar


def column_widths(screen_width: int) -> Widths:
    """Fit every column through RUNTIME; DETAIL hides first, PROJECT only when tiny."""
    runtime = RUNTIME_FULL_W if screen_width >= 160 else RUNTIME_CODE_W
    # Space left for SESSION, PROJECT and DETAIL after the fixed columns and
    # the padding (1 cell each side) of STATE, TERM, AGE and RUNTIME.
    room = screen_width - CHROME - (STATE_W + TERM_W + AGE_W + runtime) - 2 * 4
    with_detail = room - 3 * 2  # padding of SESSION, PROJECT and DETAIL
    if with_detail >= SESSION_MIN + PROJECT_MIN + DETAIL_MIN:
        session = max(SESSION_MIN, min(SESSION_MAX, with_detail * 2 // 3))
        project = max(PROJECT_MIN, min(PROJECT_MAX, with_detail - session - DETAIL_MIN))
        detail = min(DETAIL_MAX, with_detail - session - project)
        if detail >= DETAIL_MIN:
            return Widths(STATE_W, TERM_W, session, project, AGE_W, detail, runtime)
    names = room - 2 * 2  # SESSION and PROJECT padding
    if names >= SESSION_MIN + PROJECT_MIN:
        session = min(SESSION_MAX, max(SESSION_MIN, names * 2 // 3))
        return Widths(STATE_W, TERM_W, session, min(PROJECT_MAX, names - session), AGE_W, 0, runtime)
    # Tiny windows: PROJECT goes too (the focus strip still shows it).
    return Widths(STATE_W, TERM_W, max(1, min(SESSION_MAX, room - 2)), 0, AGE_W, 0, runtime)
