# Desktop integration (optional, GNOME)

OC Deck is a terminal app and needs none of this. These pieces make it feel
native on **GNOME Shell on Wayland** (developed against Shell 50, Ubuntu with
Ptyxis and tmux). They are installed with `./install.sh --desktop`.

| Piece | What it does |
|---|---|
| `ocdeck-switch@local` (extension) | Super+O focuses OC Deck or starts it, and routes notification clicks to the exact tmux window of a session. Also the D-Bus service other pieces call. |
| `ocdeck-notification-focus@local` (extension) + `ocdeck-focus-notification-daemon` (native) | Clicking an agent permission or question notification jumps straight to that session. The native daemon is a small C program; build it with `desktop/build-notification-focus.sh` (needs `gcc`, `pkg-config` and the `atspi-2`, `gio-2.0` and `glib-2.0` development packages). |
| `ocdeck-placement@local` (extension) | New session windows open at a position and size you pin with **Shift+P** in OC Deck, and the deck itself reopens at the position you pin. Optionally keeps the agents' browser on one monitor (below). |
| `ocdeck-button@local` (extension) | A "Deck" button in the top bar that does what Super+O does. |
| `install-ocdeck-shortcuts.py`, `shortcut_guard.py` | Register Super+O in GNOME's custom shortcuts, freeing it from GNOME's rotation lock, and keep the registration intact. Your previous shortcuts are backed up under `~/.local/state/ocdeck/`. |

## Install

```sh
./install.sh v2 --desktop
```

GNOME only loads **new** extensions at the next login. Log out and back in, then:

```sh
gnome-extensions enable ocdeck-switch@local ocdeck-notification-focus@local \
    ocdeck-placement@local ocdeck-button@local
```

Changing an extension's code later also needs a new login; GNOME on Wayland
cannot reload JavaScript modules it has already imported.

## Keep the agents' browser on one monitor (optional)

The agents' dedicated Chrome (see [`../agent-browser`](../agent-browser/README.md))
is a native Wayland window and cannot place itself. If you want it on a
particular monitor, create `~/.config/ocdeck/placement.json`:

```json
{ "agentBrowserMonitor": "eDP-1" }
```

The value is the monitor's connector name (shown in `gnome-control-center` →
Displays, or `xrandr`). Without this file the browser is never moved.

## Undo

- `bin/install-ocdeck-hotkey --rollback` restores the previous Super+O setup.
- `gnome-extensions disable <uuid>` turns a piece off; delete its folder under
  `~/.local/share/gnome-shell/extensions/` to remove it.
- The shortcut backups are JSON files in `~/.local/state/ocdeck/`.

## Tests

```sh
node --test desktop/tests/*.mjs
python3 -m pytest desktop/tests
```
