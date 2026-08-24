"""Platform API for event config, incidents, and network diagnostics.

This service is intentionally small. It owns the writable platform state while
the bigscreen remains a static UI served by nginx. Cisco Telnet uses the pinned
telnetlib3 compatibility module so the service also works on Python 3.13+.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from platform_config import (
    merge_env_file,
    read_env,
    stamp,
    validate_config,
)
from version_info import get_version_info
from platform_api import apply_runtime as platform_apply_runtime
from platform_api import auth as platform_auth
from platform_api import bridge as platform_bridge
from platform_api import config_transaction as platform_config_transaction
from platform_api import config_write as platform_config_write
from platform_api import dhcp_runtime as platform_dhcp_runtime
from platform_api import dhcp_settings as platform_dhcp_settings
from platform_api import dhcp_telnet as platform_dhcp_telnet
from platform_api import event_config as platform_event_config
from platform_api import incidents as platform_incidents
from platform_api import iperf as platform_iperf
from platform_api import iperf_runtime as platform_iperf_runtime
from platform_api import precheck as platform_precheck
from platform_api import read_api as platform_read_api
from platform_api import storage as platform_storage
from platform_api import write_api as platform_write_api
from platform_api.settings import load_settings


CORE_SETTINGS = load_settings()
WORKDIR = CORE_SETTINGS.workdir
STATE_DIR = CORE_SETTINGS.state_dir
CONFIG_PATH = CORE_SETTINGS.config_path
EXAMPLE_PATH = CORE_SETTINGS.example_path
ENV_PATH = CORE_SETTINGS.env_path
INCIDENT_PATH = CORE_SETTINGS.incident_path
AUTH_PATH = CORE_SETTINGS.auth_path
DHCP_SETTINGS_PATH = CORE_SETTINGS.dhcp_settings_path
IPERF_HISTORY_PATH = CORE_SETTINGS.iperf_history_path
HISTORY_DIR = CORE_SETTINGS.history_dir
TRANSACTION_DIR = CORE_SETTINGS.transaction_dir
APPLY_STATUS_DIR = CORE_SETTINGS.apply_status_dir
WRITE_ENABLED = CORE_SETTINGS.write_enabled
APPLY_ENABLED = CORE_SETTINGS.apply_enabled
APPLY_COMMAND = CORE_SETTINGS.apply_command
APPLY_TIMEOUT = CORE_SETTINGS.apply_timeout
APPLY_VERIFY_TIMEOUT = CORE_SETTINGS.apply_verify_timeout
MAX_REQUEST_BODY_BYTES = CORE_SETTINGS.max_request_body_bytes
IPERF3_COMMAND = os.environ.get(
    "PLATFORM_IPERF3_COMMAND",
    "iperf3",
)
IPERF3_TIMEOUT = max(20, min(300, int(os.environ.get("PLATFORM_IPERF3_TIMEOUT", "60"))))
IPERF3_CONNECT_TIMEOUT_MS = max(500, min(10000, int(os.environ.get("PLATFORM_IPERF3_CONNECT_TIMEOUT_MS", "3000"))))
# 默认只允许公网测速目标。自定义公网节点随便填；只有要测内网 iperf3 服务器时
# 才需要打开这个开关——否则测速接口会变成对内网的 TCP 端口探测器。
IPERF3_ALLOW_INTERNAL = os.environ.get("PLATFORM_IPERF3_ALLOW_INTERNAL", "").lower() in ("1", "true", "yes", "on")
DHCP_SWITCH_USERNAME = os.environ.get("PLATFORM_DHCP_SWITCH_USERNAME", "").strip()
DHCP_SWITCH_PASSWORD = os.environ.get("PLATFORM_DHCP_SWITCH_PASSWORD", "")
DHCP_SWITCH_ENABLE_PASSWORD = os.environ.get("PLATFORM_DHCP_SWITCH_ENABLE_PASSWORD", "")
DHCP_SWITCH_PORT = max(1, min(65535, int(os.environ.get("PLATFORM_DHCP_SWITCH_PORT", "23"))))
DHCP_SWITCH_TIMEOUT = max(3, min(30, int(os.environ.get("PLATFORM_DHCP_SWITCH_TIMEOUT", "8"))))
DHCP_REFRESH_SECONDS = max(30, min(300, int(os.environ.get("PLATFORM_DHCP_REFRESH_SECONDS", "60"))))
BRIDGE_URL = os.environ.get("PLATFORM_BRIDGE_URL", "http://alertmanager-feishu-bridge:5005").rstrip("/")
# The console's 赛前体检 queries these by service name (same docker network).
PRECHECK_PROM_URL = os.environ.get("PLATFORM_PRECHECK_PROM_URL", "http://prometheus:9090")
PRECHECK_GRAFANA_URL = os.environ.get("PLATFORM_PRECHECK_GRAFANA_URL", "http://grafana:3000")
PRECHECK_BIGSCREEN_URL = os.environ.get("PLATFORM_PRECHECK_BIGSCREEN_URL", "http://bigscreen").rstrip("/")
PRECHECK_LIBRENMS_URL = os.environ.get("PLATFORM_PRECHECK_LIBRENMS_URL", "http://librenms:8000").rstrip("/")
PRECHECK_PLAYER_TARGETS_URL = os.environ.get("PLATFORM_PRECHECK_PLAYER_TARGETS_URL", "http://player-targets:9199").rstrip("/")
AUTH_ENABLED = CORE_SETTINGS.auth_enabled
AUTH_ADMIN_USER = CORE_SETTINGS.auth_admin_user
AUTH_DEFAULT_PASSWORD = CORE_SETTINGS.auth_default_password
AUTH_COOKIE_NAME = CORE_SETTINGS.auth_cookie_name
AUTH_COOKIE_SECURE = CORE_SETTINGS.auth_cookie_secure
AUTH_SESSION_SECONDS = CORE_SETTINGS.auth_session_seconds
PASSWORD_MIN_LENGTH = CORE_SETTINGS.password_min_length
PASSWORD_HASH_ITERATIONS = platform_auth.PASSWORD_HASH_ITERATIONS
AUTH_FAILURE_WINDOW_SECONDS = CORE_SETTINGS.auth_failure_window_seconds
AUTH_FAILURE_LIMIT = CORE_SETTINGS.auth_failure_limit
AUTH_LOCK_SECONDS = CORE_SETTINGS.auth_lock_seconds
TRANSACTION_RETENTION = CORE_SETTINGS.transaction_retention
APPLY_STATUS_RETENTION = CORE_SETTINGS.apply_status_retention
AUTH_CONTEXT = platform_auth.AuthContext(
    auth_path=AUTH_PATH,
    enabled=AUTH_ENABLED,
    admin_user=AUTH_ADMIN_USER,
    default_password=AUTH_DEFAULT_PASSWORD,
    cookie_name=AUTH_COOKIE_NAME,
    cookie_secure=AUTH_COOKIE_SECURE,
    session_seconds=AUTH_SESSION_SECONDS,
    password_min_length=PASSWORD_MIN_LENGTH,
    failure_window_seconds=AUTH_FAILURE_WINDOW_SECONDS,
    failure_limit=AUTH_FAILURE_LIMIT,
    lock_seconds=AUTH_LOCK_SECONDS,
    history_writer=lambda action, actor, note, detail: (
        platform_config_transaction.append_history(
            _config_transaction_context(), action, actor, note, detail,
        )
    ),
)
SESSIONS = AUTH_CONTEXT.sessions
AUTH_FAILURES = AUTH_CONTEXT.failures
AUTH_FAILURES_LOCK = AUTH_CONTEXT.failures_lock

AuthError = platform_auth.AuthError
read_json_file = platform_storage.read_json_file
write_json_file = platform_storage.write_json_file


class DiagnosticError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.payload = {"ok": False, "error": message, **extra}


def ensure_dirs() -> None:
    platform_storage.ensure_directories((
        STATE_DIR, HISTORY_DIR, TRANSACTION_DIR, APPLY_STATUS_DIR,
    ))
    ensure_auth_store()


# Serializes config-mutating requests so the now-threaded server can't interleave
# two saves/applies writing the same files.
WRITE_LOCK = threading.Lock()
def _config_transaction_context(
) -> platform_config_transaction.ConfigTransactionContext:
    """Reflect compatibility globals that existing callers may override."""
    return platform_config_transaction.ConfigTransactionContext(
        config_path=CONFIG_PATH,
        env_path=ENV_PATH,
        history_path=STATE_DIR / "history.json",
        transaction_dir=TRANSACTION_DIR,
        apply_status_dir=APPLY_STATUS_DIR,
        transaction_retention=TRANSACTION_RETENTION,
        apply_status_retention=APPLY_STATUS_RETENTION,
    )


def _sync_auth_context() -> platform_auth.AuthContext:
    """Reflect compatibility globals that existing callers may override."""
    AUTH_CONTEXT.auth_path = AUTH_PATH
    AUTH_CONTEXT.enabled = AUTH_ENABLED
    AUTH_CONTEXT.admin_user = AUTH_ADMIN_USER
    AUTH_CONTEXT.default_password = AUTH_DEFAULT_PASSWORD
    AUTH_CONTEXT.cookie_name = AUTH_COOKIE_NAME
    AUTH_CONTEXT.cookie_secure = AUTH_COOKIE_SECURE
    AUTH_CONTEXT.session_seconds = AUTH_SESSION_SECONDS
    AUTH_CONTEXT.password_min_length = PASSWORD_MIN_LENGTH
    AUTH_CONTEXT.failure_window_seconds = AUTH_FAILURE_WINDOW_SECONDS
    AUTH_CONTEXT.failure_limit = AUTH_FAILURE_LIMIT
    AUTH_CONTEXT.lock_seconds = AUTH_LOCK_SECONDS
    AUTH_CONTEXT.sessions = SESSIONS
    AUTH_CONTEXT.failures = AUTH_FAILURES
    AUTH_CONTEXT.failures_lock = AUTH_FAILURES_LOCK
    return AUTH_CONTEXT


b64encode = platform_auth.b64encode
b64decode = platform_auth.b64decode
hash_password = platform_auth.hash_password
verify_password = platform_auth.verify_password
parse_cookies = platform_auth.parse_cookies
_auth_failure_keys = platform_auth.auth_failure_keys


def ensure_auth_store() -> None:
    platform_auth.ensure_auth_store(_sync_auth_context())


def read_auth_store() -> dict:
    return platform_auth.read_auth_store(_sync_auth_context())


def write_auth_store(store: dict) -> None:
    platform_auth.write_auth_store(_sync_auth_context(), store)


def password_strength_error(password: str) -> str | None:
    return platform_auth.password_strength_error(_sync_auth_context(), password)


def prune_sessions() -> None:
    platform_auth.prune_sessions(_sync_auth_context())


def create_session(username: str) -> str:
    return platform_auth.create_session(_sync_auth_context(), username)


def current_session(handler: BaseHTTPRequestHandler) -> dict | None:
    return platform_auth.current_session(_sync_auth_context(), handler)


def session_cookie(token: str, max_age: int | None = None) -> str:
    return platform_auth.session_cookie(
        _sync_auth_context(), token, max_age=max_age,
    )


def clear_session_cookie() -> str:
    return platform_auth.clear_session_cookie(_sync_auth_context())


def auth_status(handler: BaseHTTPRequestHandler) -> dict:
    return platform_auth.auth_status(_sync_auth_context(), handler)


def require_auth(
    handler: BaseHTTPRequestHandler,
    allow_must_change: bool = False,
) -> dict:
    return platform_auth.require_auth(
        _sync_auth_context(), handler, allow_must_change=allow_must_change,
    )


def _auth_lock_remaining(
    username: str,
    client_ip: str,
    now: float | None = None,
) -> int:
    return platform_auth.auth_lock_remaining(
        _sync_auth_context(), username, client_ip, now=now,
    )


def _record_auth_failure(
    username: str,
    client_ip: str,
    now: float | None = None,
) -> int:
    return platform_auth.record_auth_failure(
        _sync_auth_context(), username, client_ip, now=now,
    )


def _clear_auth_failures(username: str, client_ip: str) -> None:
    platform_auth.clear_auth_failures(
        _sync_auth_context(), username, client_ip,
    )


def login_auth(
    username: str,
    password: str,
    client_ip: str = "",
) -> tuple[dict, str]:
    return platform_auth.login_auth(
        _sync_auth_context(), username, password, client_ip,
    )


def change_password_auth(
    handler: BaseHTTPRequestHandler,
    data: dict,
) -> tuple[dict, str]:
    return platform_auth.change_password_auth(
        _sync_auth_context(), handler, data,
    )


def logout_auth(handler: BaseHTTPRequestHandler) -> None:
    platform_auth.logout_auth(_sync_auth_context(), handler)


def _event_config_context() -> platform_event_config.EventConfigContext:
    return platform_event_config.EventConfigContext(
        config_path=CONFIG_PATH,
        example_path=EXAMPLE_PATH,
        env_path=ENV_PATH,
        state_dir=STATE_DIR,
        write_enabled=WRITE_ENABLED,
        get_version_info=get_version_info,
    )


def require_write() -> None:
    if not WRITE_ENABLED:
        raise PermissionError("platform write endpoints are disabled")


def _apply_runtime_context() -> platform_apply_runtime.ApplyRuntimeContext:
    return platform_apply_runtime.ApplyRuntimeContext(
        workdir=WORKDIR,
        apply_enabled=APPLY_ENABLED,
        apply_command=APPLY_COMMAND,
        apply_timeout=APPLY_TIMEOUT,
        verify_timeout=APPLY_VERIFY_TIMEOUT,
        prom_url=PRECHECK_PROM_URL,
        grafana_url=PRECHECK_GRAFANA_URL,
        bridge_url=BRIDGE_URL,
        bigscreen_url=PRECHECK_BIGSCREEN_URL,
    )


def _config_write_context() -> platform_config_write.ConfigWriteContext:
    return platform_config_write.ConfigWriteContext(
        event_config_context=_event_config_context(),
        transaction_context=_config_transaction_context(),
        apply_runtime_context=_apply_runtime_context(),
        config_path=CONFIG_PATH,
        env_path=ENV_PATH,
        write_enabled=WRITE_ENABLED,
        merge_env_file=merge_env_file,
        atomic_write_text=platform_storage.atomic_write_text,
        clock=time.time,
        require_auth=require_auth,
        write_lock=WRITE_LOCK,
    )


def validate_network_host(value: str, field: str = "服务器") -> str:
    """Accept an IPv4 address or a conservative DNS hostname.

    The value is always passed as one subprocess argument / socket hostname; it
    is never interpolated into a shell command.
    """
    host = str(value or "").strip().rstrip(".")
    if not host or len(host) > 253:
        raise DiagnosticError(HTTPStatus.BAD_REQUEST, f"{field}不能为空")
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", host):
        if any(int(part) > 255 for part in host.split(".")):
            raise DiagnosticError(HTTPStatus.BAD_REQUEST, f"{field} IP 地址无效")
        return host
    labels = host.split(".")
    if any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in labels):
        raise DiagnosticError(HTTPStatus.BAD_REQUEST, f"{field}格式无效")
    return host


def configured_core_switch_host() -> str:
    """Return the one configured core switch IP used by the DHCP dashboard."""
    event_config_context = _event_config_context()
    config = platform_event_config.parse_config_text(
        platform_event_config.read_config_text(event_config_context)
    )
    devices = config.get("devices") if isinstance(config.get("devices"), dict) else {}
    core = devices.get("core") if isinstance(devices.get("core"), dict) else {}
    host = str(core.get("ip") or "").strip()
    if not host:
        # Keep older .env-only installations working while making event-config
        # the canonical source for new deployments.
        raw = str(read_env(ENV_PATH).get("CORE_SWITCH_PING") or "").split(",", 1)[0].strip()
        if ":" in raw:
            raw = raw.rsplit(":", 1)[-1].strip()
        host = raw
    if not host:
        raise DiagnosticError(HTTPStatus.UNPROCESSABLE_ENTITY, "基础配置里还没有填写核心 IP")
    return validate_network_host(host, "核心 IP")


def _dhcp_settings_context() -> platform_dhcp_settings.DhcpSettingsContext:
    """Reflect compatibility globals while keeping runtime state in this module."""
    return platform_dhcp_settings.DhcpSettingsContext(
        settings_path=DHCP_SETTINGS_PATH,
        default_username=DHCP_SWITCH_USERNAME,
        default_password=DHCP_SWITCH_PASSWORD,
        default_enable_password=DHCP_SWITCH_ENABLE_PASSWORD,
        default_port=DHCP_SWITCH_PORT,
        write_enabled=WRITE_ENABLED,
        switch_timeout=DHCP_SWITCH_TIMEOUT,
        refresh_seconds=DHCP_REFRESH_SECONDS,
        core_host=configured_core_switch_host,
        cache_clear=platform_dhcp_runtime.clear_cache,
        error_factory=DiagnosticError,
    )


def _dhcp_telnet_context() -> platform_dhcp_telnet.DhcpTelnetContext:
    """Bind the transport to current settings without moving runtime ownership."""
    return platform_dhcp_telnet.DhcpTelnetContext(
        timeout=DHCP_SWITCH_TIMEOUT,
        get_settings=partial(
            platform_dhcp_settings.dhcp_connection_settings,
            _dhcp_settings_context(),
        ),
        error_factory=DiagnosticError,
    )


def _dhcp_runtime_context() -> platform_dhcp_runtime.DhcpRuntimeContext:
    """Bind DHCP orchestration to current composition-root dependencies."""
    return platform_dhcp_runtime.DhcpRuntimeContext(
        core_host=configured_core_switch_host,
        telnet_context=_dhcp_telnet_context,
        connection_settings=lambda: (
            platform_dhcp_settings.dhcp_connection_settings(
                _dhcp_settings_context(),
            )
        ),
        refresh_seconds=DHCP_REFRESH_SECONDS,
        error_factory=DiagnosticError,
        clock=time.time,
        monotonic=time.monotonic,
    )


def _iperf_runtime_context() -> platform_iperf_runtime.IperfRuntimeContext:
    """Bind iPerf orchestration to current composition-root dependencies."""
    return platform_iperf_runtime.IperfRuntimeContext(
        workdir=WORKDIR,
        history_path=IPERF_HISTORY_PATH,
        command=IPERF3_COMMAND,
        timeout=IPERF3_TIMEOUT,
        connect_timeout_ms=IPERF3_CONNECT_TIMEOUT_MS,
        allow_internal=IPERF3_ALLOW_INTERNAL,
        error_factory=DiagnosticError,
        validate_network_host=validate_network_host,
        read_json_file=read_json_file,
        write_json_file=write_json_file,
        host_exec_env=partial(
            platform_apply_runtime.host_exec_env,
            _apply_runtime_context(),
        ),
        clock=time.time,
        monotonic=time.monotonic,
    )


def _incident_context() -> platform_incidents.IncidentContext:
    return platform_incidents.IncidentContext(
        incident_path=INCIDENT_PATH,
        require_write=require_write,
        clock=time.time,
    )


def _precheck_context() -> platform_precheck.PrecheckContext:
    return platform_precheck.PrecheckContext(
        prom_url=PRECHECK_PROM_URL,
        grafana_url=PRECHECK_GRAFANA_URL,
        bridge_url=BRIDGE_URL,
        bigscreen_url=PRECHECK_BIGSCREEN_URL,
        librenms_url=PRECHECK_LIBRENMS_URL,
        player_targets_url=PRECHECK_PLAYER_TARGETS_URL,
        config_issues=lambda: validate_config(
            platform_event_config.parse_config_text(
                platform_event_config.read_config_text(_event_config_context())
            ),
        ),
    )


def _read_api_context() -> platform_read_api.ReadApiContext:
    """Assemble the explicit dependencies for the read-only API domain."""
    return platform_read_api.ReadApiContext(
        event_config_context=_event_config_context(),
        transaction_context=_config_transaction_context(),
        incident_context=_incident_context(),
        iperf_runtime_context=_iperf_runtime_context(),
        dhcp_settings_context=_dhcp_settings_context(),
        dhcp_runtime_context=_dhcp_runtime_context(),
        bridge_url=BRIDGE_URL,
        require_auth=require_auth,
        read_json_file=read_json_file,
        stamp=stamp,
    )


def _write_api_dependencies() -> platform_write_api.WriteApiDependencies:
    """Bind the write router to the entrypoint's current compatibility API."""
    event_config_context = _event_config_context()
    incident_context = _incident_context()
    precheck_context = _precheck_context()
    dhcp_settings_context = _dhcp_settings_context()
    dhcp_runtime_context = _dhcp_runtime_context()
    iperf_runtime_context = _iperf_runtime_context()
    config_write_context = _config_write_context()
    return platform_write_api.WriteApiDependencies(
        login_auth=login_auth,
        change_password_auth=change_password_auth,
        logout_auth=logout_auth,
        clear_session_cookie=clear_session_cookie,
        require_auth=require_auth,
        config_payload=partial(platform_event_config.config_payload, event_config_context),
        write_lock=WRITE_LOCK,
        handle_config_post=partial(
            platform_config_write.handle_post,
            config_write_context,
        ),
        new_incident=partial(platform_incidents.new_incident, incident_context),
        send_test_alert=partial(platform_bridge.send_test_alert, BRIDGE_URL),
        run_precheck=partial(platform_precheck.run_precheck, precheck_context),
        start_iperf_task=partial(
            platform_iperf_runtime.start_iperf_task,
            iperf_runtime_context,
        ),
        stop_iperf_task=partial(
            platform_iperf_runtime.stop_iperf_task,
            iperf_runtime_context,
        ),
        bridge_retire_resolve=partial(
            platform_bridge.bridge_retire_resolve,
            BRIDGE_URL,
        ),
        test_dhcp_connection=partial(
            platform_dhcp_runtime.test_dhcp_connection,
            dhcp_runtime_context,
        ),
        save_dhcp_settings=partial(
            platform_dhcp_settings.save_dhcp_settings,
            dhcp_settings_context,
        ),
        update_incident=partial(
            platform_incidents.update_incident,
            incident_context,
        ),
    )


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, payload, status: int = 200, headers: dict[str, str] | None = None):
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if headers:
            for key, value in headers.items():
                self.send_header(key, value)
        self.end_headers()
        if status != HTTPStatus.NO_CONTENT:
            self.wfile.write(body)

    def _send_bytes(self, body: bytes, filename: str, content_type: str = "application/zip"):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise AuthError(HTTPStatus.BAD_REQUEST, "Content-Length 无效")
        if length < 0:
            raise AuthError(HTTPStatus.BAD_REQUEST, "Content-Length 无效")
        if length > MAX_REQUEST_BODY_BYTES:
            raise AuthError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "请求内容过大")
        if length == 0:
            return {}
        try:
            raw = self.rfile.read(length).decode("utf-8")
            payload = json.loads(raw) if raw.strip() else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise AuthError(HTTPStatus.BAD_REQUEST, "请求内容不是有效 JSON")
        if not isinstance(payload, dict):
            raise AuthError(HTTPStatus.BAD_REQUEST, "请求内容必须是 JSON 对象")
        return payload

    def do_OPTIONS(self):
        self._send_json({
            "ok": True
        }, HTTPStatus.NO_CONTENT, {
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Allow-Methods": "GET, POST, PATCH, OPTIONS",
        })

    def do_GET(self):
        try:
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path == "/health":
                self._send_json({"ok": True, "time": int(time.time())})
            elif path == "/auth/status":
                self._send_json(auth_status(self))
            else:
                platform_read_api.handle_get(
                    self,
                    self.path,
                    _read_api_context(),
                )
        except AuthError as exc:
            self._send_json(exc.payload, exc.status)
        except DiagnosticError as exc:
            self._send_json(exc.payload, exc.status)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self):
        try:
            data = self._body()
            platform_write_api.handle_post(
                self,
                self.path,
                data,
                _write_api_dependencies(),
            )
        except AuthError as exc:
            self._send_json(exc.payload, exc.status)
        except DiagnosticError as exc:
            self._send_json(exc.payload, exc.status)
        except PermissionError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.FORBIDDEN)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_PATCH(self):
        try:
            platform_write_api.handle_patch(
                self,
                self.path,
                _write_api_dependencies(),
            )
        except AuthError as exc:
            self._send_json(exc.payload, exc.status)
        except KeyError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def log_message(self, fmt, *args):
        print(f"[platform-api] {fmt % args}", flush=True)


if __name__ == "__main__":
    ensure_dirs()
    port = int(os.environ.get("PLATFORM_API_PORT", "9200"))
    # Threaded so a long "apply" (runs apply-env.sh, up to PLATFORM_APPLY_TIMEOUT)
    # doesn't freeze the console's status polls / other requests.
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[platform-api] listening on :{port}", flush=True)
    server.serve_forever()
