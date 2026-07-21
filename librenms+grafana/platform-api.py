"""Platform API for event config, incidents, and the offline-deploy manifest.

This service is intentionally small. It owns the writable platform state while
the bigscreen remains a static UI served by nginx. Cisco Telnet uses the pinned
telnetlib3 compatibility module so the service also works on Python 3.13+.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import shutil
import shlex
import secrets
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

try:
    from telnetlib3.telnetlib import Telnet
except ImportError:  # Python 3.12 developer/test fallback; production pins telnetlib3.
    from telnetlib import Telnet

from platform_config import (
    default_config_text,
    dump_simple_yaml,
    merge_env_file,
    parse_simple_yaml,
    read_env,
    render_env,
    stamp,
    validate_config,
)


WORKDIR = Path(os.environ.get("PLATFORM_WORKDIR", "/workspace"))
STATE_DIR = Path(os.environ.get("PLATFORM_STATE_DIR", str(WORKDIR / "platform-state")))
CONFIG_PATH = Path(os.environ.get("EVENT_CONFIG_FILE", str(WORKDIR / "event-config.yml")))
EXAMPLE_PATH = Path(os.environ.get("EVENT_CONFIG_EXAMPLE", str(WORKDIR / "event-config.example.yml")))
ENV_PATH = Path(os.environ.get("ENV_FILE", str(WORKDIR / ".env")))
INCIDENT_PATH = STATE_DIR / "incidents.json"
AUTH_PATH = STATE_DIR / "auth.json"
DHCP_SETTINGS_PATH = STATE_DIR / "dhcp-settings.json"
HISTORY_DIR = STATE_DIR / "history"
TRANSACTION_DIR = HISTORY_DIR / "transactions"
APPLY_STATUS_DIR = STATE_DIR / "apply-status"
WRITE_ENABLED = os.environ.get("PLATFORM_WRITE_ENABLED", "true").lower() in ("1", "true", "yes", "on")
APPLY_ENABLED = os.environ.get("PLATFORM_APPLY_ENABLED", "true").lower() in ("1", "true", "yes", "on")
APPLY_COMMAND = os.environ.get("PLATFORM_APPLY_COMMAND", "/bin/sh /workspace/apply-env.sh")
APPLY_TIMEOUT = max(30, int(os.environ.get("PLATFORM_APPLY_TIMEOUT", "300")))
APPLY_VERIFY_TIMEOUT = max(10, int(os.environ.get("PLATFORM_APPLY_VERIFY_TIMEOUT", "90")))
MAX_REQUEST_BODY_BYTES = max(1024, int(os.environ.get("PLATFORM_MAX_REQUEST_BODY_BYTES", str(1024 * 1024))))
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
AUTH_ENABLED = os.environ.get("PLATFORM_AUTH_ENABLED", "true").lower() in ("1", "true", "yes", "on")
AUTH_ADMIN_USER = os.environ.get("PLATFORM_ADMIN_USER", "admin")
AUTH_DEFAULT_PASSWORD = os.environ.get("PLATFORM_ADMIN_PASSWORD", "global")
AUTH_COOKIE_NAME = os.environ.get("PLATFORM_SESSION_COOKIE", "platform_session")
AUTH_COOKIE_SECURE = os.environ.get("PLATFORM_COOKIE_SECURE", "false").lower() in ("1", "true", "yes", "on")
AUTH_SESSION_SECONDS = max(600, int(float(os.environ.get("PLATFORM_SESSION_HOURS", "8")) * 3600))
PASSWORD_MIN_LENGTH = max(10, int(os.environ.get("PLATFORM_PASSWORD_MIN_LENGTH", "10")))
PASSWORD_HASH_ITERATIONS = 260_000
AUTH_FAILURE_WINDOW_SECONDS = max(30, int(os.environ.get("PLATFORM_AUTH_FAILURE_WINDOW_SECONDS", "300")))
AUTH_FAILURE_LIMIT = max(3, int(os.environ.get("PLATFORM_AUTH_FAILURE_LIMIT", "5")))
AUTH_LOCK_SECONDS = max(30, int(os.environ.get("PLATFORM_AUTH_LOCK_SECONDS", "900")))
TRANSACTION_RETENTION = max(5, int(os.environ.get("PLATFORM_TRANSACTION_RETENTION", "50")))
APPLY_STATUS_RETENTION = max(10, int(os.environ.get("PLATFORM_APPLY_STATUS_RETENTION", "200")))
SESSIONS: dict[str, dict] = {}
AUTH_FAILURES: dict[str, dict] = {}
AUTH_FAILURES_LOCK = threading.Lock()

CISCO_PROMPT_RE = br"(?m)^[A-Za-z0-9_.:/()\[\]-]+[>#][ \t]*\r?$"
CISCO_PRIV_PROMPT_RE = br"(?m)^[A-Za-z0-9_.:/()\[\]-]+#[ \t]*\r?$"
CISCO_USER_PROMPT_RE = br"(?m)^[A-Za-z0-9_.:/()\[\]-]+>[ \t]*\r?$"
CISCO_MORE_RE = br"(?i)--More--|<---\s*More\s*--->"


class AuthError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.payload = {"ok": False, "error": message, **extra}


class DiagnosticError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.payload = {"ok": False, "error": message, **extra}


def ensure_dirs() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    TRANSACTION_DIR.mkdir(parents=True, exist_ok=True)
    APPLY_STATUS_DIR.mkdir(parents=True, exist_ok=True)
    ensure_auth_store()


def read_json_file(path: Path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return fallback
    except json.JSONDecodeError:
        return fallback


# Serializes config-mutating requests so the now-threaded server can't interleave
# two saves/applies writing the same files.
WRITE_LOCK = threading.Lock()
# Network diagnostics are deliberately manual and single-flight. This prevents
# accidental double-clicks from running two bandwidth tests or several CLI
# sessions against an older switch at the same time.
IPERF_LOCK = threading.Lock()
IPERF_STATUS_LOCK = threading.Lock()
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


def atomic_write_text(path: Path, text: str) -> None:
    """Write via a temp file + rename so a concurrent reader never sees a partial
    file while another request is (re)writing config/.env."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def write_json_file(path: Path, payload, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if mode is None:
        tmp.write_text(text, encoding="utf-8")
    else:
        # 含密钥的文件（如 DHCP Telnet 密码）从创建那一刻就必须是私有权限，
        # 不能先按默认 umask 落盘再补 chmod——那样存在世界可读的窗口。
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
    tmp.replace(path)


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


