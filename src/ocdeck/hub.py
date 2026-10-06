"""One shared hub: identical instructions and MCP servers for every harness.

OC Deck keeps OpenCode, Claude Code and Codex in step. The hub owns a single
``hub.json`` (the path of the shared instructions file plus the MCP servers
every harness should expose) and ``ocdeck-hub sync`` writes that state into
the configuration of the *enabled* harnesses only, so OC Deck keeps working
with any subset of them. :func:`write_handoff` captures a session (prompts, last
reply, shared project memory, git state) so the work can continue in another
harness.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass, field, replace
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Callable, Sequence

from .harnesses import (
    harness_ids,
    HARNESS_LABELS,
    SETTING_VALUES,
    ClaudeHarness,
    CodexHarness,
    find_binary,
    load_harness_settings,
    model_name,
    resolve_enabled_harnesses,
    save_harness_settings,
    split_session_key,
)
from .models import SessionRecord, clean_string

# Injectable so tests (and the app) can observe or fake subprocess calls.
RUNNER = subprocess.run
CLI_TIMEOUT = 30
GIT_TIMEOUT = 20
HUB_FILE = "hub.json"
INDEX_SERVER = "ocdeck-index"
CLAUDE_BEGIN = "<!-- ocdeck-hub:begin -->"
CLAUDE_END = "<!-- ocdeck-hub:end -->"
CODEX_BEGIN = "# ocdeck-hub:begin"
CODEX_END = "# ocdeck-hub:end"
MANUAL = "MANUAL:"
BACKUP_SUFFIX = ".ocdeck-bak"
MAX_PROMPTS = 5
MAX_OPENCODE_PROMPTS = 8
MAX_TRANSCRIPT_SCAN = 64
MAX_PART_CHARS = 4096
MAX_MESSAGE_PARTS = 32
MAX_REPLY_CHARS = 4000
MAX_MEMORY_ENTRIES = 8
MAX_MEMORY_CHARS = 200
MAX_GIT_LINES = 50
MAX_COMMITS = 5
TAIL_BYTES = 256 * 1024
# Trivial continuation-only requests ("go on") carry nothing worth handing off.
# Matches are exact after strip+casefold, never substrings, so a real sentence
# that merely contains "continue" is always kept.
TRIVIAL_CONTINUATIONS = frozenset(
    {"go on", "carry on", "continue", "keep going", "ok", "okay", "yes", "next",
     "proceed", "go ahead", "do it", ""}
)
SERVER_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
TOML_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
MCP_BLOCK = re.compile(r'"mcp"\s*:\s*\{')
INSTRUCTIONS_SEED = (
    "# Shared instructions\n"
    "\n"
    "One file every harness reads. Keep the working rules here, not in a\n"
    "per-harness note: `ocdeck-hub sync` links or imports it everywhere.\n"
)


def _warn(message: str) -> None:
    print(f"ocdeck-hub: {message}", file=sys.stderr)


# --- paths -------------------------------------------------------------------

def default_hub_dir() -> Path:
    """``$OCDECK_HUB_DIR`` or ``~/.config/agents``."""
    override = os.environ.get("OCDECK_HUB_DIR", "").strip()
    return Path(override).expanduser() if override else Path.home() / ".config" / "agents"


def resolve_hub_dir(override: Path | str | None = None) -> Path:
    return Path(override).expanduser() if override else default_hub_dir()


def hub_config_path(hub: Path) -> Path:
    return hub / HUB_FILE


def opencode_config_path() -> Path:
    override = os.environ.get("OCDECK_OPENCODE_CONFIG", "").strip()
    if override:
        return Path(override).expanduser()
    if opencode_backend() == "v2" and (managed := managed_v2_config()) is not None:
        return managed
    return Path.home() / ".config" / "opencode" / "opencode.jsonc"


def opencode_backend() -> str:
    try:
        from .backend import read_saved_backend

        return read_saved_backend() or "v2"
    except (OSError, ValueError):
        return "v2"


def managed_v2_config() -> Path | None:
    """The protected config the ``opencode2`` client runs with (read-only for us)."""
    client = shutil.which("opencode2")
    if not client:
        return None
    candidate = Path(client).resolve().parent.parent / "config/opencode/opencode.jsonc"
    return candidate if candidate.is_file() else None


def opencode_config_writable() -> tuple[bool, str]:
    """OpenCode V2 runs on a protected deployment config that the hub must never edit."""
    if os.environ.get("OCDECK_OPENCODE_CONFIG", "").strip():
        return True, ""
    if opencode_backend() == "v2":
        where = managed_v2_config()
        return False, (
            f"OpenCode V2 uses the protected config {where}; change it through that deployment"
            if where else "OpenCode V2 uses a protected deployment config; change it there"
        )
    return True, ""


# The agent browser is a signed-in Chrome; it is granted per session (Shift+B in
# OC Deck), so it must never be registered globally for every session.
BROWSER_SERVER_MARKERS = ("playwright-mcp", "agent_browser", "@playwright/mcp", "signed_in_tabs")


def is_browser_server(name: str, server: dict[str, Any]) -> bool:
    if "OPENCODE_AGENT_BROWSER_CONFIG" in (server.get("env") or {}):
        return True
    haystack = [name, *(str(part) for part in server.get("command", []))]
    return any(marker in item for item in haystack for marker in BROWSER_SERVER_MARKERS)


def opencode_agents_path() -> Path:
    return Path.home() / ".config" / "opencode" / "AGENTS.md"


def opencode_session_db_file() -> Path:
    """``$OCDECK_SESSION_DB_FILE``, else the V2 DB, else the V1 one.

    Both backends keep every session in one SQLite DB. V2 is tried first and is
    also the default when neither file exists; a missing DB simply means no
    transcript for an OpenCode-origin handoff.
    """
    override = os.environ.get("OCDECK_SESSION_DB_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    v2 = Path.home() / ".local" / "share" / "opencode-v2" / "opencode.db"
    if v2.is_file():
        return v2
    v1 = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
    return v1 if v1.is_file() else v2


def claude_markdown_path() -> Path:
    return Path.home() / ".claude" / "CLAUDE.md"


def claude_settings_path() -> Path:
    return Path.home() / ".claude.json"


def codex_home() -> Path:
    override = os.environ.get("CODEX_HOME", "").strip()
    return Path(override).expanduser() if override else Path.home() / ".codex"


def codex_markdown_path() -> Path:
    return codex_home() / "AGENTS.md"


def codex_config_path() -> Path:
    return codex_home() / "config.toml"


# --- JSONC -------------------------------------------------------------------

def strip_jsonc(text: str) -> str:
    """Drop ``//`` and ``/* */`` comments and trailing commas, outside strings."""
    text = text.removeprefix("\ufeff")  # editors may save a UTF-8 BOM
    out: list[str] = []
    index = 0
    length = len(text)
    in_string = False
    escaped = False
    while index < length:
        char = text[index]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < length:
            following = text[index + 1]
            if following == "/":
                while index < length and text[index] != "\n":
                    index += 1
                continue
            if following == "*":
                index += 2
                while index + 1 < length and not (text[index] == "*" and text[index + 1] == "/"):
                    index += 1
                index += 2
                continue
        if char == "," and _next_is_close(text, index + 1):
            index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _next_is_close(text: str, start: int) -> bool:
    """Is the next meaningful character (past whitespace and comments) ``}`` or ``]``?"""
    index = start
    length = len(text)
    while index < length:
        char = text[index]
        if char in " \t\r\n":
            index += 1
        elif char == "/" and text[index + 1 : index + 2] == "/":
            while index < length and text[index] != "\n":
                index += 1
        elif char == "/" and text[index + 1 : index + 2] == "*":
            index += 2
            while index + 1 < length and not (text[index] == "*" and text[index + 1] == "/"):
                index += 1
            index += 2
        else:
            return char in "}]"
    return False


