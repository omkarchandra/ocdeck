"""Pure reconciliation of already-validated OpenCode producer snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .models import clean_int, clean_string


@dataclass(slots=True)
class LocalRuntimeState:
    permissions: dict[str, list[dict[str, Any]]]
    statuses: dict[str, str]


@dataclass(frozen=True, slots=True)
class RuntimeProducer:
    payload: dict[str, Any]
    owned_sessions: frozenset[str]
    updated: int


@dataclass(frozen=True, slots=True)
class StatusSignal:
    state: str
    updated: int
    owns_session: bool


@dataclass(frozen=True, slots=True)
class PendingSignal:
    session_id: str
    kind: str
    request: dict[str, str]
    updated: int
    producer_status: StatusSignal | None


@dataclass(frozen=True, slots=True)
class RequestSnapshot:
    updated: int
    request_ids: frozenset[str]


def local_status(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("type")
    status = clean_string(value).lower()
    return status if status in {"busy", "retry", "idle"} else ""


def merge_permissions(
    *sources: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    merged: dict[str, list[dict[str, Any]]] = {}
    seen: dict[str, set[tuple[str, str, str]]] = {}
    for source in sources:
        for session_id, requests in source.items():
            for request in requests:
                request_id = clean_string(request.get("id"))
                permission = clean_string(request.get("permission"))
                pattern = clean_string(request.get("pattern"))
                key = ("id", request_id, "") if request_id else ("value", permission, pattern)
                if key in seen.setdefault(session_id, set()):
                    continue
                seen[session_id].add(key)
                normalized: dict[str, Any] = {
                    "id": request_id,
                    "permission": permission or "permission",
                    "pattern": pattern,
                }
                resources = request.get("resources")
                if isinstance(resources, (list, tuple)) and all(
                    isinstance(item, str) for item in resources
                ):
                    normalized["resources"] = tuple(clean_string(item) for item in resources)
                merged.setdefault(session_id, []).append(normalized)
    return merged


def reconcile_runtime_producers(producers: Iterable[RuntimeProducer]) -> LocalRuntimeState:
    statuses: dict[str, StatusSignal] = {}
    requests: list[PendingSignal] = []
    deleted: dict[str, int] = {}
    # A complete, explicitly owned snapshot can supersede another instance's
    # older abandoned prompt. Missing/malformed arrays are not evidence of reply.
    request_snapshots: dict[tuple[str, str], RequestSnapshot] = {}

    for producer in producers:
        payload = producer.payload
        own_statuses: dict[str, StatusSignal] = {}
        explicit_times: dict[str, int] = {}
        records = payload.get("statuses")
        for item in records if isinstance(records, list) else ():
            if not isinstance(item, dict):
                continue
            session_id = clean_string(item.get("sessionID"))
            status = local_status(item.get("status"))
            if not session_id or not status:
                continue
            event_time = clean_int(item.get("updated"))
            signal = StatusSignal(
                state=status,
                updated=event_time or producer.updated,
                owns_session=session_id in producer.owned_sessions,
            )
            previous = own_statuses.get(session_id)
            if previous is None or signal.updated >= previous.updated:
                own_statuses[session_id] = signal
                explicit_times[session_id] = event_time
            previous = statuses.get(session_id)
            if previous is None or (signal.updated, signal.owns_session) >= (
                previous.updated, previous.owns_session
            ):
                statuses[session_id] = signal

        records = payload.get("deletedSessions")
        for item in records if isinstance(records, list) else ():
            if isinstance(item, dict):
                session_id = clean_string(item.get("sessionID"))
                if session_id:
                    deleted[session_id] = max(
                        deleted.get(session_id, -1),
                        clean_int(item.get("updated")) or producer.updated,
                    )

        for kind in ("permissions", "questions"):
            records = payload.get(kind)
            complete = isinstance(records, list) and all(
                isinstance(item, dict) and clean_string(item.get("id"))
                and clean_string(item.get("sessionID")) for item in records
            )
            if complete:
                for session_id, signal in own_statuses.items():
                    event_time = explicit_times[session_id]
                    key = (session_id, kind)
                    previous = request_snapshots.get(key)
                    if signal.owns_session and event_time and (
                        previous is None or event_time > previous.updated
                    ):
                        ids = frozenset(
                            clean_string(item.get("id")) for item in records
                            if clean_string(item.get("sessionID")) == session_id
                        )
                        request_snapshots[key] = RequestSnapshot(event_time, ids)
            for item in records if isinstance(records, list) else ():
                if not isinstance(item, dict):
                    continue
                session_id = clean_string(item.get("sessionID"))
                if not session_id:
                    continue
                if kind == "questions":
                    permission = "question"
                    pattern = clean_string(item.get("question")) or "Input required in the terminal"
                else:
                    permission = clean_string(item.get("permission")) or "permission"
                    pattern = clean_string(item.get("pattern"))
                request = {"id": clean_string(item.get("id")),
                           "permission": permission, "pattern": pattern}
                requests.append(PendingSignal(
                    session_id=session_id,
                    kind=kind,
                    request=request,
                    updated=clean_int(item.get("updated")) or producer.updated,
                    producer_status=own_statuses.get(session_id),
                ))

    statuses = {
        session_id: signal for session_id, signal in statuses.items()
        if deleted.get(session_id, -1) < signal.updated
    }
    pending: dict[str, list[dict[str, Any]]] = {}
    for candidate in requests:
        if deleted.get(candidate.session_id, -1) >= candidate.updated:
            continue
        own = candidate.producer_status
        if own is not None and own.state == "idle" and own.updated >= candidate.updated:
            continue
        chosen = statuses.get(candidate.session_id)
        if (chosen is not None and chosen.owns_session and chosen.state == "idle"
                and chosen.updated >= candidate.updated):
            continue
        authoritative = request_snapshots.get((candidate.session_id, candidate.kind))
        if (authoritative is not None and candidate.request["id"]
                and authoritative.updated > candidate.updated
                and candidate.request["id"] not in authoritative.request_ids):
            continue
        pending.setdefault(candidate.session_id, []).append(candidate.request)
    return LocalRuntimeState(
        merge_permissions(pending),
        {session_id: signal.state for session_id, signal in statuses.items()},
    )
