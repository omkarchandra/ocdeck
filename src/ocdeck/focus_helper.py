from __future__ import annotations

import os
import sys
from pathlib import Path

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

sys.path.insert(0, str(Path(__file__).resolve().parent))

# KEEP THIS FILE SELF-CONTAINED. It runs under the system Python from an
# installed snapshot (Super+O), next to ptyxis_tabs.py only: import nothing
# from the ocdeck package. tests/test_hotkey_chain.py enforces this.


def _parent_pid(pid: int) -> int:
    try:
        for line in Path(f"/proc/{pid}/status").read_text(errors="replace").splitlines():
            if line.startswith("PPid:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def ptyxis_pid_for_process(process_pid: int) -> int | None:
    """Walk a process's ancestry to the Ptyxis window process that hosts it."""
    seen: set[int] = set()
    pid = process_pid
    while pid > 1 and pid not in seen:
        seen.add(pid)
        try:
            argv0 = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0", 1)[0]
        except OSError:
            argv0 = b""
        if Path(os.fsdecode(argv0)).name == "ptyxis":
            return pid
        pid = _parent_pid(pid)
    return None


EXIT_ERROR = 2
EXIT_MISSING = 3
PLACEMENT_BUS = "org.local.OCDeckPlacement"
PLACEMENT_PATH = "/org/local/OCDeckPlacement"


def ptyxis_pid_for_tmux(session_name: str) -> int | None:
    # Attaches use tmux's exact-match target "=name"; older windows use "name".
    targets = {os.fsencode(session_name), b"=" + os.fsencode(session_name)}
    for entry in sorted(Path("/proc").iterdir(), key=lambda path: path.name):
        if not entry.name.isdigit():
            continue
        try:
            arguments = [
                value
                for value in (entry / "cmdline").read_bytes().split(b"\0")
                if value
            ]
        except OSError:
            continue
        if not arguments or Path(os.fsdecode(arguments[0])).name != "ptyxis":
            continue
        if b"--standalone" not in arguments or b"attach-session" not in arguments:
            continue
        if any(
            argument == b"-t" and index + 1 < len(arguments)
            and arguments[index + 1] in targets
            for index, argument in enumerate(arguments)
        ):
            return int(entry.name)
    return None


def bus_name_for_pid(connection: Gio.DBusConnection, pid: int) -> str:
    names_reply = connection.call_sync(
        "org.freedesktop.DBus",
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
        "ListNames",
        None,
        GLib.VariantType.new("(as)"),
        Gio.DBusCallFlags.NONE,
        3000,
        None,
    )
    for name in names_reply.unpack()[0]:
        if not name.startswith(":"):
            continue
        try:
            pid_reply = connection.call_sync(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "GetConnectionUnixProcessID",
                GLib.Variant("(s)", (name,)),
                GLib.VariantType.new("(u)"),
                Gio.DBusCallFlags.NONE,
                1000,
                None,
            )
        except GLib.Error:
            continue
        if pid_reply.unpack()[0] == pid:
            return name
    return ""


def activate_ptyxis_pid(pid: int) -> int:
    try:
        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus_name = bus_name_for_pid(connection, pid)
        if not bus_name:
            return EXIT_ERROR
        connection.call_sync(
            bus_name,
            "/org/gnome/Ptyxis",
            "org.gtk.Application",
            "Activate",
            GLib.Variant("(a{sv})", ({},)),
            None,
            Gio.DBusCallFlags.NONE,
            3000,
            None,
        )
    except GLib.Error:
        return EXIT_ERROR
    return 0


def focus_pid_via_placement_extension(process_pid: int) -> bool:
    """Focus the window whose client process hosts the renderer (any app)."""
    try:
        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = connection.call_sync(
            PLACEMENT_BUS,
            PLACEMENT_PATH,
            PLACEMENT_BUS,
            "FocusPid",
            GLib.Variant("(i)", (process_pid,)),
            GLib.VariantType.new("(b)"),
            Gio.DBusCallFlags.NONE,
            3000,
            None,
        )
    except GLib.Error:
        return False
    return bool(reply.unpack()[0])


def focus_process(process_pid: int) -> int:
    hosted = ptyxis_pid_for_process(process_pid)
    if hosted is not None:
        return activate_ptyxis_pid(hosted)
    return 0 if focus_pid_via_placement_extension(process_pid) else EXIT_MISSING


def main(argv=None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["--dashboard-tab"] and len(arguments) == 2:
        from ptyxis_tabs import select_dashboard_tab

        try:
            return 0 if select_dashboard_tab(int(arguments[1])) else EXIT_MISSING
        except ValueError:
            return EXIT_ERROR
    if arguments[:1] == ["--pid"] and len(arguments) == 2:
        try:
            return focus_process(int(arguments[1]))
        except ValueError:
            return EXIT_ERROR
    if len(arguments) != 1 or not arguments[0] or arguments[0].startswith("-"):
        return EXIT_ERROR
    pid = ptyxis_pid_for_tmux(arguments[0])
    if pid is None:
        return EXIT_MISSING
    # On Wayland a bare D-Bus Activate is ignored by focus-stealing prevention;
    # the shell extension raises the window itself.
    if focus_pid_via_placement_extension(pid):
        return 0
    return activate_ptyxis_pid(pid)


if __name__ == "__main__":
    raise SystemExit(main())
