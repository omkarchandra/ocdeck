#!/usr/bin/env python3
"""Stateless OC Deck launch/focus target for the GNOME Super+O shortcut."""
import ast
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# KEEP THIS FILE SELF-CONTAINED (stdlib only). Super+O runs it from an installed
# snapshot (~/.local/lib/ocdeck-hotkey/, see bin/install-ocdeck-hotkey) together
# with focus_helper.py and ptyxis_tabs.py, so edits to the live repo cannot break
# the shortcut. tests/test_hotkey_chain.py enforces this.
HERE = Path(__file__).resolve().parent


def _workspace():
    configured = os.environ.get("OCDECK_WORKSPACE")
    if configured:
        return Path(configured).expanduser()
    try:  # written by the installer next to the snapshot
        return Path((HERE / "workspace").read_text(encoding="utf-8").strip()).expanduser()
    except OSError:
        return HERE  # running from the repo: the checkout is the workspace


WORKSPACE = _workspace()

DBUS_DEST = "org.local.OCDeckSwitch"
DBUS_PATH = "/org/local/OCDeckSwitch"
DECK_TITLE = "OC Deck"


def dashboard_windows(proc_root=Path("/proc"), *, include_starting=False):
    """Find dashboard hosts by process ancestry, independent of terminal titles."""
    expected = {
        str(Path.home() / ".local/bin/ocdeck"),
        str(WORKSPACE / ".venv/bin/ocdeck"),
    }
    if include_starting:
        expected.update({
            str(Path.home() / ".local/bin/ocdeck-entrypoint"),
            str(WORKSPACE / "bin/ocdeck-entrypoint"),
        })

    def arguments(pid):
        try:
            return [os.fsdecode(value) for value in (proc_root / str(pid) / "cmdline").read_bytes().split(b"\0") if value]
        except OSError:
            return []

    hosts = {}
    for process in proc_root.iterdir():
        if not process.name.isdigit():
            continue
        try:
            if process.stat().st_uid != os.getuid():
                continue
            argv = arguments(process.name)
            if not argv or not (argv[0] in expected or (len(argv) > 1 and argv[1] in expected)):
                continue
            if any(option in argv for option in ("--once", "--help", "--version")):
                continue
            pid = int(process.name)
            seen = set()
            while pid > 1 and pid not in seen:
                seen.add(pid)
                parent_args = arguments(pid)
                if parent_args and Path(parent_args[0]).name == "ptyxis":
                    hosts.setdefault(pid, int(process.name))
                    break
                status = (proc_root / str(pid) / "status").read_text()
                pid = next(int(line.split()[1]) for line in status.splitlines() if line.startswith("PPid:"))
        except (OSError, ValueError, StopIteration):
            continue
    return sorted(hosts.items())


def focus_dashboard_process():
    """Ask the compositor to focus a live dashboard, then verify it.

    Every dashboard window is tried in turn, starting after the focused one:
    one that cannot be selected (e.g. OC Deck started from a plain shell tab,
    whose window title is not "OC Deck") must not block the others.
    """
    windows = dashboard_windows()
    if not windows:
        return False
    pids = [pid for pid, _ in windows]
    current = next((row["pid"] for row in inspect_windows() if row.get("focused")), None)
    start = (pids.index(current) + 1) % len(windows) if current in pids else 0
    for offset in range(len(windows)):
        host, dashboard = windows[(start + offset) % len(windows)]
        if _focus_dashboard(host, dashboard):
            return True
    return False


def _focus_dashboard(host, dashboard):
    try:
        selected = subprocess.run([
            "/usr/bin/python3", str(helper_path()), "--dashboard-tab", str(host),
        ], timeout=5, check=False)
        # If the OC Deck tab cannot be selected by title (e.g. the tab was
        # renamed), still raise the window that runs the deck: a visible
        # window beats a shortcut that silently does nothing.
        tab_selected = selected.returncode == 0
        focused = subprocess.run([
            "/usr/bin/gdbus", "call", "--session", "--dest", "org.local.OCDeckPlacement",
            "--object-path", "/org/local/OCDeckPlacement", "--method",
            "org.local.OCDeckPlacement.FocusPid", str(dashboard),
        ], capture_output=True, text=True, timeout=3)
        if focused.returncode == 0 and focused.stdout.strip() == "(true,)":
            for _ in range(10):
                if any(row.get("pid") == host and row.get("focused")
                       and (row.get("title") == DECK_TITLE or not tab_selected)
                       for row in inspect_windows()):
                    return True
                time.sleep(.05)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return False


def helper_path():
    """focus_helper.py next to this file (installed), else the repo copy."""
    beside = HERE / "focus_helper.py"
    return beside if beside.is_file() else WORKSPACE / "src/ocdeck/focus_helper.py"