def new_operation_id(prefix: str = "op") -> str:
    return f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000_000:09d}-{secrets.token_hex(3)}"


def normalize_operation_id(value: str | None, prefix: str = "op") -> str:
    value = str(value or "").strip()
    if value and re.fullmatch(r"[A-Za-z0-9_-]{8,96}", value):
        return value
    return new_operation_id(prefix)


def apply_status_path(operation_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,96}", str(operation_id or "")):
        raise ValueError("invalid operation id")
    return APPLY_STATUS_DIR / f"{operation_id}.json"


def prune_retained_paths(paths, keep: int) -> None:
    """Remove only the oldest generated state entries beyond ``keep``."""
    ordered = sorted(
        (path for path in paths if path.exists()),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    for path in ordered[max(1, keep):]:
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as exc:
            print(f"[platform-api] state retention cleanup failed for {path}: {exc}", flush=True)


def prune_generated_state() -> None:
    if TRANSACTION_DIR.exists():
        prune_retained_paths(TRANSACTION_DIR.iterdir(), TRANSACTION_RETENTION)
    if APPLY_STATUS_DIR.exists():
        prune_retained_paths(APPLY_STATUS_DIR.glob("*.json"), APPLY_STATUS_RETENTION)


def write_apply_status(operation_id: str, state: str, **detail) -> dict:
    payload = {
        "ok": state in ("succeeded", "pending"),
        "operationId": operation_id,
        "state": state,
        "updatedAt": int(time.time()),
        **detail,
    }
    write_json_file(apply_status_path(operation_id), payload)
    prune_generated_state()
    return payload


def read_apply_status(operation_id: str) -> dict:
    clean = str(operation_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,96}", clean):
        return {"ok": False, "operationId": clean, "state": "unknown", "error": "无效的应用任务编号"}
    path = APPLY_STATUS_DIR / f"{clean}.json"
    if not path.exists():
        return {"ok": False, "operationId": clean, "state": "unknown", "error": "找不到该应用任务"}
    return read_json_file(path, {"ok": False, "operationId": clean, "state": "unknown"})


def create_config_snapshot(action: str, actor: str = "", note: str = "") -> dict:
    """Snapshot config and .env as one indivisible rollback generation."""
    transaction_id = new_operation_id("txn")
    directory = TRANSACTION_DIR / transaction_id
    directory.mkdir(parents=True, exist_ok=False)
    meta = {
        "id": transaction_id,
        "action": action,
        "actor": actor,
        "note": note,
        "createdAt": int(time.time()),
        "configExisted": CONFIG_PATH.exists(),
        "envExisted": ENV_PATH.exists(),
    }
    if CONFIG_PATH.exists():
        shutil.copy2(CONFIG_PATH, directory / "event-config.yml")
    if ENV_PATH.exists():
        shutil.copy2(ENV_PATH, directory / ".env")
    write_json_file(directory / "metadata.json", meta)
    prune_generated_state()
    return {**meta, "path": str(directory)}


def list_config_snapshots() -> list[Path]:
    if not TRANSACTION_DIR.exists():
        return []
    eligible = []
    for path in TRANSACTION_DIR.iterdir():
        if not path.is_dir():
            continue
        meta = read_json_file(path / "metadata.json", {})
        if meta.get("action") == "config.rollback.guard" or meta.get("consumedAt"):
            continue
        eligible.append(path)
    return sorted(eligible, reverse=True)


def mark_config_snapshot_consumed(directory: Path) -> None:
    meta_path = directory / "metadata.json"
    meta = read_json_file(meta_path, {})
    if not meta:
        return
    meta["consumedAt"] = int(time.time())
    write_json_file(meta_path, meta)


def restore_config_snapshot(directory: Path) -> dict:
    meta = read_json_file(directory / "metadata.json", {})
    if not meta:
        raise ValueError(f"invalid config snapshot: {directory}")
    restored = {"transactionId": meta.get("id") or directory.name}
    config_backup = directory / "event-config.yml"
    env_backup = directory / ".env"
    if meta.get("configExisted"):
        atomic_write_text(CONFIG_PATH, config_backup.read_text(encoding="utf-8"))
        restored["config"] = str(config_backup)
    elif CONFIG_PATH.exists():
        CONFIG_PATH.unlink()
        restored["config"] = "removed"
    if meta.get("envExisted"):
        atomic_write_text(ENV_PATH, env_backup.read_text(encoding="utf-8"))
        restored["env"] = str(env_backup)
    elif ENV_PATH.exists():
        ENV_PATH.unlink()
        restored["env"] = "removed"
    return restored


def b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, salt: bytes | None = None, iterations: int = PASSWORD_HASH_ITERATIONS) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${b64encode(salt)}${b64encode(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, digest = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        expected = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), b64decode(salt), int(iterations))
        return hmac.compare_digest(b64encode(expected), digest)
    except Exception:
        return False


