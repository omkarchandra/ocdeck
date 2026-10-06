from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock
from unittest.mock import patch

from ocdeck.models import MAX_LAST_PROMPT_LENGTH, ProjectRecord, agent_state
from ocdeck.source import (
    DashboardSource,
    EXPECTED_V2_VERSION,
    MAX_BRIEFING_ARRAY_ITEMS,
    MAX_BRIEFING_EVIDENCE_ITEMS,
    MAX_BRIEFING_TEXT_LENGTH,
    MAX_BRIEFINGS_FILE_BYTES,
    MAX_PERMISSION_STATE_BYTES,
    V2ApiError,
    V2Location,
    api_credentials_are_safe,
    classify_opencode_command,
    clip_at_word_boundary,
    communicate_with_cleanup,
    merge_permissions,
    merge_project_catalog,
    match_project_briefings,
    normalize_v2_agents,
    parse_briefings,
    parse_api_statuses,
    parse_markdown_projects,
    parse_signed_in_tabs_status,
    parse_v2_active,
    parse_v2_forms,
    parse_v2_locations,
    parse_v2_permissions,
    parse_v2_projects,
    parse_v2_session_page,
    parse_v2_signed_in_tabs_status,
    read_agent_parent_ids,
    read_archived_session_ids,
    read_briefings_file,
    read_last_user_interactions,
    read_latest_user_prompts,
    read_live_opencode_panes,
    read_local_permissions,
    read_local_runtime_state,
    read_local_statuses,
    read_home_agent_session_evidence,
    read_opencode_instances,
    read_projects_from_database,
    read_process_tty,
    read_project_registry,
    read_server_credentials,
    read_session_routes,
    read_sessions_from_database,
    read_session_turn_activity,
    read_state_payload,
    read_tmux_tty_state,
    read_tmux_tty_sessions,
    reconcile_permissions,
    reconcile_statuses,
    validate_api_url,
    v2_request_directories,
    unlink_same_file,
)


def write_session_db(path: Path, rows: dict[str, int | None]) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE session ("
            "id TEXT PRIMARY KEY, time_archived INTEGER)"
        )
        connection.executemany(
            "INSERT INTO session (id, time_archived) VALUES (?, ?)",
            list(rows.items()),
        )
        connection.commit()
    finally:
        connection.close()


def briefing_project(project_path: str, project_id: str = "artifact-alpha") -> dict:
    return {
        "projectID": project_id,
        "projectPath": project_path,
        "name": "Alpha",
        "assessment": "on-track",
        "summary": "Ready for the next review",
        "confidence": "high",
        "evidenceAt": "2026-08-24T10:00:00Z",
        "completedOutputs": [{"label": "Report", "locator": "/tmp/report.txt"}],
        "blockers": [],
        "nextSteps": [
            {
                "id": "step-1",
                "title": "Review report",
                "detail": "Check the evidence",
                "state": "now",
                "requiresApproval": True,
            }
        ],
        "evidence": ["pytest passed"],
        "researchStatus": "completed",
    }


def briefing_report(*projects: dict, **overrides: object) -> dict:
    payload: dict[str, object] = {
        "schemaVersion": 1,
        "reportID": "report-1",
        "generatedAt": "2026-08-24T10:05:00Z",
        "status": "completed",
        "projects": list(projects),
    }
    payload.update(overrides)
    return payload


class FakeApiSource(DashboardSource):
    def __init__(self) -> None:
        super().__init__(
            backend="v1",
            api_url="http://127.0.0.1:4096",
            opencode_bin="/bin/false",
            server_env_file="/nonexistent/server.env",
        )
        self.paths: list[str] = []
        self.patches: list[tuple[str, str, object]] = []

    def _request_json(
        self,
        path: str,
        password: str,
        *,
        method: str = "GET",
        payload: object | None = None,
    ):
        self.paths.append(path)
        if method == "PATCH":
            self.patches.append((path, method, payload))
            return {"id": path.rsplit("/", 1)[-1]}
        if path == "/global/health":
            return {"healthy": True, "version": "test"}
        if path == "/session/status":
            return {"session-1": {"type": "busy"}}
        if path == "/permission":
            return [
                {
                    "id": "perm-1",
                    "sessionID": "session-1",
                    "permission": "bash",
                    "patterns": ["npm test"],
                }
            ]
        if path == "/question":
            return [
                {
                    "id": "question-1",
                    "sessionID": "session-2",
                    "questions": [
                        {
                            "header": "Test result",
                            "question": "What happened after pressing the key?",
                        }
                    ],
                }
            ]
        raise AssertionError(path)


class FakeV2PromptSource(DashboardSource):
    """V2 source that answers only the latest-user-message prompt lookup."""

    def __init__(self, text: str) -> None:
        super().__init__(backend="v2", opencode_bin="/bin/false")
        self.text = text
        self.operations: list[str] = []

    async def _v2_api_json(self, operation, **kwargs):
        self.operations.append(operation)
        if operation != "v2.session.message.list":
            raise AssertionError(operation)
        return {
            "data": [
                {
                    "id": "msg_latest",
                    "type": "user",
                    "text": self.text,
                    "time": {"created": 42},
                }
            ]
        }


