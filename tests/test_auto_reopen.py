"""a on a running OpenCode session that lacks --auto reopens it with --auto."""
import asyncio
import dataclasses
import unittest
from pathlib import Path
from unittest import mock

from textual.widgets import DataTable

from ocdeck.app import OCDeckApp, opencode_process_has_auto
from ocdeck.source import OpenCodeProcess
from tests.test_app import MultiProjectSource


class RunningInTerminal(MultiProjectSource):
    """s2 runs in OC Deck's own terminal oc2-s2 / oc-s2."""

    async def collect(self):
        snapshot = await super().collect()
        prefix = "oc2" if self.backend == "v2" else "oc"
        sessions = tuple(
            dataclasses.replace(session, terminals=(f"{prefix}-s2",)) if session.id == "s2" else session
            for session in snapshot.sessions
        )
        return dataclasses.replace(snapshot, sessions=sessions)


class AutoReopenTests(unittest.IsolatedAsyncioTestCase):
    async def press_a_twice(self, backend: str, *, has_auto: bool):
        source = RunningInTerminal()
        source.backend = backend
        app = OCDeckApp(source, auto_refresh=False)
        notices: list[str] = []
        process = OpenCodeProcess(pid=77, session_id="s2", tty="/dev/pts/7", start_time=5, backend=backend)
        async with app.run_test(size=(140, 42)) as pilot:
            await asyncio.sleep(0.1)
            await pilot.press("1")
            await pilot.pause()
            app.query_one("#sessions-table", DataTable).move_cursor(row=0)
            await pilot.pause()
            name = f"{'oc2' if backend == 'v2' else 'oc'}-s2"
            app.notify = lambda message, **kwargs: notices.append(str(message))
            with mock.patch("ocdeck.app.read_opencode_processes", return_value=(process,)), \
                    mock.patch("ocdeck.app.opencode_process_has_auto", return_value=has_auto), \
                    mock.patch.object(app, "_tmux_has_session", side_effect=lambda target: target == name), \
                    mock.patch.object(app, "_tmux_kill_session", return_value=True) as kill, \
                    mock.patch.object(app, "_run_opencode", return_value=True) as run, \
                    mock.patch.object(app, "_attach_live_terminal", return_value=True) as attach:
                app.action_open_auto()
                first = (kill.call_count, run.call_count)
                app.action_open_auto()
                await pilot.pause()
        return name, first, kill, run, attach, notices

    async def test_v2_session_without_auto_is_reopened_with_auto_after_confirming(self) -> None:
        name, first, kill, run, attach, notices = await self.press_a_twice("v2", has_auto=False)
        self.assertEqual(first, (0, 0))  # the first press only asks
        self.assertEqual(notices[0], "Press a again to reopen this session with auto-approve (--auto); "
                                     "the agent keeps running in the OpenCode service")
        kill.assert_called_once_with(name)
        arguments = run.call_args.args[0]
        self.assertEqual(arguments[1:], ["--session", "s2", "--auto"])
        self.assertEqual(run.call_args.kwargs["tmux_name"], name)
        attach.assert_not_called()

    async def test_v1_warns_that_a_running_turn_stops(self) -> None:
        _, _, kill, run, _, notices = await self.press_a_twice("v1", has_auto=False)
        self.assertEqual(notices[0], "Press a again to reopen this session with auto-approve (--auto); "
                                     "restarting its terminal stops a running turn")
        kill.assert_called_once_with("oc-s2")
        self.assertIn("--auto", run.call_args.args[0])

    async def test_a_session_already_in_auto_mode_is_just_focused(self) -> None:
        _, _, kill, run, attach, notices = await self.press_a_twice("v2", has_auto=True)
        kill.assert_not_called()
        run.assert_not_called()
        self.assertEqual(attach.call_count, 2)
        self.assertFalse(any("--auto" in notice for notice in notices))


def test_detects_auto_from_the_process_command_line(tmp_path: Path) -> None:
    for pid, argv in ((1, b"opencode2\0/work\0--session\0ses_a\0--auto\0"),
                      (2, b"opencode2\0/work\0--session\0ses_a\0"),
                      (3, b"opencode2\0/work\0--title\0--auto-thing\0")):
        (tmp_path / str(pid)).mkdir()
        (tmp_path / str(pid) / "cmdline").write_bytes(argv)
    assert opencode_process_has_auto(1, tmp_path)
    assert not opencode_process_has_auto(2, tmp_path)
    assert not opencode_process_has_auto(3, tmp_path)  # only the exact flag counts
    assert not opencode_process_has_auto(4, tmp_path)  # gone