def ensure_auth_store() -> None:
    if not AUTH_ENABLED or AUTH_PATH.exists():
        return
    write_json_file(AUTH_PATH, {
        "username": AUTH_ADMIN_USER,
        "passwordHash": hash_password(AUTH_DEFAULT_PASSWORD),
        "mustChangePassword": True,
        "createdAt": int(time.time()),
        "passwordChangedAt": None,
    })


def read_auth_store() -> dict:
    ensure_auth_store()
    store = read_json_file(AUTH_PATH, {})
    if not store.get("username") or not store.get("passwordHash"):
        store = {
            "username": AUTH_ADMIN_USER,
            "passwordHash": hash_password(AUTH_DEFAULT_PASSWORD),
            "mustChangePassword": True,
            "createdAt": int(time.time()),
            "passwordChangedAt": None,
        }
        write_json_file(AUTH_PATH, store)
    return store


def write_auth_store(store: dict) -> None:
    store["updatedAt"] = int(time.time())
    write_json_file(AUTH_PATH, store)


def password_strength_error(password: str) -> str | None:
    if len(password or "") < PASSWORD_MIN_LENGTH:
        return f"新密码至少 {PASSWORD_MIN_LENGTH} 位"
    if password == AUTH_DEFAULT_PASSWORD:
        return "新密码不能继续使用默认密码"
    if password.lower() in ("password", "admin123456", "event@2026!", "global"):
        return "新密码过于常见"
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        return "新密码需要同时包含字母和数字"
    return None


def parse_cookies(header: str) -> dict[str, str]:
    cookies = {}
    for part in str(header or "").split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        cookies[key.strip()] = value.strip()
    return cookies


def prune_sessions() -> None:
    now = time.time()
    expired = [token for token, session in SESSIONS.items() if session.get("expires", 0) <= now]
    for token in expired:
        SESSIONS.pop(token, None)


def create_session(username: str) -> str:
    prune_sessions()
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = {"username": username, "expires": time.time() + AUTH_SESSION_SECONDS}
    return token


def current_session(handler: BaseHTTPRequestHandler) -> dict | None:
    if not AUTH_ENABLED:
        return {"username": "local", "expires": time.time() + AUTH_SESSION_SECONDS}
    prune_sessions()
    token = parse_cookies(handler.headers.get("Cookie", "")).get(AUTH_COOKIE_NAME)
    session = SESSIONS.get(token or "")
    if not session:
        return None
    return session


def session_cookie(token: str, max_age: int = AUTH_SESSION_SECONDS) -> str:
    parts = [
        f"{AUTH_COOKIE_NAME}={token}",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        f"Max-Age={max_age}",
    ]
    if AUTH_COOKIE_SECURE:
        parts.append("Secure")
    return "; ".join(parts)


def clear_session_cookie() -> str:
    parts = [
        f"{AUTH_COOKIE_NAME}=",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        "Max-Age=0",
    ]
    if AUTH_COOKIE_SECURE:
        parts.append("Secure")
    return "; ".join(parts)


def auth_status(handler: BaseHTTPRequestHandler) -> dict:
    if not AUTH_ENABLED:
        return {"ok": True, "enabled": False, "authenticated": True, "username": "local", "mustChangePassword": False}
    store = read_auth_store()
    session = current_session(handler)
    authenticated = bool(session)
    return {
        "ok": True,
        "enabled": True,
        "authenticated": authenticated,
        "username": store.get("username", AUTH_ADMIN_USER) if authenticated else "",
        "defaultUser": store.get("username", AUTH_ADMIN_USER),
        "mustChangePassword": bool(store.get("mustChangePassword")) if authenticated else False,
        "sessionExpiresAt": int(session.get("expires", 0)) if session else 0,
    }


def require_auth(handler: BaseHTTPRequestHandler, allow_must_change: bool = False) -> dict:
    if not AUTH_ENABLED:
        return {"username": "local"}
    store = read_auth_store()
    session = current_session(handler)
    if not session:
        raise AuthError(HTTPStatus.UNAUTHORIZED, "需要登录", authenticated=False)
    if store.get("mustChangePassword") and not allow_must_change:
        raise AuthError(HTTPStatus.FORBIDDEN, "需要先修改默认密码", authenticated=True, mustChangePassword=True)
    return {"username": session.get("username") or store.get("username") or AUTH_ADMIN_USER}


def _auth_failure_keys(username: str, client_ip: str) -> tuple[str, str]:
    return f"ip:{client_ip or 'unknown'}", f"user:{str(username or '').strip().lower()}"


def _auth_lock_remaining(username: str, client_ip: str, now: float | None = None) -> int:
    now = time.time() if now is None else now
    with AUTH_FAILURES_LOCK:
        remaining = 0
        for key in _auth_failure_keys(username, client_ip):
            state = AUTH_FAILURES.get(key) or {}
            locked_until = float(state.get("lockedUntil") or 0)
            if locked_until > now:
                remaining = max(remaining, int(locked_until - now + 0.999))
            elif state:
                recent = [stamp for stamp in state.get("failures", []) if now - stamp <= AUTH_FAILURE_WINDOW_SECONDS]
                if recent:
                    state["failures"] = recent
                else:
                    AUTH_FAILURES.pop(key, None)
        return remaining


