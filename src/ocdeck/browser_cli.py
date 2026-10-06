"""Start an interactive signed-in-browser session from any project terminal."""

import argparse
import asyncio
import os
from pathlib import Path
import re
import sys
import urllib.error
from urllib.parse import quote, urlencode, urlsplit

from .browser_access import grant_helper
from .browser_session import main as attach_session
from .backend import read_saved_backend
from .source import DashboardSource, api_credentials_are_safe, is_loopback_host, validate_api_url


def project_directory(source, directory):
    """A nested working directory belongs to the nearest registered code root."""
    requested = Path(directory).expanduser().resolve()
    if not requested.is_dir():
        raise ValueError("The requested project directory is unavailable")
    helper = grant_helper()
    projects = helper["ha"].parse_catalog(source.projects_file)
    candidates = [project.path.resolve() for project in projects
                  if requested.is_relative_to(project.path.resolve())]
    if not candidates:
        raise ValueError("Register this project with OC Deck or new-project before using oc_agent_web")
    depth = max(len(path.parts) for path in candidates)
    nearest = [path for path in candidates if len(path.parts) == depth]
    if len(nearest) != 1:
        raise ValueError("The project catalog has conflicting roots; resolve them before starting a session")
    return nearest[0]


def session_info(source, session_id):
    if source.api_url_error:
        raise ValueError(source.api_url_error)
    if getattr(source, "backend", "v1") == "v2":
        try:
            result = asyncio.run(source._v2_api_json("v2.session.get", params={"sessionID": session_id}))
            info = result["data"]
            directory = info["location"]["directory"]
            if info["id"] != session_id or not Path(directory).is_absolute() or not Path(directory).is_dir():
                raise ValueError("The selected V2 session directory is unavailable")
            return {**info, "directory": directory}
        except (KeyError, RuntimeError) as error:
            raise ValueError("Could not load the selected V2 session") from error
    password = source._password()
    if password and not api_credentials_are_safe(source.api_url):
        raise ValueError("Use HTTPS or a loopback OpenCode API URL")
    try:
        info = source._request_json(f"/session/{quote(session_id, safe='')}", password)
    except urllib.error.HTTPError as error:
        raise ValueError(f"Could not load the selected OpenCode session (HTTP {error.code})") from None
    except (OSError, ValueError, TimeoutError):
        raise ValueError("Could not load the selected session from the OpenCode API") from None
    if not isinstance(info, dict) or info.get("id") != session_id:
        raise ValueError("The OpenCode API returned a different or invalid session")
    directory = info.get("directory")
    if not isinstance(directory, str) or not Path(directory).is_absolute() or not Path(directory).is_dir():
        raise ValueError("The selected session's saved directory is unavailable")
    return info


def browser_url(value):
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise argparse.ArgumentTypeError("provide a valid browser page URL") from None
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or any(ord(char) < 33 for char in value)
            or (port is not None and not 1 <= port <= 65535)):
        raise argparse.ArgumentTypeError("browser page URL must be HTTP(S), without credentials or whitespace")
    return value


