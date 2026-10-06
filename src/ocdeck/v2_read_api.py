"""One discovery-only SDK worker per process, with bounded IPC and crash backoff."""
from __future__ import annotations

import atexit
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import threading
import time

# An optional wrapper around `opencode2` (for example one that pins a protected
# configuration). When the deck talks to exactly this binary it may use the
# persistent read bridge (plugins/v2/read-api-bridge.mjs, needs `npm ci` there).
# Create the wrapper at this path, or point OCDECK_MANAGED_CLIENT at it.
MANAGED_CLIENT = os.environ.get("OCDECK_MANAGED_CLIENT") or str(Path.home() / ".local/bin/opencode2-client")


def find_opencode2() -> str | None:
    """The OpenCode V2 command: explicit override, the managed wrapper, then PATH."""
    for candidate in (os.environ.get("OCDECK_OPENCODE2_BIN"), MANAGED_CLIENT, shutil.which("opencode2")):
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None
READ_OPERATIONS = frozenset({
    "v2.health.get", "v2.session.list", "v2.session.active", "v2.project.list",
    "v2.debug.location.list", "v2.permission.request.list", "v2.form.request.list",
    "v2.agent.list", "v2.mcp.list", "v2.session.message.list", "v2.shell.list",
})
MAX_FRAME_BYTES = 1024 * 1024 + 4096


class ReadAPIError(RuntimeError):
    pass


def use_read_api(binary, server, operation):
    return binary == MANAGED_CLIENT and not server and operation in READ_OPERATIONS


class ReadAPI:
    def __init__(self, command=None):
        script = Path(os.environ.get("OCDECK_READ_BRIDGE") or
                      Path(__file__).resolve().parents[2] / "plugins/v2/read-api-bridge.mjs")
        self.command = command or [shutil.which("node") or "/usr/bin/node", str(script)]
        self.process = None
        self.lock = threading.Lock()
        self.sequence = 0
        self.retry_at = 0.0
        self.failures = 0

    def _close(self):
        process, self.process = self.process, None
        if process is None:
            return
        if process.stdin:
            process.stdin.close()
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)
        if process.stdout:
            process.stdout.close()

    def close(self):
        with self.lock:
            self._close()

    def request(self, operation, *, params=None, location=None, timeout=10):
        if operation not in READ_OPERATIONS:
            raise ReadAPIError("Unsupported background read operation")
        # Serialization keeps a timed-out response from being consumed by another
        # caller; the failed worker is closed before another request can use it.
        with self.lock:
            if time.monotonic() < self.retry_at:
                raise ReadAPIError("OpenCode read worker unavailable; retry delayed")
            try:
                if self.process is None or self.process.poll() is not None:
                    self._close()
                    environment = dict(os.environ)
                    environment["XDG_CONFIG_HOME"] = str(Path.home() / ".config/ocdeck-v2-runtime")
                    self.process = subprocess.Popen(
                        self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, bufsize=0, env=environment,
                    )
                self.sequence += 1
                message = json.dumps({
                    "id": self.sequence, "operation": operation, "params": params or {},
                    "location": location, "timeoutMs": max(1, int(timeout * 1000)),
                }, separators=(",", ":")).encode() + b"\n"
                if len(message) > 65536:
                    raise ValueError("Read request too large")
                self.process.stdin.write(message)
                deadline = time.monotonic() + timeout + 1
                data = bytearray()
                while not data.endswith(b"\n"):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not select.select([self.process.stdout], [], [], remaining)[0]:
                        raise TimeoutError("Read worker timed out")
                    chunk = os.read(self.process.stdout.fileno(), 65536)
                    if not chunk:
                        raise OSError("Read worker closed")
                    data.extend(chunk)
                    if len(data) > MAX_FRAME_BYTES:
                        raise ValueError("Read response too large")
                response = json.loads(data)
                if not isinstance(response, dict) or response.get("id") != self.sequence:
                    raise ValueError("Read response did not match the request")
                self.failures = 0
            except (OSError, ValueError, TypeError, TimeoutError, subprocess.SubprocessError) as error:
                self._close()
                self.retry_at = time.monotonic() + min(30, 2 * 2 ** min(self.failures, 4))
                self.failures += 1
                raise ReadAPIError("OpenCode read worker unavailable; retry delayed") from error
            if response.get("error"):
                # Keep the worker alive: it owns service discovery and backoff.
                raise ReadAPIError("Managed OpenCode service unavailable; active state stale")
            return response.get("data")


_default = ReadAPI()
atexit.register(_default.close)


def read_api(operation, **kwargs):
    return _default.request(operation, **kwargs)