class SourceTests(unittest.IsolatedAsyncioTestCase):
    def test_source_defaults_to_v2_without_probing_for_v1(self) -> None:
        with mock.patch(
            "ocdeck.source.DashboardSource._find_opencode2",
            return_value="/usr/bin/opencode2",
        ), mock.patch(
            "ocdeck.source.DashboardSource._find_opencode",
            side_effect=AssertionError("V1 executable probe"),
        ), mock.patch(
            "ocdeck.source.read_server_credentials",
            side_effect=AssertionError("V1 credential read"),
        ):
            source = DashboardSource(named_agent_files={})

        self.assertEqual(source.backend, "v2")
        self.assertEqual(source.opencode_bin, "/usr/bin/opencode2")

    def test_v2_payload_adapters_normalize_current_openapi_contract(self) -> None:
        sessions = parse_v2_session_page(
            {
                "data": [
                    {
                        "id": "ses_live",
                        "parentID": "ses_parent",
                        "projectID": "project-1",
                        "agent": "jarvis",
                        "model": {"providerID": "openai", "id": "gpt-test"},
                        "title": "Live session",
                        "location": {
                            "directory": "/work/alpha",
                            "workspaceID": "wrk_alpha",
                        },
                        "time": {"created": 10, "updated": 20},
                        "metadata": {"homeAgent": {"agent": "jarvis"}},
                    },
                    {
                        "id": "ses_archived",
                        "projectID": "project-1",
                        "location": {"directory": "/work/alpha"},
                        "time": {"created": 1, "updated": 2, "archived": 3},
                    },
                ],
                "cursor": {"previous": None, "next": "next-page"},
            }
        )
        self.assertIsNotNone(sessions)
        rows, cursor = sessions or ([], "")
        self.assertEqual(cursor, "next-page")
        self.assertEqual(
            rows,
            [
                {
                    "id": "ses_live",
                    "projectId": "project-1",
                    "directory": "/work/alpha",
                    "title": "Live session",
                    "created": 10,
                    "updated": 20,
                    "location": {
                        "directory": "/work/alpha",
                        "workspaceID": "wrk_alpha",
                    },
                    "parentID": "ses_parent",
                    "workspaceID": "wrk_alpha",
                    "agent": "jarvis",
                    "model": {"providerID": "openai", "id": "gpt-test"},
                    "metadata": {"homeAgent": {"agent": "jarvis"}},
                }
            ],
        )
        self.assertEqual(
            parse_v2_projects(
                [{"id": "project-1", "canonical": "/work/alpha", "sandboxes": []}]
            ),
            [{"id": "project-1", "worktree": "/work/alpha", "sandboxes": []}],
        )
        self.assertEqual(
            parse_v2_active({"data": {"ses_live": {"type": "running"}}}),
            {"ses_live": "busy"},
        )
        self.assertEqual(
            parse_v2_locations(
                [
                    {"directory": "/work/alpha"},
                    {
                        "directory": "/work/alpha",
                        "workspaceID": "wrk_alpha",
                    },
                ]
            ),
            (
                V2Location("/work/alpha"),
                V2Location("/work/alpha", "wrk_alpha"),
            ),
        )
        self.assertEqual(
            parse_v2_permissions(
                {
                    "data": [
                        {
                            "id": "per_1",
                            "sessionID": "ses_live",
                            "action": "shell",
                            "resources": ["npm test", "package.json"],
                        }
                    ]
                }
            ),
            {
                "ses_live": [
                    {
                        "id": "per_1",
                        "permission": "shell",
                        "pattern": "npm test; package.json",
                        "resources": ("npm test", "package.json"),
                    }
                ]
            },
        )
        self.assertEqual(
            parse_v2_forms(
                {
                    "data": [
                        {
                            "id": "frm_1",
                            "sessionID": "ses_live",
                            "title": "Choose a release",
                            "fields": [{"key": "release", "type": "string", "title": "Version"}],
                        }
                    ]
                }
            ),
            {
                "ses_live": [
                    {
                        "id": "frm_1",
                        "permission": "question",
                        "pattern": "Choose a release: Version",
                    }
                ]
            },
        )
        self.assertEqual(
            normalize_v2_agents(
                {"data": [{"id": "jarvis", "name": "jarvis", "model": {"providerID": "openai", "id": "gpt-test"}}]}
            )[0]["model"]["modelID"],
            "gpt-test",
        )
        self.assertEqual(
            parse_v2_signed_in_tabs_status(
                {"data": [{"name": "signed_in_tabs", "status": {"status": "connected"}}]}
            ),
            "connected",
        )

    def test_v2_payload_adapters_reject_partial_malformed_pages(self) -> None:
        self.assertIsNone(parse_v2_session_page({"data": [], "cursor": None}))
        self.assertIsNone(
            parse_v2_session_page(
                {"data": [{"id": "ses_bad"}], "cursor": {"next": None}}
            )
        )
        self.assertIsNone(parse_v2_active({"data": {"ses_bad": {"type": "idle"}}}))
        self.assertIsNone(parse_v2_permissions({"data": [None]}))
        self.assertIsNone(parse_v2_forms({"data": [{"id": "frm_bad"}]}))
        self.assertIsNone(
            parse_v2_locations(
                [{"directory": "/work/alpha", "workspaceID": "invalid"}]
            )
        )
        self.assertIsNone(
            parse_v2_session_page(
                {
                    "data": [
                        {
                            "id": "invalid",
                            "projectID": "project-1",
                            "location": {"directory": "/work/alpha"},
                            "time": {"created": 1, "updated": 2},
                        }
                    ],
                    "cursor": {"next": None},
                }
            )
        )

    async def test_v2_cli_large_output_uses_a_complete_bounded_capture(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            executable = Path(base) / "opencode2"
            executable.write_text(
                "#!/usr/bin/python3\nimport json, os, stat, sys\n"
                "size = 1100000 if 'oversized' in sys.argv else 400000\n"
                "data = json.dumps({'value': 'x' * size}).encode()\n"
                "os.write(1, data if stat.S_ISREG(os.fstat(1).st_mode) else data[:262144])\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            source = DashboardSource(backend="v2", opencode_bin=str(executable))
            payload = await source._v2_api_json("v2.session.list")
            self.assertEqual(len(payload["value"]), 400000)
            with self.assertRaisesRegex(V2ApiError, "exceeded the size limit"):
                await source._v2_api_json("oversized")

    async def test_v2_api_enforces_exact_location_envelopes_and_errors(self) -> None:
        class ApiProcess:
            returncode = 0

            def __init__(self, payload: object) -> None:
                self.stdout = json.dumps(payload).encode("utf-8")

            async def communicate(self) -> tuple[bytes, bytes]:
                return self.stdout, b""

        # This fixture exercises shared-service discovery, not the caller's URL.
        # Every subprocess below is mocked; keep the external test URL sentinel.
        with mock.patch.dict(os.environ, {"OPENCODE_URL": ""}):
            source = DashboardSource(backend="v2", opencode_bin="/usr/bin/opencode2")
        location = V2Location("/work/alpha")
        envelope = {
            "location": {
                "directory": "/work/alpha",
                "project": {
                    "id": "project-1",
                    "directory": "/work/alpha",
                    "canonical": "/work/alpha",
                },
            },
            "data": [],
        }
        with mock.patch(
            "ocdeck.source.asyncio.create_subprocess_exec",
            return_value=ApiProcess(envelope),
        ) as spawn:
            self.assertEqual(
                await source._v2_api_json(
                    "v2.permission.request.list", location=location
                ),
                envelope,
            )

        self.assertEqual(
            spawn.call_args.args,
            (
                "/usr/bin/opencode2",
                "api",
                "permission.request.list",
                "--param",
                "location[directory]=/work/alpha",
            ),
        )

        wrong_location = json.loads(json.dumps(envelope))
        wrong_location["location"]["workspaceID"] = "wrk_other"
        with mock.patch(
            "ocdeck.source.asyncio.create_subprocess_exec",
            return_value=ApiProcess(wrong_location),
        ):
            with self.assertRaisesRegex(V2ApiError, "invalid location envelope"):
                await source._v2_api_json(
                    "v2.permission.request.list", location=location
                )

        with mock.patch(
            "ocdeck.source.asyncio.create_subprocess_exec",
            return_value=ApiProcess(
                {"_tag": "InvalidRequestError", "message": "Invalid cursor"}
            ),
        ):
            with self.assertRaisesRegex(V2ApiError, "Invalid cursor"):
                await source._v2_api_json("v2.session.list")

    def test_v2_request_locations_preserve_workspaces_without_a_silent_cap(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            live = root / "live"
            idle = root / "idle"
            live.mkdir()
            idle.mkdir()
            locations = v2_request_directories(
                [
                    {"id": "ses_idle", "directory": str(idle)},
                    {"id": "ses_live", "directory": str(live)},
                    {
                        "id": "ses_workspace",
                        "directory": str(idle),
                        "workspaceID": "wrk_second",
                    },
                    {"id": "ses_duplicate", "directory": str(idle)},
                    {"id": "ses_missing", "directory": str(root / "missing")},
                ],
                {"ses_live": "busy"},
                {},
            )

        self.assertEqual(
            locations[1:],
            (
                V2Location(str(live)),
                V2Location(str(idle)),
                V2Location(str(idle), "wrk_second"),
                V2Location(str(root / "missing")),
            ),
        )

    async def test_v2_session_list_follows_next_cursor_and_caps_results(self) -> None:
        class PagedV2Source(DashboardSource):
            def __init__(self) -> None:
                super().__init__(backend="v2", opencode_bin="/bin/false", limit=3)
                self.calls: list[dict[str, str]] = []

            async def _v2_api_json(self, operation, **kwargs):
                self.assert_operation = operation
                params = dict(kwargs.get("params") or {})
                self.calls.append(params)
                offset = 0 if "cursor" not in params else 2
                count = 2 if offset == 0 else 1
                return {
                    "data": [
                        {
                            "id": f"ses_{index}",
                            "projectID": "project-1",
                            "location": {"directory": "/work/alpha"},
                            "time": {"created": index, "updated": index},
                        }
                        for index in range(offset, offset + count)
                    ],
                    "cursor": {
                        "previous": None,
                        "next": "cursor-2" if offset == 0 else None,
                    },
                }

        source = PagedV2Source()
        sessions = await source._v2_collect_sessions()

        self.assertEqual(source.assert_operation, "v2.session.list")
        self.assertEqual([item["id"] for item in sessions or []], ["ses_0", "ses_1", "ses_2"])
        self.assertEqual(source.calls[0], {"limit": "3", "order": "desc"})
        self.assertEqual(source.calls[1], {"limit": "1", "cursor": "cursor-2"})

    async def test_v2_session_list_rejects_cursor_and_content_loops(self) -> None:
        class LoopingV2Source(DashboardSource):
            def __init__(self, mode: str) -> None:
                super().__init__(backend="v2", opencode_bin="/bin/false", limit=5)
                self.mode = mode
                self.calls = 0

            async def _v2_api_json(self, operation, **kwargs):
                self.calls += 1
                index = 1 if self.mode == "content" else self.calls
                next_cursor = (
                    "cursor-loop"
                    if self.mode == "cursor"
                    else f"cursor-{self.calls}"
                )
                return {
                    "data": [
                        {
                            "id": f"ses_{index}",
                            "projectID": "project-1",
                            "location": {"directory": "/work/alpha"},
                            "time": {"created": index, "updated": index},
                        }
                    ],
                    "cursor": {"previous": None, "next": next_cursor},
                }

        for mode in ("cursor", "content"):
            with self.subTest(mode=mode):
                source = LoopingV2Source(mode)
                self.assertIsNone(await source._v2_collect_sessions())
                self.assertEqual(source.calls, 2)

    async def test_v2_archive_only_pages_do_not_look_like_repeated_empty_pages(self) -> None:
        class ArchivedV2Source(DashboardSource):
            def __init__(self):
                super().__init__(backend="v2", opencode_bin="/bin/false", limit=500)
                self.calls = 0

            async def _v2_api_json(self, operation, **kwargs):
                self.calls += 1
                index = self.calls
                return {
                    "data": [{
                        "id": f"ses_{index}", "projectID": "project-1",
                        "location": {"directory": "/work/alpha"},
                        "time": {"created": 1, "updated": index, **({"archived": 10} if index in (2, 3) else {})},
                    }] if index < 4 else [],
                    "cursor": {"previous": None, "next": f"cursor-{index}" if index < 4 else None},
                }

        source = ArchivedV2Source()
        sessions = await source._v2_collect_sessions()
        self.assertEqual([item["id"] for item in sessions or []], ["ses_1"])
        self.assertEqual(source.calls, 4)

    async def test_v2_session_list_caps_an_overfilled_page(self) -> None:
        class OverfilledV2Source(DashboardSource):
            def __init__(self) -> None:
                super().__init__(backend="v2", opencode_bin="/bin/false", limit=3)

            async def _v2_api_json(self, operation, **kwargs):
                return {
                    "data": [
                        {
                            "id": f"ses_{index}",
                            "projectID": "project-1",
                            "location": {"directory": "/work/alpha"},
                            "time": {"created": index, "updated": index},
                        }
                        for index in range(10)
                    ],
                    "cursor": {"previous": None, "next": None},
                }

        sessions = await OverfilledV2Source()._v2_collect_sessions()

        self.assertEqual([item["id"] for item in sessions or []], ["ses_0", "ses_1", "ses_2"])

    async def test_v2_active_and_pending_caches_are_failure_atomic(self) -> None:
        class CachedV2Source(DashboardSource):
            def __init__(self) -> None:
                super().__init__(backend="v2", opencode_bin="/bin/false")
                self.fail_active = False
                self.fail_locations = False
                self.fail_permissions = False
                self.pending_calls = 0

            async def _v2_api_json(self, operation, **kwargs):
                if operation == "v2.health.get":
                    return {"healthy": True, "version": "test"}
                if operation == "v2.session.active":
                    return (
                        {"data": {"ses_bad": {"type": "idle"}}}
                        if self.fail_active
                        else {"data": {"ses_live": {"type": "running"}}}
                    )
                if operation == "v2.debug.location.list":
                    if self.fail_locations:
                        raise V2ApiError("location discovery failed")
                    return [{"directory": "/work/alpha", "workspaceID": "wrk_one"}]
                if operation == "v2.permission.request.list":
                    self.pending_calls += 1
                    if self.fail_permissions:
                        raise V2ApiError("permission request failed")
                    return {
                        "data": [
                            {
                                "id": "per_one",
                                "sessionID": "ses_live",
                                "action": "shell",
                                "resources": ["npm test"],
                            }
                        ]
                    }
                if operation == "v2.form.request.list":
                    self.pending_calls += 1
                    return {"data": []}
                raise AssertionError(operation)

        source = CachedV2Source()
        _state, _detail, statuses = await source._v2_health_status()
        self.assertEqual(statuses, {"ses_live": "busy"})
        source.fail_active = True
        state, detail, statuses = await source._v2_health_status()
        self.assertIn("stale", detail)
        self.assertEqual(state, "degraded")
        self.assertEqual(statuses, {"ses_live": "busy"})
        self.assertNotIn("ses_bad", statuses)

        pending, complete = await source._v2_pending_requests()
        self.assertTrue(complete)
        self.assertEqual(pending["ses_live"][0]["id"], "per_one")

        source.fail_permissions = True
        pending, complete = await source._v2_pending_requests()
        self.assertFalse(complete)
        self.assertEqual(pending["ses_live"][0]["id"], "per_one")

        source.fail_permissions = False
        source.fail_locations = True
        calls_before_location_failure = source.pending_calls
        pending, complete = await source._v2_pending_requests()
        self.assertFalse(complete)
        self.assertEqual(pending["ses_live"][0]["id"], "per_one")
        self.assertEqual(source.pending_calls, calls_before_location_failure)

    async def test_v2_activity_reconciles_sessions_routes_catalog_and_registry(self) -> None:
        class ReconciledV2Source(DashboardSource):
            async def _v2_health_status(self):
                return "live", "Live V2 API v0.0.0-beta-18707", {"ses_new": "busy"}

            async def _v2_collect_sessions(self):
                return [
                    {
                        "id": "ses_new",
                        "title": "Current worker",
                        "directory": "/work/original",
                        "projectId": "api-project",
                        "created": 1,
                        "updated": 2,
                    }
                ]

            async def _v2_collect_projects(self):
                return [{"id": "api-project", "worktree": "/work/original"}]

            async def _v2_pending_requests(self):
                return {}, True

            async def _refresh_named_agent_registry_v2(self):
                return (), False, "", "available", False, ""

        source = ReconciledV2Source(backend="v2", opencode_bin="/bin/false")
        source._activity_cache = {
            "backend": "v2",
            "sessions": [
                {
                    "id": "ses_stale",
                    "title": "Screenshot row",
                    "directory": "/work/stale",
                    "projectId": "stale-project",
                    "created": 1,
                    "updated": 1,
                }
            ],
            "known_projects": {"stale-project": "/work/stale"},
            "project_names": {"stale-project": "Stale"},
            "routes": {},
            "services": (),
            "metrics": mock.Mock(memory_percent=0, load_1m=0, uptime_seconds=0),
            "briefings": (),
            "briefing_report_id": "",
            "briefing_generated_at": None,
            "briefing_status": "",
        }
        with (
            patch("ocdeck.source.read_markdown_projects", return_value=()),
            patch(
                "ocdeck.source.read_project_registry",
                return_value=(("Registry Project", "/work/registry"),),
            ),
            patch(
                "ocdeck.source.read_session_routes",
                return_value={"ses_new": "Registry Project"},
            ),
            patch("ocdeck.source.read_opencode_instances", return_value=({}, 0, {})),
            patch("ocdeck.source.read_tmux_tty_state", return_value=({}, {})),
        ):
            snapshot = await source._collect_activity_v2()

        self.assertIsNotNone(snapshot)
        self.assertEqual([session.id for session in snapshot.sessions], ["ses_new"])
        self.assertNotIn("ses_stale", {session.id for session in snapshot.sessions})
        routed = next(session for session in snapshot.sessions if session.id == "ses_new")
        project = next(item for item in snapshot.projects if item.id == routed.project_id)
        self.assertEqual(project.name, "Registry Project")

    async def test_v2_activity_marks_cached_session_fallback_degraded(self) -> None:
        class FailedRefreshSource(DashboardSource):
            async def _v2_health_status(self):
                return "live", "Live V2 API v0.0.0-beta-18707", {}

            async def _v2_collect_sessions(self):
                return None

            async def _v2_collect_projects(self):
                return []

            async def _v2_pending_requests(self):
                return {}, True

            async def _refresh_named_agent_registry_v2(self):
                return (), False, "", "available", False, ""

        source = FailedRefreshSource(backend="v2", opencode_bin="/bin/false")
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
        with (
            patch("ocdeck.source.read_markdown_projects", return_value=()),
            patch("ocdeck.source.read_project_registry", return_value=()),
            patch("ocdeck.source.read_session_routes", return_value={}),
            patch("ocdeck.source.read_opencode_instances", return_value=({}, 0, {})),
            patch("ocdeck.source.read_tmux_tty_state", return_value=({}, {})),
        ):
            snapshot = await source._collect_activity_v2()

        self.assertEqual(snapshot.connection, "degraded")
        self.assertIn("session list stale", snapshot.connection_detail)
        self.assertIn("last complete session list", snapshot.warning)

    async def test_v2_collection_uses_only_operation_ids_and_never_sqlite_or_v1_cli(self) -> None:
        class ContractV2Source(DashboardSource):
            def __init__(self, root: Path) -> None:
                super().__init__(
                    backend="v2",
                    opencode_bin="/bin/false",
                    projects_file=root / "missing-projects.md",
                    session_routes_file=root / "missing-routes.json",
                    briefings_file=root / "missing-briefings.json",
                    permission_state_dir=root / "permissions",
                    named_agent_files={},
                    limit=10,
                )
                self.root = root
                self.operations: list[str] = []

            async def _command_json(self, *arguments, **kwargs):
                raise AssertionError(f"V1 CLI called from V2: {arguments}")

            async def _service_states(self):
                return ()

            async def _v2_api_json(self, operation, **kwargs):
                self.operations.append(operation)
                requested = kwargs.get("location")
                location_info = None
                if isinstance(requested, V2Location):
                    location_info = {
                        "directory": requested.directory,
                        "project": {
                            "id": "project-1",
                            "directory": requested.directory,
                            "canonical": requested.directory,
                        },
                    }
                    if requested.workspace_id:
                        location_info["workspaceID"] = requested.workspace_id
                if operation == "v2.health.get":
                    return {"healthy": True, "version": EXPECTED_V2_VERSION, "pid": 1}
                if operation == "v2.session.active":
                    return {"data": {"ses_live": {"type": "running"}}}
                if operation == "v2.session.list":
                    return {
                        "data": [
                            {
                                "id": "ses_live",
                                "parentID": "ses_parent",
                                "projectID": "project-1",
                                "agent": "jarvis",
                                "model": {"providerID": "openai", "id": "gpt-test"},
                                "title": "V2 work",
                                "location": {
                                    "directory": str(self.root),
                                    "workspaceID": "wrk_live",
                                },
                                "time": {"created": 10, "updated": 20},
                                "metadata": {"homeAgent": {"agent": "jarvis"}},
                            }
                        ],
                        "cursor": {"previous": None, "next": None},
                    }
                if operation == "v2.project.list":
                    return [{"id": "project-1", "canonical": str(self.root), "sandboxes": []}]
                if operation == "v2.session.message.list":
                    return {"data": [{"id": "msg_latest", "type": "user", "text": "Latest V2 request", "time": {"created": 15}}], "cursor": {}}
                if operation == "v2.debug.location.list":
                    return [
                        {
                            "directory": str(self.root),
                            "workspaceID": "wrk_live",
                        }
                    ]
                if operation == "v2.permission.request.list":
                    return {
                        "location": location_info,
                        "data": [
                            {
                                "id": "per_1",
                                "sessionID": "ses_live",
                                "action": "shell",
                                "resources": ["npm test"],
                            }
                        ],
                    }
                if operation == "v2.form.request.list":
                    return {"location": location_info, "data": []}
                if operation == "v2.agent.list":
                    return {"location": location_info, "data": [{"id": "jarvis", "name": "jarvis", "mode": "primary", "hidden": False, "request": {}, "permissions": []}]}
                if operation == "v2.mcp.list":
                    return {"location": location_info, "data": []}
                raise AssertionError(operation)

        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            with mock.patch(
                "ocdeck.source.sqlite3.connect",
                side_effect=AssertionError("SQLite read"),
            ), mock.patch(
                "ocdeck.source.read_local_runtime_state",
                side_effect=AssertionError("V1 runtime-state read"),
            ), mock.patch(
                "ocdeck.source.read_server_credentials",
                side_effect=AssertionError("V1 credential read"),
            ):
                source = ContractV2Source(root)
                snapshot = await source.collect()

        self.assertEqual(snapshot.connection, "live")
        self.assertEqual(snapshot.sessions[0].status, "busy")
        self.assertEqual(snapshot.sessions[0].parent_id, "ses_parent")
        self.assertEqual(snapshot.sessions[0].agent, "jarvis")
        self.assertEqual(snapshot.sessions[0].model, "openai/gpt-test")
        self.assertEqual(snapshot.sessions[0].workspace_id, "wrk_live")
        self.assertEqual(snapshot.sessions[0].home_agent, "maverik")
        self.assertEqual(snapshot.sessions[0].permission_id, "per_1")
        self.assertEqual(snapshot.sessions[0].last_prompt, "Latest V2 request")
        self.assertEqual(snapshot.sessions[0].last_interaction_ms, 15)
        self.assertIn("v2.session.list", source.operations)
        self.assertIn("v2.permission.request.list", source.operations)
        self.assertIn("v2.form.request.list", source.operations)

    async def test_v2_mutations_and_create_use_openapi_operation_ids(self) -> None:
        class ActionV2Source(DashboardSource):
            def __init__(self) -> None:
                super().__init__(backend="v2", opencode_bin="/bin/false")
                self.calls: list[tuple[str, dict]] = []

            async def _v2_api_json(self, operation, **kwargs):
                self.calls.append((operation, kwargs))
                if operation == "v2.session.create":
                    return {
                        "data": {
                            "id": "ses_created",
                            "location": {"directory": "/work/alpha"},
                        }
                    }
                return None

        source = ActionV2Source()
        self.assertEqual(await source.rename_session("ses_one", "New title"), "")
        self.assertEqual(await source.approve_permission("ses_one", "per_one"), "")
        session_id, error = await source.create_session(Path("/work/alpha"))
        self.assertEqual(await source.remove_session("ses_created"), "")

        self.assertEqual((session_id, error), ("ses_created", ""))
        self.assertEqual(
            [(operation, call.get("params"), call.get("payload")) for operation, call in source.calls],
            [
                ("v2.session.rename", {"sessionID": "ses_one"}, {"title": "New title"}),
                ("v2.session.permission.reply", {"sessionID": "ses_one", "requestID": "per_one"}, {"decision": "once"}),
                (
                    "v2.session.create",
                    None,
                    {"location": {"directory": "/work/alpha"}},
                ),
                ("v2.session.remove", {"sessionID": "ses_created"}, None),
            ],
        )
        self.assertNotIn("cwd", source.calls[2][1])

        class InvalidActionV2Source(DashboardSource):
            def __init__(self) -> None:
                super().__init__(backend="v2", opencode_bin="/bin/false")

            async def _v2_api_json(self, operation, **kwargs):
                return {"data": []}

        invalid_action = InvalidActionV2Source()
        self.assertIn(
            "invalid rename response",
            await invalid_action.rename_session("ses_one", "New title"),
        )
        self.assertIn(
            "invalid permission reply",
            await invalid_action.approve_permission("ses_one", "per_one"),
        )
        self.assertIn(
            "invalid session removal response",
            await invalid_action.remove_session("ses_one"),
        )
        self.assertEqual(
            await invalid_action.create_session(Path("/work/alpha")),
            ("", "OpenCode V2 returned an invalid session"),
        )

        invalid = DashboardSource(
            backend="v2",
            opencode_bin="/bin/false",
            api_url="file:///tmp/not-an-api",
        )
        self.assertEqual(
            await invalid.rename_session("ses_one", "New title"),
            "OpenCode API URL must use HTTP or HTTPS",
        )

    def test_briefing_contract_is_parsed_sanitized_and_capped(self) -> None:
        project = briefing_project("/work/alpha")
        project["summary"] = "safe\x1b]52;c;payload\x07\n" + "x" * (
            MAX_BRIEFING_TEXT_LENGTH + 100
        )
        project["completedOutputs"] = [
            {"label": f"output-{index}", "locator": f"/tmp/{index}"}
            for index in range(MAX_BRIEFING_ARRAY_ITEMS + 2)
        ]
        project["blockers"] = [
            {"summary": f"blocker-{index}"}
            for index in range(MAX_BRIEFING_ARRAY_ITEMS + 2)
        ]
        project["nextSteps"] = [
            {
                "id": f"step-{index}",
                "title": "Review [bold] literally",
                "detail": "detail\x1b[31m",
                "state": "now" if index == 0 else "next",
                "requiresApproval": True,
            }
            for index in range(MAX_BRIEFING_ARRAY_ITEMS + 2)
        ]
        project["evidence"] = [
            f"evidence-{index}" for index in range(MAX_BRIEFING_EVIDENCE_ITEMS + 2)
        ]

        report = parse_briefings(json.dumps(briefing_report(project)))

        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(report.report_id, "report-1")
        self.assertEqual(len(report.projects), 1)
        parsed = report.projects[0]
        self.assertEqual(parsed.confidence, "high")
        self.assertNotIn("\x1b", parsed.summary)
        self.assertNotIn("\x07", parsed.summary)
        self.assertLessEqual(len(parsed.summary), MAX_BRIEFING_TEXT_LENGTH)
        self.assertEqual(len(parsed.completed_outputs), MAX_BRIEFING_ARRAY_ITEMS)
        self.assertEqual(len(parsed.blockers), MAX_BRIEFING_ARRAY_ITEMS)
        self.assertEqual(len(parsed.next_steps), MAX_BRIEFING_ARRAY_ITEMS)
        self.assertEqual(len(parsed.evidence), MAX_BRIEFING_EVIDENCE_ITEMS)
        self.assertIn("[bold]", parsed.next_steps[0].title)
        self.assertNotIn("\x1b", parsed.next_steps[0].detail)

    def test_literal_home_agent_artifact_accepts_nullable_incomplete_evidence(
        self,
    ) -> None:
        artifact = """{
          "schemaVersion": 1,
          "reportID": "home-agent-20260824",
          "generatedAt": "2026-08-24T10:05:00Z",
          "status": "running",
          "projects": [
            {
              "projectID": "queued-project",
              "projectPath": "/work/queued",
              "name": "Queued",
              "assessment": "unknown",
              "summary": "Research has not started.",
              "confidence": "low",
              "evidenceAt": null,
              "completedOutputs": [],
              "blockers": [],
              "nextSteps": [{
                "id": "wait-for-research",
                "title": "Wait for research",
                "detail": "No action is available yet.",
                "state": "blocked",
                "requiresApproval": true
              }],
              "evidence": [],
              "researchStatus": "queued"
            },
            {
              "projectID": "running-project",
              "projectPath": "/work/running",
              "name": "Running",
              "assessment": "waiting",
              "summary": "Research is in progress.",
              "confidence": "medium",
              "evidenceAt": null,
              "completedOutputs": [],
              "blockers": [],
              "nextSteps": [],
              "evidence": [],
              "researchStatus": "running"
            },
            {
              "projectID": "failed-project",
              "projectPath": "/work/failed",
              "name": "Failed",
              "assessment": "blocked",
              "summary": "Research failed before evidence was collected.",
              "confidence": "high",
              "evidenceAt": null,
              "completedOutputs": [],
              "blockers": [{"summary": "Research worker failed."}],
              "nextSteps": [],
              "evidence": [],
              "researchStatus": "failed"
            },
            {
              "projectID": "unknown-project",
              "projectPath": "/work/unknown",
              "name": "Unknown",
              "assessment": "unknown",
              "summary": "Completed research produced no current evidence.",
              "confidence": "low",
              "evidenceAt": null,
              "completedOutputs": [],
              "blockers": [],
              "nextSteps": [],
              "evidence": [],
              "researchStatus": "completed"
            }
          ]
        }"""

        report = parse_briefings(artifact)

        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(
            [project.confidence for project in report.projects],
            ["low", "medium", "high", "low"],
        )
        self.assertEqual(
            [project.research_status for project in report.projects],
            ["queued", "running", "failed", "completed"],
        )
        self.assertTrue(all(project.evidence_at is None for project in report.projects))

    def test_any_malformed_project_rejects_the_whole_briefing_artifact(self) -> None:
        self.assertIsNone(
            parse_briefings(
                briefing_report(briefing_project("/work/good", "good"), {})
            )
        )

        malformed_row = briefing_project("/work/bad", "bad")
        malformed_row["confidence"] = 0.5
        artifact = briefing_report(
            briefing_project("/work/good", "good"),
            malformed_row,
        )

        self.assertIsNone(parse_briefings(artifact))

        completed_without_evidence = briefing_project("/work/no-evidence")
        completed_without_evidence["evidenceAt"] = None
        self.assertIsNone(
            parse_briefings(briefing_report(completed_without_evidence))
        )

    def test_optional_briefing_file_missing_malformed_oversized_or_unsupported(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as base:
            path = Path(base) / "latest.json"
            self.assertIsNone(read_briefings_file(path))

            path.write_text("not-json", encoding="utf-8")
            self.assertIsNone(parse_briefings(read_briefings_file(path)))

            path.write_bytes(b"x" * (MAX_BRIEFINGS_FILE_BYTES + 1))
            self.assertIsNone(read_briefings_file(path))

        unsupported = briefing_report(briefing_project("/work/alpha"))
        unsupported["schemaVersion"] = 2
        self.assertIsNone(parse_briefings(json.dumps(unsupported)))

    def test_briefings_match_only_normalized_exact_project_paths(self) -> None:
        report = parse_briefings(
            json.dumps(
                briefing_report(
                    briefing_project("/work/alpha/", "unrelated-report-id"),
                    briefing_project("/work/alpha/subdir", "p1"),
                    briefing_project("/work/alpha-other", "p2"),
                )
            )
        )
        assert report is not None
        projects = (
            ProjectRecord(id="p1", directory="/work/alpha", name="Alpha"),
            ProjectRecord(id="p2", directory="/work/beta", name="Beta"),
        )

        matched = match_project_briefings(report.projects, projects)

        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0].project_id, "unrelated-report-id")
        self.assertEqual(matched[0].project_path, "/work/alpha/")

    async def test_malformed_optional_briefing_does_not_fail_collection(self) -> None:
        class CollectSource(DashboardSource):
            async def _api_status(self):
                return "offline", "test", {}, {}, (False, False)

            async def _command_json(self, *arguments, cwd=None, timeout=15):
                return []

            async def _collect_sessions(self, known_projects):
                return []

            async def _service_states(self):
                return ()

        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            artifact = root / "latest.json"
            artifact.write_text("{broken", encoding="utf-8")
            source = CollectSource(
                backend="v1",
                opencode_bin="/bin/false",
                projects_file=root / "missing.md",
                session_routes_file=root / "missing-routes.json",
                briefings_file=artifact,
                session_db_file=root / "missing-opencode.db",
                permission_state_dir=root / "permissions",
            )

            snapshot = await source.collect()

        self.assertEqual(snapshot.briefings, ())
        self.assertEqual(snapshot.briefing_status, "")
        self.assertEqual(snapshot.warning, "")

    async def test_collection_attaches_valid_briefing_metadata_by_path(self) -> None:
        class CollectSource(DashboardSource):
            def __init__(self, project_path: Path, **kwargs) -> None:
                super().__init__(backend="v1", **kwargs)
                self.project_path = project_path

            async def _api_status(self):
                return "offline", "test", {}, {}, (False, False)

            async def _command_json(self, *arguments, cwd=None, timeout=15):
                if arguments[:2] == ("debug", "scrap"):
                    return [{"id": "runtime-id", "worktree": str(self.project_path)}]
                return []

            async def _collect_sessions(self, known_projects):
                return [
                    {
                        "id": "session-1",
                        "title": "Alpha work",
                        "directory": str(self.project_path),
                        "projectId": "runtime-id",
                        "created": 1,
                        "updated": 2,
                    }
                ]

            async def _service_states(self):
                return ()

        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            project_path = root / "alpha"
            project_path.mkdir()
            artifact = root / "latest.json"
            artifact.write_text(
                json.dumps(briefing_report(briefing_project(str(project_path)))),
                encoding="utf-8",
            )
            source = CollectSource(
                project_path,
                opencode_bin="/bin/false",
                projects_file=root / "missing.md",
                session_routes_file=root / "missing-routes.json",
                briefings_file=artifact,
                session_db_file=root / "missing-opencode.db",
                permission_state_dir=root / "permissions",
            )

            snapshot = await source.collect()

        self.assertEqual(snapshot.briefing_report_id, "report-1")
        self.assertEqual(snapshot.briefing_status, "completed")
        self.assertEqual(len(snapshot.briefings), 1)
        self.assertEqual(snapshot.briefings[0].project_path, str(project_path))

    def test_briefings_file_configuration_prefers_argument_then_environment(self) -> None:
        with patch.dict(
            os.environ, {"OCDECK_BRIEFINGS_FILE": "/tmp/from-env.json"}
        ):
            from_env = DashboardSource(backend="v1", opencode_bin="/bin/false")
            explicit = DashboardSource(
                backend="v1",
                opencode_bin="/bin/false",
                briefings_file="/tmp/from-cli.json",
            )
        self.assertEqual(from_env.briefings_file, Path("/tmp/from-env.json"))
        self.assertEqual(explicit.briefings_file, Path("/tmp/from-cli.json"))

    def test_local_permission_state_tracks_only_live_opencode_processes(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            state_dir = root / "state"
            proc_root = root / "proc"
            state_dir.mkdir()
            process = proc_root / "123"
            process.mkdir(parents=True)
            (process / "comm").write_text("opencode\n", encoding="utf-8")
            (state_dir / "123.json").write_text(
                '{"pid":123,"permissions":[{"id":"perm-1",'
                '"sessionID":"session-1","permission":"bash",'
                '"pattern":"npm test"}],"questions":[{"id":"q-1",'
                '"sessionID":"session-2","question":"Choose a result"}],'
                '"statuses":[{"sessionID":"session-1","status":"busy"}]}',
                encoding="utf-8",
            )
            stale = state_dir / "456.json"
            stale.write_text(
                '{"pid":456,"permissions":[{"sessionID":"stale"}]}',
                encoding="utf-8",
            )

            self.assertEqual(
                read_local_permissions(state_dir, proc_root),
                {
                    "session-1": [
                        {
                            "id": "perm-1",
                            "permission": "bash",
                            "pattern": "npm test",
                        }
                    ],
                    "session-2": [
                        {
                            "id": "q-1",
                            "permission": "question",
                            "pattern": "Choose a result",
                        }
                    ],
                },
            )
            self.assertEqual(
                read_local_statuses(state_dir, proc_root), {"session-1": "busy"}
            )
            self.assertFalse(stale.exists())

    def test_local_statuses_use_newest_record_for_duplicate_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            state_dir = root / "state"
            proc_root = root / "proc"
            state_dir.mkdir()
            for pid in (123, 124):
                process = proc_root / str(pid)
                process.mkdir(parents=True)
                (process / "comm").write_text("opencode\n", encoding="utf-8")
            (state_dir / "123.json").write_text(
                '{"pid":123,"updated":100,"statuses":'
                '[{"sessionID":"session-1","status":"busy"}]}',
                encoding="utf-8",
            )
            (state_dir / "124.json").write_text(
                '{"pid":124,"updated":200,"statuses":'
                '[{"sessionID":"session-1","status":"idle"}]}',
                encoding="utf-8",
            )

            self.assertEqual(
                read_local_statuses(state_dir, proc_root),
                {"session-1": "idle"},
            )

    def test_newer_server_status_overrides_stale_standalone_owner(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            state_dir = root / "state"
            proc_root = root / "proc"
            state_dir.mkdir()
            for pid, command in (
                (123, b"opencode\0/work/energy\0--session\0shared-session\0"),
                (124, b"opencode\0web\0--port\0" b"4096\0"),
            ):
                process = proc_root / str(pid)
                process.mkdir(parents=True)
                (process / "comm").write_text("opencode\n")
                (process / "cmdline").write_bytes(command)
                fields = ["S"] + ["0"] * 18 + [str(pid * 100)]
                (process / "stat").write_text(f"{pid} (opencode) {' '.join(fields)}\n")

            # The owner's file can be rewritten for another session while its
            # shared-session status remains old. Use each status event's time.
            for owner_status, owner_time, server_status, server_time, expected in (
                ("idle", 100, "busy", 200, "busy"),
                ("idle", 100, "retry", 200, "retry"),
                ("busy", 100, "idle", 200, "idle"),
                ("idle", 200, "busy", 100, "idle"),
                ("idle", 200, "busy", 200, "idle"),
            ):
                with self.subTest(owner=owner_status, server=server_status,
                                  owner_time=owner_time, server_time=server_time):
                    for pid, filename, status, timestamp, file_time in (
                        (123, "z-owner.json", owner_status, owner_time, 9000),
                        (124, "a-server.json", server_status, server_time, 8000),
                    ):
                        (state_dir / filename).write_text(json.dumps({
                            "pid": pid,
                            "notifierVersion": 8,
                            "processStartTicks": str(pid * 100),
                            "workspace": "/work/energy",
                            "updated": file_time,
                            "statuses": [{
                                "sessionID": "shared-session",
                                "status": status,
                                "updated": timestamp,
                            }],
                        }))
                    self.assertEqual(
                        read_local_runtime_state(state_dir, proc_root).statuses,
                        {"shared-session": expected},
                    )

    def test_newer_idle_producer_suppresses_older_stale_request(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            state_dir = root / "state"
            proc_root = root / "proc"
            state_dir.mkdir()
            for pid in (123, 124):
                process = proc_root / str(pid)
                process.mkdir(parents=True)
                (process / "comm").write_text("opencode\n", encoding="utf-8")
            (proc_root / "124" / "cmdline").write_bytes(
                b"opencode\0--session=shared-session\0"
            )

            (state_dir / "123.json").write_text(
                json.dumps(
                    {
                        "pid": 123,
                        "updated": 300,
                        "permissions": [
                            {
                                "id": "stale-permission",
                                "sessionID": "shared-session",
                                "permission": "external_directory",
                                "pattern": "/usr/include",
                                "updated": 100,
                            }
                        ],
                        "statuses": [
                            {
                                "sessionID": "shared-session",
                                "status": "busy",
                                "updated": 100,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (state_dir / "124.json").write_text(
                json.dumps(
                    {
                        "pid": 124,
                        "updated": 200,
                        "statuses": [
                            {
                                "sessionID": "shared-session",
                                "status": "idle",
                                "updated": 200,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(read_local_permissions(state_dir, proc_root), {})

            newer_request = json.loads((state_dir / "123.json").read_text())
            newer_request["permissions"][0]["updated"] = 250
            newer_request["statuses"][0]["updated"] = 250
            (state_dir / "123.json").write_text(
                json.dumps(newer_request), encoding="utf-8"
            )
            self.assertEqual(
                list(read_local_permissions(state_dir, proc_root)), ["shared-session"]
            )

    def test_newer_unowned_idle_does_not_hide_other_producers_request(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            state_dir = root / "state"
            proc_root = root / "proc"
            state_dir.mkdir()
            for pid in (123, 124):
                process = proc_root / str(pid)
                process.mkdir(parents=True)
                (process / "comm").write_text("opencode\n", encoding="utf-8")
            (state_dir / "123.json").write_text(
                json.dumps(
                    {
                        "pid": 123,
                        "updated": 100,
                        "permissions": [
                            {"id": "real", "sessionID": "shared-session"}
                        ],
                        "statuses": [
                            {"sessionID": "shared-session", "status": "busy"}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (state_dir / "124.json").write_text(
                json.dumps(
                    {
                        "pid": 124,
                        "updated": 200,
                        "statuses": [
                            {"sessionID": "shared-session", "status": "idle"}
                        ],
                    }
                ),
                encoding="utf-8",
            )

            state = read_local_runtime_state(state_dir, proc_root)
            self.assertEqual(list(state.permissions), ["shared-session"])
            self.assertEqual(state.statuses, {"shared-session": "idle"})

    def test_same_producer_idle_suppresses_requests_and_questions(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            state_dir = root / "state"
            proc_root = root / "proc"
            state_dir.mkdir()
            process = proc_root / "123"
            process.mkdir(parents=True)
            (process / "comm").write_text("opencode\n", encoding="utf-8")
            (state_dir / "123.json").write_text(
                json.dumps(
                    {
                        "pid": 123,
                        "permissions": [
                            {"id": "p", "sessionID": "idle-session"}
                        ],
                        "questions": [
                            {"id": "q", "sessionID": "idle-session"}
                        ],
                        "statuses": [
                            {"sessionID": "idle-session", "status": "idle"}
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(read_local_permissions(state_dir, proc_root), {})

    def test_reused_pid_state_is_rejected_by_process_start_identity(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            state_dir = root / "state"
            proc_root = root / "proc"
            state_dir.mkdir()
            process = proc_root / "123"
            process.mkdir(parents=True)
            (process / "comm").write_text("opencode\n", encoding="utf-8")
            fields = ["S"] + ["0"] * 18 + ["12345"]
            (process / "stat").write_text(
                f"123 (opencode) {' '.join(fields)}\n", encoding="utf-8"
            )
            state = state_dir / "123.json"
            state.write_text(
                json.dumps(
                    {
                        "pid": 123,
                        "processStartTicks": "99999",
                        "permissions": [{"sessionID": "stale"}],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(read_local_permissions(state_dir, proc_root), {})
            self.assertFalse(state.exists())

    def test_v8_missing_identity_and_zombie_producers_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            state_dir = root / "state"
            proc_root = root / "proc"
            state_dir.mkdir()
            for pid, process_state in ((123, "S"), (124, "Z")):
                process = proc_root / str(pid)
                process.mkdir(parents=True)
                (process / "comm").write_text("opencode\n", encoding="utf-8")
                fields = [process_state] + ["0"] * 18 + [f"{pid}00"]
                (process / "stat").write_text(
                    f"{pid} (opencode) {' '.join(fields)}\n", encoding="utf-8"
                )
            missing_identity = state_dir / "123.json"
            missing_identity.write_text(
                json.dumps(
                    {
                        "pid": 123,
                        "notifierVersion": 8,
                        "permissions": [{"sessionID": "missing-identity"}],
                    }
                ),
                encoding="utf-8",
            )
            zombie = state_dir / "124.json"
            zombie.write_text(
                json.dumps(
                    {
                        "pid": 124,
                        "notifierVersion": 8,
                        "processStartTicks": "12400",
                        "permissions": [{"sessionID": "zombie"}],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(read_local_permissions(state_dir, proc_root), {})
            self.assertFalse(missing_identity.exists())
            self.assertFalse(zombie.exists())

    def test_local_plugin_state_is_isolated_by_backend(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            state_dir = root / "state"
            proc_root = root / "proc"
            state_dir.mkdir()
            for pid, executable, session_id in (
                (123, "opencode", "ses_v1"),
                (124, "opencode2", "ses_v2"),
            ):
                process = proc_root / str(pid)
                process.mkdir(parents=True)
                (process / "comm").write_text(executable + "\n", encoding="utf-8")
                (process / "cmdline").write_bytes(
                    f"{executable}\0--session\0{session_id}\0".encode("utf-8")
                )
                (state_dir / f"{pid}.json").write_text(
                    json.dumps(
                        {
                            "pid": pid,
                            "permissions": [
                                {
                                    "id": f"per_{pid}",
                                    "sessionID": session_id,
                                    "permission": "shell",
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )

            v1 = read_local_permissions(state_dir, proc_root, backend="v1")
            v2 = read_local_permissions(state_dir, proc_root, backend="v2")

            self.assertEqual(set(v1), {"ses_v1"})
            self.assertEqual(set(v2), {"ses_v2"})
            self.assertTrue((state_dir / "123.json").exists())
            self.assertTrue((state_dir / "124.json").exists())

    def test_unknown_or_older_idle_status_does_not_suppress_new_request(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            state_dir = root / "state"
            proc_root = root / "proc"
            state_dir.mkdir()
            process = proc_root / "123"
            process.mkdir(parents=True)
            (process / "comm").write_text("opencode\n", encoding="utf-8")
            state = state_dir / "123.json"
            state.write_text(
                json.dumps(
                    {
                        "pid": 123,
                        "updated": 300,
                        "permissions": [
                            {
                                "id": "new",
                                "sessionID": "session-1",
                                "updated": 300,
                            }
                        ],
                        "statuses": [
                            {
                                "sessionID": "session-1",
                                "status": "paused",
                                "updated": 400,
                            },
                            {
                                "sessionID": "session-1",
                                "status": "idle",
                                "updated": 200,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            runtime = read_local_runtime_state(state_dir, proc_root)
            self.assertEqual(list(runtime.permissions), ["session-1"])
            self.assertEqual(runtime.statuses, {"session-1": "idle"})

    def test_session_deletion_tombstone_suppresses_older_other_producer_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            state_dir = root / "state"
            proc_root = root / "proc"
            state_dir.mkdir()
            for pid in (123, 124):
                process = proc_root / str(pid)
                process.mkdir(parents=True)
                (process / "comm").write_text("opencode\n", encoding="utf-8")
            (state_dir / "123.json").write_text(
                json.dumps(
                    {
                        "pid": 123,
                        "updated": 100,
                        "permissions": [
                            {"id": "old", "sessionID": "deleted", "updated": 100}
                        ],
                        "statuses": [
                            {"sessionID": "deleted", "status": "busy", "updated": 100}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (state_dir / "124.json").write_text(
                json.dumps(
                    {
                        "pid": 124,
                        "updated": 200,
                        "deletedSessions": [
                            {"sessionID": "deleted", "updated": 200}
                        ],
                    }
                ),
                encoding="utf-8",
            )

            runtime = read_local_runtime_state(state_dir, proc_root)
            self.assertEqual(runtime.permissions, {})
            self.assertEqual(runtime.statuses, {})

    def test_cleanup_never_unlinks_a_new_atomic_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            path = Path(base) / "producer.json"
            path.write_text("not json", encoding="utf-8")
            payload, identity = read_state_payload(path)
            self.assertIsNone(payload)
            path.unlink()
            path.write_text('{"pid":123}', encoding="utf-8")

            unlink_same_file(path, identity)

            self.assertTrue(path.exists())
            self.assertEqual(json.loads(path.read_text()), {"pid": 123})

    def test_malformed_and_oversized_live_producer_files_are_removed(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            state_dir = root / "state"
            proc_root = root / "proc"
            state_dir.mkdir()
            for pid in (123, 124):
                process = proc_root / str(pid)
                process.mkdir(parents=True)
                (process / "comm").write_text("opencode\n", encoding="utf-8")
            malformed = state_dir / "123.json"
            malformed.write_text('{"pid":123', encoding="utf-8")
            oversized = state_dir / "124.json"
            oversized.write_text(
                "x" * (MAX_PERMISSION_STATE_BYTES + 1), encoding="utf-8"
            )

            self.assertEqual(read_local_runtime_state(state_dir, proc_root).permissions, {})
            self.assertFalse(malformed.exists())
            self.assertFalse(oversized.exists())

    def test_api_status_wins_only_for_sessions_represented_by_the_api(self) -> None:
        self.assertEqual(
            reconcile_statuses(
                {"shared": "busy"},
                {"shared": "idle", "standalone": "busy"},
            ),
            {"shared": "busy", "standalone": "busy"},
        )

    def test_api_status_payload_is_transactional_and_never_defaults_to_idle(self) -> None:
        self.assertEqual(
            parse_api_statuses(
                {"busy-session": {"type": "busy"}, "idle-session": "idle"}
            ),
            {"busy-session": "busy", "idle-session": "idle"},
        )
        self.assertIsNone(
            parse_api_statuses({"busy-session": "busy", "bad": "paused"})
        )
        self.assertIsNone(parse_api_statuses([]))

    def test_request_reconciliation_is_scoped_by_session_and_kind(self) -> None:
        api_permission = {
            "id": "api-permission",
            "permission": "bash",
            "pattern": "npm test",
        }
        local_permission = {
            "id": "local-permission",
            "permission": "edit",
            "pattern": "README.md",
        }
        local_question = {
            "id": "local-question",
            "permission": "question",
            "pattern": "Choose one",
        }
        result = reconcile_permissions(
            {"api-session": [api_permission]},
            {
                "api-session": [local_permission, local_question],
                "standalone-session": [local_permission],
            },
            (True, False),
            {"api-session"},
        )

        self.assertEqual(result["api-session"], [api_permission, local_question])
        self.assertEqual(result["standalone-session"], [local_permission])
        self.assertEqual(
            reconcile_permissions({}, {"standalone-session": [local_permission]}, (True, True)),
            {"standalone-session": [local_permission]},
        )

    def test_local_and_api_permissions_are_deduplicated(self) -> None:
        request = {
            "id": "perm-1",
            "permission": "bash",
            "pattern": "npm test",
        }
        self.assertEqual(
            merge_permissions(
                {"session-1": [request]},
                {"session-1": [request], "session-2": [{"permission": "edit"}]},
            ),
            {
                "session-1": [request],
                "session-2": [
                    {"id": "", "permission": "edit", "pattern": ""}
                ],
            },
        )

    async def test_unprotected_api_fetches_live_status(self) -> None:
        source = FakeApiSource()
        with patch.dict(os.environ, {}, clear=True):
            state, detail, statuses, permissions, authoritative = (
                await source._api_status()
            )
        self.assertEqual(state, "live")
        self.assertIn("test", detail)
        self.assertEqual(statuses, {"session-1": "busy"})
        self.assertEqual(
            permissions,
            {
                "session-1": [
                    {
                        "id": "perm-1",
                        "permission": "bash",
                        "pattern": "npm test",
                    }
                ],
                "session-2": [
                    {
                        "id": "question-1",
                        "permission": "question",
                        "pattern": "What happened after pressing the key?",
                    }
                ],
            },
        )
        self.assertEqual(authoritative, (True, True))
        self.assertEqual(
            source.paths,
            ["/global/health", "/session/status", "/permission", "/question"],
        )

    async def test_named_agent_registry_combines_files_api_models_and_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            home_agent_file = root / "home_agent.md"
            jarvis_file = root / "jarvis.md"
            home_agent_file.write_text("agent", encoding="utf-8")
            jarvis_file.write_text("agent", encoding="utf-8")
            source = DashboardSource(
                backend="v1",
                api_url="http://127.0.0.1:4096",
                opencode_bin="/bin/false",
                server_env_file=root / "missing.env",
                named_agent_files={
                    "home_agent": home_agent_file,
                    "jarvis": jarvis_file,
                    "jasmine": root / "missing.md",
                },
            )

            def request(path, _password, **_kwargs):
                if path == "/agent":
                    return [
                        {
                            "name": "home_agent",
                            "description": "Coordinates projects",
                            "model": {
                                "providerID": "openrouter",
                                "modelID": "gpt-4.1-nano",
                            },
                        },
                        {
                            "name": "jarvis",
                            "model": {
                                "providerID": "openai",
                                "modelID": "gpt-5.6-sol",
                            },
                        },
                    ]
                if path == "/mcp":
                    return {"signed_in_tabs": {"status": "connected"}}
                raise AssertionError(path)

            with mock.patch.object(source, "_request_json", side_effect=request):
                with patch.dict(os.environ, {}, clear=True):
                    statuses, stale, error, browser, browser_stale, browser_error = (
                        await source._refresh_named_agent_registry()
                    )

        by_name = {status.name: status for status in statuses}
        self.assertTrue(by_name["home_agent"].configured)
        self.assertTrue(by_name["home_agent"].loaded)
        self.assertEqual(by_name["home_agent"].role, "Project orchestration")
        self.assertEqual(by_name["home_agent"].model, "openrouter/gpt-4.1-nano")
        self.assertEqual(by_name["home_agent"].description, "Coordinates projects")
        self.assertEqual(by_name["maverik"].model, "openai/gpt-5.6-sol")
        self.assertFalse(by_name["jasmine"].configured)
        self.assertFalse(by_name["jasmine"].loaded)
        self.assertFalse(stale)
        self.assertEqual(error, "")
        self.assertEqual(browser, "connected")
        self.assertFalse(browser_stale)
        self.assertEqual(browser_error, "")

    async def test_named_agent_registry_retains_last_good_result_when_stale(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            source = DashboardSource(
                backend="v1",
                api_url="http://127.0.0.1:4096",
                opencode_bin="/bin/false",
                server_env_file=root / "missing.env",
                named_agent_files={
                    "home_agent": root / "home_agent.md",
                    "jarvis": root / "jarvis.md",
                    "jasmine": root / "jasmine.md",
                },
            )
            for path in source.named_agent_files.values():
                path.write_text("agent", encoding="utf-8")

            def healthy(path, _password, **_kwargs):
                if path == "/agent":
                    return [
                        {
                            "name": "jarvis",
                            "model": {
                                "providerID": "openai",
                                "modelID": "gpt-5.6-sol",
                            },
                        }
                    ]
                if path == "/mcp":
                    return {"signed_in_tabs": None}
                raise AssertionError(path)

            with mock.patch.object(source, "_request_json", side_effect=healthy):
                first, stale, error, browser, browser_stale, browser_error = (
                    await source._refresh_named_agent_registry()
                )
            self.assertFalse(stale)
            self.assertEqual(error, "")
            self.assertEqual(browser, "not configured")
            self.assertFalse(browser_stale)
            self.assertEqual(browser_error, "")
            self.assertTrue({item.name: item for item in first}["maverik"].loaded)

            source.named_agent_files["jarvis"].unlink()
            with mock.patch.object(
                source, "_request_json", side_effect=urllib.error.URLError("private")
            ):
                cached, stale, error, browser, browser_stale, browser_error = (
                    await source._refresh_named_agent_registry()
                )

            cached_by_name = {item.name: item for item in cached}
            self.assertEqual(
                cached_by_name["maverik"].model,
                {item.name: item for item in first}["maverik"].model,
            )
            self.assertFalse(cached_by_name["maverik"].configured)
            self.assertTrue(stale)
            self.assertEqual(error, "Agent registry unavailable")
            self.assertEqual(browser, "not configured")
            self.assertTrue(browser_stale)
            self.assertEqual(browser_error, "Browser MCP unavailable")
            self.assertNotIn("private", error)

    async def test_mcp_failure_marks_only_retained_browser_state_stale(self) -> None:
        source = DashboardSource(
            backend="v1",
            api_url="http://127.0.0.1:4096",
            opencode_bin="/bin/false",
            server_env_file="/nonexistent/server.env",
        )

        def first_request(path, _password, **_kwargs):
            if path == "/agent":
                return [{"name": "jarvis"}]
            if path == "/mcp":
                return {"signed_in_tabs": {"status": "connected"}}
            raise AssertionError(path)

        with mock.patch.object(source, "_request_json", side_effect=first_request):
            await source._refresh_named_agent_registry()

        def mcp_failure(path, _password, **_kwargs):
            if path == "/agent":
                return [{"name": "jarvis"}]
            raise urllib.error.URLError("mcp unavailable")

        with mock.patch.object(source, "_request_json", side_effect=mcp_failure):
            registry, registry_stale, _error, browser, browser_stale, browser_error = (
                await source._refresh_named_agent_registry()
            )

        self.assertFalse(registry_stale)
        self.assertTrue({item.name: item for item in registry}["maverik"].loaded)
        self.assertEqual(browser, "connected")
        self.assertTrue(browser_stale)
        self.assertEqual(browser_error, "Browser MCP unavailable")

    async def test_partial_pending_endpoint_failure_is_not_globally_authoritative(
        self,
    ) -> None:
        class PartialSource(FakeApiSource):
            def _request_json(self, path, password, **kwargs):
                if path == "/question":
                    return {"malformed": True}
                return super()._request_json(path, password, **kwargs)

        source = PartialSource()
        with patch.dict(os.environ, {}, clear=True):
            _state, _detail, statuses, permissions, authority = (
                await source._api_status()
            )

        self.assertEqual(authority, (True, False))
        self.assertEqual(statuses, {"session-1": "busy"})
        self.assertEqual(set(permissions), {"session-1"})

    async def test_malformed_pending_rows_fail_the_endpoint_transactionally(self) -> None:
        source = DashboardSource(
            backend="v1",
            api_url="http://127.0.0.1:4096",
            opencode_bin="/bin/false",
            server_env_file="/nonexistent/server.env",
        )
        malformed_permissions = [
            {
                "id": "valid",
                "sessionID": "session-1",
                "permission": "bash",
                "patterns": [],
            },
            None,
        ]
        with mock.patch.object(
            source, "_request_json", return_value=malformed_permissions
        ):
            self.assertEqual(await source._api_permissions(""), ({}, False))

        malformed_patterns = [
            {
                "id": "bad-pattern",
                "sessionID": "session-1",
                "permission": "bash",
                "patterns": [{"command": "npm test"}],
            }
        ]
        with mock.patch.object(
            source, "_request_json", return_value=malformed_patterns
        ):
            self.assertEqual(await source._api_permissions(""), ({}, False))

        malformed_questions = [
            {
                "id": "missing-session",
                "questions": [{"question": "Choose"}],
            }
        ]
        with mock.patch.object(
            source, "_request_json", return_value=malformed_questions
        ):
            self.assertEqual(await source._api_questions(""), ({}, False))

    def test_signed_in_tabs_status_is_independent_from_agent_loading(self) -> None:
        self.assertEqual(
            parse_signed_in_tabs_status(
                {"signed_in_tabs": {"status": "connected"}}
            ),
            "connected",
        )
        self.assertEqual(
            parse_signed_in_tabs_status({"signed_in_tabs": None}),
            "not configured",
        )
        self.assertIsNone(parse_signed_in_tabs_status([]))
        self.assertIsNone(
            parse_signed_in_tabs_status({"signed_in_tabs": "connected"})
        )

    def test_server_credentials_load_only_expected_values_without_export(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            env_file = Path(base) / "server.env"
            env_file.write_text(
                "OPENCODE_SERVER_USERNAME=test-user\n"
                "OPENCODE_SERVER_PASSWORD='test-password'\n"
                "UNRELATED_SECRET=ignored\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(
                    read_server_credentials(env_file),
                    ("test-user", "test-password"),
                )
                source = DashboardSource(
                    backend="v1",
                    opencode_bin="/bin/false", server_env_file=env_file
                )
                self.assertEqual(source._password(), "test-password")
                self.assertNotIn("OPENCODE_SERVER_PASSWORD", os.environ)

    async def test_approve_permission_uses_open_code_permission_route(self) -> None:
        source = DashboardSource(
            backend="v1",
            api_url="http://127.0.0.1:4096",
            opencode_bin="/bin/false",
            server_env_file="/nonexistent/server.env",
        )
        with mock.patch.object(source, "_request_json", return_value=None) as request:
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(
                    await source.approve_permission("session-1", "perm-1"), ""
                )
        request.assert_called_once_with(
            "/session/session-1/permissions/perm-1",
            "",
            method="POST",
            payload={"response": "once"},
        )

    async def test_rename_session_patches_the_loopback_api(self) -> None:
        source = FakeApiSource()
        with patch.dict(os.environ, {}, clear=True):
            error = await source.rename_session("ses_one", "New title")
        self.assertEqual(error, "")
        self.assertEqual(
            source.patches,
            [("/session/ses_one", "PATCH", {"title": "New title"})],
        )

    async def test_rename_session_reports_api_failures_without_raising(self) -> None:
        source = FakeApiSource()
        cases = [
            (
                urllib.error.HTTPError(
                    "http://127.0.0.1:4096/session/x",
                    401,
                    "locked",
                    None,
                    None,
                ),
                "API locked; set OPENCODE_SERVER_PASSWORD to rename",
            ),
            (
                urllib.error.HTTPError(
                    "http://127.0.0.1:4096/session/x",
                    500,
                    "boom",
                    None,
                    None,
                ),
                "API returned HTTP 500",
            ),
            (urllib.error.URLError("refused"), "API unavailable; title kept unchanged"),
            (TimeoutError("slow"), "API unavailable; title kept unchanged"),
        ]
        for raised, expected in cases:
            with mock.patch.object(source, "_request_json", side_effect=raised):
                with patch.dict(os.environ, {}, clear=True):
                    message = await source.rename_session("ses_x", "t")
            self.assertEqual(message, expected)

    async def test_rename_session_reports_locked_credentials_with_password(self) -> None:
        source = FakeApiSource()
        error = urllib.error.HTTPError(
            "http://127.0.0.1:4096/session/x",
            401,
            "locked",
            None,
            None,
        )
        with mock.patch.object(source, "_request_json", side_effect=error):
            with patch.dict(
                os.environ, {"OPENCODE_SERVER_PASSWORD": "secret"}, clear=True
            ):
                message = await source.rename_session("ses_x", "t")
        self.assertEqual(message, "API rejected the configured credentials")

    def test_rename_refuses_non_loopback_credentials_and_bad_urls(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            source = DashboardSource(
                backend="v1",
                api_url="http://10.0.0.5:4096",
                opencode_bin="/bin/false",
                projects_file=root / "projects.md",
            )
            with patch.dict(
                os.environ, {"OPENCODE_SERVER_PASSWORD": "secret"}, clear=True
            ):
                refusal = asyncio.run(source.rename_session("ses_one", "t"))
            self.assertEqual(refusal, "Refusing credentials over non-loopback HTTP")

            invalid = DashboardSource(
                backend="v1",
                api_url="http://127.0.0.1:99999",
                opencode_bin="/bin/false",
                projects_file=root / "projects.md",
            )
            message = asyncio.run(invalid.rename_session("ses_one", "t"))
            self.assertEqual(message, "Invalid OpenCode API URL")

    async def test_timed_out_process_is_reaped(self) -> None:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        result = await communicate_with_cleanup(process, timeout=0.05)
        self.assertIsNone(result)
        self.assertIsNotNone(process.returncode)

    async def test_session_metadata_retries_transient_cli_failure(self) -> None:
        class RetrySource(DashboardSource):
            def __init__(self) -> None:
                super().__init__(backend="v1", opencode_bin="/bin/false")
                self.calls: dict[str, int] = {}

            async def _command_json(
                self,
                *arguments: str,
                cwd: Path | None = None,
                timeout: float = 15,
            ):
                key = str(cwd)
                self.calls[key] = self.calls.get(key, 0) + 1
                if self.calls[key] == 1:
                    return None
                return [
                    {
                        "id": f"session-{len(self.calls)}",
                        "title": "Recovered metadata",
                        "updated": 10,
                    }
                ]

        source = RetrySource()
        sessions = await source._collect_sessions({"project": "/tmp"})

        self.assertIsNotNone(sessions)
        self.assertTrue(sessions)
        self.assertTrue(all(count == 2 for count in source.calls.values()))

    async def test_session_metadata_api_includes_child_sessions(self) -> None:
        class ChildSource(DashboardSource):
            def __init__(self) -> None:
                super().__init__(
                    backend="v1",
                    api_url="http://127.0.0.1:4096",
                    opencode_bin="/bin/false",
                )
                self.paths: list[str] = []

            def _request_json(self, path, password, *, method="GET", payload=None):
                self.paths.append(path)
                return [
                    {
                        "id": "parent",
                        "title": "Home Agent",
                        "directory": "/tmp",
                        "updated": 10,
                    },
                    {
                        "id": "child",
                        "parentID": "parent",
                        "title": "Worker",
                        "directory": "/tmp",
                        "updated": 11,
                    },
                ]

            async def _command_json(self, *arguments, cwd=None, timeout=15):
                raise AssertionError("API metadata should avoid the roots-only CLI")

        source = ChildSource()
        with patch.dict(os.environ, {}, clear=True):
            sessions = await source._collect_sessions({"project": "/tmp"})

        self.assertEqual({item["id"] for item in sessions or []}, {"parent", "child"})
        self.assertEqual(next(item for item in sessions if item["id"] == "child")["parentID"], "parent")
        self.assertTrue(source.paths)
        self.assertTrue(all("roots=false" in path for path in source.paths))

    def test_api_url_validation_and_credential_policy(self) -> None:
        self.assertEqual(validate_api_url("http://127.0.0.1:4096"), "")
        self.assertNotEqual(validate_api_url("file:///tmp/socket"), "")
        self.assertTrue(api_credentials_are_safe("http://localhost:4096"))
        self.assertTrue(api_credentials_are_safe("https://example.com"))
        self.assertFalse(api_credentials_are_safe("http://example.com"))

    def test_opencode_command_classification(self) -> None:
        self.assertEqual(
            classify_opencode_command(
                ["/home/user/.opencode/bin/opencode", "/work", "--session", "ses_one"]
            ),
            (True, "ses_one"),
        )
        self.assertEqual(
            classify_opencode_command(["opencode", "-s=ses_two", "--auto"]),
            (True, "ses_two"),
        )
        self.assertEqual(
            classify_opencode_command(["opencode", "--session=ses_three"]),
            (True, "ses_three"),
        )
        self.assertEqual(classify_opencode_command(["opencode"]), (True, ""))
        self.assertEqual(
            classify_opencode_command(["opencode", "--session", "ses_one", "--fork"]),
            (True, ""),
        )
        self.assertEqual(
            classify_opencode_command(["opencode", "web", "--port", "4096"]),
            (False, ""),
        )
        self.assertEqual(
            classify_opencode_command(["opencode", "session", "list"]),
            (False, ""),
        )
        self.assertEqual(
            classify_opencode_command(["opencode2", "--session", "ses_v2"]),
            (True, "ses_v2"),
        )
        self.assertEqual(
            classify_opencode_command(["opencode2", "mini", "-s=ses_mini"]),
            (True, "ses_mini"),
        )
        self.assertEqual(
            classify_opencode_command(["opencode2.exe", "serve", "--service"]),
            (False, ""),
        )
        self.assertEqual(
            classify_opencode_command(["opencode2", "api", "v2.health.get"]),
            (False, ""),
        )

    def test_proc_scan_filters_v1_and_v2_and_excludes_v2_service(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            proc_root = Path(base)

            def add_process(pid: int, comm: str, arguments: list[str]) -> None:
                process = proc_root / str(pid)
                process.mkdir()
                (process / "comm").write_text(comm + "\n", encoding="utf-8")
                (process / "cmdline").write_bytes(
                    b"\0".join(argument.encode() for argument in arguments) + b"\0"
                )

            add_process(100, "opencode", ["opencode", "--session", "ses_shared"])
            add_process(101, "opencode2", ["opencode2", "--session", "ses_shared"])
            add_process(102, "opencode2", ["opencode2", "mini", "-s", "ses_mini"])
            add_process(103, "opencode2.exe", ["opencode2.exe", "serve", "--service"])
            add_process(104, "opencode2", ["opencode2", "api", "v2.health.get"])

            v1 = read_opencode_instances(proc_root, backend="v1")
            v2 = read_opencode_instances(proc_root, backend="v2")

        self.assertEqual(v1[:2], ({"ses_shared": 1}, 0))
        self.assertEqual(v2[:2], ({"ses_shared": 1, "ses_mini": 1}, 0))

    def test_proc_scan_counts_duplicate_and_unlinked_tuis(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            proc_root = Path(base)

            def add_process(pid: int, comm: str, arguments: list[str] | None) -> None:
                process = proc_root / str(pid)
                process.mkdir()
                (process / "comm").write_text(comm + "\n")
                if arguments is not None:
                    (process / "cmdline").write_bytes(
                        b"\0".join(argument.encode() for argument in arguments) + b"\0"
                    )

            add_process(100, "opencode", ["opencode", "--session", "ses_same"])
            add_process(101, "opencode", ["opencode", "-s=ses_same"])
            add_process(102, "opencode", ["opencode", "/work/project"])
            add_process(103, "opencode", ["opencode", "web", "--port", "4096"])
            add_process(104, "python3", ["python3", "opencode", "--session", "ses_fake"])
            add_process(105, "opencode", None)

            (proc_root / "100" / "fd").mkdir()
            os.symlink("/dev/pts/3", proc_root / "100" / "fd" / "0")
            (proc_root / "101" / "fd").mkdir()
            os.symlink("/dev/pts/7", proc_root / "101" / "fd" / "0")

            counts, unmapped, ttys = read_opencode_instances(proc_root)
            direct = read_process_tty(proc_root / "100")

        self.assertEqual(counts, {"ses_same": 2})
        self.assertEqual(unmapped, 1)
        self.assertEqual(ttys, {"ses_same": ("/dev/pts/3", "/dev/pts/7")})
        self.assertEqual(direct, "/dev/pts/3")

    def test_tmux_tty_mapping_resolves_session_names(self) -> None:
        fake_result = mock.Mock(returncode=0)
        fake_result.stdout = (
            b"/dev/pts/3\toc-ses_same\t1\n/dev/pts/9\tmain\t0\n"
        )
        with mock.patch(
            "ocdeck.source.subprocess.run", return_value=fake_result
        ) as runner:
            mapped, attached = read_tmux_tty_state(
                {"ses_same": ("/dev/pts/3", "/dev/pts/8"), "ses_other": ("/dev/pts/9",)}
            )
        self.assertEqual(mapped, {"ses_same": ("oc-ses_same",), "ses_other": ("main",)})
        self.assertEqual(attached, {"ses_same": True, "ses_other": False})
        command = runner.call_args.args[0]
        self.assertEqual(command[:3], ["tmux", "list-panes", "-a"])

        with mock.patch("ocdeck.source.subprocess.run", return_value=fake_result):
            self.assertEqual(
                read_tmux_tty_sessions({"ses_same": ("/dev/pts/3",)}),
                {"ses_same": ("oc-ses_same",)},
            )

    def test_tmux_tty_mapping_ignores_tmux_failures(self) -> None:
        with mock.patch(
            "ocdeck.source.subprocess.run", side_effect=OSError("no tmux")
        ):
            self.assertEqual(read_tmux_tty_sessions({"s": ("/dev/pts/3",)}), {})
        self.assertEqual(read_tmux_tty_sessions({}), {})

    def test_live_opencode_panes_keep_exact_duplicate_session_panes(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            proc_root = Path(base)

            def add_process(pid: int, tty: str, session_id: str = "ses_same") -> None:
                process = proc_root / str(pid)
                process.mkdir()
                (process / "comm").write_text("opencode\n", encoding="utf-8")
                arguments = ["opencode"]
                if session_id:
                    arguments.extend(("--session", session_id))
                (process / "cmdline").write_bytes(
                    b"\0".join(item.encode() for item in arguments) + b"\0"
                )
                (process / "stat").write_text(
                    f"{pid} (opencode) S " + " ".join(["0"] * 18 + [str(pid * 10)]),
                    encoding="ascii",
                )
                (process / "fd").mkdir()
                os.symlink(tty, process / "fd" / "0")

            add_process(100, "/dev/pts/3")
            add_process(101, "/dev/pts/7")
            add_process(102, "/dev/pts/8", "")
            result = mock.Mock(returncode=0)
            result.stdout = (
                b"%3\t/dev/pts/3\toc-one\t0\t0\t1\t1\t1\t0\n"
                b"%7\t/dev/pts/7\toc-one\t0\t1\t0\t0\t0\t0\n"
                b"%8\t/dev/pts/8\toc-unlinked\t1\t0\t1\t1\t1\t0\n"
            )
            with mock.patch(
                "ocdeck.source.subprocess.run", return_value=result
            ) as runner:
                panes = read_live_opencode_panes(proc_root)

        self.assertEqual([pane.pane_id for pane in panes], ["%3", "%8", "%7"])
        self.assertEqual([pane.session_id for pane in panes], ["ses_same", "", "ses_same"])
        self.assertEqual(
            [pane.terminal_state for pane in panes],
            ["foreground", "foreground", "background"],
        )
        self.assertEqual(len({pane.destination_id for pane in panes}), 3)
        self.assertTrue(all(pane.destination_id.startswith("dst_") for pane in panes))
        legacy_identity = "\0".join(
            (
                "ocdeck-destination-v1",
                "%3",
                "/dev/pts/3",
                "100:1000:ses_same",
            )
        )
        legacy_destination = "dst_" + hashlib.sha256(
            legacy_identity.encode("utf-8")
        ).hexdigest()[:32]
        self.assertEqual(panes[0].destination_id, legacy_destination)
        self.assertEqual(runner.call_args.args[0][:3], ["tmux", "list-panes", "-a"])

    def test_live_opencode_panes_fail_closed_on_ambiguous_or_dead_panes(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            proc_root = Path(base)
            for pid, session_id in ((100, "ses_one"), (101, "ses_two")):
                process = proc_root / str(pid)
                process.mkdir()
                (process / "comm").write_text("opencode\n", encoding="utf-8")
                (process / "cmdline").write_bytes(
                    f"opencode\0--session\0{session_id}\0".encode()
                )
                (process / "stat").write_text(
                    f"{pid} (opencode) S " + " ".join(["0"] * 18 + [str(pid)]),
                    encoding="ascii",
                )
                (process / "fd").mkdir()
                os.symlink("/dev/pts/3", process / "fd" / "0")
            result = mock.Mock(returncode=0)
            result.stdout = (
                b"%3\t/dev/pts/3\toc-one\t0\t0\t1\t1\t1\t0\n"
                b"%4\t/dev/pts/3\toc-one\t0\t1\t1\t1\t0\t1\n"
                b"not-a-pane\t/dev/pts/3\toc-one\t0\t2\t1\t1\t0\t0\n"
            )
            with mock.patch("ocdeck.source.subprocess.run", return_value=result):
                panes = read_live_opencode_panes(proc_root)

        self.assertEqual(panes, ())

    def test_markdown_project_table_resolves_paths_from_vault_root(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            vault = Path(base)
            (vault / ".obsidian").mkdir()
            projects_file = (
                vault
                / "Projects/agents-start/docs/project_notes/Projects/agents-start/docs/projects.md"
            )
            projects_file.parent.mkdir(parents=True)
            source = """\
# Projects

| Project | Host | Code | Vault note |
| --- | --- | --- | --- |
| Project Alpha | test | `project_alpha` | Projects/project-alpha/main.md |
| Project Beta | test | /work/project-beta | Projects/project-beta/main.md |
"""

            projects = parse_markdown_projects(source, projects_file)

            self.assertEqual(
                projects,
                (
                    ("Project Alpha", str(vault / "project_alpha")),
                    ("Project Beta", "/work/project-beta"),
                ),
            )

    def test_markdown_catalog_merges_exact_discovered_paths(self) -> None:
        projects, names = merge_project_catalog(
            {"open-code-id": "/work/agents_start"},
            (
                ("Agents Start", "/work/agents_start"),
                ("Project Gamma", "/work/project-gamma"),
            ),
        )

        self.assertEqual(len(projects), 2)
        self.assertEqual(names["open-code-id"], "Agents Start")
        gamma_id = next(
            project_id
            for project_id, directory in projects.items()
            if directory == "/work/project-gamma"
        )
        self.assertEqual(names[gamma_id], "Project Gamma")

    def test_registry_retains_project_when_markdown_catalog_is_rewritten(self) -> None:
        registry = {
            "version": 1,
            "projects": [
                {
                    "id": "durable-id",
                    "name": "Durable Project",
                    "path": "/work/durable",
                    "host": "test",
                    "mainNote": "Projects/durable/main.md",
                }
            ],
        }
        with mock.patch.object(Path, "read_text", return_value=json.dumps(registry)):
            registry_catalog = read_project_registry(Path("/registry.json"))
        projects, names = merge_project_catalog({}, (), registry_catalog)
        project_id = next(iter(projects))
        self.assertEqual(projects[project_id], "/work/durable")
        self.assertEqual(names[project_id], "Durable Project")

    async def test_database_metadata_avoids_project_and_session_cli_calls(self) -> None:
        class DatabaseSource(DashboardSource):
            async def _api_status(self):
                return "offline", "test", {}, {}, (False, False)

            async def _command_json(self, *arguments, cwd=None, timeout=15):
                raise AssertionError(f"database metadata should avoid CLI: {arguments}")

            async def _collect_sessions(self, known_projects):
                raise AssertionError("database metadata should avoid the CLI sweep")

            async def _service_states(self):
                return ()

        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            project_path = root / "alpha"
            project_path.mkdir()
            db_file = root / "opencode.db"
            connection = sqlite3.connect(db_file)
            try:
                connection.execute(
                    "CREATE TABLE project ("
                    "id TEXT PRIMARY KEY, worktree TEXT, sandboxes TEXT)"
                )
                connection.execute(
                    "CREATE TABLE session ("
                    "id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT, "
                    "directory TEXT, title TEXT, metadata TEXT, agent TEXT, "
                    "time_created INTEGER, time_updated INTEGER, "
                    "time_archived INTEGER)"
                )
                connection.executemany(
                    "INSERT INTO project VALUES (?, ?, ?)",
                    [
                        ("global", "/", "[]"),
                        ("project-1", str(project_path), '["/unused"]'),
                    ],
                )
                connection.executemany(
                    "INSERT INTO session VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            "session-live",
                            "project-1",
                            "session-parent",
                            str(project_path),
                            "Database work",
                            '{"homeAgent":{"agent":"jarvis"}}',
                            "jarvis",
                            10,
                            20,
                            None,
                        ),
                        (
                            "session-archived",
                            "project-1",
                            None,
                            str(project_path),
                            "Archived work",
                            None,
                            None,
                            5,
                            15,
                            30,
                        ),
                    ],
                )
                connection.commit()
            finally:
                connection.close()

            self.assertEqual(
                read_projects_from_database(db_file),
                [
                    {
                        "id": "project-1",
                        "worktree": str(project_path),
                        "sandboxes": ["/unused"],
                    }
                ],
            )
            self.assertEqual(
                read_sessions_from_database(db_file),
                [
                    {
                        "id": "session-live",
                        "projectId": "project-1",
                        "directory": str(project_path),
                        "title": "Database work",
                        "created": 10,
                        "updated": 20,
                        "parentID": "session-parent",
                        "metadata": {"homeAgent": {"agent": "jarvis"}},
                        "agent": "jarvis",
                    }
                ],
            )

            source = DatabaseSource(
                backend="v1",
                opencode_bin="/bin/false",
                projects_file=root / "missing.md",
                project_registry_file=root / "missing-registry.json",
                session_routes_file=root / "missing-routes.json",
                briefings_file=root / "missing-briefings.json",
                session_db_file=db_file,
                permission_state_dir=root / "permissions",
            )
            snapshot = await source.collect()

        self.assertEqual([session.id for session in snapshot.sessions], ["session-live"])
        self.assertEqual(snapshot.sessions[0].parent_id, "session-parent")
        self.assertEqual(snapshot.sessions[0].home_agent, "maverik")
        self.assertEqual(snapshot.projects[0].directory, str(project_path))

    def test_database_metadata_falls_back_for_missing_or_incompatible_schema(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            missing = root / "missing.db"
            self.assertIsNone(read_projects_from_database(missing))
            self.assertIsNone(read_sessions_from_database(missing))

            incompatible = root / "incompatible.db"
            with sqlite3.connect(incompatible) as connection:
                connection.execute("CREATE TABLE session (id TEXT PRIMARY KEY)")
                connection.execute("CREATE TABLE project (id TEXT PRIMARY KEY)")
            self.assertIsNone(read_projects_from_database(incompatible))
            self.assertIsNone(read_sessions_from_database(incompatible))

    def test_agent_parent_ids_only_group_agent_spawned_subagents(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            db_file = Path(base) / "opencode.db"
            connection = sqlite3.connect(db_file)
            try:
                connection.execute(
                    "CREATE TABLE session ("
                    "id TEXT PRIMARY KEY, parent_id TEXT, metadata TEXT, "
                    "time_updated INTEGER, time_archived INTEGER)"
                )
                connection.executemany(
                    "INSERT INTO session VALUES (?, ?, ?, ?, ?)",
                    [
                        ("native", "parent", None, 50, None),
                        (
                            "self-parent",
                            "self-parent",
                            None,
                            60,
                            None,
                        ),
                        (
                            "controller-old",
                            None,
                            '{"homeAgent":{"kind":"orchestrator"}}',
                            100,
                            None,
                        ),
                        (
                            "controller",
                            None,
                            '{"role":"home_agent_monitor",'
                            '"managedBy":"home_agent.py"}',
                            200,
                            None,
                        ),
                        (
                            "worker",
                            None,
                            '{"homeAgent":{"project":"OC Deck",'
                            '"workerAgent":"build"}}',
                            300,
                            None,
                        ),
                        (
                            "archived-worker",
                            "parent",
                            None,
                            400,
                            500,
                        ),
                    ],
                )
                connection.commit()
            finally:
                connection.close()

            self.assertEqual(
                read_agent_parent_ids(db_file),
                {"native": "parent"},
            )

    def test_home_agent_evidence_uses_metadata_then_latest_message_then_recorded_agent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as base:
            db_file = Path(base) / "opencode.db"
            connection = sqlite3.connect(db_file)
            try:
                connection.execute(
                    "CREATE TABLE session ("
                    "id TEXT PRIMARY KEY, metadata TEXT, agent TEXT, "
                    "time_archived INTEGER)"
                )
                connection.execute(
                    "CREATE TABLE message ("
                    "id TEXT PRIMARY KEY, session_id TEXT, "
                    "time_created INTEGER, data TEXT)"
                )
                connection.executemany(
                    "INSERT INTO session VALUES (?, ?, ?, ?)",
                    [
                        (
                            "metadata-wins",
                            '{"homeAgent":{"agent":"jarvis"}}',
                            "jasmine",
                            None,
                        ),
                        ("message-wins", None, "jasmine", None),
                        ("recorded", None, "jasmine", None),
                        (
                            "explicit-empty",
                            '{"homeAgent":{"agent":""}}',
                            "jarvis",
                            None,
                        ),
                        ("archived", None, "home_agent", 100),
                    ],
                )
                connection.executemany(
                    "INSERT INTO message VALUES (?, ?, ?, ?)",
                    [
                        ("m1", "metadata-wins", 300, '{"agent":"home_agent"}'),
                        ("m2", "message-wins", 100, '{"agent":"jarvis"}'),
                        ("m3", "message-wins", 200, '{"agent":"home_agent"}'),
                        ("m4", "explicit-empty", 300, '{"agent":"jasmine"}'),
                    ],
                )
                connection.commit()
            finally:
                connection.close()

            evidence = read_home_agent_session_evidence(db_file)

            self.assertEqual(evidence["metadata-wins"], "jarvis")
            self.assertEqual(evidence["message-wins"], "home_agent")
            self.assertEqual(evidence["recorded"], "jasmine")
            self.assertIn("explicit-empty", evidence)
            self.assertEqual(evidence["explicit-empty"], "")
            self.assertNotIn("archived", evidence)
            with sqlite3.connect(db_file) as verification:
                self.assertEqual(
                    verification.execute("SELECT COUNT(*) FROM session").fetchone()[0],
                    5,
                )

    def test_archived_session_ids_read_only_and_null_aware(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            db_file = root / "opencode.db"
            self.assertEqual(read_archived_session_ids(root / "missing.db"), set())

            write_session_db(
                db_file,
                {"ses_live": None, "ses_dead": 1766588160000},
            )
            self.assertEqual(
                read_archived_session_ids(db_file), {"ses_dead"}
            )

            corrupt = root / "corrupt.db"
            corrupt.write_bytes(b"this is not a sqlite database")
            self.assertEqual(read_archived_session_ids(corrupt), set())

    def test_last_user_interactions_reads_only_user_messages(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            db_file = Path(base) / "opencode.db"
            connection = sqlite3.connect(db_file)
            try:
                connection.execute(
                    "CREATE TABLE message (session_id TEXT, time_created INTEGER, data TEXT)"
                )
                connection.executemany(
                    "INSERT INTO message VALUES (?, ?, ?)",
                    [
                        ("s1", 100, '{"role":"user"}'),
                        ("s1", 300, '{"role":"assistant"}'),
                        ("s1", 200, '{"role":"user"}'),
                        ("s2", 400, '{"role":"assistant"}'),
                        ("s3", 500, "invalid json"),
                    ],
                )
                connection.commit()
            finally:
                connection.close()

            self.assertEqual(read_last_user_interactions(db_file), {"s1": 200})

    async def test_v2_prompt_metadata_condenses_worker_launch_boilerplate(self) -> None:
        raw = (
            "WORKDIR=/home/user/ocdeck\n"
            "Agent: jarvis\n"
            "Session mode: home-agent worker\n"
            "User-approved task: fix the thing"
        )

        source = FakeV2PromptSource(raw)
        with mock.patch(
            "ocdeck.recent_open.load_recent_open_sessions", return_value=[]
        ):
            prompts, interactions = await source._v2_prompt_metadata(
                [{"id": "ses_live", "updated": 10}], {"ses_live"}
            )

        self.assertEqual(prompts, {"ses_live": "fix the thing"})
        self.assertEqual(interactions, {"ses_live": 42})
        self.assertEqual(source.operations, ["v2.session.message.list"])

    async def test_v2_prompt_metadata_keeps_plain_prompt_text_unchanged(self) -> None:
        text = "refactor the prompt reader and rerun the agents board tests"

        with mock.patch(
            "ocdeck.recent_open.load_recent_open_sessions", return_value=[]
        ):
            prompts, interactions = await FakeV2PromptSource(text)._v2_prompt_metadata(
                [{"id": "ses_live", "updated": 10}], {"ses_live"}
            )

        self.assertEqual(prompts, {"ses_live": text})
        self.assertEqual(interactions, {"ses_live": 42})

    async def test_v2_prompt_metadata_clips_long_prompts_at_a_word_boundary(self) -> None:
        text = (
            "please review the dashboard detail column rendering path "
            + "again " * 26
            + "and report back once the agents board looks right again"
        )
        self.assertGreater(len(text), MAX_LAST_PROMPT_LENGTH)

        with mock.patch(
            "ocdeck.recent_open.load_recent_open_sessions", return_value=[]
        ):
            prompts, _ = await FakeV2PromptSource(text)._v2_prompt_metadata(
                [{"id": "ses_live", "updated": 10}], {"ses_live"}
            )

        clipped = prompts["ses_live"]
        self.assertLessEqual(len(clipped), MAX_LAST_PROMPT_LENGTH)
        self.assertTrue(clipped.endswith("…"))
        self.assertTrue(text.startswith(clipped[:-1]))
        self.assertFalse(clipped[:-1].endswith(" "))
        self.assertTrue(text[len(clipped) - 1].isspace())

    def test_latest_user_prompts_condense_clip_and_keep_short_prompts(self) -> None:
        short = "rerun the failing test and report the diff"
        long_text = (
            "please review the dashboard detail column rendering path "
            + "again " * 26
            + "and report back once the agents board looks right again"
        )
        rows = [
            ("m1", "s_worker", (
                "WORKDIR=/home/user/ocdeck\n"
                "Agent: jarvis\n"
                "User-approved task: fix the thing"
            )),
            ("m2", "s_plain", short),
            ("m3", "s_long", long_text),
        ]

        with tempfile.TemporaryDirectory() as base:
            db_file = Path(base) / "opencode.db"
            connection = sqlite3.connect(db_file)
            try:
                connection.execute(
                    "CREATE TABLE message ("
                    "id TEXT PRIMARY KEY, session_id TEXT, "
                    "time_created INTEGER, data TEXT)"
                )
                connection.execute(
                    "CREATE TABLE part ("
                    "message_id TEXT, time_created INTEGER, data TEXT)"
                )
                connection.executemany(
                    "INSERT INTO message VALUES (?, ?, ?, ?)",
                    [
                        (message_id, session_id, 100, '{"role":"user"}')
                        for message_id, session_id, _ in rows
                    ],
                )
                connection.executemany(
                    "INSERT INTO part VALUES (?, ?, ?)",
                    [
                        (
                            message_id,
                            100,
                            json.dumps({"type": "text", "text": text}),
                        )
                        for message_id, _, text in rows
                    ],
                )
                connection.commit()
            finally:
                connection.close()

            prompts = read_latest_user_prompts(
                db_file, ["s_worker", "s_plain", "s_long"]
            )

        self.assertEqual(prompts["s_worker"], "fix the thing")
        self.assertEqual(prompts["s_plain"], short)
        clipped = prompts["s_long"]
        self.assertLessEqual(len(clipped), MAX_LAST_PROMPT_LENGTH)
        self.assertTrue(clipped.endswith("…"))
        self.assertTrue(long_text.startswith(clipped[:-1]))
        self.assertTrue(long_text[len(clipped) - 1].isspace())

    def test_clip_at_word_boundary_leaves_text_within_the_limit_untouched(self) -> None:
        self.assertEqual(
            clip_at_word_boundary("refactor the prompt reader", 26),
            "refactor the prompt reader",
        )
        self.assertEqual(
            clip_at_word_boundary("refactor the prompt reader", 40),
            "refactor the prompt reader",
        )
        self.assertEqual(
            clip_at_word_boundary("exactly ten!", 12), "exactly ten!"
        )
        self.assertEqual(clip_at_word_boundary("", 10), "")

    def test_clip_at_word_boundary_backs_up_to_the_last_space(self) -> None:
        clipped = clip_at_word_boundary(
            "the quick brown fox jumps over the lazy dog", 30
        )
        self.assertEqual(clipped, "the quick brown fox jumps…")
        self.assertTrue(clipped.endswith("…"))
        self.assertFalse(clipped[:-1].endswith(" "))

        self.assertEqual(
            clip_at_word_boundary("trailing space kept", 15), "trailing space…"
        )
        self.assertEqual(
            clip_at_word_boundary("alpha  bravo  charlie delta", 14), "alpha  bravo…"
        )

    def test_clip_at_word_boundary_hard_cuts_a_single_oversized_word(self) -> None:
        blob = "x" * 50

        self.assertEqual(clip_at_word_boundary(blob, 10), blob[:10])
        # A space further back than the lookback window must not shorten the cut.
        self.assertEqual(clip_at_word_boundary("abc " + "y" * 20, 10), "abc yyyyyy")
        for limit in (1, 2, 5):
            with self.subTest(limit=limit):
                clipped = clip_at_word_boundary(blob, limit)
                self.assertEqual(clipped, blob[:limit])
                self.assertTrue(clipped)
                self.assertLessEqual(len(clipped), limit)
        self.assertEqual(clip_at_word_boundary(blob, 0), "")

    def test_clip_at_word_boundary_handles_unicode_text(self) -> None:
        clipped = clip_at_word_boundary("これは とても 長い です テスト です", 8)
        self.assertEqual(clipped, "これは とても…")
        self.assertLessEqual(len(clipped), 8)

        unbroken = "これは非常に長いテキストです"
        hard = clip_at_word_boundary(unbroken, 10)
        self.assertEqual(hard, unbroken[:10])
        self.assertEqual(len(hard), 10)

        emoji_text = "ship it " + "🎉" * 8 + " 🚀 done"
        emoji_clipped = clip_at_word_boundary(emoji_text, 20)
        self.assertTrue(emoji_clipped.endswith("…"))
        self.assertTrue(emoji_text.startswith(emoji_clipped[:-1]))
        self.assertTrue(emoji_text[len(emoji_clipped) - 1].isspace())
        self.assertLessEqual(len(emoji_clipped), 20)

        self.assertEqual(clip_at_word_boundary("短い", 5), "短い")

    def test_session_turn_activity_reads_latest_assistant_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            db_file = Path(base) / "opencode.db"
            self.assertEqual(
                read_session_turn_activity(Path(base) / "missing.db"), {}
            )

            connection = sqlite3.connect(db_file)
            try:
                connection.execute(
                    "CREATE TABLE message ("
                    "id TEXT PRIMARY KEY, session_id TEXT, "
                    "time_created INTEGER, data TEXT)"
                )
                connection.execute(
                    "CREATE TABLE part (message_id TEXT, time_updated INTEGER)"
                )
                connection.executemany(
                    "INSERT INTO message VALUES (?, ?, ?, ?)",
                    [
                        ("m0", "s_active", 100, '{"role":"user"}'),
                        (
                            "m1",
                            "s_active",
                            200,
                            '{"role":"assistant","time":{"created":200}}',
                        ),
                        (
                            "m2",
                            "s_done",
                            300,
                            '{"role":"assistant",'
                            '"time":{"created":300,"completed":350},'
                            '"finish":"stop"}',
                        ),
                        (
                            "m3",
                            "s_tools",
                            400,
                            '{"role":"assistant",'
                            '"time":{"created":400,"completed":450},'
                            '"finish":"tool-calls"}',
                        ),
                        (
                            "m4",
                            "s_abort",
                            500,
                            '{"role":"assistant",'
                            '"time":{"created":500,"completed":550}}',
                        ),
                        ("m5", "s_junk", 600, "not json"),
                        ("m6", "", 700, '{"role":"assistant","finish":"stop"}'),
                        (
                            "z-old",
                            "s_order",
                            800,
                            '{"role":"assistant",'
                            '"time":{"created":800,"completed":850},'
                            '"finish":"stop"}',
                        ),
                        (
                            "a-new",
                            "s_order",
                            900,
                            '{"role":"assistant","time":{"created":900}}',
                        ),
                        (
                            "stale",
                            "s_stale",
                            1,
                            '{"role":"assistant","time":{"created":1}}',
                        ),
                        (
                            "slow",
                            "s_slow",
                            1400000,
                            '{"role":"assistant","time":{"created":1400000}}',
                        ),
                    ],
                )
                connection.executemany(
                    "INSERT INTO part VALUES (?, ?)",
                    [
                        ("m1", 1999900),
                        ("m3", 1999900),
                        ("a-new", 1999950),
                        ("stale", 1),
                        ("slow", 1400000),
                    ],
                )
                connection.commit()
            finally:
                connection.close()

            self.assertEqual(
                read_session_turn_activity(db_file, now_ms=2000000),
                {
                    "s_active": (True, 0, 1999900),
                    "s_done": (False, 350, 300),
                    "s_tools": (True, 450, 1999900),
                    "s_abort": (False, 550, 500),
                    "s_order": (True, 0, 1999950),
                    "s_stale": (False, 0, 1),
                    "s_slow": (True, 0, 1400000),
                },
            )
            self.assertEqual(
                read_session_turn_activity(
                    db_file,
                    now_ms=2000000,
                    allow_stale=True,
                )["s_stale"],
                (True, 0, 1),
            )

            corrupt = Path(base) / "corrupt.db"
            corrupt.write_bytes(b"this is not a sqlite database")
            self.assertEqual(read_session_turn_activity(corrupt), {})

    async def test_collect_activity_refreshes_without_cli_sweep(self) -> None:
        class PulsedSource(DashboardSource):
            def __init__(self, project_path: Path, db_file: Path) -> None:
                super().__init__(
                    backend="v1",
                    opencode_bin="/bin/false",
                    api_url="http://127.0.0.1:99999",
                    projects_file=project_path.parent / "missing.md",
                    session_routes_file=project_path.parent / "missing-routes.json",
                    briefings_file=project_path.parent / "missing.json",
                    session_db_file=db_file,
                    permission_state_dir=project_path.parent / "permissions",
                )
                self.project_path = project_path
                self.cli_calls = 0
                self.api_busy = False

            async def _api_status(self):
                status = {"ses_live": {"type": "busy"}} if self.api_busy else {}
                return "live", "test", status, {}, (True, True)

            async def _command_json(self, *arguments, cwd=None, timeout=15):
                self.cli_calls += 1
                if arguments[:2] == ("debug", "scrap"):
                    return [
                        {"id": "runtime-id", "worktree": str(self.project_path)}
                    ]
                return [
                    {
                        "id": "ses_live",
                        "title": "Live work",
                        "directory": str(self.project_path),
                        "projectId": "runtime-id",
                        "created": 1,
                        "updated": 2,
                    }
                ]

            async def _service_states(self):
                return ()

        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            project_path = root / "alpha"
            project_path.mkdir()
            db_file = root / "opencode.db"
            now_ms = int(time.time() * 1000)
            connection = sqlite3.connect(db_file)
            try:
                connection.execute(
                    "CREATE TABLE message ("
                    "id TEXT PRIMARY KEY, session_id TEXT, "
                    "time_created INTEGER, data TEXT)"
                )
                connection.execute(
                    "INSERT INTO message VALUES (?, ?, ?, ?)",
                    (
                        "m1",
                        "ses_live",
                        now_ms,
                        json.dumps(
                            {
                                "role": "assistant",
                                "time": {"created": now_ms},
                            }
                        ),
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            source = PulsedSource(project_path, db_file)
            self.assertIsNone(await source.collect_activity())

            first = await source.collect()
            self.assertTrue(first.sessions[0].assistant_active)
            cli_after_full_collect = source.cli_calls
            self.assertGreater(cli_after_full_collect, 0)

            with mock.patch(
                "ocdeck.source.read_opencode_instances",
                return_value=({"ses_live": 1}, 0, {}),
            ):
                running = await source.collect_activity()
                self.assertIsNotNone(running)
                self.assertEqual(source.cli_calls, cli_after_full_collect)
                self.assertEqual(agent_state(running.sessions[0]), "busy")

                connection = sqlite3.connect(db_file)
                try:
                    connection.execute(
                        "UPDATE message SET data = ? WHERE id = ?",
                        (
                            json.dumps(
                                {
                                    "role": "assistant",
                                    "time": {
                                        "created": now_ms,
                                        "completed": now_ms + 1,
                                    },
                                    "finish": "stop",
                                }
                            ),
                            "m1",
                        ),
                    )
                    connection.commit()
                finally:
                    connection.close()

                settled = await source.collect_activity()
                self.assertFalse(settled.sessions[0].assistant_active)
                self.assertEqual(agent_state(settled.sessions[0]), "open")

                source.api_busy = True
                busy = await source.collect_activity()
                self.assertEqual(busy.sessions[0].status, "busy")

            self.assertEqual(source.cli_calls, cli_after_full_collect)

    async def test_archived_sessions_are_hidden_from_all_views(self) -> None:
        class ArchiveSource(DashboardSource):
            def __init__(self, project_path: Path, db_file: Path) -> None:
                super().__init__(
                    backend="v1",
                    opencode_bin="/bin/false",
                    api_url="http://127.0.0.1:99999",
                    projects_file=project_path.parent / "missing.md",
                    project_registry_file=project_path.parent / "missing-registry.json",
                    session_routes_file=project_path.parent / "missing-routes.json",
                    briefings_file=project_path.parent / "missing.json",
                    session_db_file=db_file,
                    permission_state_dir=project_path.parent / "permissions",
                )
                self.project_path = project_path

            async def _api_status(self):
                return "offline", "test", {}, {}, (False, False)

            async def _command_json(self, *arguments, cwd=None, timeout=15):
                if arguments[:2] == ("debug", "scrap"):
                    return [
                        {"id": "runtime-id", "worktree": str(self.project_path)}
                    ]
                return [
                    {
                        "id": "ses_live",
                        "title": "Live work",
                        "directory": str(self.project_path),
                        "projectId": "runtime-id",
                        "created": 1,
                        "updated": 2,
                    },
                    {
                        "id": "ses_dead",
                        "title": "Archived work",
                        "directory": str(self.project_path),
                        "projectId": "runtime-id",
                        "created": 1,
                        "updated": 3,
                    },
                ]

            async def _service_states(self):
                return ()

        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            project_path = root / "alpha"
            project_path.mkdir()
            db_file = root / "opencode.db"
            write_session_db(db_file, {"ses_dead": 1766588160000})

            snapshot = await ArchiveSource(project_path, db_file).collect()

        self.assertEqual(
            [session.id for session in snapshot.sessions], ["ses_live"]
        )
        self.assertEqual(len(snapshot.projects), 1)
        self.assertEqual(snapshot.projects[0].session_count, 1)
        self.assertEqual(snapshot.warning, "")

    async def test_database_failure_shows_every_session_unfiltered(self) -> None:
        class FailingDbSource(DashboardSource):
            async def _api_status(self):
                return "offline", "test", {}, {}, (False, False)

            async def _command_json(self, *arguments, cwd=None, timeout=15):
                return []

            async def _collect_sessions(self, known_projects):
                return [
                    {
                        "id": "session-1",
                        "title": "Kept",
                        "directory": "/work/kept",
                        "created": 1,
                        "updated": 2,
                    }
                ]

            async def _service_states(self):
                return ()

        source = FailingDbSource(
            backend="v1",
            opencode_bin="/bin/false",
            session_db_file="/nonexistent-parent/opencode.db",
        )
        snapshot = await source.collect()

        self.assertEqual(
            [session.id for session in snapshot.sessions], ["session-1"]
        )
        self.assertEqual(snapshot.warning, "")

    def test_session_routes_reject_invalid_files(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            routes_file = Path(base) / "routes.json"
            routes_file.write_text(
                '{"sessions":{"session-1":"OC Deck","":"Ignored","bad":7}}'
            )

            self.assertEqual(
                read_session_routes(routes_file),
                {"session-1": "OC Deck"},
            )
            routes_file.write_text("not-json")
            self.assertEqual(read_session_routes(routes_file), {})


if __name__ == "__main__":
    unittest.main()
