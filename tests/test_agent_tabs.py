from __future__ import annotations

import os
import signal
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from textual.widgets import Button

from ocdeck.agent_tabs import (
    AttachedAgentTab,
    PurgeReport,
    collect_attached_tabs,
    is_managed_session,
    parse_tmux_clients,
    purge_attached_tabs,
    window_hosts_other_tabs,
    ptyxis_pid_for_process,
    window_pid_for_client,
)
from ocdeck.app import OCDeckApp
from tests.test_relaunch import ReopenSource


def write_process(root: Path, pid: int, argv: tuple[str, ...], parent: int) -> None:
    process = root / str(pid)
    process.mkdir(parents=True)
    (process / "cmdline").write_bytes(
        b"\0".join(os.fsencode(item) for item in argv) + b"\0"
    )
    (process / "status").write_text(
        f"Name:\ttest\nPPid:\t{parent}\n", encoding="utf-8"
    )


PTYXIS_WINDOW = (
    "/usr/bin/ptyxis",
    "--standalone",
    "--tab",
    "--title",
    "OpenCode \u00b7 alpha",
    "--working-directory=/work/alpha",
    "--",
    "/usr/bin/tmux",
    "attach-session",
    "-t",
    "oc-ses_alpha",
)


class ClientParsingTests(unittest.TestCase):
    def test_managed_session_prefixes(self) -> None:
        self.assertTrue(is_managed_session("oc-ses_a"))
        self.assertTrue(is_managed_session("oc-browser-ses_a"))
        self.assertTrue(is_managed_session("oc2-ses_a"))
        self.assertTrue(is_managed_session("cc-5906cf70"))
        self.assertTrue(is_managed_session("cx-11111111"))
        self.assertFalse(is_managed_session("work"))
        self.assertFalse(is_managed_session(""))

    def test_parse_tmux_clients_skips_malformed_rows(self) -> None:
        output = (
            "/dev/pts/9\t11\toc-ses_a\n"
            "no-tty\t12\toc-ses_b\n"
            "/dev/pts/10\tnot-a-pid\toc-ses_c\n"
            "/dev/pts/11\t13\t\n"
            "\n"
            "/dev/pts/12\t14\twork\n"
        )
        self.assertEqual(
            parse_tmux_clients(output),
            [("/dev/pts/9", 11, "oc-ses_a"), ("/dev/pts/12", 14, "work")],
        )


