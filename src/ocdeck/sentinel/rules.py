"""Explicit, versioned detection criteria (S1/S3/S4/S7/S8) per C104/C105/C111-r2.

A rule fires only when its full criteria match; otherwise nothing happens.
Transcript evidence is *intent*: every finding is outcome="attempted", and
the S4 composite is "suspected", never confirmed exfiltration (C105).
A rules file that is missing, unreadable, or invalid stops evaluation and
raises S8 (fail closed, mirroring the backend selector discipline).
"""
from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

RULES_SCHEMA = 1

# S1 — sensitive path classes (C104: protected data stays protected even
# inside project roots). Matched against expanded absolute paths.
SENSITIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ssh-keys", re.compile(r"/\.ssh/|/id_(?:rsa|ed25519|ecdsa)[^/]*$")),
    ("auth-store", re.compile(r"/auth\.json$|/credentials\.json$|/guard\.key$")),
    ("server-env", re.compile(r"/server\.env$")),
    ("env-secrets", re.compile(r"/\.env(?:\.(?:local|production|development|dev|prod))?$")),
    ("private-keys", re.compile(r"\.pem$|\.key$|\.p12$")),
    ("browser-cookies", re.compile(r"/Cookies$|/Login Data$")),
)

# S3 — tools and commands treated as egress-capable (C104 widened list).
EGRESS_TOOLS = {"WebFetch", "webfetch", "websearch", "signed_in_tabs_browser_navigate"}
EGRESS_COMMAND = re.compile(
    r"\b(?:curl|wget|rsync|scp|sftp|rclone|twine|gh\s+(?:gist|release))\b"
)
ORIGIN_PATTERN = re.compile(r"https?://[A-Za-z0-9.\-]+(?::\d+)?")

# S7 — cross-session interference and defense evasion (C111-r2 + tamper surface).
# The sentinel sees transcript text, never the executing environment, so a
# private-server kill is only downgraded (to MEDIUM, never suppressed) when the
# socket choice is bound to that same tmux statement.
TMUX_KILL = re.compile(
    r"\btmux\b[^|;&\n]*\b(?:kill-server|kill-session|kill-pane|kill-window)\b"
)
# C111-r2 item 2 reverses C111's decided CRITICAL text; until the council
# records that amendment, private-server kills keep CRITICAL (labelled private).
PRIVATE_KILL_SEVERITY = "CRITICAL"
TMUX_SOCKET_FLAG = re.compile(r"(?:^|\s)-([LS])\s*(\S+)")
TMUX_ENV_UNSET = re.compile(
    r"(?:^|\s)env\s+(?:\S+\s+)*?(?:-u\s*TMUX|--unset(?:=|\s+)TMUX)(?:\s|$)"
    r"|(?:^|\s)TMUX=(?:''|\"\")?(?:\s|$)"
)
SHELL_PROGRAMS = {"bash", "sh", "zsh", "dash", "ksh"}
HEREDOC_START = re.compile(r"(?<!<)<<(?!<)(-?)[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
STATEMENT_SPLIT = re.compile(r"\n|;|&&|\|\||\||(?<![>&0-9])&(?![>&])")
COMMAND_WRAPPERS = {"sudo", "env", "nohup", "exec", "command", "time", "nice", "stdbuf", "timeout", "setsid"}
WRAPPER_VALUE_OPTIONS = {"-u", "--unset", "-n", "-s", "-k", "--signal", "--kill-after", "-o", "-e"}
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Sentinel state/config (tamper = CRITICAL) versus its source tree (MEDIUM).
SENTINEL_STATE = re.compile(
    r"sentinel-rules\.json|sentinel-alarms\.json|sentinel-acks\.jsonl"
    r"|\.local/state/ocdeck/sentinel(?:/|$)"
)
SENTINEL_SOURCE = re.compile(r"ocdeck/sentinel/")
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "write", "edit", "multiedit",
               "patch", "apply_patch"}
PATCH_FILE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", re.MULTILINE)
REDIRECT_TARGET = re.compile(r"(?:^|[^<&0-9])\d*>{1,2}\|?\s*([^\s;&|<>]+)")
WRITE_ALL_OPERANDS = {"rm", "unlink", "truncate", "chmod", "chown", "touch", "shred", "mv", "tee", "sed"}
WRITE_LAST_OPERAND = {"cp", "install", "ln", "rsync"}

