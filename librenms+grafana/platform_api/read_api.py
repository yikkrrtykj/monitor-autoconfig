"""Read-only HTTP routing for the platform API entrypoint.

The entrypoint supplies every stateful operation explicitly. Importing this
module therefore performs no file access, starts no threads, and creates no
dependency back to the compatibility entrypoint.
"""
from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse


@dataclass(frozen=True)
class ReadApiDependencies:
    clock: Callable[[], float]
    version_payload: Callable[[], dict]
    auth_status: Callable[[Any], dict]
    require_auth: Callable[[Any], dict]
    config_payload: Callable[[], dict]
    read_json_file: Callable[[Path, Any], Any]
    history_path: Path
    read_apply_status: Callable[[str], dict]
    incident_list: Callable[[], list[dict]]
    iperf_status_payload: Callable[[str], dict]
    iperf_history_payload: Callable[[], dict]
    get_dhcp_settings: Callable[[], dict]
    get_dhcp_bindings: Callable[[], dict]
    bridge_retire_pending: Callable[[], dict]
    get_dhcp_dashboard: Callable[[bool], dict]
    config_path: Path
    stamp: Callable[[], str]


def handle_get(handler: Any, request_target: str, deps: ReadApiDependencies) -> None:
    """Dispatch one GET request without owning any mutable platform state."""
    parsed_url = urlparse(request_target)
    path = parsed_url.path.rstrip("/") or "/"
    if path == "/health":
        handler._send_json({"ok": True, "time": int(deps.clock())})
    elif path == "/version":
        handler._send_json(deps.version_payload())
    elif path == "/auth/status":
        handler._send_json(deps.auth_status(handler))
    elif path == "/config":
        deps.require_auth(handler)
        payload = deps.config_payload()
        payload["history"] = deps.read_json_file(deps.history_path, [])[:20]
        handler._send_json(payload)
    elif path == "/config/apply-status":
        deps.require_auth(handler)
        operation_id = (parse_qs(parsed_url.query).get("operationId") or [""])[-1]
        handler._send_json(deps.read_apply_status(operation_id))
    elif path == "/incidents":
        deps.require_auth(handler)
        handler._send_json({"ok": True, "incidents": deps.incident_list()})
    elif path == "/network/iperf3/status":
        deps.require_auth(handler)
        task_id = (parse_qs(parsed_url.query).get("taskId") or [""])[-1]
        handler._send_json(deps.iperf_status_payload(task_id))
    elif path == "/network/iperf3/history":
        deps.require_auth(handler)
        handler._send_json(deps.iperf_history_payload())
    elif path == "/network/dhcp/settings":
        deps.require_auth(handler)
        handler._send_json(deps.get_dhcp_settings())
    elif path == "/network/dhcp/bindings":
        deps.require_auth(handler)
        handler._send_json(deps.get_dhcp_bindings())
    elif path == "/network/retire/pending":
        deps.require_auth(handler)
        handler._send_json(deps.bridge_retire_pending())
    elif path == "/network/dhcp":
        # Authentication must precede the optional privileged Telnet refresh.
        deps.require_auth(handler)
        force = (
            (parse_qs(parsed_url.query).get("force") or [""])[-1].lower()
            in ("1", "true", "yes")
        )
        handler._send_json(deps.get_dhcp_dashboard(force))
    elif path == "/config/download":
        deps.require_auth(handler)
        text = (
            deps.config_path.read_text(encoding="utf-8")
            if deps.config_path.exists()
            else ""
        )
        handler._send_bytes(
            text.encode("utf-8"),
            f"event-config-{deps.stamp()}.yml",
            "application/x-yaml; charset=utf-8",
        )
    else:
        handler._send_json(
            {"ok": False, "error": "not found"},
            HTTPStatus.NOT_FOUND,
        )