def parse_jsonc(text: str) -> Any:
    return json.loads(strip_jsonc(text))


def read_jsonc(path: Path) -> dict[str, Any]:
    try:
        payload = parse_jsonc(path.read_text(encoding="utf-8"))
    except OSError:
        return {}
    return payload if isinstance(payload, dict) else {}


# --- files -------------------------------------------------------------------

def file_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def backup_file(path: Path) -> Path | None:
    """Copy an existing file to ``<file>.ocdeck-bak`` once, before the first edit."""
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if backup.exists() or not path.is_file():
        return None
    try:
        shutil.copy2(path, backup)
    except OSError as error:
        _warn(f"could not back up {path}: {error}")
        return None
    return backup


def atomic_write(path: Path, text: str, *, mode: int | None = None) -> None:
    """Write ``text`` through a temp file + ``os.replace``, keeping the file mode.

    A symlink (e.g. a dotfiles entry) is kept: the write goes to its target.
    A file the user made read-only is never replaced.
    """
    if path.is_symlink():
        path = path.resolve()
    if path.exists() and not os.access(path, os.W_OK):
        raise PermissionError(f"{path} is read-only; left unchanged")
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_file(path)
    if mode is None:
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            mode = 0o644
    temporary = path.with_name(path.name + ".ocdeck-tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, path)


# --- managed blocks ----------------------------------------------------------

def block_text(begin: str, end: str, body: str) -> str:
    # A body line identical to a marker would end the block early on the next
    # read; indent it so markers only ever appear as the block's own lines.
    lines = [f" {line}" if line.strip() in {begin, end} else line for line in body.rstrip().split("\n")]
    return f"{begin}\n" + "\n".join(lines) + f"\n{end}\n"


def _marker_lines(text: str, marker: str) -> list[tuple[int, int]]:
    """(start, end) offsets of lines consisting of exactly ``marker``."""
    found, offset = [], 0
    for line in text.splitlines(keepends=True):
        if line.strip() == marker:
            found.append((offset, offset + len(line)))
        offset += len(line)
    return found


def block_bounds(text: str, begin: str, end: str) -> tuple[int, int] | None:
    begins = _marker_lines(text, begin)
    if not begins:
        return None
    start = begins[0][0]
    stops = [stop for first, stop in _marker_lines(text, end) if first > start]
    return (start, stops[0]) if stops else None


def _drop_orphan_begin(text: str, begin: str, end: str) -> str:
    """Remove a begin marker whose end marker is missing, keeping the text after it."""
    begins = _marker_lines(text, begin)
    if begins and block_bounds(text, begin, end) is None:
        start, stop = begins[0]
        return text[:start] + text[stop:]
    return text


def upsert_block(text: str, begin: str, end: str, body: str) -> str:
    """Replace the managed block in place, or append it, keeping everything else."""
    block = block_text(begin, end, body)
    text = _drop_orphan_begin(text, begin, end)
    bounds = block_bounds(text, begin, end)
    if bounds is not None:
        start, stop = bounds
        return text[:start] + block + text[stop:]
    if text and not text.endswith("\n"):
        text += "\n"
    return text + block if not text or text.endswith("\n\n") else text + "\n" + block


def remove_block(text: str, begin: str, end: str) -> str:
    bounds = block_bounds(text, begin, end)
    if bounds is None:
        return text
    start, stop = bounds
    return text[:start] + text[stop:]


# --- hub config --------------------------------------------------------------

@dataclass(slots=True)
class HubConfig:
    instructions: str = ""
    mcp: dict[str, dict[str, Any]] = field(default_factory=dict)


def _normalize_server(entry: Any, *, environment_key: str = "env") -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    command = entry.get("command")
    if not isinstance(command, list) or not command:
        return None
    environment = entry.get(environment_key)
    if not isinstance(environment, dict):
        environment = {}
    return {
        "command": [str(part) for part in command],
        "env": {str(key): str(value) for key, value in environment.items()},
    }


def load_hub_config(hub: Path | str | None = None) -> HubConfig:
    """Read ``hub.json``; a missing or broken file falls back to the hub defaults."""
    hub = resolve_hub_dir(hub)
    fallback = str(hub / "AGENTS.md")
    try:
        payload = json.loads(hub_config_path(hub).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return HubConfig(instructions=fallback)
    if not isinstance(payload, dict):
        return HubConfig(instructions=fallback)
    instructions = payload.get("instructions")
    servers = payload.get("mcp")
    mcp: dict[str, dict[str, Any]] = {}
    if isinstance(servers, dict):
        for name, entry in servers.items():
            if isinstance(entry, dict) and entry.get("enabled") is False:
                continue
            if (server := _normalize_server(entry)) is not None:
                mcp[str(name)] = server
    return HubConfig(
        instructions=str(instructions).strip() if isinstance(instructions, str) and str(instructions).strip() else fallback,
        mcp=mcp,
    )


def save_hub_config(config: HubConfig, hub: Path | str | None = None) -> Path:
    hub = resolve_hub_dir(hub)
    hub.mkdir(parents=True, exist_ok=True)
    try:
        hub.chmod(0o700)
    except OSError:
        pass
    path = hub_config_path(hub)
    payload = {
        "instructions": config.instructions,
        "mcp": {
            name: {"command": server.get("command", []), "env": server.get("env", {})}
            for name, server in config.mcp.items()
        },
    }
    atomic_write(path, json.dumps(payload, indent=2) + "\n", mode=0o600)
    return path


def active_servers(config: HubConfig) -> dict[str, dict[str, Any]]:
    """Hub servers in a stable order, minus the explicitly disabled ones."""
    return {
        name: server
        for name, server in sorted(config.mcp.items())
        if not (isinstance(server, dict) and server.get("enabled") is False)
        and not is_browser_server(name, server)
    }


def import_opencode_servers(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Local MCP servers from the OpenCode config, in the hub's own shape."""
    path = path or opencode_config_path()
    try:
        payload = read_jsonc(path)
    except ValueError as error:
        _warn(f"could not parse {path}: {error}")
        payload = {}
    entries = payload.get("mcp") if isinstance(payload, dict) else None
    imported: dict[str, dict[str, Any]] = {}
    if isinstance(entries, dict):
        for name, entry in entries.items():
            if not isinstance(entry, dict) or entry.get("type") != "local":
                continue
            if entry.get("enabled") is False:
                continue
            if (server := _normalize_server(entry, environment_key="environment")) is not None:
                if is_browser_server(str(name), server):
                    continue  # per-session only; see is_browser_server
                imported[str(name)] = server
    imported[INDEX_SERVER] = {"command": [sys.executable, "-m", "ocdeck.index_server"], "env": {}}
    return imported


def default_instructions_path(hub: Path) -> Path:
    """The target of ``~/.config/opencode/AGENTS.md`` when it exists, else the hub copy."""
    shared = opencode_agents_path()
    if shared.exists():
        return shared.resolve()
    return hub / "AGENTS.md"


def initialize_hub(hub: Path | str | None = None) -> tuple[Path, bool]:
    """Create ``hub.json`` and the shared instructions file; never overwrite."""
    hub = resolve_hub_dir(hub)
    path = hub_config_path(hub)
    if path.exists():
        return path, False
    hub.mkdir(parents=True, exist_ok=True)
    try:
        hub.chmod(0o700)
    except OSError:
        pass
    instructions = default_instructions_path(hub)
    if not instructions.exists():
        instructions.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(instructions, INSTRUCTIONS_SEED, mode=0o644)
    save_hub_config(HubConfig(instructions=str(instructions), mcp=import_opencode_servers()), hub)
    return path, True


# --- sync plan ---------------------------------------------------------------

@dataclass(slots=True)
class Action:
    harness: str
    target_path: str
    description: str
    apply_fn: Callable[[], None]

    @property
    def manual(self) -> bool:
        return self.description.startswith(MANUAL)


def plan_actions(hub: Path | str | None = None) -> list[Action]:
    """What ``sync`` would do, without doing anything."""
    config = load_hub_config(hub)
    plan: list[Action] = []
    for harness in resolve_enabled_harnesses(load_harness_settings()):
        planner = SYNC_PLANNERS.get(harness)
        if planner is None:
            _warn(f"{HARNESS_LABELS.get(harness, harness)}: no config sync available; skipped")
            continue
        plan.extend(planner(config))
    return plan


def action_kind(action: Action) -> str:
    """"mcp" for MCP server registration, "instructions" for shared-instruction files."""
    return "mcp" if "MCP server" in action.description else "instructions"


def format_plan(plan: Sequence[Action]) -> list[str]:
    if not plan:
        return ["in sync — nothing to do"]
    return [
        f"  {action.harness:<9} {action.target_path}  {action.description}"
        for action in plan
    ]


# --- Claude Code -------------------------------------------------------------

def read_claude_servers(path: Path | None = None) -> dict[str, Any]:
    """``~/.claude.json`` -> ``mcpServers``; read only, a missing file means {}."""
    path = path or claude_settings_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    servers = payload.get("mcpServers") if isinstance(payload, dict) else None
    return servers if isinstance(servers, dict) else {}


def _run_cli(argv: list[str]) -> subprocess.CompletedProcess:
    return RUNNER(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=CLI_TIMEOUT,
        check=False,
    )


def add_claude_server(name: str, payload: dict[str, Any]) -> None:
    """``claude mcp add-json --scope user <name> <json>`` (Claude writes its own config)."""
    argv = [find_binary("claude") or "claude", "mcp", "add-json", "--scope", "user", name, json.dumps(payload)]
    try:
        result = _run_cli(argv)
    except (OSError, subprocess.TimeoutExpired) as error:
        _warn(f"claude mcp add-json {name} failed: {error}")
        return
    if getattr(result, "returncode", 0) != 0:
        _warn(f"claude mcp add-json {name} exited {getattr(result, 'returncode', '?')}")


def claude_actions(config: HubConfig) -> list[Action]:
    actions: list[Action] = []
    path = claude_markdown_path()
    body = f"@{config.instructions}\n"
    text = file_text(path)
    if upsert_block(text, CLAUDE_BEGIN, CLAUDE_END, body) != text:
        actions.append(
            Action(
                "claude",
                str(path),
                "managed instructions block (@import)",
                partial(_write_block, path, CLAUDE_BEGIN, CLAUDE_END, body),
            )
        )
    installed = read_claude_servers()
    for name, server in active_servers(config).items():
        if name in installed:
            continue
        argv = [str(part) for part in server.get("command", [])]
        payload = {
            "type": "stdio",
            "command": argv[0],
            "args": argv[1:],
            "env": dict(server.get("env", {})),
        }
        actions.append(
            Action(
                "claude",
                str(claude_settings_path()),
                f'register MCP server "{name}"',
                partial(add_claude_server, name, payload),
            )
        )
    return actions


def _write_block(path: Path, begin: str, end: str, body: str) -> None:
    atomic_write(path, upsert_block(file_text(path), begin, end, body))


# --- Codex -------------------------------------------------------------------

def toml_key(key: str) -> str:
    return key if TOML_KEY.match(key) else json.dumps(key, ensure_ascii=False)


def codex_declared_servers(text: str) -> set[str]:
    """``[mcp_servers.<name>]`` tables found in ``text``."""
    names = set()
    for match in re.finditer(r'^\s*\[mcp_servers\.("(?:[^"\\]|\\.)*"|[^\].\s]+)\s*\]', text, re.MULTILINE):
        name = match.group(1)
        if name.startswith('"'):
            try:
                name = json.loads(name)
            except ValueError:
                continue
        names.add(name)
    return names


def codex_server_block(name: str, server: dict[str, Any]) -> str:
    argv = [str(part) for part in server.get("command", [])]
    if not argv:
        return ""
    lines = [f"command = {json.dumps(argv[0], ensure_ascii=False)}"]
    if len(argv) > 1:
        lines.append(f"args = {json.dumps(argv[1:], ensure_ascii=False)}")
    body = "\n".join(lines)
    environment = server.get("env") or {}
    if isinstance(environment, dict) and environment:
        pairs = "\n".join(
            f"{toml_key(str(key))} = {json.dumps(str(value), ensure_ascii=False)}"
            for key, value in environment.items()
        )
        body += f"\n\n[mcp_servers.{name}.env]\n{pairs}"
    return f"[mcp_servers.{name}]\n{body}"


def codex_block(config: HubConfig, skip: set[str] | None = None) -> tuple[str, list[str]]:
    """The managed TOML body for the hub servers that are not defined elsewhere."""
    skip = skip or set()
    warnings: list[str] = []
    chunks: list[str] = []
    for name, server in active_servers(config).items():
        if not SERVER_NAME.match(name):
            warnings.append(f'codex: skipping MCP server "{name}" (unsupported characters in the name)')
            continue
        if name in skip:
            continue
        if block := codex_server_block(name, server):
            chunks.append(block)
    return "\n\n".join(chunks), warnings


def _codex_instructions(instructions: str) -> str:
    """Codex has no ``@import``, so the shared text is inlined verbatim."""
    text = file_text(Path(instructions))
    return text if text.strip() else f"(shared instructions missing: {instructions})\n"


def codex_actions(config: HubConfig) -> list[Action]:
    actions: list[Action] = []
    markdown = codex_markdown_path()
    text = file_text(markdown)
    if upsert_block(text, CODEX_BEGIN, CODEX_END, _codex_instructions(config.instructions)) != text:
        actions.append(
            Action(
                "codex",
                str(markdown),
                "managed instructions block (inlined text)",
                partial(_write_block, markdown, CODEX_BEGIN, CODEX_END, _codex_instructions(config.instructions)),
            )
        )
    toml = codex_config_path()
    text = file_text(toml)
    block, warnings = codex_block(config, codex_declared_servers(remove_block(text, CODEX_BEGIN, CODEX_END)))
    for warning in warnings:
        _warn(warning)
    try:
        changed = place_codex_block(text, block) != text
    except ValueError as error:
        actions.append(Action("codex", str(toml), f"{MANUAL} {error}", lambda: None))
        return actions
    if changed:
        actions.append(
            Action(
                "codex",
                str(toml),
                "managed MCP server tables" if block else "managed MCP server tables (empty)",
                partial(_write_codex_block, toml, block),
            )
        )
    return actions


def _top_level_keys(lines: Sequence[str]) -> set[str]:
    keys = set()
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            break
        if "=" in stripped and not stripped.startswith("#"):
            keys.add(stripped.split("=", 1)[0].strip().strip('"'))
    return keys


def _first_table_offset(text: str) -> int:
    offset = 0
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("["):
            return offset
        offset += len(line)
    return len(text)


def place_codex_block(text: str, body: str) -> str:
    """Put the managed tables before the first user table.

    In TOML every key below a table header belongs to that table, so a
    top-level setting written after the block would silently become part of the
    last managed table. Such keys are moved back above the block; if that would
    duplicate an existing setting the sync refuses instead of guessing.
    """
    text = _drop_orphan_begin(text, CODEX_BEGIN, CODEX_END)
    bounds = block_bounds(text, CODEX_BEGIN, CODEX_END)
    before, after = (text[: bounds[0]], text[bounds[1]:]) if bounds else (text, "")
    after_lines = after.splitlines(keepends=True)
    split = next((i for i, line in enumerate(after_lines) if line.lstrip().startswith("[")), len(after_lines))
    stray, rest = after_lines[:split], "".join(after_lines[split:])
    stray_keys = _top_level_keys(stray)
    if stray_keys:
        duplicated = stray_keys & _top_level_keys(before.splitlines(keepends=True))
        if duplicated:
            raise ValueError(
                "top-level settings below the ocdeck-hub block in config.toml repeat existing ones "
                f"({', '.join(sorted(duplicated))}); move or remove them by hand"
            )
        cut = _first_table_offset(before)
        moved = "".join(stray).strip("\n") + "\n"
        head = before[:cut].rstrip("\n")
        before = (head + "\n" if head else "") + moved + ("\n" if before[cut:] else "") + before[cut:]
    elif "".join(stray).strip():
        rest = "".join(stray) + rest
    remaining = before + rest
    if not body:
        return remaining
    cut = _first_table_offset(remaining)
    head, tail = remaining[:cut], remaining[cut:]
    if head and not head.endswith("\n"):
        head += "\n"
    separator = "\n" if head and not head.endswith("\n\n") else ""
    return head + separator + block_text(CODEX_BEGIN, CODEX_END, body) + ("\n" + tail if tail else "")


def _write_codex_block(path: Path, body: str) -> None:
    atomic_write(path, place_codex_block(file_text(path), body))


# --- OpenCode ----------------------------------------------------------------

def _line_indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def opencode_entry_text(name: str, server: dict[str, Any], indent: str) -> str:
    entry: dict[str, Any] = {"type": "local", "command": [str(part) for part in server.get("command", [])]}
    environment = server.get("env") or {}
    if environment:
        entry["environment"] = {str(key): str(value) for key, value in environment.items()}
    entry["enabled"] = True
    return f"{indent}{json.dumps(name, ensure_ascii=False)}: {json.dumps(entry)},\n"


def top_level_object_start(text: str, key: str) -> tuple[int, int] | None:
    """(key offset, offset just past ``{``) of a root-level ``"key": {`` in JSONC.

    Strings and comments are skipped, and only keys of the root object count,
    so a commented-out or nested ``"mcp"`` is never mistaken for the real one.
    """
    index, depth, length = 0, 0, len(text)
    while index < length:
        char = text[index]
        if char == "/" and text.startswith("//", index):
            index = text.find("\n", index)
            index = length if index < 0 else index
            continue
        if char == "/" and text.startswith("/*", index):
            index = text.find("*/", index + 2)
            index = length if index < 0 else index + 2
            continue
        if char == '"':
            start, index = index, index + 1
            while index < length and text[index] != '"':
                index += 2 if text[index] == "\\" else 1
            index += 1
            if depth == 1:
                try:
                    literal = json.loads(text[start:index])
                except ValueError:
                    literal = None
                probe = index
                while probe < length and text[probe] in " \t\r\n":
                    probe += 1
                if literal == key and text.startswith(":", probe):
                    probe += 1
                    while probe < length and text[probe] in " \t\r\n":
                        probe += 1
                    if text.startswith("{", probe):
                        return start, probe + 1
            continue
        if char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
        index += 1
    return None


def insert_opencode_server(text: str, name: str, server: dict[str, Any]) -> str:
    """Insert one server entry right after the root ``"mcp": {`` opening brace."""
    if not SERVER_NAME.match(name):
        # Same rule as the Codex tables: server names are plain identifiers.
        raise ValueError(f"unsupported MCP server name {name!r}: use letters, digits, '_' or '-'")
    found = top_level_object_start(text, "mcp")
    if found is None:
        raise ValueError(f'no "mcp" object in the OpenCode config: {name} must be added by hand')
    key_start, position = found
    opening_line = text[text.rfind("\n", 0, key_start) + 1 : key_start]
    key_indent = _line_indent(opening_line) + "  "
    rest = text[position:]
    entry = opencode_entry_text(name, server, key_indent)
    if not rest.strip() or rest.strip().startswith("}"):  # an empty "mcp": {} object
        return text[:position] + "\n" + entry.rstrip(",\n") + "\n" + _line_indent(opening_line) + rest.lstrip("\n")
    return text[:position] + "\n" + entry + rest.lstrip("\n")


def _link_instructions(link: Path, target: str) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    temporary = link.with_name(link.name + ".ocdeck-tmp")
    if temporary.is_symlink() or temporary.exists():
        temporary.unlink()
    os.symlink(target, temporary)
    os.replace(temporary, link)


def opencode_actions(config: HubConfig) -> list[Action]:
    actions: list[Action] = []
    writable, reason = opencode_config_writable()
    if not writable:
        return _opencode_manual_actions(config, reason)
    shared = opencode_agents_path()
    if not shared.exists():
        actions.append(
            Action(
                "opencode",
                str(shared),
                f"symlink the shared instructions ({config.instructions})",
                partial(_link_instructions, shared, config.instructions),
            )
        )
    path = opencode_config_path()
    text = file_text(path)
    payload: dict[str, Any] = {}
    broken = ""
    if not text.strip():
        broken = "the OpenCode config is missing" if not path.exists() else "the OpenCode config is empty"
    else:
        try:
            payload = read_jsonc(path)
        except ValueError as error:
            payload = {}
            broken = f"the OpenCode config is not valid JSONC ({error})"
    entries = payload.get("mcp") if isinstance(payload, dict) else None
    existing = entries if isinstance(entries, dict) else {}
    for name, server in active_servers(config).items():
        if name in existing:
            continue
        if broken or top_level_object_start(text, "mcp") is None:
            reason = broken or "it has no \"mcp\" object"
            actions.append(
                Action(
                    "opencode",
                    str(path),
                    f'{MANUAL} add MCP server "{name}" — {reason}',
                    lambda: None,
                )
            )
            continue
        actions.append(
            Action(
                "opencode",
                str(path),
                f'add MCP server "{name}"',
                partial(_add_opencode_server, name, server),
            )
        )
    return actions


def _opencode_manual_actions(config: HubConfig, reason: str) -> list[Action]:
    path = opencode_config_path()
    try:
        payload = read_jsonc(path)
    except ValueError:
        payload = {}
    entries = payload.get("mcp") if isinstance(payload, dict) else None
    existing = entries if isinstance(entries, dict) else {}
    return [
        Action("opencode", str(path), f'{MANUAL} add MCP server "{name}" — {reason}', lambda: None)
        for name in active_servers(config)
        if name not in existing
    ]


def _add_opencode_server(name: str, server: dict[str, Any]) -> None:
    path = opencode_config_path()
    original = file_text(path)
    updated = insert_opencode_server(original, name, server)
    atomic_write(path, updated)
    try:
        read_jsonc(path)
    except ValueError as error:
        atomic_write(path, original)
        raise ValueError(f"{path} stayed invalid JSONC after adding {name}: {error}") from None


# --- handoff -----------------------------------------------------------------

def _safe_name(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "-_." else "-" for char in value).strip(".")
    return cleaned[:60] or "workspace"


def find_transcript(source: str, native_id: str) -> Path | None:
    """The newest on-disk transcript for a native session id, if there is one."""
    if not native_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", native_id):
        return None  # ids are never globbed, so no metacharacter can select another file
    try:
        if source == "claude":
            candidates = [
                path for directory in ClaudeHarness().root.iterdir() if directory.is_dir()
                for path in [directory / f"{native_id}.jsonl"]
            ]
        elif source == "codex":
            candidates = [
                path for path in CodexHarness().root.glob("**/rollout-*.jsonl")
                if path.stem.endswith(f"-{native_id}")
            ]
        elif source == "opencode":
            # The OpenCode backend keeps every session in one SQLite DB, not
            # one file per session; the DB itself is the transcript.
            database = opencode_session_db_file()
            return database if database.is_file() else None
        else:
            return None
    except OSError:
        return None
    existing = [path for path in candidates if path.is_file()]
    if not existing:
        return None
    return max(existing, key=lambda path: path.stat().st_mtime)


def _tail_lines(path: Path, limit: int = TAIL_BYTES) -> list[str]:
    """The last ``limit`` bytes of a transcript as text lines (whole file when small)."""
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - limit) if limit else 0)
            data = handle.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    if limit and size > limit and lines:
        del lines[0]  # the first line is probably truncated
    return lines


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") in {"text", "input_text", "output_text"}
        )
    return ""