def record_browser_target(source, directory, session_id, url):
    """Record the explicit page for the agent without running a model turn."""
    info = session_info(source, session_id)
    if getattr(source, "backend", "v1") == "v2":
        from .v2_browser import native_identifier
        identifier = native_identifier("msg")
        result = asyncio.run(source._v2_api_json("v2.session.synthetic", params={"sessionID": session_id}, payload={
            "id": identifier, "text": f"Browser target selected with oc_agent_web: {url}\nUse signed_in_tabs when I give you a task.",
            "resume": False,
        }))
        if not isinstance(result, dict) or result.get("data", {}).get("id") != identifier:
            raise ValueError("Could not verify recording the browser target; inspect before retrying")
        return
    model = info.get("model") or {}
    body = {"agent": info.get("agent") or "build", "noReply": True,
            "parts": [{"type": "text", "text": (
                f"Browser target selected with oc_agent_web: {url}\n"
                "Use the signed_in_tabs browser tools for this page when I give you a task. "
                "This records the target only; no browser action or model response is requested yet.")}]}
    model_id = model.get("modelID", model.get("id"))
    if model.get("providerID") and model_id:
        body["model"] = {"providerID": model["providerID"], "modelID": model_id}
    variant = info.get("variant", model.get("variant"))
    if variant:
        body["variant"] = variant
    path = f"/session/{quote(session_id, safe='')}/message?{urlencode({'directory': str(directory)})}"
    try:
        result = source._request_json(path, source._password(), method="POST", payload=body)
    except (OSError, ValueError, TimeoutError):
        raise ValueError("Could not verify recording the browser target; inspect this session before retrying") from None
    if not isinstance(result, dict) or result.get("info", {}).get("role") != "user":
        raise ValueError("Could not verify recording the browser target; inspect this session before retrying")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="oc_agent_web", description=__doc__,
        epilog="Uses OC Deck's selected backend. Choose/change the model inside OpenCode with /models.",
    )
    parser.add_argument("directory", nargs="?", help="project directory (default: saved session directory, or current directory for a new session)")
    def session_id(value):
        if not re.fullmatch(r"ses_[A-Za-z0-9_-]+", value):
            raise argparse.ArgumentTypeError("provide an exact ses_ session ID")
        return value

    parser.add_argument("--session", type=session_id, help="grant browser access to and open this existing idle session")
    parser.add_argument("--url", type=browser_url, help="browser page to record for the agent; does not start a model turn")
    parser.add_argument("--api-url", help="explicit OpenCode API server (default: selected local backend)")
    parser.add_argument("--backend", choices=("v1", "v2"), help="override OC Deck's saved backend")
    args = parser.parse_args(argv)
    api_url, page_url = args.api_url, args.url
    # Preserve the old local-backend spelling, but never send API credentials
    # to a webpage supplied as --url (e.g. a ChatGPT conversation).
    if page_url and not api_url:
        parsed = urlsplit(page_url)
        if is_loopback_host(parsed.hostname or "") and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment:
            api_url, page_url = page_url, None
    if api_url and (error := validate_api_url(api_url)):
        parser.error(error)
    try:
        backend = args.backend or read_saved_backend() or "v2"
        source = DashboardSource(backend=backend, api_url=api_url)
        if not source.opencode_bin:
            print("The selected OpenCode executable is unavailable", file=sys.stderr)
            return 1
        if args.session is not None:
            info = session_info(source, args.session)
            directory = Path(info["directory"]).resolve()
            if args.directory is not None and project_directory(source, args.directory) != directory:
                raise ValueError(f"This session belongs to {directory}; omit the directory argument to use it automatically")
        else:
            directory = project_directory(source, args.directory or ".")
        result = asyncio.run(
            source.enable_session_browser(directory, args.session)
            if args.session is not None else source.create_browser_session(directory)
        )
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print("Could not prepare browser access; inspect the project before retrying", file=sys.stderr)
        return 1
    if result.error:
        print(result.error, file=sys.stderr)
        if result.session_id:
            print(f"Session retained: {result.session_id}", file=sys.stderr)
        return 2 if result.uncertain else 1
    if page_url:
        try:
            record_browser_target(source, directory, result.session_id, page_url)
        except ValueError as error:
            print(str(error), file=sys.stderr)
            print(f"Session retained: {result.session_id}", file=sys.stderr)
            return 2
        print(f"Browser target recorded: {page_url}", flush=True)
    print(f"Browser session {result.session_id}. Choose your model with /models.", flush=True)
    if getattr(source, "backend", "v1") == "v2":
        command = [source.opencode_bin]
        if source.api_url:
            command.extend(("--server", source.api_url))
        command.extend((str(directory), "--session", result.session_id))
        environment = {key: value for key, value in os.environ.items()
                       if key not in {"OPENCODE_URL", "OPENCODE_SERVER_USERNAME", "OPENCODE_SERVER_PASSWORD"}}
        os.execvpe(source.opencode_bin, command, environment)
        return 0
    return attach_session([
        "--opencode", source.opencode_bin, "--url", source.api_url,
        "--directory", str(directory), "--session", result.session_id,
        "--server-env", str(source.server_env_file),
    ])


if __name__ == "__main__":
    raise SystemExit(main())
