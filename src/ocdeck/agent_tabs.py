from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


try:
    from .harnesses import managed_tmux_prefixes
except ImportError:
    # focus_helper.py loads this file standalone with the system Python (the
    # Super+O path), outside the package; keep it importable there.
    def managed_tmux_prefixes() -> tuple[str, ...]:
        return ("oc-", "oc2-", "cc-", "cx-")

# Terminals OC Deck launches: OpenCode (oc-, oc2-) plus every registered harness.
MANAGED_SESSION_PREFIXES = managed_tmux_prefixes()
TMUX_CLIENT_FORMAT = "#{client_tty}\t#{client_pid}\t#{client_session}"
DEFAULT_PROC_ROOT = Path("/proc")
DEFAULT_TMUX_BIN = "tmux"
ATTACH_WINDOW_GRACE_SECONDS = 1.5


@dataclass(frozen=True)
class AttachedAgentTab:
    session: str
    client_tty: str
    client_pid: int
    window_pid: int


@dataclass(frozen=True)
class PurgeReport:
    detach_requested: int
    windows: int
    windows_closed: int
    windows_remaining: int
    sessions_running: int
    # Windows left open because they still host other, unrelated tabs.
    windows_shared: int = 0


def is_managed_session(name: str) -> bool:
    return bool(name) and name.startswith(managed_tmux_prefixes())


def parse_tmux_clients(output: str) -> list[tuple[str, int, str]]:
    """Parse ``tmux list-clients`` rows into (tty, pid, session) tuples."""
    clients: list[tuple[str, int, str]] = []
    for line in output.splitlines():
        fields = line.split("\t")
        if len(fields) != 3:
            continue
        tty, pid_text, session = (field.strip() for field in fields)
        if not tty.startswith("/") or not session:
            continue
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid <= 0:
            continue
        clients.append((tty, pid, session))
    return clients


def _arguments(process: Path) -> tuple[str, ...]:
    try:
        raw = (process / "cmdline").read_bytes()
    except OSError:
        return ()
    return tuple(os.fsdecode(value) for value in raw.split(b"\0") if value)


def _parent_pid(process: Path) -> int:
    try:
        status = (process / "status").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    for line in status.splitlines():
        if not line.startswith("PPid:"):
            continue
        try:
            return int(line.split()[1])
        except (IndexError, ValueError):
            return 0
    return 0


def _is_ocdeck_ptyxis_window(arguments: Sequence[str]) -> bool:
    return (
        bool(arguments)
        and Path(arguments[0]).name == "ptyxis"
        and "--standalone" in arguments
        and "attach-session" in arguments
    )


def window_pid_for_client(
    client_pid: int, proc_root: Path = DEFAULT_PROC_ROOT
) -> int | None:
    """Walk a tmux client's ancestry to the OC Deck Ptyxis window that owns it."""
    seen: set[int] = set()
    pid = client_pid
    while pid > 1 and pid not in seen:
        seen.add(pid)
        process = proc_root / str(pid)
        if _is_ocdeck_ptyxis_window(_arguments(process)):
            return pid
        pid = _parent_pid(process)
    return None


def ptyxis_pid_for_process(
    process_pid: int, proc_root: Path = DEFAULT_PROC_ROOT
) -> int | None:
    """Walk any process ancestry to the Ptyxis window process that hosts it."""
    seen: set[int] = set()
    pid = process_pid
    while pid > 1 and pid not in seen:
        seen.add(pid)
        process = proc_root / str(pid)
        arguments = _arguments(process)
        if arguments and Path(arguments[0]).name == "ptyxis":
            return pid
        pid = _parent_pid(process)
    return None


def collect_attached_tabs(
    output: str, proc_root: Path = DEFAULT_PROC_ROOT
) -> list[AttachedAgentTab]:
    tabs: list[AttachedAgentTab] = []
    for tty, client_pid, session in parse_tmux_clients(output):
        if not is_managed_session(session):
            continue
        window_pid = window_pid_for_client(client_pid, proc_root)
        if window_pid is None:
            continue
        tabs.append(
            AttachedAgentTab(
                session=session,
                client_tty=tty,
                client_pid=client_pid,
                window_pid=window_pid,
            )
        )
    return tabs


