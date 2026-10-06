"""The real-terminal fixture must never inherit an operator's live tmux socket."""
from types import SimpleNamespace
from unittest import mock

from tests.e2e_keys import Sandbox


def test_every_tmux_command_pins_the_disposable_socket_and_scrubs_environment(tmp_path):
    box = Sandbox.__new__(Sandbox)
    box.socket = tmp_path / "server.sock"
    box.env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    with mock.patch.dict("os.environ", {"TMUX": "/live/socket,1,0", "DISPLAY": ":0",
                                      "DBUS_SESSION_BUS_ADDRESS": "unix:path=/live/bus",
                                      "FIXTURE_SECRET": "must-not-inherit"}), \
            mock.patch("tests.e2e_keys.subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
        for operation in ("new-session", "capture-pane", "send-keys", "kill-server"):
            box.tmux(operation)
            assert run.call_args.args[0] == ["tmux", "-S", str(box.socket), "-f", "/dev/null", operation]
            assert run.call_args.kwargs["env"] == box.env
            assert {"TMUX", "DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "FIXTURE_SECRET"}.isdisjoint(run.call_args.kwargs["env"])


def test_cleanup_uses_the_same_socket_bound_helper(tmp_path):
    box = Sandbox.__new__(Sandbox)
    box.base = tmp_path
    with mock.patch.object(box, "tmux") as tmux:
        box.close(keep=True)
    tmux.assert_called_once_with("kill-server")
    assert tmp_path.exists()