class AttachedTabDiscoveryTests(unittest.TestCase):
    def test_client_ancestry_resolves_to_the_ocdeck_ptyxis_window(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            proc_root = Path(base)
            write_process(proc_root, 100, PTYXIS_WINDOW, 1)
            write_process(proc_root, 101, ("/usr/libexec/ptyxis-agent",), 100)
            write_process(
                proc_root,
                102,
                ("/usr/bin/tmux", "attach-session", "-t", "oc-ses_alpha"),
                101,
            )
            write_process(proc_root, 200, ("/usr/bin/gnome-terminal",), 1)
            write_process(
                proc_root,
                201,
                ("/usr/bin/tmux", "attach-session", "-t", "oc-ses_beta"),
                200,
            )
            write_process(
                proc_root,
                202,
                ("/usr/bin/tmux", "attach-session", "-t", "work"),
                1,
            )
            output = (
                "/dev/pts/9\t102\toc-ses_alpha\n"
                "/dev/pts/10\t201\toc-ses_beta\n"
                "/dev/pts/11\t202\twork\n"
            )
            self.assertEqual(
                collect_attached_tabs(output, proc_root),
                [AttachedAgentTab("oc-ses_alpha", "/dev/pts/9", 102, 100)],
            )
            self.assertIsNone(window_pid_for_client(201, proc_root))

    def test_parent_cycles_do_not_loop_forever(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            proc_root = Path(base)
            write_process(proc_root, 300, ("/usr/bin/tmux", "attach-session"), 301)
            write_process(proc_root, 301, ("/usr/bin/tmux", "attach-session"), 300)
            self.assertIsNone(window_pid_for_client(300, proc_root))


class RendererAncestryTests(unittest.TestCase):
    def test_process_ancestry_resolves_to_the_ptyxis_window(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            proc_root = Path(base)
            write_process(proc_root, 700, PTYXIS_WINDOW, 1)
            write_process(proc_root, 701, ("/usr/libexec/ptyxis-agent",), 700)
            write_process(proc_root, 702, ("/usr/bin/bash",), 701)
            write_process(
                proc_root, 703, ("/home/user/.opencode/bin/opencode",), 702
            )
            self.assertEqual(ptyxis_pid_for_process(703, proc_root), 700)

    def test_process_ancestry_without_ptyxis_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            proc_root = Path(base)
            write_process(proc_root, 800, ("/usr/share/code/code",), 1)
            write_process(proc_root, 801, ("/usr/bin/bash",), 800)
            self.assertIsNone(ptyxis_pid_for_process(801, proc_root))

    def test_process_ancestry_cycles_do_not_loop_forever(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            proc_root = Path(base)
            write_process(proc_root, 900, ("/usr/bin/bash",), 901)
            write_process(proc_root, 901, ("/usr/bin/bash",), 900)
            self.assertIsNone(ptyxis_pid_for_process(900, proc_root))


class PurgeTests(unittest.TestCase):
    def test_purge_detaches_clients_and_leaves_sessions_running(self) -> None:
        tabs = [
            AttachedAgentTab("oc-a", "/dev/pts/9", 11, 100),
            AttachedAgentTab("oc-a", "/dev/pts/10", 12, 100),
            AttachedAgentTab("oc-b", "/dev/pts/11", 13, 200),
        ]
        window_by_tty = {"/dev/pts/9": 100, "/dev/pts/10": 100, "/dev/pts/11": 200}
        alive = {100, 200}
        detached: list[str] = []
        signals: list[tuple[int, int]] = []

        def detach(tty: str) -> bool:
            detached.append(tty)
            alive.discard(window_by_tty[tty])
            return True

        report = purge_attached_tabs(
            tabs,
            detach_client=detach,
            has_session=lambda name: True,
            process_exists=lambda pid: pid in alive,
            send_signal=lambda pid, sig: signals.append((pid, sig)),
            sleep=lambda seconds: None,
        )

        self.assertEqual(detached, ["/dev/pts/9", "/dev/pts/10", "/dev/pts/11"])
        self.assertEqual(signals, [])
        self.assertEqual(
            report,
            PurgeReport(
                detach_requested=3,
                windows=2,
                windows_closed=2,
                windows_remaining=0,
                sessions_running=2,
            ),
        )

    def test_stubborn_windows_are_terminated_without_touching_sessions(self) -> None:
        tabs = [AttachedAgentTab("oc-a", "/dev/pts/9", 11, 100)]
        alive = {100}
        signals: list[tuple[int, int]] = []

        def send_signal(pid: int, sig: int) -> None:
            signals.append((pid, sig))
            alive.discard(pid)

        report = purge_attached_tabs(
            tabs,
            detach_client=lambda tty: True,
            has_session=lambda name: True,
            process_exists=lambda pid: pid in alive,
            send_signal=send_signal,
            sleep=lambda seconds: None,
            hosts_other_tabs=lambda pid, clients: False,
        )

        self.assertEqual(signals, [(100, signal.SIGTERM)])
        self.assertEqual(report.windows_closed, 1)
        self.assertEqual(report.windows_remaining, 0)
        self.assertEqual(report.sessions_running, 1)

    def test_window_with_other_tabs_is_never_terminated(self) -> None:
        # Regression: purging an agent tab killed a Claude Code tab that
        # shared the same Ptyxis window.
        tabs = [AttachedAgentTab("oc2-a", "/dev/pts/9", 11, 100)]
        signals: list[tuple[int, int]] = []
        report = purge_attached_tabs(
            tabs,
            detach_client=lambda tty: True,
            has_session=lambda name: True,
            process_exists=lambda pid: pid == 100,
            send_signal=lambda pid, sig: signals.append((pid, sig)),
            sleep=lambda seconds: None,
            hosts_other_tabs=lambda pid, clients: pid == 100 and clients == {11},
        )
        self.assertEqual(signals, [])
        self.assertEqual((report.windows_shared, report.windows_closed), (1, 0))
        self.assertEqual(report.sessions_running, 1)

    def test_other_tab_detection_walks_the_process_tree(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)

            def process(pid: int, parent: int, *argv: str) -> None:
                directory = root / str(pid)
                directory.mkdir()
                (directory / "status").write_text(f"Name:\tx\nPPid:\t{parent}\n")
                (directory / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")

            process(100, 1, "/usr/bin/ptyxis", "--standalone")
            process(101, 100, "/usr/libexec/ptyxis-agent", "--socket-fd=3")
            process(11, 101, "/usr/bin/tmux", "attach-session", "-t", "oc2-a")
            self.assertFalse(window_hosts_other_tabs(100, {11}, root))
            process(12, 101, "/usr/bin/bash")
            process(13, 12, "/home/u/.local/bin/claude")
            self.assertTrue(window_hosts_other_tabs(100, {11}, root))

    def test_failed_detach_is_reported_and_sessions_still_checked(self) -> None:
        tabs = [AttachedAgentTab("oc-a", "/dev/pts/9", 11, 100)]
        report = purge_attached_tabs(
            tabs,
            detach_client=lambda tty: False,
            has_session=lambda name: True,
            process_exists=lambda pid: False,
            send_signal=lambda pid, sig: None,
            sleep=lambda seconds: None,
        )
        self.assertEqual(report.detach_requested, 0)
        self.assertEqual(report.windows_closed, 1)
        self.assertEqual(report.sessions_running, 1)


class PurgeWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_agents_tab_button_offers_purge(self) -> None:
        app = OCDeckApp(ReopenSource(), auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause()
            button = app.query_one("#purge-agent-tabs", Button)
            self.assertFalse(button.disabled)
            self.assertIn("Z Z", str(button.label))

    async def test_purge_requires_two_presses_and_reports_background(self) -> None:
        tab = AttachedAgentTab("oc-ses_alpha", "/dev/pts/9", 11, 100)
        report = PurgeReport(
            detach_requested=1,
            windows=1,
            windows_closed=1,
            windows_remaining=0,
            sessions_running=1,
        )
        app = OCDeckApp(ReopenSource(), auto_refresh=False)
        with mock.patch("ocdeck.app.attached_agent_tabs", return_value=[tab]) as scan, \
                mock.patch("ocdeck.app.purge_attached_tabs", return_value=report) as purge, \
                mock.patch.object(app, "notify") as notify:
            async with app.run_test(size=(140, 42)) as pilot:
                await pilot.pause()
                await pilot.press("z")
                scan.assert_not_called()
                purge.assert_not_called()
                await pilot.press("z")
                await app.workers.wait_for_complete()
                scan.assert_called_once_with()
                purge.assert_called_once_with([tab])
                text = " ".join(str(call.args[0]) for call in notify.call_args_list)
                self.assertIn("still running", text)

    async def test_purge_without_attached_tabs_reports_nothing_to_do(self) -> None:
        app = OCDeckApp(ReopenSource(), auto_refresh=False)
        with mock.patch("ocdeck.app.attached_agent_tabs", return_value=[]), \
                mock.patch("ocdeck.app.purge_attached_tabs") as purge, \
                mock.patch.object(app, "notify") as notify:
            async with app.run_test(size=(140, 42)) as pilot:
                await pilot.pause()
                await pilot.press("z", "z")
                await app.workers.wait_for_complete()
                purge.assert_not_called()
                text = " ".join(str(call.args[0]) for call in notify.call_args_list)
                self.assertIn("No attached agent terminal windows", text)

    async def test_purge_is_disabled_on_mobile(self) -> None:
        app = OCDeckApp(ReopenSource(), auto_refresh=False, inline_tmux=True)
        with mock.patch("ocdeck.app.attached_agent_tabs") as scan, \
                mock.patch.object(app, "notify") as notify:
            async with app.run_test(size=(90, 40)) as pilot:
                await pilot.pause()
                await pilot.press("z")
                scan.assert_not_called()
                text = " ".join(str(call.args[0]) for call in notify.call_args_list)
                self.assertIn("desktop", text)

    async def test_purge_is_blocked_when_offline(self) -> None:
        app = OCDeckApp(ReopenSource(), auto_refresh=False)
        with mock.patch("ocdeck.app.attached_agent_tabs") as scan, \
                mock.patch.object(app, "notify") as notify:
            async with app.run_test(size=(140, 42)) as pilot:
                await pilot.pause()
                app.snapshot = replace(app.snapshot, connection="offline")
                await pilot.press("z")
                scan.assert_not_called()
                text = " ".join(str(call.args[0]) for call in notify.call_args_list)
                self.assertIn("Refresh the connection", text)
