"""Pluggable agent harnesses (OpenCode, Claude Code, Codex) for OC Deck.

Every harness is optional. OpenCode keeps its full-featured source
(:class:`ocdeck.source.DashboardSource`); the others are read from their
on-disk transcripts and live processes. :class:`MultiHarnessSource` merges the
enabled ones into a single snapshot, so OC Deck keeps working when any
harness (OpenCode included) is disabled or uninstalled.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from .models import DashboardSnapshot, ProjectRecord, SessionRecord, clean_string
from .codex_status import apply_statuses as apply_codex_statuses, read_statuses as read_codex_statuses

# --- harness registry ----------------------------------------------------------
# Every harness is described once, by a HarnessSpec. The tables below are live
# views of the registry, so OC Deck, the hub, tab purging and session history
# pick up a new harness from its spec alone. To add one, drop a module into
# ``ocdeck/harness_plugins/`` that calls ``register_harness`` (see _template.py).


@dataclass(frozen=True, slots=True)
class HarnessSpec:
    id: str                      # stable key, also the session-key prefix ("gemini:<id>")
    label: str                   # "Gemini CLI"
    code: str                    # two letters for narrow columns: "GM"
    binaries: tuple[str, ...]    # CLI names, tried on PATH then common install dirs
    tmux_prefix: str = ""        # OC Deck terminal names: "<prefix>-<native id>"
    badge: str = ""              # shown before titles; "" hides it (OpenCode)
    style: str = "#8ba4b5"       # colour in the Agents board
    adapter: Any = None          # TranscriptHarness subclass; None for OpenCode


HARNESS_REGISTRY: dict[str, HarnessSpec] = {}
HARNESS_IDS: tuple[str, ...] = ()
HARNESS_LABELS: dict[str, str] = {}
HARNESS_BADGES: dict[str, str] = {}
HARNESS_CODES: dict[str, str] = {}
HARNESS_BINARIES: dict[str, tuple[str, ...]] = {}
HARNESS_STYLES: dict[str, str] = {}
_RESERVED_PREFIXES = {"oc", "oc2"}  # OpenCode terminals (oc-, oc2-, oc-browser-)


def register_harness(spec: HarnessSpec) -> HarnessSpec:
    """Add (or replace) a harness. Validates what other modules rely on."""
    global HARNESS_IDS
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,19}", spec.id):
        raise ValueError(f"harness id {spec.id!r} must be lowercase letters, digits or '_'")
    if not re.fullmatch(r"[A-Z0-9]{2}", spec.code):
        raise ValueError(f"harness code {spec.code!r} must be two capital letters or digits")
    if spec.id == "opencode":
        # The built-in may be re-registered but keeps its shape: OpenCode's
        # terminals use the reserved oc-/oc2- names, never a registry prefix.
        if spec.tmux_prefix or spec.adapter is not None or not spec.binaries:
            raise ValueError("the OpenCode spec must keep no tmux prefix, no adapter and its binaries")
    elif not re.fullmatch(r"[a-z][a-z0-9]{0,5}", spec.tmux_prefix) or spec.tmux_prefix in _RESERVED_PREFIXES:
        raise ValueError(f"harness {spec.id}: tmux prefix {spec.tmux_prefix!r} is invalid or reserved")
    clash = [
        other.id for other in HARNESS_REGISTRY.values()
        if other.id != spec.id
        and ((spec.tmux_prefix and other.tmux_prefix == spec.tmux_prefix) or other.code == spec.code)
    ]
    if clash:
        raise ValueError(f"harness {spec.id}: tmux prefix or code already used by {clash[0]}")
    HARNESS_REGISTRY[spec.id] = spec
    HARNESS_IDS = tuple(HARNESS_REGISTRY)
    for table, value in (
        (HARNESS_LABELS, spec.label), (HARNESS_BADGES, spec.badge), (HARNESS_CODES, spec.code),
        (HARNESS_BINARIES, spec.binaries), (HARNESS_STYLES, spec.style),
    ):
        table[spec.id] = value
    return spec


def unregister_harness(harness: str) -> None:
    """Remove a harness (tests, or a plugin being replaced)."""
    global HARNESS_IDS
    if harness == "opencode":
        raise ValueError("OpenCode is built in")
    HARNESS_REGISTRY.pop(harness, None)
    HARNESS_IDS = tuple(HARNESS_REGISTRY)
    for table in (HARNESS_LABELS, HARNESS_BADGES, HARNESS_CODES, HARNESS_BINARIES, HARNESS_STYLES):
        table.pop(harness, None)


def harness_ids() -> tuple[str, ...]:
    """Registered harnesses, in registration order (always current)."""
    return tuple(HARNESS_REGISTRY)


def managed_tmux_prefixes() -> tuple[str, ...]:
    """Name prefixes of every terminal OC Deck itself launches."""
    return ("oc-", "oc2-") + tuple(
        f"{spec.tmux_prefix}-" for spec in HARNESS_REGISTRY.values() if spec.tmux_prefix
    )


register_harness(HarnessSpec(
    "opencode", "OpenCode", "OC", ("opencode2", "opencode"), style="#86b7ff",
))
# Model family -> short code; a version digit is appended for versioned families.
MODEL_FAMILIES = (
    ("astra", "AST", False), ("sol", "SOL", False), ("codex", "CDX", False),
    ("opus", "OP", True), ("sonnet", "SN", True), ("haiku", "HK", True), ("fable", "FB", True),
    ("big-pickle", "BPK", False), ("nemotron", "NEM", False), ("qwen", "QWN", False),
    ("gemini", "GMN", False), ("gemma", "GEM", False), ("mimo", "MIM", False),
    ("longcat", "LCT", False), ("deepseek", "DSK", False), ("kimi", "KMI", False),
    ("grok", "GRK", False), ("glm", "GLM", False), ("gpt", "GP", True),
)
SETTING_VALUES = {"on", "off", "auto"}
# A transcript written this recently belongs to a turn that is still running.
BUSY_WINDOW_SECONDS = 10
MAX_SESSIONS_PER_HARNESS = 200
HEAD_LINES = 60
TAIL_BYTES = 64 * 1024


def model_name(model: str) -> str:
    """``"openai/gpt-6-astra#max"`` -> ``"gpt-6-astra"``."""
    return model.split("#", 1)[0].rsplit("/", 1)[-1].strip()


def model_code(model: str) -> str:
    """A 3-letter code for a model id, e.g. ``claude-opus-5-5`` -> ``OP5``."""
    name = model_name(model).lower()
    if not name:
        return "—"
    for family, code, versioned in MODEL_FAMILIES:
        position = name.find(family)
        if position < 0:
            continue
        if versioned:
            digit = next((char for char in name[position + len(family):] if char.isdigit()), "")
            digit = digit or next((char for char in name if char.isdigit()), "")
            return (code + digit)[:3]
        return code
    letters = "".join(char for char in name if char.isalpha())
    return letters[:3].upper() or "?"


def runtime_label(harness: str, model: str, *, full: bool) -> str:
    """Harness + model for the Agents board: full names or 2-3 letter codes."""
    if full:
        name = model_name(model)
        label = HARNESS_LABELS.get(harness, harness)
        return f"{label} · {name}" if name else label
    return f"{HARNESS_CODES.get(harness, harness[:2].upper())} {model_code(model)}"


def default_harness_settings_file() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser()
    if base.name == "ocdeck-v2-runtime":
        base = base.parent
    return base / "ocdeck/harnesses.json"


def load_harness_settings(path: Path | None = None) -> dict[str, str]:
    """Return ``{harness: "on"|"off"|"auto"}``; unknown or invalid entries are ignored."""
    settings = {harness: "auto" for harness in harness_ids()}
    path = path or default_harness_settings_file()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return settings
    if isinstance(payload, dict):
        for harness, value in payload.items():
            if isinstance(value, bool):
                value = "on" if value else "off"
            if harness in settings and value in SETTING_VALUES:
                settings[harness] = value
    return settings


def save_harness_settings(settings: dict[str, str], path: Path | None = None) -> None:
    _write_private_json(
        path or default_harness_settings_file(),
        {harness: settings.get(harness, "auto") for harness in harness_ids()},
    )


# --- agent browser -----------------------------------------------------------
# The agent browser is a dedicated signed-in Chrome (its own profile), served as
# an MCP server. It is granted per session: only sessions the operator enabled
# with Shift+B are launched with it attached.
BROWSER_MCP_NAME = "agent_browser"


def _ocdeck_config_dir() -> Path:
    return default_harness_settings_file().parent


def browser_grants_file() -> Path:
    return _ocdeck_config_dir() / "browser-grants.json"


def load_browser_grants(path: Path | None = None) -> set[str]:
    try:
        payload = json.loads((path or browser_grants_file()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {item for item in payload if isinstance(item, str)} if isinstance(payload, list) else set()


def save_browser_grants(grants: set[str], path: Path | None = None) -> None:
    _write_private_json(path or browser_grants_file(), sorted(grants))


def _write_private_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    os.replace(temporary, path)


def agent_browser_server() -> tuple[list[str], dict[str, str]] | None:
    """The agent browser MCP command and environment, if it is installed."""
    command = Path.home() / ".local/lib/opencode/playwright-mcp-v1"
    config = Path.home() / ".config/opencode/agent-browser.json"
    if not (command.is_file() and os.access(command, os.X_OK) and config.is_file()):
        return None
    return [str(command)], {"OPENCODE_AGENT_BROWSER_CONFIG": str(config)}


def browser_mcp_config_file() -> Path | None:
    """Write (and return) a Claude ``--mcp-config`` file for the agent browser."""
    server = agent_browser_server()
    if server is None:
        return None
    command, environment = server
    path = _ocdeck_config_dir() / "agent-browser-mcp.json"
    payload = {"mcpServers": {BROWSER_MCP_NAME: {
        "type": "stdio", "command": command[0], "args": command[1:], "env": environment,
    }}}
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        current = None
    if current != payload:
        _write_private_json(path, payload)
    return path


def find_binary(harness: str) -> str | None:
    """PATH first, then the usual install locations (launchers may have a thin PATH)."""
    for name in HARNESS_BINARIES.get(harness, ()):
        found = shutil.which(name)
        if found:
            return found
    for name in HARNESS_BINARIES.get(harness, ()):
        for directory in (Path.home() / ".local/bin", Path("/usr/local/bin"), Path("/usr/bin")):
            candidate = directory / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None


def resolve_enabled_harnesses(
    settings: dict[str, str],
    override: Iterable[str] | None = None,
    which: Callable[[str], str | None] = find_binary,
) -> tuple[str, ...]:
    """Pick enabled harnesses; an explicit override (CLI/env) wins over settings."""
    if override is not None:
        wanted = {clean_string(item).lower() for item in override}
        unknown = wanted - set(harness_ids()) - {""}
        if unknown:
            raise ValueError(f"Unknown harness: {', '.join(sorted(unknown))}")
        return tuple(harness for harness in harness_ids() if harness in wanted)
    return tuple(
        harness
        for harness in harness_ids()
        if settings.get(harness, "auto") == "on"
        or (settings.get(harness, "auto") == "auto" and which(harness))
    )


@dataclass(frozen=True, slots=True)
class LiveProcess:
    pid: int
    cwd: str
    args: tuple[str, ...]
    tty: str = ""


@dataclass(slots=True)
class TranscriptInfo:
    session_id: str
    directory: str
    title: str
    created_ms: int
    updated_ms: int
    last_prompt: str = ""
    git_branch: str = ""
    model: str = ""
    # Turn boundaries from the transcript: last human prompt, last completed turn.
    prompt_ms: int = 0
    done_ms: int = 0
    turn_open: bool | None = None  # None: the tail did not show either boundary
    question: str = ""  # a tool call waiting on the user, e.g. Claude's AskUserQuestion
    # How the CLI was launched, when the transcript records it: a Claude
    # transcript "entrypoint" or a Codex rollout "source". "" when unrecorded.
    launch_source: str = ""


@dataclass(slots=True)
class _CacheEntry:
    key: tuple[int, int]
    info: TranscriptInfo | None


def _timestamp_ms(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value if value > 10**11 else value * 1000)
    if isinstance(value, str) and value:
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            return 0
    return 0


def _read_head(path: Path, lines: int = HEAD_LINES) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for _ in range(lines):
                line = handle.readline()
                if not line:
                    break
                entry = _parse_line(line)
                if entry is not None:
                    entries.append(entry)
    except OSError:
        pass
    return entries


def _read_tail(path: Path, size: int, limit: int = TAIL_BYTES) -> list[dict[str, Any]]:
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, size - limit))
            data = handle.read(limit)
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    if size > limit and lines:
        lines = lines[1:]  # the first line is probably truncated
    return [entry for line in lines if (entry := _parse_line(line)) is not None]


def _parse_line(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line:
        return None
    try:
        entry = json.loads(line)
    except ValueError:
        return None
    return entry if isinstance(entry, dict) else None


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") in {"text", "input_text", "output_text"}:
                parts.append(str(part.get("text") or ""))
        return "\n".join(parts)
    return ""


def _is_injected_prompt(text: str) -> bool:
    stripped = text.lstrip()
    return not stripped or stripped.startswith((
        "<environment_context>", "<user_instructions>", "<command-", "<local-command",
        "<system-reminder>", "<task-notification>", "Caveat:",
    ))


def _one_line(text: str, limit: int = 160) -> str:
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def read_live_processes(names: set[str], proc_root: Path = Path("/proc")) -> list[LiveProcess]:
    """Find this user's processes whose executable basename is in ``names``."""
    uid = os.getuid()
    found: list[LiveProcess] = []
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return found
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        args = tuple(part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part)
        if not args or not _matches_executable(args, names):
            continue
        try:
            cwd = os.readlink(entry / "cwd")
        except OSError:
            cwd = ""
        try:
            tty = os.readlink(entry / "fd/0")
        except OSError:
            tty = ""
        found.append(LiveProcess(int(entry.name), cwd, args, tty if tty.startswith("/dev/pts/") else ""))
    return found


