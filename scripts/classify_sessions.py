#!/usr/bin/env python3
"""Classify every current session: owner-opened, agent-opened, or unknown.

READ-ONLY. Loads the same session stores OC Deck itself reads — the OpenCode
SQLite session database, Claude transcripts, Codex rollouts, and OC Deck's
stored lineage and owner-opened state files — and prints one line per session:

    <title> | <harness> | <verdict> | <signal>

Nothing here writes, launches, or attaches anything: no tmux queries (the
harness adapters run without live-process and tmux tables), no process scans,
no state refreshes, no pending-launch resolution. Verdicts come from the
signals verified in ocdeck.session_origin: an owner record beats every agent
signal, and no signal means unknown, which renders like an owner row.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ocdeck.harnesses import ClaudeHarness, CodexHarness, load_lineage  # noqa: E402
from ocdeck.models import SessionRecord, parse_sessions  # noqa: E402
from ocdeck.session_origin import (  # noqa: E402
    AGENT_VERDICT,
    OWNER_VERDICT,
    UNKNOWN_VERDICT,
    classify_session,
    load_owner_sessions,
)
from ocdeck.source import read_sessions_from_database  # noqa: E402


def opencode_sessions() -> list[SessionRecord]:
    """OpenCode sessions straight from its session database, read-only."""
    payload = read_sessions_from_database()
    if not payload:
        return []
    return list(parse_sessions(payload))


def foreign_sessions() -> list[SessionRecord]:
    """Claude and Codex transcripts, without live process or tmux queries."""
    sessions: list[SessionRecord] = []
    for adapter in (ClaudeHarness(), CodexHarness()):
        try:
            # processes=[]/tmux={} keep this read-only: no /proc scan, no
            # `tmux list-panes`, and no Codex app-server status exchange.
            sessions.extend(adapter.collect(processes=[], tmux={}))
        except Exception as error:  # one broken store must not hide the rest
            print(
                f"{adapter.harness}: unavailable: {type(error).__name__}: {error}",
                file=sys.stderr,
            )
    return sessions


def with_lineage(sessions: list[SessionRecord]) -> list[SessionRecord]:
    """Apply OC Deck's stored lineage map (apply_lineage minus its write)."""
    known = load_lineage()
    if not known:
        return sessions
    ids = {session.id for session in sessions}
    return [
        replace(session, agent_parent_id=known[session.id])
        if session.id in known
        and known[session.id] in ids
        and not (session.parent_id or session.agent_parent_id)
        else session
        for session in sessions
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Classify current sessions by who opened them (read-only)."
    )
    parser.add_argument(
        "--limit", type=int, default=500,
        help="Maximum sessions to read per store (like the deck's limits)",
    )
    args = parser.parse_args(argv)

    sessions = with_lineage(opencode_sessions() + foreign_sessions())
    sessions.sort(key=lambda session: (-session.updated_ms, -session.created_ms))
    sessions = sessions[: max(1, args.limit)]
    if not sessions:
        print("no sessions found in any store", file=sys.stderr)
        return 1

    owner_ids = load_owner_sessions()
    counts = {OWNER_VERDICT: 0, AGENT_VERDICT: 0, UNKNOWN_VERDICT: 0}
    for session in sessions:
        origin = classify_session(session, owner_ids)
        counts[origin.verdict] += 1
        print(f"{session.title} | {session.harness} | {origin.verdict} | {origin.signal}")
    print(
        f"{len(sessions)} sessions: {counts[OWNER_VERDICT]} owner, "
        f"{counts[AGENT_VERDICT]} agent, {counts[UNKNOWN_VERDICT]} unknown",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
