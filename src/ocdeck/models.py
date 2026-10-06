from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True, slots=True)
class SessionRecord:
    id: str
    title: str
    directory: str
    project_id: str
    created_ms: int
    updated_ms: int
    parent_id: str = ""
    agent_parent_id: str = ""
    last_interaction_ms: int = 0
    status: str = "idle"
    instance_count: int = 0
    terminals: tuple[str, ...] = ()
    terminal_attached: bool = False
    permission: str = ""
    question: str = ""
    last_prompt: str = ""
    assistant_active: bool = False
    assistant_activity_ms: int = 0
    assistant_done_ms: int = 0
    permission_id: str = ""
    permission_resources: tuple[str, ...] = ()
    question_id: str = ""
    home_agent: str = ""
    agent: str = ""
    model: str = ""
    workspace_id: str = ""
    automation_kind: str = ""
    browser_enabled: bool = False
    # Which agent CLI owns the session: "opencode", "claude" or "codex".
    harness: str = "opencode"
    # How the CLI was launched, when the transcript records it: a Claude
    # transcript "entrypoint" ("cli"/"sdk-cli") or a Codex rollout "source"
    # ("cli"/"vscode"/"exec"/...). OpenCode storage records no launch mode.
    launch_source: str = ""
    # Running background shell commands associated with this session.
    background_jobs: tuple[str, ...] = ()

    @property
    def agent_session_kind(self) -> str:
        if any(
            parent and parent != self.id
            for parent in (self.parent_id, self.agent_parent_id)
        ):
            return "Subagent"
        return self.automation_kind


@dataclass(frozen=True, slots=True)
class ProjectRecord:
    id: str
    directory: str
    name: str
    session_count: int = 0
    active_count: int = 0
    attached_count: int = 0
    instance_count: int = 0
    updated_ms: int = 0
    registered: bool = False
    git_branch: str = ""
    git_dirty: int = -1


@dataclass(frozen=True, slots=True)
class NextStepRecord:
    id: str
    title: str
    detail: str
    state: str
    requires_approval: bool = True


@dataclass(frozen=True, slots=True)
class ProjectBriefingRecord:
    project_id: str
    project_path: str
    name: str
    assessment: str
    summary: str
    confidence: str
    evidence_at: datetime | None
    completed_outputs: tuple[tuple[str, str], ...] = ()
    blockers: tuple[str, ...] = ()
    next_steps: tuple[NextStepRecord, ...] = ()
    evidence: tuple[str, ...] = ()
    research_status: str = "completed"