def _matches_executable(args: tuple[str, ...], names: set[str]) -> bool:
    if Path(args[0]).name in names:
        return True
    # Node-based CLIs run as ``node /path/to/<name>/cli.js``.
    return (
        Path(args[0]).name in {"node", "bun"}
        and len(args) > 1
        and any(f"/{name}/" in args[1] or args[1].endswith(f"/{name}") for name in names)
    )


def read_tmux_sessions() -> dict[str, tuple[bool, tuple[str, ...]]]:
    """Map tmux session name -> (attached, pane ttys)."""
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{session_name}\t#{session_attached}\t#{pane_tty}"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if result.returncode != 0:
        return {}
    sessions: dict[str, tuple[bool, tuple[str, ...]]] = {}
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        name, _, remainder = line.partition("\t")
        attached, _, tty = remainder.partition("\t")
        if not name:
            continue
        previous = sessions.get(name, (False, ()))
        sessions[name] = (
            previous[0] or attached not in {"", "0"},
            previous[1] + ((tty,) if tty else ()),
        )
    return sessions


class TranscriptHarness:
    """Base adapter for CLIs that store sessions as JSONL transcripts."""

    harness = ""

    @property
    def tmux_prefix(self) -> str:
        """Defined once, by the harness's registry spec."""
        spec = HARNESS_REGISTRY.get(self.harness)
        return spec.tmux_prefix if spec and spec.tmux_prefix else self.harness[:2]
    process_names: set[str] = set()

    def __init__(self, root: Path, binary: str | None = None) -> None:
        self.root = root
        self.binary = binary
        self._cache: dict[Path, _CacheEntry] = {}
        # Native session id -> pids of its live CLI processes (last collect).
        self.live_pids: dict[str, tuple[int, ...]] = {}
        self.stoppable_pids: dict[str, tuple[int, ...]] = {}

    # --- adapter interface -------------------------------------------------
    def transcript_files(self) -> list[Path]:
        raise NotImplementedError

    def parse_transcript(self, path: Path, size: int) -> TranscriptInfo | None:
        raise NotImplementedError

    def process_session_id(self, process: LiveProcess) -> str:
        raise NotImplementedError

    def resume_command(self, session_id: str, directory: str, *, browser: bool = False) -> list[str]:
        raise NotImplementedError

    def new_command(
        self, directory: str, prompt: str = "", *, browser: bool = False
    ) -> tuple[list[str], str]:
        """Return (command, pre-assigned session id or "")."""
        raise NotImplementedError

    def browser_arguments(self) -> list[str]:
        """Extra CLI arguments that attach the agent browser to one session."""
        return []

    # --- shared behaviour --------------------------------------------------
    def is_live(self, native_id: str) -> bool:
        """Fresh /proc check for a process explicitly running this session.

        Id-less processes are attributed to the newest transcript in their cwd
        by ``collect`` and so already show up in the snapshot's instance count.
        """
        return any(
            self.process_session_id(process) == native_id
            for process in read_live_processes(self.process_names)
        )

    def original_directory(self, native_id: str) -> str:
        """The folder the session was started in (from its transcript), if known."""
        for entry in self._cache.values():
            if entry.info is not None and entry.info.session_id == native_id:
                return entry.info.directory
        return ""

    def in_original_directory(self, native_id: str, directory: str, argv: list[str]) -> list[str]:
        """Run ``argv`` in the session's own folder even when OC Deck shows (and
        opens its terminal in) another project: CLIs find and continue a
        conversation by its original working directory."""
        original = self.original_directory(native_id)
        if original and os.path.isdir(original) and os.path.realpath(original) != os.path.realpath(directory or "."):
            return ["/usr/bin/env", "-C", original, *argv]
        return argv

    def session_titles(self) -> dict[str, str]:
        """Titles the CLI keeps outside its transcripts (e.g. a rename); {} by default."""
        return {}

    def session_key(self, native_id: str) -> str:
        return f"{self.harness}:{native_id}"

    def tmux_name(self, native_id: str) -> str:
        return f"{self.tmux_prefix}-{native_id}"

    def collect(
        self,
        processes: list[LiveProcess] | None = None,
        tmux: dict[str, tuple[bool, tuple[str, ...]]] | None = None,
        now: float | None = None,
    ) -> list[SessionRecord]:
        now = time.time() if now is None else now
        infos = self._transcripts()
        if processes is None:
            processes = read_live_processes(self.process_names)
        if tmux is None:
            tmux = read_tmux_sessions()
        tty_to_tmux = {tty: name for name, (_, ttys) in tmux.items() for tty in ttys}
        grants = load_browser_grants()
        try:
            titles = self.session_titles()
        except Exception:  # a broken index must not hide sessions
            titles = {}

        live: dict[str, list[LiveProcess]] = {}
        latest_by_dir: dict[str, TranscriptInfo] = {}
        for info in infos:
            current = latest_by_dir.get(info.directory)
            if current is None or info.updated_ms > current.updated_ms:
                latest_by_dir[info.directory] = info
        known = {info.session_id for info in infos}
        explicit: dict[str, list[int]] = {}
        for process in processes:
            native = self.process_session_id(process)
            if native:
                explicit.setdefault(native, []).append(process.pid)
            else:
                # A fresh session has no id on its command line yet; attribute it
                # to the newest transcript in its cwd. Explicit ids are never remapped.
                fallback = latest_by_dir.get(process.cwd)
                native = fallback.session_id if fallback else ""
            if native:
                live.setdefault(native, []).append(process)
        self.live_pids = {
            native: tuple(process.pid for process in procs) for native, procs in live.items()
        }
        # Stoppable without guessing: processes that name the session on their
        # command line, or the only process attributed to it.
        self.stoppable_pids = {
            native: tuple(explicit.get(native, ())) or (pids if len(pids) == 1 else ())
            for native, pids in self.live_pids.items()
        }

        records: list[SessionRecord] = []
        for info in infos:
            procs = live.get(info.session_id, [])
            terminals: set[str] = set()
            own = self.tmux_name(info.session_id)
            if own in tmux:
                terminals.add(own)
            for process in procs:
                if process.tty in tty_to_tmux:
                    terminals.add(tty_to_tmux[process.tty])
            attached = any(tmux.get(name, (False, ()))[0] for name in terminals)
            instance_count = max(len(procs), 1 if terminals else 0)
            if not instance_count:
                status = "idle"
            elif info.turn_open is not None:
                status = "busy" if info.turn_open else "idle"
            else:
                fresh = now * 1000 - info.updated_ms < BUSY_WINDOW_SECONDS * 1000
                status = "busy" if fresh else "idle"
            # A pending question only matters while a process can still receive the answer.
            question = info.question if instance_count else ""
            if question:
                status = "idle"
            records.append(SessionRecord(
                id=self.session_key(info.session_id),
                title=titles.get(info.session_id) or info.title or f"{HARNESS_LABELS[self.harness]} session",
                directory=info.directory,
                project_id="",
                created_ms=info.created_ms,
                updated_ms=info.updated_ms,
                last_interaction_ms=info.prompt_ms or info.updated_ms,
                assistant_activity_ms=info.updated_ms,
                assistant_done_ms=info.done_ms,
                status=status,
                instance_count=instance_count,
                terminals=tuple(sorted(terminals)),
                terminal_attached=attached,
                last_prompt=info.last_prompt,
                question=question,
                assistant_active=status == "busy",
                model=info.model,
                harness=self.harness,
                launch_source=info.launch_source,
                browser_enabled=self.session_key(info.session_id) in grants,
            ))
        return self.apply_permission_status(records)

    def apply_permission_status(self, records: list[SessionRecord]) -> list[SessionRecord]:
        """Hook for a harness whose transcripts don't carry approval state.

        Reserved for CodexHarness (owner bug: Codex permission prompts were
        invisible in OC Deck — rollouts have no approval events). Override this
        method only; do not touch collect() itself. Identity by default.
        """
        return records

    def _transcripts(self) -> list[TranscriptInfo]:
        files: list[tuple[float, Path, os.stat_result]] = []
        for path in self.transcript_files():
            try:
                metadata = path.stat()
            except OSError:
                continue
            files.append((metadata.st_mtime, path, metadata))
        files.sort(key=lambda item: item[0], reverse=True)
        files = files[:MAX_SESSIONS_PER_HARNESS]
        seen: set[Path] = set()
        infos: list[TranscriptInfo] = []
        for _, path, metadata in files:
            seen.add(path)
            key = (metadata.st_mtime_ns, metadata.st_size)
            cached = self._cache.get(path)
            if cached is None or cached.key != key:
                try:
                    info = self.parse_transcript(path, metadata.st_size)
                except Exception:  # a corrupt transcript must never break the deck
                    info = None
                if info is not None and not info.updated_ms:
                    info.updated_ms = int(metadata.st_mtime * 1000)
                cached = _CacheEntry(key, info)
                self._cache[path] = cached
            if cached.info is not None:
                infos.append(cached.info)
        for stale in set(self._cache) - seen:
            del self._cache[stale]
        return infos


