"""Write HTTP routing for the platform API compatibility entrypoint.

The entrypoint owns request parsing, exception-to-HTTP mapping, locks, and all
stateful business operations. Importing this module therefore performs no file
or network access and starts no background work.
"""
from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Callable
from urllib.parse import unquote, urlparse


@dataclass(frozen=True)
class WriteApiDependencies:
    login_auth: Callable[[str, str, str], tuple[dict, str]]
    change_password_auth: Callable[[Any, dict], tuple[dict, str]]
    logout_auth: Callable[[Any], None]
    clear_session_cookie: Callable[[], str]
    require_auth: Callable[[Any], dict]
    config_payload: Callable[[str], dict]
    write_lock: Any
    save_config: Callable[[str, str, str], dict]
    apply_config: Callable[[str | None, str, str, Any], dict]
    rollback_config: Callable[[str, str, Any], dict]
    new_incident: Callable[[dict], dict]
    send_test_alert: Callable[[], dict]
    run_precheck: Callable[[], dict]
    start_iperf_task: Callable[[dict], dict]
    stop_iperf_task: Callable[[dict], dict]
    bridge_retire_resolve: Callable[[dict], dict]
    test_dhcp_connection: Callable[[], dict]
    save_dhcp_settings: Callable[[dict], dict]
    update_incident: Callable[[int, dict], dict]


def handle_post(
    handler: Any,
    request_target: str,
    data: dict,
    deps: WriteApiDependencies,
) -> None:
    """Dispatch one already-parsed POST body to existing business functions."""
    path = urlparse(request_target).path.rstrip("/") or "/"
    if path == "/auth/login":
        payload, cookie = deps.login_auth(
            str(data.get("username") or ""),
            str(data.get("password") or ""),
            str((handler.client_address or ("", 0))[0]),
        )
        handler._send_json(payload, headers={"Set-Cookie": cookie})
    elif path == "/auth/change-password":
        payload, cookie = deps.change_password_auth(handler, data)
        handler._send_json(payload, headers={"Set-Cookie": cookie})
    elif path == "/auth/logout":
        deps.logout_auth(handler)
        handler._send_json(
            {"ok": True, "authenticated": False},
            headers={"Set-Cookie": deps.clear_session_cookie()},
        )
    elif path == "/config/validate":
        deps.require_auth(handler)
        handler._send_json(deps.config_payload(data.get("text", "")))
    elif path == "/config/save":
        auth = deps.require_auth(handler)
        with deps.write_lock:
            handler._send_json(
                deps.save_config(
                    data.get("text", ""),
                    auth["username"],
                    data.get("note", ""),
                )
            )
    elif path == "/config/apply":
        auth = deps.require_auth(handler)
        text = data.get("text") if "text" in data else None
        with deps.write_lock:
            handler._send_json(
                deps.apply_config(
                    text,
                    auth["username"],
                    data.get("note", ""),
                    data.get("operationId"),
                )
            )
    elif path == "/config/rollback":
        auth = deps.require_auth(handler)
        with deps.write_lock:
            handler._send_json(
                deps.rollback_config(
                    auth["username"],
                    data.get("note", ""),
                    data.get("operationId"),
                )
            )
    elif path == "/config/import":
        auth = deps.require_auth(handler)
        with deps.write_lock:
            handler._send_json(
                deps.save_config(data.get("text", ""), auth["username"], "import")
            )
    elif path == "/incidents":
        deps.require_auth(handler)
        # incidents.json is read-modify-write; concurrent submissions must
        # remain serialized under the entrypoint's existing write lock.
        with deps.write_lock:
            handler._send_json({"ok": True, "incident": deps.new_incident(data)})
    elif path == "/test-alert":
        deps.require_auth(handler)
        handler._send_json(deps.send_test_alert())
    elif path == "/pre-check":
        deps.require_auth(handler)
        handler._send_json(deps.run_precheck())
    elif path == "/network/iperf3":
        deps.require_auth(handler)
        handler._send_json(deps.start_iperf_task(data))
    elif path == "/network/iperf3/stop":
        deps.require_auth(handler)
        handler._send_json(deps.stop_iperf_task(data))
    elif path == "/network/retire/resolve":
        deps.require_auth(handler)
        handler._send_json(deps.bridge_retire_resolve(data))
    elif path == "/network/dhcp/test":
        deps.require_auth(handler)
        handler._send_json(deps.test_dhcp_connection())
    elif path == "/network/dhcp/settings":
        deps.require_auth(handler)
        with deps.write_lock:
            handler._send_json(deps.save_dhcp_settings(data))
    else:
        handler._send_json(
            {"ok": False, "error": "not found"},
            HTTPStatus.NOT_FOUND,
        )


def handle_patch(
    handler: Any,
    request_target: str,
    deps: WriteApiDependencies,
) -> None:
    """Dispatch PATCH while preserving auth-before-path/body ordering."""
    deps.require_auth(handler)
    path = urlparse(request_target).path.rstrip("/")
    parts = [unquote(part) for part in path.split("/") if part]
    if len(parts) == 2 and parts[0] == "incidents":
        with deps.write_lock:
            incident = deps.update_incident(int(parts[1]), handler._body())
        handler._send_json({"ok": True, "incident": incident})
    else:
        handler._send_json(
            {"ok": False, "error": "not found"},
            HTTPStatus.NOT_FOUND,
        )
