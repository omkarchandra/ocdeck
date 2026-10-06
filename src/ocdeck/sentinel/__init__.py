"""Sentinel — central agent-activity monitor (P0c observation pilot).

Implements a small, reviewed slice of a central monitoring plan:
bounded incremental
transcript tailing, explicit criteria rules (S1/S3/S4/S7/S8/S14), and a
bounded hash-chained alarm artifact. Registry/served as a library plus a
``python -m ocdeck.sentinel --once`` CLI.

Honest adapter status (C102/C110 — statuses, not claims):
- Claude Code transcripts (``~/.claude/projects/**/*.jsonl``, recursive,
  including sidechain subdirectories): **OBSERVED**.
- OpenCode V2: **OBSERVED** through the sanctioned read bridge
  (``v2.session.list`` + ``v2.session.message.list``, tool-call intent only;
  contract verified live against 2.0.14 — see ``opencode.py``).
- Codex: **INACTIVE** while ``~/.codex`` is absent.
- S10 (unsanctioned spawn) and S11 (bypass launch) require live process
  evidence per C2-r2/C3-r2 and are reserved, not implemented here.

Evidence semantics (C105): a tool command in a transcript is *intent*
evidence. Findings are labeled ``attempted``; the S4 composite is labeled
``suspected``, never "confirmed exfiltration".

Interim trust caveat (C103): this module currently lives in the writable
development checkout; until the read-only release (P1-8) it must be treated
as observation tooling, not a tamper-proof control plane.
"""
from __future__ import annotations

from .alarms import alarm_artifact_path, load_alarms, write_alarms
from .collect import ScanResult, ToolEvent, discover_roots, scan_transcripts
from .opencode import scan_opencode
from .rules import RuleConfig, RuleFinding, RulesUnavailable, evaluate_events, s8_finding

__all__ = [
    "alarm_artifact_path", "load_alarms", "write_alarms",
    "ScanResult", "ToolEvent", "discover_roots", "scan_transcripts", "scan_opencode",
    "RuleConfig", "RuleFinding", "RulesUnavailable", "evaluate_events", "s8_finding",
]
