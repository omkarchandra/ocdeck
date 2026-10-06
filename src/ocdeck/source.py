from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import ipaddress
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .browser_access import BrowserAccessResult, browser_access
from .backend import desktop_config_home
from .v2_read_api import ReadAPIError, read_api, use_read_api
from .runtime_state import (
    LocalRuntimeState,
    RuntimeProducer,
    local_status,
    merge_permissions,
    reconcile_runtime_producers,
)
from .models import (
    ACTIVE_TURN_WINDOW_MS,
    BriefingReportRecord,
    DashboardSnapshot,
    MAX_LAST_PROMPT_LENGTH,
    NamedAgentStatus,
    NextStepRecord,
    ProjectBriefingRecord,
    ProjectRecord,
    ServiceRecord,
    SystemMetrics,
    apply_session_routes,
    assign_project_roots,
    build_projects,
    clean_int,
    clean_string,
    expected_named_agents,
    normalize_status,
    parse_named_agent_registry,
    parse_known_projects,
    parse_sessions,
    sanitize_terminal_text,
    summarize_named_agents,
)


# systemd user units shown in the services panel. Add your own here, or install
# a Home Agent integration (see home-agent/README.md) that provides its monitor.
SERVICE_ALLOWLIST = (
    ("opencode-web.service", "OpenCode Web", "local API and web client"),
    ("home-agent-monitor.timer", "Home Agent", "project task monitor (optional)"),
)

MAX_API_RESPONSE_BYTES = 1024 * 1024
MAX_V2_SESSION_PAGES = 100
MAX_SESSION_CACHE_BYTES = 4 * 1024 * 1024
MAX_DATABASE_SESSIONS = 5000
SESSION_CACHE_TTL_MS = 10 * 1000
SESSION_CACHE_LOCK_STALE_SECONDS = 60
MAX_BRIEFINGS_FILE_BYTES = 1024 * 1024
MAX_BRIEFING_PROJECTS = 256
MAX_BRIEFING_ARRAY_ITEMS = 64
MAX_BRIEFING_EVIDENCE_ITEMS = 128
MAX_BRIEFING_IDENTIFIER_LENGTH = 256
MAX_BRIEFING_TEXT_LENGTH = 4096
MAX_BRIEFING_PATH_LENGTH = 4096
DEFAULT_BRIEFINGS_FILE = (
    Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    / "home-agent/reports/latest.json"
)
DEFAULT_SESSION_DB_FILE = (
    Path.home() / ".local/share/opencode/opencode.db"
)
RFC3339_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})$"
)
DEFAULT_PROJECTS_FILE = (
    desktop_config_home()
    / "home-agent/projects.md"
)
DEFAULT_PROJECT_REGISTRY_FILE = (
    desktop_config_home()
    / "home-agent/registry.json"
)
EXPECTED_V2_VERSION = "2.0.14"
DEFAULT_SERVER_ENV_FILE = (
    desktop_config_home()
    / "opencode/server.env"
)
# Named orchestrator agents OC Deck recognises: {name: path of its definition}.
# Empty by default; a Home Agent integration provides them (home-agent/README.md).
DEFAULT_NAMED_AGENT_FILES: dict[str, Path] = {}
NON_TUI_SUBCOMMANDS = {
    "acp",
    "agent",
    "api",
    "auth",
    "completion",
    "console",
    "db",
    "debug",
    "export",
    "github",
    "import",
    "mcp",
    "models",
    "plugin",
    "plug",
    "pr",
    "pair",
    "providers",
    "run",
    "serve",
    "service",
    "session",
    "stats",
    "uninstall",
    "upgrade",
    "web",
}
CLI_OPTIONS_WITH_VALUES = {
    "--completions",
    "--log-level",
    "--prompt",
    "--server",
    "--session",
    "-s",
}
PANE_ID_PATTERN = re.compile(r"^%[0-9]+$")
BACKGROUND_JOB_DIRECTORY_LIMIT = 8
BACKGROUND_JOB_RECENT_MS = 24 * 60 * 60 * 1000
V2_SESSION_ID_PATTERN = re.compile(r"ses_[A-Za-z0-9]{1,125}")
V2_LOCATION_OPERATIONS = frozenset(
    {
        "v2.agent.list",
        "v2.form.request.list",
        "v2.mcp.list",
        "v2.permission.request.list",
        "v2.shell.list",
    }
)


@dataclass(frozen=True, slots=True)
class OpenCodeProcess:
    pid: int
    session_id: str
    tty: str
    start_time: int
    backend: str


@dataclass(frozen=True, slots=True)
class LiveOpenCodePane:
    destination_id: str
    pane_id: str
    session_id: str
    session_name: str
    window_index: str
    pane_index: str
    terminal_state: str
    backend: str = "v1"


class V2ApiError(RuntimeError):
    pass


def v2_api_operation(operation: str) -> str:
    """Resolve Deck's internal operation labels to the stable OpenCode 2 API."""
    return {
        "v2.health.get": "server.info",
        "v2.session.rename": "session.update",
        "v2.form.request.list": "form.list",
        "v2.session.wait": "experimental.session.wait",
        "v2.session.import": "experimental.session.import",
        "v2.session.export": "experimental.session.export",
    }.get(operation, operation.removeprefix("v2."))


@dataclass(frozen=True, slots=True)
class V2Location:
    directory: str
    workspace_id: str = ""

    def parameters(self) -> dict[str, str]:
        parameters = {"location[directory]": self.directory}
        if self.workspace_id:
            parameters["location[workspace]"] = self.workspace_id
        return parameters

    def reference(self) -> dict[str, str]:
        reference = {"directory": self.directory}
        if self.workspace_id:
            reference["workspaceID"] = self.workspace_id
        return reference


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


