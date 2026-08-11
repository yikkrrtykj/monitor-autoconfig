"""Incident persistence and domain behavior for the platform API."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .storage import read_json_file, write_json_file


@dataclass(frozen=True)
class IncidentContext:
    incident_path: Path
    require_write: Callable[[], None]
    clock: Callable[[], float]


def incident_list(context: IncidentContext) -> list[dict]:
    return read_json_file(context.incident_path, [])


def save_incidents(context: IncidentContext, items: list[dict]) -> None:
    write_json_file(context.incident_path, items)


def new_incident(context: IncidentContext, data: dict) -> dict:
    context.require_write()
    items = incident_list(context)
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
    items.insert(0, incident)
    save_incidents(context, items)
    return incident


def update_incident(
    context: IncidentContext,
    incident_id: int,
    data: dict,
) -> dict:
    context.require_write()
    items = incident_list(context)
    for item in items:
        if int(item.get("id", 0)) == incident_id:
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
                    item[key] = data[key]
            if data.get("event"):
                item.setdefault("events", []).append(
                    {
                        "time": int(context.clock()),
                        "type": data.get("eventType") or "note",
                        "message": data["event"],
                    }
                )
            save_incidents(context, items)
            return item
    raise KeyError(f"incident {incident_id} not found")
