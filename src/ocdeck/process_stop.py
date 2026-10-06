"""Stop an agent process that runs outside any OC Deck tmux terminal.

Used for sessions such as a headless ``claude -p`` or ``codex exec`` an agent
started, or a CLI the owner started in a plain terminal. The conversation is
on disk, so stopping loses nothing; OC Deck can resume it in its own terminal.

Safety: a target is captured when the owner is asked to confirm, then on
confirmation opened as a pidfd and revalidated (same owner, same start time,
same command line) before one SIGTERM. Never SIGKILL, never a process group,
never a bare numeric PID that may have been reused.
"""
from __future__ import annotations

import os
import signal
from dataclasses import dataclass
from pathlib import Path

from .source import read_process_identity


@dataclass(frozen=True, slots=True)
class ProcessTarget:
    pid: int
    start_time: int
    args: tuple[str, ...]


def identify(pid: int, proc_root: Path = Path("/proc")) -> ProcessTarget | None:
    process = proc_root / str(pid)
    try:
        if process.stat().st_uid != os.getuid():
            return None
        args = tuple(os.fsdecode(part) for part in (process / "cmdline").read_bytes().split(b"\0") if part)
    except OSError:
        return None
    state, start_time = read_process_identity(process)
    if not args or state == "Z" or start_time <= 0:
        return None
    return ProcessTarget(pid, start_time, args)


def pidfd_open(pid: int) -> int:
    """os.pidfd_open, with a direct syscall for Python builds that lack it."""
    opener = getattr(os, "pidfd_open", None)
    if opener is not None:
        return opener(pid)
    import ctypes
    descriptor = ctypes.CDLL(None, use_errno=True).syscall(434, pid, 0)  # pidfd_open
    if descriptor < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return descriptor


def pidfd_send_signal(descriptor: int, number: int) -> None:
    """signal.pidfd_send_signal, with a direct syscall for builds that lack it."""
    sender = getattr(signal, "pidfd_send_signal", None)
    if sender is not None:
        sender(descriptor, number)
        return
    import ctypes
    if ctypes.CDLL(None, use_errno=True).syscall(424, descriptor, number, None, 0) < 0:  # pidfd_send_signal
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def stop_processes(targets: tuple[ProcessTarget, ...], proc_root: Path = Path("/proc")) -> tuple[int, int]:
    """Return (stopped, unresolved)."""
    stopped = unresolved = 0
    for target in targets:
        descriptor = None
        try:
            descriptor = pidfd_open(target.pid)
            if identify(target.pid, proc_root) != target:
                unresolved += 1
                continue
            pidfd_send_signal(descriptor, signal.SIGTERM)
            stopped += 1
        except (OSError, ValueError):
            unresolved += 1
        finally:
            if descriptor is not None:
                os.close(descriptor)
    return stopped, unresolved