@dataclass(frozen=True, slots=True)
class BriefingReportRecord:
    report_id: str
    generated_at: datetime
    status: str
    projects: tuple[ProjectBriefingRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class ServiceRecord:
    unit: str
    label: str
    role: str
    state: str


@dataclass(frozen=True, slots=True)
class NamedAgentStatus:
    name: str
    role: str
    model: str = ""
    description: str = ""
    configured: bool = False
    loaded: bool = False
    state: str = "unavailable"
    session_count: int = 0
    detail: str = ""


@dataclass(frozen=True, slots=True)
class SystemMetrics:
    load_1m: float = 0.0
    cpu_count: int = 1
    memory_percent: float = 0.0
    disk_percent: float = 0.0
    uptime_seconds: int = 0


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    sessions: tuple[SessionRecord, ...] = ()
    projects: tuple[ProjectRecord, ...] = ()
    services: tuple[ServiceRecord, ...] = ()
    metrics: SystemMetrics = field(default_factory=SystemMetrics)
    connection: str = "offline"
    connection_detail: str = "OpenCode API unavailable"
    unmapped_instance_count: int = 0
    collected_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    warning: str = ""
    briefings: tuple[ProjectBriefingRecord, ...] = ()
    briefing_report_id: str = ""
    briefing_generated_at: datetime | None = None
    briefing_status: str = ""
    named_agents: tuple[NamedAgentStatus, ...] = ()
    named_agents_stale: bool = False
    named_agents_error: str = ""
    signed_in_tabs_status: str = "unknown"
    signed_in_tabs_stale: bool = True
    signed_in_tabs_error: str = ""

    @property
    def mapped_instance_count(self) -> int:
        return sum(session.instance_count for session in self.sessions)

    @property
    def terminal_instance_count(self) -> int:
        return self.mapped_instance_count + self.unmapped_instance_count

    @property
    def attached_session_count(self) -> int:
        return sum(session.instance_count > 0 for session in self.sessions)


def parse_sessions(
    payload: Any,
    statuses: dict[str, str] | None = None,
    instance_counts: dict[str, int] | None = None,
    terminals: dict[str, tuple[str, ...]] | None = None,
    permissions: dict[str, list[dict[str, Any]]] | None = None,
    terminal_attached: dict[str, bool] | None = None,
    last_interactions: dict[str, int] | None = None,
    turn_activity: dict[str, tuple[bool, int, int]] | None = None,
    latest_prompts: dict[str, str] | None = None,
    agent_parent_ids: dict[str, str] | None = None,
    home_agent_evidence: dict[str, str] | None = None,
    background_jobs: dict[str, tuple[str, ...]] | None = None,
) -> tuple[SessionRecord, ...]:
    if not isinstance(payload, list):
        return ()

    status_map = statuses or {}
    instance_map = instance_counts or {}
    terminal_map = terminals or {}
    permission_map = permissions or {}
    attached_map = terminal_attached or {}
    interaction_map = last_interactions or {}
    turn_map = turn_activity or {}
    prompt_map = latest_prompts or {}
    agent_parent_map = agent_parent_ids or {}
    home_agent_map = home_agent_evidence or {}
    background_jobs_map = background_jobs or {}
    sessions: list[SessionRecord] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        session_id = clean_string(item.get("id"))
        directory = clean_string(item.get("directory"))
        if not session_id or not directory:
            continue
        requests = permission_map.get(session_id) or ()
        permission_label = ""
        question_label = ""
        permission_id = ""
        permission_resources: tuple[str, ...] = ()
        question_id = ""
        for request in requests:
            request_type = clean_string(request.get("permission"))
            pattern = clean_string(request.get("pattern"))
            if request_type == "question":
                question_label = question_label or pattern or "Input required"
                question_id = question_id or clean_string(request.get("id"))
            elif not permission_label:
                resources = request.get("resources")
                if isinstance(resources, (list, tuple)) and all(
                    isinstance(resource, str) for resource in resources
                ):
                    permission_resources = tuple(
                        clean_string(resource) for resource in resources
                    )
                elif pattern:
                    permission_resources = (pattern,)
                resource_label = "; ".join(
                    resource for resource in permission_resources if resource
                )
                permission_label = f"{request_type} {resource_label or pattern}".strip()
                permission_id = clean_string(request.get("id"))
        turn_active, assistant_done_ms, assistant_activity_ms = turn_map.get(
            session_id, (False, 0, 0)
        )
        title = clean_string(item.get("title")) or "Untitled session"
        home_agent = home_agent_for_session(
            item,
            title,
            home_agent_map.get(session_id),
            session_id in home_agent_map,
        )
        metadata = item.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        home_metadata = metadata.get("homeAgent")
        home_metadata = home_metadata if isinstance(home_metadata, dict) else {}
        automation_kind = {
            "project-worker": "Worker",
            "portfolio-research": "Reporter",
        }.get(clean_string(home_metadata.get("kind")), "")
        migrated_home = metadata.get("ocdeckMigration", {}).get("originalHomeAgent", {}) if isinstance(metadata.get("ocdeckMigration"), dict) else {}
        if (home_metadata.get("kind") == "interactive-project" and isinstance(migrated_home, dict)
                and migrated_home.get("source") != "operator-browser-grant"):
            automation_kind = {"project-worker": "Worker", "portfolio-research": "Reporter"}.get(clean_string(migrated_home.get("kind")), automation_kind)
        browser_enabled = (
            clean_string(home_metadata.get("kind")) in {"project-worker", "interactive-project"}
            and home_metadata.get("browserEnabled") is True
            and not item.get("parentID") and not agent_parent_map.get(session_id)
            and home_metadata.get("delegatedBy") is None
        )
        if browser_enabled and home_metadata.get("source") == "operator-browser-grant":
            # A user's browser grant does not turn their interactive session into
            # an automated worker hidden by the default Main sessions filter.
            automation_kind = ""
        native_browser = metadata.get("ocdeckBrowser")
        native_rules = item.get("permissions")
        if isinstance(native_rules, list) and not home_metadata and not item.get("parentID"):
            from fnmatch import fnmatchcase
            decisions = [rule.get("effect") for rule in native_rules if isinstance(rule, dict)
                         and isinstance(rule.get("action"), str)
                         and rule.get("resource") == "*"
                         and fnmatchcase("signed_in_tabs_browser_tabs", rule["action"])]
            browser_enabled = bool(decisions and clean_string(decisions[-1]) in {"allow", "ask"})
        if (not home_metadata and isinstance(native_browser, dict)
                and native_browser.get("version") == 1
                and native_browser.get("source") == "operator-native-browser"
                and native_browser.get("directory") == item.get("directory")
                and not item.get("parentID") and not agent_parent_map.get(session_id)):
            browser_enabled = True
        if (
            home_metadata.get("kind") == "orchestrator"
            and home_metadata.get("role") == "monitor"
        ) or (
            metadata.get("managedBy") == "home_agent.py"
            and metadata.get("role") in ("home_agent_monitor", "home_agent")
        ):
            automation_kind = "Monitor"
        sessions.append(
            SessionRecord(
                id=session_id,
                title=title,
                directory=directory,
                project_id=clean_string(item.get("projectId")) or "unknown",
                created_ms=clean_int(item.get("created")),
                updated_ms=clean_int(item.get("updated")),
                parent_id=clean_string(item.get("parentID")),
                agent_parent_id=clean_string(agent_parent_map.get(session_id)),
                last_interaction_ms=clean_int(interaction_map.get(session_id, 0)),
                status=normalize_status(status_map.get(session_id, "idle")),
                instance_count=clean_int(instance_map.get(session_id, 0)),
                terminals=tuple(terminal_map.get(session_id, ())),
                terminal_attached=bool(attached_map.get(session_id, False)),
                permission=permission_label,
                question=question_label,
                last_prompt=clean_string(prompt_map.get(session_id, ""))[
                    :MAX_LAST_PROMPT_LENGTH
                ],
                assistant_active=bool(turn_active),
                assistant_activity_ms=clean_int(assistant_activity_ms),
                assistant_done_ms=clean_int(assistant_done_ms),
                permission_id=permission_id,
                permission_resources=permission_resources,
                question_id=question_id,
                home_agent=home_agent,
                agent=clean_string(item.get("agent")),
                model=format_model_ref(item.get("model")),
                workspace_id=clean_string(item.get("workspaceID")),
                automation_kind=automation_kind,
                browser_enabled=browser_enabled,
                background_jobs=background_jobs_map.get(session_id, ()),
            )
        )

    return tuple(sorted(sessions, key=lambda item: item.updated_ms, reverse=True))


EXPECTED_HOME_AGENT_ROLES = (
    ("home_agent", "Project orchestration"),
    ("maverik", "Voice and project orchestration"),
    ("jasmine", "Coding and research"),
)
EXPECTED_HOME_AGENT_NAMES = frozenset(name for name, _role in EXPECTED_HOME_AGENT_ROLES)
HOME_AGENT_NAME_ALIASES = {"jarvis": "maverik"}
HOME_AGENT_TITLE_ALIASES = {
    "home agent": "home_agent",
    "home agent monitor": "home_agent",
    "maverik": "maverik",
    "maverik voice": "maverik",
    "jarvis": "maverik",
    "jarvis voice": "maverik",
    "jasmine": "jasmine",
    "jasmine voice": "jasmine",
}


def _with_named_agent_aliases(values: dict[str, Any]) -> dict[str, Any]:
    result = dict(values)
    for legacy, canonical in HOME_AGENT_NAME_ALIASES.items():
        if legacy in result and canonical not in result:
            result[canonical] = result[legacy]
    return result


def expected_named_agents(
    configured: dict[str, bool] | None = None,
) -> tuple[NamedAgentStatus, ...]:
    configured_map = _with_named_agent_aliases(configured or {})
    return tuple(
        NamedAgentStatus(
            name=name,
            role=role,
            configured=bool(configured_map.get(name)),
        )
        for name, role in EXPECTED_HOME_AGENT_ROLES
    )


def parse_named_agent_registry(
    payload: Any,
    configured: dict[str, bool] | None = None,
) -> tuple[NamedAgentStatus, ...] | None:
    if not isinstance(payload, list):
        return None
    by_name = {
        clean_string(item.get("name")): item
        for item in payload
        if isinstance(item, dict) and clean_string(item.get("name"))
    }
    by_name = _with_named_agent_aliases(by_name)
    configured_map = _with_named_agent_aliases(configured or {})
    statuses: list[NamedAgentStatus] = []
    for name, role in EXPECTED_HOME_AGENT_ROLES:
        item = by_name.get(name)
        model = item.get("model") if isinstance(item, dict) else None
        provider = clean_string(model.get("providerID")) if isinstance(model, dict) else ""
        model_id = clean_string(model.get("modelID")) if isinstance(model, dict) else ""
        model_label = "/".join(value for value in (provider, model_id) if value)[:160]
        description = (
            clean_string(item.get("description"))[:300]
            if isinstance(item, dict)
            else ""
        )
        loaded = item is not None
        statuses.append(
            NamedAgentStatus(
                name=name,
                role=role,
                model=model_label,
                description=description,
                configured=bool(configured_map.get(name)),
                loaded=loaded,
                state="ready" if loaded else "unavailable",
                detail="Loaded; no open session" if loaded else "Missing from /agent",
            )
        )
    return tuple(statuses)


def home_agent_for_session(
    payload: dict[str, Any],
    title: str,
    evidence: str | None,
    evidence_present: bool,
) -> str:
    metadata = payload.get("metadata")
    home_metadata = metadata.get("homeAgent") if isinstance(metadata, dict) else None
    metadata_agent_present = (
        isinstance(home_metadata, dict) and "agent" in home_metadata
    )
    if metadata_agent_present:
        candidate = clean_string(home_metadata.get("agent"))
        has_authoritative_evidence = True
    elif evidence_present:
        candidate = clean_string(evidence)
        has_authoritative_evidence = True
    else:
        candidate = clean_string(payload.get("agent"))
        has_authoritative_evidence = bool(candidate)
    normalized = candidate.casefold()
    normalized = HOME_AGENT_NAME_ALIASES.get(normalized, normalized)
    if normalized in EXPECTED_HOME_AGENT_NAMES:
        return normalized
    if has_authoritative_evidence:
        return ""
    normalized_title = " ".join(title.casefold().replace("_", " ").split())
    return HOME_AGENT_TITLE_ALIASES.get(normalized_title, "")


def summarize_named_agents(
    registry: tuple[NamedAgentStatus, ...],
    sessions: tuple[SessionRecord, ...],
) -> tuple[NamedAgentStatus, ...]:
    summarized: list[NamedAgentStatus] = []
    for registered in registry:
        matches = []
        states = []
        for session in sessions:
            if HOME_AGENT_NAME_ALIASES.get(session.home_agent, session.home_agent) != (
                HOME_AGENT_NAME_ALIASES.get(registered.name, registered.name)
            ):
                continue
            state = agent_state(session)
            if session.instance_count <= 0 and state in {"idle", "open", "review"}:
                continue
            matches.append(session)
            states.append(state)

        if not (registered.configured or registered.loaded or matches):
            continue  # never set up here, e.g. no Home Agent integration installed
        if not registered.loaded:
            state = "unavailable"
            detail = "Missing from /agent"
        elif any(state in {"permission", "question"} for state in states):
            state = "attention"
            detail = "Permission or question pending"
        elif any(state in {"busy", "retry", "stalled", "job"} for state in states):
            state = "active"
            detail = "Running session"
        elif matches:
            state = "idle"
            detail = "Open inactive session"
        else:
            state = "ready"
            detail = "Loaded; no open session"
        if len(matches) > 1:
            detail = f"{len(matches)} open sessions; {detail.lower()}"
        if not registered.configured:
            detail += "; agent file missing"
        summarized.append(
            replace(
                registered,
                state=state,
                session_count=len(matches),
                detail=detail,
            )
        )
    return tuple(summarized)


def parse_known_projects(payload: Any) -> dict[str, str]:
    if not isinstance(payload, list):
        return {}
    projects: dict[str, str] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        project_id = clean_string(item.get("id"))
        if not project_id or project_id == "global":
            continue
        worktree = clean_string(item.get("worktree"))
        directory = worktree if worktree and Path(worktree).is_dir() else ""
        if not directory:
            sandboxes = item.get("sandboxes")
            if isinstance(sandboxes, list):
                for sandbox in sandboxes:
                    candidate = clean_string(sandbox)
                    if candidate and Path(candidate).is_dir():
                        directory = candidate
                        break
        chosen = directory or worktree
        if chosen:
            projects[project_id] = chosen
    return projects


def assign_project_roots(
    sessions: Iterable[SessionRecord],
    known_projects: dict[str, str] | None = None,
) -> tuple[SessionRecord, ...]:
    roots = [
        (directory.rstrip("/"), project_id)
        for project_id, directory in (known_projects or {}).items()
        if directory
    ]
    live_projects = {
        project_id: directory
        for project_id, directory in (known_projects or {}).items()
        if directory and Path(directory).is_dir()
    }
    resolved: list[SessionRecord] = []
    for session in sessions:
        directory = session.directory.rstrip("/")
        stale = bool(directory) and not Path(session.directory).is_dir()
        match: tuple[int, str] | None = None
        for root, project_id in roots:
            if directory == root or (
                not stale and directory.startswith(root + "/")
            ):
                if match is None or len(root) > match[0]:
                    match = (len(root), project_id)
        if match is not None:
            project_id = match[1]
        elif stale and session.project_id in live_projects:
            project_id = session.project_id
        else:
            project_id = f"dir::{directory or session.project_id}"
        if project_id != session.project_id:
            session = replace(session, project_id=project_id)
        resolved.append(session)
    return tuple(resolved)


def apply_session_routes(
    sessions: Iterable[SessionRecord],
    routes: dict[str, str],
    project_names: dict[str, str],
) -> tuple[SessionRecord, ...]:
    project_ids_by_name = {
        name.casefold(): project_id for project_id, name in project_names.items()
    }
    sessions_by_id = {session.id: session for session in sessions}
    resolved: dict[str, str] = {}

    def resolve_project(session: SessionRecord, resolving: set[str]) -> str:
        if session.id in resolved:
            return resolved[session.id]
        explicit = project_ids_by_name.get(routes.get(session.id, "").casefold())
        if explicit:
            resolved[session.id] = explicit
            return explicit
        if session.parent_id and session.id not in resolving:
            parent = sessions_by_id.get(session.parent_id)
            if parent is not None:
                project_id = resolve_project(parent, resolving | {session.id})
                resolved[session.id] = project_id
                return project_id
        resolved[session.id] = session.project_id
        return session.project_id

    routed: list[SessionRecord] = []
    for session in sessions_by_id.values():
        project_id = resolve_project(session, set())
        routed.append(
            replace(session, project_id=project_id)
            if project_id != session.project_id
            else session
        )
    return tuple(routed)


def build_projects(
    sessions: Iterable[SessionRecord],
    known_projects: dict[str, str] | None = None,
    project_names: dict[str, str] | None = None,
) -> tuple[ProjectRecord, ...]:
    grouped: dict[str, list[SessionRecord]] = {}
    for session in sessions:
        grouped.setdefault(session.project_id, []).append(session)

    for project_id in (known_projects or {}):
        if project_names is None or project_id in project_names:
            grouped.setdefault(project_id, [])

    projects: list[ProjectRecord] = []
    for project_id, project_sessions in grouped.items():
        directory = (
            (known_projects or {}).get(project_id, "")
            or (project_sessions[0].directory if project_sessions else "")
        )
        projects.append(
            ProjectRecord(
                id=project_id,
                directory=directory,
                name=(project_names or {}).get(project_id) or project_name(directory),
                session_count=len(project_sessions),
                active_count=sum(
                    session.status in {"busy", "retry"} or bool(session.background_jobs)
                    for session in project_sessions
                ),
                attached_count=sum(
                    session.instance_count > 0 for session in project_sessions
                ),
                instance_count=sum(
                    session.instance_count for session in project_sessions
                ),
                updated_ms=max(
                    (session_age_ms(session) for session in project_sessions),
                    default=0,
                ),
                registered=project_id in (project_names or {}),
            )
        )

    return tuple(
        sorted(
            projects,
            key=lambda item: (item.updated_ms, item.name.casefold()),
            reverse=True,
        )
    )


def normalize_status(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("type")
    status = clean_string(value).lower()
    return status if status in {"busy", "retry", "idle"} else "idle"


REVIEW_WINDOW_MS = 15 * 60 * 1000
ACTIVE_TURN_WINDOW_MS = 15 * 60 * 1000
MAX_LAST_PROMPT_LENGTH = 200


def agent_state(session: SessionRecord, now_ms: int | None = None) -> str:
    if session.permission:
        return "permission"
    if session.question:
        return "question"
    if session.status == "busy":
        return "busy"
    if session.status == "retry":
        return "retry"
    if session.background_jobs:
        # The turn finished (idle, window possibly closed) but a background
        # shell command from this session is still running.
        return "job"
    if session.instance_count > 0:
        current = (
            now_ms
            if now_ms is not None
            else int(datetime.now(timezone.utc).timestamp() * 1000)
        )
        if session.assistant_active:
            assistant_follows_prompt = (
                session.last_interaction_ms <= 0
                or session.assistant_activity_ms <= 0
                or session.assistant_activity_ms >= session.last_interaction_ms
            )
            if (
                assistant_follows_prompt
                and session.assistant_activity_ms > 0
                and current - session.assistant_activity_ms
                <= ACTIVE_TURN_WINDOW_MS
            ):
                return "busy"
            if not assistant_follows_prompt:
                return "open"
            return "stalled"
        completed_after_prompt = (
            session.assistant_done_ms > 0
            and session.last_interaction_ms > 0
            and session.assistant_done_ms >= session.last_interaction_ms
        )
        recent = (
            current - session.assistant_done_ms <= REVIEW_WINDOW_MS
            if session.assistant_done_ms > 0
            else False
        )
        if completed_after_prompt and recent:
            return "review"
        return "open"
    return "idle"


def clean_string(value: Any) -> str:
    return sanitize_terminal_text(value) if isinstance(value, str) else ""


def format_model_ref(value: Any) -> str:
    if not isinstance(value, dict):
        return clean_string(value)
    provider = clean_string(value.get("providerID"))
    model = clean_string(value.get("modelID")) or clean_string(value.get("id"))
    variant = clean_string(value.get("variant"))
    label = "/".join(item for item in (provider, model) if item)
    return f"{label}#{variant}" if label and variant else label


def sanitize_terminal_text(value: str) -> str:
    cleaned: list[str] = []
    for character in value:
        category = unicodedata.category(character)
        if category in {"Cc", "Cf", "Cs"}:
            if character in {"\t", "\n", "\r"}:
                cleaned.append(" ")
            continue
        cleaned.append(character)
    return " ".join("".join(cleaned).split())


def clean_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        try:
            return max(0, int(value))
        except (ValueError, OverflowError):
            return 0
    return 0


def project_name(directory: str) -> str:
    if not directory:
        return "Unknown project"
    path = Path(directory)
    return path.name or str(path)


def relative_time(epoch_ms: int, now: datetime | None = None) -> str:
    if epoch_ms <= 0:
        return "unknown"
    current = now or datetime.now(timezone.utc)
    try:
        instant = datetime.fromtimestamp(epoch_ms / 1000, timezone.utc)
    except (ValueError, OverflowError, OSError):
        return "unknown"
    seconds = max(0, int((current - instant).total_seconds()))
    if seconds < 60:
        return "now"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    if seconds < 604800:
        return f"{seconds // 86400}d"
    return instant.astimezone().strftime("%b %d")


def session_age_ms(session: SessionRecord) -> int:
    """Choose the best available timestamp for a session's displayed age."""
    return session.updated_ms or session.last_interaction_ms or session.created_ms


def format_uptime(seconds: int) -> str:
    if seconds < 3600:
        return f"{max(0, seconds) // 60}m"
    hours = seconds // 3600
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def compact_path(path: str, private: bool = False) -> str:
    if private:
        return "[hidden]"
    home = str(Path.home())
    return "~" + path[len(home) :] if path == home or path.startswith(home + "/") else path