def transcript_exchange(path: Path, source: str, native_id: str = "") -> tuple[list[str], str]:
    """Return (recent user prompts, last assistant reply) from a transcript."""
    if source == "opencode":
        # The OpenCode transcript is one SQLite DB shared by every session; the
        # session's own rows are selected inside by ``native_id``.
        return opencode_transcript_exchange(path, native_id)
    prompts: list[str] = []
    reply = ""
    for line in _tail_lines(path):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        kind, text = _entry_exchange(entry, source)
        if not text:
            continue
        if kind == "user":
            if text.startswith("<"):  # injected context, not a real request
                continue
            if _is_trivial_continuation(text):  # "go on" says nothing worth keeping
                continue
            if prompts and prompts[-1] == text:  # the same request logged twice
                continue
            prompts.append(text)
            del prompts[:-MAX_TRANSCRIPT_SCAN]  # bound the walk back over old turns
        else:
            reply = text
    return prompts[-MAX_PROMPTS:], reply


def _entry_exchange(entry: dict[str, Any], source: str) -> tuple[str, str]:
    if source == "claude":
        kind = entry.get("type")
        if kind not in {"user", "assistant"}:
            return "", ""
        message = entry.get("message")
        text = _message_text(message.get("content") if isinstance(message, dict) else None)
        return ("user" if kind == "user" else "assistant"), text
    payload = entry.get("payload")
    if not isinstance(payload, dict):
        payload = entry
    outer = entry.get("type")
    if outer == "event_msg" and payload.get("type") in {"user_message", "agent_message"}:
        kind = "user" if payload.get("type") == "user_message" else "assistant"
        return kind, str(payload.get("message") or "")
    if payload.get("type") == "message" and payload.get("role") in {"user", "assistant"}:
        return str(payload.get("role")), _message_text(payload.get("content"))
    return "", ""