def _record_auth_failure(username: str, client_ip: str, now: float | None = None) -> int:
    now = time.time() if now is None else now
    locked_until = 0.0
    with AUTH_FAILURES_LOCK:
        for key in _auth_failure_keys(username, client_ip):
            state = AUTH_FAILURES.setdefault(key, {"failures": [], "lockedUntil": 0.0})
            recent = [stamp for stamp in state.get("failures", []) if now - stamp <= AUTH_FAILURE_WINDOW_SECONDS]
            recent.append(now)
            state["failures"] = recent
            if len(recent) >= AUTH_FAILURE_LIMIT:
                state["lockedUntil"] = max(float(state.get("lockedUntil") or 0), now + AUTH_LOCK_SECONDS)
            locked_until = max(locked_until, float(state.get("lockedUntil") or 0))
    return max(0, int(locked_until - now + 0.999))


def _clear_auth_failures(username: str, client_ip: str) -> None:
    with AUTH_FAILURES_LOCK:
        for key in _auth_failure_keys(username, client_ip):
            AUTH_FAILURES.pop(key, None)


def login_auth(username: str, password: str, client_ip: str = "") -> tuple[dict, str]:
    remaining = _auth_lock_remaining(username, client_ip)
    if remaining:
        raise AuthError(
            HTTPStatus.TOO_MANY_REQUESTS,
            f"登录失败次数过多，请在 {remaining} 秒后重试",
            authenticated=False,
            retryAfter=remaining,
        )
    store = read_auth_store()
    if username != store.get("username") or not verify_password(password, store.get("passwordHash", "")):
        remaining = _record_auth_failure(username, client_ip)
        if remaining:
            raise AuthError(
                HTTPStatus.TOO_MANY_REQUESTS,
                f"登录失败次数过多，请在 {remaining} 秒后重试",
                authenticated=False,
                retryAfter=remaining,
            )
        raise AuthError(HTTPStatus.UNAUTHORIZED, "账号或密码错误", authenticated=False)
    _clear_auth_failures(username, client_ip)
    token = create_session(store["username"])
    return {
        "ok": True,
        "authenticated": True,
        "username": store["username"],
        "mustChangePassword": bool(store.get("mustChangePassword")),
        "sessionExpiresAt": int(SESSIONS[token]["expires"]),
    }, session_cookie(token)


def change_password_auth(handler: BaseHTTPRequestHandler, data: dict) -> tuple[dict, str]:
    auth = require_auth(handler, allow_must_change=True)
    current_password = str(data.get("currentPassword") or "")
    new_password = str(data.get("newPassword") or "")
    confirm_password = str(data.get("confirmPassword") or new_password)
    store = read_auth_store()
    if not verify_password(current_password, store.get("passwordHash", "")):
        raise AuthError(HTTPStatus.FORBIDDEN, "当前密码不正确", authenticated=True, mustChangePassword=bool(store.get("mustChangePassword")))
    if new_password != confirm_password:
        raise AuthError(HTTPStatus.BAD_REQUEST, "两次输入的新密码不一致", authenticated=True)
    strength_error = password_strength_error(new_password)
    if strength_error:
        raise AuthError(HTTPStatus.BAD_REQUEST, strength_error, authenticated=True)
    store["passwordHash"] = hash_password(new_password)
    store["mustChangePassword"] = False
    store["passwordChangedAt"] = int(time.time())
    write_auth_store(store)
    SESSIONS.clear()
    token = create_session(auth["username"])
    append_history("auth.password_change", auth["username"], "password changed", {})
    return {
        "ok": True,
        "authenticated": True,
        "username": auth["username"],
        "mustChangePassword": False,
        "sessionExpiresAt": int(SESSIONS[token]["expires"]),
    }, session_cookie(token)


def logout_auth(handler: BaseHTTPRequestHandler) -> None:
    token = parse_cookies(handler.headers.get("Cookie", "")).get(AUTH_COOKIE_NAME)
    if token:
        SESSIONS.pop(token, None)


def read_config_text() -> str:
    if CONFIG_PATH.exists():
        return CONFIG_PATH.read_text(encoding="utf-8")
    return default_config_text(EXAMPLE_PATH)


def parse_config_text(text: str):
    config = parse_simple_yaml(text)
    if not isinstance(config, dict):
        raise ValueError("event config must be a mapping")
    return config


def config_payload(text: str | None = None) -> dict:
    editing_existing = text is None
    text = read_config_text() if editing_existing else text
    config = parse_config_text(text)
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
        "paths": {
            "config": str(CONFIG_PATH),
            "env": str(ENV_PATH),
            "state": str(STATE_DIR),
        },
    }


def backup_file(path: Path, prefix: str) -> str | None:
    if not path.exists():
        return None
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    dest = HISTORY_DIR / f"{prefix}-{stamp()}{path.suffix or '.bak'}"
    shutil.copy2(path, dest)
    return str(dest)


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
    plugin_dirs = ":".join([
        "/host/usr/libexec/docker/cli-plugins",
        "/host/usr/lib/docker/cli-plugins",
        "/host/usr/local/lib/docker/cli-plugins",
        env.get("DOCKER_CLI_PLUGIN_EXTRA_DIRS", ""),
    ]).strip(":")
    if plugin_dirs:
        env["DOCKER_CLI_PLUGIN_EXTRA_DIRS"] = plugin_dirs
    return env