class ClaudeHarness(TranscriptHarness):
    harness = "claude"
    process_names = {"claude"}

    def __init__(self, root: Path | None = None, binary: str | None = None) -> None:
        super().__init__(root or Path.home() / ".claude/projects", binary or find_binary("claude"))

    def transcript_files(self) -> list[Path]:
        # Subagent transcripts live one level deeper and are intentionally skipped.
        try:
            return [path for directory in self.root.iterdir() if directory.is_dir()
                    for path in directory.glob("*.jsonl")]
        except OSError:
            return []

    def parse_transcript(self, path: Path, size: int) -> TranscriptInfo | None:
        head = _read_head(path)
        tail = _read_tail(path, size)
        session_id = path.stem
        head_model = ""
        directory = ""
        created = 0
        first_prompt = ""
        # "cli" is the interactive TUI; "sdk-cli" is a headless `claude -p` run.
        launch_source = ""
        for entry in head:
            directory = directory or clean_string(entry.get("cwd"))
            created = created or _timestamp_ms(entry.get("timestamp"))
            launch_source = launch_source or clean_string(entry.get("entrypoint"))
            if entry.get("type") == "assistant" and isinstance(entry.get("message"), dict):
                head_model = head_model or _claude_model(entry)
            if not first_prompt and entry.get("type") == "user" and _claude_human_prompt(entry):
                message = entry.get("message")
                first_prompt = _message_text(message.get("content") if isinstance(message, dict) else "")
        if not directory:
            return None
        titles: dict[str, str] = {}
        last_prompt = ""
        updated = 0
        branch = ""
        model = ""
        prompt_ms = done_ms = 0
        turn_open: bool | None = None
        pending_input: dict[str, str] = {}  # tool_use id -> label, until its tool_result arrives
        local_commands: set[str] = set()  # uuids of slash-command entries (e.g. /context)
        for entry in tail:
            kind = entry.get("type")
            stamp = _timestamp_ms(entry.get("timestamp"))
            updated = max(updated, stamp)
            branch = clean_string(entry.get("gitBranch")) or branch
            launch_source = launch_source or clean_string(entry.get("entrypoint"))
            _track_claude_input_calls(entry, pending_input)
            if kind == "system" and entry.get("subtype") == "local_command":
                local_commands.add(clean_string(entry.get("uuid")))
            # Transcript 'user' entries include local slash-command bookkeeping,
            # which can be written while the CLI is idle. Only work-bearing
            # entries reopen a turn; terminal errors/interruption close it.
            if kind == "user":
                message = entry.get("message")
                text = _message_text(message.get("content") if isinstance(message, dict) else None).lstrip()
                if text.startswith("[Request interrupted by user"):
                    done_ms, turn_open = stamp or done_ms, False
                elif (text.startswith(("<command-", "<local-command", "Caveat:"))
                      or entry.get("parentUuid") in local_commands):
                    # The second case is a command's output copied in for the
                    # model (/context writes one); peer and task messages are
                    # not parented to a local command and still reopen.
                    # A local-only tail is not evidence of model activity, even
                    # if its fresh timestamp would trigger the mtime fallback.
                    if turn_open is None:
                        turn_open = False
                else:
                    turn_open = True
                    if _claude_human_prompt(entry):
                        prompt_ms = stamp or prompt_ms
            elif kind == "assistant":
                message = entry.get("message")
                if entry.get("isApiErrorMessage"):
                    done_ms, turn_open = stamp or done_ms, False
                elif isinstance(message, dict) and message.get("model") != "<synthetic>":
                    turn_open = True
            elif kind == "system" and entry.get("subtype") == "turn_duration":
                done_ms, turn_open = stamp or done_ms, False
            if kind == "custom-title":
                titles[kind] = clean_string(entry.get("customTitle")) or titles.get(kind, "")
            elif kind == "ai-title":
                titles[kind] = clean_string(entry.get("aiTitle")) or titles.get(kind, "")
            elif kind == "summary":
                titles[kind] = clean_string(entry.get("summary")) or titles.get(kind, "")
            elif kind == "last-prompt":
                last_prompt = clean_string(entry.get("lastPrompt")) or last_prompt
            elif kind == "assistant":
                model = _claude_model(entry) or model
        model = model or head_model  # long turns can push every assistant entry out of the tail
        # A user's rename wins over generated titles.
        title = titles.get("custom-title") or titles.get("ai-title") or titles.get("summary", "")
        return TranscriptInfo(
            session_id=session_id,
            directory=directory,
            title=_one_line(title or first_prompt, 80),
            created_ms=created,
            updated_ms=updated,
            last_prompt=_one_line(last_prompt or first_prompt),
            git_branch=branch,
            model=model,
            launch_source=launch_source,
            prompt_ms=prompt_ms,
            done_ms=done_ms,
            turn_open=turn_open,
            question=next(reversed(pending_input.values()), "") if turn_open is not False else "",
        )

    def process_session_id(self, process: LiveProcess) -> str:
        return _flag_value(process.args, ("--resume", "-r", "--session-id"))

    def browser_arguments(self) -> list[str]:
        path = browser_mcp_config_file()
        return ["--mcp-config", str(path)] if path else []

    def resume_command(self, session_id: str, directory: str, *, browser: bool = False) -> list[str]:
        # --mcp-config is variadic, so it must precede the other options.
        extra = self.browser_arguments() if browser else []
        return self.in_original_directory(
            session_id, directory, [self.binary or "claude", *extra, "--resume", session_id]
        )

    def new_command(
        self, directory: str, prompt: str = "", *, browser: bool = False
    ) -> tuple[list[str], str]:
        session_id = _new_uuid()
        extra = self.browser_arguments() if browser else []
        command = [self.binary or "claude", *extra, "--session-id", session_id]
        if prompt:
            command.append(prompt)
        return command, session_id


