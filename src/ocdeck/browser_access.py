"""Operator-only bridge to the shared browser grant; no model or prompt choices."""

from dataclasses import dataclass, replace
from functools import lru_cache
import os
from pathlib import Path
import runpy


@dataclass(frozen=True, slots=True)
class BrowserAccessResult:
    session_id: str = ""
    error: str = ""
    uncertain: bool = False


@lru_cache(maxsize=1)
def grant_helper():
    # The signed-in browser grant belongs to an optional Home Agent integration
    # (see home-agent/README.md). Point OCDECK_BROWSER_GRANT_HELPER at its helper
    # script; without one, browser-session actions report that it is unavailable.
    configured = os.environ.get("OCDECK_BROWSER_GRANT_HELPER", "").strip()
    if not configured:
        raise FileNotFoundError("No browser grant helper is configured (OCDECK_BROWSER_GRANT_HELPER)")
    return runpy.run_path(str(Path(configured).expanduser()))


def browser_access(source, directory: Path, session_id: str | None = None) -> BrowserAccessResult:
    """Use the selected dashboard's backend, catalog and credentials, never fallback."""
    if source.backend != "v1":
        return BrowserAccessResult(error="Browser grants are available on the V1 backend; this session was not changed.")
    if source.api_url_error:
        return BrowserAccessResult(error=source.api_url_error)
    try:
        helper = grant_helper()
    except (OSError, ImportError):
        return BrowserAccessResult(error="The browser grant helper is unavailable")
    ha = helper["ha"]
    try:
        settings = replace(
            ha.Settings.from_environment(), api_version=source.backend, api_url=source.api_url,
            catalog_file=source.projects_file, registry_file=source.project_registry_file,
            routes_file=source.session_routes_file, server_env=source.server_env_file,
        )
        # Resolve by exact catalog root, never a possibly duplicated display name.
        matches = [project for project in ha.parse_catalog(settings.catalog_file)
                   if project.path.resolve() == directory.resolve()]
        helper["require"](len(matches) <= 1, f"Multiple catalog entries match {directory}; resolve the duplicate registration first")
        api = ha.OpenCodeAPI(settings)
        api.username, api.password = source._username(), source._password()
        if not matches and session_id is None:
            # OC Deck also lists folders discovered from session history. Native
            # browser access does not make those folders managed catalog roots.
            result = helper["create_native"](settings, api, directory)
        else:
            helper["require"](len(matches) == 1,
                              f"Register the exact directory {directory} before adopting an existing session for scoped browser access")
            result = (helper["enable"](settings, api, matches[0].name, session_id, grant=True)
                      if session_id is not None else helper["create"](settings, api, matches[0].name))
        resolved_id = result.get("sessionID") or session_id or ""
        if result.get("inspectOnly"):
            return BrowserAccessResult(resolved_id, "Browser operation is unresolved; inspect the session before retrying", True)
        if result.get("status") not in {"granted", "granted-reconciled", "already-granted", "created", "created-reconciled"} or not resolved_id:
            return BrowserAccessResult(resolved_id, "Browser operation returned an unverified result", True)
        return BrowserAccessResult(resolved_id)
    except helper["GrantError"] as error:
        return BrowserAccessResult(session_id or "", str(error))
    except helper["INSPECTION_ERRORS"]:
        return BrowserAccessResult(session_id or "", "Browser access could not be verified; inspect before retrying", True)