def _http_json(url: str, timeout: int = 5):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def _prom_query(expr: str):
    return _http_json(f"{PRECHECK_PROM_URL}/api/v1/query?query={quote(expr)}").get("data", {}).get("result", [])


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


def run_precheck() -> dict:
    """Native readiness check for the console. Uses only urllib against the stack
    services (reachable by name on the docker network) -- no curl/ping/compose,
    so it works from the slim container and offline. pre-match-check.sh stays for
    deeper host-side checks on the CLI."""
    checks: list[dict] = []

    def add(level, text):
        checks.append({"level": level, "text": text})

    # 1. Prometheus 可达 + 抓取目标
    try:
        ups = _prom_query("up")
        online = sum(1 for x in ups if (x.get("value") or [None, "0"])[1] == "1")
        failed = [x for x in ups if (x.get("value") or [None, "0"])[1] != "1"]
        if not ups:
            add("bad", "Prometheus 可达，但没有任何抓取目标")
        elif failed:
            names = "、".join(
                (x.get("metric") or {}).get("job", "?") + ":" + (x.get("metric") or {}).get("instance", "?")
                for x in failed[:8]
            )
            add("bad", f"Prometheus 有 {len(failed)} 个抓取目标失败（{online}/{len(ups)} 在线）：{names}")
        else:
            add("good", f"Prometheus 正常，抓取目标 {online}/{len(ups)} 全部在线")
    except Exception as exc:
        add("bad", f"Prometheus 不可达（{PRECHECK_PROM_URL}）：{exc}")
        # Without Prometheus the rest can't be judged.
        return _precheck_result(checks)

    # 2. 基础设施设备在线率（ping）
    try:
        infra = _prom_query('probe_success{job=~"infra-.*"}')
        down = [x for x in infra if (x.get("value") or [None, "1"])[1] != "1"]
        if not infra:
            add("warn", "还没有基础设施 ping 目标（配置未填或未应用？）")
        elif down:
            names = "、".join((x.get("metric") or {}).get("display_name") or (x.get("metric") or {}).get("instance", "?") for x in down[:8])
            add("bad", f"{len(down)} 台基础设施设备离线：{names}")
        else:
            add("good", f"基础设施 {len(infra)} 台全部在线")
    except Exception as exc:
        add("warn", f"无法查询设备在线状态：{exc}")

    # 3. 选手机位 ping 目标
    try:
        players = _prom_query('probe_success{job="player-ping"}')
        online = sum(1 for x in players if (x.get("value") or [None, "0"])[1] == "1")
        if not players:
            add("bad", "选手机位监控目标为 0，不能确认比赛网络状态")
        elif online != len(players):
            add("bad", f"选手机位仅 {online}/{len(players)} 在线")
        else:
            add("good", f"选手机位 {online}/{len(players)} 全部在线")
    except Exception as exc:
        add("warn", f"无法查询选手目标：{exc}")

    # 4. Grafana
    try:
        _http_json(f"{PRECHECK_GRAFANA_URL}/api/health")
        add("good", "Grafana 正常")
    except Exception as exc:
        add("bad", f"Grafana 不可达（{PRECHECK_GRAFANA_URL}）：{exc}")

    # 5. 飞书告警链路
    try:
        try:
            bridge_health = _http_json(f"{BRIDGE_URL}/health")
        except urllib.error.HTTPError as exc:
            # 看门狗线程死亡时桥接按 503 返回同样的 JSON——读出来照常展示细节
            bridge_health = json.loads(exc.read().decode("utf-8", errors="replace") or "{}")
        if not bridge_health.get("ready"):
            details = []
            if not bridge_health.get("tokenConfigured") and not bridge_health.get("dryRun"):
                details.append("未配置飞书 Token")
            if bridge_health.get("deadWatchers"):
                details.append("后台线程已停止：" + ",".join(bridge_health["deadWatchers"]))
            add("bad", "告警服务未就绪：" + ("；".join(details) or "健康检查未通过"))
        else:
            watcher_errors = [
                f"{name}: {state.get('lastError')}"
                for name, state in (bridge_health.get("watchers") or {}).items()
                if state.get("lastError")
            ]
            if watcher_errors:
                add("warn", "告警服务线程存活，但最近轮询失败：" + "；".join(watcher_errors[:4]))
            else:
                add("good", "告警服务及后台线程正常")
    except Exception as exc:
        add("bad", f"告警服务不可达：{exc}")

    # 6. 用户入口与目标生成器
    try:
        with urllib.request.urlopen(f"{PRECHECK_BIGSCREEN_URL}/", timeout=5) as resp:
            resp.read(1024)
        add("good", "赛事大屏入口正常")
    except Exception as exc:
        add("bad", f"赛事大屏不可达：{exc}")

    try:
        target_status = _http_json(f"{PRECHECK_PLAYER_TARGETS_URL}/status")
        target_count = int((target_status.get("targets") or {}).get("total") or 0)
        if target_status.get("error"):
            add("bad", f"选手目标生成器异常：{target_status.get('error')}")
        elif target_count <= 0:
            add("bad", "选手目标生成器尚未生成任何目标")
        else:
            add("good", f"选手目标生成器正常，共 {target_count} 个目标")
    except Exception as exc:
        add("bad", f"选手目标生成器不可达：{exc}")

    try:
        with urllib.request.urlopen(f"{PRECHECK_LIBRENMS_URL}/", timeout=5) as resp:
            resp.read(1024)
        add("good", "LibreNMS Web 正常")
    except Exception as exc:
        add("bad", f"LibreNMS 不可达：{exc}")

    # 7. 配置阻塞项
    try:
        issues = validate_config(parse_config_text(read_config_text()))
        blocking = [i for i in issues if i.get("level") == "bad"]
        if blocking:
            for i in blocking[:6]:
                add("bad", f"配置缺项：{i.get('message')}（{i.get('path')}）")
        else:
            add("good", "配置无阻塞项")
    except Exception as exc:
        add("warn", f"配置检查失败：{exc}")

    return _precheck_result(checks)


