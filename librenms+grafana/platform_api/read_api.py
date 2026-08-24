"""Read-only HTTP routing and orchestration for the platform API."""
from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from . import (
    bridge,
    config_transaction,
    dhcp_runtime,
    dhcp_settings,
    event_config,
    incidents,
    iperf_runtime,
)


@dataclass(frozen=True)
class ReadApiContext:
    event_config_context: event_config.EventConfigContext
    transaction_context: config_transaction.ConfigTransactionContext
    incident_context: incidents.IncidentContext
    iperf_runtime_context: iperf_runtime.IperfRuntimeContext
    dhcp_settings_context: dhcp_settings.DhcpSettingsContext
    dhcp_runtime_context: dhcp_runtime.DhcpRuntimeContext
    bridge_url: str
    require_auth: Callable[[Any], dict]
    read_json_file: Callable[[Path, Any], Any]
    stamp: Callable[[], str]


def handle_get(handler: Any, request_target: str, context: ReadApiContext) -> None:
    """Dispatch one GET request without owning any mutable platform state."""
    parsed_url = urlparse(request_target)
    path = parsed_url.path.rstrip("/") or "/"
    if path == "/version":
        handler._send_json(
            event_config.version_payload(context.event_config_context),
        )
    elif path == "/config":
        context.require_auth(handler)
        payload = event_config.config_payload(context.event_config_context)
        payload["history"] = context.read_json_file(
            context.transaction_context.history_path,
            [],
        )[:20]
        handler._send_json(payload)
    elif path == "/config/apply-status":
        context.require_auth(handler)
        operation_id = (parse_qs(parsed_url.query).get("operationId") or [""])[-1]
        handler._send_json(config_transaction.read_apply_status(
            context.transaction_context,
            operation_id,
        ))
    elif path == "/incidents":
        context.require_auth(handler)
        handler._send_json({
            "ok": True,
            "incidents": incidents.incident_list(context.incident_context),
        })
    elif path == "/network/iperf3/status":
        context.require_auth(handler)
        task_id = (parse_qs(parsed_url.query).get("taskId") or [""])[-1]
        handler._send_json(iperf_runtime.iperf_status_payload(
            context.iperf_runtime_context,
            task_id,
        ))
    elif path == "/network/iperf3/history":
        context.require_auth(handler)
        handler._send_json(iperf_runtime.iperf_history_payload(
            context.iperf_runtime_context,
        ))
    elif path == "/network/dhcp/settings":
        context.require_auth(handler)
        handler._send_json(dhcp_settings.get_dhcp_settings(
            context.dhcp_settings_context,
        ))
    elif path == "/network/dhcp/bindings":
        context.require_auth(handler)
        handler._send_json(dhcp_runtime.get_dhcp_bindings(
            context.dhcp_runtime_context,
        ))
    elif path == "/network/retire/pending":
        context.require_auth(handler)
        handler._send_json(bridge.bridge_retire_pending(context.bridge_url))
    elif path == "/network/dhcp":
        # Authentication must precede the optional privileged Telnet refresh.
        context.require_auth(handler)
        force = (
            (parse_qs(parsed_url.query).get("force") or [""])[-1].lower()
            in ("1", "true", "yes")
        )
        handler._send_json(dhcp_runtime.get_dhcp_dashboard(
            context.dhcp_runtime_context,
            force,
        ))
    elif path == "/config/download":
        context.require_auth(handler)
        config_path = context.event_config_context.config_path
        text = (
            config_path.read_text(encoding="utf-8")
            if config_path.exists()
            else ""
        )
        handler._send_bytes(
            text.encode("utf-8"),
            f"event-config-{context.stamp()}.yml",
            "application/x-yaml; charset=utf-8",
        )
    else:
        handler._send_json(
            {"ok": False, "error": "not found"},
            HTTPStatus.NOT_FOUND,
        )
