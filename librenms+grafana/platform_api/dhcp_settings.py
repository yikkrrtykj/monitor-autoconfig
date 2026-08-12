"""Private credential storage and sanitized API payloads for DHCP Telnet."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
import os
from pathlib import Path
import time

from platform_api import storage as platform_storage


ErrorFactory = Callable[[int, str], Exception]


@dataclass(frozen=True)
class DhcpSettingsContext:
    settings_path: Path
    default_username: str
    default_password: str
    default_enable_password: str
    default_port: int
    write_enabled: bool
    switch_timeout: int
    refresh_seconds: int
    core_host: Callable[[], str]
    cache_clear: Callable[[], None]
    error_factory: ErrorFactory


def dhcp_connection_settings(context: DhcpSettingsContext) -> dict:
    """Return runtime Telnet settings, preferring the private console store."""
    stored = platform_storage.read_json_file(context.settings_path, {})
    if not isinstance(stored, dict):
        stored = {}
    try:
        port = int(stored.get("port", context.default_port))
    except (TypeError, ValueError):
        port = context.default_port
    return {
        "username": str(stored.get("username", context.default_username) or "").strip(),
        "password": str(stored.get("password", context.default_password) or ""),
        "enablePassword": str(
            stored.get("enablePassword", context.default_enable_password) or ""
        ),
        "port": max(1, min(65535, port)),
        "source": "console" if context.settings_path.exists() else "environment",
    }


def get_dhcp_settings(context: DhcpSettingsContext) -> dict:
    settings = dhcp_connection_settings(context)
    return {
        "ok": True,
        "host": context.core_host(),
        "username": settings["username"],
        "port": settings["port"],
        "passwordConfigured": bool(settings["password"]),
        "enablePasswordConfigured": bool(settings["enablePassword"]),
        "source": settings["source"],
        "timeoutSeconds": context.switch_timeout,
        "refreshSeconds": context.refresh_seconds,
    }


def save_dhcp_settings(context: DhcpSettingsContext, data: dict) -> dict:
    if not context.write_enabled:
        raise context.error_factory(
            HTTPStatus.FORBIDDEN,
            "当前环境不允许保存 Telnet 配置",
        )
    current = dhcp_connection_settings(context)
    username = str(data.get("username", current["username"]) or "").strip()
    password_input = data.get("password")
    enable_input = data.get("enablePassword")
    password = (
        current["password"]
        if password_input in (None, "")
        else str(password_input)
    )
    enable_password = (
        current["enablePassword"]
        if enable_input in (None, "")
        else str(enable_input)
    )
    try:
        port = int(data.get("port", current["port"]))
    except (TypeError, ValueError):
        raise context.error_factory(
            HTTPStatus.BAD_REQUEST,
            "Telnet 端口必须是数字",
        )
    if not 1 <= port <= 65535:
        raise context.error_factory(
            HTTPStatus.BAD_REQUEST,
            "Telnet 端口必须在 1-65535 之间",
        )
    if len(username) > 128:
        raise context.error_factory(HTTPStatus.BAD_REQUEST, "Telnet 用户名过长")
    if len(password) > 512 or len(enable_password) > 512:
        raise context.error_factory(HTTPStatus.BAD_REQUEST, "Telnet 密码过长")
    # 凭据会被原样写进 Telnet 会话，换行/控制字符等于向交换机注入额外命令行。
    for value in (username, password, enable_password):
        if any(ord(ch) < 0x20 or ch == "\x7f" for ch in value):
            raise context.error_factory(
                HTTPStatus.BAD_REQUEST,
                "Telnet 凭据不能包含换行或控制字符",
            )
    platform_storage.write_json_file(context.settings_path, {
        "username": username,
        "password": password,
        "enablePassword": enable_password,
        "port": port,
        "updatedAt": int(time.time()),
    }, mode=0o600)
    try:
        os.chmod(context.settings_path, 0o600)
    except OSError as exc:
        print(f"[platform-api] dhcp settings chmod failed: {exc}", flush=True)
    context.cache_clear()
    return get_dhcp_settings(context)
