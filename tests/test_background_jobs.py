"""Background shell jobs (V2 shell.list) surface as the "job" agent state.

Ground truth for the shell.list payload shape comes from the verified live
response on this machine:

    {"location":{...},"data":[{"id":"sh_…","status":"running",
      "command":"python3 scripts/s08_v01b_recovery_queue.py","cwd":"…",
      "pid":17833,"metadata":{"sessionID":"ses_…"},
      "time":{"started":<ms>}}]}

and the call MUST be scoped with location[directory].
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
import unittest
from unittest import mock

from textual.widgets import DataTable, Static

from ocdeck.agents_layout import state_cell
from ocdeck.app import OCDeckApp
from ocdeck.models import (
    DashboardSnapshot,
    SessionRecord,
    SystemMetrics,
    agent_state,
    parse_sessions,
)
from ocdeck.source import BACKGROUND_JOB_DIRECTORY_LIMIT, DashboardSource, V2ApiError, V2Location

RUNNING_COMMAND = "python3 scripts/s08_v01b_recovery_queue.py"


def dead_pid() -> int:
    """A pid with no process: spawn one, let it exit, reap it."""
    child = subprocess.Popen(["/bin/true"])
    child.wait()
    return child.pid


def shell_row(
    *,
    shell_id: str = "sh_9f2c1a7e",
    status: str = "running",
    command: object = RUNNING_COMMAND,
    session_id: object = "ses_bgowner",
    pid: object = os.getpid(),  # a live process: jobs whose process is gone are dropped
) -> dict:
    row: dict = {
        "id": shell_id,
        "status": status,
        "command": command,
        "cwd": "/work/proj1",
        "pid": pid,
        "metadata": ({} if session_id is None else {"sessionID": session_id}),
        "time": {"started": 1759243000000},
    }
    return row


def shell_list_payload(*rows: object, directory: str = "/work/proj1") -> dict:
    return {"location": {"directory": directory}, "data": list(rows)}


def session_row(
    session_id: str = "ses_bgowner",
    *,
    directory: str = "/work/proj1",
    workspace_id: str = "",
    updated: int | None = None,
) -> dict:
    now = int(time.time() * 1000)
    row = {
        "id": session_id,
        "projectId": "prj_1",
        "directory": directory,
        "title": "Recovery queue worker",
        "created": now - 60_000,
        "updated": now if updated is None else updated,  # recently active by default
    }
    if workspace_id:
        row["workspaceID"] = workspace_id
    return row


def job_session(**overrides) -> SessionRecord:
    fields = {
        "id": "ses_bgowner",
        "title": "Recovery queue worker",
        "directory": "/work/proj1",
        "project_id": "prj_1",
        "created_ms": 1759242000000,
        "updated_ms": 1759242900000,
        "status": "idle",
        "instance_count": 0,
        "background_jobs": (RUNNING_COMMAND,),
    }
    fields.update(overrides)
    return SessionRecord(**fields)


class BackgroundJobStateTests(unittest.TestCase):
    def test_idle_session_with_running_job_is_job_state(self) -> None:
        # The owner's scenario: the turn finished and the window closed
        # (no instances), but the background shell keeps running.
        self.assertEqual(agent_state(job_session()), "job")

    def test_busy_turn_wins_over_background_job(self) -> None:
        self.assertEqual(
            agent_state(job_session(status="busy", instance_count=1)), "busy"
        )

    def test_retry_turn_wins_over_background_job(self) -> None:
        self.assertEqual(agent_state(job_session(status="retry")), "retry")

    def test_permission_and_question_win_over_background_job(self) -> None:
        self.assertEqual(agent_state(job_session(permission="bash run.sh")), "permission")
        self.assertEqual(agent_state(job_session(question="Continue?")), "question")

    def test_session_without_jobs_defaults_to_empty_tuple(self) -> None:
        self.assertEqual(job_session(background_jobs=()).background_jobs, ())


class BackgroundJobParseTests(unittest.TestCase):
    def test_parse_sessions_attaches_jobs_by_session_id(self) -> None:
        sessions = parse_sessions(
            [session_row("ses_a"), session_row("ses_b", directory="/work/proj2")],
            background_jobs={
                "ses_a": (RUNNING_COMMAND,),
                "ses_b": ("Rscript analysis.R", "make data"),
                "ses_missing": ("ignored",),
            },
        )
        jobs = {session.id: session.background_jobs for session in sessions}
        self.assertEqual(jobs["ses_a"], (RUNNING_COMMAND,))
        self.assertEqual(jobs["ses_b"], ("Rscript analysis.R", "make data"))
        self.assertEqual(agent_state(sessions[0]), "job")

    def test_parse_sessions_without_job_map_leaves_sessions_idle(self) -> None:
        for background_jobs in (None, {}):
            sessions = parse_sessions([session_row()], background_jobs=background_jobs)
            self.assertEqual(sessions[0].background_jobs, ())
            self.assertEqual(agent_state(sessions[0]), "idle")


class BackgroundJobFetchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_URL": ""}):
            self.source = DashboardSource(backend="v2", opencode_bin="/bin/false")

    @mock.patch("ocdeck.source.DashboardSource._v2_api_json")
    async def test_running_job_for_known_session_is_kept(self, api_json) -> None:
        api_json.return_value = shell_list_payload(
            shell_row(),
            shell_row(shell_id="sh_done", status="completed", command="python3 done.py"),
            shell_row(shell_id="sh_other", session_id="ses_someone_else"),
            shell_row(shell_id="sh_stale", status="error"),
        )
        jobs = await self.source._v2_fetch_background_jobs([session_row()])
        self.assertEqual(jobs, {"ses_bgowner": (RUNNING_COMMAND,)})

    @mock.patch("ocdeck.source.DashboardSource._v2_api_json")
    async def test_malformed_rows_are_ignored(self, api_json) -> None:
        api_json.return_value = shell_list_payload(
            "not a dict",
            {"status": "running"},  # no command, no metadata
            {"status": "running", "command": 123, "metadata": {"sessionID": "ses_bgowner"}},
            {"status": 5, "command": RUNNING_COMMAND, "metadata": {"sessionID": "ses_bgowner"}},
            {"status": "running", "command": RUNNING_COMMAND, "metadata": "not a dict"},
            {"status": "running", "command": RUNNING_COMMAND, "metadata": {"sessionID": 123}},
            {"status": "running", "command": RUNNING_COMMAND, "metadata": {}},
        )
        jobs = await self.source._v2_fetch_background_jobs([session_row()])
        self.assertEqual(jobs, {})

    @mock.patch("ocdeck.source.DashboardSource._v2_api_json")
    async def test_commands_are_clipped_to_120_characters(self, api_json) -> None:
        api_json.return_value = shell_list_payload(
            shell_row(command="python3 " + "a" * 200)
        )
        jobs = await self.source._v2_fetch_background_jobs([session_row()])
        self.assertEqual(len(jobs["ses_bgowner"][0]), 120)

    @mock.patch("ocdeck.source.DashboardSource._v2_api_json")
    async def test_failed_call_means_unknown_not_crash(self, api_json) -> None:
        async def side_effect(operation, *, location, **kwargs):
            if location.directory == "/work/proj1":
                raise V2ApiError("OpenCode V2 API unavailable")
            return shell_list_payload(
                shell_row(shell_id="sh_2", session_id="ses_other"),
                directory="/work/proj2",
            )

        api_json.side_effect = side_effect
        jobs = await self.source._v2_fetch_background_jobs(
            [session_row(), session_row("ses_other", directory="/work/proj2")]
        )
        self.assertNotIn("ses_bgowner", jobs)
        self.assertEqual(jobs["ses_other"], (RUNNING_COMMAND,))

    @mock.patch("ocdeck.source.DashboardSource._v2_api_json")
    async def test_no_sessions_means_no_calls(self, api_json) -> None:
        self.assertEqual(await self.source._v2_fetch_background_jobs([]), {})
        self.assertEqual(await self.source._v2_fetch_background_jobs(None), {})
        api_json.assert_not_called()

    @mock.patch("ocdeck.source.DashboardSource._v2_api_json")
    async def test_shell_list_is_scoped_by_directory_once(self, api_json) -> None:
        # Stable 2.x routes by directory: one call per distinct directory,
        # workspace selectors are not sent even when sessions carry them.
        api_json.return_value = shell_list_payload()
        await self.source._v2_fetch_background_jobs(
            [
                session_row(),
                session_row("ses_twin"),
                session_row("ses_ws", workspace_id="wrk_second"),
                session_row("ses_other", directory="/work/proj2"),
            ]
        )
        self.assertEqual(api_json.call_count, 2)
        locations = [call.kwargs["location"] for call in api_json.call_args_list]
        self.assertEqual(
            sorted(locations, key=lambda location: location.directory),
            [V2Location("/work/proj1"), V2Location("/work/proj2")],
        )

    @mock.patch("ocdeck.source.DashboardSource._v2_api_json")
    async def test_directories_are_capped_at_twenty(self, api_json) -> None:
        api_json.return_value = shell_list_payload()
        await self.source._v2_fetch_background_jobs(
            [session_row(f"ses_{index}", directory=f"/work/proj{index}") for index in range(25)]
        )
        self.assertEqual(api_json.call_count, BACKGROUND_JOB_DIRECTORY_LIMIT)

    @mock.patch("ocdeck.source.DashboardSource._v2_api_json")
    async def test_running_job_whose_process_is_gone_is_dropped(self, api_json) -> None:
        dead = shell_row(shell_id="sh_dead", pid=dead_pid())
        api_json.return_value = shell_list_payload(dead, shell_row(shell_id="sh_live"))
        jobs = await self.source._v2_fetch_background_jobs([session_row("ses_bgowner")])
        self.assertEqual(jobs, {"ses_bgowner": (RUNNING_COMMAND,)})  # only the live one

    @mock.patch("ocdeck.source.DashboardSource._v2_api_json")
    async def test_remote_service_jobs_are_not_checked_against_local_processes(self, api_json) -> None:
        self.source.api_url = "http://192.0.2.10:4096"
        api_json.return_value = shell_list_payload(shell_row(pid=dead_pid()))
        jobs = await self.source._v2_fetch_background_jobs([session_row("ses_bgowner")])
        self.assertEqual(jobs, {"ses_bgowner": (RUNNING_COMMAND,)})

    async def test_shell_list_uses_the_persistent_reader_with_the_managed_client(self) -> None:
        from ocdeck.v2_read_api import MANAGED_CLIENT, use_read_api
        self.assertTrue(use_read_api(MANAGED_CLIENT, "", "v2.shell.list"))

    @mock.patch("ocdeck.source.DashboardSource._v2_api_json")
    async def test_only_recently_active_folders_are_asked_newest_first(self, api_json) -> None:
        api_json.return_value = shell_list_payload()
        now = int(time.time() * 1000)
        await self.source._v2_fetch_background_jobs([
            session_row("ses_old", directory="/work/old", updated=now - 3 * 24 * 3600 * 1000),
            session_row("ses_mid", directory="/work/mid", updated=now - 3600 * 1000),
            session_row("ses_new", directory="/work/new", updated=now),
        ])
        asked = [call.kwargs["location"].directory for call in api_json.call_args_list]
        self.assertEqual(asked, ["/work/new", "/work/mid"])  # the 3-day-old folder is skipped

    async def test_v2_api_json_sends_location_directory_for_shell_list(self) -> None:
        # An unscoped shell.list silently lists the server's own cwd and finds
        # nothing, so the scoping must reach the CLI command line.
        class ApiProcess:
            returncode = 0

            def __init__(self, payload: object) -> None:
                self.stdout = json.dumps(payload).encode("utf-8")

            async def communicate(self) -> tuple[bytes, bytes]:
                return self.stdout, b""

        with mock.patch(
            "ocdeck.source.asyncio.create_subprocess_exec",
            return_value=ApiProcess(shell_list_payload()),
        ) as spawn:
            payload = await self.source._v2_api_json(
                "v2.shell.list", location=V2Location("/work/proj1")
            )
        self.assertEqual(payload, shell_list_payload())
        self.assertEqual(
            spawn.call_args.args,
            (
                "/bin/false",
                "api",
                "shell.list",
                "--param",
                "location[directory]=/work/proj1",
            ),
        )


class BackgroundJobRefreshTests(unittest.IsolatedAsyncioTestCase):
    def make_source(self) -> DashboardSource:
        class BgJobSource(DashboardSource):
            async def _v2_health_status(self):
                return "live", "Live V2 API", {}

            async def _v2_collect_sessions(self):
                return [session_row()]

            async def _v2_collect_projects(self):
                return []

            async def _v2_pending_requests(self):
                return {}, True

            async def _refresh_named_agent_registry_v2(self):
                return (), False, "", "available", False, ""

        with mock.patch.dict(os.environ, {"OPENCODE_URL": ""}):
            source = BgJobSource(backend="v2", opencode_bin="/bin/false")
        source._activity_cache = {
            "backend": "v2",
            "sessions": [],
            "known_projects": {},
            "project_names": {},
            "routes": {},
            "services": (),
            "metrics": mock.Mock(memory_percent=0, load_1m=0, uptime_seconds=0),
            "briefings": (),
            "briefing_report_id": "",
            "briefing_generated_at": None,
            "briefing_status": "",
        }
        return source

    async def collect(self, source: DashboardSource):
        with (
            mock.patch("ocdeck.source.read_markdown_projects", return_value=()),
            mock.patch("ocdeck.source.read_project_registry", return_value=()),
            mock.patch("ocdeck.source.read_session_routes", return_value={}),
            mock.patch("ocdeck.source.read_opencode_instances", return_value=({}, 0, {})),
            mock.patch("ocdeck.source.read_tmux_tty_state", return_value=({}, {})),
        ):
            return await source._collect_activity_v2()

    @mock.patch("ocdeck.source.DashboardSource._v2_api_json")
    async def test_refresh_surfaces_running_job_as_job_state(self, api_json) -> None:
        api_json.return_value = shell_list_payload(shell_row())
        snapshot = await self.collect(self.make_source())
        session = next(s for s in snapshot.sessions if s.id == "ses_bgowner")
        self.assertEqual(session.background_jobs, (RUNNING_COMMAND,))
        self.assertEqual(agent_state(session), "job")
        self.assertEqual(
            api_json.call_args.args, ("v2.shell.list",)
        )
        self.assertEqual(api_json.call_args.kwargs["location"], V2Location("/work/proj1"))

    async def test_job_lookups_start_only_after_the_permission_check(self) -> None:
        # Both share the serialized reader; queued job lookups used to make the
        # permission/question check time out, and the deck showed "degraded".
        source = self.make_source()
        order: list[str] = []
        real_pending = source._v2_pending_requests

        async def pending():
            order.append("pending:start")
            result = await real_pending()
            order.append("pending:end")
            return result

        async def jobs(_sessions):
            order.append("jobs")
            return {}

        with mock.patch.object(source, "_v2_pending_requests", side_effect=pending), \
                mock.patch.object(source, "_v2_fetch_background_jobs", side_effect=jobs), \
                mock.patch("ocdeck.source.DashboardSource._v2_api_json", return_value=shell_list_payload()):
            await self.collect(source)
        self.assertEqual(order, ["pending:start", "pending:end", "jobs"])

    @mock.patch("ocdeck.source.DashboardSource._v2_api_json")
    async def test_refresh_survives_shell_list_failure(self, api_json) -> None:
        api_json.side_effect = V2ApiError("OpenCode V2 API request failed")
        snapshot = await self.collect(self.make_source())
        session = next(s for s in snapshot.sessions if s.id == "ses_bgowner")
        self.assertEqual(session.background_jobs, ())
        self.assertEqual(agent_state(session), "idle")


class BackgroundJobRenderTests(unittest.IsolatedAsyncioTestCase):
    def make_source(self):
        class JobSource:
            opencode_bin = None

            async def collect(self) -> DashboardSnapshot:
                return DashboardSnapshot(
                    sessions=(job_session(),),
                    metrics=SystemMetrics(memory_percent=42, load_1m=0.5),
                    connection="live",
                    connection_detail="test API",
                )

        return JobSource()

    async def test_agents_board_shows_job_state_and_command(self) -> None:
        app = OCDeckApp(self.make_source(), auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await asyncio.sleep(0.1)
            app.action_show_tab("agents")
            await pilot.pause()
            table = app.query_one("#agents-table", DataTable)
            row = table.get_row("ses_bgowner")
            self.assertIn(state_cell("job"), str(row[0]))
            # The DETAIL cell clips; the focus strip below shows the full text.
            self.assertTrue(str(row[5]).startswith("Background job: python3"))

            table.focus()
            table.move_cursor(row=0)
            await pilot.pause()
            focus = str(app.query_one("#agent-focus", Static).visual)
            self.assertIn("BACKGROUND JOB", focus)
            self.assertIn(RUNNING_COMMAND, focus)

            app.selected_session_id = "ses_bgowner"
            app._render_detail()
            detail = str(app.query_one("#session-detail", Static).visual)
            self.assertIn("BACKGROUND JOBS", detail)
            self.assertIn(RUNNING_COMMAND, detail)

    async def test_privacy_mode_hides_the_command(self) -> None:
        app = OCDeckApp(self.make_source(), auto_refresh=False)
        async with app.run_test(size=(140, 42)) as pilot:
            await asyncio.sleep(0.1)
            app.action_show_tab("agents")
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            self.assertTrue(app.private)

            row = app.query_one("#agents-table", DataTable).get_row("ses_bgowner")
            self.assertNotIn("python3", str(row[5]))
            self.assertNotIn(RUNNING_COMMAND, str(row[5]))
            self.assertIn("[hidden]", str(row[5]))

            focus = str(app.query_one("#agent-focus", Static).visual)
            self.assertNotIn(RUNNING_COMMAND, focus)
            self.assertIn("Privacy mode", focus)

            app._render_detail()
            detail = str(app.query_one("#session-detail", Static).visual)
            self.assertNotIn(RUNNING_COMMAND, detail)


if __name__ == "__main__":
    unittest.main()
