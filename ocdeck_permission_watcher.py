#!/usr/bin/env python3
"""Deliver one desktop notification per pending OpenCode request globally."""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ocdeck.v2_read_api import ReadAPIError, read_api, use_read_api
from ocdeck.source import (
    MAX_API_RESPONSE_BYTES,
    NoRedirectHandler,
    V2_LOCATION_OPERATIONS,
    V2Location,
    api_credentials_are_safe,
    merge_permissions,
    parse_v2_forms,
    parse_v2_locations,
    parse_v2_permissions,
    read_local_permissions,
    v2_error_message,
    v2_location_envelope_is_valid,
    validate_api_url,
)

GDBUS = "/usr/bin/gdbus"
NOTIFICATION_DESTINATION = "org.gtk.Notifications"
NOTIFICATION_PATH = "/org/gtk/Notifications"
NOTIFICATION_APP_ID = "org.local.OCDeckSwitch"
POLL_SECONDS = 1.0
V2_LOCATION_REFRESH_SECONDS = 15.0


def runtime_state_file(backend: str = "v1") -> Path:
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/ocdeck-{os.getuid()}"))
    suffix = "" if backend == "v1" else f"-{backend}"
    return runtime / f"ocdeck-global-permission-notifications{suffix}.json"


def read_server_environment(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition("=")
        if separator and key in {"OPENCODE_SERVER_PASSWORD", "OPENCODE_SERVER_USERNAME"}:
            values[key] = value.strip().strip('"').strip("'")
    return values


def pending_requests(
    base_url: str, environment: dict[str, str]
) -> tuple[list[dict[str, str]], bool]:
    """Return requests and whether both endpoint snapshots were complete."""
    headers: dict[str, str] = {}
    password = environment.get("OPENCODE_SERVER_PASSWORD", "")
    if password and not api_credentials_are_safe(base_url):
        return [], False
    if password:
        username = environment.get("OPENCODE_SERVER_USERNAME", "opencode")
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    requests: list[dict[str, str]] = []
    complete = True
    for endpoint, kind in (("/permission", "permission"), ("/question", "question")):
        try:
            request = urllib.request.Request(f"{base_url}{endpoint}", headers=headers)
            opener = urllib.request.build_opener(NoRedirectHandler())
            with opener.open(request, timeout=2) as response:
                content = response.read(MAX_API_RESPONSE_BYTES + 1)
            if len(content) > MAX_API_RESPONSE_BYTES:
                complete = False
                continue
            payload = json.loads(content.decode("utf-8"))
        except (
            OSError,
            UnicodeDecodeError,
            urllib.error.URLError,
            json.JSONDecodeError,
        ):
            complete = False
            continue
        values = payload.get("data", payload) if isinstance(payload, dict) else payload
        if not isinstance(values, list):
            complete = False
            continue
        for value in values:
            if not isinstance(value, dict):
                complete = False
                continue
            request_id = value.get("id")
            session_id = value.get("sessionID")
            if not isinstance(request_id, str) or not request_id or not isinstance(session_id, str) or not session_id:
                complete = False
                continue
            if kind == "permission":
                detail = str(value.get("permission") or "permission")
                patterns = value.get("patterns")
                if isinstance(patterns, list):
                    detail = f"{detail} {'; '.join(str(item) for item in patterns if isinstance(item, str))}".strip()
            else:
                questions = value.get("questions")
                detail = "question"
                if isinstance(questions, list) and questions:
                    first = questions[0]
                    if isinstance(first, dict):
                        detail = str(first.get("question") or first.get("header") or detail)
            requests.append(
                {"id": request_id, "sessionID": session_id, "kind": kind, "detail": detail}
            )
    return requests, complete


def find_opencode2() -> str | None:
    from ocdeck.v2_read_api import find_opencode2 as locate
    return locate()


def v2_api_json(
    opencode_bin: str,
    operation: str,
    *,
    server: str = "",
    params: dict[str, str] | None = None,
    location: V2Location | None = None,
) -> Any:
    from ocdeck.source import v2_api_operation
    if use_read_api(opencode_bin, server, operation):
        if operation in V2_LOCATION_OPERATIONS and (
            location is None or location.workspace_id or
            any(name.startswith("location[") for name in (params or {}))
        ):
            return None
        try:
            payload = read_api(operation, params=params,
                               location=location.reference() if location else None)
        except ReadAPIError:
            return None
        if v2_error_message(payload) or (
            operation in V2_LOCATION_OPERATIONS and
            not v2_location_envelope_is_valid(payload, location)
        ):
            return None
        return payload
    command = [opencode_bin, "api", v2_api_operation(operation)]
    if server:
        command.extend(("--server", server))
    request_params = dict(params or {})
    if operation in V2_LOCATION_OPERATIONS:
        if location is None or location.workspace_id or any(
            name.startswith("location[") for name in request_params
        ):
            return None
        request_params.update(location.parameters())
    for name, value in request_params.items():
        command.extend(("--param", f"{name}={value}"))
    try:
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(
                command, stdout=output, stderr=subprocess.DEVNULL,
                timeout=10, check=False,
            )
            output.seek(0, os.SEEK_END)
            if output.tell() > MAX_API_RESPONSE_BYTES:
                return None
            output.seek(0)
            stdout = output.read(MAX_API_RESPONSE_BYTES + 1) or result.stdout or b""
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or len(stdout) > MAX_API_RESPONSE_BYTES:
        return None
    try:
        payload = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if v2_error_message(payload):
        return None
    if operation in V2_LOCATION_OPERATIONS and not v2_location_envelope_is_valid(
        payload, location
    ):
        return None
    return payload


def v2_request_locations(
    opencode_bin: str,
    *,
    server: str = "",
) -> tuple[tuple[V2Location, ...], bool]:
    locations = parse_v2_locations(
        v2_api_json(opencode_bin, "v2.debug.location.list", server=server)
    )
    return (locations, True) if locations is not None else ((), False)


def v2_pending_requests(
    opencode_bin: str,
    locations: tuple[V2Location, ...],
    *,
    server: str = "",
) -> tuple[list[dict[str, str]], bool]:
    merged: dict[str, list[dict[str, str]]] = {}
    complete = True
    for location in locations:
        permissions = parse_v2_permissions(
            v2_api_json(
                opencode_bin,
                "v2.permission.request.list",
                server=server,
                location=location,
            )
        )
        forms = parse_v2_forms(
            v2_api_json(
                opencode_bin,
                "v2.form.request.list",
                server=server,
                location=location,
            )
        )
        if permissions is None:
            complete = False
            permissions = {}
        if forms is None:
            complete = False
            forms = {}
        merged = merge_permissions(merged, permissions, forms)

    requests: list[dict[str, str]] = []
    for session_id, records in merged.items():
        for record in records:
            kind = (
                "question" if record.get("permission") == "question" else "permission"
            )
            requests.append(
                {
                    "id": str(record.get("id") or ""),
                    "sessionID": session_id,
                    "kind": kind,
                    "detail": str(record.get("pattern") or kind),
                }
            )
    return requests, complete


def local_pending_requests(backend: str = "v1") -> list[dict[str, str]]:
    requests: list[dict[str, str]] = []
    for session_id, records in read_local_permissions(backend=backend).items():
        for record in records:
            request_id = str(record.get("id") or "")
            if not request_id:
                continue
            kind = "question" if record.get("permission") == "question" else "permission"
            requests.append(
                {
                    "id": request_id,
                    "sessionID": session_id,
                    "kind": kind,
                    "detail": str(record.get("pattern") or kind),
                }
            )
    return requests


def request_identity(request: dict[str, str]) -> str:
    return f"{request['kind']}:{request['sessionID']}:{request['id']}"


def notification_candidates(
    api_requests: list[dict[str, str]],
    local_requests: list[dict[str, str]],
) -> tuple[list[dict[str, str]], set[str]]:
    local_ids = {request_identity(item) for item in local_requests}
    api_by_id = {request_identity(item): item for item in api_requests}
    candidates = [item for identity, item in api_by_id.items() if identity not in local_ids]
    return candidates, set(api_by_id) | local_ids


def gvariant_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def notify(request: dict[str, str]) -> bool:
    title = f"OC Deck {request['kind']}: {request['detail'][:80]}"
    body = f"Session {request['sessionID'][:24]}. Open OC Deck and press G to review."
    payload = f"{{'title': <{gvariant_string(title)}>, 'body': <{gvariant_string(body)}>}}"
    notification_id = f"ocdeck-global-{request['kind']}-{request['id']}"
    try:
        result = subprocess.run(
            [
                GDBUS,
                "call",
                "--session",
                "--dest",
                NOTIFICATION_DESTINATION,
                "--object-path",
                NOTIFICATION_PATH,
                "--method",
                "org.gtk.Notifications.AddNotification",
                NOTIFICATION_APP_ID,
                notification_id,
                payload,
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def load_seen(path: Path) -> set[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    pending = payload.get("pending") if isinstance(payload, dict) else None
    if not isinstance(pending, list):
        return set()
    return {value for value in pending if isinstance(value, str) and value}


def save_seen(path: Path, seen: set[str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"pending": sorted(seen)}), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)
    except OSError:
        pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Notify once for each pending OpenCode permission or form",
        allow_abbrev=False,
    )
    backend_default = os.environ.get("OCDECK_OPENCODE_BACKEND", "v2")
    parser.add_argument(
        "--backend",
        choices=("v2", "v1"),
        default=backend_default,
        help=(
            "OpenCode backend (defaults to OCDECK_OPENCODE_BACKEND or v2; "
            "v1 is the temporary rollback)"
        ),
    )
    parser.add_argument("--url", default="", help="explicit OpenCode server URL")
    parser.add_argument("--opencode2-bin", default="", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.backend not in {"v1", "v2"}:
        parser.error("OCDECK_OPENCODE_BACKEND must be 'v1' or 'v2'")
    if args.url:
        error = validate_api_url(args.url.rstrip("/"))
        if error:
            parser.error(error)
        args.url = args.url.rstrip("/")
    return args


def start_completion_watcher(args: argparse.Namespace) -> subprocess.Popen | None:
    # The native CLI's completion alerts require terminal OSC notification support.
    # Our managed Ptyxis/tmux path instead delivers those events straight to GNOME.
    if args.backend != "v2" or args.url:
        return None
    node = shutil.which("node")
    script = Path(__file__).resolve().parent / "plugins/v2/session-notify-watcher.mjs"
    if not node or not script.is_file():
        return None
    environment = dict(os.environ)
    environment["XDG_CONFIG_HOME"] = str(Path.home() / ".config/ocdeck-v2-runtime")
    try:
        return subprocess.Popen([node, str(script)], env=environment)
    except OSError:
        return None


def watch_requests(args: argparse.Namespace, completion: list[subprocess.Popen | None]) -> None:
    environment_file = Path.home() / ".config/opencode/server.env"
    state_file = runtime_state_file(args.backend)
    seen = load_seen(state_file)
    opencode2_bin = args.opencode2_bin or find_opencode2()
    locations: tuple[V2Location, ...] = ()
    locations_complete = False
    next_location_refresh = 0.0
    while True:
        if completion[0] is None or completion[0].poll() is not None:
            completion[0] = start_completion_watcher(args)
        complete = True
        if args.backend == "v2":
            now = time.monotonic()
            if opencode2_bin and now >= next_location_refresh:
                discovered, locations_complete = v2_request_locations(
                    opencode2_bin, server=args.url
                )
                if locations_complete:
                    locations = discovered
                next_location_refresh = now + V2_LOCATION_REFRESH_SECONDS
            complete = locations_complete
            if opencode2_bin:
                api_requests, requests_ok = v2_pending_requests(
                    opencode2_bin, locations, server=args.url
                )
                complete = complete and requests_ok
            else:
                api_requests, complete = [], False
            requests, current = notification_candidates(
                api_requests, local_pending_requests("v2")
            )
        else:
            environment = read_server_environment(environment_file)
            api_requests, complete = pending_requests(
                args.url or "http://127.0.0.1:4096", environment
            )
            requests, current = notification_candidates(
                api_requests,
                local_pending_requests("v1"),
            )
        if not complete:
            current |= seen
        for request in requests:
            identity = request_identity(request)
            if identity not in seen:
                if not notify(request):
                    current.discard(identity)
        if current != seen:
            seen = current
            save_seen(state_file, seen)
        time.sleep(POLL_SECONDS)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    completion: list[subprocess.Popen | None] = [None]
    try:
        watch_requests(args, completion)
    finally:
        if completion[0] is not None and completion[0].poll() is None:
            completion[0].terminate()
            try:
                completion[0].wait(timeout=5)
            except subprocess.TimeoutExpired:
                completion[0].kill()
                completion[0].wait()


if __name__ == "__main__":
    main()
