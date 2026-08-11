"""Platform API for event config, incidents, and network diagnostics.

This service is intentionally small. It owns the writable platform state while
the bigscreen remains a static UI served by nginx. Cisco Telnet uses the pinned
telnetlib3 compatibility module so the service also works on Python 3.13+.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import shlex
import secrets
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from telnetlib3.telnetlib import Telnet
except ImportError:  # Python 3.12 developer/test fallback; production pins telnetlib3.
    from telnetlib import Telnet

from platform_config import (
    ConfigSchemaError,
    default_config_text,
    dump_simple_yaml,
    inspect_config_schema,
    merge_env_file,
    migrate_config,
    parse_simple_yaml,
    read_env,
    render_env,
    stamp,
    validate_config,
)
from version_info import get_version_info
from platform_api import auth as platform_auth
from platform_api import incidents as platform_incidents
from platform_api import precheck as platform_precheck
from platform_api import read_api as platform_read_api
from platform_api import storage as platform_storage
from platform_api import transactions as platform_transactions
from platform_api import write_api as platform_write_api
from platform_api.settings import load_settings
from cisco_dhcp import (
    attach_dhcp_pool_exclusions,
    parse_cisco_arp_entries,
    parse_cisco_dhcp_bindings,
    parse_cisco_dhcp_conflicts,
    parse_cisco_dhcp_excluded,
    parse_cisco_dhcp_pools,
    parse_cisco_dhcp_statistics,
)


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
APPLY_CHILD_TIMEOUT_MARGIN_SECONDS = 30
APPLY_OPERATION_GRACE_SECONDS = 30
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
    history_writer=lambda action, actor, note, detail: append_history(
        action, actor, note, detail,
    ),
)
SESSIONS = AUTH_CONTEXT.sessions
AUTH_FAILURES = AUTH_CONTEXT.failures
AUTH_FAILURES_LOCK = AUTH_CONTEXT.failures_lock

CISCO_PROMPT_RE = br"(?m)^[A-Za-z0-9_.:/()\[\]-]+[>#][ \t]*\r?$"
CISCO_PRIV_PROMPT_RE = br"(?m)^[A-Za-z0-9_.:/()\[\]-]+#[ \t]*\r?$"
CISCO_USER_PROMPT_RE = br"(?m)^[A-Za-z0-9_.:/()\[\]-]+>[ \t]*\r?$"
CISCO_MORE_RE = br"(?i)--More--|<---\s*More\s*--->"


AuthError = platform_auth.AuthError
read_json_file = platform_storage.read_json_file
atomic_write_text = platform_storage.atomic_write_text
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
# Network diagnostics are deliberately manual and single-flight. This prevents
# accidental double-clicks from running two bandwidth tests or several CLI
# sessions against an older switch at the same time.
IPERF_LOCK = threading.Lock()
IPERF_STATUS_LOCK = threading.Lock()
IPERF_TASKS_LOCK = threading.Lock()
IPERF_PROCESS_LOCK = threading.Lock()
IPERF_TASKS: dict[str, dict] = {}
IPERF_CANCEL_EVENTS: dict[str, threading.Event] = {}
IPERF_PROCESSES: dict[str, subprocess.Popen] = {}
IPERF_ACTIVE_TASK_ID = ""
IPERF_HISTORY_LIMIT = 5
IPERF_STATUS: dict = {
    "ok": True,
    "state": "idle",
    "phase": "idle",
    "percent": 0,
    "message": "尚未开始测速",
}
# Only one switch session may run at a time. The short cache also collapses
# simultaneous requests from multiple browser tabs into one CLI query.
DHCP_LOCK = threading.Lock()
DHCP_CACHE: dict = {}


def dhcp_connection_settings() -> dict:
    """Return runtime Telnet settings, preferring the private console store."""
    stored = read_json_file(DHCP_SETTINGS_PATH, {})
    if not isinstance(stored, dict):
        stored = {}
    try:
        port = int(stored.get("port", DHCP_SWITCH_PORT))
    except (TypeError, ValueError):
        port = DHCP_SWITCH_PORT
    return {
        "username": str(stored.get("username", DHCP_SWITCH_USERNAME) or "").strip(),
        "password": str(stored.get("password", DHCP_SWITCH_PASSWORD) or ""),
        "enablePassword": str(stored.get("enablePassword", DHCP_SWITCH_ENABLE_PASSWORD) or ""),
        "port": max(1, min(65535, port)),
        "source": "console" if DHCP_SETTINGS_PATH.exists() else "environment",
    }


def _transaction_context() -> platform_transactions.TransactionContext:
    """Reflect compatibility globals that existing callers may override."""
    return platform_transactions.TransactionContext(
        config_path=CONFIG_PATH,
        env_path=ENV_PATH,
        transaction_dir=TRANSACTION_DIR,
        apply_status_dir=APPLY_STATUS_DIR,
        transaction_retention=TRANSACTION_RETENTION,
        apply_status_retention=APPLY_STATUS_RETENTION,
    )


new_operation_id = platform_transactions.new_operation_id
normalize_operation_id = platform_transactions.normalize_operation_id
prune_retained_paths = platform_transactions.prune_retained_paths
mark_config_snapshot_consumed = platform_transactions.mark_config_snapshot_consumed


def apply_status_path(operation_id: str) -> Path:
    return platform_transactions.apply_status_path(
        _transaction_context(), operation_id,
    )


def prune_generated_state() -> None:
    platform_transactions.prune_generated_state(_transaction_context())


def write_apply_status(operation_id: str, state: str, **detail) -> dict:
    return platform_transactions.write_apply_status(
        _transaction_context(), operation_id, state, **detail,
    )


def read_apply_status(operation_id: str) -> dict:
    return platform_transactions.read_apply_status(
        _transaction_context(), operation_id,
    )


def create_config_snapshot(
    action: str,
    actor: str = "",
    note: str = "",
) -> dict:
    return platform_transactions.create_config_snapshot(
        _transaction_context(), action, actor, note,
    )


def list_config_snapshots() -> list[Path]:
    return platform_transactions.list_config_snapshots(_transaction_context())


def restore_config_snapshot(directory: Path) -> dict:
    return platform_transactions.restore_config_snapshot(
        _transaction_context(), directory,
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


def read_config_text() -> str:
    if CONFIG_PATH.exists():
        return CONFIG_PATH.read_text(encoding="utf-8")
    return default_config_text(EXAMPLE_PATH)


def parse_config_text(text: str):
    config = parse_simple_yaml(text)
    if not isinstance(config, dict):
        raise ValueError("event config must be a mapping")
    return config


def _schema_response(status: dict) -> dict:
    return {
        "configSchemaOriginal": status["original_version"],
        "configSchemaCurrent": status["current_version"],
        "configSchemaSupported": status["current_supported"],
        "migrationRequired": status["migration_required"],
        "configTooNew": status["config_too_new"],
    }


def _schema_error_payload(message: str, config: dict | None = None) -> dict:
    return {
        "ok": False,
        "error": message,
        "config": config or {},
        "issues": [{"level": "bad", "path": "schema_version", "message": message}],
        "env": {},
        "normalizedText": "",
        "writeEnabled": False,
    }


def current_config_write_guard() -> dict | None:
    """Refuse every config mutation when the on-disk file is not writable by
    this schema version. The check happens before snapshots or target parsing so
    an older platform cannot replace a newer config with submitted schema 1."""
    if not CONFIG_PATH.exists():
        return None
    try:
        config = parse_config_text(CONFIG_PATH.read_text(encoding="utf-8"))
        status = inspect_config_schema(config)
    except (OSError, ValueError) as exc:
        return _schema_error_payload(f"Cannot modify event config: {exc}")
    if status["config_too_new"]:
        message = (
            f"Refusing to modify schema {status['original_version']}; "
            f"software supports schema {status['current_supported']}. "
            "Upgrade the monitoring platform first."
        )
        return {**_schema_error_payload(message, config), **_schema_response(status)}
    return None


def config_payload(text: str | None = None) -> dict:
    editing_existing = text is None
    text = read_config_text() if editing_existing else text
    config = parse_config_text(text)
    try:
        schema = inspect_config_schema(config)
    except ConfigSchemaError as exc:
        return {
            **_schema_error_payload(str(exc), config),
            "text": text,
            "paths": {
                "config": str(CONFIG_PATH),
                "env": str(ENV_PATH),
                "state": str(STATE_DIR),
            },
        }
    if schema["config_too_new"]:
        message = (
            f"event-config schema {schema['original_version']} is newer than supported "
            f"schema {schema['current_supported']}; upgrade the monitoring platform first"
        )
        return {
            **_schema_error_payload(message, config),
            **_schema_response(schema),
            "ok": editing_existing,
            "readOnly": True,
            "text": text,
            "paths": {
                "config": str(CONFIG_PATH),
                "env": str(ENV_PATH),
                "state": str(STATE_DIR),
            },
        }

    config = migrate_config(config)
    existing_env = read_env(ENV_PATH)
    # Migrate legacy .env-only application credentials into the authenticated
    # editor model.  They are then visible beside the old webhook token and are
    # persisted to event-config.yml on the next save/apply.  Do not do this for
    # submitted text: an operator must still be able to clear a credential.
    if editing_existing:
        alerts = config.setdefault("alerts", {})
        if isinstance(alerts, dict):
            for config_key, env_key in (
                ("feishu_app_id", "FEISHU_APP_ID"),
                ("feishu_app_secret", "FEISHU_APP_SECRET"),
                ("feishu_chat_id", "FEISHU_CHAT_ID"),
            ):
                if config_key not in alerts and existing_env.get(env_key):
                    alerts[config_key] = existing_env[env_key]
    issues = validate_config(config)
    env = render_env(config, existing_env)
    return {
        "ok": True,
        "text": text,
        "config": config,
        "normalizedText": dump_simple_yaml(config) + "\n",
        "issues": issues,
        "env": env,
        "writeEnabled": WRITE_ENABLED,
        **_schema_response(schema),
        "paths": {
            "config": str(CONFIG_PATH),
            "env": str(ENV_PATH),
            "state": str(STATE_DIR),
        },
    }


def version_payload() -> dict:
    payload = {"ok": True, **get_version_info()}
    try:
        config = parse_config_text(read_config_text())
        status = inspect_config_schema(config)
    except (OSError, ValueError) as exc:
        return {
            **payload,
            "config_schema_original": None,
            "config_schema_current": None,
            "migration_required": False,
            "config_too_new": False,
            "config_schema_error": str(exc),
        }
    return {
        **payload,
        "config_schema_original": status["original_version"],
        "config_schema_current": status["current_version"],
        "migration_required": status["migration_required"],
        "config_too_new": status["config_too_new"],
    }


def require_write() -> None:
    if not WRITE_ENABLED:
        raise PermissionError("platform write endpoints are disabled")


def _host_exec_env() -> dict:
    """Env for running apply-env from inside the container. Prefer the container's
    own binaries (python3, sed, ...) and only fall back to the host's for what the
    slim image lacks (docker) -- so /host/usr/bin goes LAST. Putting it first ran
    the host's dynamically-linked python3, which fails on missing libs here."""
    env = os.environ.copy()
    env["PATH"] = "/usr/local/bin:/usr/bin:/bin:/host/usr/bin"
    # apply-env runs inside platform-api for console applies. Recreating the
    # caller here would kill it before the durable operation result is written.
    # A direct host apply does not set this flag and therefore refreshes the API.
    env["PLATFORM_API_SELF_APPLY"] = "true"
    requested_check_timeout = env.get("DEPLOY_CHECK_TIMEOUT", "180")
    try:
        requested_check_seconds = max(0, int(requested_check_timeout))
    except ValueError:
        # Preserve deploy-check's existing validation and explicit diagnostic
        # for a malformed operator-provided value.
        pass
    else:
        child_maximum = max(0, APPLY_TIMEOUT - APPLY_CHILD_TIMEOUT_MARGIN_SECONDS)
        env["DEPLOY_CHECK_TIMEOUT"] = str(min(requested_check_seconds, child_maximum))
    plugin_dirs = ":".join([
        "/host/usr/libexec/docker/cli-plugins",
        "/host/usr/lib/docker/cli-plugins",
        "/host/usr/local/lib/docker/cli-plugins",
        env.get("DOCKER_CLI_PLUGIN_EXTRA_DIRS", ""),
    ]).strip(":")
    if plugin_dirs:
        env["DOCKER_CLI_PLUGIN_EXTRA_DIRS"] = plugin_dirs
    return env


