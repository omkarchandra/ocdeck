"""x x on a Claude Code session: one Esc into its pane, never anything else."""
import asyncio
from types import SimpleNamespace
from unittest import mock

from ocdeck.app import OCDeckApp
from ocdeck.claude_interrupt import NOT_CLAUDE, interrupt_claude_turn
from ocdeck.v2_interrupt import FAILED, IDLE, INTERRUPTED
from tests.test_v2_interrupt import FakeTmux, no_sleep

# Last lines of Claude Code's screen, copied from a real session (v2.1.293).
RUNNING = "● 31 is a Mersenne prime.\n" + "─" * 40 + "\n❯ \n" + "─" * 40 + \
    "\n  ⏵⏵ auto mode on (shift+tab to cycle) · esc to interrupt · ← for agents\n"
IDLE_SCREEN = "  ⎿  Interrupted · What should Claude do instead?\n" + "─" * 40 + "\n❯ \n" + "─" * 40 + \
    "\n  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents\n"
DIALOG = "Do you want to proceed?\n ❯ 1. Yes\n   4. No\n Esc to cancel · Tab to amend\n"
QUOTED = "● the footer says esc to interrupt when a turn runs\n" + "─" * 40 + "\n❯ \n  ⏵⏵ manual mode on\n"


def test_a_running_turn_gets_exactly_one_escape_into_the_exact_pane():
    tmux = FakeTmux([RUNNING, IDLE_SCREEN], command="claude")
    assert interrupt_claude_turn("cc-9d2d9af9", run=tmux, sleep=no_sleep) == INTERRUPTED
    assert tmux.sent() == [["tmux", "send-keys", "-t", "=cc-9d2d9af9:", "Escape"]]
    assert all(argv[argv.index("-t") + 1] == "=cc-9d2d9af9:" for argv in tmux.calls)


def test_an_idle_session_gets_no_keys():
    tmux = FakeTmux([IDLE_SCREEN], command="claude")
    assert interrupt_claude_turn("cc-x", run=tmux, sleep=no_sleep) == IDLE
    assert tmux.sent() == []


def test_a_permission_dialog_is_never_answered_by_an_interrupt():
    tmux = FakeTmux([DIALOG], command="claude")  # Esc here would mean "No"
    assert interrupt_claude_turn("cc-x", run=tmux, sleep=no_sleep) == IDLE
    assert tmux.sent() == []


def test_only_the_footer_decides_not_quoted_conversation_text():
    tmux = FakeTmux([QUOTED], command="claude")
    assert interrupt_claude_turn("cc-x", run=tmux, sleep=no_sleep) == IDLE
    assert tmux.sent() == []


def test_a_foreign_terminal_or_a_non_claude_pane_gets_nothing():
    assert interrupt_claude_turn("main", run=FakeTmux([RUNNING]), sleep=no_sleep) == NOT_CLAUDE
    assert interrupt_claude_turn("oc2-ses_a", run=FakeTmux([RUNNING]), sleep=no_sleep) == NOT_CLAUDE
    tmux = FakeTmux([RUNNING], command="bash")
    assert interrupt_claude_turn("cc-x", run=tmux, sleep=no_sleep) == NOT_CLAUDE
    assert tmux.sent() == []


def test_an_interrupt_that_does_not_take_effect_is_reported_and_never_repeated():
    tmux = FakeTmux([RUNNING], command="claude")  # the screen never changes
    assert interrupt_claude_turn("cc-x", run=tmux, sleep=no_sleep) == FAILED
    assert len(tmux.sent()) == 1


def test_a_tmux_failure_never_raises():
    def broken(argv, **kwargs):
        raise OSError("no tmux")
    assert interrupt_claude_turn("cc-x", run=broken, sleep=no_sleep) == FAILED


def run_worker(result, *, killed=True):
    notices = []
    fake = SimpleNamespace(
        notify=lambda text, **kwargs: notices.append(text),
        _tmux_kill_session=lambda name: killed,
        _request_refresh=lambda **kwargs: None,
    )
    with mock.patch("ocdeck.app.interrupt_claude_turn", return_value=result):
        asyncio.run(OCDeckApp._interrupt_claude_worker.__wrapped__(fake, "cc-x")
                    if hasattr(OCDeckApp._interrupt_claude_worker, "__wrapped__")
                    else _call_worker(fake))
    return notices


def _call_worker(fake):
    return OCDeckApp._interrupt_claude_worker(fake, "cc-x")


def test_the_worker_says_what_happened():
    assert "Interrupted the agent in cc-x; the session stays open" in run_worker(INTERRUPTED)[0]
    assert "Nothing was running; closed cc-x" in run_worker(IDLE)[0]
    assert "could not be closed" in run_worker(IDLE, killed=False)[0]
    assert "not showing Claude Code" in run_worker(NOT_CLAUDE)[0]
    assert "open it and press Esc" in run_worker(FAILED)[0]
