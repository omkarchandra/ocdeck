"""Let OC Deck answer a Claude Code permission prompt ("allow once").

Claude's transcript never records a pending permission, so OC Deck launches its
Claude sessions with a ``PermissionRequest`` hook (``--settings``). The hook, run
as ``python -m ocdeck.claude_permissions``, does three things:

1. writes ``<session>__<request>.json`` into the state directory (what is being
   asked, plus its own pid), so the deck can show PERM;
2. waits a bounded time for ``<session>__<request>.decision`` to appear;
3. prints Claude's allow decision when the deck wrote ``allow``. On timeout or any
   error it prints nothing, and Claude shows its normal prompt in the terminal.

Claude does not stop the hook when the owner answers in the terminal, so the same module
also runs as ``python -m ocdeck.claude_permissions clear`` from the PostToolUse,
PostToolUseFailure, PermissionDenied and Stop hooks: it removes the matching request at
once. (PermissionRequest events carry no tool_use_id, so a request is matched by a
fingerprint of tool name + input.)

The hook only ever allows once, and only when the owner pressed ``y`` in the deck.
It never denies, so it can never block work the terminal prompt would allow.
Standard library only: it runs on every prompt and must start fast.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HOOK_MODULE = "ocdeck.claude_permissions"
DEFAULT_WAIT_SECONDS = 90.0
POLL_SECONDS = 0.25
MAX_FILES = 64
MAX_REQUEST_BYTES = 16 * 1024
SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
SUMMARY_CHARS = 200


def state_dir() -> Path:
    override = os.environ.get("OCDECK_CLAUDE_PERMISSIONS_DIR")
    if override:
        return Path(override).expanduser()
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")).expanduser()
    return base / "ocdeck" / "claude-permissions"


def wait_seconds() -> float:
    try:
        return max(1.0, float(os.environ.get("OCDECK_PERMISSION_WAIT", DEFAULT_WAIT_SECONDS)))
    except ValueError:
        return DEFAULT_WAIT_SECONDS


CLEAR_EVENTS = ("PostToolUse", "PostToolUseFailure", "PermissionDenied", "Stop")


def hook_settings(python: str | None = None) -> dict[str, Any]:
    """The ``--settings`` payload that attaches the hooks to a Claude session."""
    command = f"{python or sys.executable} -m {HOOK_MODULE}"

    def entry(text: str, timeout: int) -> list[dict[str, Any]]:
        return [{"hooks": [{"type": "command", "command": text, "timeout": timeout}]}]

    hooks = {"PermissionRequest": entry(command, int(wait_seconds()) + 10)}
    hooks.update({event: entry(f"{command} clear", 10) for event in CLEAR_EVENTS})
    return {"hooks": hooks}


def fingerprint(tool: str, tool_input: Any) -> str:
    """Identify one tool call across hook events that share no id."""
    text = json.dumps([tool, tool_input], sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:24]


# --- what the deck reads ------------------------------------------------------

@dataclass(frozen=True, slots=True)
class PendingRequest:
    session_id: str
    request_id: str
    summary: str
    tool: str
    pid: int


def _hook_alive(pid: int) -> bool:
    """The waiting hook is still running (a dead one leaves a stale file behind)."""
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    return HOOK_MODULE.encode() in command


def _read_request(path: Path) -> dict[str, Any] | None:
    try:
        if path.stat().st_size > MAX_REQUEST_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def pending_requests(directory: Path | None = None) -> dict[str, PendingRequest]:
    """Live pending requests keyed by Claude session id (the newest one per session)."""
    directory = directory or state_dir()
    found: dict[str, tuple[float, PendingRequest]] = {}
    try:
        names = sorted(name for name in os.listdir(directory) if name.endswith(".json"))[:MAX_FILES]
    except OSError:
        return {}
    for name in names:
        path = directory / name
        data = _read_request(path)
        if data is None:
            continue
        session, request = str(data.get("session", "")), str(data.get("id", ""))
        pid = data.get("pid")
        if not (SAFE_ID.fullmatch(session) and SAFE_ID.fullmatch(request) and isinstance(pid, int)):
            continue
        if name != f"{session}__{request}.json" or not _hook_alive(pid):
            continue
        started = float(data.get("started") or 0)
        current = found.get(session)
        if current is None or started >= current[0]:
            found[session] = (started, PendingRequest(
                session, request, str(data.get("summary", ""))[:SUMMARY_CHARS],
                str(data.get("tool", ""))[:64], pid))
    return {session: item[1] for session, item in found.items()}


def approve(session_id: str, request_id: str, directory: Path | None = None) -> str:
    """Answer one pending request with "allow once". Returns "" or an error message."""
    if not (SAFE_ID.fullmatch(session_id) and SAFE_ID.fullmatch(request_id)):
        return "invalid permission request id"
    directory = directory or state_dir()
    stem = f"{session_id}__{request_id}"
    data = _read_request(directory / f"{stem}.json")
    if data is None or not isinstance(data.get("pid"), int) or not _hook_alive(data["pid"]):
        return "this permission request is no longer waiting (answered in the terminal, or timed out)"
    temporary = directory / f".{stem}.{os.getpid()}.tmp"
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("allow\n")
        os.replace(temporary, directory / f"{stem}.decision")
    except OSError as error:
        return f"could not record the decision: {error.strerror or error}"
    return ""


# --- the hook process ---------------------------------------------------------

def summarize(tool: str, tool_input: Any) -> str:
    """One short line saying what the tool wants, without dumping file contents."""
    if not isinstance(tool_input, dict):
        return tool
    for key in ("command", "file_path", "path", "url", "pattern", "query", "description", "prompt"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return f"{tool} {' '.join(value.split())}"[:SUMMARY_CHARS]
    return f"{tool} {', '.join(sorted(map(str, tool_input)))}"[:SUMMARY_CHARS]


def _write_private(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(temporary, path)


def _allow_output() -> str:
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}})


def run_hook(stdin_text: str, directory: Path | None = None, *, wait: float | None = None) -> str:
    """Return the text to print: Claude's allow decision, or "" to leave the prompt to the terminal."""
    try:
        event = json.loads(stdin_text)
        session = str(event.get("session_id", ""))
        if not isinstance(event, dict) or not SAFE_ID.fullmatch(session):
            return ""
        directory = directory or state_dir()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        request = uuid.uuid4().hex[:12]
        stem = f"{session}__{request}"
        tool = str(event.get("tool_name", ""))
        _write_private(directory / f"{stem}.json", json.dumps({
            "id": request, "session": session, "tool": tool, "pid": os.getpid(),
            "summary": summarize(tool, event.get("tool_input")), "started": time.time(),
            "fingerprint": fingerprint(tool, event.get("tool_input")),
            "cwd": str(event.get("cwd", ""))[:512],
        }))
    except Exception:
        return ""
    decision = directory / f"{stem}.decision"
    request_file = directory / f"{stem}.json"
    allowed = False
    try:
        deadline = time.monotonic() + (wait_seconds() if wait is None else wait)
        while time.monotonic() < deadline:
            if not request_file.exists():
                break  # answered elsewhere; a clear hook removed the request
            if decision.exists():
                try:
                    allowed = decision.read_text(encoding="utf-8").strip() == "allow"
                except OSError:
                    allowed = False
                break
            time.sleep(POLL_SECONDS)
    finally:
        for suffix in (".json", ".decision"):
            try:
                (directory / f"{stem}{suffix}").unlink()
            except OSError:
                pass
    return _allow_output() if allowed else ""


def clear_requests(stdin_text: str, directory: Path | None = None) -> int:
    """Remove the requests an event proves are no longer waiting; returns how many."""
    try:
        event = json.loads(stdin_text)
        session = str(event.get("session_id", ""))
        if not isinstance(event, dict) or not SAFE_ID.fullmatch(session):
            return 0
        directory = directory or state_dir()
        names = [name for name in os.listdir(directory)
                 if name.startswith(f"{session}__") and name.endswith(".json")]
    except Exception:
        return 0
    everything = event.get("hook_event_name") == "Stop"  # the turn ended: nothing can be pending
    wanted = fingerprint(str(event.get("tool_name", "")), event.get("tool_input"))
    removed = 0
    for name in names:
        data = _read_request(directory / name)
        if data is None or everything or data.get("fingerprint") == wanted:
            try:
                (directory / name).unlink()
                removed += 1
            except OSError:
                pass
    return removed


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    text = sys.stdin.read()
    if arguments[:1] == ["clear"]:
        clear_requests(text)
        return 0
    output = run_hook(text)
    if output:
        sys.stdout.write(output + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
