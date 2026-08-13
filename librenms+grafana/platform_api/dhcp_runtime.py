"""Runtime orchestration and cache for the platform DHCP console."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
import re
import threading
import time

from cisco_dhcp import (
    attach_dhcp_pool_exclusions,
    parse_cisco_arp_entries,
    parse_cisco_dhcp_bindings,
    parse_cisco_dhcp_conflicts,
    parse_cisco_dhcp_excluded,
    parse_cisco_dhcp_pools,
    parse_cisco_dhcp_statistics,
)
from platform_api import dhcp_telnet


DHCP_LOCK = threading.Lock()
DHCP_CACHE: dict = {}


@dataclass(frozen=True)
class DhcpRuntimeContext:
    core_host: Callable[[], str]
    telnet_context: Callable[[], dhcp_telnet.DhcpTelnetContext]
    connection_settings: Callable[[], dict]
    refresh_seconds: int
    error_factory: type[Exception]
    clock: Callable[[], float] = time.time
    monotonic: Callable[[], float] = time.monotonic


def clear_cache() -> None:
    DHCP_CACHE.clear()


def collect_cisco_dhcp(context: DhcpRuntimeContext, host: str) -> dict:
    session = None
    warnings: list[str] = []
    try:
        telnet_context = context.telnet_context()
        session = dhcp_telnet._open_cisco_telnet(telnet_context, host)
        dhcp_telnet._telnet_command(
            telnet_context, session, "terminal length 0",
        )
        pool_output = dhcp_telnet._telnet_command(
            telnet_context, session, "show ip dhcp pool",
        )
        if re.search(r"(?im)^\s*%\s*(?:Invalid input|Unknown command)", pool_output):
            raise context.error_factory(
                HTTPStatus.BAD_GATEWAY,
                "核心交换机不支持 show ip dhcp pool",
            )

        optional_outputs = {}
        for key, command in (
            ("conflicts", "show ip dhcp conflict"),
            ("statistics", "show ip dhcp server statistics"),
            ("excluded", "show running-config | include ^ip dhcp excluded-address"),
        ):
            output = dhcp_telnet._telnet_command(
                telnet_context, session, command,
            )
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
    except context.error_factory:
        raise
    except (EOFError, OSError) as exc:
        raise context.error_factory(
            HTTPStatus.BAD_GATEWAY,
            f"无法读取核心交换机 DHCP：{exc}",
        )
    finally:
        if session is not None:
            try:
                session.write(b"exit\n")
                session.close()
            except Exception:
                pass


def get_dhcp_bindings(context: DhcpRuntimeContext) -> dict:
    """Read exact leases and current ARP neighbours on operator request."""
    host = context.core_host()
    if not DHCP_LOCK.acquire(blocking=False):
        raise context.error_factory(
            HTTPStatus.CONFLICT,
            "DHCP 面板正在读取交换机，请稍后再查询已用 IP",
        )
    session = None
    try:
        telnet_context = context.telnet_context()
        session = dhcp_telnet._open_cisco_telnet(telnet_context, host)
        dhcp_telnet._telnet_command(
            telnet_context, session, "terminal length 0",
        )
        output = dhcp_telnet._telnet_command(
            telnet_context, session, "show ip dhcp binding",
        )
        if re.search(r"(?im)^\s*%\s*(?:Invalid input|Unknown command)", output):
            raise context.error_factory(
                HTTPStatus.BAD_GATEWAY,
                "核心交换机不支持 show ip dhcp binding",
            )
        bindings = parse_cisco_dhcp_bindings(output)
        arp_output = dhcp_telnet._telnet_command(
            telnet_context, session, "show ip arp",
        )
        arp_warning = ""
        if re.search(r"(?im)^\s*%\s*(?:Invalid input|Unknown command)", arp_output):
            arp_output = dhcp_telnet._telnet_command(
                telnet_context, session, "show arp",
            )
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
            "capturedAt": int(context.clock()),
        }
    except context.error_factory:
        raise
    except (EOFError, OSError) as exc:
        raise context.error_factory(
            HTTPStatus.BAD_GATEWAY,
            f"无法读取核心交换机 DHCP 租约：{exc}",
        )
    finally:
        if session is not None:
            try:
                session.write(b"exit\n")
                session.close()
            except Exception:
                pass
        DHCP_LOCK.release()


def test_dhcp_connection(context: DhcpRuntimeContext) -> dict:
    """Test the configured core switch login without collecting DHCP data."""
    host = context.core_host()
    if not DHCP_LOCK.acquire(blocking=False):
        raise context.error_factory(
            HTTPStatus.CONFLICT,
            "DHCP 面板正在读取交换机，请稍后再测试连接",
        )
    session = None
    started = context.monotonic()
    try:
        telnet_context = context.telnet_context()
        session = dhcp_telnet._open_cisco_telnet(telnet_context, host)
        privilege_output = dhcp_telnet._telnet_command(
            telnet_context, session, "show privilege",
        )
        match = re.search(r"(?i)privilege\s+level\s+(?:is\s+)?(\d+)", privilege_output)
        privilege_level = int(match.group(1)) if match else None
        privileged = privilege_level == 15
        if privilege_level is None:
            message = "Telnet 登录成功，交换机未返回权限级别"
        elif privileged:
            message = "Telnet 登录成功，已进入特权模式"
        else:
            message = f"Telnet 登录成功，当前权限级别 {privilege_level}"
        settings = context.connection_settings()
        return {
            "ok": True,
            "host": host,
            "port": settings["port"],
            "login": True,
            "privileged": privileged,
            "privilegeLevel": privilege_level,
            "latencyMs": round((context.monotonic() - started) * 1000),
            "message": message,
            "testedAt": int(context.clock()),
        }
    except context.error_factory:
        raise
    except (EOFError, OSError) as exc:
        raise context.error_factory(
            HTTPStatus.BAD_GATEWAY,
            f"无法连接核心交换机 Telnet：{exc}",
        )
    finally:
        if session is not None:
            try:
                session.write(b"exit\n")
                session.close()
            except Exception:
                pass
        DHCP_LOCK.release()


def _cached_dhcp_payload(
    context: DhcpRuntimeContext,
    refreshing: bool = False,
) -> dict | None:
    payload = DHCP_CACHE.get("payload")
    if not payload:
        return None
    age = max(0, context.monotonic() - float(DHCP_CACHE.get("monotonic") or 0))
    return {
        **payload,
        "cached": True,
        "cacheAgeSeconds": round(age, 1),
        "refreshing": refreshing,
    }


def get_dhcp_dashboard(
    context: DhcpRuntimeContext,
    force: bool = False,
) -> dict:
    host = context.core_host()
    cached = _cached_dhcp_payload(context)
    cache_seconds = max(10, context.refresh_seconds - 5)
    # Even the manual refresh button cannot create more than one switch session
    # every 30 seconds. This keeps the read-only endpoint harmless if a browser
    # is double-clicked or several operators open it together.
    hard_minimum_seconds = 30
    if (
        cached
        and cached.get("host") == host
        and (
            cached.get("cacheAgeSeconds", cache_seconds) < hard_minimum_seconds
            or (
                not force
                and cached.get("cacheAgeSeconds", cache_seconds) < cache_seconds
            )
        )
    ):
        return cached
    if not DHCP_LOCK.acquire(blocking=False):
        busy = _cached_dhcp_payload(context, refreshing=True)
        if busy and busy.get("host") == host:
            return busy
        raise context.error_factory(
            HTTPStatus.CONFLICT,
            "DHCP 面板正在刷新，请稍后再试",
        )
    try:
        # Recheck after acquiring the lock in case another request just finished.
        cached = _cached_dhcp_payload(context)
        if (
            cached
            and cached.get("host") == host
            and (
                cached.get("cacheAgeSeconds", cache_seconds) < hard_minimum_seconds
                or (
                    not force
                    and cached.get("cacheAgeSeconds", cache_seconds) < cache_seconds
                )
            )
        ):
            return cached
        collection_started = context.monotonic()
        payload = {
            **collect_cisco_dhcp(context, host),
            "capturedAt": int(context.clock()),
            "collectionSeconds": round(
                context.monotonic() - collection_started,
                2,
            ),
            "refreshSeconds": context.refresh_seconds,
            "cached": False,
            "cacheAgeSeconds": 0,
            "refreshing": False,
        }
        DHCP_CACHE.clear()
        DHCP_CACHE.update({
            "payload": payload,
            "monotonic": context.monotonic(),
        })
        return payload
    finally:
        DHCP_LOCK.release()
