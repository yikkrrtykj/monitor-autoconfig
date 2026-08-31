"""Authentication state and session behavior independent of HTTP routing."""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import hmac
from http import HTTPStatus
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Any, Callable

from .storage import read_json_file, write_json_file


PASSWORD_HASH_ITERATIONS = 260_000


class AuthError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.payload = {"ok": False, "error": message, **extra}


@dataclass
class AuthContext:
    auth_path: Path
    enabled: bool = True
    admin_user: str = "admin"
    default_password: str = "global123!@#"
    cookie_name: str = "platform_session"
    cookie_secure: bool = False
    session_seconds: int = 8 * 3600
    password_min_length: int = 10
    failure_window_seconds: int = 300
    failure_limit: int = 5
    lock_seconds: int = 900
    sessions: dict[str, dict] = field(default_factory=dict)
    failures: dict[str, dict] = field(default_factory=dict)
    failures_lock: threading.Lock = field(default_factory=threading.Lock)
    history_writer: Callable[[str, str, str, dict], None] | None = None


def b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(
    password: str,
    salt: bytes | None = None,
    iterations: int = PASSWORD_HASH_ITERATIONS,
) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations,
    )
    return f"pbkdf2_sha256${iterations}${b64encode(salt)}${b64encode(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, digest = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        expected = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), b64decode(salt), int(iterations),
        )
        return hmac.compare_digest(b64encode(expected), digest)
    except Exception:
        return False


def ensure_auth_store(context: AuthContext) -> None:
    if not context.enabled or context.auth_path.exists():
        return
    write_json_file(context.auth_path, {
        "username": context.admin_user,
        "passwordHash": hash_password(context.default_password),
        "createdAt": int(time.time()),
        "passwordChangedAt": None,
    })


def read_auth_store(context: AuthContext) -> dict:
    ensure_auth_store(context)
    store = read_json_file(context.auth_path, {})
    if not store.get("username") or not store.get("passwordHash"):
        store = {
            "username": context.admin_user,
            "passwordHash": hash_password(context.default_password),
            "createdAt": int(time.time()),
            "passwordChangedAt": None,
        }
        write_json_file(context.auth_path, store)
    elif "mustChangePassword" in store:
        # Older appliances persisted this first-login gate. Authentication now
        # depends only on the stored password hash and a valid session, so
        # migrate the obsolete field without resetting operator credentials.
        store.pop("mustChangePassword", None)
        write_json_file(context.auth_path, store)
    return store


def write_auth_store(context: AuthContext, store: dict) -> None:
    store["updatedAt"] = int(time.time())
    write_json_file(context.auth_path, store)


def password_strength_error(context: AuthContext, password: str) -> str | None:
    if len(password or "") < context.password_min_length:
        return f"新密码至少 {context.password_min_length} 位"
    if password == context.default_password:
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


def prune_sessions(context: AuthContext, now: float | None = None) -> None:
    current = time.time() if now is None else now
    expired = [
        token for token, session in context.sessions.items()
        if session.get("expires", 0) <= current
    ]
    for token in expired:
        context.sessions.pop(token, None)


def create_session(
    context: AuthContext,
    username: str,
    now: float | None = None,
) -> str:
    current = time.time() if now is None else now
    prune_sessions(context, current)
    token = secrets.token_urlsafe(32)
    context.sessions[token] = {
        "username": username,
        "expires": current + context.session_seconds,
    }
    return token


def current_session(context: AuthContext, handler: Any) -> dict | None:
    if not context.enabled:
        return {
            "username": "local",
            "expires": time.time() + context.session_seconds,
        }
    prune_sessions(context)
    token = parse_cookies(handler.headers.get("Cookie", "")).get(context.cookie_name)
    return context.sessions.get(token or "")


def session_cookie(
    context: AuthContext,
    token: str,
    max_age: int | None = None,
) -> str:
    age = context.session_seconds if max_age is None else max_age
    parts = [
        f"{context.cookie_name}={token}",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        f"Max-Age={age}",
    ]
    if context.cookie_secure:
        parts.append("Secure")
    return "; ".join(parts)


def clear_session_cookie(context: AuthContext) -> str:
    parts = [
        f"{context.cookie_name}=", "Path=/", "HttpOnly", "SameSite=Lax",
        "Max-Age=0",
    ]
    if context.cookie_secure:
        parts.append("Secure")
    return "; ".join(parts)


def auth_status(context: AuthContext, handler: Any) -> dict:
    if not context.enabled:
        return {
            "ok": True, "enabled": False, "authenticated": True,
            "username": "local", "mustChangePassword": False,
        }
    store = read_auth_store(context)
    session = current_session(context, handler)
    authenticated = bool(session)
    return {
        "ok": True,
        "enabled": True,
        "authenticated": authenticated,
        "username": store.get("username", context.admin_user) if authenticated else "",
        "defaultUser": store.get("username", context.admin_user),
        "mustChangePassword": False,
        "sessionExpiresAt": int(session.get("expires", 0)) if session else 0,
    }


