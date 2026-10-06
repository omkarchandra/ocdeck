#!/usr/bin/python3
"""Install one GNOME owner for Super+O and Super+N, preserving other shortcuts."""
import json
import os
from pathlib import Path
import subprocess
import time
import shutil

HOME = Path.home()
# Tools launched inside OpenCode otherwise inherit its private CLI profile.
os.environ["XDG_CONFIG_HOME"] = str(HOME / ".config")

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio

ROOT = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"
CUSTOM_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"
bindings = {
    ROOT + "ocdeck-launch/": ("Open or focus OC Deck", "<Super>o", str(HOME / ".local/bin/ocdeck-hotkey") + " --dispatch"),
}
settings = Gio.Settings.new("org.gnome.settings-daemon.plugins.media-keys")
shell = Gio.Settings.new("org.gnome.shell.keybindings")
paths = settings.get_strv("custom-keybindings")
previous = {}
for path in paths:
    shortcut = Gio.Settings.new_with_path(CUSTOM_SCHEMA, path)
    previous[path] = {key: shortcut.get_string(key) for key in ("name", "binding", "command")}
backup = HOME / ".local/state/ocdeck" / f"shortcuts-{time.time_ns()}.json"
backup.parent.mkdir(parents=True, exist_ok=True)
backup.write_text(json.dumps({
    "custom": previous,
    "rotate-video-lock": settings.get_strv("rotate-video-lock"),
    "rotate-video-lock-static": settings.get_strv("rotate-video-lock-static"),
    "focus-active-notification": shell.get_strv("focus-active-notification"),
}, indent=2) + "\n")
backup.chmod(0o600)

# GNOME reserves Super+O for rotation lock even on machines that are not tablets.
# Retain the dedicated hardware key, releasing only OC Deck's accelerator.
settings.set_strv("rotate-video-lock-static", [
    key for key in settings.get_strv("rotate-video-lock-static") if key.lower() != "<super>o"
])
rotation = [key for key in settings.get_strv("rotate-video-lock") if key.lower() != "<super>o"]
# GSD watches the base key, not its -static companion. Refresh its cached grab
# through that key, restoring all unrelated rotation shortcuts afterwards.
settings.set_strv("rotate-video-lock", rotation + ["<Super>o"])
Gio.Settings.sync()
time.sleep(.25)
settings.set_strv("rotate-video-lock", rotation)
Gio.Settings.sync()
time.sleep(.25)
for path, values in previous.items():
    if path not in bindings and values["binding"].lower() in {"<super>o", "<super>n"}:
        Gio.Settings.new_with_path(CUSTOM_SCHEMA, path).set_string("binding", "")
for path in bindings:
    Gio.Settings.new_with_path(CUSTOM_SCHEMA, path).set_string("binding", "")
Gio.Settings.sync()
# Re-register a shortcut whose previous grab failed, without restarting GNOME.
time.sleep(.25)
for path, (name, binding, command) in bindings.items():
    shortcut = Gio.Settings.new_with_path(CUSTOM_SCHEMA, path)
    shortcut.set_string("name", name)
    shortcut.set_string("command", command)
    shortcut.set_string("binding", binding)
    if path not in paths:
        paths.append(path)
settings.set_strv("custom-keybindings", paths)

shell.set_strv("focus-active-notification", [
    key for key in shell.get_strv("focus-active-notification") if key.lower() != "<super>n"
] + ["<Super>n"])
schema_dir = HOME / ".local/share/gnome-shell/extensions/ocdeck-switch@local/schemas"
source = (Gio.SettingsSchemaSource.new_from_directory(str(schema_dir), Gio.SettingsSchemaSource.get_default(), False)
          if (schema_dir / "gschemas.compiled").is_file() else None)
schema = source.lookup("org.gnome.shell.extensions.ocdeck-switch", False) if source else None
if schema:
    extension = Gio.Settings.new_full(schema, None, None)
    extension.set_strv("launch-switch", [
        key for key in extension.get_strv("launch-switch") if key.lower() != "<super>o"
    ])
Gio.Settings.sync()

unit_dir = HOME / ".config/systemd/user"
unit_dir.mkdir(parents=True, exist_ok=True)
shutil.copyfile(Path(__file__).resolve().parent / "systemd/user/ocdeck-permission-focus.service",
                unit_dir / "ocdeck-permission-focus.service")
shutil.copyfile(Path(__file__).resolve().parent / "systemd/user/ocdeck-shortcut-guard.service",
                unit_dir / "ocdeck-shortcut-guard.service")
guard = HOME / ".local/bin/ocdeck-shortcut-guard"
shutil.copyfile(Path(__file__).resolve().parent / "shortcut_guard.py", guard)
guard.chmod(0o755)
subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, timeout=10)
subprocess.run(["systemctl", "--user", "enable", "--now", "ocdeck-shortcut-guard.service"], check=True, timeout=10)
subprocess.run(["systemctl", "--user", "disable", "--now", "ocdeck-hotkey.service"], check=True, timeout=10)
# Retained for exact-session routing from existing native permission plugins.
# Super+N no longer sends accessibility requests to this daemon.
subprocess.run(["systemctl", "--user", "enable", "--now", "ocdeck-permission-focus.service"], check=True, timeout=10)
subprocess.run(["systemctl", "--user", "restart", "ocdeck-permission-focus.service"], check=True, timeout=10)
uuid = "ocdeck-notification-focus@local"
source_dir = Path(__file__).resolve().parent / "gnome-extension" / uuid
target_dir = HOME / ".local/share/gnome-shell/extensions" / uuid
target_dir.mkdir(parents=True, exist_ok=True)
for filename in ("metadata.json", "extension.js"):
    shutil.copyfile(source_dir / filename, target_dir / filename)
extensions = Gio.Settings.new("org.gnome.shell")
enabled = extensions.get_strv("enabled-extensions")
if uuid not in enabled:
    extensions.set_strv("enabled-extensions", enabled + [uuid])
extensions.set_strv("disabled-extensions", [
    value for value in extensions.get_strv("disabled-extensions") if value != uuid
])
Gio.Settings.sync()
try:
    result = subprocess.run(["gnome-extensions", "enable", uuid], capture_output=True, text=True, timeout=5)
    active = result.returncode == 0
except subprocess.TimeoutExpired:
    active = False
if not active:
    print("New notification-focus extension is installed for the next GNOME login.")
print("Installed GNOME Super+O and Super+N; raw evdev hotkey service disabled.")
print(f"Previous shortcut settings: {backup}")