def read_tmux_clients(tmux_bin: str = DEFAULT_TMUX_BIN) -> str:
    try:
        result = subprocess.run(
            [tmux_bin, "list-clients", "-F", TMUX_CLIENT_FORMAT],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.decode("utf-8", errors="replace")


def attached_agent_tabs(
    tmux_bin: str = DEFAULT_TMUX_BIN, proc_root: Path = DEFAULT_PROC_ROOT
) -> list[AttachedAgentTab]:
    return collect_attached_tabs(read_tmux_clients(tmux_bin), proc_root)


def _process_exists(pid: int) -> bool:
    return (DEFAULT_PROC_ROOT / str(pid)).exists()


def _detach_client(client_tty: str, tmux_bin: str = DEFAULT_TMUX_BIN) -> bool:
    try:
        result = subprocess.run(
            [tmux_bin, "detach-client", "-t", client_tty],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _tmux_has_session(name: str, tmux_bin: str = DEFAULT_TMUX_BIN) -> bool:
    try:
        result = subprocess.run(
            [tmux_bin, "has-session", "-t", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _children_by_parent(proc_root: Path) -> dict[int, list[int]]:
    children: dict[int, list[int]] = {}
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return children
    for entry in entries:
        if entry.name.isdigit():
            children.setdefault(_parent_pid(entry), []).append(int(entry.name))
    return children


def window_hosts_other_tabs(
    window_pid: int, detached_clients: set[int], proc_root: Path = DEFAULT_PROC_ROOT
) -> bool:
    """True when a Ptyxis window still runs processes besides the detached clients.

    Tabs of one Ptyxis window share its process, so terminating the window
    would also kill unrelated tabs (for example an agent CLI running directly
    in a shell tab). Ptyxis' own helper processes do not count as tabs.
    """
    children = _children_by_parent(proc_root)
    pending = list(children.get(window_pid, ()))
    seen: set[int] = set()
    while pending:
        pid = pending.pop()
        if pid in seen or pid in detached_clients:
            continue
        seen.add(pid)
        arguments = _arguments(proc_root / str(pid))
        if arguments and Path(arguments[0]).name.startswith("ptyxis"):
            pending.extend(children.get(pid, ()))
            continue
        return True
    return False


def _await_windows(
    windows: Sequence[int],
    process_exists: Callable[[int], bool],
    sleep: Callable[[float], None],
    grace_seconds: float,
    interval: float,
) -> set[int]:
    remaining = {pid for pid in windows if process_exists(pid)}
    if not remaining:
        return remaining
    attempts = max(1, int(grace_seconds / interval))
    for attempt in range(attempts):
        sleep(interval)
        remaining = {pid for pid in remaining if process_exists(pid)}
        if not remaining:
            break
    return remaining


def purge_attached_tabs(
    tabs: Sequence[AttachedAgentTab],
    *,
    tmux_bin: str = DEFAULT_TMUX_BIN,
    detach_client: Callable[[str], bool] | None = None,
    has_session: Callable[[str], bool] | None = None,
    process_exists: Callable[[int], bool] = _process_exists,
    send_signal: Callable[[int, int], None] = os.kill,
    sleep: Callable[[float], None] = time.sleep,
    grace_seconds: float = ATTACH_WINDOW_GRACE_SECONDS,
    interval: float = 0.05,
    hosts_other_tabs: Callable[[int, set[int]], bool] = window_hosts_other_tabs,
) -> PurgeReport:
    """Detach OC Deck terminal windows, never the tmux sessions themselves.

    Each window's client is detached first so Ptyxis closes the tab through its
    normal exit action. Windows that survive the grace period are terminated so
    the tabs disappear; their tmux sessions keep running in the background.
    """
    detach = detach_client or (lambda tty: _detach_client(tty, tmux_bin))
    session_alive = has_session or (lambda name: _tmux_has_session(name, tmux_bin))

    windows = tuple(sorted({tab.window_pid for tab in tabs}))
    sessions = tuple(sorted({tab.session for tab in tabs}))
    detach_requested = sum(1 for tab in tabs if detach(tab.client_tty))

    remaining = _await_windows(windows, process_exists, sleep, grace_seconds, interval)
    clients = {tab.client_pid for tab in tabs}
    shared = {pid for pid in remaining if hosts_other_tabs(pid, clients)}
    remaining -= shared
    if remaining:
        for pid in remaining:
            try:
                send_signal(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                continue
        remaining = _await_windows(
            remaining, process_exists, sleep, grace_seconds, interval
        )

    return PurgeReport(
        detach_requested=detach_requested,
        windows=len(windows),
        windows_closed=len(windows) - len(remaining) - len(shared),
        windows_remaining=len(remaining) + len(shared),
        sessions_running=sum(1 for name in sessions if session_alive(name)),
        windows_shared=len(shared),
    )
