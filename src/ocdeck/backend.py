"""Read the backend selection shared by OC Deck's installed launchers."""

from __future__ import annotations

import fcntl
import os
import stat
from contextlib import ExitStack
from pathlib import Path


def desktop_config_home() -> Path:
    """The managed OpenCode profile is private to OpenCode, not desktop tools."""
    default = Path.home() / ".config"
    configured = Path(os.environ.get("XDG_CONFIG_HOME", default)).expanduser()
    return default if configured == default / "ocdeck-v2-runtime" else configured


def read_saved_backend() -> str | None:
    override = os.environ.get("OCDECK_BACKEND_FILE")
    if override == "":
        raise ValueError("OCDECK_BACKEND_FILE must be a non-empty path")
    path = (
        Path(override)
        if override is not None
        else desktop_config_home() / "ocdeck/backend"
    ).expanduser()
    try:
        path.lstat()
    except FileNotFoundError:
        if override is None:
            return None
        raise ValueError(f"Configured OC Deck backend selector is missing: {path}") from None

    with ExitStack() as opened:
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        opened.callback(os.close, directory)
        metadata = os.fstat(directory)
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise ValueError("OC Deck backend selector directory must be owner-only")
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        lock = os.open(".backend.lock", flags, dir_fd=directory)
        opened.callback(os.close, lock)
        metadata = os.fstat(lock)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
        ):
            raise ValueError("OC Deck backend selector lock must be owner-only")
        fcntl.flock(lock, fcntl.LOCK_SH)
        selector = os.open(path.name, flags, dir_fd=directory)
        opened.callback(os.close, selector)
        metadata = os.fstat(selector)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
            or metadata.st_size != 3
        ):
            raise ValueError("OC Deck backend selector must be an owner-only regular file")
        value = os.read(selector, 4)
        if value not in (b"v1\n", b"v2\n"):
            raise ValueError("OC Deck backend selector must contain exactly 'v1' or 'v2'")
        return value[:2].decode("ascii")