def require_auth(
    context: AuthContext,
    handler: Any,
) -> dict:
    if not context.enabled:
        return {"username": "local"}
    store = read_auth_store(context)
    session = current_session(context, handler)
    if not session:
        raise AuthError(
            HTTPStatus.UNAUTHORIZED, "需要登录", authenticated=False,
        )
    return {
        "username": session.get("username") or store.get("username")
        or context.admin_user,
    }


def auth_failure_keys(username: str, client_ip: str) -> tuple[str, str]:
    return (
        f"ip:{client_ip or 'unknown'}",
        f"user:{str(username or '').strip().lower()}",
    )


def auth_lock_remaining(
    context: AuthContext,
    username: str,
    client_ip: str,
    now: float | None = None,
) -> int:
    current = time.time() if now is None else now
    with context.failures_lock:
        remaining = 0
        for key in auth_failure_keys(username, client_ip):
            state = context.failures.get(key) or {}
            locked_until = float(state.get("lockedUntil") or 0)
            if locked_until > current:
                remaining = max(
                    remaining, int(locked_until - current + 0.999),
                )
            elif state:
                recent = [
                    stamp for stamp in state.get("failures", [])
                    if current - stamp <= context.failure_window_seconds
                ]
                if recent:
                    state["failures"] = recent
                else:
                    context.failures.pop(key, None)
        return remaining


def record_auth_failure(
    context: AuthContext,
    username: str,
    client_ip: str,
    now: float | None = None,
) -> int:
    current = time.time() if now is None else now
    locked_until = 0.0
    with context.failures_lock:
        for key in auth_failure_keys(username, client_ip):
            state = context.failures.setdefault(
                key, {"failures": [], "lockedUntil": 0.0},
            )
            recent = [
                stamp for stamp in state.get("failures", [])
                if current - stamp <= context.failure_window_seconds
            ]
            recent.append(current)
            state["failures"] = recent
            if len(recent) >= context.failure_limit:
                state["lockedUntil"] = max(
                    float(state.get("lockedUntil") or 0),
                    current + context.lock_seconds,
                )
            locked_until = max(
                locked_until, float(state.get("lockedUntil") or 0),
            )
    return max(0, int(locked_until - current + 0.999))


def clear_auth_failures(
    context: AuthContext,
    username: str,
    client_ip: str,
) -> None:
    with context.failures_lock:
        for key in auth_failure_keys(username, client_ip):
            context.failures.pop(key, None)


def login_auth(
    context: AuthContext,
    username: str,
    password: str,
    client_ip: str = "",
) -> tuple[dict, str]:
    remaining = auth_lock_remaining(context, username, client_ip)
    if remaining:
        raise AuthError(
            HTTPStatus.TOO_MANY_REQUESTS,
            f"登录失败次数过多，请在 {remaining} 秒后重试",
            authenticated=False,
            retryAfter=remaining,
        )
    store = read_auth_store(context)
    if username != store.get("username") or not verify_password(
        password, store.get("passwordHash", ""),
    ):
        remaining = record_auth_failure(context, username, client_ip)
        if remaining:
            raise AuthError(
                HTTPStatus.TOO_MANY_REQUESTS,
                f"登录失败次数过多，请在 {remaining} 秒后重试",
                authenticated=False,
                retryAfter=remaining,
            )
        raise AuthError(
            HTTPStatus.UNAUTHORIZED, "账号或密码错误", authenticated=False,
        )
    clear_auth_failures(context, username, client_ip)
    token = create_session(context, store["username"])
    return {
        "ok": True,
        "authenticated": True,
        "username": store["username"],
        "mustChangePassword": False,
        "sessionExpiresAt": int(context.sessions[token]["expires"]),
    }, session_cookie(context, token)


def change_password_auth(
    context: AuthContext,
    handler: Any,
    data: dict,
) -> tuple[dict, str]:
    authenticated = require_auth(context, handler)
    current_password = str(data.get("currentPassword") or "")
    new_password = str(data.get("newPassword") or "")
    confirm_password = str(data.get("confirmPassword") or new_password)
    store = read_auth_store(context)
    if not verify_password(current_password, store.get("passwordHash", "")):
        raise AuthError(
            HTTPStatus.FORBIDDEN, "当前密码不正确", authenticated=True,
            mustChangePassword=False,
        )
    if new_password != confirm_password:
        raise AuthError(
            HTTPStatus.BAD_REQUEST, "两次输入的新密码不一致", authenticated=True,
        )
    strength_error = password_strength_error(context, new_password)
    if strength_error:
        raise AuthError(
            HTTPStatus.BAD_REQUEST, strength_error, authenticated=True,
        )
    store["passwordHash"] = hash_password(new_password)
    store["passwordChangedAt"] = int(time.time())
    write_auth_store(context, store)
    context.sessions.clear()
    token = create_session(context, authenticated["username"])
    if context.history_writer:
        context.history_writer(
            "auth.password_change", authenticated["username"],
            "password changed", {},
        )
    return {
        "ok": True,
        "authenticated": True,
        "username": authenticated["username"],
        "mustChangePassword": False,
        "sessionExpiresAt": int(context.sessions[token]["expires"]),
    }, session_cookie(context, token)


def logout_auth(context: AuthContext, handler: Any) -> None:
    token = parse_cookies(handler.headers.get("Cookie", "")).get(context.cookie_name)
    if token:
        context.sessions.pop(token, None)