# S1 — commands that only inspect metadata, never contents.
METADATA_COMMANDS = {"ls", "stat", "test", "[", "[[", "file", "realpath", "readlink"}
SEVERITY_RANK = {"": -1, "LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}

PATH_INPUT_KEYS = ("file_path", "path", "file", "filePath", "notebook_path", "filename")


class RulesUnavailable(RuntimeError):
    """Raised when the rules file cannot be trusted; caller must emit S8."""


@dataclass(slots=True)
class RuleConfig:
    version: int = RULES_SCHEMA
    allow_origins: set[str] = field(default_factory=set)

    @classmethod
    def load(cls, path: Path) -> "RuleConfig":
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as error:
            raise RulesUnavailable(f"rules file unreadable: {error}") from error
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
                raise RulesUnavailable("rules file must be an owner-only regular file")
            if metadata.st_size > 64 * 1024:
                raise RulesUnavailable("rules file exceeds 64 KiB")
            with os.fdopen(descriptor, "rb") as handle:
                payload = json.loads(handle.read(64 * 1024 + 1))
        except (OSError, ValueError) as error:
            raise RulesUnavailable(f"rules file invalid: {error}") from error
        if not isinstance(payload, dict) or payload.get("version") != RULES_SCHEMA:
            raise RulesUnavailable("rules file schema mismatch")
        origins = payload.get("allow_origins")
        if not isinstance(origins, list) or not all(isinstance(o, str) for o in origins):
            raise RulesUnavailable("allow_origins must be a list of strings")
        return cls(version=payload["version"], allow_origins={o.rstrip("/") for o in origins})


@dataclass(slots=True)
class RuleFinding:
    rule: str
    severity: str
    harness: str
    session_id: str
    cwd: str
    summary: str
    outcome: str = "attempted"
    criteria: dict = field(default_factory=dict)


def _candidate_paths(event) -> list[tuple[str, bool]]:
    """(path, metadata_only) pairs: tool path inputs count as content access."""
    paths: list[tuple[str, bool]] = []
    for key in PATH_INPUT_KEYS:
        value = event.input.get(key)
        if isinstance(value, str):
            paths.append((os.path.expanduser(value), False))
    command = event.input.get("command") or event.input.get("cmd")
    if isinstance(command, str):
        for statement in STATEMENT_SPLIT.split(command):
            metadata = _command_word(statement) in METADATA_COMMANDS
            paths.extend(
                (os.path.expanduser(token), metadata) for token in statement.split()
                if token.startswith(("~", "/")) and "." in token
            )
    return paths


def _command_word(statement: str) -> str:
    """Program a shell statement runs, skipping assignments and exec wrappers."""
    tokens = re.sub(r"^[\s({!`]+|\$\(", " ", statement).split()
    index = 0
    while index < len(tokens):
        token = tokens[index].strip("'\"")
        base = os.path.basename(token)
        if ASSIGNMENT.match(token):
            index += 1
        elif base in COMMAND_WRAPPERS:
            index += 1
            while index < len(tokens) and (
                tokens[index].startswith("-") or ASSIGNMENT.match(tokens[index])
                or re.fullmatch(r"\d+(?:\.\d+)?[smhd]?", tokens[index])
            ):
                if tokens[index] in WRAPPER_VALUE_OPTIONS:
                    index += 1
                index += 1
        else:
            return base
    return ""


def _quoted_at(prefix: str) -> bool:
    """Whether the end of `prefix` sits inside a shell quote."""
    single = double = escaped = False
    for char in prefix:
        if escaped:
            escaped = False
        elif char == "\\" and not single:
            escaped = True
        elif char == "'" and not double:
            single = not single
        elif char == '"' and not single:
            double = not double
    return single or double


def _split_heredocs(text: str) -> tuple[str, list[tuple[bool, str]]]:
    """Text outside heredoc bodies, plus (fed_to_a_shell, body) per heredoc."""
    lines = text.split("\n")
    outer: list[str] = []
    bodies: list[tuple[bool, str]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        outer.append(line)
        index += 1
        for match in HEREDOC_START.finditer(line):
            if _quoted_at(line[:match.start()]):
                continue
            strip_tabs, tag = match.group(1) == "-", match.group(3)
            consumer = _command_word(STATEMENT_SPLIT.split(line[:match.start()])[-1])
            body: list[str] = []
            while index < len(lines):
                candidate = lines[index].lstrip("\t") if strip_tabs else lines[index]
                index += 1
                if candidate.rstrip() == tag:
                    break
                body.append(lines[index - 1])
            bodies.append((consumer in SHELL_PROGRAMS, "\n".join(body)))
    return "\n".join(outer), bodies


def _private_tmux(prefix: str, invocation: str) -> bool:
    """A private server bound to this very statement: -L/-S non-default, or env-cleared $TMUX."""
    for kind, value in TMUX_SOCKET_FLAG.findall(invocation[len("tmux"):]):
        value = value.strip("'\"")
        name = value if kind == "L" else os.path.basename(value.rstrip("/"))
        if name and name != "default":
            return True
    return bool(TMUX_ENV_UNSET.search(prefix)) and "TMUX_TMPDIR=" in prefix


def _tmux_kill_severity(text: str) -> str:
    worst = ""
    for statement in STATEMENT_SPLIT.split(text):
        match = TMUX_KILL.search(statement)
        if not match:
            continue
        if not _private_tmux(statement[:match.start()], match.group(0)):
            return "CRITICAL"
        worst = "MEDIUM"
    return worst


def _s7_kill(command: str) -> tuple[str, str]:
    """(severity, socket) for tmux kills: shared → CRITICAL; private or inert text → MEDIUM."""
    outer, bodies = _split_heredocs(command)
    severities = [_tmux_kill_severity(outer)]
    inert_text = False
    for fed_to_shell, body in bodies:
        if fed_to_shell:
            severities.append(_tmux_kill_severity(body))
        elif TMUX_KILL.search(body):
            inert_text = True
    if "CRITICAL" in severities:
        return "CRITICAL", "shared"
    if "MEDIUM" in severities:
        return PRIVATE_KILL_SEVERITY, "private"
    return ("MEDIUM", "text") if inert_text else ("", "")


def _shell_write_targets(text: str) -> list[str]:
    targets = REDIRECT_TARGET.findall(text)
    for statement in STATEMENT_SPLIT.split(text):
        word = _command_word(statement)
        tokens = statement.split()
        start = next((i + 1 for i, token in enumerate(tokens)
                      if os.path.basename(token.strip("'\"")) == word), len(tokens))
        arguments = tokens[start:]
        operands = [token.strip("'\"") for token in arguments if not token.startswith("-")]
        if word == "sed" and not any(a == "--in-place" or re.match(r"-[A-Za-z]*i", a) for a in arguments):
            continue
        if word in WRITE_ALL_OPERANDS:
            targets.extend(operands)
        elif word in WRITE_LAST_OPERAND and operands:
            targets.append(operands[-1])
        elif word == "dd":
            targets.extend(token[3:] for token in arguments if token.startswith("of="))
    return targets


def _s7_surface(event, command: str) -> str:
    """CRITICAL for writes to sentinel state/config, MEDIUM for its source; reads raise nothing."""
    targets: list[str] = []
    if event.tool in WRITE_TOOLS:
        targets.extend(value for key in PATH_INPUT_KEYS
                       if isinstance(value := event.input.get(key), str))
        for key in ("patch", "patchText", "input"):
            value = event.input.get(key)
            if isinstance(value, str):
                targets.extend(PATCH_FILE.findall(value))
    if command:
        outer, bodies = _split_heredocs(command)
        for text in (outer, *(body for fed_to_shell, body in bodies if fed_to_shell)):
            targets.extend(_shell_write_targets(text))
    severity = ""
    for target in targets:
        if SENTINEL_STATE.search(target):
            return "CRITICAL"
        if SENTINEL_SOURCE.search(target):
            severity = "MEDIUM"
    return severity


def _candidate_urls(event) -> list[str]:
    urls: list[str] = []
    for value in event.input.values():
        if isinstance(value, str):
            urls.extend(ORIGIN_PATTERN.findall(value))
    return urls


def _matches_sensitive(path: str) -> str | None:
    for label, pattern in SENSITIVE_PATTERNS:
        if pattern.search(path):
            if path.endswith((".example", ".sample", ".template")):
                return None
            return label
    return None


def evaluate_events(events, config: RuleConfig) -> list[RuleFinding]:
    findings: list[RuleFinding] = []
    sensitive_sessions: set[str] = set()
    egress_sessions: set[str] = set()

    for event in events:
        # S1 — out-of-permission read of a sensitive path class. Metadata-only
        # listings (ls/stat/test…) are LOW and never feed the S4 composite.
        hits = [(label, metadata) for path, metadata in _candidate_paths(event)
                if (label := _matches_sensitive(path))]
        content_label = next((label for label, metadata in hits if not metadata), None)
        if content_label:
            sensitive_sessions.add(event.session_id)
            findings.append(RuleFinding(
                rule="S1", severity="HIGH", harness=event.harness,
                session_id=event.session_id, cwd=event.cwd,
                summary=f"{event.tool} touched sensitive path class '{content_label}'",
                criteria={"class": content_label, "tool": event.tool, "outcome": "attempted"},
            ))
        elif hits:
            findings.append(RuleFinding(
                rule="S1", severity="LOW", harness=event.harness,
                session_id=event.session_id, cwd=event.cwd,
                summary=f"metadata-only listing of sensitive path class '{hits[0][0]}'",
                criteria={"class": hits[0][0], "tool": event.tool, "access": "metadata",
                          "outcome": "attempted"},
            ))

        # S3 — egress to a non-allowlisted origin.
        is_egress_tool = event.tool in EGRESS_TOOLS
        command = event.input.get("command") or event.input.get("cmd")
        command_egress = isinstance(command, str) and bool(EGRESS_COMMAND.search(command))
        if is_egress_tool or command_egress:
            for origin in _candidate_urls(event):
                if origin.rstrip("/") in config.allow_origins:
                    continue
                egress_sessions.add(event.session_id)
                findings.append(RuleFinding(
                    rule="S3", severity="HIGH", harness=event.harness,
                    session_id=event.session_id, cwd=event.cwd,
                    summary=f"{event.tool} attempted egress to non-allowlisted origin",
                    criteria={"origin": origin, "tool": event.tool},
                ))
                break

        # S7 — cross-session interference / defense evasion (C111).
        # Match only *command* fields: file content that merely mentions a
        # kill command (incident reports, tests) must not fire (C105).
        command_text = command if isinstance(command, str) else ""
        if not command_text:
            for key in ("command", "cmd"):
                value = event.input.get(key)
                if isinstance(value, str):
                    command_text = value
                    break
        kill_severity, socket = _s7_kill(command_text) if command_text else ("", "")
        surface_severity = _s7_surface(event, command_text)
        if kill_severity and SEVERITY_RANK[kill_severity] >= SEVERITY_RANK[surface_severity]:
            findings.append(RuleFinding(
                rule="S7", severity=kill_severity, harness=event.harness,
                session_id=event.session_id, cwd=event.cwd,
                summary={
                    "shared": "tmux kill command attempted from an agent session",
                    "private": "tmux kill on an explicitly private tmux server",
                    "text": "tmux kill text inside a heredoc body",
                }[socket],
                criteria={"surface": "tmux", "socket": socket, "outcome": "attempted"},
            ))
        elif surface_severity:
            state = surface_severity == "CRITICAL"
            findings.append(RuleFinding(
                rule="S7", severity=surface_severity, harness=event.harness,
                session_id=event.session_id, cwd=event.cwd,
                summary="write attempt against sentinel state/config" if state
                else "sentinel source modified (development edit or tamper — review)",
                criteria={"surface": "sentinel", "target": "state" if state else "source",
                          "outcome": "attempted"},
            ))

    # S4 — correlated exfiltration, suspected (C105 labeling).
    for session_id in sensitive_sessions & egress_sessions:
        session_events = [e for e in events if e.session_id == session_id]
        findings.append(RuleFinding(
            rule="S4", severity="CRITICAL", harness=session_events[0].harness if session_events else "",
            session_id=session_id,
            cwd=session_events[0].cwd if session_events else "",
            summary="suspected exfiltration composite: sensitive read + non-allowlisted egress in one session",
            criteria={"basis": "correlated intent evidence", "confidence": "suspected"},
        ))
    return findings


def s8_finding(reason: str) -> RuleFinding:
    return RuleFinding(
        rule="S8", severity="CRITICAL", harness="sentinel", session_id="",
        cwd="", summary=f"sentinel fail-closed: {reason}",
        criteria={"reason": reason},
    )
