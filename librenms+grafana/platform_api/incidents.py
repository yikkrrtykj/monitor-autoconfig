"""Incident persistence and domain behavior for the platform API."""
from __future__ import annotations

import json
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Callable

MAX_INCIDENTS = 1000
MAX_EVENTS_PER_INCIDENT = 200
MAX_INCIDENT_BYTES = 64 * 1024
MAX_INCIDENT_FILE_BYTES = 16 * 1024 * 1024


class IncidentError(Exception):
    status = HTTPStatus.INTERNAL_SERVER_ERROR


class IncidentStorageError(IncidentError):
    """Existing incident storage cannot be read or safely operated on."""


class IncidentCapacityError(IncidentError):
    """A requested mutation would exceed an incident storage bound."""

    status = HTTPStatus.CONFLICT


@dataclass(frozen=True)
class IncidentContext:
    incident_path: Path
    require_write: Callable[[], None]
    clock: Callable[[], float]


def incident_list(context: IncidentContext) -> list[dict]:
    try:
        with context.incident_path.open("rb") as handle:
            raw = handle.read(MAX_INCIDENT_FILE_BYTES + 1)
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise IncidentStorageError("事故存储不可读取，需要人工处理") from exc
    if len(raw) > MAX_INCIDENT_FILE_BYTES:
        raise IncidentStorageError(
            f"事故存储文件超过 {MAX_INCIDENT_FILE_BYTES} 字节读取上限，需要人工处理"
        )
    try:
        items = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise IncidentStorageError("事故存储损坏：不是有效 UTF-8，需要人工处理") from exc
    except json.JSONDecodeError as exc:
        raise IncidentStorageError("事故存储损坏：不是有效 JSON，需要人工处理") from exc
    if not isinstance(items, list):
        raise IncidentStorageError("事故存储损坏：JSON 根节点必须是列表，需要人工处理")
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise IncidentStorageError(
                f"事故存储损坏：第 {index + 1} 条记录不是对象，需要人工处理"
            )
        try:
            int(item.get("id", 0))
        except (TypeError, ValueError) as exc:
            raise IncidentStorageError(
                f"事故存储损坏：第 {index + 1} 条记录 ID 无效，需要人工处理"
            ) from exc
        if "events" in item and not isinstance(item["events"], list):
            raise IncidentStorageError(
                f"事故存储损坏：第 {index + 1} 条记录 events 不是列表，需要人工处理"
            )
    return items


def _serialized(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _serialized_size(payload) -> int:
    return len(_serialized(payload).encode("utf-8"))


def _validate_growth(current: int, candidate: int, limit: int, label: str) -> None:
    if candidate > limit and not (current > limit and candidate <= current):
        raise IncidentCapacityError(f"{label}超过上限 {limit}")


def _validate_candidate(items: list[dict], incident: dict, previous: dict | None = None) -> None:
    events = incident.get("events", [])
    if not isinstance(events, list):
        raise IncidentCapacityError("事故 events 必须是列表")
    previous_events = previous.get("events", []) if previous is not None else []
    _validate_growth(
        len(previous_events), len(events), MAX_EVENTS_PER_INCIDENT,
        "事故 events 数量",
    )
    _validate_growth(
        _serialized_size(previous) if previous is not None else 0,
        _serialized_size(incident), MAX_INCIDENT_BYTES,
        "单条事故序列化字节数",
    )
    file_size = _serialized_size(items)
    if file_size > MAX_INCIDENT_FILE_BYTES:
        raise IncidentCapacityError(
            f"事故存储候选文件序列化字节数超过上限 {MAX_INCIDENT_FILE_BYTES}"
        )


def save_incidents(context: IncidentContext, items: list[dict]) -> None:
    context.incident_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = context.incident_path.with_suffix(context.incident_path.suffix + ".tmp")
    temporary.write_bytes(_serialized(items).encode("utf-8"))
    temporary.replace(context.incident_path)


def new_incident(context: IncidentContext, data: dict) -> dict:
    context.require_write()
    items = incident_list(context)
    if len(items) >= MAX_INCIDENTS:
        raise IncidentCapacityError(f"事故数量达到上限 {MAX_INCIDENTS}，无法新建事故")
    next_id = max([int(item.get("id", 0)) for item in items] or [0]) + 1
    now = int(context.clock())
    incident = {
        "id": next_id,
        "title": data.get("title") or "未命名事故",
        "severity": data.get("severity") or "warn",
        "status": data.get("status") or "open",
        "scope": data.get("scope") or "",
        "owner": data.get("owner") or "",
        "rootCause": data.get("rootCause") or "",
        "startedAt": data.get("startedAt") or now,
        "recoveredAt": data.get("recoveredAt") or None,
        "related": data.get("related") or {},
        "events": data.get("events")
        or [
            {
                "time": now,
                "type": "note",
                "message": data.get("note") or "事故创建",
            }
        ],
    }
    candidate_items = [incident, *items]
    _validate_candidate(candidate_items, incident)
    save_incidents(context, candidate_items)
    return incident


def update_incident(
    context: IncidentContext,
    incident_id: int,
    data: dict,
) -> dict:
    context.require_write()
    items = incident_list(context)
    for index, item in enumerate(items):
        if int(item.get("id", 0)) == incident_id:
            candidate = dict(item)
            for key in (
                "title",
                "severity",
                "status",
                "scope",
                "owner",
                "rootCause",
                "recoveredAt",
                "related",
            ):
                if key in data:
                    candidate[key] = data[key]
            if data.get("event"):
                candidate["events"] = [
                    *candidate.get("events", []),
                    {
                        "time": int(context.clock()),
                        "type": data.get("eventType") or "note",
                        "message": data["event"],
                    },
                ]
            candidate_items = [*items]
            candidate_items[index] = candidate
            _validate_candidate(candidate_items, candidate, item)
            save_incidents(context, candidate_items)
            return candidate
    raise KeyError(f"incident {incident_id} not found")
