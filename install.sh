#!/usr/bin/env bash
# Install OC Deck.
#
#   ./install.sh [v1|v2] [--desktop]
#
# Without --desktop this installs only the terminal app (ocdeck and its helper
# commands in ~/.local/bin). --desktop adds the optional GNOME integration:
# the Super+O hotkey, the top-bar button, notification-click focusing and window
# placement (see desktop/README.md). It needs GNOME on Wayland.
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
VENV="$ROOT/.venv"
BACKEND="${OCDECK_INSTALL_BACKEND:-v2}"
DESKTOP=no

usage() {
    printf 'Usage: %s [v1|v2] [--desktop]\n' "$0" >&2
}
for argument in "$@"; do
    case $argument in
        v1|v2) BACKEND=$argument ;;
        --desktop) DESKTOP=yes ;;
        -h|--help) usage; exit 0 ;;
        *) usage; exit 2 ;;
    esac
done
if [[ $BACKEND != v1 && $BACKEND != v2 ]]; then
    usage
    exit 2
fi

SERVICE="opencode2-ready.service"
if [[ $BACKEND == v1 ]]; then
    SERVICE="opencode-web.service"
fi
# OC Deck talks to OpenCode through its CLI/API. A managed user service is
# optional; if you run one, OC Deck's launcher starts it for you.
if ! systemctl --user cat "$SERVICE" >/dev/null 2>&1; then
    printf 'Note: no %s user unit found; OC Deck will use the opencode command directly.\n' \
        "$SERVICE" >&2
fi

python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip
"$VENV/bin/pip" install --editable "$ROOT"

mkdir -p "$HOME/.local/bin"

install_link() {
    local source=$1 target=$2
    if [[ -L $target && $(readlink "$target") == "$source" ]]; then
        return
    fi
    if [[ -e $target || -L $target ]]; then
        printf 'Refusing to replace drifted launcher: %s\n' "$target" >&2
        exit 1
    fi
    ln -s "$source" "$target"
}

install_link "$VENV/bin/ocdeck" "$HOME/.local/bin/ocdeck"
install_link "$ROOT/bin/oc_agent_web" "$HOME/.local/bin/oc_agent_web"
install_link "$ROOT/bin/ocdeck-entrypoint" "$HOME/.local/bin/ocdeck-entrypoint"
install_link "$ROOT/bin/ocdeck-backend" "$HOME/.local/bin/ocdeck-backend"

# Initialize the selector (v1 or v2) before anything depends on it.
"$HOME/.local/bin/ocdeck-backend" "$BACKEND"

if [[ $DESKTOP != yes ]]; then
    printf 'Installed OC Deck with %s. Run: ocdeck\n' "$BACKEND"
    printf 'For the GNOME hotkey, top-bar button and notification focus, run: %s %s --desktop\n' "$0" "$BACKEND"
    exit 0
fi

install_link "$ROOT/bin/ocdeck-permission-watcher" "$HOME/.local/bin/ocdeck-permission-watcher"
# Super+O runs from a verified snapshot, not the live checkout, so repo edits
# cannot break the shortcut (bin/install-ocdeck-hotkey --rollback undoes it).
"$ROOT/bin/install-ocdeck-hotkey"

mkdir -p "$HOME/.config/systemd/user"
install -m 0644 "$ROOT/ocdeck-hotkey.service" \
    "$HOME/.config/systemd/user/ocdeck-hotkey.service"
install -m 0644 "$ROOT/systemd/ocdeck-permission-watcher.service" \
    "$HOME/.config/systemd/user/ocdeck-permission-watcher.service"

# GNOME extensions. GNOME only loads new extensions at the next login.
EXTENSIONS="$HOME/.local/share/gnome-shell/extensions"
mkdir -p "$EXTENSIONS"
for extension in ocdeck-switch@local ocdeck-notification-focus@local \
        ocdeck-placement@local ocdeck-button@local; do
    source_dir="$ROOT/desktop/gnome-extensions/$extension"
    target_dir="$EXTENSIONS/$extension"
    mkdir -p "$target_dir"
    cp -R "$source_dir/." "$target_dir/"
    if [[ -d $target_dir/schemas ]]; then
        glib-compile-schemas "$target_dir/schemas"
    fi
done
systemctl --user daemon-reload
systemctl --user enable ocdeck-permission-watcher.service
/usr/bin/python3 "$ROOT/desktop/install-ocdeck-shortcuts.py"

printf 'Installed OC Deck with %s and the GNOME integration.\n' "$BACKEND"
printf 'Log out and back in so GNOME loads the extensions, then enable them:\n'
printf '  gnome-extensions enable ocdeck-switch@local ocdeck-notification-focus@local ocdeck-placement@local ocdeck-button@local\n'
printf 'Notification-click focusing also needs a small native helper: see desktop/README.md\n'