def verify_runtime_after_apply() -> dict:
    """Wait until the user-facing core services answer after recreation."""
    checks = {
        "Prometheus": f"{PRECHECK_PROM_URL}/-/ready",
        "Grafana": f"{PRECHECK_GRAFANA_URL}/api/health",
        "告警服务": f"{BRIDGE_URL}/health",
        "大屏": f"{PRECHECK_BIGSCREEN_URL}/",
    }
    deadline = time.monotonic() + APPLY_VERIFY_TIMEOUT
    last_errors: dict[str, str] = {}
    while time.monotonic() < deadline:
        last_errors = {}
        for name, url in checks.items():
            try:
                with urllib.request.urlopen(url, timeout=5) as response:
                    response.read(4096)
                    if not 200 <= response.status < 400:
                        raise RuntimeError(f"HTTP {response.status}")
            except Exception as exc:
                last_errors[name] = str(exc)
        if not last_errors:
            return {"ok": True, "services": sorted(checks)}
        time.sleep(2)
    return {"ok": False, "errors": last_errors}


def _process_output_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _combined_process_output(*parts) -> str:
    return "\n".join(
        text for text in (_process_output_text(part) for part in parts) if text
    ).strip()


def apply_operation_timeout_seconds() -> int:
    """Upper bound for primary apply plus one deterministic recovery apply."""
    return 2 * (APPLY_TIMEOUT + APPLY_VERIFY_TIMEOUT) + APPLY_OPERATION_GRACE_SECONDS


def run_apply_command() -> dict:
    if not APPLY_ENABLED:
        return {
            "needsRedeploy": True,
            "nextStep": "cd librenms+grafana && ./apply-env.sh",
            "applyOutput": "automatic apply is disabled",
        }

    env = _host_exec_env()

    try:
        completed = subprocess.run(
            shlex.split(APPLY_COMMAND),
            cwd=str(WORKDIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=APPLY_TIMEOUT,
            check=False,
        )
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "error": "配置已写入，但自动应用失败：找不到 apply 命令",
            "needsRedeploy": True,
            "nextStep": "cd librenms+grafana && ./apply-env.sh",
            "applyOutput": str(exc),
        }
    except subprocess.TimeoutExpired as exc:
        output = _combined_process_output(exc.stdout, exc.stderr)
        return {
            "ok": False,
            "error": f"配置已写入，但自动应用超时（{APPLY_TIMEOUT}s）",
            "needsRedeploy": True,
            "nextStep": "cd librenms+grafana && ./apply-env.sh",
            "applyOutput": output[-4000:],
        }

    output = _combined_process_output(completed.stdout, completed.stderr)
    if completed.returncode != 0:
        return {
            "ok": False,
            "error": "配置已写入，但自动应用失败",
            "needsRedeploy": True,
            "nextStep": "cd librenms+grafana && ./apply-env.sh",
            "applyOutput": output[-4000:],
        }
    verification = verify_runtime_after_apply()
    if not verification.get("ok"):
        errors = "；".join(f"{name}: {message}" for name, message in verification.get("errors", {}).items())
        return {
            "ok": False,
            "error": "容器重建命令已完成，但关键服务未能恢复",
            "needsRedeploy": True,
            "nextStep": "cd librenms+grafana && ./apply-env.sh",
            "applyOutput": (output + "\n运行验证失败：" + errors)[-4000:],
            "verification": verification,
        }
    return {
        "applied": True,
        "needsRedeploy": False,
        "applyOutput": output[-4000:],
        "verification": verification,
    }


