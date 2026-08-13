"""Platform API for event config, incidents, and network diagnostics.

This service is intentionally small. It owns the writable platform state while
the bigscreen remains a static UI served by nginx. Cisco Telnet uses the pinned
telnetlib3 compatibility module so the service also works on Python 3.13+.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import threading
import time
import urllib.request
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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
from platform_api import bridge as platform_bridge
from platform_api import dhcp_runtime as platform_dhcp_runtime
from platform_api import dhcp_settings as platform_dhcp_settings
from platform_api import dhcp_telnet as platform_dhcp_telnet
from platform_api import incidents as platform_incidents
from platform_api import iperf as platform_iperf
from platform_api import iperf_runtime as platform_iperf_runtime
from platform_api import precheck as platform_precheck
from platform_api import read_api as platform_read_api
from platform_api import storage as platform_storage
from platform_api import transactions as platform_transactions
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
        host_exec_env=_host_exec_env,
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
            parse_config_text(read_config_text()),
        ),
    )


def _read_api_dependencies() -> platform_read_api.ReadApiDependencies:
    """Bind the read router to the entrypoint's current compatibility API."""
    incident_context = _incident_context()
    dhcp_settings_context = _dhcp_settings_context()
    dhcp_runtime_context = _dhcp_runtime_context()
    iperf_runtime_context = _iperf_runtime_context()
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
        iperf_status_payload=partial(
            platform_iperf_runtime.iperf_status_payload,
            iperf_runtime_context,
        ),
        iperf_history_payload=partial(
            platform_iperf_runtime.iperf_history_payload,
            iperf_runtime_context,
        ),
        get_dhcp_settings=partial(
            platform_dhcp_settings.get_dhcp_settings,
            dhcp_settings_context,
        ),
        get_dhcp_bindings=partial(
            platform_dhcp_runtime.get_dhcp_bindings,
            dhcp_runtime_context,
        ),
        bridge_retire_pending=partial(
            platform_bridge.bridge_retire_pending,
            BRIDGE_URL,
        ),
        get_dhcp_dashboard=partial(
            platform_dhcp_runtime.get_dhcp_dashboard,
            dhcp_runtime_context,
        ),
        config_path=CONFIG_PATH,
        stamp=stamp,
    )


def _write_api_dependencies() -> platform_write_api.WriteApiDependencies:
    """Bind the write router to the entrypoint's current compatibility API."""
    incident_context = _incident_context()
    precheck_context = _precheck_context()
    dhcp_settings_context = _dhcp_settings_context()
    dhcp_runtime_context = _dhcp_runtime_context()
    iperf_runtime_context = _iperf_runtime_context()
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