def _is_trivial_continuation(text: str) -> bool:
    """An exact continuation-only message; a longer sentence is never eaten."""
    return text.strip().casefold() in TRIVIAL_CONTINUATIONS


def opencode_transcript_exchange(database: Path, native_id: str) -> tuple[list[str], str]:
    """(recent user prompts, last assistant reply) from the OpenCode session DB.

    The V2 backend stores every session in one SQLite DB (``session_message``
    rows plus ``part`` rows) instead of one JSONL per session. Every read is
    read-only and bounded, and a missing, locked or malformed DB simply yields
    no exchange — a handoff must never break over its transcript.
    """
    prompts: list[str] = []
    reply = ""
    try:
        connection = sqlite3.connect(
            f"file:{urllib.parse.quote(str(database))}?mode=ro", uri=True, timeout=1
        )
    except (OSError, sqlite3.Error):
        return [], ""
    try:
        rows = connection.execute(
            "SELECT id, type, data FROM session_message "
            "WHERE session_id = ? AND type IN ('user', 'assistant') "
            "ORDER BY time_created DESC, seq DESC LIMIT ?",
            (native_id, MAX_TRANSCRIPT_SCAN),
        ).fetchall()  # the newest MAX_TRANSCRIPT_SCAN messages, newest first
        for message_id, kind, data in reversed(rows):  # transcript order
            text = _opencode_message_text(connection, message_id, kind, data)
            if not text:
                continue
            if kind == "assistant":
                reply = text  # the last assistant turn that said anything wins
                continue
            if text.startswith("<"):  # injected context, not a real request
                continue
            if _is_trivial_continuation(text):  # "go on" says nothing worth keeping
                continue
            if prompts and prompts[-1] == text:  # the same request logged twice
                continue
            prompts.append(text)
            del prompts[:-MAX_TRANSCRIPT_SCAN]  # bound the walk back over old turns
    except (OSError, ValueError, sqlite3.Error):
        return [], ""
    finally:
        connection.close()
    return prompts[-MAX_OPENCODE_PROMPTS:], reply