def save_config(text: str, actor: str = "", note: str = "") -> dict:
    require_write()
    blocked = current_config_write_guard()
    if blocked:
        return blocked
    payload = config_payload(text)
    if not payload.get("ok") or payload.get("configTooNew"):
        return payload
    bad = [item for item in payload["issues"] if item.get("level") == "bad"]
    if bad:
        return {**payload, "ok": False, "error": "config has blocking validation errors"}
    snapshot = create_config_snapshot("config.save", actor, note)
    atomic_write_text(CONFIG_PATH, payload["normalizedText"])
    append_history("config.save", actor, note, {"transactionId": snapshot["id"], "snapshot": snapshot["path"]})
    return {**config_payload(), "transactionId": snapshot["id"], "snapshot": snapshot["path"]}


def apply_config(text: str | None, actor: str = "", note: str = "", operation_id: str | None = None) -> dict:
    require_write()
    operation_id = normalize_operation_id(operation_id, "apply")
    blocked = current_config_write_guard()
    if blocked:
        return {**blocked, "operationId": operation_id}
    try:
        payload = config_payload(text) if text is not None else config_payload()
    except Exception as exc:
        return {"ok": False, "operationId": operation_id, "error": f"应用配置失败：{exc}"}
    if not payload.get("ok") or payload.get("configTooNew"):
        return {**payload, "operationId": operation_id}
    started_at = int(time.time())
    operation_timeout = apply_operation_timeout_seconds()
    write_apply_status(
        operation_id,
        "running",
        action="apply",
        startedAt=started_at,
        timeoutSeconds=operation_timeout,
        deadlineAt=started_at + operation_timeout,
    )
    snapshot = None
    try:
        bad = [item for item in payload["issues"] if item.get("level") == "bad"]
        if bad:
            result = {**payload, "ok": False, "error": "config has blocking validation errors", "operationId": operation_id}
            write_apply_status(operation_id, "failed", action="apply", error=result["error"])
            return result

        snapshot = create_config_snapshot("config.apply", actor, note)
        if text is not None or payload.get("migrationRequired"):
            atomic_write_text(CONFIG_PATH, payload["normalizedText"])
        rendered = merge_env_file(ENV_PATH, payload["env"])
        atomic_write_text(ENV_PATH, rendered)
        append_history("config.apply", actor, note, {
            "operationId": operation_id,
            "transactionId": snapshot["id"],
            "snapshot": snapshot["path"],
            "envKeys": sorted(payload["env"]),
        })
        apply_result = run_apply_command()
        failed = apply_result.get("ok") is False
        rollback_result = None
        restored = None
        if failed:
            restored = restore_config_snapshot(Path(snapshot["path"]))
            rollback_result = run_apply_command()
        append_history("config.apply_command", actor, note, {
            "operationId": operation_id,
            "transactionId": snapshot["id"],
            "applied": bool(apply_result.get("applied")),
            "needsRedeploy": bool(apply_result.get("needsRedeploy")),
            "error": apply_result.get("error", ""),
            "rolledBack": bool(restored),
            "runtimeRestored": bool(rollback_result and rollback_result.get("applied")),
        })
        if failed:
            result = {
                **config_payload(),
                **apply_result,
                "ok": False,
                "operationId": operation_id,
                "transactionId": snapshot["id"],
                "rolledBack": True,
                "restored": restored,
                "rollbackApply": rollback_result,
            }
            write_apply_status(
                operation_id,
                "failed",
                action="apply",
                error=apply_result.get("error", "应用失败"),
                rolledBack=True,
                runtimeRestored=bool(rollback_result and rollback_result.get("applied")),
                applyOutput=apply_result.get("applyOutput", ""),
            )
            return result

        state = "succeeded" if apply_result.get("applied") else "pending"
        status = write_apply_status(
            operation_id,
            state,
            action="apply",
            applied=bool(apply_result.get("applied")),
            needsRedeploy=bool(apply_result.get("needsRedeploy")),
            applyOutput=apply_result.get("applyOutput", ""),
        )
        return {
            **config_payload(),
            **apply_result,
            "operationId": operation_id,
            "transactionId": snapshot["id"],
            "state": status["state"],
        }
    except Exception as exc:
        restored = None
        rollback_result = None
        if snapshot:
            try:
                restored = restore_config_snapshot(Path(snapshot["path"]))
                rollback_result = run_apply_command()
            except Exception as rollback_exc:
                rollback_result = {"ok": False, "error": str(rollback_exc)}
        write_apply_status(
            operation_id,
            "failed",
            action="apply",
            error=str(exc),
            rolledBack=bool(restored),
            runtimeRestored=bool(rollback_result and rollback_result.get("applied")),
        )
        return {
            "ok": False,
            "operationId": operation_id,
            "error": f"应用配置失败：{exc}",
            "rolledBack": bool(restored),
            "rollbackApply": rollback_result,
        }


def append_history(action: str, actor: str, note: str, detail: dict) -> None:
    history_path = STATE_DIR / "history.json"
    history = read_json_file(history_path, [])
    history.insert(0, {
        "time": int(time.time()),
        "action": action,
        "actor": actor,
        "note": note,
        "detail": detail,
    })
    write_json_file(history_path, history[:200])


def rollback_config(actor: str = "", note: str = "", operation_id: str | None = None) -> dict:
    require_write()
    operation_id = normalize_operation_id(operation_id, "rollback")
    blocked = current_config_write_guard()
    if blocked:
        return {**blocked, "operationId": operation_id}
    started_at = int(time.time())
    operation_timeout = apply_operation_timeout_seconds()
    write_apply_status(
        operation_id,
        "running",
        action="rollback",
        startedAt=started_at,
        timeoutSeconds=operation_timeout,
        deadlineAt=started_at + operation_timeout,
    )
    snapshots = list_config_snapshots()
    if not snapshots:
        error_message = "没有可用的一致性配置快照；旧版分散备份不会自动混合回滚"
        write_apply_status(operation_id, "failed", action="rollback", error=error_message)
        return {"ok": False, "operationId": operation_id, "error": error_message}

    target = snapshots[0]
    guard = create_config_snapshot("config.rollback.guard", actor, note)
    try:
        restored = restore_config_snapshot(target)
        apply_result = run_apply_command()
        if apply_result.get("ok") is False:
            restore_config_snapshot(Path(guard["path"]))
            recovery_result = run_apply_command()
            error_message = apply_result.get("error", "回滚后的服务应用失败")
            append_history("config.rollback_failed", actor, note, {
                "operationId": operation_id,
                "targetTransactionId": restored.get("transactionId"),
                "guardTransactionId": guard["id"],
                "error": error_message,
                "runtimeRestored": bool(recovery_result.get("applied")),
            })
            write_apply_status(
                operation_id,
                "failed",
                action="rollback",
                error=error_message,
                rolledBack=True,
                runtimeRestored=bool(recovery_result.get("applied")),
            )
            return {
                **config_payload(),
                "ok": False,
                "operationId": operation_id,
                "error": error_message,
                "rolledBack": True,
                "rollbackApply": recovery_result,
            }

        state = "succeeded" if apply_result.get("applied") else "pending"
        mark_config_snapshot_consumed(target)
        append_history("config.rollback", actor, note, {
            "operationId": operation_id,
            "targetTransactionId": restored.get("transactionId"),
            "guardTransactionId": guard["id"],
            "restored": restored,
            "applied": bool(apply_result.get("applied")),
        })
        write_apply_status(
            operation_id,
            state,
            action="rollback",
            applied=bool(apply_result.get("applied")),
            needsRedeploy=bool(apply_result.get("needsRedeploy")),
            restored=restored,
            applyOutput=apply_result.get("applyOutput", ""),
        )
        return {
            **config_payload(),
            **apply_result,
            "operationId": operation_id,
            "restored": restored,
            "state": state,
        }
    except Exception as exc:
        try:
            restore_config_snapshot(Path(guard["path"]))
        except Exception:
            pass
        write_apply_status(operation_id, "failed", action="rollback", error=str(exc))
        return {"ok": False, "operationId": operation_id, "error": f"回滚失败：{exc}"}


