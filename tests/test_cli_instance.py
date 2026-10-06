"""One desktop OC Deck at a time; SSH and web-terminal decks have independent views."""
import os
import subprocess
import sys
import textwrap
import time
from unittest import mock

import pytest

from ocdeck import cli


@pytest.fixture(autouse=True)
def private_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.setattr(cli, "_instance_lock", None)
    yield


def release():
    if cli._instance_lock is not None:
        os.close(cli._instance_lock)
        cli._instance_lock = None


def test_a_second_desktop_deck_is_refused_and_told_where_the_first_is():
    assert cli.acquire_instance_lock() == (True, "")
    first = cli._instance_lock
    cli._instance_lock = None
    acquired, message = cli.acquire_instance_lock()
    assert not acquired
    assert f"process {os.getpid()}" in message
    assert "Super+O" in message and "ocdeck --replace" in message
    os.close(first)
    assert cli.acquire_instance_lock()[0]  # free again once the first exits
    release()


def fake_deck(tmp_path):
    """A real process named .../ocdeck that holds the lock until signalled."""
    (tmp_path / "bin").mkdir()
    script = tmp_path / "bin" / "ocdeck"
    script.write_text(textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(os.path.dirname(cli.__file__) + '/..')!r})
        from ocdeck import cli
        assert cli.acquire_instance_lock()[0]
        print("ready", flush=True)
        time.sleep(60)
    """))
    child = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, text=True,
                             env={**os.environ})
    assert child.stdout.readline().strip() == "ready"
    return child


def test_replace_closes_the_running_deck_and_takes_over(tmp_path):
    child = fake_deck(tmp_path)
    try:
        assert not cli.acquire_instance_lock()[0]
        assert cli.acquire_instance_lock(replace=True) == (True, "")
        assert child.wait(timeout=5) == -15  # SIGTERM, never SIGKILL
        release()
    finally:
        child.kill()


def test_replace_never_signals_a_lock_holder_that_is_not_a_deck(tmp_path):
    path = cli.instance_lock_path()
    path.parent.mkdir(parents=True)
    path.write_text(f"{os.getppid()}\n/dev/pts/0\n")  # pytest's parent, not a deck
    import fcntl
    holder = os.open(path, os.O_RDWR)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        acquired, message = cli.acquire_instance_lock(replace=True)
        assert not acquired and "could not be verified" in message
    finally:
        os.close(holder)


def test_remote_decks_are_never_blocked(monkeypatch):
    assert cli.acquire_instance_lock()[0]  # a desktop deck is running
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.2 5555 10.0.0.1 22")
    with mock.patch("ocdeck.app.main") as app_main, mock.patch.object(cli, "set_terminal_title"):
        cli.main([])
        cli.main([])
    assert app_main.call_count == 2
    release()


@pytest.mark.parametrize("replace", [False, True])
def test_web_terminal_deck_can_open_while_desktop_lock_remains_held(replace):
    assert cli.acquire_instance_lock()[0]
    holder = cli.instance_lock_path().read_text()
    arguments = ["--backend", "v2", "--inline-tmux"]
    try:
        with (
            mock.patch("ocdeck.app.main") as app_main,
            mock.patch.object(cli, "set_terminal_title"),
            mock.patch.object(cli.os, "kill") as kill,
        ):
            cli.main((["--replace"] if replace else []) + arguments)
            app_main.assert_called_once_with(arguments)
            kill.assert_not_called()
        assert cli.instance_lock_path().read_text() == holder
        assert not cli.acquire_instance_lock()[0]
    finally:
        release()


def test_desktop_main_exits_when_a_deck_runs_and_replace_is_not_passed_on(capsys):
    assert cli.acquire_instance_lock()[0]
    first = cli._instance_lock
    cli._instance_lock = None
    with mock.patch("ocdeck.app.main") as app_main, mock.patch.object(cli, "set_terminal_title"):
        with pytest.raises(SystemExit) as exit_info:
            cli.main(["--backend", "v2"])
        assert exit_info.value.code == 1
        assert "already running on this desktop" in capsys.readouterr().err
        app_main.assert_not_called()
        os.close(first)
        cli.main(["--replace", "--backend", "v2"])
        app_main.assert_called_once_with(["--backend", "v2"])
    release()


def test_one_shot_commands_never_take_the_lock():
    assert cli.acquire_instance_lock()[0]
    with mock.patch("ocdeck.app.main") as app_main:
        cli.main(["--once"])
    app_main.assert_called_once_with(["--once"])
    release()
