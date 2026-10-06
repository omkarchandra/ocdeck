"""Literal-text presentation of Sentinel's bounded, read-only artifact."""
from __future__ import annotations

import re

from rich.text import Text

from .harnesses import HARNESS_LABELS, split_session_key
from .models import ProjectRecord, SessionRecord, sanitize_terminal_text
from .sentinel.health import SentinelReport

SEVERITY_STYLES = {"CRITICAL": "bold #ff6b7a", "HIGH": "#ffa657", "MEDIUM": "#f2b84b"}
ORIGIN_PATTERN = re.compile(r"https?://[A-Za-z0-9.\-]+(?::\d+)?")


def alarm_origin(record: dict) -> str:
    """Validated S3 egress origin; '' when it is not displayable."""
    criteria = record.get("criteria")
    if not isinstance(criteria, dict) or record.get("rule") != "S3":
        return ""
    origin = criteria.get("origin")
    if not isinstance(origin, str) or not ORIGIN_PATTERN.fullmatch(origin):
        return ""
    return origin


def alarm_destination(record: dict) -> str:
    """S3 summary destination, e.g. ``webfetch → https://opencode.ai``."""
    criteria = record.get("criteria")
    if not isinstance(criteria, dict):
        return ""
    tool = criteria.get("tool")
    if not isinstance(tool, str) or not tool:
        return ""
    origin = alarm_origin(record)
    return f"{tool} → {origin}" if origin else ""


def alarm_session(record: dict, sessions: dict[str, SessionRecord]) -> SessionRecord | None:
    """Only join exact harness/native-id/directory identities, never a title or prefix."""
    for session in sessions.values():
        harness, native = split_session_key(session.id)
        if (harness == record["harness"] and native == record["sessionId"]
                and session.directory == record["projectPath"]):
            return session
    return None


def alarm_project(record: dict, projects: dict[str, ProjectRecord]) -> str:
    return next((project.name for project in projects.values()
                 if project.directory == record["projectPath"]), "Unassigned")


def health_text(report: SentinelReport, *, details: bool = False) -> Text:
    health = report.health
    tone = "#5eead4" if health.status == "OBSERVED" else "#f2b84b"
    content = Text(health.label, style=f"bold {tone}")
    content.append(f"  ALARMS({health.alarm_count})", style="bold #ff6b7a" if health.alarm_count else "")
    if health.critical_count:
        content.append(f" · {health.critical_count} CRITICAL", style="bold #ff6b7a")
    if health.overflow:
        content.append(f" · {health.overflow} omitted", style="#ffa657")
    if health.acked_count:
        content.append(f" · {health.acked_count} dismissed", style="dim")
    if details:
        content.append(
            "\nObservation pilot · read-only · Enter locates a known session · Shift+D dismisses",
            style="dim",
        )
        for reason in health.reasons:
            content.append(f"\n{reason}", style="#f2b84b")
        content.append("\nA reported scan is not proof of enforcement or authenticated provenance.", style="dim")
    else:
        content.append("  · press 6 for detail", style="dim")
    return content


def alarm_cells(record: dict, sessions: dict[str, SessionRecord], projects: dict[str, ProjectRecord],
                *, private: bool, dismissed: bool = False) -> tuple[Text, ...]:
    session = alarm_session(record, sessions)
    values = (
        record["severity"] if record["severity"] in {"CRITICAL", "HIGH", "MEDIUM", "LOW", "WARN"} else "UNKNOWN",
        record["rule"] if re.fullmatch(r"[CS][0-9]{1,3}", record["rule"]) else "?",
        "[hidden]" if private else HARNESS_LABELS.get(record["harness"], record["harness"]),
        "[hidden]" if private else session.title if session else record["sessionId"] or "Unknown session",
        "[hidden]" if private else alarm_project(record, projects),
        "[hidden]" if private else (alarm_destination(record) or record["summary"]),
    )
    cells = tuple(Text(sanitize_terminal_text(value)) for value in values)
    cells[0].stylize(SEVERITY_STYLES.get(record["severity"], "#8ba4b5"))
    if dismissed:
        # Receipt only (C106/C108): a dismissed row stays listed, dimmed.
        for cell in cells:
            cell.stylize("dim")
    return cells


def alarm_detail(record: dict | None, *, private: bool, acked: bool = False) -> Text:
    if record is None:
        return Text("No displayable alarm records. Check Sentinel health above.", style="dim")
    if private:
        return Text("Privacy mode: alarm details hidden", style="dim")
    rows = [
        ("", f'{record["severity"]} · {record["rule"]} · {record["firedAt"]}'),
        ("", record["summary"]),
        ("Session", f'{record["harness"]}:{record["sessionId"]}'),
        ("Project", record["projectPath"] or "Unassigned"),
        ("Evidence", f'{record["outcome"]} · {record["origin"]}'),
    ]
    origin = alarm_origin(record)
    if origin:
        rows.append(("Origin", origin))
    if acked:
        rows.append(("Status", "dismissed"))
    content = Text()
    for label, value in rows:
        if content:
            content.append("\n")
        content.append((label + ": ") if label else "", style="dim")
        content.append(sanitize_terminal_text(value)[:2000])
    return content
