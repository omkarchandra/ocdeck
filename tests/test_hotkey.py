from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import ocdeck_hotkey as hotkey


def test_dashboard_discovery_uses_ancestry_not_title(tmp_path, monkeypatch):
    def proc(pid, args, parent):
        directory = tmp_path / str(pid)
        directory.mkdir()
        (directory / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in args) + b"\0")
        (directory / "status").write_text(f"PPid:\t{parent}\n")
    proc(200, ["/usr/bin/ptyxis", "--title", "arbitrary dynamic title"], 1)
    proc(201, ["/usr/bin/python3", str(Path.home() / ".local/bin/ocdeck")], 200)
    proc(202, ["/usr/bin/opencode", "--session", "ses_other"], 200)
    assert hotkey.dashboard_windows(tmp_path) == [(200, 201)]


def test_shortcut_uses_exact_process_focus(monkeypatch):
    monkeypatch.setattr(hotkey, "desktop_locked", lambda: False)
    focus = Mock(return_value=True)
    launch = Mock()
    monkeypatch.setattr(hotkey, "focus_dashboard_process", focus)
    monkeypatch.setattr(hotkey, "launch_new", launch)
    hotkey.focus_or_launch()
    focus.assert_called_once()
    launch.assert_not_called()


def test_exited_dashboard_window_cannot_suppress_a_fresh_launch(monkeypatch):
    monkeypatch.setattr(hotkey, "desktop_locked", lambda: False)
    monkeypatch.setattr(hotkey, "focus_dashboard_process", lambda: False)
    monkeypatch.setattr(hotkey, "dashboard_windows", lambda **kw: [])
    launch = Mock()
    monkeypatch.setattr(hotkey, "launch_new", launch)
    hotkey.focus_or_launch()
    launch.assert_called_once()


def test_focus_failure_does_not_duplicate_a_live_dashboard(monkeypatch):
    monkeypatch.setattr(hotkey, "desktop_locked", lambda: False)
    monkeypatch.setattr(hotkey, "focus_dashboard_process", lambda: False)
    monkeypatch.setattr(hotkey, "dashboard_windows", lambda **kw: [(200, 201)])
    launch = Mock()
    monkeypatch.setattr(hotkey, "launch_new", launch)
    hotkey.focus_or_launch()
    launch.assert_not_called()


def test_locked_desktop_does_not_launch_or_claim_focus(monkeypatch):
    monkeypatch.setattr(hotkey, "desktop_locked", lambda: True)
    focus = Mock()
    launch = Mock()
    monkeypatch.setattr(hotkey, "focus_dashboard_process", focus)
    monkeypatch.setattr(hotkey, "launch_new", launch)
    assert hotkey.focus_or_launch() is False
    focus.assert_not_called()
    launch.assert_not_called()


def test_native_focus_reply_must_be_confirmed_by_compositor(monkeypatch):
    monkeypatch.setattr(hotkey, "dashboard_windows", lambda: [(200, 201)])
    monkeypatch.setattr(hotkey, "inspect_windows", lambda: [{"pid": 200, "title": "OC Deck", "focused": False}])
    monkeypatch.setattr(hotkey.time, "sleep", lambda _: None)
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout="(true,)\n"))
    monkeypatch.setattr(hotkey.subprocess, "run", run)
    assert hotkey.focus_dashboard_process() is False
    assert "org.local.OCDeckPlacement.FocusPid" in run.call_args.args[0]
    monkeypatch.setattr(hotkey, "inspect_windows", lambda: [{"pid": 200, "title": "shell tab", "focused": True}])
    assert hotkey.focus_dashboard_process() is False
    monkeypatch.setattr(hotkey, "inspect_windows", lambda: [{"pid": 200, "title": "OC Deck", "focused": True}])
    assert hotkey.focus_dashboard_process() is True


def test_starting_entrypoint_prevents_duplicate_launch(tmp_path):
    for pid, arguments, parent in (
        (200, ["/usr/bin/ptyxis"], 1),
        (201, ["/usr/bin/python3", str(Path.home() / ".local/bin/ocdeck-entrypoint")], 200),
    ):
        directory = tmp_path / str(pid)
        directory.mkdir()
        (directory / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in arguments))
        (directory / "status").write_text(f"PPid:\t{parent}\n")
    assert hotkey.dashboard_windows(tmp_path) == []
    assert hotkey.dashboard_windows(tmp_path, include_starting=True) == [(200, 201)]


def test_launch_opens_a_maximized_ocdeck_window(monkeypatch):
    started = []
    monkeypatch.setattr(hotkey.subprocess, "Popen", lambda argv, **kwargs: started.append(argv))
    hotkey.launch_new()
    assert started and "--maximize" in started[0]
    # --maximize only applies to a new window; with --tab it was silently ignored.
    assert "--new-window" in started[0] and "--tab" not in started[0]
    assert "--title=OC Deck" in started[0]  # the GNOME switch extension matches this


def test_focus_tries_the_next_dashboard_when_one_cannot_be_selected(monkeypatch):
    # Regression: an OC Deck started from a plain shell tab could not be
    # selected, and Super+O gave up instead of focusing the real OC Deck window.
    monkeypatch.setattr(hotkey, "dashboard_windows", lambda **kwargs: [(10, 11), (20, 21)])
    monkeypatch.setattr(hotkey, "inspect_windows", lambda: [])
    tried = []
    monkeypatch.setattr(hotkey, "_focus_dashboard", lambda host, pid: tried.append(host) or host == 20)
    assert hotkey.focus_dashboard_process() is True
    assert tried == [10, 20]
    tried.clear()
    monkeypatch.setattr(hotkey, "_focus_dashboard", lambda host, pid: tried.append(host) and False)
    assert hotkey.focus_dashboard_process() is False
    assert tried == [10, 20]


def test_focus_helper_modules_import_with_the_system_python():
    # Regression: focus_helper.py (Super+O, and focusing direct sessions) loads
    # agent_tabs.py outside the package with /usr/bin/python3.
    import subprocess
    from pathlib import Path
    source = Path(__file__).resolve().parents[1] / "src/ocdeck"
    result = subprocess.run(
        ["/usr/bin/python3", "-c",
         f"import sys; sys.path.insert(0, {str(source)!r}); import agent_tabs; "
         "print(agent_tabs.is_managed_session('cc-x'), agent_tabs.is_managed_session('main'))"],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["True", "False"]