def bridge_retire_pending() -> dict:
    """Fetch the bridge's pending-delete device list (48h+ offline, unconfirmed)."""
    try:
        with urllib.request.urlopen(f"{BRIDGE_URL}/retire/pending", timeout=8) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except Exception as exc:
        return {"ok": False, "error": f"无法连接告警服务：{exc}", "pending": []}


def bridge_retire_resolve(data: dict) -> dict:
    """Forward a confirm/keep decision to the bridge (which owns the state)."""
    payload = json.dumps({
        "key": str(data.get("key") or ""),
        "action": str(data.get("action") or ""),
        "token": str(data.get("token") or ""),
    }).encode("utf-8")
    request_obj = urllib.request.Request(
        f"{BRIDGE_URL}/retire/resolve", data=payload, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request_obj, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8", errors="replace") or "{}")
        except json.JSONDecodeError:
            return {"ok": False, "error": f"告警服务返回 HTTP {exc.code}"}
    except Exception as exc:
        return {"ok": False, "error": f"无法连接告警服务：{exc}"}


def send_test_alert() -> dict:
    """Ask the Feishu bridge to push a test card, so operators can confirm the
    alert path works before an event without waiting for a real incident."""
    request = urllib.request.Request(
        f"{BRIDGE_URL}/test-alert", data=b"{}", method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except Exception as exc:
        return {"ok": False, "error": f"无法连接告警服务：{exc}"}


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


def _iperf_target_is_internal(host: str) -> bool:
    """True when the target is (or resolves to) a non-public address.

    覆盖私网/环回/链路本地/保留/组播/未指定地址；域名会先解析再逐个地址判断，
    防止用一个解析到内网的域名绕过。解析失败按"非内网"放行——反正 iperf3
    连不上会给出明确报错，这里不用抢先拦。
    """
    def non_public(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        return (
            addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_reserved or addr.is_multicast or addr.is_unspecified
        )

    try:
        return non_public(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    for info in infos:
        try:
            if non_public(ipaddress.ip_address(info[4][0])):
                return True
        except ValueError:
            continue
    return False


def configured_core_switch_host() -> str:
    """Return the one configured core switch IP used by the DHCP dashboard."""
    config = parse_config_text(read_config_text())
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


def get_dhcp_settings() -> dict:
    settings = dhcp_connection_settings()
    return {
        "ok": True,
        "host": configured_core_switch_host(),
        "username": settings["username"],
        "port": settings["port"],
        "passwordConfigured": bool(settings["password"]),
        "enablePasswordConfigured": bool(settings["enablePassword"]),
        "source": settings["source"],
        "timeoutSeconds": DHCP_SWITCH_TIMEOUT,
        "refreshSeconds": DHCP_REFRESH_SECONDS,
    }


def save_dhcp_settings(data: dict) -> dict:
    if not WRITE_ENABLED:
        raise DiagnosticError(HTTPStatus.FORBIDDEN, "当前环境不允许保存 Telnet 配置")
    current = dhcp_connection_settings()
    username = str(data.get("username", current["username"]) or "").strip()
    password_input = data.get("password")
    enable_input = data.get("enablePassword")
    password = current["password"] if password_input in (None, "") else str(password_input)
    enable_password = current["enablePassword"] if enable_input in (None, "") else str(enable_input)
    try:
        port = int(data.get("port", current["port"]))
    except (TypeError, ValueError):
        raise DiagnosticError(HTTPStatus.BAD_REQUEST, "Telnet 端口必须是数字")
    if not 1 <= port <= 65535:
        raise DiagnosticError(HTTPStatus.BAD_REQUEST, "Telnet 端口必须在 1-65535 之间")
    if len(username) > 128:
        raise DiagnosticError(HTTPStatus.BAD_REQUEST, "Telnet 用户名过长")
    if len(password) > 512 or len(enable_password) > 512:
        raise DiagnosticError(HTTPStatus.BAD_REQUEST, "Telnet 密码过长")
    # 凭据会被原样写进 Telnet 会话，换行/控制字符等于向交换机注入额外命令行。
    for value in (username, password, enable_password):
        if any(ord(ch) < 0x20 or ch == "\x7f" for ch in value):
            raise DiagnosticError(HTTPStatus.BAD_REQUEST, "Telnet 凭据不能包含换行或控制字符")
    write_json_file(DHCP_SETTINGS_PATH, {
        "username": username,
        "password": password,
        "enablePassword": enable_password,
        "port": port,
        "updatedAt": int(time.time()),
    }, mode=0o600)
    try:
        os.chmod(DHCP_SETTINGS_PATH, 0o600)
    except OSError as exc:
        print(f"[platform-api] dhcp settings chmod failed: {exc}", flush=True)
    DHCP_CACHE.clear()
    return get_dhcp_settings()


def _telnet_expect(session, patterns: list[bytes], step: str):
    index, match, output = session.expect(patterns, DHCP_SWITCH_TIMEOUT)
    decoded = (output or b"").decode("utf-8", errors="replace")
    if index < 0:
        raise DiagnosticError(HTTPStatus.BAD_GATEWAY, f"核心交换机 Telnet {step}超时")
    return index, match, decoded


def _telnet_command(session, command: str) -> str:
    session.write(command.encode("ascii") + b"\n")
    chunks = []
    for _page in range(100):
        index, _match, output = _telnet_expect(
            session,
            [CISCO_PROMPT_RE, CISCO_MORE_RE],
            f"执行 {command} ",
        )
        chunks.append(output)
        if index == 0:
            break
        session.write(b" ")
    else:
        raise DiagnosticError(HTTPStatus.BAD_GATEWAY, "核心交换机分页输出超过安全上限")
    output = "".join(chunks)
    output = re.sub(r"(?i)--More--|<---\s*More\s*--->", "", output)
    output = output.replace("\x08", "")
    lines = output.replace("\r", "").splitlines()
    if lines and lines[0].strip() == command:
        lines.pop(0)
    if lines and re.fullmatch(r"[A-Za-z0-9_.:/()\[\]-]+[>#]\s*", lines[-1]):
        lines.pop()
    cleaned = "\n".join(lines).strip()
    return cleaned


def _open_cisco_telnet(host: str):
    settings = dhcp_connection_settings()
    username = settings["username"]
    password = settings["password"]
    enable_password = settings["enablePassword"]
    if not password:
        raise DiagnosticError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "尚未配置核心交换机 Telnet 密码，请先在赛事控制台填写",
        )
    session = Telnet(host, settings["port"], DHCP_SWITCH_TIMEOUT)
    username_prompt = br"(?im)^(?:user ?name|login):[ \t]*\r?$"
    password_prompt = br"(?im)^password:[ \t]*\r?$"
    command_prompt = CISCO_PROMPT_RE
    failed_prompt = br"(?i)(?:login invalid|authentication failed|access denied)"
    index, _match, _output = _telnet_expect(
        session,
        [username_prompt, password_prompt, command_prompt, failed_prompt],
        "登录",
    )
    if index == 3:
        raise DiagnosticError(HTTPStatus.BAD_GATEWAY, "核心交换机拒绝 Telnet 登录")
    if index == 0:
        if not username:
            raise DiagnosticError(HTTPStatus.SERVICE_UNAVAILABLE, "交换机要求用户名，但尚未配置 Telnet 用户名")
        session.write(username.encode("utf-8") + b"\n")
        index, _match, _output = _telnet_expect(
            session,
            [password_prompt, command_prompt, failed_prompt],
            "用户名验证",
        )
        if index == 2:
            raise DiagnosticError(HTTPStatus.BAD_GATEWAY, "核心交换机拒绝 Telnet 用户名")
        if index == 1:
            return session
        index = 0
    if index in (0, 1):
        session.write(password.encode("utf-8") + b"\n")
        index, match, _output = _telnet_expect(
            session,
            [command_prompt, failed_prompt, password_prompt],
            "密码验证",
        )
        if index != 0:
            raise DiagnosticError(HTTPStatus.BAD_GATEWAY, "核心交换机 Telnet 密码错误")
        prompt = (match.group(0) if match else b"").strip()
        if prompt.endswith(b">") and enable_password:
            session.write(b"enable\n")
            enable_index, _match, _output = _telnet_expect(
                session,
                [password_prompt, CISCO_PRIV_PROMPT_RE, failed_prompt, CISCO_USER_PROMPT_RE],
                "进入特权模式",
            )
            if enable_index == 0:
                session.write(enable_password.encode("utf-8") + b"\n")
                enable_index, _match, _output = _telnet_expect(
                    session,
                    [CISCO_PRIV_PROMPT_RE, failed_prompt, password_prompt, CISCO_USER_PROMPT_RE],
                    "特权密码验证",
                )
            elif enable_index == 1:
                enable_index = 0
            if enable_index != 0:
                raise DiagnosticError(HTTPStatus.BAD_GATEWAY, "核心交换机 Enable 密码错误")
    return session


def collect_cisco_dhcp(host: str) -> dict:
    session = None
    warnings: list[str] = []
    try:
        session = _open_cisco_telnet(host)
        _telnet_command(session, "terminal length 0")
        pool_output = _telnet_command(session, "show ip dhcp pool")
        if re.search(r"(?im)^\s*%\s*(?:Invalid input|Unknown command)", pool_output):
            raise DiagnosticError(HTTPStatus.BAD_GATEWAY, "核心交换机不支持 show ip dhcp pool")

        optional_outputs = {}
        for key, command in (
            ("conflicts", "show ip dhcp conflict"),
            ("statistics", "show ip dhcp server statistics"),
            ("excluded", "show running-config | include ^ip dhcp excluded-address"),
        ):
            output = _telnet_command(session, command)
            if re.search(r"(?im)^\s*%\s*(?:Invalid input|Unknown command)", output):
                warnings.append(f"交换机不支持 {command}")
                output = ""
            optional_outputs[key] = output
        pools = parse_cisco_dhcp_pools(pool_output)
        conflicts = parse_cisco_dhcp_conflicts(optional_outputs["conflicts"])
        statistics = parse_cisco_dhcp_statistics(optional_outputs["statistics"])
        excluded_addresses = parse_cisco_dhcp_excluded(optional_outputs["excluded"])
        attach_dhcp_pool_exclusions(pools, excluded_addresses)
        total = sum(pool["total"] for pool in pools)
        leased = sum(pool["leased"] for pool in pools)
        excluded = sum(pool["excluded"] for pool in pools)
        usable = max(0, total - excluded)
        return {
            "ok": True,
            "host": host,
            "source": "devices.core.ip",
            "pools": pools,
            "conflicts": conflicts,
            "excludedAddresses": excluded_addresses,
            "statistics": statistics,
            "summary": {
                "poolCount": len(pools),
                "total": total,
                "leased": leased,
                "excluded": excluded,
                "available": max(0, usable - leased),
                "utilization": round((leased / usable * 100) if usable else 0, 1),
                "conflictCount": len(conflicts),
            },
            "warnings": warnings,
        }
    except DiagnosticError:
        raise
    except (EOFError, OSError) as exc:
        raise DiagnosticError(HTTPStatus.BAD_GATEWAY, f"无法读取核心交换机 DHCP：{exc}")
    finally:
        if session is not None:
            try:
                session.write(b"exit\n")
                session.close()
            except Exception:
                pass


def get_dhcp_bindings() -> dict:
    """Read exact leases and current ARP neighbours on operator request."""
    host = configured_core_switch_host()
    if not DHCP_LOCK.acquire(blocking=False):
        raise DiagnosticError(HTTPStatus.CONFLICT, "DHCP 面板正在读取交换机，请稍后再查询已用 IP")
    session = None
    try:
        session = _open_cisco_telnet(host)
        _telnet_command(session, "terminal length 0")
        output = _telnet_command(session, "show ip dhcp binding")
        if re.search(r"(?im)^\s*%\s*(?:Invalid input|Unknown command)", output):
            raise DiagnosticError(HTTPStatus.BAD_GATEWAY, "核心交换机不支持 show ip dhcp binding")
        bindings = parse_cisco_dhcp_bindings(output)
        arp_output = _telnet_command(session, "show ip arp")
        arp_warning = ""
        if re.search(r"(?im)^\s*%\s*(?:Invalid input|Unknown command)", arp_output):
            arp_output = _telnet_command(session, "show arp")
        if re.search(r"(?im)^\s*%\s*(?:Invalid input|Unknown command)", arp_output):
            arp_output = ""
            arp_warning = "交换机不支持读取 ARP 表，无法判断固定排除地址是否正在使用"
        arp_entries = parse_cisco_arp_entries(arp_output)
        return {
            "ok": True,
            "host": host,
            "bindings": bindings,
            "usedAddresses": [item["ip"] for item in bindings],
            "arpEntries": arp_entries,
            "observedAddresses": [item["ip"] for item in arp_entries],
            "parserWarning": (
                "show ip dhcp binding 当前未返回可解析的活动地址"
                if not bindings else ""
            ),
            "arpWarning": arp_warning,
            "capturedAt": int(time.time()),
        }
    except DiagnosticError:
        raise
    except (EOFError, OSError) as exc:
        raise DiagnosticError(HTTPStatus.BAD_GATEWAY, f"无法读取核心交换机 DHCP 租约：{exc}")
    finally:
        if session is not None:
            try:
                session.write(b"exit\n")
                session.close()
            except Exception:
                pass
        DHCP_LOCK.release()


def test_dhcp_connection() -> dict:
    """Test the configured core switch login without collecting DHCP data."""
    host = configured_core_switch_host()
    if not DHCP_LOCK.acquire(blocking=False):
        raise DiagnosticError(HTTPStatus.CONFLICT, "DHCP 面板正在读取交换机，请稍后再测试连接")
    session = None
    started = time.monotonic()
    try:
        session = _open_cisco_telnet(host)
        privilege_output = _telnet_command(session, "show privilege")
        match = re.search(r"(?i)privilege\s+level\s+(?:is\s+)?(\d+)", privilege_output)
        privilege_level = int(match.group(1)) if match else None
        privileged = privilege_level == 15
        if privilege_level is None:
            message = "Telnet 登录成功，交换机未返回权限级别"
        elif privileged:
            message = "Telnet 登录成功，已进入特权模式"
        else:
            message = f"Telnet 登录成功，当前权限级别 {privilege_level}"
        settings = dhcp_connection_settings()
        return {
            "ok": True,
            "host": host,
            "port": settings["port"],
            "login": True,
            "privileged": privileged,
            "privilegeLevel": privilege_level,
            "latencyMs": round((time.monotonic() - started) * 1000),
            "message": message,
            "testedAt": int(time.time()),
        }
    except DiagnosticError:
        raise
    except (EOFError, OSError) as exc:
        raise DiagnosticError(HTTPStatus.BAD_GATEWAY, f"无法连接核心交换机 Telnet：{exc}")
    finally:
        if session is not None:
            try:
                session.write(b"exit\n")
                session.close()
            except Exception:
                pass
        DHCP_LOCK.release()


def _cached_dhcp_payload(refreshing: bool = False) -> dict | None:
    payload = DHCP_CACHE.get("payload")
    if not payload:
        return None
    age = max(0, time.monotonic() - float(DHCP_CACHE.get("monotonic") or 0))
    return {**payload, "cached": True, "cacheAgeSeconds": round(age, 1), "refreshing": refreshing}


def get_dhcp_dashboard(force: bool = False) -> dict:
    host = configured_core_switch_host()
    cached = _cached_dhcp_payload()
    cache_seconds = max(10, DHCP_REFRESH_SECONDS - 5)
    # Even the manual refresh button cannot create more than one switch session
    # every 30 seconds. This keeps the read-only endpoint harmless if a browser
    # is double-clicked or several operators open it together.
    hard_minimum_seconds = 30
    if (
        cached
        and cached.get("host") == host
        and (
            cached.get("cacheAgeSeconds", cache_seconds) < hard_minimum_seconds
            or (not force and cached.get("cacheAgeSeconds", cache_seconds) < cache_seconds)
        )
    ):
        return cached
    if not DHCP_LOCK.acquire(blocking=False):
        busy = _cached_dhcp_payload(refreshing=True)
        if busy and busy.get("host") == host:
            return busy
        raise DiagnosticError(HTTPStatus.CONFLICT, "DHCP 面板正在刷新，请稍后再试")
    try:
        # Recheck after acquiring the lock in case another request just finished.
        cached = _cached_dhcp_payload()
        if (
            cached
            and cached.get("host") == host
            and (
                cached.get("cacheAgeSeconds", cache_seconds) < hard_minimum_seconds
                or (not force and cached.get("cacheAgeSeconds", cache_seconds) < cache_seconds)
            )
        ):
            return cached
        collection_started = time.monotonic()
        payload = {
            **collect_cisco_dhcp(host),
            "capturedAt": int(time.time()),
            "collectionSeconds": round(time.monotonic() - collection_started, 2),
            "refreshSeconds": DHCP_REFRESH_SECONDS,
            "cached": False,
            "cacheAgeSeconds": 0,
            "refreshing": False,
        }
        DHCP_CACHE.clear()
        DHCP_CACHE.update({"payload": payload, "monotonic": time.monotonic()})
        return payload
    finally:
        DHCP_LOCK.release()


def parse_port_range(value, default: str = "5201-5210", max_ports: int = 10) -> list[int]:
    text = str(value if value not in (None, "") else default).strip()
    match = re.fullmatch(r"(\d{1,5})(?:\s*-\s*(\d{1,5}))?", text)
    if not match:
        raise DiagnosticError(HTTPStatus.BAD_REQUEST, "端口应为单个端口或范围，例如 5201-5210")
    start = int(match.group(1))
    end = int(match.group(2) or start)
    if not (1 <= start <= end <= 65535):
        raise DiagnosticError(HTTPStatus.BAD_REQUEST, "端口范围无效")
    if end - start + 1 > max_ports:
        raise DiagnosticError(HTTPStatus.BAD_REQUEST, f"一次最多尝试 {max_ports} 个端口")
    return list(range(start, end + 1))


def parse_iperf3_json(text: str) -> dict:
    raw = str(text or "").strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("iperf3 未返回可解析的 JSON")
        payload = json.loads(raw[start:end + 1])
    # 合法但非对象的 JSON（裸数组/数字/被代理截断的响应）必须走 ValueError，
    # 否则 AttributeError 会越过调用方的逐端口重试直接把整次测速打成 500。
    if not isinstance(payload, dict):
        raise ValueError("iperf3 返回的 JSON 不是对象")
    if payload.get("error"):
        raise ValueError(str(payload["error"]))

    def _as_dict(value) -> dict:
        return value if isinstance(value, dict) else {}

    ending = _as_dict(payload.get("end"))
    received = _as_dict(ending.get("sum_received"))
    sent = _as_dict(ending.get("sum_sent"))
    fallback = _as_dict(ending.get("sum"))
    bits_per_second = received.get("bits_per_second")
    if bits_per_second is None:
        bits_per_second = sent.get("bits_per_second", fallback.get("bits_per_second"))
    if bits_per_second is None:
        raise ValueError("iperf3 结果中没有速率数据")

    def endpoint_stats(value: dict) -> dict:
        return {
            "mbps": round(float(value.get("bits_per_second") or 0) / 1_000_000, 2),
            "bytes": int(value.get("bytes") or 0),
            "seconds": round(float(value.get("seconds") or 0), 2),
            "retransmits": int(value.get("retransmits") or 0),
        }

    intervals = []
    for item in payload.get("intervals") or []:
        if not isinstance(item, dict):
            continue
        interval = _as_dict(item.get("sum"))
        if not interval and item.get("streams"):
            streams = [stream for stream in item["streams"] if isinstance(stream, dict)]
        else:
            streams = []
        if not interval and streams:
            interval = {
                "start": min(float(stream.get("start") or 0) for stream in streams),
                "end": max(float(stream.get("end") or 0) for stream in streams),
                "seconds": max(float(stream.get("seconds") or 0) for stream in streams),
                "bytes": sum(int(stream.get("bytes") or 0) for stream in streams),
                "bits_per_second": sum(float(stream.get("bits_per_second") or 0) for stream in streams),
                "retransmits": sum(int(stream.get("retransmits") or 0) for stream in streams),
            }
        if not interval:
            continue
        intervals.append({
            "start": round(float(interval.get("start") or 0), 2),
            "end": round(float(interval.get("end") or 0), 2),
            "seconds": round(float(interval.get("seconds") or 0), 2),
            "bytes": int(interval.get("bytes") or 0),
            "mbps": round(float(interval.get("bits_per_second") or 0) / 1_000_000, 2),
            "retransmits": int(interval["retransmits"]) if interval.get("retransmits") is not None else None,
        })

    sender = endpoint_stats(sent or fallback)
    receiver = endpoint_stats(received or fallback)
    return {
        "mbps": round(float(bits_per_second) / 1_000_000, 2),
        "seconds": round(float(received.get("seconds") or sent.get("seconds") or fallback.get("seconds") or 0), 2),
        "retransmits": int(sent.get("retransmits") or 0),
        "bytes": receiver["bytes"],
        "sender": sender,
        "receiver": receiver,
        "intervals": intervals,
    }


def _set_iperf_status(**updates) -> None:
    with IPERF_STATUS_LOCK:
        IPERF_STATUS.update(updates)
        task_id = str(IPERF_STATUS.get("taskId") or "")
        snapshot = dict(IPERF_STATUS)
    if task_id:
        with IPERF_TASKS_LOCK:
            if task_id in IPERF_TASKS:
                IPERF_TASKS[task_id] = snapshot


def _public_iperf_payload(payload: dict) -> dict:
    payload = dict(payload or {})
    started = payload.pop("_startedMonotonic", None)
    if started is not None:
        payload["elapsedSeconds"] = round(max(0, time.monotonic() - started), 1)
    else:
        payload.setdefault("elapsedSeconds", 0)
    return payload


def iperf_status_payload(task_id: str = "") -> dict:
    task_id = str(task_id or "").strip()
    if task_id:
        with IPERF_TASKS_LOCK:
            task = dict(IPERF_TASKS.get(task_id) or {})
        if not task:
            # Completed task summaries survive a platform-api restart.  This
            # keeps history buttons useful without retaining an unbounded
            # in-memory task dictionary.
            history = read_json_file(IPERF_HISTORY_PATH, [])
            if isinstance(history, list):
                task = next((dict(item) for item in history
                             if isinstance(item, dict) and item.get("taskId") == task_id), {})
            if not task:
                raise DiagnosticError(HTTPStatus.NOT_FOUND, "iPerf3 任务不存在或已过期")
        return _public_iperf_payload(task)
    with IPERF_STATUS_LOCK:
        payload = dict(IPERF_STATUS)
    return _public_iperf_payload(payload)


def iperf_history_payload() -> dict:
    history = read_json_file(IPERF_HISTORY_PATH, [])
    if not isinstance(history, list):
        history = []
    return {"ok": True, "history": history[:IPERF_HISTORY_LIMIT]}


def _save_iperf_history(payload: dict) -> None:
    history = read_json_file(IPERF_HISTORY_PATH, [])
    if not isinstance(history, list):
        history = []
    summary = {
        "taskId": payload.get("taskId"),
        "startedAt": payload.get("startedAt"),
        "finishedAt": payload.get("finishedAt"),
        "state": payload.get("state"),
        "server": payload.get("server"),
        "requestedPorts": payload.get("requestedPorts"),
        "duration": payload.get("duration"),
        "parallel": payload.get("parallel"),
        "results": payload.get("results") or [],
        "message": payload.get("message") or "",
    }
    history = [summary, *[
        item for item in history
        if isinstance(item, dict) and item.get("taskId") != summary["taskId"]
    ]]
    write_json_file(IPERF_HISTORY_PATH, history[:IPERF_HISTORY_LIMIT], mode=0o600)


def _iperf_error_summary(stdout: str, stderr: str, returncode: int) -> str:
    raw = (stderr or stdout or f"退出码 {returncode}").strip()
    try:
        payload = json.loads(raw)
        raw = str(payload.get("error") or raw)
    except (json.JSONDecodeError, TypeError):
        pass
    lowered = raw.lower()
    if "control socket has closed unexpectedly" in lowered:
        return "服务器中途关闭连接"
    if "server is busy" in lowered:
        return "服务器正忙"
    if "unable to connect" in lowered or "connection refused" in lowered:
        return "无法连接"
    return re.sub(r"\s+", " ", raw)[-160:]


class IperfCancelled(Exception):
    pass


def _execute_iperf_command(command: list[str], timeout: float, task_id: str = "",
                           cancel_event: threading.Event | None = None):
    """Run one iperf process and make only managed background tasks stoppable.

    Direct calls retain ``subprocess.run`` for compatibility with the parser's
    unit tests. Browser-started tasks use Popen so the stop endpoint can
    terminate exactly that task without touching any other process.
    """
    if not task_id:
        return subprocess.run(
            command,
            cwd=str(WORKDIR),
            env=_host_exec_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    # A 30-second, 20-stream JSON result can exceed an OS pipe buffer. Waiting
    # for process exit before communicate() would then deadlock. Temporary
    # files keep output bounded by disk/state resources while preserving stop.
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stdout_handle, \
            tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr_handle:
        process = subprocess.Popen(
            command,
            cwd=str(WORKDIR),
            env=_host_exec_env(),
            stdout=stdout_handle,
            stderr=stderr_handle,
            text=True,
        )
        with IPERF_PROCESS_LOCK:
            IPERF_PROCESSES[task_id] = process
        end = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if cancel_event and cancel_event.is_set():
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    raise IperfCancelled("测速已停止")
                if time.monotonic() >= end:
                    process.kill()
                    process.wait(timeout=2)
                    raise subprocess.TimeoutExpired(command, timeout)
                time.sleep(0.1)
            if cancel_event and cancel_event.is_set():
                raise IperfCancelled("测速已停止")
            stdout_handle.seek(0)
            stderr_handle.seek(0)
            return subprocess.CompletedProcess(
                command,
                process.returncode,
                stdout_handle.read(),
                stderr_handle.read(),
            )
        finally:
            with IPERF_PROCESS_LOCK:
                if IPERF_PROCESSES.get(task_id) is process:
                    IPERF_PROCESSES.pop(task_id, None)


def _run_iperf_direction(host: str, ports: list[int], duration: int, parallel: int, reverse: bool,
                         deadline: float, direction_index: int, direction_total: int,
                         task_id: str = "", cancel_event: threading.Event | None = None) -> dict:
    attempts: list[str] = []
    direction_name = "download" if reverse else "upload"
    direction_label = "下载" if reverse else "上传"
    for attempt_index, port in enumerate(ports, 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        progress = ((direction_index + (attempt_index - 1) / max(1, len(ports))) / direction_total) * 100
        _set_iperf_status(
            state="running",
            phase=direction_name,
            direction=direction_name,
            currentPort=port,
            attempt=attempt_index,
            totalAttempts=len(ports),
            percent=round(progress, 1),
            message=f"正在测试{direction_label}，端口 {port}（第 {attempt_index}/{len(ports)} 个）",
        )
        command = [
            *shlex.split(IPERF3_COMMAND),
            "-c", host,
            "-p", str(port),
            "--connect-timeout", str(IPERF3_CONNECT_TIMEOUT_MS),
            "-t", str(duration),
            "-P", str(parallel),
            "-J",
        ]
        if reverse:
            command.append("-R")
        try:
            completed = _execute_iperf_command(
                command, max(1, min(duration + 5, remaining)), task_id, cancel_event,
            )
        except FileNotFoundError:
            raise DiagnosticError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "找不到 iPerf3 客户端，请重新运行 deploy.sh 构建 platform-api 镜像",
            )
        except subprocess.TimeoutExpired:
            attempts.append(f"{port}: 超时")
            continue

        output = (completed.stdout or "").strip()
        error = (completed.stderr or "").strip()
        if completed.returncode == 0:
            try:
                result = parse_iperf3_json(output)
                _set_iperf_status(
                    percent=round(((direction_index + 1) / direction_total) * 100, 1),
                    message=f"{direction_label}完成，端口 {port}",
                )
                return {**result, "port": port}
            except (ValueError, TypeError) as exc:
                attempts.append(f"{port}: {exc}")
        else:
            attempts.append(f"{port}: {_iperf_error_summary(output, error, completed.returncode)}")
    detail = "；".join(attempts[-4:]) or "没有端口完成测试"
    raise DiagnosticError(HTTPStatus.BAD_GATEWAY, f"iperf3 测速失败：{detail}")


def run_iperf_test(data: dict, task_id: str = "", cancel_event: threading.Event | None = None) -> dict:
    host = validate_network_host(
        data.get("server") or "speedtest.hkg12.hk.leaseweb.net",
        "测速服务器",
    )
    if not IPERF3_ALLOW_INTERNAL and _iperf_target_is_internal(host):
        raise DiagnosticError(
            HTTPStatus.BAD_REQUEST,
            "测速目标是内网地址。默认仅允许公网节点；确需测内网请在 .env 设置 "
            "PLATFORM_IPERF3_ALLOW_INTERNAL=true 后重新应用配置",
        )
    ports = parse_port_range(data.get("ports"), "5201-5210", 10)
    try:
        duration = int(data.get("duration") or 10)
        parallel = int(data.get("parallel") or 10)
    except (TypeError, ValueError):
        raise DiagnosticError(HTTPStatus.BAD_REQUEST, "测试时长和并发数必须是整数")
    if not 3 <= duration <= 30:
        raise DiagnosticError(HTTPStatus.BAD_REQUEST, "测试时长必须在 3-30 秒之间")
    if not 1 <= parallel <= 20:
        raise DiagnosticError(HTTPStatus.BAD_REQUEST, "并发数必须在 1-20 之间")
    direction = str(data.get("direction") or "both").strip().lower()
    if direction not in ("upload", "download", "both"):
        raise DiagnosticError(HTTPStatus.BAD_REQUEST, "测速方向无效")
    if not IPERF_LOCK.acquire(blocking=False):
        raise DiagnosticError(HTTPStatus.CONFLICT, "已有 iperf3 测速正在运行，请稍后再试")
    directions = []
    if direction in ("upload", "both"):
        directions.append(("upload", False))
    if direction in ("download", "both"):
        directions.append(("download", True))
    started_monotonic = time.monotonic()
    deadline = started_monotonic + IPERF3_TIMEOUT
    _set_iperf_status(
        ok=True,
        state="running",
        phase="preparing",
        server=host,
        currentPort=None,
        attempt=0,
        totalAttempts=len(ports),
        direction="",
        directionIndex=0,
        directionTotal=len(directions),
        percent=0,
        startedAt=int(time.time()),
        finishedAt=None,
        _startedMonotonic=started_monotonic,
        elapsedSeconds=0,
        # One cap covers the entire task. A blocked public node must not consume
        # the timeout once for upload and then a second time for download.
        maxSeconds=IPERF3_TIMEOUT,
        message="正在准备测速",
        taskId=task_id or None,
    )
    try:
        results = []
        preferred_ports = list(ports)
        for direction_index, (direction_name, reverse) in enumerate(directions):
            _set_iperf_status(directionIndex=direction_index + 1)
            result = _run_iperf_direction(
                host, preferred_ports, duration, parallel, reverse, deadline,
                direction_index, len(directions), task_id, cancel_event,
            )
            results.append({"direction": direction_name, **result})
            preferred_ports = [result["port"], *[port for port in ports if port != result["port"]]]
        payload = {
            "ok": True,
            "protocol": "TCP",
            "server": host,
            "requestedPorts": f"{ports[0]}-{ports[-1]}" if len(ports) > 1 else str(ports[0]),
            "duration": duration,
            "parallel": parallel,
            "results": results,
            "taskId": task_id or None,
        }
        _set_iperf_status(
            state="complete",
            phase="complete",
            percent=100,
            finishedAt=int(time.time()),
            _startedMonotonic=None,
            elapsedSeconds=round(time.monotonic() - started_monotonic, 1),
            message="测速完成",
        )
        return payload
    except IperfCancelled as exc:
        _set_iperf_status(
            state="cancelled",
            phase="cancelled",
            finishedAt=int(time.time()),
            _startedMonotonic=None,
            elapsedSeconds=round(time.monotonic() - started_monotonic, 1),
            message=str(exc),
        )
        raise
    except DiagnosticError as exc:
        _set_iperf_status(
            state="failed",
            phase="failed",
            finishedAt=int(time.time()),
            _startedMonotonic=None,
            elapsedSeconds=round(time.monotonic() - started_monotonic, 1),
            message=exc.payload.get("error", str(exc)),
        )
        raise
    except Exception as exc:
        _set_iperf_status(
            state="failed",
            phase="failed",
            finishedAt=int(time.time()),
            _startedMonotonic=None,
            elapsedSeconds=round(time.monotonic() - started_monotonic, 1),
            message=str(exc),
        )
        raise
    finally:
        IPERF_LOCK.release()


def _iperf_task_worker(task_id: str, data: dict, cancel_event: threading.Event) -> None:
    global IPERF_ACTIVE_TASK_ID
    try:
        result = run_iperf_test(data, task_id, cancel_event)
        with IPERF_STATUS_LOCK:
            status = dict(IPERF_STATUS)
        final = {**status, **result, "state": "complete", "phase": "complete"}
    except IperfCancelled as exc:
        with IPERF_STATUS_LOCK:
            status = dict(IPERF_STATUS)
        final = {**status, "ok": False, "taskId": task_id, "state": "cancelled",
                 "phase": "cancelled", "message": str(exc)}
    except DiagnosticError as exc:
        with IPERF_STATUS_LOCK:
            status = dict(IPERF_STATUS)
        final = {**status, "ok": False, "taskId": task_id, "state": "failed",
                 "phase": "failed", "message": exc.payload.get("error", str(exc))}
    except Exception as exc:
        with IPERF_STATUS_LOCK:
            status = dict(IPERF_STATUS)
        final = {**status, "ok": False, "taskId": task_id, "state": "failed",
                 "phase": "failed", "message": str(exc)}
    final.pop("_startedMonotonic", None)
    final.setdefault("finishedAt", int(time.time()))
    with IPERF_TASKS_LOCK:
        IPERF_TASKS[task_id] = final
        IPERF_CANCEL_EVENTS.pop(task_id, None)
        if IPERF_ACTIVE_TASK_ID == task_id:
            IPERF_ACTIVE_TASK_ID = ""
    _save_iperf_history(final)


def start_iperf_task(data: dict) -> dict:
    global IPERF_ACTIVE_TASK_ID
    with IPERF_TASKS_LOCK:
        if IPERF_ACTIVE_TASK_ID:
            active = IPERF_TASKS.get(IPERF_ACTIVE_TASK_ID) or {}
            if active.get("state") in ("queued", "running"):
                raise DiagnosticError(
                    HTTPStatus.CONFLICT,
                    "已有 iPerf3 测速正在运行，请先等待或停止当前任务",
                    taskId=IPERF_ACTIVE_TASK_ID,
                )
        task_id = f"iperf-{int(time.time())}-{secrets.token_hex(3)}"
        cancel_event = threading.Event()
        task = {
            "ok": True,
            "taskId": task_id,
            "state": "queued",
            "phase": "preparing",
            "percent": 0,
            "startedAt": int(time.time()),
            "maxSeconds": IPERF3_TIMEOUT,
            "message": "测速任务已创建",
        }
        IPERF_TASKS[task_id] = task
        IPERF_CANCEL_EVENTS[task_id] = cancel_event
        IPERF_ACTIVE_TASK_ID = task_id
        # Keep active plus a small recent window in memory. Persistent history
        # is separately capped at five entries.
        if len(IPERF_TASKS) > 20:
            removable = [
                key for key, value in IPERF_TASKS.items()
                if key != task_id and value.get("state") not in ("queued", "running")
            ]
            for key in removable[:len(IPERF_TASKS) - 20]:
                IPERF_TASKS.pop(key, None)
    threading.Thread(
        target=_iperf_task_worker,
        args=(task_id, dict(data or {}), cancel_event),
        name=f"iperf-{task_id[-6:]}",
        daemon=True,
    ).start()
    return task


def stop_iperf_task(data: dict) -> dict:
    task_id = str((data or {}).get("taskId") or "").strip()
    if not task_id:
        raise DiagnosticError(HTTPStatus.BAD_REQUEST, "缺少 iPerf3 任务编号")
    with IPERF_TASKS_LOCK:
        task = dict(IPERF_TASKS.get(task_id) or {})
        cancel_event = IPERF_CANCEL_EVENTS.get(task_id)
    if not task:
        raise DiagnosticError(HTTPStatus.NOT_FOUND, "iPerf3 任务不存在或已过期")
    if task.get("state") not in ("queued", "running") or cancel_event is None:
        return {"ok": True, "taskId": task_id, "state": task.get("state"), "message": "任务已经结束"}
    cancel_event.set()
    with IPERF_PROCESS_LOCK:
        process = IPERF_PROCESSES.get(task_id)
    if process and process.poll() is None:
        process.terminate()
    return {"ok": True, "taskId": task_id, "state": "stopping", "message": "正在停止测速"}


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
            parse_config_text(read_config_text()),
        ),
    )


def _read_api_dependencies() -> platform_read_api.ReadApiDependencies:
    """Bind the read router to the entrypoint's current compatibility API."""
    incident_context = _incident_context()
    return platform_read_api.ReadApiDependencies(
        clock=time.time,
        version_payload=version_payload,
        auth_status=auth_status,
        require_auth=require_auth,
        config_payload=config_payload,
        read_json_file=read_json_file,
        history_path=STATE_DIR / "history.json",
        read_apply_status=read_apply_status,
        incident_list=partial(platform_incidents.incident_list, incident_context),
        iperf_status_payload=iperf_status_payload,
        iperf_history_payload=iperf_history_payload,
        get_dhcp_settings=get_dhcp_settings,
        get_dhcp_bindings=get_dhcp_bindings,
        bridge_retire_pending=bridge_retire_pending,
        get_dhcp_dashboard=get_dhcp_dashboard,
        config_path=CONFIG_PATH,
        stamp=stamp,
    )


def _write_api_dependencies() -> platform_write_api.WriteApiDependencies:
    """Bind the write router to the entrypoint's current compatibility API."""
    incident_context = _incident_context()
    precheck_context = _precheck_context()
    return platform_write_api.WriteApiDependencies(
        login_auth=login_auth,
        change_password_auth=change_password_auth,
        logout_auth=logout_auth,
        clear_session_cookie=clear_session_cookie,
        require_auth=require_auth,
        config_payload=config_payload,
        write_lock=WRITE_LOCK,
        save_config=save_config,
        apply_config=apply_config,
        rollback_config=rollback_config,
        new_incident=partial(platform_incidents.new_incident, incident_context),
        send_test_alert=send_test_alert,
        run_precheck=partial(platform_precheck.run_precheck, precheck_context),
        start_iperf_task=start_iperf_task,
        stop_iperf_task=stop_iperf_task,
        bridge_retire_resolve=bridge_retire_resolve,
        test_dhcp_connection=test_dhcp_connection,
        save_dhcp_settings=save_dhcp_settings,
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
            platform_read_api.handle_get(
                self,
                self.path,
                _read_api_dependencies(),
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
