"""Platform API for event config, incidents, and the offline-deploy manifest.

This service is intentionally small and dependency-free. It owns the writable
platform state while the bigscreen remains a static UI served by nginx.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import shlex
import secrets
import subprocess
import threading
import time
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

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
HISTORY_DIR = STATE_DIR / "history"
TRANSACTION_DIR = HISTORY_DIR / "transactions"
APPLY_STATUS_DIR = STATE_DIR / "apply-status"
WRITE_ENABLED = os.environ.get("PLATFORM_WRITE_ENABLED", "true").lower() in ("1", "true", "yes", "on")
APPLY_ENABLED = os.environ.get("PLATFORM_APPLY_ENABLED", "true").lower() in ("1", "true", "yes", "on")
APPLY_COMMAND = os.environ.get("PLATFORM_APPLY_COMMAND", "/bin/sh /workspace/apply-env.sh")
APPLY_TIMEOUT = max(30, int(os.environ.get("PLATFORM_APPLY_TIMEOUT", "300")))
APPLY_VERIFY_TIMEOUT = max(10, int(os.environ.get("PLATFORM_APPLY_VERIFY_TIMEOUT", "90")))
MAX_REQUEST_BODY_BYTES = max(1024, int(os.environ.get("PLATFORM_MAX_REQUEST_BODY_BYTES", str(1024 * 1024))))
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
SESSIONS: dict[str, dict] = {}


class AuthError(Exception):
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


def atomic_write_text(path: Path, text: str) -> None:
    """Write via a temp file + rename so a concurrent reader never sees a partial
    file while another request is (re)writing config/.env."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def write_json_file(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


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


def write_apply_status(operation_id: str, state: str, **detail) -> dict:
    payload = {
        "ok": state in ("succeeded", "pending"),
        "operationId": operation_id,
        "state": state,
        "updatedAt": int(time.time()),
        **detail,
    }
    write_json_file(apply_status_path(operation_id), payload)
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
    return {**meta, "path": str(directory)}


def list_config_snapshots() -> list[Path]:
    if not TRANSACTION_DIR.exists():
        return []
    return sorted((path for path in TRANSACTION_DIR.iterdir() if path.is_dir()), reverse=True)


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


def login_auth(username: str, password: str) -> tuple[dict, str]:
    store = read_auth_store()
    if username != store.get("username") or not verify_password(password, store.get("passwordHash", "")):
        time.sleep(0.2)
        raise AuthError(HTTPStatus.UNAUTHORIZED, "账号或密码错误", authenticated=False)
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
    text = read_config_text() if text is None else text
    config = parse_config_text(text)
    issues = validate_config(config)
    env = render_env(config, read_env(ENV_PATH))
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
        bridge_health = _http_json(f"{BRIDGE_URL}/health")
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
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self):
        try:
            path = urlparse(self.path).path.rstrip("/") or "/"
            data = self._body()
            if path == "/auth/login":
                payload, cookie = login_auth(str(data.get("username") or ""), str(data.get("password") or ""))
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
                self._send_json({"ok": True, "incident": new_incident(data)})
            elif path == "/test-alert":
                require_auth(self)
                self._send_json(send_test_alert())
            elif path == "/pre-check":
                require_auth(self)
                self._send_json(run_precheck())
            else:
                self._send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)
        except AuthError as exc:
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