def _precheck_result(checks: list[dict]) -> dict:
    icon = {"good": "✓", "warn": "⚠", "bad": "✗"}
    passed = sum(1 for c in checks if c["level"] == "good")
    warned = sum(1 for c in checks if c["level"] == "warn")
    failed = sum(1 for c in checks if c["level"] == "bad")
    verdict = "bad" if failed else ("warn" if warned else "good")
    output = "\n".join(f"  {icon[c['level']]} {c['text']}" for c in checks)
    return {"ok": True, "verdict": verdict, "pass": passed, "warn": warned, "fail": failed, "output": output}


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
        output = "\n".join(part for part in [exc.stdout or "", exc.stderr or ""] if part).strip()
        return {
            "ok": False,
            "error": f"配置已写入，但自动应用超时（{APPLY_TIMEOUT}s）",
            "needsRedeploy": True,
            "nextStep": "cd librenms+grafana && ./apply-env.sh",
            "applyOutput": output[-4000:],
        }

    output = "\n".join(part for part in [completed.stdout, completed.stderr] if part).strip()
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
    payload = config_payload(text)
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
    write_apply_status(operation_id, "running", action="apply", startedAt=int(time.time()))
    snapshot = None
    try:
        payload = config_payload(text) if text is not None else config_payload()
        bad = [item for item in payload["issues"] if item.get("level") == "bad"]
        if bad:
            result = {**payload, "ok": False, "error": "config has blocking validation errors", "operationId": operation_id}
            write_apply_status(operation_id, "failed", action="apply", error=result["error"])
            return result

        snapshot = create_config_snapshot("config.apply", actor, note)
        if text is not None:
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
    write_apply_status(operation_id, "running", action="rollback", startedAt=int(time.time()))
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


def incident_list() -> list[dict]:
    return read_json_file(INCIDENT_PATH, [])


def save_incidents(items: list[dict]) -> None:
    write_json_file(INCIDENT_PATH, items)


def new_incident(data: dict) -> dict:
    require_write()
    items = incident_list()
    next_id = max([int(item.get("id", 0)) for item in items] or [0]) + 1
    now = int(time.time())
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
        "events": data.get("events") or [{"time": now, "type": "note", "message": data.get("note") or "事故创建"}],
    }
    items.insert(0, incident)
    save_incidents(items)
    return incident


def update_incident(incident_id: int, data: dict) -> dict:
    require_write()
    items = incident_list()
    for item in items:
        if int(item.get("id", 0)) == incident_id:
            for key in ("title", "severity", "status", "scope", "owner", "rootCause", "recoveredAt", "related"):
                if key in data:
                    item[key] = data[key]
            if data.get("event"):
                item.setdefault("events", []).append({
                    "time": int(time.time()),
                    "type": data.get("eventType") or "note",
                    "message": data["event"],
                })
            save_incidents(items)
            return item
    raise KeyError(f"incident {incident_id} not found")


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


def _dhcp_number(block: str, label: str) -> int:
    match = re.search(rf"(?im)^\s*{re.escape(label)}\s*:\s*(\d+)\s*$", block)
    return int(match.group(1)) if match else 0


def parse_cisco_dhcp_pools(text: str) -> list[dict]:
    """Parse the stable fields from Cisco IOS/IOS-XE `show ip dhcp pool`."""
    source = str(text or "").replace("\r", "")
    starts = list(re.finditer(r"(?im)^\s*Pool\s+(.+?)\s*:\s*$", source))
    pools: list[dict] = []
    for index, match in enumerate(starts):
        block = source[match.end():starts[index + 1].start() if index + 1 < len(starts) else len(source)]
        total = _dhcp_number(block, "Total addresses")
        leased = _dhcp_number(block, "Leased addresses")
        excluded = _dhcp_number(block, "Excluded addresses")
        usable = max(0, total - excluded)
        available = max(0, usable - leased)
        address_range = ""
        range_match = re.search(
            r"(?m)(\d{1,3}(?:\.\d{1,3}){3})\s*-\s*(\d{1,3}(?:\.\d{1,3}){3})",
            block,
        )
        if range_match:
            address_range = f"{range_match.group(1)} - {range_match.group(2)}"
        utilization = round((leased / usable * 100) if usable else 0, 1)
        pools.append({
            "name": match.group(1).strip(),
            "range": address_range,
            "total": total,
            "leased": leased,
            "excluded": excluded,
            "available": available,
            "utilization": utilization,
            "level": "bad" if utilization >= 90 else "warn" if utilization >= 80 else "good",
        })
    return pools


def parse_cisco_dhcp_conflicts(text: str) -> list[str]:
    source = str(text or "").replace("\r", "")
    addresses = re.findall(r"(?m)^\s*(\d{1,3}(?:\.\d{1,3}){3})\s+", source)
    return list(dict.fromkeys(addresses))


