"""Stopping agent CLIs that run outside OC Deck's tmux terminals."""
import os
import subprocess
import sys
import time
from dataclasses import replace

from ocdeck.harnesses import ClaudeHarness, LiveProcess
from ocdeck.process_stop import identify, stop_processes


def sleeper():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    # Right after fork the child is mid-exec and /proc may not show its
    # command line yet; wait until it is the program we started.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        target = identify(child.pid)
        if target is not None and target.args[-1] == "import time; time.sleep(60)":
            return child
        time.sleep(0.01)
    child.kill()
    raise AssertionError("the test process never started")


def test_stops_exactly_the_confirmed_process():
    child = sleeper()
    try:
        target = identify(child.pid)
        assert target is not None and target.pid == child.pid
        assert stop_processes((target,)) == (1, 0)
        assert child.wait(timeout=5) == -15  # SIGTERM, never SIGKILL
    finally:
        child.kill()


def test_a_changed_identity_is_never_signalled():
    child = sleeper()
    try:
        target = identify(child.pid)
        # A reused PID or a different program: start time or argv differ.
        for stale in (replace(target, start_time=target.start_time - 1),
                      replace(target, args=("claude", "--resume", "other"))):
            assert stop_processes((stale,)) == (0, 1)
        assert child.poll() is None  # still running
    finally:
        child.kill()
        child.wait()


def test_an_exited_or_foreign_process_is_not_a_target():
    child = sleeper()
    child.kill()
    child.wait()
    assert identify(child.pid) is None
    assert identify(1) is None or os.getuid() == 0  # init belongs to root


def write_transcript(root, directory, session_id):
    path = root / directory.strip("/").replace("/", "-") / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"type":"user","cwd":"%s","sessionId":"%s","timestamp":"2026-09-29T10:00:00Z",'
                    '"message":{"content":"hi"}}\n' % (directory, session_id))


def test_only_unambiguous_processes_are_stoppable(tmp_path):
    for session_id in ("aaa", "bbb"):
        write_transcript(tmp_path, "/project", session_id)
    adapter = ClaudeHarness(tmp_path, "/bin/claude")
    adapter.collect(processes=[
        LiveProcess(10, "/project", ("claude", "--resume", "aaa")),  # names its session
        LiveProcess(11, "/elsewhere", ("claude",)),                   # no transcript there
    ], tmux={})
    assert adapter.stoppable_pids.get("aaa") == (10,)
    # Two id-less processes attributed to the same session by folder: a guess,
    # so neither may be stopped.
    adapter.collect(processes=[
        LiveProcess(20, "/project", ("claude",)),
        LiveProcess(21, "/project", ("claude",)),
    ], tmux={})
    assert all(not pids for pids in adapter.stoppable_pids.values())
    # A single id-less process in that folder is unambiguous.
    adapter.collect(processes=[LiveProcess(30, "/project", ("claude",))], tmux={})
    assert (30,) in adapter.stoppable_pids.values()
