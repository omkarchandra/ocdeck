from dataclasses import replace
from unittest.mock import Mock
from types import SimpleNamespace

from ocdeck import direct_terminal as terminal
from ocdeck.source import OpenCodeProcess


def renderer():
    return OpenCodeProcess(1234, "ses_selected", "/dev/pts/7", 4567, "v2")


def test_close_uses_confirmed_process_and_waits_for_exit(monkeypatch):
    target = renderer()
    opened = Mock(return_value=99)
    sent = Mock()
    closed = Mock()
    monkeypatch.setattr(terminal.os, "pidfd_open", opened, raising=False)
    monkeypatch.setattr(terminal.os, "close", closed)
    monkeypatch.setattr(terminal.signal, "pidfd_send_signal", sent, raising=False)
    monkeypatch.setattr(terminal, "_read_opencode_process", lambda *_: target)
    monkeypatch.setattr(terminal.select, "poll", lambda: Mock(poll=Mock(return_value=[(99, 1)])))
    assert terminal.close_renderers((target,)) == (1, 0)
    opened.assert_called_once_with(1234)
    sent.assert_called_once_with(99, terminal.signal.SIGTERM)
    closed.assert_called_once_with(99)


def test_reused_pid_changed_session_or_server_cannot_be_closed(monkeypatch):
    target = renderer()
    sent = Mock()
    monkeypatch.setattr(terminal.os, "pidfd_open", lambda _: 99, raising=False)
    monkeypatch.setattr(terminal.os, "close", lambda _: None)
    monkeypatch.setattr(terminal.signal, "pidfd_send_signal", sent, raising=False)
    for current in (None, replace(target, start_time=9999), replace(target, session_id="ses_other"), replace(target, backend="v1")):
        monkeypatch.setattr(terminal, "_read_opencode_process", lambda *_, value=current: value)
        assert terminal.close_renderers((target,)) == (0, 1)
    sent.assert_not_called()


def test_discovery_is_exact_session_and_backend(monkeypatch):
    records = (renderer(), replace(renderer(), session_id="ses_other"), replace(renderer(), tty=""))
    discover = Mock(return_value=records)
    monkeypatch.setattr(terminal, "read_opencode_processes", discover)
    assert terminal.direct_renderers("ses_selected", "v2") == (renderer(),)
    discover.assert_called_once_with(backend="v2")


def test_direct_close_requires_two_presses_and_reconfirms_changed_process(monkeypatch):
    from ocdeck import app as app_module
    from ocdeck.models import SessionRecord

    app = app_module.OCDeckApp(SimpleNamespace(backend="v2"), auto_refresh=False)
    selected = SessionRecord("ses_selected", "New session", "/work", "project", 1, 1, instance_count=1)
    app._guard_next_read_only = lambda: False
    app._current_session = lambda: selected
    app._tmux_has_session = lambda _: False
    app.notify = Mock()
    app.set_timer = Mock()
    app._stop_direct_job_worker = Mock()
    current = [renderer()]
    monkeypatch.setattr(app_module, "direct_renderers", lambda *_: tuple(current))
    app.action_stop_job()
    app._stop_direct_job_worker.assert_not_called()
    current[0] = replace(current[0], start_time=9999)
    app.action_stop_job()
    app._stop_direct_job_worker.assert_not_called()
    app.action_stop_job()
    app._stop_direct_job_worker.assert_called_once_with(tuple(current))
    assert app.stop_confirm == ""
