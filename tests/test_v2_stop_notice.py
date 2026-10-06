"""x x on an OpenCode V2 session interrupts its turn (Esc, Esc) instead of
killing the terminal, which never stopped the agent in the OpenCode service."""
import asyncio
import unittest
from unittest import mock

from textual.widgets import DataTable

from ocdeck.app import OCDeckApp
from ocdeck.v2_interrupt import FAILED, IDLE, INTERRUPTED
from tests.test_app import MultiProjectSource


class V2StopTests(unittest.IsolatedAsyncioTestCase):
    async def stop_twice(self, backend: str, result: str = INTERRUPTED):
        source = MultiProjectSource()
        source.backend = backend
        app = OCDeckApp(source, auto_refresh=False)
        notices: list[str] = []
        killed: list[str] = []
        interrupted: list[str] = []

        def interrupt(name):
            interrupted.append(name)
            return result

        async with app.run_test(size=(140, 42)) as pilot:
            await asyncio.sleep(0.1)
            await pilot.press("1")
            await pilot.pause()
            app.query_one("#sessions-table", DataTable).move_cursor(row=0)
            await pilot.pause()
            name = app._session_tmux_name_for_id("s2")
            app._tmux_kill_session = lambda target: killed.append(target) or True
            app._tmux_has_session = lambda target: target == name
            app.notify = lambda message, **kwargs: notices.append(str(message))
            with mock.patch("ocdeck.app.interrupt_v2_turn", interrupt):
                app.action_stop_job()
                app.action_stop_job()
                await app.workers.wait_for_complete()
                await asyncio.sleep(0.1)
        return killed, interrupted, notices

    async def test_v2_interrupts_the_turn_and_keeps_the_session(self) -> None:
        killed, interrupted, notices = await self.stop_twice("v2")
        self.assertEqual(killed, [])  # the terminal and session stay
        self.assertEqual(interrupted, ["oc2-s2"])
        self.assertEqual(notices[0], "Press x again to interrupt the agent in oc2-s2 "
                                     "(Esc twice, as in OpenCode; the session stays open)")
        self.assertEqual(notices[-1], "Interrupted the agent in oc2-s2; the session stays open")

    async def test_v2_says_so_when_nothing_is_running_or_it_cannot_confirm(self) -> None:
        killed, _, notices = await self.stop_twice("v2", IDLE)
        self.assertEqual(killed, ["oc2-s2"])  # idle: stopping closes the terminal
        self.assertEqual(notices[-1], "Nothing was running; closed oc2-s2 (o reopens it)")
        _, _, notices = await self.stop_twice("v2", FAILED)
        self.assertEqual(notices[-1], "Could not confirm the interrupt in oc2-s2; open it and press Esc twice")

    async def test_v1_still_stops_the_tmux_job(self) -> None:
        killed, interrupted, notices = await self.stop_twice("v1")
        self.assertEqual(killed, ["oc-s2"])
        self.assertEqual(interrupted, [])
        self.assertEqual(notices[0], "Press x again to stop tmux job oc-s2")
        self.assertEqual(notices[-1], "Stopped tmux job oc-s2")

    async def headless_stop_twice(self, backend: str, reply=(True, "")):
        """A session with no tmux or direct terminal, e.g. an agent's `opencode run`."""
        source = MultiProjectSource()
        source.backend = backend
        calls: list[str] = []

        async def interrupt_session(session_id):
            calls.append(session_id)
            return reply

        source.interrupt_session = interrupt_session
        app = OCDeckApp(source, auto_refresh=False)
        notices: list[str] = []
        async with app.run_test(size=(140, 42)) as pilot:
            await asyncio.sleep(0.1)
            await pilot.press("1")
            await pilot.pause()
            app.query_one("#sessions-table", DataTable).move_cursor(row=0)
            await pilot.pause()
            app._tmux_has_session = lambda target: False
            app.notify = lambda message, **kwargs: notices.append(str(message))
            with mock.patch("ocdeck.app.direct_renderers", return_value=()):
                app.action_stop_job()
                self.assertEqual(calls, [])  # the first press only asks
                app.action_stop_job()
                await app.workers.wait_for_complete()
                await asyncio.sleep(0.1)
        return calls, notices

    async def test_v2_without_a_terminal_interrupts_through_the_service(self) -> None:
        calls, notices = await self.headless_stop_twice("v2")
        self.assertEqual(calls, ["s2"])
        self.assertEqual(notices[0], "Press x again to interrupt this session's agent in the OpenCode "
                                     "service (the session stays open)")
        self.assertEqual(notices[-1], "Interrupted the agent in the OpenCode service; the session stays open")

    async def test_v2_without_a_terminal_reports_idle_and_errors(self) -> None:
        _, notices = await self.headless_stop_twice("v2", (False, ""))
        self.assertEqual(notices[-1], "Nothing is running in this session")
        _, notices = await self.headless_stop_twice("v2", (False, "OpenCode V2 API timed out"))
        self.assertEqual(notices[-1], "Could not interrupt the agent: OpenCode V2 API timed out")

    async def test_v1_without_a_terminal_still_says_there_is_none(self) -> None:
        calls, notices = await self.headless_stop_twice("v1")
        self.assertEqual(calls, [])
        self.assertEqual(notices[-1], "No live terminal is attached to this session")


class InterruptSessionSourceTests(unittest.IsolatedAsyncioTestCase):
    def source(self, backend="v2"):
        from ocdeck.source import DashboardSource
        source = DashboardSource.__new__(DashboardSource)
        source.backend = backend
        return source

    async def test_calls_the_stable_interrupt_operation(self) -> None:
        from ocdeck.source import v2_api_operation
        source = self.source()
        source._v2_api_json = mock.AsyncMock(return_value={"interrupted": True})
        self.assertEqual(await source.interrupt_session("ses_abc123"), (True, ""))
        source._v2_api_json.assert_awaited_once_with("v2.session.interrupt", params={"sessionID": "ses_abc123"})
        # Verified on OpenCode 2.0.14: "v2.session.interrupt" is not an operation id there.
        self.assertEqual(v2_api_operation("v2.session.interrupt"), "session.interrupt")

    async def test_rejects_bad_ids_other_backends_and_bad_replies(self) -> None:
        source = self.source()
        source._v2_api_json = mock.AsyncMock(return_value={"interrupted": True})
        for bad in ("claude:abc", "ses_", "ses_a/../b", "ses_a b", ""):
            self.assertEqual((await source.interrupt_session(bad))[0], False)
        source._v2_api_json.assert_not_awaited()
        self.assertEqual((await self.source("v1").interrupt_session("ses_abc"))[0], False)
        for reply in (None, {}, {"interrupted": "yes"}, [True]):
            source._v2_api_json = mock.AsyncMock(return_value=reply)
            self.assertEqual(await source.interrupt_session("ses_abc"),
                             (False, "OpenCode V2 returned an invalid interrupt response"))


if __name__ == "__main__":
    unittest.main()