def parse_cisco_dhcp_excluded(text: str) -> list[str]:
    """Expand IOS DHCP exclusions without reading the full binding table."""
    addresses: set[ipaddress.IPv4Address] = set()
    for match in re.finditer(
        r"(?im)^\s*ip\s+dhcp\s+excluded-address\s+"
        r"(\d{1,3}(?:\.\d{1,3}){3})(?:\s+(\d{1,3}(?:\.\d{1,3}){3}))?\s*$",
        str(text or "").replace("\r", ""),
    ):
        try:
            start = ipaddress.IPv4Address(match.group(1))
            end = ipaddress.IPv4Address(match.group(2) or match.group(1))
        except ipaddress.AddressValueError:
            continue
        if end < start:
            start, end = end, start
        # Refuse pathological output instead of allocating millions of entries.
        if int(end) - int(start) > 65535:
            continue
        addresses.update(ipaddress.IPv4Address(value) for value in range(int(start), int(end) + 1))
    return [str(value) for value in sorted(addresses)]


def attach_dhcp_pool_exclusions(pools: list[dict], excluded_addresses: list[str]) -> None:
    """Attach exact exclusions that fall inside each returned pool range."""
    parsed = []
    for value in excluded_addresses:
        try:
            parsed.append(ipaddress.IPv4Address(value))
        except ipaddress.AddressValueError:
            continue
    for pool in pools:
        bounds = re.match(
            r"^\s*(\d{1,3}(?:\.\d{1,3}){3})\s*-\s*(\d{1,3}(?:\.\d{1,3}){3})\s*$",
            str(pool.get("range") or ""),
        )
        if not bounds:
            pool["excludedAddresses"] = []
            continue
        try:
            start = ipaddress.IPv4Address(bounds.group(1))
            end = ipaddress.IPv4Address(bounds.group(2))
        except ipaddress.AddressValueError:
            pool["excludedAddresses"] = []
            continue
        pool["excludedAddresses"] = [str(value) for value in parsed if start <= value <= end]


def parse_cisco_dhcp_statistics(text: str) -> dict:
    source = str(text or "").replace("\r", "")

    def value(label: str) -> int:
        match = re.search(rf"(?im)^\s*{re.escape(label)}\s+(\d+)\s*$", source)
        return int(match.group(1)) if match else 0

    return {
        "automaticBindings": value("Automatic bindings"),
        "manualBindings": value("Manual bindings"),
        "expiredBindings": value("Expired bindings"),
        "malformedMessages": value("Malformed messages"),
    }