class CodexHarness(TranscriptHarness):
    harness = "codex"
    process_names = {"codex"}

    def __init__(self, root: Path | None = None, binary: str | None = None) -> None:
        home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
        super().__init__(root or home / "sessions", binary or find_binary("codex"))
        self.index_file = self.root.parent / "session_index.jsonl"
        self._titles: tuple[tuple[int, int], dict[str, str]] = ((0, 0), {})

    def apply_permission_status(self, records: list[SessionRecord]) -> list[SessionRecord]:
        native_ids = [record.id.removeprefix("codex:") for record in records if record.instance_count]
        statuses = read_codex_statuses(self.root.parent, native_ids)
        return apply_codex_statuses(records, statuses)

    def session_titles(self) -> dict[str, str]:
        """Thread names Codex shows (``session_index.jsonl``), newest entry wins."""
        try:
            metadata = self.index_file.stat()
        except OSError:
            return {}
        key = (metadata.st_mtime_ns, metadata.st_size)
        if key != self._titles[0]:
            titles: dict[str, str] = {}
            for entry in _read_tail(self.index_file, metadata.st_size, limit=1024 * 1024):
                name = _one_line(clean_string(entry.get("thread_name")), 80)
                native = clean_string(entry.get("id"))
                if native and name:
                    titles[native] = name
            self._titles = (key, titles)
        return self._titles[1]

    def transcript_files(self) -> list[Path]:
        try:
            return list(self.root.glob("**/rollout-*.jsonl"))
        except OSError:
            return []

    def parse_transcript(self, path: Path, size: int) -> TranscriptInfo | None:
        head = _read_head(path)
        tail = _read_tail(path, size)
        session_id = ""
        directory = ""
        created = 0
        first_prompt = ""
        model = ""
        # The session_meta line's payload names the launch surface: "cli" and
        # "vscode" are interactive; "exec"/"appServer"/"mcp"/"subAgent*" are
        # programmatic launches an agent started.
        launch_source = ""
        for entry in head:
            payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else entry
            kind = entry.get("type")
            if kind == "session_meta" or (not session_id and "id" in entry and "timestamp" in entry):
                session_id = session_id or clean_string(payload.get("id"))
                directory = directory or clean_string(payload.get("cwd"))
                created = created or _timestamp_ms(payload.get("timestamp") or entry.get("timestamp"))
                launch_source = launch_source or clean_string(payload.get("source"))
            if kind == "turn_context":
                directory = directory or clean_string(payload.get("cwd"))
                model = clean_string(payload.get("model")) or model
            if not first_prompt:
                first_prompt = _codex_user_text(entry)
        if not session_id:
            session_id = _codex_id_from_name(path.stem)
        if not session_id or not directory:
            return None
        last_prompt = ""
        updated = prompt_ms = done_ms = 0
        turn_open: bool | None = None
        for entry in tail:
            stamp = _timestamp_ms(entry.get("timestamp"))
            updated = max(updated, stamp)
            text = _codex_user_text(entry)
            if text:
                prompt_ms, turn_open = stamp or prompt_ms, True
            last_prompt = text or last_prompt
            payload = entry.get("payload")
            if entry.get("type") == "event_msg" and isinstance(payload, dict):
                if payload.get("type") == "task_started":
                    prompt_ms, turn_open = stamp or prompt_ms, True
                elif payload.get("type") in {"task_complete", "turn_aborted"}:
                    done_ms, turn_open = stamp or done_ms, False
            if entry.get("type") == "turn_context" and isinstance(payload, dict):
                model = clean_string(payload.get("model")) or model
        return TranscriptInfo(
            session_id=session_id,
            directory=directory,
            title=_one_line(first_prompt, 80),
            created_ms=created,
            updated_ms=updated,
            last_prompt=_one_line(last_prompt or first_prompt),
            model=model,
            launch_source=launch_source,
            prompt_ms=prompt_ms,
            done_ms=done_ms,
            turn_open=turn_open,
        )

    def process_session_id(self, process: LiveProcess) -> str:
        args = process.args
        for index, value in enumerate(args[:-1]):
            if value == "resume" and not args[index + 1].startswith("-"):
                return args[index + 1]
        return ""

    def browser_arguments(self) -> list[str]:
        server = agent_browser_server()
        if server is None:
            return []
        command, environment = server
        prefix = f"mcp_servers.{BROWSER_MCP_NAME}"
        arguments = ["-c", f"{prefix}.command={json.dumps(command[0])}",
                     "-c", f"{prefix}.args={json.dumps(command[1:])}"]
        for name, value in environment.items():
            arguments += ["-c", f"{prefix}.env.{name}={json.dumps(value)}"]
        return arguments

    def resume_command(self, session_id: str, directory: str, *, browser: bool = False) -> list[str]:
        extra = self.browser_arguments() if browser else []
        return self.in_original_directory(
            session_id, directory, [self.binary or "codex", *extra, "resume", session_id]
        )

    def new_command(
        self, directory: str, prompt: str = "", *, browser: bool = False
    ) -> tuple[list[str], str]:
        extra = self.browser_arguments() if browser else []
        command = [self.binary or "codex", *extra]
        if prompt:
            command.append(prompt)
        return command, ""


