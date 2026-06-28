"""Platform API for event config, incidents, and delivery artifacts.

This service is intentionally small and dependency-free. It owns the writable
platform state while the bigscreen remains a static UI served by nginx.
"""
from __future__ import annotations

import io
import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import secrets
import time
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

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
WRITE_ENABLED = os.environ.get("PLATFORM_WRITE_ENABLED", "true").lower() in ("1", "true", "yes", "on")
AUTH_ENABLED = os.environ.get("PLATFORM_AUTH_ENABLED", "true").lower() in ("1", "true", "yes", "on")
AUTH_ADMIN_USER = os.environ.get("PLATFORM_ADMIN_USER", "admin")
AUTH_DEFAULT_PASSWORD = os.environ.get("PLATFORM_ADMIN_PASSWORD", "Event@2026!")
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
    ensure_auth_store()


def read_json_file(path: Path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return fallback
    except json.JSONDecodeError:
        return fallback


def write_json_file(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


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
    if password.lower() in ("password", "admin123456", "event@2026!"):
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


def save_config(text: str, actor: str = "", note: str = "") -> dict:
    require_write()
    payload = config_payload(text)
    bad = [item for item in payload["issues"] if item.get("level") == "bad"]
    if bad:
        return {**payload, "ok": False, "error": "config has blocking validation errors"}
    backup = backup_file(CONFIG_PATH, "event-config")
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(payload["normalizedText"], encoding="utf-8")
    append_history("config.save", actor, note, {"backup": backup})
    return {**config_payload(), "backup": backup}


def apply_config(text: str | None, actor: str = "", note: str = "") -> dict:
    require_write()
    if text is not None:
        saved = save_config(text, actor, note)
        if not saved.get("ok"):
            return saved
    payload = config_payload()
    bad = [item for item in payload["issues"] if item.get("level") == "bad"]
    if bad:
        return {**payload, "ok": False, "error": "config has blocking validation errors"}
    backup = backup_file(ENV_PATH, "env")
    rendered = merge_env_file(ENV_PATH, payload["env"])
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    ENV_PATH.write_text(rendered, encoding="utf-8")
    append_history("config.apply", actor, note, {"backup": backup, "envKeys": sorted(payload["env"])})
    return {
        **config_payload(),
        "envBackup": backup,
        "needsRedeploy": True,
        "nextStep": "cd librenms+grafana && ./apply-env.sh",
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


def rollback_config(actor: str = "", note: str = "") -> dict:
    require_write()
    config_backups = sorted(HISTORY_DIR.glob("event-config-*"), reverse=True)
    env_backups = sorted(HISTORY_DIR.glob("env-*"), reverse=True)
    restored = {}
    if config_backups:
        shutil.copy2(config_backups[0], CONFIG_PATH)
        restored["config"] = str(config_backups[0])
    if env_backups:
        shutil.copy2(env_backups[0], ENV_PATH)
        restored["env"] = str(env_backups[0])
    append_history("config.rollback", actor, note, restored)
    return {**config_payload(), "restored": restored, "needsRedeploy": bool(restored)}


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


def export_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in [CONFIG_PATH, ENV_PATH, INCIDENT_PATH, WORKDIR / "README.md", WORKDIR / "docker-compose.yml"]:
            if path.exists():
                zf.write(path, arcname=path.name)
        manifest = json.dumps(delivery_manifest(), ensure_ascii=False, indent=2)
        zf.writestr("platform-manifest.json", manifest)
    return buffer.getvalue()


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

    def _send_bytes(self, body: bytes, filename: str):
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw.strip() else {}

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
            elif path == "/config":
                require_auth(self)
                payload = config_payload()
                payload["history"] = read_json_file(STATE_DIR / "history.json", [])[:20]
                self._send_json(payload)
            elif path == "/incidents":
                require_auth(self)
                self._send_json({"ok": True, "incidents": incident_list()})
            elif path == "/delivery/manifest":
                require_auth(self)
                self._send_json(delivery_manifest())
            elif path == "/delivery/export" or path == "/config/export":
                require_auth(self)
                self._send_bytes(export_zip(), f"event-platform-{stamp()}.zip")
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
                self._send_json(save_config(data.get("text", ""), data.get("actor", auth["username"]), data.get("note", "")))
            elif path == "/config/apply":
                auth = require_auth(self)
                text = data.get("text") if "text" in data else None
                self._send_json(apply_config(text, data.get("actor", auth["username"]), data.get("note", "")))
            elif path == "/config/rollback":
                auth = require_auth(self)
                self._send_json(rollback_config(data.get("actor", auth["username"]), data.get("note", "")))
            elif path == "/config/import":
                auth = require_auth(self)
                self._send_json(save_config(data.get("text", ""), data.get("actor", auth["username"]), "import"))
            elif path == "/incidents":
                require_auth(self)
                self._send_json({"ok": True, "incident": new_incident(data)})
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
    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"[platform-api] listening on :{port}", flush=True)
    server.serve_forever()
