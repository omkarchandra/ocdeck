"""``python -m ocdeck.sentinel --once`` — one bounded scan, then exit.

Observation pilot only (C110): writes the alarm artifact and prints a
summary, with deduplicated notifications. No enforcement. Interim caveat
(C103): runs from the writable development checkout until the read-only
release; treat output as tooling, not a tamper-proof control plane.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

from .alarms import (
    alarm_artifact_path,
    build_records,
    finding_body,
    load_alarms,
    write_alarms,
)
from .collect import scan_transcripts
from .codex import scan_codex
from .opencode import scan_opencode
from .notify import notify_new_records
from .rules import RulesUnavailable, RuleConfig, evaluate_events, s8_finding

DEFAULT_RULES = Path("~/.config/ocdeck/sentinel-rules.json").expanduser()


def state_dir() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return base / "ocdeck" / "sentinel"


def once(*, rules_path: Path | None = None, home: Path | None = None) -> int:
    rules_file = rules_path or DEFAULT_RULES
    scan = scan_transcripts(state_dir(), home=home)
    opencode_events, opencode_notes, opencode_status = scan_opencode(state_dir())
    scan.events.extend(opencode_events)
    scan.coverage_notes.extend(opencode_notes)
    scan.adapter_status["opencode"] = opencode_status
    codex_events, codex_notes, codex_status = scan_codex(state_dir(), home=home)
    scan.events.extend(codex_events)
    scan.coverage_notes.extend(codex_notes)
    scan.adapter_status["codex"] = codex_status

    findings = []
    rules_ok = True
    try:
        config = RuleConfig.load(rules_file)
    except RulesUnavailable as error:
        rules_ok = False
        findings.append(s8_finding(str(error)))
    else:
        findings.extend(evaluate_events(scan.events, config))

    # Persist unresolved records: merge with the existing artifact (C106)
    # instead of replacing it. A broken chain on the old artifact is kept
    # visible in meta rather than silently healed (C108).
    artifact_path = alarm_artifact_path()
    previous_payload, chain_ok = load_alarms(artifact_path)
    previous_bodies = []
    if previous_payload and chain_ok:
        previous_bodies = [
            {k: v for k, v in record.items() if k != "hash"}
            for record in previous_payload.get("records") or []
        ]
    carried_overflow = 0 if not previous_payload else previous_payload.get("meta", {}).get("overflow", 0)

    bodies = previous_bodies + [finding_body(f) for f in findings]
    meta = {
        "filesScanned": scan.files_scanned,
        "events": len(scan.events),
        "rulesAvailable": rules_ok,
        "adapters": scan.adapter_status,
        "coverageNotes": scan.coverage_notes,
        # A clean follow-up scan must not erase evidence of a prior broken
        # chain. Clearing this requires a future explicit recovery workflow.
        "priorChainOk": chain_ok and (
            previous_payload is None
            or previous_payload.get("meta", {}).get("priorChainOk", True)
        ),
        "overflow": carried_overflow,
        # Reserved until process evidence exists (C2-r2/C3-r2).
        "unsupportedSurfaces": ["S10 unsanctioned spawn", "S11 bypass launch"],
        "interim": "writable-checkout build; not a tamper-proof control plane",
    }
    unique_ids = {body["id"] for body in bodies}
    records = build_records(bodies, overflow=carried_overflow)
    # Overflow counts only records dropped by the size cap (C108); identical
    # duplicates collapsed by id are not lost alarms.
    meta["overflow"] = carried_overflow + max(0, len(unique_ids) - len(records))
    payload = write_alarms(artifact_path, records, meta)
    notified = notify_new_records(state_dir(), payload["records"])

    counts = Counter(r["rule"] for r in payload["records"])
    print(f"sentinel --once: files={scan.files_scanned} events={len(scan.events)}")
    print(f"adapters: {', '.join(f'{k}={v}' for k, v in sorted(scan.adapter_status.items()))}")
    if not rules_ok:
        print("RULES UNAVAILABLE — evaluation skipped (fail closed, S8 raised)", file=sys.stderr)
    for note in scan.coverage_notes:
        print(f"coverage: {note}", file=sys.stderr)
    if counts:
        print("alarms: " + ", ".join(f"{rule}={count}" for rule, count in sorted(counts.items())))
    print(
        f"artifact: {alarm_artifact_path()} records={len(payload['records'])} "
        f"overflow={payload['meta']['overflow']} notified={notified}"
    )
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="ocdeck.sentinel")
    parser.add_argument("--once", action="store_true", default=True, help="scan once and exit (default)")
    parser.add_argument("--rules", type=Path, default=None, help="explicit rules file path")
    args = parser.parse_args(argv)
    return once(rules_path=args.rules)


if __name__ == "__main__":
    raise SystemExit(main())
