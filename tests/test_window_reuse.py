"""o/Enter on a live session reuses its open window instead of stacking new ones.

Regression: attaches switched to tmux exact targets ("-t =name"), but window
lookup still expected the bare name, so every open spawned another window.
"""
import subprocess
import unittest
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from ocdeck.app import OCDeckApp

HELPER = Path(__file__).resolve().parents[1] / "src/ocdeck/focus_helper.py"
SYSTEM_PYTHON = Path("/usr/bin/python3")  # the helper needs system gi, like OC Deck runs it


def ptyxis_pid_for_tmux(name: str):
    script = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('focus_helper', {str(HELPER)!r})\n"
        "module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)\n"
        "print(module.ptyxis_pid_for_tmux(sys.argv[1]))\n"
    )
    output = subprocess.run([str(SYSTEM_PYTHON), "-c", script, name],
                            capture_output=True, text=True, timeout=20, check=True).stdout.strip()
    return None if output == "None" else int(output)


def fake_window(target: str) -> subprocess.Popen:
    # A process whose cmdline looks like an OC Deck viewer window.
    return subprocess.Popen(
        ["ptyxis", "-c", "sleep 5; true", "--standalone", "--tab", "--",
         "/usr/bin/tmux", "attach-session", "-t", target],
        executable="/bin/sh",
    )


@pytest.mark.skipif(not SYSTEM_PYTHON.is_file(), reason="system python not available")
def test_focus_helper_finds_exact_target_windows():
    name = f"cc-reuse-probe-{time.monotonic_ns()}"
    for target in (f"={name}", name):
        window = fake_window(target)
        try:
            time.sleep(0.1)
            assert ptyxis_pid_for_tmux(name) == window.pid
        finally:
            window.kill()
            window.wait()
    assert ptyxis_pid_for_tmux(name) is None


def app() -> OCDeckApp:
    instance = OCDeckApp.__new__(OCDeckApp)
    instance.inline_tmux = False
    instance.private = False
    instance.notify = mock.Mock()
    instance._request_refresh = mock.Mock()
    return instance


def test_shell_not_found_still_tries_the_ptyxis_helper():
    deck = app()
    deck._focus_tmux_via_shell = mock.Mock(return_value=False)
    deck._focus_tmux_via_ptyxis = mock.Mock(return_value=True)
    assert deck._raise_existing_window("cc-a", "t") is True
    deck._focus_tmux_via_ptyxis.return_value = False
    assert deck._raise_existing_window("cc-a", "t") is False
    deck._focus_tmux_via_ptyxis.return_value = None
    assert deck._raise_existing_window("cc-a", "t") is False


def test_an_already_open_terminal_never_gets_a_second_window():
    deck = app()
    session = SimpleNamespace(instance_count=1, terminals=("cc-a",), title="t", directory="/tmp")
    deck._raise_existing_window = mock.Mock(return_value=False)
    deck._tmux_client_attached = mock.Mock(return_value=True)
    deck._tmux_attach = mock.Mock(return_value=True)
    deck._session_directory = lambda s: "/tmp"
    assert deck._attach_live_terminal(session) is True
    deck._tmux_attach.assert_not_called()


def test_launch_on_an_existing_terminal_reuses_its_open_window():
    deck = app()
    deck._tmux_has_session = mock.Mock(return_value=True)
    deck._tmux_session_matches = mock.Mock(return_value=True)
    deck._raise_existing_window = mock.Mock(return_value=False)
    deck._tmux_client_attached = mock.Mock(return_value=True)
    deck._tmux_attach = mock.Mock(return_value=True)
    assert deck._launch_tmux("oc2-ses_x", Path("/tmp"), ["opencode"], accent="#fff", label="p")
    deck._tmux_attach.assert_not_called()


def test_a_closed_window_is_reopened():
    deck = app()
    session = SimpleNamespace(instance_count=1, terminals=("cc-a",), title="t", directory="/tmp")
    deck._raise_existing_window = mock.Mock(return_value=False)
    deck._tmux_client_attached = mock.Mock(return_value=False)
    deck._tmux_attach = mock.Mock(return_value=True)
    deck._session_directory = lambda s: "/tmp"
    assert deck._attach_live_terminal(session) is True
    deck._tmux_attach.assert_called_once_with("cc-a", Path("/tmp"), "t")


class RemoteClientTests(unittest.TestCase):
    """A viewer on the owner's phone (tmux over SSH) never blocks a desktop viewer."""

    def fake_proc(self, root, pid, environment):
        (root / str(pid)).mkdir()
        (root / str(pid) / "environ").write_bytes(b"\0".join(environment) + b"\0")

    def test_remote_and_local_clients_are_told_apart(self):
        import tempfile
        from pathlib import Path
        from ocdeck.app import tmux_client_is_remote
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            self.fake_proc(root, 10, [b"HOME=/home/o", b"SSH_CONNECTION=10.0.0.2 5 10.0.0.1 22"])
            self.fake_proc(root, 11, [b"HOME=/home/o", b"DISPLAY=:0"])
            self.assertTrue(tmux_client_is_remote(10, root))
            self.assertFalse(tmux_client_is_remote(11, root))
            self.assertFalse(tmux_client_is_remote(12, root))  # unreadable: treated as local

    def test_only_local_clients_block_a_new_viewer(self):
        from types import SimpleNamespace
        from unittest import mock
        from ocdeck.app import OCDeckApp
        app = OCDeckApp.__new__(OCDeckApp)
        listing = SimpleNamespace(returncode=0, stdout="10\n11\n")
        with mock.patch("ocdeck.app.subprocess.run", return_value=listing):
            with mock.patch("ocdeck.app.tmux_client_is_remote", side_effect=lambda pid: True):
                self.assertFalse(app._tmux_client_attached("oc2-s"))  # phone only
            with mock.patch("ocdeck.app.tmux_client_is_remote", side_effect=lambda pid: pid == 10):
                self.assertTrue(app._tmux_client_attached("oc2-s"))  # a desktop window too


class PinViewerSlotTests(unittest.TestCase):
    def app(self, terminals=("oc2-s",)):
        from ocdeck.app import OCDeckApp
        app = OCDeckApp.__new__(OCDeckApp)
        app._guard_next_read_only = lambda: False
        app._current_session = lambda: SimpleNamespace(terminals=terminals)
        app._tmux_has_session = lambda name: True
        app.notify = mock.Mock()
        return app

    def test_shift_p_pins_the_selected_session_window(self):
        app = self.app()
        reply = SimpleNamespace(returncode=0, stdout=b"(true,)\n")
        with mock.patch("ocdeck.app.subprocess.run", return_value=reply) as run:
            app.action_pin_viewer_slot()
        argv = run.call_args.args[0]
        self.assertEqual(argv[-2:], ["org.local.OCDeckPlacement.SetAgentReference", "oc2-s"])
        self.assertEqual(app.notify.call_args.args[0],
                         "New session windows will open at this window's position and size")

    def test_shift_p_needs_an_open_window(self):
        app = self.app(terminals=())
        with mock.patch("ocdeck.app.subprocess.run") as run:
            app.action_pin_viewer_slot()
        run.assert_not_called()
        self.assertIn("Open this session's window first", app.notify.call_args.args[0])
        app = self.app()
        with mock.patch("ocdeck.app.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=b"(false,)")):
            app.action_pin_viewer_slot()
        self.assertIn("Could not find this session's window", app.notify.call_args.args[0])
