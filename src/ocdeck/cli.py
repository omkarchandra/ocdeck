"""The ``ocdeck`` command: name the terminal tab, then run the app.

Super+O finds OC Deck by its tab title ("OC Deck"). A deck started by typing
``ocdeck`` in any shell tab would otherwise keep the shell's title and could
never be focused — and the launcher refuses to open a second deck while one
runs. Setting the title here makes every instance findable, however started.
"""

from __future__ import annotations

import fcntl
import os
import signal
import sys
import time
from pathlib import Path

DECK_TITLE = "OC Deck"
ONE_SHOT_FLAGS = ("--once", "--destinations-json", "-h", "--help")
_instance_lock: int | None = None  # held open for the life of the process


def is_remote() -> bool:
    """Started over SSH (e.g. Termius): the owner allows any number of those."""
    return bool(os.environ.get("SSH_CONNECTION"))


def instance_lock_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/ocdeck-{os.getuid()}"
    return Path(runtime) / "ocdeck" / "desktop-instance.lock"


def _holder(path: Path) -> tuple[int, str]:
    """PID and terminal recorded by the running deck ((0, "") if unknown)."""
    try:
        pid_text, _, tty = path.read_text(encoding="utf-8").partition("\n")
        return int(pid_text), tty.strip()
    except (OSError, ValueError):
        return 0, ""


def _is_deck(pid: int) -> bool:
    try:
        if Path(f"/proc/{pid}").stat().st_uid != os.getuid():
            return False
        arguments = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    return any(argument.endswith(b"/ocdeck") or argument == b"ocdeck" for argument in arguments)


def acquire_instance_lock(path: Path | None = None, *, replace: bool = False,
                          wait_seconds: float = 5.0) -> tuple[bool, str]:
    """Hold the one-deck lock. Returns (acquired, message for the user)."""
    global _instance_lock
    path = path or instance_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + wait_seconds
    signalled = False
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            pid, tty = _holder(path)
            if not replace:
                os.close(descriptor)
                where = f" (process {pid}{', terminal ' + tty if tty else ''})" if pid else ""
                return False, (f"OC Deck is already running on this desktop{where}. Press Super+O "
                               "to switch to it, or run `ocdeck --replace` to close it and open it here.")
            if not signalled:
                if not pid or not _is_deck(pid):
                    os.close(descriptor)
                    return False, "Another OC Deck holds the lock, but it could not be verified; not replacing it."
                os.kill(pid, signal.SIGTERM)
                signalled = True
            if time.monotonic() > deadline:
                os.close(descriptor)
                return False, f"The running OC Deck (process {pid}) did not close in time."
            time.sleep(0.1)
    try:
        tty = os.ttyname(0)
    except OSError:
        tty = ""
    os.ftruncate(descriptor, 0)
    os.pwrite(descriptor, f"{os.getpid()}\n{tty}\n".encode(), 0)
    _instance_lock = descriptor
    return True, ""


def set_terminal_title(title: str, tty_path: str = "/dev/tty") -> bool:
    """Set the terminal (tab) title with OSC 2; never fails loudly."""
    if not sys.stdout.isatty() and tty_path == "/dev/tty":
        return False  # --once output piped to a file: no escape codes
    try:
        descriptor = os.open(tty_path, os.O_WRONLY | os.O_NOCTTY)
    except OSError:
        return False
    try:
        os.write(descriptor, f"\x1b]2;{title}\x07".encode())
        return True
    except OSError:
        return False
    finally:
        os.close(descriptor)


def main(argv: list[str] | None = None) -> None:
    arguments = list(sys.argv[1:] if argv is None else argv)
    replace = "--replace" in arguments
    arguments = [argument for argument in arguments if argument != "--replace"]
    if not any(flag in arguments for flag in ONE_SHOT_FLAGS):
        # One desktop deck, however it was started (Super+O, a shell tab):
        # duplicates fight over terminals and refreshes. Remote decks (SSH,
        # e.g. a phone SSH client or a web terminal) have independent views.
        if not is_remote() and "--inline-tmux" not in arguments:
            acquired, message = acquire_instance_lock(replace=replace)
            if not acquired:
                print(message, file=sys.stderr)
                raise SystemExit(1)
        set_terminal_title(DECK_TITLE)
    from .app import main as app_main

    app_main(arguments)
