#!/usr/bin/python3
"""Keep the managed Super+O registration intact without polling or reading keys."""
import os
from pathlib import Path
import signal

PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/ocdeck-launch/"


def reconcile(settings, shortcut, home):
    changed = []
    expected = {
        "name": "Open or focus OC Deck",
        "command": str(home / ".local/bin/ocdeck-hotkey") + " --dispatch",
        "binding": "<Super>o",
    }
    for key, value in expected.items():
        if shortcut.get_string(key) != value:
            shortcut.set_string(key, value)
            changed.append(key)
    # Always merge the current desktop list, including newly installed shortcuts.
    paths = settings.get_strv("custom-keybindings")
    if PATH not in paths:
        settings.set_strv("custom-keybindings", [*paths, PATH])
        changed.append("registration")
    return changed


def main():
    os.environ["XDG_CONFIG_HOME"] = str(Path.home() / ".config")
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    settings = Gio.Settings.new("org.gnome.settings-daemon.plugins.media-keys")
    shortcut = Gio.Settings.new_with_path(
        "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding", PATH)
    pending = 0

    def repair():
        nonlocal pending
        pending = 0
        changed = reconcile(settings, shortcut, Path.home())
        if changed:
            Gio.Settings.sync()
            print("Restored OC Deck shortcut: " + ", ".join(changed), flush=True)
        return GLib.SOURCE_REMOVE

    def schedule(*_args):
        nonlocal pending
        if not pending:
            pending = GLib.timeout_add(150, repair)

    settings.connect("changed::custom-keybindings", schedule)
    shortcut.connect("changed", schedule)
    repair()
    loop = GLib.MainLoop()
    for number in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, number, lambda: loop.quit() or False)
    loop.run()


if __name__ == "__main__":
    main()
