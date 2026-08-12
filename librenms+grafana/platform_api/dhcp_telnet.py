"""Cisco Telnet transport used by the platform DHCP runtime."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
import re

try:
    from telnetlib3.telnetlib import Telnet
except ImportError:  # Python 3.12 developer/test fallback; production pins telnetlib3.
    from telnetlib import Telnet


CISCO_PROMPT_RE = br"(?m)^[A-Za-z0-9_.:/()\[\]-]+[>#][ \t]*\r?$"
CISCO_PRIV_PROMPT_RE = br"(?m)^[A-Za-z0-9_.:/()\[\]-]+#[ \t]*\r?$"
CISCO_USER_PROMPT_RE = br"(?m)^[A-Za-z0-9_.:/()\[\]-]+>[ \t]*\r?$"
CISCO_MORE_RE = br"(?i)--More--|<---\s*More\s*--->"


ErrorFactory = Callable[[int, str], Exception]


@dataclass(frozen=True)
class DhcpTelnetContext:
    timeout: int
    get_settings: Callable[[], dict]
    error_factory: ErrorFactory


def _telnet_expect(
    context: DhcpTelnetContext,
    session,
    patterns: list[bytes],
    step: str,
):
    index, match, output = session.expect(patterns, context.timeout)
    decoded = (output or b"").decode("utf-8", errors="replace")
    if index < 0:
        raise context.error_factory(
            HTTPStatus.BAD_GATEWAY,
            f"核心交换机 Telnet {step}超时",
        )
    return index, match, decoded


def _telnet_command(
    context: DhcpTelnetContext,
    session,
    command: str,
) -> str:
    session.write(command.encode("ascii") + b"\n")
    chunks = []
    for _page in range(100):
        index, _match, output = _telnet_expect(
            context,
            session,
            [CISCO_PROMPT_RE, CISCO_MORE_RE],
            f"执行 {command} ",
        )
        chunks.append(output)
        if index == 0:
            break
        session.write(b" ")
    else:
        raise context.error_factory(
            HTTPStatus.BAD_GATEWAY,
            "核心交换机分页输出超过安全上限",
        )
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


def _open_cisco_telnet(context: DhcpTelnetContext, host: str):
    settings = context.get_settings()
    username = settings["username"]
    password = settings["password"]
    enable_password = settings["enablePassword"]
    if not password:
        raise context.error_factory(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "尚未配置核心交换机 Telnet 密码，请先在赛事控制台填写",
        )
    session = Telnet(host, settings["port"], context.timeout)
    username_prompt = br"(?im)^(?:user ?name|login):[ \t]*\r?$"
    password_prompt = br"(?im)^password:[ \t]*\r?$"
    command_prompt = CISCO_PROMPT_RE
    failed_prompt = br"(?i)(?:login invalid|authentication failed|access denied)"
    index, _match, _output = _telnet_expect(
        context,
        session,
        [username_prompt, password_prompt, command_prompt, failed_prompt],
        "登录",
    )
    if index == 3:
        raise context.error_factory(
            HTTPStatus.BAD_GATEWAY,
            "核心交换机拒绝 Telnet 登录",
        )
    if index == 0:
        if not username:
            raise context.error_factory(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "交换机要求用户名，但尚未配置 Telnet 用户名",
            )
        session.write(username.encode("utf-8") + b"\n")
        index, _match, _output = _telnet_expect(
            context,
            session,
            [password_prompt, command_prompt, failed_prompt],
            "用户名验证",
        )
        if index == 2:
            raise context.error_factory(
                HTTPStatus.BAD_GATEWAY,
                "核心交换机拒绝 Telnet 用户名",
            )
        if index == 1:
            return session
        index = 0
    if index in (0, 1):
        session.write(password.encode("utf-8") + b"\n")
        index, match, _output = _telnet_expect(
            context,
            session,
            [command_prompt, failed_prompt, password_prompt],
            "密码验证",
        )
        if index != 0:
            raise context.error_factory(
                HTTPStatus.BAD_GATEWAY,
                "核心交换机 Telnet 密码错误",
            )
        prompt = (match.group(0) if match else b"").strip()
        if prompt.endswith(b">") and enable_password:
            session.write(b"enable\n")
            enable_index, _match, _output = _telnet_expect(
                context,
                session,
                [
                    password_prompt,
                    CISCO_PRIV_PROMPT_RE,
                    failed_prompt,
                    CISCO_USER_PROMPT_RE,
                ],
                "进入特权模式",
            )
            if enable_index == 0:
                session.write(enable_password.encode("utf-8") + b"\n")
                enable_index, _match, _output = _telnet_expect(
                    context,
                    session,
                    [
                        CISCO_PRIV_PROMPT_RE,
                        failed_prompt,
                        password_prompt,
                        CISCO_USER_PROMPT_RE,
                    ],
                    "特权密码验证",
                )
            elif enable_index == 1:
                enable_index = 0
            if enable_index != 0:
                raise context.error_factory(
                    HTTPStatus.BAD_GATEWAY,
                    "核心交换机 Enable 密码错误",
                )
    return session