def _claude_model(entry: dict[str, Any]) -> str:
    """Synthetic CLI notices do not identify the model that ran the session."""
    message = entry.get("message")
    model = clean_string(message.get("model")) if isinstance(message, dict) else ""
    return "" if model == "<synthetic>" or entry.get("isApiErrorMessage") else model


CLAUDE_INPUT_TOOLS = {
    "AskUserQuestion": "Claude is asking a question — open its terminal to answer",
    "ExitPlanMode": "Claude's plan is waiting for approval — open its terminal to respond",
}


def _track_claude_input_calls(entry: dict[str, Any], pending: dict[str, str]) -> None:
    """Record tool calls that block on the user until their tool_result arrives."""
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if entry.get("isSidechain"):
        return
    if entry.get("type") == "user" and _message_text(content).lstrip().startswith("[Request interrupted by user"):
        pending.clear()
        return
    if not isinstance(content, list):
        return
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "tool_use" and part.get("name") in CLAUDE_INPUT_TOOLS:
            tool_id = clean_string(part.get("id"))
            if tool_id:
                pending[tool_id] = _claude_question_label(part)
        elif part.get("type") == "tool_result":
            pending.pop(clean_string(part.get("tool_use_id")), None)


def _claude_question_label(part: dict[str, Any]) -> str:
    fallback = CLAUDE_INPUT_TOOLS[part["name"]]
    questions = (part.get("input") or {}).get("questions") if isinstance(part.get("input"), dict) else None
    if isinstance(questions, list) and questions and isinstance(questions[0], dict):
        text = _one_line(clean_string(questions[0].get("question")), 160)
        if text:
            return text
    return fallback


def _claude_human_prompt(entry: dict[str, Any]) -> bool:
    """A typed prompt, not a tool result or injected context."""
    if entry.get("isMeta") or entry.get("isSidechain"):
        return False
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list) and any(
        isinstance(part, dict) and part.get("type") == "tool_result" for part in content
    ):
        return False
    text = _message_text(content)
    return not _is_injected_prompt(text) and not text.lstrip().startswith("[Request interrupted by user")