def _opencode_message_text(
    connection: sqlite3.Connection, message_id: str, kind: str, data: Any
) -> str:
    """A message's text: its ``text`` parts in ``time_created`` order, else the
    inline payload (``data.text`` for user rows, ``data.content`` for assistant)."""
    parts: list[str] = []
    try:
        rows = connection.execute(
            "SELECT data FROM part WHERE message_id = ? ORDER BY time_created LIMIT ?",
            (message_id, MAX_MESSAGE_PARTS),
        ).fetchall()
    except sqlite3.Error:  # no part table, or a locked read: inline text still works
        rows = []
    for row in rows:
        try:
            part = json.loads(row[0]) if isinstance(row[0], str) else None
        except ValueError:
            continue
        if isinstance(part, dict) and part.get("type") == "text":
            parts.append(str(part.get("text") or "")[:MAX_PART_CHARS])
    if parts:
        return clean_string("\n".join(parts))
    try:
        payload = json.loads(data) if isinstance(data, str) else None
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    if kind == "user":
        return clean_string(str(payload.get("text") or "")[:MAX_PART_CHARS])
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    return clean_string("\n".join(
        str(item.get("text") or "")[:MAX_PART_CHARS]
        for item in content
        if isinstance(item, dict) and item.get("type") == "text"
    ))