def notify_failure(message):
    """Failures must be visible: a desktop notification, not only a log line."""
    print(message, file=sys.stderr, flush=True)
    try:
        subprocess.run(
            ["/usr/bin/notify-send", "--app-name=OC Deck", "--urgency=normal",
             "Super+O could not focus OC Deck", message + "\nRun: ocdeck-hotkey --check"],
            timeout=3, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def health_check():
    """Check every link Super+O depends on, without changing focus. Returns (ok, lines)."""
    lines, ok = [], True

    def report(passed, text):
        nonlocal ok
        ok = ok and passed
        lines.append(("PASS " if passed else "FAIL ") + text)

    helper = helper_path()
    report(helper.is_file(), f"focus helper present: {helper}")
    probe = subprocess.run(
        ["/usr/bin/python3", "-c",
         "import sys; sys.path.insert(0, sys.argv[1]); import focus_helper, ptyxis_tabs",
         str(helper.parent)],
        capture_output=True, text=True, timeout=20, check=False,
    ) if helper.is_file() else None
    report(bool(probe) and probe.returncode == 0,
           "helper imports under the system Python" + ("" if not probe or not probe.returncode
                                                       else f": {probe.stderr.strip().splitlines()[-1:]}"))
    placement = subprocess.run(
        ["/usr/bin/gdbus", "introspect", "--session", "--dest", "org.local.OCDeckPlacement",
         "--object-path", "/org/local/OCDeckPlacement"],
        capture_output=True, text=True, timeout=5, check=False,
    )
    report(placement.returncode == 0 and "FocusPid" in placement.stdout, "GNOME placement extension answers")
    report(bool(inspect_windows()) or not dashboard_windows(), "GNOME switch extension lists windows")
    decks = dashboard_windows()
    lines.append(f"INFO {len(decks)} OC Deck instance(s) running")
    for host, deck in decks:
        titled = any(row.get("pid") == host and row.get("title") == DECK_TITLE for row in inspect_windows())
        lines.append(f"INFO deck pid {deck} in window {host}: tab titled {DECK_TITLE!r}: {'yes' if titled else 'no (window fallback)'}")
    return ok, lines


def inspect_windows():
    try:
        result = subprocess.run([
            "/usr/bin/gdbus", "call", "--session", "--dest", DBUS_DEST,
            "--object-path", DBUS_PATH, "--method", DBUS_DEST + ".Inspect",
        ], capture_output=True, text=True, timeout=2)
        if result.returncode == 0:
            rows = json.loads(ast.literal_eval(result.stdout)[0])
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    except (OSError, subprocess.TimeoutExpired, ValueError, SyntaxError, TypeError, IndexError):
        pass
    return []


def desktop_locked():
    try:
        result = subprocess.run(
            ["/usr/bin/gdbus", "call", "--session", "--dest", "org.gnome.ScreenSaver",
             "--object-path", "/org/gnome/ScreenSaver", "--method", "org.gnome.ScreenSaver.GetActive"],
            capture_output=True, text=True, timeout=2,
        )
        return result.returncode == 0 and result.stdout.strip() == "(true,)"
    except (OSError, subprocess.TimeoutExpired):
        return False


def launch_new():
    argv = [
        "/usr/bin/ptyxis", "--standalone", "--new-window", "--title=OC Deck",
        # Open large: the agents board needs the width for its RUNTIME column.
        # Ptyxis applies --maximize only to a new window; --tab ignored it.
        "--maximize",
        "--working-directory=" + str(WORKSPACE),
        "--", "/usr/bin/bash", "-lc",
        'exec "$HOME/.local/bin/ocdeck-entrypoint"',
    ]
    env = dict(os.environ)
    if env.get("XDG_CONFIG_HOME") == str(Path.home() / ".config/ocdeck-v2-runtime"):
        env["XDG_CONFIG_HOME"] = str(Path.home() / ".config")
    subprocess.Popen(argv, start_new_session=True, env=env)


def focus_or_launch():
    # A Ptyxis window can survive after Deck exits, or retain its original
    # launcher title while showing another shell. Process ownership is the
    # authority; a successful Shell D-Bus call alone does not prove Deck exists.
    if desktop_locked():
        print("Desktop is locked; OC Deck can be focused after unlocking.", file=sys.stderr, flush=True)
        return False
    if focus_dashboard_process():
        print("Verified OC Deck window focus through GNOME", flush=True)
        return True
    elif dashboard_windows(include_starting=True):
        notify_failure("OC Deck is running but its window could not be focused")
        return False
    else:
        print("Launching new OC Deck", flush=True)
        launch_new()
        return True


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["--check"]:
        ok, lines = health_check()
        print("\n".join(lines))
        return 0 if ok else 1
    if arguments not in ([], ["--dispatch"]):
        print("Usage: ocdeck-hotkey [--dispatch | --check]", file=sys.stderr)
        return 2
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    # Coalesce taps during startup without retaining any keyboard/modifier state.
    with (runtime / "ocdeck-launch.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        had_window = bool(dashboard_windows())
        if not focus_or_launch():
            return 1
        if not had_window:
            for _ in range(900):
                if dashboard_windows() and focus_dashboard_process():
                    return 0
                time.sleep(.1)
            print("OC Deck launch did not produce a live dashboard within 90 seconds", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