def _codex_user_text(entry: dict[str, Any]) -> str:
    payload = entry.get("payload")
    if not isinstance(payload, dict):
        payload = entry
    text = ""
    if entry.get("type") == "event_msg" and payload.get("type") == "user_message":
        text = str(payload.get("message") or "")
    elif entry.get("type") == "event_msg" and payload.get("type") == "item_completed":
        item = payload.get("item")
        if isinstance(item, dict) and item.get("type") == "UserMessage":
            text = _message_text(item.get("content"))
    elif payload.get("type") == "message" and payload.get("role") == "user":
        text = _message_text(payload.get("content"))
    return "" if _is_injected_prompt(text) else text


def _codex_id_from_name(stem: str) -> str:
    # rollout-2025-05-07T17-24-21-<uuid>
    parts = stem.split("-")
    return "-".join(parts[-5:]) if len(parts) >= 10 else ""


def _flag_value(args: tuple[str, ...], flags: tuple[str, ...]) -> str:
    for index, value in enumerate(args):
        for flag in flags:
            if value == flag and index + 1 < len(args) and not args[index + 1].startswith("-"):
                return args[index + 1]
            if value.startswith(flag + "="):
                return value.split("=", 1)[1]
    return ""


def _new_uuid() -> str:
    import uuid

    return str(uuid.uuid4())


def split_session_key(session_id: str) -> tuple[str, str]:
    """``"claude:abc"`` -> ``("claude", "abc")``; OpenCode ids have no prefix."""
    harness, sep, native = session_id.partition(":")
    if sep and harness in HARNESS_REGISTRY and harness != "opencode":
        return harness, native
    return "opencode", session_id


# --- git + project merge ----------------------------------------------------

@dataclass(slots=True)
class GitProbe:
    ttl: float = 30.0
    _cache: dict[str, tuple[float, str, str, int]] = field(default_factory=dict)

    def root(self, directory: str) -> str:
        return self.state(directory)[0]

    def state(self, directory: str) -> tuple[str, str, int]:
        """Return (git root or "", branch, dirty file count or -1)."""
        now = time.monotonic()
        cached = self._cache.get(directory)
        if cached and now - cached[0] < self.ttl:
            return cached[1:]
        result = _git_state(directory)
        self._cache[directory] = (now, *result)
        return result