class DashboardSource:
    def __init__(
        self,
        *,
        opencode_bin: str | None = None,
        api_url: str | None = None,
        limit: int = 100,
        projects_file: str | Path | None = None,
        project_registry_file: str | Path | None = None,
        session_routes_file: str | Path | None = None,
        briefings_file: str | Path | None = None,
        session_db_file: str | Path | None = None,
        permission_state_dir: str | Path | None = None,
        server_env_file: str | Path | None = None,
        named_agent_files: dict[str, str | Path] | None = None,
        backend: str = "v2",
    ) -> None:
        if backend not in {"v1", "v2"}:
            raise ValueError("OpenCode backend must be 'v1' or 'v2'")
        self.backend = backend
        self.opencode_bin = opencode_bin or (
            self._find_opencode2() if backend == "v2" else self._find_opencode()
        )
        configured_api_url = api_url or os.environ.get("OPENCODE_URL")
        self.api_url = (
            configured_api_url
            or ("" if backend == "v2" else "http://127.0.0.1:4096")
        ).rstrip("/")
        self.api_url_error = validate_api_url(self.api_url) if self.api_url else ""
        self.limit = max(1, min(limit, 500))
        configured_projects_file = (
            projects_file
            or os.environ.get("OCDECK_PROJECTS_FILE")
            or DEFAULT_PROJECTS_FILE
        )
        self.projects_file = Path(configured_projects_file).expanduser()
        self.project_registry_file = Path(
            project_registry_file
            or os.environ.get("OCDECK_PROJECT_REGISTRY_FILE")
            or DEFAULT_PROJECT_REGISTRY_FILE
        ).expanduser()
        self.session_routes_file = Path(
            session_routes_file
            or os.environ.get("OCDECK_SESSION_ROUTES_FILE")
            or find_vault_root(self.projects_file) / "Projects/_session-routes.json"
        ).expanduser()
        self.briefings_file = Path(
            briefings_file
            or os.environ.get("OCDECK_BRIEFINGS_FILE")
            or DEFAULT_BRIEFINGS_FILE
        ).expanduser()
        self.session_db_file = Path(
            session_db_file
            or os.environ.get("OCDECK_SESSION_DB_FILE")
            or DEFAULT_SESSION_DB_FILE
        ).expanduser()
        self.permission_state_dir = Path(
            permission_state_dir or default_permission_state_dir()
        ).expanduser()
        self.server_env_file = Path(
            server_env_file or DEFAULT_SERVER_ENV_FILE
        ).expanduser()
        if backend == "v1":
            self.server_username, self.server_password = read_server_credentials(
                self.server_env_file
            )
        else:
            self.server_username, self.server_password = "", ""
        configured_agents = (
            named_agent_files if named_agent_files is not None else DEFAULT_NAMED_AGENT_FILES
        )
        self.named_agent_files = {
            clean_string(name): Path(path).expanduser()
            for name, path in configured_agents.items()
            if clean_string(name)
        }
        configured = {
            name: path.is_file() for name, path in self.named_agent_files.items()
        }
        self._named_agent_registry = expected_named_agents(configured)
        self._named_agent_registry_valid = False
        self._named_agents_stale = True
        self._named_agents_error = "Agent registry has not been loaded"
        self._signed_in_tabs_status = "unknown"
        self._signed_in_tabs_stale = True
        self._signed_in_tabs_error = "Browser MCP status has not been loaded"
        self._activity_cache: dict[str, Any] | None = None
        self._v2_active_statuses: dict[str, str] = {}
        self._v2_pending_cache: dict[str, list[dict[str, Any]]] = {}
        self._v2_locations_cache: tuple[V2Location, ...] = ()
        self._v2_api_slots = asyncio.Semaphore(4)
        self._v2_prompt_cache: dict[str, tuple[int, str, int, float]] = {}
        self._unresolved_browser_creations: dict[str, BrowserAccessResult] = {}

    async def collect(self) -> DashboardSnapshot:
        if self.backend == "v2":
            return await self._collect_v2()
        return await self._collect_v1()

    async def _collect_v1(self) -> DashboardSnapshot:
        status_task = asyncio.create_task(self._api_status())
        named_registry_task = asyncio.create_task(self._refresh_named_agent_registry())
        projects_task = asyncio.create_task(self._collect_known_projects())
        catalog_task = asyncio.create_task(
            asyncio.to_thread(read_markdown_projects, self.projects_file)
        )
        registry_task = asyncio.create_task(
            asyncio.to_thread(read_project_registry, self.project_registry_file)
        )
        routes_task = asyncio.create_task(
            asyncio.to_thread(read_session_routes, self.session_routes_file)
        )
        briefings_task = asyncio.create_task(
            asyncio.to_thread(read_briefings_file, self.briefings_file)
        )
        services_task = asyncio.create_task(self._service_states())
        metrics_task = asyncio.create_task(asyncio.to_thread(read_system_metrics))
        instances_task = asyncio.create_task(
            asyncio.to_thread(read_opencode_instances, backend="v1")
        )
        local_state_task = asyncio.create_task(
            asyncio.to_thread(
                read_local_runtime_state,
                self.permission_state_dir,
                backend="v1",
            )
        )

        (
            status_result,
            projects_result,
            catalog,
            registry_catalog,
            routes,
            briefing_source,
            services,
            metrics,
            instance_result,
            local_state,
            named_registry_result,
        ) = await asyncio.gather(
            status_task,
            projects_task,
            catalog_task,
            registry_task,
            routes_task,
            briefings_task,
            services_task,
            metrics_task,
            instances_task,
            local_state_task,
            named_registry_task,
        )
        local_permissions = local_state.permissions
        local_statuses = local_state.statuses
        (
            connection,
            connection_detail,
            api_statuses,
            permissions,
            request_authority,
        ) = status_result
        api_session_ids = frozenset(api_statuses) | frozenset(permissions)
        statuses = reconcile_statuses(api_statuses, local_statuses)

        known_projects, project_names = merge_project_catalog(
            parse_known_projects(projects_result), catalog, registry_catalog
        )
        archived_task = asyncio.create_task(
            asyncio.to_thread(read_archived_session_ids, self.session_db_file)
        )
        interactions_task = asyncio.create_task(
            asyncio.to_thread(read_last_user_interactions, self.session_db_file)
        )
        turns_task = asyncio.create_task(
            asyncio.to_thread(
                read_session_turn_activity,
                self.session_db_file,
                allow_stale=True,
            )
        )
        agent_parents_task = asyncio.create_task(
            asyncio.to_thread(read_agent_parent_ids, self.session_db_file)
        )
        home_agent_evidence_task = asyncio.create_task(
            asyncio.to_thread(read_home_agent_session_evidence, self.session_db_file)
        )
        sessions_result = await self._collect_sessions_shared(known_projects)
        (
            archived_ids,
            last_interactions,
            turn_activity,
            agent_parent_ids,
            home_agent_evidence,
        ) = await asyncio.gather(
            archived_task,
            interactions_task,
            turns_task,
            agent_parents_task,
            home_agent_evidence_task,
        )
        turn_activity = dict(turn_activity)
        for session_id, status in statuses.items():
            if status == "idle":
                turn_activity[session_id] = (
                    False,
                    turn_activity.get(session_id, (False, 0))[1],
                    turn_activity.get(session_id, (False, 0, 0))[2],
                )
        if sessions_result and archived_ids:
            sessions_result = [
                item
                for item in sessions_result
                if isinstance(item, dict) and item.get("id") not in archived_ids
            ]

        permissions = reconcile_permissions(
            permissions, local_permissions, request_authority, api_session_ids
        )
        instance_counts, unmapped_instances, instance_ttys = instance_result
        prompts_task = asyncio.create_task(
            asyncio.to_thread(
                read_latest_user_prompts,
                self.session_db_file,
                live_session_ids(
                    statuses, instance_counts, permissions, sessions_result
                ),
            )
        )
        terminal_map, attached_map = await asyncio.to_thread(
            read_tmux_tty_state, instance_ttys
        )
        latest_prompts = await prompts_task
        sessions = parse_sessions(
            sessions_result,
            statuses,
            instance_counts,
            terminal_map,
            permissions,
            attached_map,
            last_interactions,
            turn_activity,
            latest_prompts,
            agent_parent_ids,
            home_agent_evidence,
        )
        sessions = assign_project_roots(sessions, known_projects)
        sessions = apply_session_routes(sessions, routes, project_names)
        projects = build_projects(sessions, known_projects, project_names)
        (
            registry,
            registry_stale,
            registry_error,
            browser_status,
            browser_stale,
            browser_error,
        ) = named_registry_result
        named_agents = summarize_named_agents(registry, sessions)
        briefing_report = parse_briefings(briefing_source)
        briefings = (
            match_project_briefings(briefing_report.projects, projects)
            if briefing_report is not None
            else ()
        )
        warning = "" if sessions_result is not None else "Session metadata unavailable"
        if sessions_result is not None:
            # Cache the heavy collection results so collect_activity() can
            # refresh live signals later without rerunning CLI sweeps.
            self._activity_cache = {
                "sessions": sessions_result,
                "known_projects": known_projects,
                "project_names": project_names,
                "routes": routes,
                "agent_parent_ids": agent_parent_ids,
                "services": services,
                "metrics": metrics,
                "briefings": briefings,
                "briefing_report_id": (
                    briefing_report.report_id if briefing_report is not None else ""
                ),
                "briefing_generated_at": (
                    briefing_report.generated_at
                    if briefing_report is not None
                    else None
                ),
                "briefing_status": (
                    briefing_report.status if briefing_report is not None else ""
                ),
            }
        return DashboardSnapshot(
            sessions=sessions,
            projects=projects,
            services=services,
            metrics=metrics,
            connection=connection,
            connection_detail=connection_detail,
            unmapped_instance_count=unmapped_instances,
            warning=warning,
            briefings=briefings,
            briefing_report_id=(
                briefing_report.report_id if briefing_report is not None else ""
            ),
            briefing_generated_at=(
                briefing_report.generated_at if briefing_report is not None else None
            ),
            briefing_status=(
                briefing_report.status if briefing_report is not None else ""
            ),
            named_agents=named_agents,
            named_agents_stale=registry_stale,
            named_agents_error=registry_error,
            signed_in_tabs_status=browser_status,
            signed_in_tabs_stale=browser_stale,
            signed_in_tabs_error=browser_error,
        )

    async def _collect_v2(self) -> DashboardSnapshot:
        sessions_task = asyncio.create_task(self._v2_collect_sessions())
        projects_task = asyncio.create_task(self._v2_collect_projects())
        status_task = asyncio.create_task(self._v2_health_status())
        catalog_task = asyncio.create_task(
            asyncio.to_thread(read_markdown_projects, self.projects_file)
        )
        registry_task = asyncio.create_task(
            asyncio.to_thread(read_project_registry, self.project_registry_file)
        )
        routes_task = asyncio.create_task(
            asyncio.to_thread(read_session_routes, self.session_routes_file)
        )
        briefings_task = asyncio.create_task(
            asyncio.to_thread(read_briefings_file, self.briefings_file)
        )
        services_task = asyncio.create_task(self._service_states())
        metrics_task = asyncio.create_task(asyncio.to_thread(read_system_metrics))
        instances_task = asyncio.create_task(
            asyncio.to_thread(read_opencode_instances, backend="v2")
        )
        named_registry_task = asyncio.create_task(
            self._refresh_named_agent_registry_v2()
        )

        (
            sessions_result,
            projects_result,
            status_result,
            catalog,
            registry_catalog,
            routes,
            briefing_source,
            services,
            metrics,
            instance_result,
            named_registry_result,
        ) = await asyncio.gather(
            sessions_task,
            projects_task,
            status_task,
            catalog_task,
            registry_task,
            routes_task,
            briefings_task,
            services_task,
            metrics_task,
            instances_task,
            named_registry_task,
        )

        connection, connection_detail, statuses = status_result
        known_projects, project_names = merge_project_catalog(
            parse_known_projects(projects_result), catalog, registry_catalog
        )
        instance_counts, unmapped_instances, instance_ttys = instance_result
        permissions, pending_ok = await self._v2_pending_requests()
        if not pending_ok:
            if connection == "live":
                connection = "degraded"
            connection_detail += "; pending requests stale"

        # After the permission/question check, never alongside it: both share
        # the serialized reader, and queued job lookups made that check time
        # out (the deck then showed "degraded").
        background_jobs = await self._v2_fetch_background_jobs(sessions_result)

        terminal_map, attached_map = await asyncio.to_thread(
            read_tmux_tty_state, instance_ttys
        )
        prompt_map, interactions = await self._v2_prompt_metadata(sessions_result, set(instance_counts) | set(statuses))
        sessions = parse_sessions(
            sessions_result,
            statuses,
            instance_counts,
            terminal_map,
            permissions,
            attached_map,
            last_interactions=interactions,
            latest_prompts=prompt_map,
            background_jobs=background_jobs,
        )
        sessions = assign_project_roots(sessions, known_projects)
        sessions = apply_session_routes(sessions, routes, project_names)
        projects = build_projects(sessions, known_projects, project_names)
        (
            registry,
            registry_stale,
            registry_error,
            browser_status,
            browser_stale,
            browser_error,
        ) = named_registry_result
        named_agents = summarize_named_agents(registry, sessions)
        briefing_report = parse_briefings(briefing_source)
        briefings = (
            match_project_briefings(briefing_report.projects, projects)
            if briefing_report is not None
            else ()
        )
        warning = "" if sessions_result is not None else "V2 session metadata unavailable"
        if sessions_result is not None:
            self._activity_cache = {
                "backend": "v2",
                "sessions": sessions_result,
                "known_projects": known_projects,
                "project_names": project_names,
                "routes": routes,
                "services": services,
                "metrics": metrics,
                "briefings": briefings,
                "briefing_report_id": (
                    briefing_report.report_id if briefing_report is not None else ""
                ),
                "briefing_generated_at": (
                    briefing_report.generated_at
                    if briefing_report is not None
                    else None
                ),
                "briefing_status": (
                    briefing_report.status if briefing_report is not None else ""
                ),
            }
        else:
            self._activity_cache = None
        return DashboardSnapshot(
            sessions=sessions,
            projects=projects,
            services=services,
            metrics=metrics,
            connection=connection,
            connection_detail=connection_detail,
            unmapped_instance_count=unmapped_instances,
            warning=warning,
            briefings=briefings,
            briefing_report_id=(
                briefing_report.report_id if briefing_report is not None else ""
            ),
            briefing_generated_at=(
                briefing_report.generated_at if briefing_report is not None else None
            ),
            briefing_status=(
                briefing_report.status if briefing_report is not None else ""
            ),
            named_agents=named_agents,
            named_agents_stale=registry_stale,
            named_agents_error=registry_error,
            signed_in_tabs_status=browser_status,
            signed_in_tabs_stale=browser_stale,
            signed_in_tabs_error=browser_error,
        )

    async def _collect_activity_v2(self) -> DashboardSnapshot | None:
        cache = self._activity_cache
        if cache is None or cache.get("backend") != "v2":
            return None
        status_task = asyncio.create_task(self._v2_health_status())
        sessions_task = asyncio.create_task(self._v2_collect_sessions())
        projects_task = asyncio.create_task(self._v2_collect_projects())
        catalog_task = asyncio.create_task(
            asyncio.to_thread(read_markdown_projects, self.projects_file)
        )
        registry_task = asyncio.create_task(
            asyncio.to_thread(read_project_registry, self.project_registry_file)
        )
        routes_task = asyncio.create_task(
            asyncio.to_thread(read_session_routes, self.session_routes_file)
        )
        named_registry_task = asyncio.create_task(
            self._refresh_named_agent_registry_v2()
        )
        instances_task = asyncio.create_task(
            asyncio.to_thread(read_opencode_instances, backend="v2")
        )
        (
            status_result,
            sessions_result,
            projects_result,
            catalog,
            registry_catalog,
            routes,
            named_registry_result,
        ) = await asyncio.gather(
            status_task,
            sessions_task,
            projects_task,
            catalog_task,
            registry_task,
            routes_task,
            named_registry_task,
        )
        connection, connection_detail, statuses = status_result
        instance_counts, unmapped_instances, instance_ttys = await instances_task
        permissions, pending_ok = await self._v2_pending_requests()
        if not pending_ok:
            if connection == "live":
                connection = "degraded"
            connection_detail += "; pending requests stale"
        terminal_map, attached_map = await asyncio.to_thread(
            read_tmux_tty_state, instance_ttys
        )
        warning = ""
        if sessions_result is None:
            sessions_result = cache["sessions"]
            warning = "V2 session metadata stale; showing the last complete session list"
            if connection == "live":
                connection = "degraded"
            connection_detail += "; session list stale"
        discovered = (
            parse_known_projects(projects_result)
            if projects_result is not None
            else cache["known_projects"]
        )
        if projects_result is None:
            if connection == "live":
                connection = "degraded"
            connection_detail += "; project list stale"
        known_projects, project_names = merge_project_catalog(
            discovered, catalog, registry_catalog
        )
        prompt_map, interactions = await self._v2_prompt_metadata(sessions_result, set(instance_counts) | set(statuses))
        background_jobs = await self._v2_fetch_background_jobs(sessions_result)
        sessions = parse_sessions(
            sessions_result,
            statuses,
            instance_counts,
            terminal_map,
            permissions,
            attached_map,
            last_interactions=interactions,
            latest_prompts=prompt_map,
            background_jobs=background_jobs,
        )
        sessions = assign_project_roots(sessions, known_projects)
        sessions = apply_session_routes(sessions, routes, project_names)
        projects = build_projects(sessions, known_projects, project_names)
        (
            registry,
            registry_stale,
            registry_error,
            browser_status,
            browser_stale,
            browser_error,
        ) = named_registry_result
        return DashboardSnapshot(
            sessions=sessions,
            projects=projects,
            services=cache["services"],
            metrics=cache["metrics"],
            connection=connection,
            connection_detail=connection_detail,
            unmapped_instance_count=unmapped_instances,
            warning=warning,
            briefings=cache["briefings"],
            briefing_report_id=cache["briefing_report_id"],
            briefing_generated_at=cache["briefing_generated_at"],
            briefing_status=cache["briefing_status"],
            named_agents=summarize_named_agents(registry, sessions),
            named_agents_stale=registry_stale,
            named_agents_error=registry_error,
            signed_in_tabs_status=browser_status,
            signed_in_tabs_stale=browser_stale,
            signed_in_tabs_error=browser_error,
        )

    async def _v2_api_json(
        self,
        operation: str,
        *,
        params: dict[str, str] | None = None,
        payload: Any = None,
        location: V2Location | None = None,
        timeout: float = 15,
    ) -> Any:
        if not self.opencode_bin:
            raise V2ApiError("OpenCode V2 executable not found")
        if self.api_url_error:
            raise V2ApiError(self.api_url_error)
        command = [self.opencode_bin, "api", v2_api_operation(operation)]
        if self.api_url:
            command.extend(("--server", self.api_url))
        request_params = dict(params or {})
        if operation in V2_LOCATION_OPERATIONS:
            if location is None:
                raise V2ApiError(f"{operation} requires an explicit location")
            if location.workspace_id:
                raise V2ApiError("Stable OpenCode 2 routes by directory; workspace selectors are unsupported")
            if any(name.startswith("location[") for name in request_params):
                raise V2ApiError("V2 location parameters must use the location argument")
            request_params.update(location.parameters())
        for name, value in request_params.items():
            command.extend(("--param", f"{name}={value}"))
        if payload is not None:
            command.extend(("--data", json.dumps(payload, separators=(",", ":"))))
        if payload is None and use_read_api(self.opencode_bin, self.api_url, operation):
            try:
                async with self._v2_api_slots:
                    decoded = await asyncio.to_thread(
                        read_api, operation, params=params,
                        location=location.reference() if location else None,
                        timeout=timeout,
                    )
            except ReadAPIError as error:
                raise V2ApiError(str(error)) from error
            return validate_v2_api_payload(operation, decoded, location)
        try:
            async with self._v2_api_slots:
                # The 2.x CLI can exit before a large piped stdout is drained
                # (observed at 256 KiB). A private regular descriptor avoids
                # that truncation without exposing transcripts in argv/logs.
                with tempfile.TemporaryFile() as output:
                    process = await asyncio.create_subprocess_exec(
                        *command,
                        stdout=output,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    result = await communicate_with_cleanup(process, timeout=timeout)
                    output.seek(0, os.SEEK_END)
                    if output.tell() > MAX_API_RESPONSE_BYTES:
                        raise V2ApiError("OpenCode V2 API response exceeded the size limit")
                    output.seek(0)
                    stdout = output.read(MAX_API_RESPONSE_BYTES + 1)
                    if not stdout and result is not None:
                        stdout = result[0] or b""
        except OSError as error:
            raise V2ApiError("OpenCode V2 API unavailable") from error
        if result is None:
            raise V2ApiError("OpenCode V2 API timed out")
        if process.returncode != 0:
            raise V2ApiError("OpenCode V2 API request failed")
        if len(stdout) > MAX_API_RESPONSE_BYTES:
            raise V2ApiError("OpenCode V2 API response exceeded the size limit")
        if not stdout.strip():
            return None
        try:
            decoded = json.loads(stdout.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise V2ApiError("OpenCode V2 API returned invalid JSON") from error
        return validate_v2_api_payload(operation, decoded, location)

    async def _v2_prompt_metadata(self, sessions, live_ids):
        from .recent_open import default_recent_open_file, load_recent_open_sessions
        if sessions is None:
            return {}, {}
        remembered = load_recent_open_sessions(default_recent_open_file("v2", self.api_url))
        wanted = set(live_ids) | set(remembered)
        rows = [row for row in sessions if row.get("id") in wanted][:40]
        now = time.monotonic()

        async def refresh(row):
            identifier = row["id"]
            updated = row.get("updated", 0)
            cached = self._v2_prompt_cache.get(identifier)
            if cached and (cached[0] == updated or now < cached[3]):
                return
            try:
                payload = await self._v2_api_json("v2.session.message.list", params={
                    "sessionID": identifier, "type": "user", "order": "desc", "limit": "1",
                })
                records = payload.get("data") if isinstance(payload, dict) else None
                if not isinstance(records, list) or len(records) > 1:
                    return
                record = records[0] if records else {}
                raw = record.get("text")
                condensed = condense_worker_prompt(
                    raw if isinstance(raw, str) else ""
                )
                text = clip_at_word_boundary(
                    clean_string(condensed), MAX_LAST_PROMPT_LENGTH
                )
                created = clean_int((record.get("time") or {}).get("created"))
                self._v2_prompt_cache[identifier] = (updated, text, created, now + 3)
            except V2ApiError:
                if cached:
                    self._v2_prompt_cache[identifier] = (*cached[:3], now + 3)

        await asyncio.gather(*(refresh(row) for row in rows))
        valid = {row["id"] for row in sessions}
        self._v2_prompt_cache = {key: value for key, value in self._v2_prompt_cache.items() if key in valid}
        return ({key: value[1] for key, value in self._v2_prompt_cache.items()},
                {key: value[2] for key, value in self._v2_prompt_cache.items()})

    async def _v2_collect_sessions(self) -> list[dict[str, Any]] | None:
        remaining = self.limit
        cursor = ""
        seen_cursors: set[str] = set()
        seen_pages: set[str] = set()
        sessions: dict[str, dict[str, Any]] = {}
        pages = 0
        try:
            while remaining > 0:
                if pages >= MAX_V2_SESSION_PAGES:
                    return None
                pages += 1
                page_size = min(50, remaining)
                params = {"limit": str(page_size), "order": "desc"}
                if cursor:
                    params = {"limit": str(page_size), "cursor": cursor}
                payload = await self._v2_api_json(
                    "v2.session.list", params=params, timeout=30
                )
                page = parse_v2_session_page(payload)
                if page is None:
                    return None
                rows, next_cursor = page
                fingerprint = json.dumps(
                    payload["data"], ensure_ascii=True, sort_keys=True, separators=(",", ":")
                )
                if fingerprint in seen_pages:
                    return None
                seen_pages.add(fingerprint)
                for item in rows:
                    existing = sessions.get(item["id"])
                    if existing is None or item["updated"] >= existing["updated"]:
                        sessions[item["id"]] = item
                    if len(sessions) >= self.limit:
                        break
                remaining = self.limit - len(sessions)
                if not next_cursor or remaining <= 0:
                    break
                if next_cursor == cursor or next_cursor in seen_cursors:
                    return None
                seen_cursors.add(next_cursor)
                cursor = next_cursor
        except V2ApiError:
            return None
        return list(sessions.values())

    async def _v2_collect_projects(self) -> list[dict[str, Any]] | None:
        try:
            return parse_v2_projects(
                await self._v2_api_json("v2.project.list")
            )
        except V2ApiError:
            return None

    async def _v2_health_status(self) -> tuple[str, str, dict[str, str]]:
        try:
            health = await self._v2_api_json("v2.health.get")
        except V2ApiError:
            return (
                "offline",
                "OpenCode V2 service unavailable; active state stale",
                dict(self._v2_active_statuses),
            )
        if not isinstance(health, dict) or health.get("healthy") is not True:
            return (
                "offline",
                "OpenCode V2 health response was invalid; active state stale",
                dict(self._v2_active_statuses),
            )
        version = clean_string(health.get("version"))
        detail = "Live V2 API" + (f" v{version}" if version else "")
        connection = "live"
        if version != EXPECTED_V2_VERSION:
            connection = "degraded"
            detail += f"; expected v{EXPECTED_V2_VERSION}"
        try:
            statuses = parse_v2_active(
                await self._v2_api_json("v2.session.active")
            )
        except V2ApiError:
            statuses = None
        if statuses is None:
            return (
                "degraded",
                detail + "; active state stale",
                dict(self._v2_active_statuses),
            )
        self._v2_active_statuses = dict(statuses)
        return connection, detail, statuses

    async def _v2_loaded_locations(self) -> tuple[tuple[V2Location, ...], bool]:
        try:
            parsed = parse_v2_locations(
                await self._v2_api_json("v2.debug.location.list")
            )
        except V2ApiError:
            parsed = None
        if parsed is None:
            return self._v2_locations_cache, False
        self._v2_locations_cache = parsed
        return parsed, True

    async def _v2_pending_requests(
        self,
    ) -> tuple[dict[str, list[dict[str, Any]]], bool]:
        locations, locations_ok = await self._v2_loaded_locations()
        if not locations_ok:
            return merge_permissions(self._v2_pending_cache), False

        async def collect_location(
            location: V2Location,
        ) -> tuple[dict[str, list[dict[str, Any]]], bool]:
            permission_task = asyncio.create_task(
                self._v2_permission_requests(location)
            )
            form_task = asyncio.create_task(self._v2_form_requests(location))
            permissions, forms = await asyncio.gather(permission_task, form_task)
            return merge_permissions(permissions[0], forms[0]), permissions[1] and forms[1]

        results = await asyncio.gather(
            *(collect_location(location) for location in locations)
        )
        complete = all(ok for _requests, ok in results)
        if not complete:
            return merge_permissions(self._v2_pending_cache), False
        pending = merge_permissions(*(requests for requests, _ok in results))
        self._v2_pending_cache = pending
        return merge_permissions(pending), True

    async def _v2_fetch_background_jobs(
        self, sessions: list[dict[str, Any]] | None
    ) -> dict[str, tuple[str, ...]]:
        """Running background shell commands for the sessions being shown.

        Asks shell.list once per distinct session directory (stable 2.x routes
        by directory) and keeps only running commands whose metadata.sessionID
        is a known session. Any failed lookup means "unknown", never a crash.
        """
        if not sessions:
            return {}

        # Jobs come from sessions active recently; look only in those folders,
        # newest first, so a long history never turns into many lookups.
        cutoff = int(time.time() * 1000) - BACKGROUND_JOB_RECENT_MS
        recent = sorted(
            (item for item in sessions
             if isinstance(item, dict) and clean_int(item.get("updated")) >= cutoff),
            key=lambda item: -clean_int(item.get("updated")),
        )
        directories: list[V2Location] = []
        seen: set[str] = set()
        for item in recent:
            directory = item.get("directory")
            if not isinstance(directory, str) or not directory or directory in seen:
                continue
            seen.add(directory)
            directories.append(V2Location(directory))
            if len(directories) >= BACKGROUND_JOB_DIRECTORY_LIMIT:
                break

        if not directories:
            return {}

        known_session_ids = {
            clean_string(item.get("id"))
            for item in sessions
            if isinstance(item, dict) and clean_string(item.get("id"))
        }

        # The managed service runs on this machine, so a job's pid can be checked.
        check_pids = not self.api_url or urllib.parse.urlparse(self.api_url).hostname in {"127.0.0.1", "localhost", "::1"}

        async def fetch_for_location(location: V2Location) -> dict[str, list[str]]:
            try:
                payload = await self._v2_api_json("v2.shell.list", location=location)
            except V2ApiError:
                return {}
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, list):
                return {}
            jobs_by_session: dict[str, list[str]] = {}
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                if clean_string(entry.get("status")) != "running":
                    continue
                pid = entry.get("pid")
                if check_pids and type(pid) is int and pid > 0 and not Path(f"/proc/{pid}").exists():
                    continue  # reported running, but its process is gone

                metadata = entry.get("metadata")
                if not isinstance(metadata, dict):
                    continue
                session_id = clean_string(metadata.get("sessionID"))
                if not session_id or session_id not in known_session_ids:
                    continue
                command = clean_string(entry.get("command"))
                if not command:
                    continue
                jobs_by_session.setdefault(session_id, []).append(command[:120])
            return jobs_by_session

        results = await asyncio.gather(
            *(fetch_for_location(location) for location in directories),
            return_exceptions=True,
        )

        merged: dict[str, list[str]] = {}
        for result in results:
            if isinstance(result, Exception) or not isinstance(result, dict):
                continue
            for session_id, commands in result.items():
                merged.setdefault(session_id, []).extend(commands)
        return {session_id: tuple(commands) for session_id, commands in merged.items()}

    async def _v2_permission_requests(
        self, location: V2Location
    ) -> tuple[dict[str, list[dict[str, Any]]], bool]:
        try:
            payload = await self._v2_api_json(
                "v2.permission.request.list", location=location
            )
        except V2ApiError:
            return {}, False
        parsed = parse_v2_permissions(payload)
        return (parsed, True) if parsed is not None else ({}, False)

    async def _v2_form_requests(
        self, location: V2Location
    ) -> tuple[dict[str, list[dict[str, Any]]], bool]:
        try:
            payload = await self._v2_api_json(
                "v2.form.request.list", location=location
            )
        except V2ApiError:
            return {}, False
        parsed = parse_v2_forms(payload)
        return (parsed, True) if parsed is not None else ({}, False)

    async def _refresh_named_agent_registry_v2(
        self,
    ) -> tuple[tuple[NamedAgentStatus, ...], bool, str, str, bool, str]:
        configured = {
            agent.name: agent.configured
            for agent in expected_named_agents({
                name: path.is_file() for name, path in self.named_agent_files.items()
            })
        }
        location = V2Location(str(Path.home()))

        async def request(operation: str) -> tuple[Any, str]:
            try:
                return await self._v2_api_json(operation, location=location), ""
            except V2ApiError:
                return None, "unavailable"

        agent_result, mcp_result = await asyncio.gather(
            request("v2.agent.list"), request("v2.mcp.list")
        )
        agents_payload, agent_error = agent_result
        mcp_payload, mcp_error = mcp_result
        normalized_agents = normalize_v2_agents(agents_payload)
        parsed = parse_named_agent_registry(normalized_agents, configured)
        if parsed is not None:
            self._named_agent_registry = parsed
            self._named_agent_registry_valid = True
            self._named_agents_stale = False
            self._named_agents_error = ""
        else:
            if not self._named_agent_registry_valid:
                self._named_agent_registry = expected_named_agents(configured)
            else:
                self._named_agent_registry = tuple(
                    replace(
                        agent,
                        configured=bool(configured.get(agent.name)),
                    )
                    for agent in self._named_agent_registry
                )
            self._named_agents_stale = True
            self._named_agents_error = (
                f"Agent registry {agent_error}"
                if agent_error
                else "Agent registry response was invalid"
            )

        browser_status = parse_v2_signed_in_tabs_status(mcp_payload)
        if browser_status is not None:
            self._signed_in_tabs_status = browser_status
            self._signed_in_tabs_stale = False
            self._signed_in_tabs_error = ""
        else:
            self._signed_in_tabs_stale = True
            self._signed_in_tabs_error = (
                f"Browser MCP {mcp_error}"
                if mcp_error
                else "Browser MCP response was invalid"
            )
        return (
            self._named_agent_registry,
            self._named_agents_stale,
            self._named_agents_error,
            self._signed_in_tabs_status,
            self._signed_in_tabs_stale,
            self._signed_in_tabs_error,
        )

    async def _collect_known_projects(self) -> list[Any] | None:
        projects = await asyncio.to_thread(
            read_projects_from_database, self.session_db_file
        )
        if projects is not None:
            return projects
        result = await self._command_json("debug", "scrap", "--pure")
        return result if isinstance(result, list) else None

    async def collect_activity(self) -> DashboardSnapshot | None:
        if self.backend == "v2":
            return await self._collect_activity_v2()
        return await self._collect_activity_v1()

    async def _collect_activity_v1(self) -> DashboardSnapshot | None:
        """Refresh live agent signals without rerunning CLI collection.

        Combines the session payload cached by the last full ``collect()``
        with fast local signals only: HTTP status and permissions, process
        liveness, tmux mapping, and read-only session database metadata.
        Returns ``None`` until the first full collection populated the cache.
        """
        cache = self._activity_cache
        if cache is None:
            return None
        status_task = asyncio.create_task(self._api_status())
        instances_task = asyncio.create_task(
            asyncio.to_thread(read_opencode_instances, backend="v1")
        )
        local_state_task = asyncio.create_task(
            asyncio.to_thread(
                read_local_runtime_state,
                self.permission_state_dir,
                backend="v1",
            )
        )
        activity_task = asyncio.create_task(
            asyncio.to_thread(
                read_session_turn_activity,
                self.session_db_file,
                allow_stale=True,
            )
        )
        interactions_task = asyncio.create_task(
            asyncio.to_thread(read_last_user_interactions, self.session_db_file)
        )
        home_agent_evidence_task = asyncio.create_task(
            asyncio.to_thread(read_home_agent_session_evidence, self.session_db_file)
        )
        (
            connection,
            connection_detail,
            api_statuses,
            permissions,
            request_authority,
        ) = await status_task
        instance_counts, unmapped_instances, instance_ttys = await instances_task
        (
            local_state,
            turn_activity,
            last_interactions,
            home_agent_evidence,
        ) = await asyncio.gather(
            local_state_task,
            activity_task,
            interactions_task,
            home_agent_evidence_task,
        )
        local_permissions = local_state.permissions
        local_statuses = local_state.statuses
        api_session_ids = frozenset(api_statuses) | frozenset(permissions)
        statuses = reconcile_statuses(api_statuses, local_statuses)
        turn_activity = dict(turn_activity)
        for session_id, status in statuses.items():
            if status == "idle":
                turn_activity[session_id] = (
                    False,
                    turn_activity.get(session_id, (False, 0))[1],
                    turn_activity.get(session_id, (False, 0, 0))[2],
                )
        permissions = reconcile_permissions(
            permissions, local_permissions, request_authority, api_session_ids
        )
        prompts_task = asyncio.create_task(
            asyncio.to_thread(
                read_latest_user_prompts,
                self.session_db_file,
                live_session_ids(
                    statuses, instance_counts, permissions, cache["sessions"]
                ),
            )
        )
        terminal_map, attached_map = await asyncio.to_thread(
            read_tmux_tty_state, instance_ttys
        )
        latest_prompts = await prompts_task
        known_projects = cache["known_projects"]
        project_names = cache["project_names"]
        sessions = parse_sessions(
            cache["sessions"],
            statuses,
            instance_counts,
            terminal_map,
            permissions,
            attached_map,
            last_interactions,
            turn_activity,
            latest_prompts,
            cache["agent_parent_ids"],
            home_agent_evidence,
        )
        sessions = assign_project_roots(sessions, known_projects)
        sessions = apply_session_routes(sessions, cache["routes"], project_names)
        projects = build_projects(sessions, known_projects, project_names)
        named_agents = summarize_named_agents(self._named_agent_registry, sessions)
        return DashboardSnapshot(
            sessions=sessions,
            projects=projects,
            services=cache["services"],
            metrics=cache["metrics"],
            connection=connection,
            connection_detail=connection_detail,
            unmapped_instance_count=unmapped_instances,
            briefings=cache["briefings"],
            briefing_report_id=cache["briefing_report_id"],
            briefing_generated_at=cache["briefing_generated_at"],
            briefing_status=cache["briefing_status"],
            named_agents=named_agents,
            named_agents_stale=self._named_agents_stale,
            named_agents_error=self._named_agents_error,
            signed_in_tabs_status=self._signed_in_tabs_status,
            signed_in_tabs_stale=self._signed_in_tabs_stale,
            signed_in_tabs_error=self._signed_in_tabs_error,
        )

    async def _collect_sessions(
        self, known_projects: dict[str, str]
    ) -> list[Any] | None:
        directories: list[Path] = [Path.cwd(), Path.home()]
        for directory in known_projects.values():
            path = Path(directory)
            if path.is_dir() and path not in directories:
                directories.append(path)

        semaphore = asyncio.Semaphore(3)
        password = self._password()
        api_available = not self.api_url_error and (
            not password or api_credentials_are_safe(self.api_url)
        )

        async def collect_directory(path: Path) -> Any:
            async with semaphore:
                if api_available:
                    query = urllib.parse.urlencode(
                        {
                            "directory": str(path),
                            "roots": "false",
                            "limit": self.limit,
                        }
                    )
                    try:
                        payload = await asyncio.to_thread(
                            self._request_json,
                            f"/session?{query}",
                            password,
                        )
                        payload = unwrap_data(payload)
                        if isinstance(payload, list):
                            return payload
                    except (
                        OSError,
                        ValueError,
                        urllib.error.HTTPError,
                        urllib.error.URLError,
                        TimeoutError,
                    ):
                        pass
                payload = await self._command_json(
                    "session",
                    "list",
                    "--format",
                    "json",
                    "--max-count",
                    str(self.limit),
                    "--pure",
                    cwd=path,
                    timeout=30,
                )
                if isinstance(payload, list):
                    return payload
                await asyncio.sleep(0.2)
                return await self._command_json(
                    "session",
                    "list",
                    "--format",
                    "json",
                    "--max-count",
                    str(self.limit),
                    "--pure",
                    cwd=path,
                    timeout=30,
                )

        results = await asyncio.gather(
            *(collect_directory(path) for path in directories)
        )

        merged: dict[str, Any] = {}
        any_succeeded = False
        for payload in results:
            if not isinstance(payload, list):
                continue
            any_succeeded = True
            for item in payload:
                if not isinstance(item, dict):
                    continue
                session_id = item.get("id")
                if not isinstance(session_id, str) or not session_id:
                    continue
                existing = merged.get(session_id)
                if (
                    existing is None
                    or clean_int(item.get("updated"))
                    >= clean_int(existing.get("updated"))
                ):
                    merged[session_id] = item
        return list(merged.values()) if any_succeeded else None

    def _session_cache_path(self, known_projects: dict[str, str]) -> Path:
        identity = "\0".join(
            [
                self.backend,
                self.opencode_bin or "",
                str(self.limit),
                str(self.projects_file),
                str(self.session_db_file),
                *(f"{key}={value}" for key, value in sorted(known_projects.items())),
            ]
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        return default_permission_state_dir().parent / f"ocdeck-sessions-{digest}.json"

    @staticmethod
    def _read_shared_session_cache(path: Path) -> list[Any] | None:
        try:
            age_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - int(
                path.stat().st_mtime * 1000
            )
            if age_ms > SESSION_CACHE_TTL_MS:
                return None
            content = path.read_bytes()
            if len(content) > MAX_SESSION_CACHE_BYTES:
                return None
            payload = json.loads(content)
            sessions = payload.get("sessions") if isinstance(payload, dict) else None
            return sessions if isinstance(sessions, list) else None
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    @staticmethod
    def _write_shared_session_cache(path: Path, sessions: list[Any]) -> None:
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary.write_text(
                json.dumps(
                    {
                        "created_ms": int(
                            datetime.now(timezone.utc).timestamp() * 1000
                        ),
                        "sessions": sessions,
                    },
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            os.replace(temporary, path)
        except (OSError, TypeError, ValueError):
            with contextlib.suppress(OSError):
                temporary.unlink()

    async def _collect_sessions_shared(
        self, known_projects: dict[str, str]
    ) -> list[Any] | None:
        """Share the expensive session-list sweep between OC Deck windows."""
        database_sessions = await asyncio.to_thread(
            read_sessions_from_database,
            self.session_db_file,
            MAX_DATABASE_SESSIONS,
        )
        if database_sessions is not None:
            return database_sessions
        cache_path = self._session_cache_path(known_projects)
        lock_path = cache_path.with_suffix(".lock")
        while True:
            cached = self._read_shared_session_cache(cache_path)
            if cached is not None:
                return cached
            try:
                lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                try:
                    if (
                        datetime.now(timezone.utc).timestamp() - lock_path.stat().st_mtime
                        > SESSION_CACHE_LOCK_STALE_SECONDS
                    ):
                        lock_path.unlink()
                        continue
                except OSError:
                    pass
                await asyncio.sleep(0.1)
                continue
            except OSError:
                return await self._collect_sessions(known_projects)
            try:
                cached = self._read_shared_session_cache(cache_path)
                if cached is not None:
                    return cached
                sessions = await self._collect_sessions(known_projects)
                if sessions is not None:
                    self._write_shared_session_cache(cache_path, sessions)
                return sessions
            finally:
                os.close(lock_fd)
                with contextlib.suppress(OSError):
                    lock_path.unlink()

    async def _command_json(
        self,
        *arguments: str,
        cwd: Path | None = None,
        timeout: float = 15,
    ) -> Any:
        if not self.opencode_bin:
            return None
        try:
            process = await asyncio.create_subprocess_exec(
                self.opencode_bin,
                *arguments,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=str(cwd) if cwd else None,
            )
            result = await communicate_with_cleanup(process, timeout=timeout)
        except OSError:
            return None
        if result is None:
            return None
        stdout, _ = result
        if process.returncode != 0:
            return None
        try:
            return json.loads(stdout.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return None

    async def _api_status(
        self,
    ) -> tuple[
        str,
        str,
        dict[str, str],
        dict[str, list[dict[str, str]]],
        tuple[bool, bool],
    ]:
        password = self._password()
        if self.api_url_error:
            return "blocked", self.api_url_error, {}, {}, (False, False)
        if password and not api_credentials_are_safe(self.api_url):
            return (
                "blocked",
                "Refusing credentials over non-loopback HTTP",
                {},
                {},
                (False, False),
            )
        try:
            health = await asyncio.to_thread(self._request_json, "/global/health", password)
        except urllib.error.HTTPError as error:
            if error.code == 401:
                if password:
                    return (
                        "locked",
                        "API rejected the configured credentials",
                        {},
                        {},
                        (False, False),
                    )
                return (
                    "locked",
                    "API locked; set OPENCODE_SERVER_PASSWORD for agent states",
                    {},
                    {},
                    (False, False),
                )
            return "offline", f"API returned HTTP {error.code}", {}, {}, (False, False)
        except (
            OSError,
            ValueError,
            urllib.error.URLError,
            TimeoutError,
        ):
            return "offline", "API offline; CLI metadata active", {}, {}, (False, False)

        health_data = unwrap_data(health)
        if not isinstance(health_data, dict) or health_data.get("healthy") is not True:
            return "offline", "API health response was invalid", {}, {}, (False, False)

        statuses: dict[str, str] = {}
        try:
            payload = await asyncio.to_thread(
                self._request_json,
                "/session/status",
                password,
            )
            data = unwrap_data(payload)
            parsed_statuses = parse_api_statuses(data)
            if parsed_statuses is not None:
                statuses = parsed_statuses
        except (
            OSError,
            ValueError,
            urllib.error.URLError,
            urllib.error.HTTPError,
            TimeoutError,
        ):
            pass

        permission_result, question_result = await asyncio.gather(
            self._api_permissions(password), self._api_questions(password)
        )
        permissions, permissions_ok = permission_result
        questions, questions_ok = question_result
        permissions = merge_permissions(permissions, questions)

        version = ""
        version_value = health_data.get("version")
        if isinstance(version_value, str):
            version = clean_string(version_value)
        detail = "Live API" + (f" v{version}" if version else "")
        return "live", detail, statuses, permissions, (permissions_ok, questions_ok)

    async def _refresh_named_agent_registry(
        self,
    ) -> tuple[tuple[NamedAgentStatus, ...], bool, str, str, bool, str]:
        configured = {
            agent.name: agent.configured
            for agent in expected_named_agents({
                name: path.is_file() for name, path in self.named_agent_files.items()
            })
        }
        password = self._password()
        if self.api_url_error:
            error = "Agent registry URL is invalid"
            return self._stale_named_agent_registry(configured, error)
        if password and not api_credentials_are_safe(self.api_url):
            error = "Agent registry credentials require a loopback API"
            return self._stale_named_agent_registry(configured, error)

        async def request(path: str) -> tuple[Any, str]:
            try:
                return await asyncio.to_thread(self._request_json, path, password), ""
            except urllib.error.HTTPError as error:
                return None, f"HTTP {error.code}"
            except (
                OSError,
                ValueError,
                urllib.error.URLError,
                TimeoutError,
            ):
                return None, "unavailable"

        agent_result, mcp_result = await asyncio.gather(
            request("/agent"), request("/mcp")
        )
        agents_payload, agent_error = agent_result
        mcp_payload, mcp_error = mcp_result
        parsed = parse_named_agent_registry(
            unwrap_data(agents_payload), configured
        )
        if parsed is not None:
            self._named_agent_registry = parsed
            self._named_agent_registry_valid = True
            self._named_agents_stale = False
            self._named_agents_error = ""
        else:
            if not self._named_agent_registry_valid:
                self._named_agent_registry = expected_named_agents(configured)
            else:
                self._named_agent_registry = tuple(
                    replace(
                        agent,
                        configured=bool(configured.get(agent.name)),
                    )
                    for agent in self._named_agent_registry
                )
            self._named_agents_stale = True
            self._named_agents_error = (
                f"Agent registry {agent_error}"
                if agent_error
                else "Agent registry response was invalid"
            )

        browser_status = parse_signed_in_tabs_status(mcp_payload)
        if browser_status is not None:
            self._signed_in_tabs_status = browser_status
            self._signed_in_tabs_stale = False
            self._signed_in_tabs_error = ""
        else:
            self._signed_in_tabs_stale = True
            self._signed_in_tabs_error = (
                f"Browser MCP {mcp_error}"
                if mcp_error
                else "Browser MCP response was invalid"
            )
        return (
            self._named_agent_registry,
            self._named_agents_stale,
            self._named_agents_error,
            self._signed_in_tabs_status,
            self._signed_in_tabs_stale,
            self._signed_in_tabs_error,
        )

    def _stale_named_agent_registry(
        self, configured: dict[str, bool], error: str
    ) -> tuple[tuple[NamedAgentStatus, ...], bool, str, str, bool, str]:
        if not self._named_agent_registry_valid:
            self._named_agent_registry = expected_named_agents(configured)
        else:
            self._named_agent_registry = tuple(
                replace(
                    agent,
                    configured=bool(configured.get(agent.name)),
                )
                for agent in self._named_agent_registry
            )
        self._named_agents_stale = True
        self._named_agents_error = error
        self._signed_in_tabs_stale = True
        self._signed_in_tabs_error = error.replace("Agent registry", "Browser MCP")
        return (
            self._named_agent_registry,
            True,
            error,
            self._signed_in_tabs_status,
            self._signed_in_tabs_stale,
            self._signed_in_tabs_error,
        )

    async def _api_permissions(
        self, password: str
    ) -> tuple[dict[str, list[dict[str, str]]], bool]:
        try:
            payload = await asyncio.to_thread(
                self._request_json,
                "/permission",
                password,
            )
        except (
            OSError,
            ValueError,
            urllib.error.URLError,
            urllib.error.HTTPError,
            TimeoutError,
        ):
            return {}, False
        data = unwrap_data(payload)
        if not isinstance(data, list):
            return {}, False
        pending: dict[str, list[dict[str, str]]] = {}
        for item in data:
            if not isinstance(item, dict):
                return {}, False
            session_id = clean_string(item.get("sessionID"))
            request_id = clean_string(item.get("id"))
            permission = (
                clean_string(item.get("permission"))
                or clean_string(item.get("type"))
            )
            if not session_id or not request_id or not permission:
                return {}, False
            patterns = item.get("patterns")
            if patterns is not None and not isinstance(patterns, list):
                return {}, False
            if isinstance(patterns, list) and not all(
                isinstance(pattern, str) for pattern in patterns
            ):
                return {}, False
            pattern = ""
            if isinstance(patterns, list) and patterns:
                pattern = clean_string(patterns[0])
            pending.setdefault(session_id, []).append(
                {
                    "id": request_id,
                    "permission": permission,
                    "pattern": pattern,
                }
            )
        return pending, True

    async def _api_questions(
        self, password: str
    ) -> tuple[dict[str, list[dict[str, str]]], bool]:
        try:
            payload = await asyncio.to_thread(
                self._request_json,
                "/question",
                password,
            )
        except (
            OSError,
            ValueError,
            urllib.error.URLError,
            urllib.error.HTTPError,
            TimeoutError,
        ):
            return {}, False
        data = unwrap_data(payload)
        if not isinstance(data, list):
            return {}, False
        pending: dict[str, list[dict[str, str]]] = {}
        for item in data:
            if not isinstance(item, dict):
                return {}, False
            session_id = clean_string(item.get("sessionID"))
            request_id = clean_string(item.get("id"))
            if not session_id or not request_id:
                return {}, False
            prompt = ""
            questions = item.get("questions")
            if not isinstance(questions, list):
                return {}, False
            for question in questions:
                if not isinstance(question, dict):
                    return {}, False
                prompt = clean_string(question.get("question")) or clean_string(
                    question.get("header")
                )
                if prompt:
                    break
            pending.setdefault(session_id, []).append(
                {
                    "id": request_id,
                    "permission": "question",
                    "pattern": prompt or "Input required in the terminal",
                }
            )
        return pending, True

    async def rename_session(self, session_id: str, title: str) -> str:
        """Update a session title through the loopback API.

        Returns an empty string on success, or a human-readable error message.
        """
        if self.backend == "v2":
            try:
                result = await self._v2_api_json(
                    "v2.session.rename",
                    params={"sessionID": session_id},
                    payload={"title": title},
                )
            except V2ApiError as error:
                return str(error)
            error = v2_error_message(result)
            if error:
                return error
            if result is not None:
                return "OpenCode V2 returned an invalid rename response"
            return ""
        password = self._password()
        if self.api_url_error:
            return self.api_url_error
        if password and not api_credentials_are_safe(self.api_url):
            return "Refusing credentials over non-loopback HTTP"
        try:
            await asyncio.to_thread(
                self._request_json,
                f"/session/{urllib.parse.quote(session_id, safe='')}",
                password,
                method="PATCH",
                payload={"title": title},
            )
        except urllib.error.HTTPError as error:
            if error.code == 401:
                if password:
                    return "API rejected the configured credentials"
                return "API locked; set OPENCODE_SERVER_PASSWORD to rename"
            return f"API returned HTTP {error.code}"
        except (
            OSError,
            ValueError,
            urllib.error.URLError,
            TimeoutError,
        ):
            return "API unavailable; title kept unchanged"
        return ""

    async def approve_permission(self, session_id: str, permission_id: str) -> str:
        """Approve one pending permission through OpenCode's loopback API."""
        if self.backend == "v2":
            try:
                result = await self._v2_api_json(
                    "v2.session.permission.reply",
                    params={"sessionID": session_id, "requestID": permission_id},
                    payload={"decision": "once"},
                )
            except V2ApiError as error:
                return str(error)
            error = v2_error_message(result)
            if error:
                return error
            if result is not None:
                return "OpenCode V2 returned an invalid permission reply"
            return ""
        password = self._password()
        if self.api_url_error:
            return self.api_url_error
        if password and not api_credentials_are_safe(self.api_url):
            return "Refusing credentials over non-loopback HTTP"
        path = (
            f"/session/{urllib.parse.quote(session_id, safe='')}/permissions/"
            f"{urllib.parse.quote(permission_id, safe='')}"
        )
        try:
            await asyncio.to_thread(
                self._request_json,
                path,
                password,
                method="POST",
                payload={"response": "once"},
            )
        except urllib.error.HTTPError as error:
            if error.code == 401:
                if password:
                    return "API rejected the configured credentials"
                return "API locked; set OPENCODE_SERVER_PASSWORD to approve"
            return f"API returned HTTP {error.code}"
        except (
            OSError,
            ValueError,
            urllib.error.URLError,
            TimeoutError,
        ):
            return "API unavailable; permission was not approved"
        return ""

    async def enable_session_browser(self, directory: Path, session_id: str) -> BrowserAccessResult:
        if self.backend == "v2":
            from .v2_browser import browser_access as native_browser_access
            return await native_browser_access(self, directory, session_id)
        if self.backend == "v1" and self._password() and not api_credentials_are_safe(self.api_url):
            return BrowserAccessResult(error="Refusing credentials over non-loopback HTTP")
        return await asyncio.to_thread(browser_access, self, directory, session_id)

    async def create_browser_session(self, directory: Path) -> BrowserAccessResult:
        if self.backend == "v1" and self._password() and not api_credentials_are_safe(self.api_url):
            return BrowserAccessResult(error="Refusing credentials over non-loopback HTTP")
        key = str(directory.resolve())
        if key in self._unresolved_browser_creations:
            return self._unresolved_browser_creations[key]
        if self.backend == "v2":
            from .v2_browser import browser_access as native_browser_access
            result = await native_browser_access(self, directory)
        else:
            result = await asyncio.to_thread(browser_access, self, directory)
        if result.uncertain:
            self._unresolved_browser_creations[key] = result
        return result

    async def create_session(self, directory: Path) -> tuple[str, str]:
        if self.backend != "v2":
            return "", "Explicit session creation is available only for V2"
        location = V2Location(str(directory))
        try:
            payload = await self._v2_api_json(
                "v2.session.create", payload={"location": location.reference()}
            )
        except V2ApiError as error:
            return "", str(error)
        error = v2_error_message(payload)
        if error:
            return "", error
        data = payload.get("data") if isinstance(payload, dict) else None
        session_id = clean_string(data.get("id")) if isinstance(data, dict) else ""
        if (
            not session_id.startswith("ses")
            or not isinstance(data, dict)
            or not v2_location_ref_is_valid(data.get("location"), location)
        ):
            return "", "OpenCode V2 returned an invalid session"
        return session_id, ""

    async def interrupt_session(self, session_id: str) -> tuple[bool, str]:
        """Stop the running turn in the service, like Esc twice in OpenCode.

        Returns (interrupted, error). The session stays; an idle session is
        simply not interrupted. Used when no terminal exists to act in, e.g.
        a headless `opencode run` started by an agent.
        """
        if self.backend != "v2":
            return False, "Interrupting through the service is available only for V2"
        if not V2_SESSION_ID_PATTERN.fullmatch(session_id):
            return False, "Invalid OpenCode V2 session id"
        try:
            result = await self._v2_api_json("v2.session.interrupt", params={"sessionID": session_id})
        except V2ApiError as error:
            return False, str(error)
        error = v2_error_message(result)
        if error:
            return False, error
        if not isinstance(result, dict) or type(result.get("interrupted")) is not bool:
            return False, "OpenCode V2 returned an invalid interrupt response"
        return result["interrupted"], ""

    async def remove_session(self, session_id: str) -> str:
        if self.backend != "v2":
            return "Session rollback is available only for V2"
        try:
            result = await self._v2_api_json(
                "v2.session.remove", params={"sessionID": session_id}
            )
        except V2ApiError as error:
            return str(error)
        error = v2_error_message(result)
        if error:
            return error
        if result is not None:
            return "OpenCode V2 returned an invalid session removal response"
        return ""

    def _request_json(
        self,
        path: str,
        password: str,
        *,
        method: str = "GET",
        payload: Any = None,
    ) -> Any:
        headers = {"Accept": "application/json"}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if password:
            username = self._username()
            token = base64.b64encode(f"{username}:{password}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"
        request = urllib.request.Request(
            self.api_url + path, data=data, headers=headers, method=method
        )
        opener = urllib.request.build_opener(NoRedirectHandler())
        with opener.open(request, timeout=2.5) as response:
            content = response.read(MAX_API_RESPONSE_BYTES + 1)
        if len(content) > MAX_API_RESPONSE_BYTES:
            raise ValueError("API response exceeded the size limit")
        return json.loads(content.decode("utf-8")) if content else None

    def _username(self) -> str:
        return os.environ.get("OPENCODE_SERVER_USERNAME") or self.server_username

    def _password(self) -> str:
        return os.environ.get("OPENCODE_SERVER_PASSWORD") or self.server_password

    async def _service_states(self) -> tuple[ServiceRecord, ...]:
        allowlist = (
            SERVICE_ALLOWLIST
            if self.backend == "v1"
            else (("opencode2.service", "OpenCode V2", "shared V2 API and session service"),)
            + tuple(item for item in SERVICE_ALLOWLIST if item[0] != "opencode-web.service")
        )
        units = [unit for unit, _, _ in allowlist]
        try:
            process = await asyncio.create_subprocess_exec(
                "systemctl",
                "--user",
                "is-active",
                *units,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            result = await communicate_with_cleanup(process, timeout=5)
            states = (
                result[0].decode("utf-8", errors="replace").splitlines()
                if result is not None
                else []
            )
        except OSError:
            states = []

        records: list[ServiceRecord] = []
        for index, (unit, label, role) in enumerate(allowlist):
            state = states[index].strip() if index < len(states) else "unknown"
            records.append(ServiceRecord(unit=unit, label=label, role=role, state=state))
        return tuple(records)

    @staticmethod
    def _find_opencode() -> str | None:
        executable = Path.home() / ".opencode" / "bin" / "opencode"
        if executable.is_file():
            return str(executable)
        return shutil.which("opencode")

    @staticmethod
    def _find_opencode2() -> str | None:
        from .v2_read_api import find_opencode2
        return find_opencode2()


def unwrap_data(payload: Any) -> Any:
    if isinstance(payload, dict) and set(payload).issuperset({"data"}):
        return payload.get("data")
    return payload


def v2_error_message(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    tag = payload.get("_tag")
    message = payload.get("message")
    if isinstance(tag, str) and tag:
        return clean_string(message)[:500] if isinstance(message, str) else clean_string(tag)
    error_type = payload.get("type")
    if isinstance(error_type, str) and error_type and isinstance(message, str):
        detail = clean_string(message) or clean_string(error_type)
        return detail[:500]
    nested = payload.get("error")
    return v2_error_message(nested) if isinstance(nested, dict) else ""


def _v2_raw_string(value: Any) -> str:
    return value if isinstance(value, str) and value else ""


def v2_location_ref_is_valid(
    value: Any, expected: V2Location | None = None
) -> bool:
    if not isinstance(value, dict) or not set(value).issubset(
        {"directory", "workspaceID"}
    ):
        return False
    directory = _v2_raw_string(value.get("directory"))
    workspace = value.get("workspaceID", "")
    if not directory or not isinstance(workspace, str):
        return False
    if workspace and not workspace.startswith("wrk"):
        return False
    return expected is None or (
        directory == expected.directory and workspace == expected.workspace_id
    )


def validate_v2_api_payload(operation, decoded, location):
    error_message = v2_error_message(decoded)
    if error_message:
        raise V2ApiError(error_message)
    if operation == "v2.health.get" and isinstance(decoded, dict):
        decoded = {**decoded, "healthy": bool(decoded.get("version")) and type(decoded.get("pid")) is int and decoded["pid"] > 0}
    if operation in V2_LOCATION_OPERATIONS and not v2_location_envelope_is_valid(decoded, location):
        raise V2ApiError("OpenCode V2 returned an invalid location envelope")
    return decoded


def parse_v2_locations(payload: Any) -> tuple[V2Location, ...] | None:
    if not isinstance(payload, list):
        return None
    locations: list[V2Location] = []
    seen: set[tuple[str, str]] = set()
    for value in payload:
        if not v2_location_ref_is_valid(value):
            return None
        location = V2Location(
            _v2_raw_string(value.get("directory")),
            _v2_raw_string(value.get("workspaceID")),
        )
        key = (location.directory, location.workspace_id)
        if key not in seen:
            seen.add(key)
            locations.append(location)
    return tuple(locations)


def v2_location_envelope_is_valid(
    payload: Any, expected: V2Location | None
) -> bool:
    if expected is None or not isinstance(payload, dict):
        return False
    location = payload.get("location")
    if not isinstance(location, dict) or not set(location).issubset(
        {"directory", "workspaceID", "project"}
    ):
        return False
    if not v2_location_ref_is_valid(
        {key: location[key] for key in ("directory", "workspaceID") if key in location},
        expected,
    ):
        return False
    project = location.get("project")
    if project is None:
        # Stable 2.x location responses expose the public directory reference.
        return True
    if not isinstance(project, dict) or set(project) != {
        "id",
        "directory",
        "canonical",
    }:
        return False
    return all(_v2_raw_string(project.get(key)) for key in project)


def parse_v2_session_page(
    payload: Any,
) -> tuple[list[dict[str, Any]], str] | None:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    cursor = payload.get("cursor")
    if not isinstance(data, list) or not isinstance(cursor, dict):
        return None
    next_cursor = cursor.get("next")
    if next_cursor is not None and not isinstance(next_cursor, str):
        return None
    sessions: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            return None
        session_id = clean_string(item.get("id"))
        project_id = clean_string(item.get("projectID"))
        location = item.get("location")
        time_info = item.get("time")
        if (
            not session_id
            or not project_id
            or not isinstance(location, dict)
            or not isinstance(time_info, dict)
        ):
            return None
        directory = _v2_raw_string(location.get("directory"))
        workspace_id = location.get("workspaceID")
        created = time_info.get("created")
        updated = time_info.get("updated")
        if (
            not session_id.startswith("ses")
            or not directory
            or not v2_location_ref_is_valid(location)
            or isinstance(created, bool)
            or not isinstance(created, (int, float))
            or isinstance(updated, bool)
            or not isinstance(updated, (int, float))
        ):
            return None
        archived = time_info.get("archived")
        if isinstance(archived, (int, float)) and not isinstance(archived, bool):
            continue
        parent_id = item.get("parentID")
        agent = item.get("agent")
        model = item.get("model")
        metadata = item.get("metadata")
        if parent_id is not None and (
            not isinstance(parent_id, str) or not parent_id.startswith("ses")
        ):
            return None
        if agent is not None and not isinstance(agent, str):
            return None
        if model is not None and not isinstance(model, dict):
            return None
        if metadata is not None and not isinstance(metadata, dict):
            return None
        normalized: dict[str, Any] = {
            "id": session_id,
            "projectId": project_id,
            "directory": directory,
            "title": clean_string(item.get("title")),
            "created": clean_int(created),
            "updated": clean_int(updated),
            "location": location,
        }
        if parent_id:
            normalized["parentID"] = parent_id
        if workspace_id:
            normalized["workspaceID"] = workspace_id
        if agent:
            normalized["agent"] = agent
        if model is not None:
            normalized["model"] = model
        if metadata is not None:
            normalized["metadata"] = metadata
        if isinstance(item.get("permissions"), list):
            normalized["permissions"] = item["permissions"]
        sessions.append(normalized)
    return sessions, next_cursor or ""


def parse_v2_projects(payload: Any) -> list[dict[str, Any]] | None:
    if not isinstance(payload, list):
        return None
    projects: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            return None
        project_id = clean_string(item.get("id"))
        canonical = clean_string(item.get("canonical"))
        sandboxes = item.get("sandboxes")
        if (
            not project_id
            or not canonical
            or not isinstance(sandboxes, list)
            or not all(isinstance(path, str) for path in sandboxes)
        ):
            return None
        projects.append(
            {
                "id": project_id,
                "worktree": canonical,
                "sandboxes": [clean_string(path) for path in sandboxes if path],
            }
        )
    return projects


def parse_v2_active(payload: Any) -> dict[str, str] | None:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return None
    statuses: dict[str, str] = {}
    for session_id, value in data.items():
        key = clean_string(session_id)
        if not key or not isinstance(value, dict) or value.get("type") != "running":
            return None
        statuses[key] = "busy"
    return statuses


def parse_v2_permissions(
    payload: Any,
) -> dict[str, list[dict[str, Any]]] | None:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return None
    pending: dict[str, list[dict[str, Any]]] = {}
    for item in data:
        if not isinstance(item, dict):
            return None
        request_id = clean_string(item.get("id"))
        session_id = clean_string(item.get("sessionID"))
        action = clean_string(item.get("action"))
        resources = item.get("resources")
        if (
            not request_id
            or not session_id
            or not action
            or not isinstance(resources, list)
            or not all(isinstance(resource, str) for resource in resources)
        ):
            return None
        visible_resources = tuple(clean_string(resource) for resource in resources)
        pattern = "; ".join(resource for resource in visible_resources if resource)
        if not pattern:
            pattern = clean_string(item.get("message"))
        pending.setdefault(session_id, []).append(
            {
                "id": request_id,
                "permission": action,
                "pattern": pattern,
                "resources": visible_resources,
            }
        )
    return pending


def parse_v2_forms(payload: Any) -> dict[str, list[dict[str, str]]] | None:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return None
    pending: dict[str, list[dict[str, str]]] = {}
    for item in data:
        if not isinstance(item, dict):
            return None
        form_id = clean_string(item.get("id"))
        session_id = clean_string(item.get("sessionID"))
        title = clean_string(item.get("title"))
        fields = item.get("fields")
        if (
            not form_id
            or not session_id
            or not isinstance(fields, list)
            or not fields
            or not all(isinstance(field, dict) for field in fields)
        ):
            return None
        first = fields[0]
        field_detail = (
            clean_string(first.get("title"))
            or clean_string(first.get("description"))
            or clean_string(first.get("placeholder"))
        )
        title = title or "Input required"
        detail = (
            f"{title}: {field_detail}"
            if field_detail and field_detail != title
            else title
        )
        pending.setdefault(session_id, []).append(
            {"id": form_id, "permission": "question", "pattern": detail}
        )
    return pending


def normalize_v2_agents(payload: Any) -> list[dict[str, Any]] | None:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return None
    normalized: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            return None
        agent = dict(item)
        model = item.get("model")
        if isinstance(model, dict):
            agent["model"] = {
                **model,
                "modelID": model.get("modelID") or model.get("id"),
            }
        normalized.append(agent)
    return normalized


def parse_v2_signed_in_tabs_status(payload: Any) -> str | None:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return None
    for item in data:
        if not isinstance(item, dict):
            return None
        if clean_string(item.get("name")) != "signed_in_tabs":
            continue
        status = item.get("status")
        if not isinstance(status, dict):
            return None
        value = clean_string(status.get("status")).lower()
        return value or "unknown"
    return "not configured"


def v2_request_directories(
    sessions: list[Any] | None,
    statuses: dict[str, str],
    instance_counts: dict[str, int],
) -> tuple[V2Location, ...]:
    live_candidates: list[V2Location] = []
    other_candidates: list[V2Location] = []
    for item in sessions or ():
        if not isinstance(item, dict):
            continue
        session_id = clean_string(item.get("id"))
        directory = _v2_raw_string(item.get("directory"))
        if directory:
            location = V2Location(
                directory,
                _v2_raw_string(item.get("workspaceID")),
            )
            target = (
                live_candidates
                if session_id in statuses
                or clean_int(instance_counts.get(session_id)) > 0
                else other_candidates
            )
            target.append(location)
    candidates = [V2Location(str(Path.home())), *live_candidates, *other_candidates]
    locations: list[V2Location] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        key = (candidate.directory, candidate.workspace_id)
        if key in seen:
            continue
        seen.add(key)
        locations.append(candidate)
    return tuple(locations)


def parse_signed_in_tabs_status(payload: Any) -> str | None:
    data = unwrap_data(payload)
    if not isinstance(data, dict):
        return None
    signed_tabs = data.get("signed_in_tabs")
    if signed_tabs is None:
        return "not configured"
    if not isinstance(signed_tabs, dict):
        return None
    status = clean_string(signed_tabs.get("status")).lower()
    return status or "unknown"


def parse_api_statuses(payload: Any) -> dict[str, str] | None:
    data = unwrap_data(payload)
    if not isinstance(data, dict):
        return None
    statuses: dict[str, str] = {}
    for session_id, value in data.items():
        session_key = clean_string(session_id)
        status = local_status(value)
        if not session_key or not status:
            return None
        statuses[session_key] = status
    return statuses


def read_server_credentials(path: Path) -> tuple[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name not in {"OPENCODE_SERVER_USERNAME", "OPENCODE_SERVER_PASSWORD"}:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[name] = value
    return (
        values.get("OPENCODE_SERVER_USERNAME", "opencode"),
        values.get("OPENCODE_SERVER_PASSWORD", ""),
    )


def default_permission_state_dir() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    root = Path(runtime) if runtime else Path("/tmp") / f"ocdeck-{os.getuid()}"
    return root / "ocdeck-permissions"


MAX_PERMISSION_STATE_BYTES = 1024 * 1024


def read_local_runtime_state(
    state_dir: Path | None = None,
    proc_root: Path = Path("/proc"),
    *,
    backend: str = "v1",
) -> LocalRuntimeState:
    if backend not in {"v1", "v2"}:
        raise ValueError("OpenCode backend must be 'v1' or 'v2'")
    directory = state_dir or default_permission_state_dir()
    try:
        files = tuple(sorted(directory.glob("*.json")))
    except OSError:
        return LocalRuntimeState({}, {})

    uid = os.getuid()
    producers: list[RuntimeProducer] = []

    for path in files:
        payload, identity = read_state_payload(path)
        if payload is None:
            unlink_same_file(path, identity)
            continue
        pid = clean_int(payload.get("pid"))
        process = proc_root / str(pid)
        producer = state_producer_validation(process, payload, uid, backend)
        if producer == "other":
            continue
        if producer != "valid":
            unlink_same_file(path, identity)
            continue

        updated = clean_int(payload.get("updated"))
        if updated <= 0:
            try:
                updated = int(path.stat().st_mtime * 1000)
            except OSError:
                updated = 0
        producers.append(RuntimeProducer(payload, process_owned_session_ids(process), updated))
    return reconcile_runtime_producers(producers)


def read_local_permissions(
    state_dir: Path | None = None,
    proc_root: Path = Path("/proc"),
    *,
    backend: str = "v1",
) -> dict[str, list[dict[str, str]]]:
    return read_local_runtime_state(state_dir, proc_root, backend=backend).permissions


def read_local_statuses(
    state_dir: Path | None = None,
    proc_root: Path = Path("/proc"),
    *,
    backend: str = "v1",
) -> dict[str, str]:
    return read_local_runtime_state(state_dir, proc_root, backend=backend).statuses


def read_state_payload(path: Path) -> tuple[dict[str, Any] | None, tuple[int, int] | None]:
    identity: tuple[int, int] | None = None
    try:
        with path.open("r", encoding="utf-8") as handle:
            metadata = os.fstat(handle.fileno())
            identity = (metadata.st_dev, metadata.st_ino)
            if metadata.st_size > MAX_PERMISSION_STATE_BYTES:
                return None, identity
            payload = json.load(handle)
    except (OSError, ValueError, json.JSONDecodeError):
        return None, identity
    return (payload, identity) if isinstance(payload, dict) else (None, identity)


def unlink_same_file(path: Path, identity: tuple[int, int] | None) -> None:
    if identity is None:
        return
    try:
        current = path.stat()
        if (current.st_dev, current.st_ino) == identity:
            path.unlink()
    except OSError:
        pass


def read_process_state_and_start(process_dir: Path) -> tuple[str, str]:
    try:
        stat = (process_dir / "stat").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return "", ""
    closing_parenthesis = stat.rfind(")")
    if closing_parenthesis < 0:
        return "", ""
    fields = stat[closing_parenthesis + 1 :].split()
    return (
        fields[0] if fields else "",
        fields[19] if len(fields) > 19 else "",
    )


def read_process_start_ticks(process_dir: Path) -> str:
    return read_process_state_and_start(process_dir)[1]


def state_producer_validation(
    process: Path,
    payload: dict[str, Any],
    uid: int,
    backend: str,
) -> str:
    try:
        if process.stat().st_uid != uid:
            return "invalid"
        comm = (process / "comm").read_text(errors="replace").strip()
    except OSError:
        return "invalid"
    if comm == "opencode":
        actual_backend = "v1"
    elif comm in {"opencode2", "opencode2.exe"}:
        try:
            command = [
                os.fsdecode(value)
                for value in (process / "cmdline").read_bytes().split(b"\0")
                if value
            ]
        except OSError:
            return "invalid"
        if not command or Path(command[0]).name not in {"opencode2", "opencode2.exe"}:
            return "invalid"
        actual_backend = "v2"
    else:
        return "invalid"
    state, actual_start = read_process_state_and_start(process)
    if state in {"X", "Z"}:
        return "invalid"
    expected_start = clean_string(payload.get("processStartTicks"))
    version = clean_int(payload.get("notifierVersion"))
    if version >= 8 and (not expected_start or not actual_start):
        return "invalid"
    if expected_start and expected_start != actual_start:
        return "invalid"
    return "valid" if actual_backend == backend else "other"


def state_producer_is_live(
    process: Path,
    payload: dict[str, Any],
    uid: int,
    backend: str = "v1",
) -> bool:
    return state_producer_validation(process, payload, uid, backend) == "valid"


def process_owned_session_ids(process: Path) -> frozenset[str]:
    try:
        arguments = [
            value.decode("utf-8", errors="replace")
            for value in (process / "cmdline").read_bytes().split(b"\0")
            if value
        ]
    except OSError:
        return frozenset()
    sessions: set[str] = set()
    for index, argument in enumerate(arguments):
        if argument in {"-s", "--session"}:
            session_id = (
                clean_string(arguments[index + 1])
                if index + 1 < len(arguments)
                else ""
            )
            if session_id:
                sessions.add(session_id)
        elif argument.startswith("--session=") or argument.startswith("-s="):
            session_id = clean_string(argument.partition("=")[2])
            if session_id:
                sessions.add(session_id)
    return frozenset(sessions)


def reconcile_permissions(
    api_requests: dict[str, list[dict[str, str]]],
    local_requests: dict[str, list[dict[str, str]]],
    api_authority: tuple[bool, bool],
    api_session_ids: Iterable[str] = (),
) -> dict[str, list[dict[str, str]]]:
    """Prefer API request kinds only for sessions represented by that API."""
    permissions_authoritative, questions_authoritative = api_authority
    represented = set(api_session_ids)
    fallback: dict[str, list[dict[str, str]]] = {}
    for session_id, requests in local_requests.items():
        for request in requests:
            is_question = clean_string(request.get("permission")) == "question"
            authoritative = (
                questions_authoritative if is_question else permissions_authoritative
            )
            if session_id in represented and authoritative:
                continue
            fallback.setdefault(session_id, []).append(request)
    return merge_permissions(api_requests, fallback)


def reconcile_statuses(
    api_statuses: dict[str, str], local_statuses: dict[str, str]
) -> dict[str, str]:
    """Retain standalone producers while preferring API state for known sessions."""
    return {**local_statuses, **api_statuses}


def read_markdown_projects(projects_file: Path) -> tuple[tuple[str, str], ...]:
    try:
        source = projects_file.read_text(encoding="utf-8")
    except OSError:
        return ()
    return parse_markdown_projects(source, projects_file)


def read_project_registry(registry_file: Path) -> tuple[tuple[str, str], ...]:
    try:
        payload = json.loads(registry_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    if not isinstance(payload, dict):
        return ()
    rows = payload.get("projects")
    if payload.get("version") != 1 or not isinstance(rows, list):
        return ()
    projects: list[tuple[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            return ()
        name = clean_string(row.get("name"))
        directory = clean_string(row.get("path"))
        identity = clean_string(row.get("id"))
        if not name or not directory or not identity or not Path(directory).is_absolute():
            return ()
        path_key = normalized_project_path(directory)
        if path_key in seen:
            return ()
        seen.add(path_key)
        projects.append((name, os.path.normpath(directory)))
    return tuple(projects)


def read_session_routes(routes_file: Path) -> dict[str, str]:
    try:
        payload = json.loads(routes_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    sessions = payload.get("sessions") if isinstance(payload, dict) else None
    if not isinstance(sessions, dict):
        return {}
    return {
        clean_string(session_id): clean_string(project_name)
        for session_id, project_name in sessions.items()
        if clean_string(session_id) and clean_string(project_name)
    }


def read_projects_from_database(
    db_file: str | Path | None = None,
) -> list[dict[str, Any]] | None:
    path = Path(db_file) if db_file else DEFAULT_SESSION_DB_FILE
    try:
        if not path.is_file():
            return None
        connection = sqlite3.connect(
            f"file:{urllib.parse.quote(str(path))}?mode=ro",
            uri=True,
            timeout=1,
        )
    except (OSError, sqlite3.Error):
        return None
    try:
        columns = {
            clean_string(row[1])
            for row in connection.execute("PRAGMA table_info(project)").fetchall()
            if len(row) > 1
        }
        if not {"id", "worktree"}.issubset(columns):
            return None
        sandboxes = "sandboxes" if "sandboxes" in columns else "'[]'"
        rows = connection.execute(
            f"SELECT id, worktree, {sandboxes} FROM project WHERE id != 'global'"
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        connection.close()

    projects: list[dict[str, Any]] = []
    for project_id, worktree, raw_sandboxes in rows:
        if not isinstance(project_id, str) or not project_id:
            continue
        sandbox_paths: list[str] = []
        if isinstance(raw_sandboxes, str):
            try:
                decoded = json.loads(raw_sandboxes)
            except json.JSONDecodeError:
                decoded = []
            if isinstance(decoded, list):
                sandbox_paths = [
                    item for item in decoded if isinstance(item, str) and item
                ]
        projects.append(
            {
                "id": project_id,
                "worktree": worktree if isinstance(worktree, str) else "",
                "sandboxes": sandbox_paths,
            }
        )
    return projects


def read_sessions_from_database(
    db_file: str | Path | None = None,
    limit: int = MAX_DATABASE_SESSIONS,
) -> list[dict[str, Any]] | None:
    path = Path(db_file) if db_file else DEFAULT_SESSION_DB_FILE
    try:
        if not path.is_file():
            return None
        connection = sqlite3.connect(
            f"file:{urllib.parse.quote(str(path))}?mode=ro",
            uri=True,
            timeout=1,
        )
    except (OSError, sqlite3.Error):
        return None
    try:
        columns = {
            clean_string(row[1])
            for row in connection.execute("PRAGMA table_info(session)").fetchall()
            if len(row) > 1
        }
        required = {
            "id",
            "project_id",
            "directory",
            "title",
            "time_created",
            "time_updated",
        }
        if not required.issubset(columns):
            return None
        parent = "parent_id" if "parent_id" in columns else "NULL"
        metadata = "metadata" if "metadata" in columns else "NULL"
        agent = '"agent"' if "agent" in columns else "NULL"
        archive_filter = (
            "WHERE time_archived IS NULL" if "time_archived" in columns else ""
        )
        rows = connection.execute(
            f"SELECT id, project_id, {parent}, directory, title, {metadata}, "
            f"{agent}, time_created, time_updated FROM session {archive_filter} "
            "ORDER BY time_updated DESC LIMIT ?",
            (max(1, min(int(limit), MAX_DATABASE_SESSIONS)),),
        ).fetchall()
    except (TypeError, ValueError, sqlite3.Error):
        return None
    finally:
        connection.close()

    sessions: list[dict[str, Any]] = []
    for (
        session_id,
        project_id,
        parent_id,
        directory,
        title,
        raw_metadata,
        agent_name,
        created,
        updated,
    ) in rows:
        if not all(
            isinstance(value, str) and value
            for value in (session_id, project_id, directory, title)
        ):
            continue
        metadata_value: dict[str, Any] = {}
        if isinstance(raw_metadata, str):
            try:
                decoded = json.loads(raw_metadata)
            except json.JSONDecodeError:
                decoded = None
            if isinstance(decoded, dict):
                metadata_value = decoded
        session = {
            "id": session_id,
            "projectId": project_id,
            "directory": directory,
            "title": title,
            "created": clean_int(created),
            "updated": clean_int(updated),
        }
        if isinstance(parent_id, str) and parent_id:
            session["parentID"] = parent_id
        if metadata_value:
            session["metadata"] = metadata_value
        if isinstance(agent_name, str) and agent_name:
            session["agent"] = agent_name
        sessions.append(session)
    return sessions if sessions or not rows else None


def read_agent_parent_ids(
    db_file: str | Path | None = None,
) -> dict[str, str]:
    """Read native agent-spawned subagent relationships without message content.

    Only OpenCode's own ``parent_id`` links group a subagent beneath the agent
    session that spawned it. Orchestration metadata (for example Home Agent
    worker launches) is launch provenance, not a subagent relationship, and is
    deliberately ignored.
    """
    path = Path(db_file) if db_file else DEFAULT_SESSION_DB_FILE
    try:
        if not path.is_file():
            return {}
        connection = sqlite3.connect(
            f"file:{urllib.parse.quote(str(path))}?mode=ro",
            uri=True,
            timeout=1,
        )
    except (OSError, sqlite3.Error):
        return {}
    try:
        rows = connection.execute(
            "SELECT id, parent_id FROM session "
            "WHERE time_archived IS NULL "
            "AND parent_id IS NOT NULL AND parent_id != '' AND parent_id != id"
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        connection.close()
    return {
        session_id: parent_id
        for session_id, parent_id in rows
        if isinstance(session_id, str)
        and session_id
        and isinstance(parent_id, str)
        and parent_id
    }


def read_home_agent_session_evidence(
    db_file: str | Path | None = None,
) -> dict[str, str]:
    """Read agent-routing metadata without creating sessions or model requests."""
    path = Path(db_file) if db_file else DEFAULT_SESSION_DB_FILE
    try:
        if not path.is_file():
            return {}
        connection = sqlite3.connect(
            f"file:{urllib.parse.quote(str(path))}?mode=ro",
            uri=True,
            timeout=1,
        )
    except (OSError, sqlite3.Error):
        return {}
    try:
        session_columns = {
            clean_string(row[1])
            for row in connection.execute("PRAGMA table_info(session)").fetchall()
            if len(row) > 1
        }
        if "id" not in session_columns:
            return {}
        metadata_expression = "metadata" if "metadata" in session_columns else "NULL"
        agent_expression = '"agent"' if "agent" in session_columns else "NULL"
        archive_filter = (
            " WHERE time_archived IS NULL" if "time_archived" in session_columns else ""
        )
        rows = connection.execute(
            f"SELECT id, {metadata_expression}, {agent_expression} "
            f"FROM session{archive_filter}"
        ).fetchall()

        message_columns = {
            clean_string(row[1])
            for row in connection.execute("PRAGMA table_info(message)").fetchall()
            if len(row) > 1
        }
        latest_message_agents: dict[str, str] = {}
        if {"session_id", "data"}.issubset(message_columns):
            order_columns = ["session_id"]
            if "time_created" in message_columns:
                order_columns.append("time_created DESC")
            if "id" in message_columns:
                order_columns.append("id DESC")
            message_rows = connection.execute(
                "SELECT session_id, json_extract(data, '$.agent') FROM message "
                "WHERE json_valid(data) "
                "AND typeof(json_extract(data, '$.agent')) = 'text' "
                f"ORDER BY {', '.join(order_columns)}"
            ).fetchall()
            for session_id, agent in message_rows:
                session_key = clean_string(session_id)
                if session_key and session_key not in latest_message_agents:
                    latest_message_agents[session_key] = clean_string(agent)
    except sqlite3.Error:
        return {}
    finally:
        connection.close()

    evidence: dict[str, str] = {}
    for session_id, metadata_raw, recorded_agent in rows:
        session_key = clean_string(session_id)
        if not session_key:
            continue
        metadata = None
        if isinstance(metadata_raw, str):
            try:
                metadata = json.loads(metadata_raw)
            except json.JSONDecodeError:
                metadata = None
        elif isinstance(metadata_raw, dict):
            metadata = metadata_raw
        home_metadata = metadata.get("homeAgent") if isinstance(metadata, dict) else None
        if isinstance(home_metadata, dict) and "agent" in home_metadata:
            evidence[session_key] = clean_string(home_metadata.get("agent"))
            continue
        if session_key in latest_message_agents:
            evidence[session_key] = latest_message_agents[session_key]
            continue
        fallback_agent = clean_string(recorded_agent)
        if fallback_agent:
            evidence[session_key] = fallback_agent
    return evidence


def read_briefings_file(
    briefings_file: Path,
    max_bytes: int = MAX_BRIEFINGS_FILE_BYTES,
) -> str | None:
    try:
        with briefings_file.open("rb") as handle:
            content = handle.read(max(0, max_bytes) + 1)
    except OSError:
        return None
    if len(content) > max_bytes:
        return None
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return None


def read_archived_session_ids(
    db_file: str | Path | None = None,
) -> frozenset[str]:
    path = Path(db_file) if db_file else DEFAULT_SESSION_DB_FILE
    try:
        if not path.is_file():
            return frozenset()
        connection = sqlite3.connect(
            f"file:{urllib.parse.quote(str(path))}?mode=ro",
            uri=True,
            timeout=1,
        )
    except (OSError, sqlite3.Error):
        return frozenset()
    try:
        rows = connection.execute(
            "SELECT id FROM session WHERE time_archived IS NOT NULL"
        ).fetchall()
    except sqlite3.Error:
        return frozenset()
    finally:
        connection.close()
    return frozenset(
        row[0] for row in rows if isinstance(row[0], str) and row[0]
    )


def read_last_user_interactions(
    db_file: str | Path | None = None,
) -> dict[str, int]:
    path = Path(db_file) if db_file else DEFAULT_SESSION_DB_FILE
    try:
        if not path.is_file():
            return {}
        connection = sqlite3.connect(
            f"file:{urllib.parse.quote(str(path))}?mode=ro",
            uri=True,
            timeout=1,
        )
    except (OSError, sqlite3.Error):
        return {}
    try:
        rows = connection.execute(
            "SELECT session_id, MAX(time_created) FROM message "
            "WHERE json_valid(data) AND json_extract(data, '$.role') = 'user' "
            "GROUP BY session_id"
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        connection.close()
    return {
        session_id: timestamp
        for session_id, timestamp in rows
        if isinstance(session_id, str)
        and session_id
        and isinstance(timestamp, int)
        and timestamp > 0
    }


# Long provider calls can be silent between database writes for several minutes.
# Keep the fallback bounded, but align it with the existing review window.
TURN_ACTIVITY_FALLBACK_WINDOW_MS = ACTIVE_TURN_WINDOW_MS


def read_session_turn_activity(
    db_file: str | Path | None = None,
    now_ms: int | None = None,
    allow_stale: bool = False,
) -> dict[str, tuple[bool, int, int]]:
    """Read latest assistant-turn metadata per session from the local DB.

    Returns ``{session_id: (turn_in_progress, completed_ms, activity_ms)}``
    derived only from ``message.data`` JSON metadata and part timestamps.
    Message text and tool output live in a separate table that is never
    queried; a missing or unreadable database simply yields no signals.
    """
    path = Path(db_file) if db_file else DEFAULT_SESSION_DB_FILE
    try:
        if not path.is_file():
            return {}
        connection = sqlite3.connect(
            f"file:{urllib.parse.quote(str(path))}?mode=ro",
            uri=True,
            timeout=1,
        )
    except (OSError, sqlite3.Error):
        return {}
    try:
        has_parts = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'part'"
        ).fetchone() is not None
        activity_column = (
            "COALESCE((SELECT MAX(p.time_updated) FROM part AS p "
            "WHERE p.message_id = ranked.id), ranked.time_created)"
            if has_parts
            else "ranked.time_created"
        )
        rows = connection.execute(
            f"SELECT session_id, data, {activity_column} FROM ("
            "SELECT id, session_id, time_created, data, ROW_NUMBER() OVER ("
            "PARTITION BY session_id ORDER BY time_created DESC, id DESC"
            ") AS position FROM message "
            "WHERE json_valid(data) "
            "AND json_extract(data, '$.role') = 'assistant'"
            ") AS ranked WHERE position = 1"
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        connection.close()
    activity: dict[str, tuple[bool, int, int]] = {}
    current = (
        now_ms
        if now_ms is not None
        else int(datetime.now(timezone.utc).timestamp() * 1000)
    )
    for session_id, payload, latest_activity_ms in rows:
        if not isinstance(session_id, str) or not session_id:
            continue
        try:
            data = json.loads(payload) if isinstance(payload, str) else None
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        time_info = data.get("time")
        completed = (
            clean_int(time_info.get("completed"))
            if isinstance(time_info, dict)
            else 0
        )
        finish = data.get("finish")
        # No completion timestamp means the assistant turn is still being
        # written; `tool-calls` means tools are executing between messages.
        active = completed <= 0 or finish == "tool-calls"
        if has_parts and active and not allow_stale:
            active = (
                current - clean_int(latest_activity_ms)
                <= TURN_ACTIVITY_FALLBACK_WINDOW_MS
            )
        activity[session_id] = (active, completed, clean_int(latest_activity_ms))
    return activity


WORKER_TASK_MARKER = "user-approved task:"
MAX_PROMPT_JSON_BYTES = 4096


def live_session_ids(
    statuses: dict[str, str],
    instance_counts: dict[str, int],
    permissions: dict[str, list[dict[str, str]]],
    sessions_result: list[Any] | None,
) -> tuple[str, ...]:
    """Session IDs that currently own a live agents-board row.

    Mirrors ``agent_state``'s non-idle conditions so prompts are read only
    for sessions the dashboard can actually show them for.
    """
    if not sessions_result:
        return ()
    listed = {
        item.get("id")
        for item in sessions_result
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    return tuple(
        sorted(
            session_id
            for session_id in listed
            if session_id
            and (
                normalize_status(statuses.get(session_id)) in {"busy", "retry"}
                or clean_int(instance_counts.get(session_id, 0)) > 0
                or session_id in permissions
            )
        )
    )


def condense_worker_prompt(text: str) -> str:
    """Prefer the approved task inside Home Agent launch boilerplate.

    Worker launch messages start with fixed boilerplate before an explicit
    ``User-approved task:`` marker; everything after the marker is the part
    worth showing. A marker with no trailing text means nothing is usable.
    """
    marker_at = text.casefold().find(WORKER_TASK_MARKER)
    if marker_at < 0:
        return text
    candidate = text[marker_at + len(WORKER_TASK_MARKER) :].lstrip(" :-")
    return candidate if candidate.strip() else ""


def clip_at_word_boundary(text: str, limit: int) -> str:
    """Clip ``text`` to ``limit`` characters without cutting a word in half.

    A prompt clipped mid-word reads as garbled text in the agents board, so a
    cut that lands inside a word backs up to the last whitespace boundary and
    appends an ellipsis. The lookback is capped at a fifth of ``limit`` so a
    single very long word (a URL, a pasted blob) is not over-shortened by
    chasing a distant space; that case keeps the plain hard cut. The result
    never exceeds ``limit`` characters and is never empty for non-empty input.
    """
    if not isinstance(text, str) or limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    lookback = max(1, limit // 5)
    for index in range(limit - 1, limit - lookback - 1, -1):
        if index > 0 and text[index].isspace():
            return text[:index].rstrip() + "…"
    return text[:limit]


def read_latest_user_prompts(
    db_file: str | Path | None = None,
    session_ids: Iterable[str] | None = None,
) -> dict[str, str]:
    """Read each listed session's latest textual user prompt, read-only.

    Only the first ``text`` part of the newest ``user`` message per requested
    session is read; attachments, tool output, and assistant content are never
    queried. Values are sanitized to a bounded single line. A missing, locked,
    or unreadable database simply yields no prompts instead of failing.
    """
    wanted = tuple(
        dict.fromkeys(
            session_id
            for session_id in (session_ids or ())
            if isinstance(session_id, str) and session_id
        )
    )
    if not wanted:
        return {}
    path = Path(db_file) if db_file else DEFAULT_SESSION_DB_FILE
    try:
        if not path.is_file():
            return {}
        connection = sqlite3.connect(
            f"file:{urllib.parse.quote(str(path))}?mode=ro",
            uri=True,
            timeout=1,
        )
    except (OSError, sqlite3.Error):
        return {}
    prompts: dict[str, str] = {}
    try:
        for session_id in wanted:
            message_row = connection.execute(
                "SELECT id FROM message "
                "WHERE session_id = ? AND json_valid(data) "
                "AND json_extract(data, '$.role') = 'user' "
                "ORDER BY time_created DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            if message_row is None or not isinstance(message_row[0], str):
                continue
            part_row = connection.execute(
                "SELECT substr(data, 1, ?) FROM part "
                "WHERE message_id = ? AND json_valid(data) "
                "AND json_extract(data, '$.type') = 'text' "
                "ORDER BY time_created ASC LIMIT 1",
                (MAX_PROMPT_JSON_BYTES, message_row[0]),
            ).fetchone()
            if part_row is None or not isinstance(part_row[0], str):
                continue
            try:
                payload = json.loads(part_row[0])
            except json.JSONDecodeError:
                continue
            text = payload.get("text") if isinstance(payload, dict) else None
            if not isinstance(text, str) or not text.strip():
                continue
            condensed = sanitize_terminal_text(condense_worker_prompt(text))
            if condensed:
                prompts[session_id] = clip_at_word_boundary(
                    condensed, MAX_LAST_PROMPT_LENGTH
                )
    except sqlite3.Error:
        return {}
    finally:
        connection.close()
    return prompts


def parse_briefings(
    source: str | bytes | dict[str, Any] | None,
) -> BriefingReportRecord | None:
    if isinstance(source, dict):
        payload: Any = source
    elif isinstance(source, (str, bytes)):
        if len(source) > MAX_BRIEFINGS_FILE_BYTES:
            return None
        if isinstance(source, str):
            try:
                if len(source.encode("utf-8")) > MAX_BRIEFINGS_FILE_BYTES:
                    return None
            except UnicodeEncodeError:
                return None
        try:
            payload = json.loads(source)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            return None
    else:
        return None
    if not isinstance(payload, dict):
        return None
    schema_version = payload.get("schemaVersion")
    if type(schema_version) is not int or schema_version != 1:
        return None

    report_id = _briefing_string(
        payload.get("reportID"), MAX_BRIEFING_IDENTIFIER_LENGTH
    )
    generated_at = parse_rfc3339(payload.get("generatedAt"))
    status = _briefing_string(payload.get("status"), 16)
    projects = payload.get("projects")
    if (
        report_id is None
        or generated_at is None
        or status not in {"running", "completed", "partial", "failed"}
        or not isinstance(projects, list)
    ):
        return None

    if len(projects) > MAX_BRIEFING_PROJECTS:
        return None
    parsed_projects: list[ProjectBriefingRecord] = []
    for item in projects:
        briefing = parse_project_briefing(item)
        if briefing is None:
            return None
        parsed_projects.append(briefing)
    return BriefingReportRecord(
        report_id=report_id,
        generated_at=generated_at,
        status=status,
        projects=tuple(parsed_projects),
    )


def parse_project_briefing(payload: Any) -> ProjectBriefingRecord | None:
    if not isinstance(payload, dict):
        return None
    project_id = _briefing_string(
        payload.get("projectID"), MAX_BRIEFING_IDENTIFIER_LENGTH
    )
    project_path = _briefing_string(
        payload.get("projectPath"), MAX_BRIEFING_PATH_LENGTH
    )
    name = _briefing_string(payload.get("name"), MAX_BRIEFING_TEXT_LENGTH)
    assessment = _briefing_string(payload.get("assessment"), 16)
    summary = _briefing_string(
        payload.get("summary"), MAX_BRIEFING_TEXT_LENGTH, allow_empty=True
    )
    confidence = _briefing_string(payload.get("confidence"), 16)
    research_status = _briefing_string(payload.get("researchStatus"), 16)
    evidence_value = payload.get("evidenceAt")
    evidence_at = (
        parse_rfc3339(evidence_value) if evidence_value is not None else None
    )
    completed_outputs = payload.get("completedOutputs")
    blockers = payload.get("blockers")
    next_steps = payload.get("nextSteps")
    evidence = payload.get("evidence")
    null_evidence_allowed = (
        research_status in {"queued", "running", "failed"}
        or assessment == "unknown"
    )
    if (
        project_id is None
        or project_path is None
        or name is None
        or assessment
        not in {"on-track", "at-risk", "blocked", "waiting", "complete", "unknown"}
        or summary is None
        or confidence not in {"low", "medium", "high"}
        or research_status not in {"queued", "running", "completed", "failed"}
        or "evidenceAt" not in payload
        or (
            evidence_value is None
            and not null_evidence_allowed
        )
        or (evidence_value is not None and evidence_at is None)
        or not isinstance(completed_outputs, list)
        or not isinstance(blockers, list)
        or not isinstance(next_steps, list)
        or not isinstance(evidence, list)
    ):
        return None

    parsed_outputs: list[tuple[str, str]] = []
    for item in completed_outputs[:MAX_BRIEFING_ARRAY_ITEMS]:
        if not isinstance(item, dict):
            return None
        label = _briefing_string(item.get("label"), MAX_BRIEFING_TEXT_LENGTH)
        locator = _briefing_string(
            item.get("locator"), MAX_BRIEFING_PATH_LENGTH
        )
        if label is None or locator is None:
            return None
        parsed_outputs.append((label, locator))

    parsed_blockers: list[str] = []
    for item in blockers[:MAX_BRIEFING_ARRAY_ITEMS]:
        if not isinstance(item, dict):
            return None
        blocker = _briefing_string(
            item.get("summary"), MAX_BRIEFING_TEXT_LENGTH
        )
        if blocker is None:
            return None
        parsed_blockers.append(blocker)

    parsed_steps: list[NextStepRecord] = []
    for item in next_steps[:MAX_BRIEFING_ARRAY_ITEMS]:
        step = parse_next_step(item)
        if step is None:
            return None
        parsed_steps.append(step)

    parsed_evidence: list[str] = []
    for item in evidence[:MAX_BRIEFING_EVIDENCE_ITEMS]:
        candidate: Any = item
        if isinstance(item, dict):
            candidate = next(
                (
                    item.get(key)
                    for key in ("summary", "label", "detail", "locator")
                    if isinstance(item.get(key), str)
                ),
                None,
            )
        evidence_text = _briefing_string(
            candidate, MAX_BRIEFING_TEXT_LENGTH
        )
        if evidence_text is not None:
            parsed_evidence.append(evidence_text)

    return ProjectBriefingRecord(
        project_id=project_id,
        project_path=project_path,
        name=name,
        assessment=assessment,
        summary=summary,
        confidence=confidence,
        evidence_at=evidence_at,
        completed_outputs=tuple(parsed_outputs),
        blockers=tuple(parsed_blockers),
        next_steps=tuple(parsed_steps),
        evidence=tuple(parsed_evidence),
        research_status=research_status,
    )


def parse_next_step(payload: Any) -> NextStepRecord | None:
    if not isinstance(payload, dict):
        return None
    step_id = _briefing_string(
        payload.get("id"), MAX_BRIEFING_IDENTIFIER_LENGTH
    )
    title = _briefing_string(payload.get("title"), MAX_BRIEFING_TEXT_LENGTH)
    detail = _briefing_string(
        payload.get("detail"), MAX_BRIEFING_TEXT_LENGTH, allow_empty=True
    )
    state = _briefing_string(payload.get("state"), 16)
    if (
        step_id is None
        or title is None
        or detail is None
        or state not in {"now", "next", "blocked", "done"}
        or payload.get("requiresApproval") is not True
    ):
        return None
    return NextStepRecord(
        id=step_id,
        title=title,
        detail=detail,
        state=state,
        requires_approval=True,
    )


def parse_rfc3339(value: Any) -> datetime | None:
    if (
        not isinstance(value, str)
        or len(value) > 64
        or RFC3339_PATTERN.fullmatch(value) is None
    ):
        return None
    candidate = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        instant = datetime.fromisoformat(candidate)
        normalized = instant.astimezone(timezone.utc)
        normalized.timestamp()
    except (OSError, OverflowError, ValueError):
        return None
    if instant.tzinfo is None or instant.utcoffset() is None:
        return None
    return normalized


def match_project_briefings(
    briefings: tuple[ProjectBriefingRecord, ...],
    projects: tuple[ProjectRecord, ...],
) -> tuple[ProjectBriefingRecord, ...]:
    known_paths = {
        normalized_project_path(project.directory)
        for project in projects
        if project.directory
    }
    matched: list[ProjectBriefingRecord] = []
    seen_paths: set[str] = set()
    for briefing in briefings:
        path_key = normalized_project_path(briefing.project_path)
        if path_key not in known_paths or path_key in seen_paths:
            continue
        seen_paths.add(path_key)
        matched.append(briefing)
    return tuple(matched)


def _briefing_string(
    value: Any,
    max_length: int,
    *,
    allow_empty: bool = False,
) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = sanitize_terminal_text(value)[:max_length]
    if not cleaned and not allow_empty:
        return None
    return cleaned


def find_vault_root(file_path: Path) -> Path:
    for parent in file_path.parents:
        if (parent / ".obsidian").is_dir():
            return parent
    return file_path.parent


def parse_markdown_projects(
    source: str, projects_file: Path
) -> tuple[tuple[str, str], ...]:
    lines = source.splitlines()
    header_index = -1
    project_column = -1
    code_column = -1
    for index, line in enumerate(lines):
        cells = markdown_table_cells(line)
        normalized = [cell.casefold() for cell in cells]
        if "project" in normalized and "code" in normalized:
            header_index = index
            project_column = normalized.index("project")
            code_column = normalized.index("code")
            break
    if header_index < 0:
        return ()

    base_directory = find_vault_root(projects_file)

    projects: list[tuple[str, str]] = []
    seen_paths: set[str] = set()
    required_columns = max(project_column, code_column)
    for line in lines[header_index + 1 :]:
        if not line.strip():
            if projects:
                break
            continue
        cells = markdown_table_cells(line)
        if len(cells) <= required_columns:
            if projects:
                break
            continue
        if is_markdown_separator(cells):
            continue
        name = markdown_cell_text(cells[project_column])
        code_path = markdown_cell_text(cells[code_column])
        if not name or not code_path:
            continue
        directory = Path(code_path).expanduser()
        if not directory.is_absolute():
            directory = base_directory / directory
        directory_text = os.path.normpath(str(directory))
        path_key = normalized_project_path(directory_text)
        if path_key in seen_paths:
            continue
        seen_paths.add(path_key)
        projects.append((clean_string(name), directory_text))
    return tuple(projects)


def markdown_table_cells(line: str) -> list[str]:
    stripped = line.strip()
    if "|" not in stripped:
        return []
    return [cell.strip() for cell in stripped.strip("|").split("|")]


def markdown_cell_text(cell: str) -> str:
    value = cell.strip()
    if len(value) >= 2 and value.startswith("`") and value.endswith("`"):
        value = value[1:-1].strip()
    if value.startswith("[[") and value.endswith("]]"):
        value = value[2:-2].split("|", 1)[-1].strip()
    return clean_string(value)


def is_markdown_separator(cells: list[str]) -> bool:
    return bool(cells) and all(
        "-" in cell and not set(cell).difference({"-", ":", " "})
        for cell in cells
    )


def normalized_project_path(directory: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(directory)))


def merge_project_catalog(
    discovered: dict[str, str],
    catalog: tuple[tuple[str, str], ...],
    registry: tuple[tuple[str, str], ...] = (),
) -> tuple[dict[str, str], dict[str, str]]:
    projects = dict(discovered)
    names: dict[str, str] = {}
    ids_by_path = {
        normalized_project_path(directory): project_id
        for project_id, directory in projects.items()
    }
    registry_paths = {normalized_project_path(directory) for _name, directory in registry}
    for name, directory in (*catalog, *registry):
        path_key = normalized_project_path(directory)
        project_id = ids_by_path.get(path_key)
        if project_id is None:
            project_id = f"markdown::{path_key}"
            projects[project_id] = directory
            ids_by_path[path_key] = project_id
        if path_key in registry_paths:
            names[project_id] = name
        else:
            names.setdefault(project_id, name)
    return projects, names


def validate_api_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        return "Invalid OpenCode API URL"
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return "OpenCode API URL must use HTTP or HTTPS"
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return "OpenCode API URL must not contain credentials, query, or fragment"
    if port is not None and not 1 <= port <= 65535:
        return "OpenCode API URL has an invalid port"
    return ""


def api_credentials_are_safe(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    return parsed.scheme == "https" or is_loopback_host(parsed.hostname or "")


def is_loopback_host(hostname: str) -> bool:
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


async def communicate_with_cleanup(
    process: asyncio.subprocess.Process,
    *,
    timeout: float,
) -> tuple[bytes, bytes] | None:
    try:
        return await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.CancelledError:
        await stop_process(process)
        raise
    except TimeoutError:
        await stop_process(process)
        return None


async def stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=1)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()


def classify_opencode_command(arguments: list[str]) -> tuple[bool, str]:
    if not arguments or Path(arguments[0]).name not in {
        "opencode",
        "opencode2",
        "opencode2.exe",
    }:
        return False, ""
    if any(argument in {"-h", "--help", "-v", "--version"} for argument in arguments[1:]):
        return False, ""
    if any(
        argument == "--completions" or argument.startswith("--completions=")
        for argument in arguments[1:]
    ):
        return False, ""

    positional = ""
    index = 1
    while index < len(arguments):
        argument = arguments[index]
        option = argument.partition("=")[0]
        if option in CLI_OPTIONS_WITH_VALUES:
            index += 1 if "=" in argument else 2
            continue
        if argument.startswith("-"):
            index += 1
            continue
        positional = argument
        break
    if positional in NON_TUI_SUBCOMMANDS:
        return False, ""
    if "--fork" in arguments:
        return True, ""

    session_id = ""
    for index, argument in enumerate(arguments[1:], start=1):
        if argument in {"-s", "--session"}:
            if index + 1 < len(arguments):
                session_id = arguments[index + 1]
            break
        if argument.startswith("--session="):
            session_id = argument.partition("=")[2]
            break
        if argument.startswith("-s="):
            session_id = argument.partition("=")[2]
            break

    return True, session_id if session_id.startswith("ses") else ""


def session_renderer_pids(
    session_id: str,
    proc_root: Path = Path("/proc"),
    *,
    backend: str | None = None,
) -> tuple[int, ...]:
    """Live OpenCode renderer PIDs for one session, in discovery order."""
    if not session_id:
        return ()
    return tuple(
        process.pid
        for process in read_opencode_processes(proc_root, backend=backend)
        if process.session_id == session_id
    )


def read_opencode_instances(
    proc_root: Path = Path("/proc"),
    *,
    backend: str | None = None,
) -> tuple[dict[str, int], int, dict[str, tuple[str, ...]]]:
    counts: dict[str, int] = {}
    unmapped = 0
    ttys: dict[str, list[str]] = {}
    for process in read_opencode_processes(proc_root, backend=backend):
        if process.session_id:
            counts[process.session_id] = counts.get(process.session_id, 0) + 1
            if process.tty:
                ttys.setdefault(process.session_id, []).append(process.tty)
        else:
            unmapped += 1
    return counts, unmapped, {key: tuple(sorted(value)) for key, value in ttys.items()}


def read_opencode_processes(
    proc_root: Path = Path("/proc"),
    *,
    backend: str | None = None,
) -> tuple[OpenCodeProcess, ...]:
    try:
        processes = tuple(proc_root.iterdir())
    except OSError:
        return ()

    uid = os.getuid()
    records: list[OpenCodeProcess] = []
    for process in processes:
        if not process.name.isdigit():
            continue
        record = _read_opencode_process(process, uid)
        if record is not None and (backend is None or record.backend == backend):
            records.append(record)
    return tuple(sorted(records, key=lambda item: item.pid))


def _read_opencode_process(process: Path, uid: int) -> OpenCodeProcess | None:
    try:
        if process.stat().st_uid != uid:
            return None
        comm = (process / "comm").read_text(errors="replace").strip()
        command = [
            os.fsdecode(argument)
            for argument in (process / "cmdline").read_bytes().split(b"\0")
            if argument
        ]
    except OSError:
        return None

    executable = Path(command[0]).name if command else ""
    if executable == "opencode":
        backend = "v1"
        if comm != "opencode":
            return None
    elif executable in {"opencode2", "opencode2.exe"}:
        backend = "v2"
        if comm not in {"opencode", "opencode2", "opencode2.exe"}:
            return None
    else:
        return None

    is_tui, session_id = classify_opencode_command(command)
    if not is_tui:
        return None
    state, start_time = read_process_identity(process)
    if state == "Z":
        return None
    return OpenCodeProcess(
        pid=int(process.name),
        session_id=session_id,
        tty=read_process_tty(process),
        start_time=start_time,
        backend=backend,
    )


def read_process_identity(process: Path) -> tuple[str, int]:
    """Return process state and stable start time from /proc/PID/stat."""
    try:
        content = (process / "stat").read_text(encoding="ascii", errors="replace")
        _, separator, remainder = content.rpartition(")")
        fields = remainder.split()
        if not separator or len(fields) < 20:
            return "", 0
        return fields[0], int(fields[19])
    except (OSError, ValueError):
        return "", 0


def read_process_tty(process: Path) -> str:
    try:
        target = os.readlink(process / "fd" / "0")
    except OSError:
        return ""
    return target if target.startswith("/dev/pts/") else ""


def read_live_opencode_panes(
    proc_root: Path = Path("/proc"),
    tmux_bin: str = "tmux",
    *,
    backend: str | None = None,
) -> tuple[LiveOpenCodePane, ...]:
    """Discover exact tmux panes that currently own an OpenCode TUI."""
    processes = tuple(
        item
        for item in read_opencode_processes(proc_root, backend=backend)
        if item.tty
    )
    if not processes:
        return ()
    by_tty: dict[str, list[OpenCodeProcess]] = {}
    for process in processes:
        by_tty.setdefault(process.tty, []).append(process)

    format_string = "\t".join(
        (
            "#{pane_id}",
            "#{pane_tty}",
            "#{session_name}",
            "#{window_index}",
            "#{pane_index}",
            "#{session_attached}",
            "#{window_active}",
            "#{pane_active}",
            "#{pane_dead}",
        )
    )
    try:
        result = subprocess.run(
            [tmux_bin, "list-panes", "-a", "-F", format_string],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ()
    if result.returncode != 0:
        return ()

    pane_rows: dict[str, tuple[str, ...] | None] = {}
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        fields = tuple(line.split("\t"))
        if len(fields) != 9 or not PANE_ID_PATTERN.fullmatch(fields[0]):
            continue
        pane_id, tty, *_rest, pane_dead = fields
        if not tty or tty not in by_tty or pane_dead not in {"", "0"}:
            continue
        previous = pane_rows.get(pane_id)
        if previous is not None and previous != fields:
            pane_rows[pane_id] = None
        elif pane_id not in pane_rows:
            pane_rows[pane_id] = fields

    uid = os.getuid()
    live: list[LiveOpenCodePane] = []
    for pane_id, fields in pane_rows.items():
        if fields is None:
            continue
        (
            _,
            tty,
            session_name,
            window_index,
            pane_index,
            session_attached,
            window_active,
            pane_active,
            _,
        ) = fields
        current: list[OpenCodeProcess] = []
        for candidate in by_tty[tty]:
            refreshed = _read_opencode_process(proc_root / str(candidate.pid), uid)
            if refreshed == candidate:
                current.append(candidate)
        session_ids = {item.session_id for item in current}
        backends = {item.backend for item in current}
        if not current or len(session_ids) != 1 or len(backends) != 1:
            continue
        session_id = next(iter(session_ids))
        process_backend = next(iter(backends))
        identity = "\0".join(
            [
                (
                    "ocdeck-destination-v1"
                    if process_backend == "v1"
                    else "ocdeck-destination-v2"
                ),
                pane_id,
                tty,
                *(f"{item.pid}:{item.start_time}:{item.session_id}" for item in current),
            ]
        )
        destination_id = "dst_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        if session_attached not in {"", "0"} and window_active not in {"", "0"}:
            terminal_state = (
                "foreground" if pane_active not in {"", "0"} else "visible"
            )
        else:
            terminal_state = "background"
        live.append(
            LiveOpenCodePane(
                destination_id=destination_id,
                pane_id=pane_id,
                session_id=session_id,
                session_name=session_name,
                window_index=window_index,
                pane_index=pane_index,
                terminal_state=terminal_state,
                backend=process_backend,
            )
        )
    order = {"foreground": 0, "visible": 1, "background": 2}
    return tuple(
        sorted(
            live,
            key=lambda item: (
                order.get(item.terminal_state, 3),
                item.session_name.casefold(),
                item.window_index,
                item.pane_index,
                item.pane_id,
            ),
        )
    )


def read_tmux_tty_sessions(
    ttys: dict[str, tuple[str, ...]],
) -> dict[str, tuple[str, ...]]:
    return read_tmux_tty_state(ttys)[0]


def read_tmux_tty_state(
    ttys: dict[str, tuple[str, ...]],
) -> tuple[dict[str, tuple[str, ...]], dict[str, bool]]:
    wanted = {tty for paths in ttys.values() for tty in paths}
    if not wanted:
        return {}, {}
    try:
        result = subprocess.run(
            [
                "tmux",
                "list-panes",
                "-a",
                "-F",
                "#{pane_tty}\t#{session_name}\t#{session_attached}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}, {}
    if result.returncode != 0:
        return {}, {}
    tty_state: dict[str, tuple[str, bool]] = {}
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        tty, _, remainder = line.partition("\t")
        name, _, attached = remainder.partition("\t")
        if tty and name:
            tty_state[tty] = (name, attached not in {"", "0"})
    mapped: dict[str, tuple[str, ...]] = {}
    attached_map: dict[str, bool] = {}
    for session_id, paths in ttys.items():
        states = [tty_state[tty] for tty in paths if tty in tty_state]
        names = tuple(sorted({name for name, _ in states}))
        if names:
            mapped[session_id] = names
            attached_map[session_id] = any(attached for _, attached in states)
    return mapped, attached_map


def read_system_metrics() -> SystemMetrics:
    try:
        load_1m = os.getloadavg()[0]
    except OSError:
        load_1m = 0.0

    memory_percent = 0.0
    try:
        memory: dict[str, int] = {}
        with open("/proc/meminfo", encoding="ascii") as handle:
            for line in handle:
                key, value = line.split(":", 1)
                memory[key] = int(value.strip().split()[0])
        total = memory.get("MemTotal", 0)
        available = memory.get("MemAvailable", 0)
        if total:
            memory_percent = (total - available) / total * 100
    except (OSError, ValueError):
        pass

    disk_percent = 0.0
    try:
        disk = shutil.disk_usage(Path.home())
        if disk.total:
            disk_percent = disk.used / disk.total * 100
    except OSError:
        pass

    uptime_seconds = 0
    try:
        with open("/proc/uptime", encoding="ascii") as handle:
            uptime_seconds = int(float(handle.read().split()[0]))
    except (OSError, ValueError, IndexError):
        pass

    return SystemMetrics(
        load_1m=load_1m,
        cpu_count=os.cpu_count() or 1,
        memory_percent=memory_percent,
        disk_percent=disk_percent,
        uptime_seconds=uptime_seconds,
    )
