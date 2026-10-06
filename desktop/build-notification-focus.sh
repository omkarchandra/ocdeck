#!/usr/bin/env bash
set -euo pipefail
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
BUILD="${XDG_CACHE_HOME:-$HOME/.cache}/ocdeck-notification-focus"
mkdir -p "$BUILD" "$HOME/.local/bin"

if [[ -n ${OCDECK_NATIVE_HEADERS:-} ]]; then
    # Optional extracted distro development packages; link installed runtime libs.
    PREFIX="$OCDECK_NATIVE_HEADERS/usr"
    CFLAGS=(
        "-I$PREFIX/include" "-I$PREFIX/include/glib-2.0"
        "-I$PREFIX/lib/x86_64-linux-gnu/glib-2.0/include"
        "-I$PREFIX/include/at-spi-2.0" "-I$PREFIX/include/dbus-1.0"
        "-I$PREFIX/lib/x86_64-linux-gnu/dbus-1.0/include"
    )
    LIBS=(-l:libatspi.so.0 -l:libgio-2.0.so.0 -l:libgobject-2.0.so.0 -l:libglib-2.0.so.0 -l:libdbus-1.so.3)
else
    flags="$(pkg-config --cflags atspi-2 gio-2.0 glib-2.0)"
    libraries="$(pkg-config --libs atspi-2 gio-2.0 glib-2.0)"
    read -r -a CFLAGS <<< "$flags"
    read -r -a LIBS <<< "$libraries"
fi
cc -O2 -Wall -Wextra -Werror "${CFLAGS[@]}" \
    "$ROOT/focus_permission_notification_daemon.c" \
    -o "$BUILD/ocdeck-focus-notification-daemon" "${LIBS[@]}"
install -m 0755 "$BUILD/ocdeck-focus-notification-daemon" \
    "$HOME/.local/bin/ocdeck-focus-notification-daemon.new"
mv -f "$HOME/.local/bin/ocdeck-focus-notification-daemon.new" \
    "$HOME/.local/bin/ocdeck-focus-notification-daemon"
printf 'Built and installed the native notification action router.\n'
