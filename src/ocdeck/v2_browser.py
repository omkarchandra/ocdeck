"""Native OpenCode 2 browser-session creation and operator permission grants."""

from dataclasses import replace
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from .browser_access import BrowserAccessResult, grant_helper

BROWSER_RULES = [
    {"action": "signed_in_tabs_browser_*", "resource": "*", "effect": "ask"},
    *[{"action": "signed_in_tabs_browser_" + name, "resource": "*", "effect": "deny"}
      for name in ("file_upload", "evaluate", "run_code_unsafe", "drop", "close", "shutdown", "install")],
]


def native_identifier(prefix="ses"):
    stamp = int(time.time() * 1000) * 4096 + 1
    stamp = (~stamp if prefix == "ses" else stamp) & ((1 << 48) - 1)
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    return f"{prefix}_{stamp:012x}" + "".join(secrets.choice(alphabet) for _ in range(14))


def scoped_metadata(source, directory):
    helper = grant_helper()
    ha = helper["ha"]
    settings = replace(ha.Settings.from_environment(), catalog_file=source.projects_file,
                       registry_file=source.project_registry_file, routes_file=source.session_routes_file,
                       guard_key_file=Path(os.environ.get("HOME_AGENT_GUARD_KEY_FILE", Path.home() / ".config/opencode-v2/home-agent/guard.key")))
    matches = [project for project in ha.parse_catalog(settings.catalog_file) if project.path.resolve() == directory]
    if len(matches) > 1:
        raise ValueError("Resolve the duplicate project registration before creating a browser session")
    note = ha.durable_note_path(settings, matches[0].note) if matches else ""
    return ha, settings, {"homeAgent": {"kind": "interactive-project", "projectPath": str(directory),
                                      "notePath": note, "browserEnabled": True}}


async def browser_access(source, directory, session_id=None):
    from .source import V2ApiError, V2Location, parse_v2_signed_in_tabs_status, is_loopback_host

    if source.api_url and not is_loopback_host(urlsplit(source.api_url).hostname or ""):
        return BrowserAccessResult(error="Operator browser grants require the managed local V2 server")
    directory = Path(directory).resolve()
    if not directory.is_dir():
        return BrowserAccessResult(error="The session directory is unavailable")
    location = V2Location(str(directory))
    resolved_id = session_id or native_identifier()
    write_attempted = False
    try:
        mcp = await source._v2_api_json("v2.mcp.list", location=location)
        if parse_v2_signed_in_tabs_status(mcp) != "connected":
            return BrowserAccessResult(error="The dedicated browser MCP is not connected")
        if session_id:
            before = (await source._v2_api_json("v2.session.get", params={"sessionID": session_id}))["data"]
            if before.get("parentID") or before.get("location", {}).get("directory") != str(directory):
                return BrowserAccessResult(session_id, "Select a primary session in its exact saved directory")
            home = (before.get("metadata") or {}).get("homeAgent", {})
            if home and not (home.get("kind") == "interactive-project" and home.get("browserEnabled") is True):
                return BrowserAccessResult(session_id, "This managed session's immutable policy does not admit an operator browser grant")
            active = await source._v2_api_json("v2.session.active")
            pending, complete = await source._v2_pending_requests()
            if not complete or session_id in active.get("data", {}) or pending.get(session_id):
                return BrowserAccessResult(session_id, "Wait for this session to become idle with no pending approval")
            permissions = list(before.get("permissions") or [])
            if permissions[-len(BROWSER_RULES):] != BROWSER_RULES:
                permissions.extend(BROWSER_RULES)
            write_attempted = True
            try:
                await source._v2_api_json("v2.session.update", params={"sessionID": session_id}, payload={"permissions": permissions})
            except V2ApiError:
                pass  # Reconcile by exact session and permission set before retry.
            after = (await source._v2_api_json("v2.session.get", params={"sessionID": session_id}))["data"]
            unchanged = all(after.get(key) == before.get(key) for key in ("id", "agent", "model", "metadata", "title", "location", "parentID"))
            if after.get("permissions") != permissions or not unchanged:
                return BrowserAccessResult(session_id, "Browser grant could not be reconciled; inspect before retrying", True)
        else:
            ha, settings, metadata = scoped_metadata(source, directory)
            metadata = ha.guarded_metadata(settings, resolved_id, metadata)
            body = {"id": resolved_id, "location": location.reference(), "metadata": metadata, "permissions": BROWSER_RULES}
            write_attempted = True
            try:
                await source._v2_api_json("v2.session.create", payload=body)
            except V2ApiError:
                pass
            after = (await source._v2_api_json("v2.session.get", params={"sessionID": resolved_id}))["data"]
            if (after.get("id") != resolved_id or after.get("location") != location.reference()
                    or after.get("metadata") != metadata or after.get("permissions") != BROWSER_RULES):
                return BrowserAccessResult(resolved_id, "Browser session creation is unresolved; inspect before retrying", True)
        return BrowserAccessResult(resolved_id)
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        return BrowserAccessResult(resolved_id if write_attempted else session_id or "",
                                   f"Browser access could not be verified: {error}", write_attempted)
