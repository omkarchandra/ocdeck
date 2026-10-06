"""Close only the selected native renderer, never its server or parent shell."""

from __future__ import annotations

import os
from pathlib import Path
import select
import signal

from .process_stop import pidfd_open, pidfd_send_signal
from .source import OpenCodeProcess, _read_opencode_process, read_opencode_processes


def direct_renderers(session_id: str, backend: str) -> tuple[OpenCodeProcess, ...]:
    if not session_id or backend not in {"v1", "v2"}:
        return ()
    return tuple(process for process in read_opencode_processes(backend=backend)
                 if process.session_id == session_id and process.tty and process.start_time > 0)


def close_renderers(targets: tuple[OpenCodeProcess, ...]) -> tuple[int, int]:
    """Use pidfds and revalidate the confirmed process identity before signalling.

    Return (closed, unresolved). Never escalate to SIGKILL or signal by title,
    directory, process group, shared API process, or a reusable numeric PID.
    """
    closed = 0
    unresolved = 0
    for target in targets:
        descriptor = None
        try:
            if not target.session_id or not target.tty or target.start_time <= 0:
                unresolved += 1
                continue
            descriptor = pidfd_open(target.pid)
            current = _read_opencode_process(Path("/proc") / str(target.pid), os.getuid())
            if current != target:
                unresolved += 1
                continue
            pidfd_send_signal(descriptor, signal.SIGTERM)
            poller = select.poll()
            poller.register(descriptor, select.POLLIN)
            if poller.poll(1500):
                closed += 1
            else:
                unresolved += 1
        except ProcessLookupError:
            closed += 1
        except (OSError, AttributeError):
            unresolved += 1
        finally:
            if descriptor is not None:
                os.close(descriptor)
    return closed, unresolved
