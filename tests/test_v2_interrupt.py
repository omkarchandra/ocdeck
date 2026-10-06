"""x x on an OpenCode V2 session: Esc, Esc into its pane, like the owner would."""
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from ocdeck.v2_interrupt import FAILED, IDLE, INTERRUPTED, NOT_OPENCODE, interrupt_v2_turn

RUNNING_SCREEN = "Build · GPT-6 Astra\n  ■■■⬝⬝ esc interrupt        189.4K (47%)  ctrl+p commands\n"
CONFIRM_SCREEN = "Build · GPT-6 Astra\n  ■■■■⬝ esc again to interrupt  189.5K (47%)  ctrl+p commands\n"
IDLE_SCREEN = "Build · GPT-6 Astra · 8.4s · interrupted\n  /mnt/data/project   189.5K (47%)  ctrl+p commands\n"


class FakeTmux:
    """Scripted pane: each Escape advances the screen like OpenCode's TUI."""

    def __init__(self, screens, command="opencode2"):
        self.screens = list(screens)
        self.command = command
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        verb = argv[1]
        if verb == "display-message":
            return SimpleNamespace(returncode=0, stdout=self.command + "\n")
        if verb == "capture-pane":
            return SimpleNamespace(returncode=0, stdout=self.screens[0])
        if verb == "send-keys":
            if len(self.screens) > 1:
                self.screens.pop(0)
            return SimpleNamespace(returncode=0, stdout="")
        raise AssertionError(f"unexpected tmux call {argv}")

    def sent(self):
        return [argv for argv in self.calls if argv[1] == "send-keys"]


def no_sleep(_seconds):
    pass


class InterruptTests(unittest.TestCase):
    def test_running_turn_gets_exactly_two_escapes_into_the_exact_pane(self):
        tmux = FakeTmux([RUNNING_SCREEN, CONFIRM_SCREEN, IDLE_SCREEN])
        self.assertEqual(interrupt_v2_turn("oc2-ses_abc", run=tmux, sleep=no_sleep), INTERRUPTED)
        self.assertEqual(tmux.sent(), [["tmux", "send-keys", "-t", "=oc2-ses_abc:", "Escape"]] * 2)
        self.assertTrue(all(argv[argv.index("-t") + 1] == "=oc2-ses_abc:" for argv in tmux.calls))

    def test_idle_session_is_left_alone(self):
        tmux = FakeTmux([IDLE_SCREEN])
        self.assertEqual(interrupt_v2_turn("oc2-ses_abc", run=tmux, sleep=no_sleep), IDLE)
        self.assertEqual(tmux.sent(), [])

    def test_conversation_text_mentioning_the_keys_does_not_count_as_running(self):
        # Only the footer decides; an old line in the transcript must not.
        screen = "why does it say esc interrupt?\n" + "\n" * 5 + IDLE_SCREEN
        tmux = FakeTmux([screen])
        self.assertEqual(interrupt_v2_turn("oc2-ses_abc", run=tmux, sleep=no_sleep), IDLE)
        self.assertEqual(tmux.sent(), [])

    def test_a_pane_not_running_opencode_gets_no_keys(self):
        tmux = FakeTmux([RUNNING_SCREEN], command="bash")
        self.assertEqual(interrupt_v2_turn("oc2-ses_abc", run=tmux, sleep=no_sleep), NOT_OPENCODE)
        self.assertEqual(tmux.sent(), [])

    def test_only_oc_deck_v2_terminals_are_touched(self):
        tmux = FakeTmux([RUNNING_SCREEN])
        for name in ("cc-5906", "oc-ses_x", "main", "oc2"):
            self.assertEqual(interrupt_v2_turn(name, run=tmux, sleep=no_sleep), NOT_OPENCODE)
        self.assertEqual(tmux.calls, [])

    def test_no_second_escape_unless_opencode_asks_for_it(self):
        tmux = FakeTmux([RUNNING_SCREEN, RUNNING_SCREEN])  # the first Esc changed nothing
        self.assertEqual(interrupt_v2_turn("oc2-ses_abc", run=tmux, sleep=no_sleep), FAILED)
        self.assertEqual(len(tmux.sent()), 1)

    def test_already_at_the_confirm_prompt_needs_only_one_escape(self):
        tmux = FakeTmux([CONFIRM_SCREEN, IDLE_SCREEN])
        self.assertEqual(interrupt_v2_turn("oc2-ses_abc", run=tmux, sleep=no_sleep), INTERRUPTED)
        self.assertEqual(len(tmux.sent()), 1)

    def test_still_running_after_both_escapes_is_reported_as_failed(self):
        tmux = FakeTmux([RUNNING_SCREEN, CONFIRM_SCREEN, RUNNING_SCREEN])
        self.assertEqual(interrupt_v2_turn("oc2-ses_abc", run=tmux, sleep=no_sleep), FAILED)


STUB = textwrap.dedent("""\
    import os, sys, tty
    tty.setraw(0)
    def draw(*lines):
        os.write(1, b"\\x1b[2J\\x1b[H" + "\\r\\n".join(lines).encode() + b"\\r\\n")
    state = "running"
    draw("working on it", "  esc interrupt   ctrl+p commands")
    while True:
        if os.read(0, 1) == b"\\x1b":
            if state == "running":
                state = "confirm"; draw("working on it", "  esc again to interrupt   ctrl+p commands")
            elif state == "confirm":
                state = "idle"; draw("Build · interrupted", "  /project   ctrl+p commands")
    """)


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class PrivateTmuxInterruptTests(unittest.TestCase):
    """A throwaway tmux server (own socket) with a stub that behaves like OpenCode."""

    def test_two_real_escapes_interrupt_the_stub(self):
        with tempfile.TemporaryDirectory() as base:
            stub = Path(base) / "stub.py"
            stub.write_text(STUB)
            env = {**os.environ, "TMUX_TMPDIR": base, "TERM": "xterm-256color"}
            env.pop("TMUX", None)
            command = ["tmux", "-S", str(Path(base) / "test.sock"), "-f", "/dev/null"]
            run = lambda argv, **kwargs: subprocess.run([*command, *argv[1:]], env=env, **kwargs)
            launch = f"exec -a opencode2 {sys.executable} {stub}"
            try:
                self.assertEqual(run(["tmux", "new-session", "-d", "-s", "oc2-probe", "-x", "100", "-y", "10",
                                      "bash", "-c", launch]).returncode, 0)
                deadline = time.monotonic() + 5
                while "esc interrupt" not in run(["tmux", "capture-pane", "-p", "-t", "=oc2-probe:"],
                                                 capture_output=True, text=True).stdout:
                    self.assertLess(time.monotonic(), deadline, "stub never drew its footer")
                    time.sleep(0.1)
                self.assertEqual(interrupt_v2_turn("oc2-probe", run=run), INTERRUPTED)
                screen = run(["tmux", "capture-pane", "-p", "-t", "=oc2-probe:"], capture_output=True, text=True).stdout
                self.assertIn("interrupted", screen)
                self.assertEqual(interrupt_v2_turn("oc2-probe", run=run), IDLE)  # nothing left to stop
            finally:
                run(["tmux", "kill-server"])  # the private server only


if __name__ == "__main__":
    unittest.main()