def parse_cisco_dhcp_bindings(text: str) -> list[dict]:
    """Parse active addresses from IOS/IOS-XE ``show ip dhcp binding``.

    Cisco has added columns across releases, so only the stable leading IPv4
    address is structural.  The remaining one-line detail is retained for the
    operator's hover text without guessing at a particular column layout.
    """
    bindings = []
    seen = set()
    for line in str(text or "").replace("\r", "").splitlines():
        match = re.match(r"^\s*(\d{1,3}(?:\.\d{1,3}){3})\s+(.+?)\s*$", line)
        if not match:
            continue
        try:
            address = str(ipaddress.IPv4Address(match.group(1)))
        except ipaddress.AddressValueError:
            continue
        if address in seen:
            continue
        seen.add(address)
        bindings.append({
            "ip": address,
            "detail": re.sub(r"\s+", " ", match.group(2)).strip()[:512],
        })
        if len(bindings) >= 65536:
            break
    return bindings


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
    """Read exact leases only after the operator explicitly requests them."""
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
        return {
            "ok": True,
            "host": host,
            "bindings": bindings,
            "usedAddresses": [item["ip"] for item in bindings],
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
        payload = {
            **collect_cisco_dhcp(host),
            "capturedAt": int(time.time()),
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


def iperf_status_payload() -> dict:
    with IPERF_STATUS_LOCK:
        payload = dict(IPERF_STATUS)
    started = payload.pop("_startedMonotonic", None)
    if started is not None:
        payload["elapsedSeconds"] = round(max(0, time.monotonic() - started), 1)
    else:
        payload.setdefault("elapsedSeconds", 0)
    return payload


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


def _run_iperf_direction(host: str, ports: list[int], duration: int, parallel: int, reverse: bool,
                         deadline: float, direction_index: int, direction_total: int) -> dict:
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
            completed = subprocess.run(
                command,
                cwd=str(WORKDIR),
                env=_host_exec_env(),
                capture_output=True,
                text=True,
                timeout=max(1, min(duration + 5, remaining)),
                check=False,
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


def run_iperf_test(data: dict) -> dict:
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
    )
    try:
        results = []
        preferred_ports = list(ports)
        for direction_index, (direction_name, reverse) in enumerate(directions):
            _set_iperf_status(directionIndex=direction_index + 1)
            result = _run_iperf_direction(
                host, preferred_ports, duration, parallel, reverse, deadline,
                direction_index, len(directions),
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


def delivery_manifest() -> dict:
    compose = WORKDIR / "docker-compose.yml"
    files = [
        "docker-compose.yml",
        "event-config.yml",
        "event-config.example.yml",
        ".env",
        "apply-env.sh",
        "deploy.sh",
        "pre-match-check.sh",
        "offline-package.sh",
        "install-offline.sh",
    ]
    images = []
    if compose.exists():
        for line in compose.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("image:"):
                image = stripped.split(":", 1)[1].strip().strip('"')
                match = re.fullmatch(r"\$\{[^:}]+:-([^}]+)\}", image)
                images.append(match.group(1) if match else image)
    return {
        "ok": True,
        "images": sorted(set(images)),
        "files": [name for name in files if (WORKDIR / name).exists()],
        "commands": [
            "./offline-package.sh",
            "tar -xf monitor-offline-*.tar.gz",
            "cd monitor-offline-* && ./install-offline.sh",
        ],
    }


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
            parsed_url = urlparse(self.path)
            path = parsed_url.path.rstrip("/") or "/"
            if path == "/health":
                self._send_json({"ok": True, "time": int(time.time())})
            elif path == "/auth/status":
                self._send_json(auth_status(self))
            elif path == "/config":
                require_auth(self)
                payload = config_payload()
                payload["history"] = read_json_file(STATE_DIR / "history.json", [])[:20]
                self._send_json(payload)
            elif path == "/config/apply-status":
                require_auth(self)
                operation_id = (parse_qs(parsed_url.query).get("operationId") or [""])[-1]
                self._send_json(read_apply_status(operation_id))
            elif path == "/incidents":
                require_auth(self)
                self._send_json({"ok": True, "incidents": incident_list()})
            elif path == "/delivery/manifest":
                require_auth(self)
                self._send_json(delivery_manifest())
            elif path == "/network/iperf3/status":
                require_auth(self)
                self._send_json(iperf_status_payload())
            elif path == "/network/dhcp/settings":
                require_auth(self)
                self._send_json(get_dhcp_settings())
            elif path == "/network/dhcp/bindings":
                require_auth(self)
                self._send_json(get_dhcp_bindings())
            elif path == "/network/retire/pending":
                require_auth(self)
                self._send_json(bridge_retire_pending())
            elif path == "/network/dhcp":
                # 必须鉴权：它返回核心交换机的地址池/接口信息，force=1 还会真实
                # 发起特权 Telnet 会话——绝不能让未登录方触发。
                require_auth(self)
                force = (parse_qs(parsed_url.query).get("force") or [""])[-1].lower() in ("1", "true", "yes")
                self._send_json(get_dhcp_dashboard(force))
            elif path == "/config/download":
                # The single round-trippable config file: export this, edit or archive
                # it, then re-import it via /config/import.
                require_auth(self)
                text = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else ""
                self._send_bytes(
                    text.encode("utf-8"),
                    f"event-config-{stamp()}.yml",
                    "application/x-yaml; charset=utf-8",
                )
            else:
                self._send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)
        except AuthError as exc:
            self._send_json(exc.payload, exc.status)
        except DiagnosticError as exc:
            self._send_json(exc.payload, exc.status)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self):
        try:
            path = urlparse(self.path).path.rstrip("/") or "/"
            data = self._body()
            if path == "/auth/login":
                payload, cookie = login_auth(
                    str(data.get("username") or ""),
                    str(data.get("password") or ""),
                    str((self.client_address or ("", 0))[0]),
                )
                self._send_json(payload, headers={"Set-Cookie": cookie})
            elif path == "/auth/change-password":
                payload, cookie = change_password_auth(self, data)
                self._send_json(payload, headers={"Set-Cookie": cookie})
            elif path == "/auth/logout":
                logout_auth(self)
                self._send_json({"ok": True, "authenticated": False}, headers={"Set-Cookie": clear_session_cookie()})
            elif path == "/config/validate":
                require_auth(self)
                self._send_json(config_payload(data.get("text", "")))
            elif path == "/config/save":
                auth = require_auth(self)
                with WRITE_LOCK:
                    self._send_json(save_config(data.get("text", ""), auth["username"], data.get("note", "")))
            elif path == "/config/apply":
                auth = require_auth(self)
                text = data.get("text") if "text" in data else None
                with WRITE_LOCK:
                    self._send_json(apply_config(
                        text,
                        auth["username"],
                        data.get("note", ""),
                        data.get("operationId"),
                    ))
            elif path == "/config/rollback":
                auth = require_auth(self)
                with WRITE_LOCK:
                    self._send_json(rollback_config(
                        auth["username"],
                        data.get("note", ""),
                        data.get("operationId"),
                    ))
            elif path == "/config/import":
                auth = require_auth(self)
                with WRITE_LOCK:
                    self._send_json(save_config(data.get("text", ""), auth["username"], "import"))
            elif path == "/incidents":
                require_auth(self)
                # incidents.json 也是读-改-写，threaded server 下并发提交会互相覆盖
                with WRITE_LOCK:
                    self._send_json({"ok": True, "incident": new_incident(data)})
            elif path == "/test-alert":
                require_auth(self)
                self._send_json(send_test_alert())
            elif path == "/pre-check":
                require_auth(self)
                self._send_json(run_precheck())
            elif path == "/network/iperf3":
                require_auth(self)
                self._send_json(run_iperf_test(data))
            elif path == "/network/retire/resolve":
                require_auth(self)
                self._send_json(bridge_retire_resolve(data))
            elif path == "/network/dhcp/test":
                require_auth(self)
                self._send_json(test_dhcp_connection())
            elif path == "/network/dhcp/settings":
                require_auth(self)
                with WRITE_LOCK:
                    self._send_json(save_dhcp_settings(data))
            else:
                self._send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)
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
            require_auth(self)
            path = urlparse(self.path).path.rstrip("/")
            parts = [unquote(part) for part in path.split("/") if part]
            if len(parts) == 2 and parts[0] == "incidents":
                with WRITE_LOCK:
                    incident = update_incident(int(parts[1]), self._body())
                self._send_json({"ok": True, "incident": incident})
            else:
                self._send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)
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