def _git(directory: str, arguments: list[str]) -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["git", "-C", directory, *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return result.returncode, result.stdout.decode("utf-8", errors="replace")


def _clip(text: str, limit: int = MAX_GIT_LINES) -> list[str]:
    lines = text.splitlines()
    if len(lines) > limit:
        return lines[:limit] + [f"… {len(lines) - limit} more line(s)"]
    return lines


def render_git_state(directory: str) -> list[str]:
    """Markdown for the git state, or nothing when the directory is not a repo."""
    if not directory or not Path(directory).is_dir():
        return []
    code, out = _git(directory, ["rev-parse", "--is-inside-work-tree"])
    if code != 0 or out.strip() != "true":
        return []
    _, branch = _git(directory, ["rev-parse", "--abbrev-ref", "HEAD"])
    _, status = _git(directory, ["status", "--porcelain=v1"])
    _, diff = _git(directory, ["diff", "--stat"])
    _, commits = _git(directory, ["log", f"-{MAX_COMMITS}", "--pretty=%h %s"])
    lines = ["## Git state", "", f"- Branch: `{branch.strip() or 'unknown'}`"]
    for title, command, output in (
        ("git status --porcelain=v1", "git status --porcelain=v1", status),
        ("git diff --stat", "git diff --stat", diff),
        (f"last {MAX_COMMITS} commits", f"git log -{MAX_COMMITS} --pretty=%h %s", commits),
    ):
        body = _clip(output) if output.strip() else ["(nothing)"]
        lines.extend(["", f"- {title} (`{command}`):", "", "  ```"])
        lines.extend("  " + line for line in body)
        lines.append("  ```")
    return lines


def _numbered(text: str) -> list[str]:
    lines = text.splitlines() or [""]
    return [f"1. {lines[0]}"] + ["   " + line for line in lines[1:]]


def _memory_line(entry: dict[str, Any]) -> str:
    """One memory entry as a single clipped line with its writer harness."""
    text = clean_string(entry.get("text"))
    if len(text) > MAX_MEMORY_CHARS:
        text = text[: MAX_MEMORY_CHARS - 1] + "…"
    return f"- ({clean_string(entry.get('writer_harness')) or 'unknown'}) {text}"


def render_handoff(
    session: SessionRecord,
    source: str,
    native_id: str,
    target: str,
    prompts: Sequence[str],
    reply: str,
    git_lines: Sequence[str],
    stamp: datetime,
    memory: Sequence[dict[str, Any]] = (),
) -> str:
    title = session.title or f"{HARNESS_LABELS.get(source, source)} session"
    model = model_name(session.model) if session.model else ""
    lines = [
        f"# Handoff: {title}",
        "",
        "## Source",
        "",
        f"- Harness: {HARNESS_LABELS.get(source, source)} (`{source}`)",
        f"- Session: {native_id or session.id}",
        f"- Title: {title}",
        f"- Model: {model or 'unknown'}",
        f"- Directory: {session.directory or 'unknown'}",
        f"- Target harness: {HARNESS_LABELS.get(target, target)}",
        f"- Written: {stamp.strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    if prompts:
        lines.extend(["", "## Recent requests", ""])
        for text in prompts:
            lines.extend(_numbered(text))
    if reply.strip():
        trimmed = reply.strip()
        if len(trimmed) > MAX_REPLY_CHARS:
            trimmed = "…\n" + trimmed[-MAX_REPLY_CHARS:]
        lines.extend(["", "## Last assistant reply", "", trimmed])
    if memory:
        lines.extend(["", "## PROJECT MEMORY (ocdeck-index, if any)", ""])
        lines.extend(_memory_line(entry) for entry in memory)
    lines.extend(["", *git_lines, ""])
    return "\n".join(lines)


def project_memory_entries(directory: str) -> list[dict[str, Any]]:
    """Shared project memory (``ocdeck-index``) for a handoff; never raises.

    Memory is scoped by project identity, so every harness working on the same
    repository reads the same notes. A missing index, a broken store or an
    invalid directory just yields nothing, and the section is omitted entirely.
    """
    if not directory or not Path(directory).is_dir():
        return []
    try:
        from .index_server import IndexServer

        entries = IndexServer(cwd=Path(directory)).memory_search(limit=MAX_MEMORY_ENTRIES)
    except Exception:  # best-effort: a broken index must never block a handoff
        return []
    if not isinstance(entries, list):
        return []
    return [
        entry
        for entry in entries
        if isinstance(entry, dict) and clean_string(entry.get("text"))
    ][:MAX_MEMORY_ENTRIES]


def write_handoff(
    session: SessionRecord,
    target_harness: str,
    hub_dir: Path | None = None,
    *,
    now: datetime | None = None,
) -> tuple[Path, str]:
    """Write handoff notes for ``session`` and return (path, continuation prompt)."""
    if target_harness not in harness_ids():
        raise ValueError(f"Unknown target harness: {target_harness}")
    source, native_id = split_session_key(session.id)
    if source == "opencode" and session.harness in {"claude", "codex"}:
        # A transcript-backed session keeps its label even when the key has no prefix.
        source = session.harness
    stamp = now or datetime.now()
    prompts: list[str] = []
    reply = ""
    transcript = find_transcript(source, native_id)
    if transcript is not None:
        prompts, reply = transcript_exchange(transcript, source, native_id)
    if not prompts and session.last_prompt:
        prompts = [session.last_prompt]
    hub = resolve_hub_dir(hub_dir)
    project = _safe_name(Path(session.directory).name) if session.directory else "workspace"
    directory = hub / "handoffs" / project
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    for private in (hub, hub / "handoffs", directory):  # notes quote the user's prompts
        try:
            private.chmod(0o700)
        except OSError:
            pass
    name = f"{stamp.strftime('%Y%m%d-%H%M%S')}-{source}-to-{target_harness}.md"
    path = directory / name
    document = render_handoff(session, source, native_id, target_harness, prompts, reply,
                              render_git_state(session.directory), stamp,
                              project_memory_entries(session.directory))
    atomic_write(path, document, mode=0o600)
    prompt = (
        f'Continue the work handed off from {HARNESS_LABELS.get(source, source)} session "{session.title}". '
        f"First read the handoff notes at {path}, check the git state, then carry on."
    )
    return path, prompt


def session_record_from_key(key: str) -> SessionRecord:
    """A best-effort :class:`SessionRecord` for a ``claude:``/``codex:``/OpenCode key."""
    source, native_id = split_session_key(key)
    now = int(time.time() * 1000)
    record = SessionRecord(
        id=key,
        title=f"{HARNESS_LABELS.get(source, source)} session",
        directory="",
        project_id="",
        created_ms=now,
        updated_ms=now,
        harness=source,
    )
    if (transcript := find_transcript(source, native_id)) is not None:
        info = _transcript_info(source, transcript)
        if info is not None:
            record = replace(
                record,
                title=info.title or record.title,
                directory=info.directory or record.directory,
                last_prompt=info.last_prompt,
                model=info.model,
                created_ms=info.created_ms or record.created_ms,
                updated_ms=info.updated_ms or record.updated_ms,
            )
    if not record.directory:
        record = replace(record, directory=str(Path.cwd()))
    return record


def _transcript_info(source: str, path: Path):
    harness = {"claude": ClaudeHarness, "codex": CodexHarness}.get(source)
    if harness is None:
        return None
    try:
        return harness().parse_transcript(path, path.stat().st_size)
    except (OSError, ValueError):
        return None


def default_target_harness(source: str) -> str:
    enabled = resolve_enabled_harnesses(load_harness_settings())
    for harness in enabled:
        if harness != source:
            return harness
    return enabled[0] if enabled else "opencode"


# --- cli ---------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ocdeck-hub",
        description="Keep every agent harness on the same instructions and MCP servers.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="create the hub config (never overwrites)")
    commands.add_parser("status", help="show the enabled harnesses and the sync plan")
    harness = commands.add_parser("harness", help="turn a harness on, off or auto")
    harness.add_argument("setting", choices=sorted(SETTING_VALUES))
    harness.add_argument("name", help="opencode, claude or codex")
    sync = commands.add_parser("sync", help="show, or apply, the sync plan")
    sync.add_argument("--apply", action="store_true", help="write the plan instead of only showing it")
    sync.add_argument(
        "--only", choices=("mcp", "instructions"),
        help="limit to MCP server registration (e.g. the shared project memory) or to instructions",
    )
    handoff = commands.add_parser("handoff", help="write handoff notes for a session")
    handoff.add_argument("session", help="claude:<uuid>, codex:<uuid> or an OpenCode session id")
    handoff.add_argument("--hub", help="hub directory (default $OCDECK_HUB_DIR or ~/.config/agents)")
    handoff.add_argument("--to", choices=harness_ids(), help="target harness (default: first enabled other harness)")
    arguments = parser.parse_args(argv)

    if arguments.command == "init":
        path, created = initialize_hub()
        if not created:
            print(f"hub config already exists: {path}")
            return 0
        config = load_hub_config()
        print(f"hub config created: {path}")
        print(f"instructions: {config.instructions}")
        print(f"MCP servers: {', '.join(config.mcp) or 'none'}")
        return 0

    if arguments.command == "status":
        enabled = resolve_enabled_harnesses(load_harness_settings())
        hub = default_hub_dir()
        plan = plan_actions(hub)
        print(f"harnesses: {', '.join(HARNESS_LABELS[item] for item in enabled) or 'none'}")
        print(f"hub: {hub}")
        print(f"sync plan ({len(plan)} action{'s' if len(plan) != 1 else ''}, dry run — pass --apply to write):")
        for line in format_plan(plan):
            print(line)
        return 0

    if arguments.command == "harness":
        if arguments.name not in harness_ids():
            print(f"unknown harness: {arguments.name}", file=sys.stderr)
            return 2
        settings = load_harness_settings()
        settings[arguments.name] = arguments.setting
        save_harness_settings(settings)
        print(f"{arguments.name}: {arguments.setting}")
        return 0

    if arguments.command == "sync":
        plan = [action for action in plan_actions() if arguments.only in (None, action_kind(action))]
        for line in format_plan(plan):
            print(line)
        if not arguments.apply:
            print(f"{len(plan)} action(s) planned; nothing was written (pass --apply).")
            return 0
        applied = 0
        for action in plan:
            if action.manual:
                print(f"manual: {action.description}", file=sys.stderr)
                continue
            try:
                action.apply_fn()
            except Exception as error:  # a broken harness must not corrupt the others
                print(f"sync failed for {action.harness} {action.target_path}: {error}", file=sys.stderr)
                return 1
            print(f"applied: {action.harness} {action.target_path} {action.description}")
            applied += 1
        print(f"applied {applied} action(s)")
        return 0

    try:
        session = session_record_from_key(arguments.session)
        target = arguments.to or default_target_harness(session.harness)
        path, prompt = write_handoff(session, target, arguments.hub)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(path)
    print()
    print(prompt)
    return 0


# How each harness receives the shared instructions and MCP servers. A harness
# plugin can add its own entry: ``SYNC_PLANNERS["gemini"] = gemini_actions``.
SYNC_PLANNERS: dict[str, Callable[[HubConfig], list[Action]]] = {
    "opencode": opencode_actions,
    "claude": claude_actions,
    "codex": codex_actions,
}


if __name__ == "__main__":
    raise SystemExit(main())