def _git_state(directory: str) -> tuple[str, str, int]:
    if not directory or not Path(directory).is_dir():
        return "", "", -1
    try:
        top = subprocess.run(
            ["git", "-C", directory, "rev-parse", "--show-toplevel"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, check=False,
        )
        if top.returncode != 0:
            return "", "", -1
        status = subprocess.run(
            ["git", "-C", directory, "status", "--porcelain=v1", "--branch", "--untracked-files=normal"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "", "", -1
    root = top.stdout.decode("utf-8", errors="replace").strip()
    lines = status.stdout.decode("utf-8", errors="replace").splitlines()
    branch = ""
    if lines and lines[0].startswith("## "):
        header = lines.pop(0)[3:]
        branch = header.split("...", 1)[0].split(" ", 1)[0]
        if branch.startswith("No commits yet on "):
            branch = header.removeprefix("No commits yet on ")
    return root, branch, len(lines) if status.returncode == 0 else -1


def _normalized(path: str) -> str:
    return os.path.normpath(os.path.expanduser(path)) if path else ""


def _directory_project_id(directory: str) -> str:
    return "dir-" + hashlib.sha256(directory.encode("utf-8")).hexdigest()[:12]


def merge_harness_sessions(
    snapshot: DashboardSnapshot,
    sessions: list[SessionRecord],
    git: GitProbe | None = None,
) -> DashboardSnapshot:
    """Attach foreign sessions to projects (by directory) and add them to the snapshot."""
    git = git or GitProbe()
    projects = list(snapshot.projects)
    by_directory = sorted(
        ((_normalized(project.directory), index) for index, project in enumerate(projects)),
        key=lambda item: len(item[0]), reverse=True,
    )
    merged: list[SessionRecord] = []
    for session in sessions:
        directory = _normalized(session.directory)
        index = next(
            (i for root, i in by_directory
             if root and (directory == root or directory.startswith(root.rstrip(os.sep) + os.sep))),
            None,
        )
        if index is None:
            root = git.root(directory) or directory
            project_id = _directory_project_id(root)
            index = next((i for i, p in enumerate(projects) if p.id == project_id), None)
            if index is None:
                projects.append(ProjectRecord(id=project_id, directory=root, name=Path(root).name or root))
                index = len(projects) - 1
                by_directory.append((root, index))
                by_directory.sort(key=lambda item: len(item[0]), reverse=True)
        project = projects[index]
        projects[index] = replace(
            project,
            session_count=project.session_count + 1,
            active_count=project.active_count + (session.status == "busy"),
            attached_count=project.attached_count + session.terminal_attached,
            instance_count=project.instance_count + session.instance_count,
            updated_ms=max(project.updated_ms, session.updated_ms),
        )
        merged.append(replace(session, project_id=project.id))
    projects = [
        replace(project, git_branch=branch, git_dirty=dirty)
        for project in projects
        for _, branch, dirty in (git.state(_normalized(project.directory)),)
    ]
    return replace(snapshot, sessions=snapshot.sessions + tuple(merged), projects=tuple(projects))


# --- source ----------------------------------------------------------------

class MultiHarnessSource:
    """Wrap the OpenCode source (optional) and add the other enabled harnesses.

    Attribute access falls through to the OpenCode source so every existing
    OpenCode-only feature keeps working unchanged when OpenCode is enabled.
    """

    def __init__(
        self,
        opencode: Any | None,
        adapters: Iterable[TranscriptHarness] = (),
        *,
        opencode_enabled: bool = True,
        metrics_reader: Callable[[], Any] | None = None,
        refresh_harnesses: bool = False,
        harness_override: tuple[str, ...] | None = None,
    ) -> None:
        self._opencode = opencode
        self.opencode_enabled = opencode_enabled and opencode is not None
        self.adapters = {adapter.harness: adapter for adapter in adapters}
        self._git = GitProbe()
        self._metrics_reader = metrics_reader
        self._refresh_harnesses = refresh_harnesses
        self._harness_override = harness_override
        self.launch_harness = next(iter(self.enabled_harnesses), "opencode")

    def __getattr__(self, name: str) -> Any:
        opencode = self.__dict__.get("_opencode")
        if opencode is None:
            raise AttributeError(name)
        return getattr(opencode, name)

    @property
    def enabled_harnesses(self) -> tuple[str, ...]:
        return tuple(
            harness for harness in harness_ids()
            if (harness == "opencode" and self.opencode_enabled) or harness in self.adapters
        )

    @property
    def backend(self) -> str:
        return getattr(self._opencode, "backend", "v2") if self._opencode is not None else "v2"

    @property
    def opencode_bin(self) -> str | None:
        if not self.opencode_enabled:
            return None
        return getattr(self._opencode, "opencode_bin", None)

    def adapter(self, harness: str) -> TranscriptHarness | None:
        return self.adapters.get(harness)

    async def collect(self) -> DashboardSnapshot:
        if self._refresh_harnesses:
            enabled, candidates, opencode_binary = await asyncio.to_thread(self._discover_harnesses)
            adapters = {}
            for candidate in candidates:
                existing = self.adapters.get(candidate.harness)
                if existing is not None and existing.root == candidate.root:
                    existing.binary = candidate.binary
                    adapters[candidate.harness] = existing
                else:
                    adapters[candidate.harness] = candidate
            self.adapters = adapters
            self.opencode_enabled = "opencode" in enabled and self._opencode is not None
            if self._opencode is not None:
                self._opencode.opencode_bin = opencode_binary
            if self.launch_harness not in self.enabled_harnesses:
                self.launch_harness = next(iter(self.enabled_harnesses), "opencode")
        base = await self._collect_opencode("collect")
        return await self._merge(base or self._empty_snapshot())

    def _discover_harnesses(self) -> tuple[tuple[str, ...], list[TranscriptHarness], str | None]:
        """Recheck optional installations/settings without resetting transcript caches."""
        settings = load_harness_settings()
        enabled = resolve_enabled_harnesses(settings, self._harness_override, which=find_binary)
        binary = getattr(self._opencode, "opencode_bin", None)
        if self._opencode is not None and not binary:
            finder = self._opencode._find_opencode2 if self.backend == "v2" else self._opencode._find_opencode
            binary = finder()
        if self._harness_override is None and settings.get("opencode", "auto") == "auto":
            enabled = tuple(harness for harness in harness_ids()
                            if (harness == "opencode" and bool(binary))
                            or (harness != "opencode" and harness in enabled))
        return enabled, build_adapters(enabled), binary

    async def collect_activity(self) -> DashboardSnapshot | None:
        if self.opencode_enabled:
            base = await self._collect_opencode("collect_activity")
            if base is None:
                return None
        else:
            base = self._empty_snapshot()
        return await self._merge(base)

    async def _collect_opencode(self, method: str) -> DashboardSnapshot | None:
        if not self.opencode_enabled:
            return None
        collect = getattr(self._opencode, method, None)
        return await collect() if collect is not None else None

    def _empty_snapshot(self) -> DashboardSnapshot:
        metrics = None
        if self._metrics_reader is not None:
            try:
                metrics = self._metrics_reader()
            except Exception:
                metrics = None
        snapshot = DashboardSnapshot(connection="live", connection_detail="")
        return replace(snapshot, metrics=metrics) if metrics is not None else snapshot

    async def _merge(self, base: DashboardSnapshot) -> DashboardSnapshot:
        adapters = dict(self.adapters)
        if not adapters:
            # Still enrich projects with git state for an OpenCode-only deck.
            merged = await asyncio.to_thread(merge_harness_sessions, base, [], self._git)
            try:
                return route_projects(merged, load_routes())
            except Exception:  # grouping is display-only
                return merged
        results = await asyncio.gather(
            *(asyncio.to_thread(adapter.collect) for adapter in adapters.values()),
            return_exceptions=True,
        )
        sessions: list[SessionRecord] = []
        failures: list[str] = []
        for harness, result in zip(adapters, results):
            if isinstance(result, BaseException):
                failures.append(f"{HARNESS_LABELS[harness]} unavailable: {type(result).__name__}")
            else:
                sessions.extend(result)
        merged = await asyncio.to_thread(merge_harness_sessions, base, sessions, self._git)
        parents_by_pid = {
            pid: adapter.session_key(native)
            for adapter in adapters.values()
            for native, pids in getattr(adapter, "live_pids", {}).items()
            for pid in pids
        }
        try:
            merged = await asyncio.to_thread(apply_lineage, merged, parents_by_pid)
        except Exception:  # lineage is display-only
            pass
        try:
            merged = route_projects(merged, load_routes())
        except Exception:  # grouping is display-only
            pass
        labels = [HARNESS_LABELS[harness] for harness in self.enabled_harnesses]
        detail = base.connection_detail if self.opencode_enabled else ""
        healthy_foreign = len(failures) < len(adapters)
        connection = base.connection
        if not self.opencode_enabled:
            connection = "live" if healthy_foreign else "offline"
        warning = "; ".join(item for item in (base.warning, *failures) if item)
        return replace(
            merged,
            connection=connection,
            connection_detail=(f"{detail} · " if detail else "") + " + ".join(labels),
            warning=warning,
        )


# --- cross-harness lineage ---------------------------------------------------
# OpenCode links its own subagents to parents; nothing links an agent that a
# different harness started (e.g. a Claude session running `opencode2 run`).
# Lineage is observed from the live process tree and remembered, so helpers stay
# nested under the session that launched them after they exit. Display only:
# this is observed correlation, not a trusted launch record.
LINEAGE_LIMIT = 500
CHILD_START_WINDOW_MS = (-5_000, 120_000)


def default_lineage_file() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")).expanduser()
    return base / "ocdeck/lineage.json"


def load_lineage(path: Path | None = None) -> dict[str, str]:
    try:
        payload = json.loads((path or default_lineage_file()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(k): str(v) for k, v in payload.items() if isinstance(k, str) and isinstance(v, str)}


@dataclass(frozen=True, slots=True)
class ProcessInfo:
    pid: int
    ppid: int
    start_ms: int
    cwd: str
    args: tuple[str, ...]


def read_process_table(proc_root: Path = Path("/proc")) -> dict[int, ProcessInfo]:
    """This user's processes with parent, start time, cwd and argv."""
    uid = os.getuid()
    try:
        ticks = os.sysconf("SC_CLK_TCK")
        boot = next(
            int(line.split()[1]) for line in (proc_root / "stat").read_text().splitlines()
            if line.startswith("btime ")
        )
    except (OSError, ValueError, StopIteration):
        return {}
    table: dict[int, ProcessInfo] = {}
    for entry in proc_root.iterdir() if proc_root.is_dir() else ():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            stat = (entry / "stat").read_text()
            fields = stat[stat.rindex(")") + 2:].split()
            raw = (entry / "cmdline").read_bytes()
            cwd = os.readlink(entry / "cwd")
        except (OSError, ValueError):
            continue
        args = tuple(part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part)
        if not args:
            continue
        table[int(entry.name)] = ProcessInfo(
            pid=int(entry.name),
            ppid=int(fields[1]),
            start_ms=int((boot + int(fields[19]) / ticks) * 1000),
            cwd=cwd,
            args=args,
        )
    return table


# Environment stamps that name the launching session. Children inherit them even
# after being re-parented, unlike the process tree. Only these keys are read;
# the environment also holds tokens that must never be copied.
PARENT_STAMPS = {"CLAUDE_CODE_SESSION_ID": "claude"}


def parent_stamp(pid: int, proc_root: Path = Path("/proc")) -> str:
    try:
        environ = (proc_root / str(pid) / "environ").read_bytes()
    except OSError:
        return ""
    for item in environ.split(b"\0"):
        key, _, value = item.partition(b"=")
        harness = PARENT_STAMPS.get(key.decode("ascii", errors="replace"))
        native = value.decode("utf-8", errors="replace").strip()
        if harness and native and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", native):
            return f"{harness}:{native}"
    return ""


def headless_child_harness(args: tuple[str, ...]) -> str:
    """Harness of a non-interactive agent run started by another program."""
    name = Path(args[0]).name
    rest = args[1:]
    if name in {"opencode", "opencode2", "opencode2-client"} and rest[:1] == ("run",):
        return "opencode"
    if name == "claude" and ("-p" in rest or "--print" in rest):
        return "claude"
    if name == "codex" and rest[:1] == ("exec",):
        return "codex"
    return ""


def observe_lineage(
    sessions: Iterable[SessionRecord],
    parents_by_pid: dict[int, str],
    table: dict[int, ProcessInfo],
    stamp: Callable[[int], str] | None = None,
) -> dict[str, str]:
    """Map child session id -> parent session key for live headless children."""
    candidates: dict[tuple[str, str], list[SessionRecord]] = {}
    for session in sessions:
        if not (session.parent_id or session.agent_parent_id):
            candidates.setdefault((session.harness, _normalized(session.directory)), []).append(session)
    found: dict[str, str] = {}
    for process in table.values():
        harness = headless_child_harness(process.args)
        if not harness:
            continue
        parent_key, pid, seen = (stamp or parent_stamp)(process.pid), process.ppid, set()
        while pid > 1 and pid not in seen and not parent_key:
            seen.add(pid)
            parent_key = parents_by_pid.get(pid, "")
            pid = table[pid].ppid if pid in table else 0
        if not parent_key:
            continue
        low, high = CHILD_START_WINDOW_MS
        matches = [
            session for session in candidates.get((harness, _normalized(process.cwd)), ())
            if low <= session.created_ms - process.start_ms <= high and session.id != parent_key
        ]
        if matches:
            child = min(matches, key=lambda session: abs(session.created_ms - process.start_ms))
            found[child.id] = parent_key
    return found


def apply_lineage(
    snapshot: DashboardSnapshot,
    parents_by_pid: dict[int, str],
    path: Path | None = None,
    table: dict[int, ProcessInfo] | None = None,
) -> DashboardSnapshot:
    path = path or default_lineage_file()
    known = load_lineage(path)
    table = read_process_table() if table is None else table
    observed = observe_lineage(snapshot.sessions, parents_by_pid, table)
    new = {child: parent for child, parent in observed.items() if known.get(child) != parent}
    if new:
        known.update(new)
        if len(known) > LINEAGE_LIMIT:
            known = dict(list(known.items())[-LINEAGE_LIMIT:])
        try:
            _write_private_json(path, known)
        except OSError:
            pass  # nesting is cosmetic; never break the snapshot
    if not known:
        return snapshot
    ids = {session.id for session in snapshot.sessions}
    return replace(snapshot, sessions=tuple(
        replace(session, agent_parent_id=known[session.id])
        if session.id in known and known[session.id] in ids
        and not (session.parent_id or session.agent_parent_id)
        else session
        for session in snapshot.sessions
    ))


# --- project routing (display only) ------------------------------------------
# Which project a session is *shown* under, never where it runs: resuming always
# uses the session's own folder (TranscriptHarness.in_original_directory).
SCRATCH_PROJECT_ID = "dir-scratch"


def local_routes_file() -> Path:
    """OC Deck's own session -> project routes (private, not Drive-synced)."""
    return _ocdeck_config_dir() / "session-routes.json"


def load_routes(path: Path | None = None) -> dict[str, str]:
    """``{session key: project name}``; the same shape as the vault routes file."""
    try:
        payload = json.loads((path or local_routes_file()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    sessions = payload.get("sessions") if isinstance(payload, dict) else None
    if not isinstance(sessions, dict):
        return {}
    return {
        clean_string(key): clean_string(name)
        for key, name in sessions.items()
        if isinstance(key, str) and isinstance(name, str) and clean_string(key) and clean_string(name)
    }


def save_route(session_key: str, project_name: str, path: Path | None = None) -> None:
    path = path or local_routes_file()
    routes = load_routes(path)
    if project_name:
        routes[session_key] = project_name
    else:
        routes.pop(session_key, None)
    _write_private_json(path, {"sessions": dict(sorted(routes.items()))})


def _temporary(directory: str) -> bool:
    roots = {os.path.realpath(tempfile.gettempdir()), "/tmp", "/var/tmp"}
    real = os.path.realpath(directory) if directory else ""
    return bool(real) and any(real == root or real.startswith(root.rstrip("/") + "/") for root in roots)


def route_projects(snapshot: DashboardSnapshot, routes: dict[str, str]) -> DashboardSnapshot:
    """Group sessions under the right project, for display only.

    1. An explicit route (session key -> project name) wins.
    2. A nested helper follows its parent's project.
    3. Sessions in temporary folders without a parent go to one "Scratch"
       project, and temporary-folder projects left without sessions disappear.
    """
    by_name = {project.name: project.id for project in snapshot.projects}
    sessions = {session.id: session for session in snapshot.sessions}
    assigned: dict[str, str] = {}

    def project_of(session: SessionRecord, depth: int = 0) -> str:
        if session.id in assigned:
            return assigned[session.id]
        target = by_name.get(routes.get(session.id, ""), "")
        parent = sessions.get(session.parent_id or session.agent_parent_id or "")
        if not target and parent is not None and parent.id != session.id and depth < 16:
            target = project_of(parent, depth + 1)
        if not target and _temporary(session.directory):
            target = SCRATCH_PROJECT_ID
        assigned[session.id] = target or session.project_id
        return assigned[session.id]

    for session in snapshot.sessions:
        project_of(session)
    moved = tuple(replace(s, project_id=assigned[s.id]) if assigned[s.id] != s.project_id else s
                  for s in snapshot.sessions)
    used = {session.project_id for session in moved}
    projects = [p for p in snapshot.projects if p.id in used or not _temporary(p.directory)]
    if SCRATCH_PROJECT_ID in used and all(p.id != SCRATCH_PROJECT_ID for p in projects):
        projects.append(ProjectRecord(id=SCRATCH_PROJECT_ID, directory=tempfile.gettempdir(), name="Scratch"))
    return replace(snapshot, sessions=moved, projects=tuple(projects))


def build_adapters(enabled: Iterable[str]) -> list[TranscriptHarness]:
    return [
        HARNESS_REGISTRY[harness].adapter()
        for harness in enabled
        if harness in HARNESS_REGISTRY and HARNESS_REGISTRY[harness].adapter is not None
    ]


register_harness(HarnessSpec(
    "claude", "Claude Code", "CC", ("claude",), tmux_prefix="cc", badge="CC",
    style="#f2b84b", adapter=ClaudeHarness,
))
register_harness(HarnessSpec(
    "codex", "Codex", "CX", ("codex",), tmux_prefix="cx", badge="CX",
    style="#7ee081", adapter=CodexHarness,
))


def load_harness_plugins() -> list[str]:
    """Import every in-tree harness plugin (``ocdeck/harness_plugins/*.py``).

    Plugins live in the reviewed source tree on purpose: loading modules named
    in a user config would let anything that can edit that config run code in
    OC Deck. A broken plugin is skipped with a warning, never fatal.
    """
    import importlib
    import pkgutil
    import sys as _sys

    loaded = []
    try:
        from . import harness_plugins
    except ImportError:
        return loaded
    for module in pkgutil.iter_modules(harness_plugins.__path__):
        if module.name.startswith("_"):
            continue
        try:
            importlib.import_module(f"{harness_plugins.__name__}.{module.name}")
            loaded.append(module.name)
        except (Exception, SystemExit) as error:  # noqa: BLE001 - one bad plugin must not break the deck
            print(f"ocdeck: harness plugin {module.name} failed to load: {error}", file=_sys.stderr)
    return loaded


load_harness_plugins()
