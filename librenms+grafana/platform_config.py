"""Structured event configuration helpers for the monitoring platform.

The deployment still consumes .env, but operators edit event-config.yml.  This
module parses a small YAML subset (dict/list/scalar), validates the platform
objects, renders .env keys, and keeps comments/secrets outside the browser UI.
It intentionally uses only the Python standard library so deployment and
recovery do not require additional host Python packages.
"""
from __future__ import annotations

import copy
import json
import ipaddress
import os
import re
import shlex
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from version_info import CURRENT_SCHEMA_VERSION


class ConfigSchemaError(ValueError):
    """Raised when a configuration schema cannot be safely processed."""


class ConfigTooNewError(ConfigSchemaError):
    """Raised when a configuration was created by newer platform software."""


def get_schema_version(config: dict[str, Any]) -> int:
    if not isinstance(config, dict):
        raise ConfigSchemaError("event config must be a mapping")
    if "schema_version" not in config:
        return 0
    version = config["schema_version"]
    if type(version) is not int:
        raise ConfigSchemaError("schema_version must be a non-negative integer")
    if version < 0:
        raise ConfigSchemaError("schema_version must be a non-negative integer")
    return version


def inspect_config_schema(config: dict[str, Any]) -> dict[str, int | bool]:
    original = get_schema_version(config)
    too_new = original > CURRENT_SCHEMA_VERSION
    migration_required = original < CURRENT_SCHEMA_VERSION
    return {
        "original_version": original,
        "current_supported": CURRENT_SCHEMA_VERSION,
        "current_version": original if too_new else CURRENT_SCHEMA_VERSION,
        "migration_required": migration_required,
        "config_too_new": too_new,
    }


def migrate_v0_to_v1(config: dict[str, Any]) -> dict[str, Any]:
    if get_schema_version(config) != 0:
        raise ConfigSchemaError("migrate_v0_to_v1 requires schema 0")
    migrated = copy.deepcopy(config)
    return {"schema_version": 1, **migrated}


SCHEMA_MIGRATIONS = {
    0: migrate_v0_to_v1,
}


def migrate_config(
    config: dict[str, Any],
    target_version: int = CURRENT_SCHEMA_VERSION,
) -> dict[str, Any]:
    if type(target_version) is not int or target_version < 0:
        raise ConfigSchemaError("target schema version must be a non-negative integer")
    original = get_schema_version(config)
    if original > target_version:
        raise ConfigTooNewError(
            f"Refusing to downgrade schema {original}; software supports schema {target_version}."
        )

    migrated = copy.deepcopy(config)
    version = original
    while version < target_version:
        migration = SCHEMA_MIGRATIONS.get(version)
        if migration is None:
            raise ConfigSchemaError(f"no migration available from schema {version} to {version + 1}")
        migrated = migration(migrated)
        next_version = get_schema_version(migrated)
        if next_version != version + 1:
            raise ConfigSchemaError(
                f"migration from schema {version} must produce schema {version + 1}"
            )
        version = next_version
    return migrated



def _strip_comment(line: str) -> str:
    quote = ""
    escaped = False
    for idx, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char in ("'", '"'):
            quote = "" if quote == char else char if not quote else quote
            continue
        if char == "#" and not quote:
            return line[:idx]
    return line


def _scalar(value: str) -> Any:
    value = value.strip()
    if value == "":
        return ""
    if value in ("[]", "{}"):
        return [] if value == "[]" else {}
    if value[0:1] == value[-1:] and value[:1] in ("'", '"'):
        if value.startswith('"'):
            try:
                return json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid quoted scalar: {value}") from exc
        # YAML single-quoted strings escape a literal quote by doubling it;
        # backslashes otherwise stay literal.
        return value[1:-1].replace("''", "'")
    lower = value.lower()
    if lower in ("true", "yes", "on"):
        return True
    if lower in ("false", "no", "off"):
        return False
    if lower in ("null", "none", "~"):
        return None
    if re.fullmatch(r"-?\d+", value):
        try:
            return int(value)
        except ValueError:
            return value
    if re.fullmatch(r"-?\d+\.\d+", value):
        try:
            return float(value)
        except ValueError:
            return value
    return value


def parse_simple_yaml(text: str) -> Any:
    if text.strip().startswith("{"):
        return json.loads(text)

    rows: list[tuple[int, str]] = []
    for raw in text.splitlines():
        clean = _strip_comment(raw).rstrip()
        if not clean.strip():
            continue
        rows.append((len(clean) - len(clean.lstrip(" ")), clean.lstrip(" ")))

    def parse_block(index: int, indent: int) -> tuple[Any, int]:
        if index >= len(rows):
            return {}, index
        is_list = rows[index][0] == indent and rows[index][1].startswith("- ")
        if is_list:
            result = []
            while index < len(rows):
                row_indent, row = rows[index]
                if row_indent < indent:
                    break
                if row_indent != indent or not row.startswith("- "):
                    break
                item = row[2:].strip()
                index += 1
                if not item:
                    value, index = parse_block(index, indent + 2)
                    result.append(value)
                elif ":" in item and not item.startswith(("'", '"')):
                    key, raw_value = item.split(":", 1)
                    obj = {key.strip(): _scalar(raw_value)}
                    while index < len(rows) and rows[index][0] > indent:
                        child_indent, child = rows[index]
                        if child_indent < indent + 2:
                            break
                        if ":" not in child:
                            break
                        ckey, cvalue = child.split(":", 1)
                        if cvalue.strip():
                            obj[ckey.strip()] = _scalar(cvalue)
                            index += 1
                        else:
                            if index + 1 < len(rows) and rows[index + 1][0] > child_indent:
                                value, index = parse_block(index + 1, child_indent + 2)
                                obj[ckey.strip()] = value
                            else:
                                obj[ckey.strip()] = ""
                                index += 1
                    result.append(obj)
                else:
                    result.append(_scalar(item))
            return result, index

        result = {}
        while index < len(rows):
            row_indent, row = rows[index]
            if row_indent < indent:
                break
            if row_indent != indent:
                break
            if ":" not in row:
                raise ValueError(f"Invalid YAML line: {row}")
            key, raw_value = row.split(":", 1)
            key = key.strip()
            if raw_value.strip():
                result[key] = _scalar(raw_value)
                index += 1
            else:
                if index + 1 < len(rows) and rows[index + 1][0] > row_indent:
                    value, index = parse_block(index + 1, indent + 2)
                    result[key] = value
                else:
                    result[key] = ""
                    index += 1
        return result, index

    parsed, pos = parse_block(0, rows[0][0] if rows else 0)
    if pos != len(rows):
        raise ValueError("Could not parse the complete YAML document")
    return parsed


def dump_simple_yaml(value: Any, indent: int = 0) -> str:
    pad = " " * indent
    if isinstance(value, dict):
        lines = []
        for key, child in value.items():
            if child == []:
                lines.append(f"{pad}{key}: []")
                continue
            if child == {}:
                lines.append(f"{pad}{key}: {{}}")
                continue
            if isinstance(child, (dict, list)):
                lines.append(f"{pad}{key}:")
                nested = dump_simple_yaml(child, indent + 2)
                if nested:
                    lines.append(nested)
            else:
                lines.append(f"{pad}{key}: {format_scalar(child)}")
        return "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return f"{pad}[]"
        lines = []
        for child in value:
            if isinstance(child, dict):
                if not child:
                    lines.append(f"{pad}- {{}}")
                    continue
                first = True
                for key, val in child.items():
                    if first:
                        if isinstance(val, (dict, list)):
                            lines.append(f"{pad}- {key}:")
                            lines.append(dump_simple_yaml(val, indent + 4))
                        else:
                            lines.append(f"{pad}- {key}: {format_scalar(val)}")
                        first = False
                    else:
                        if isinstance(val, (dict, list)):
                            lines.append(f"{pad}  {key}:")
                            lines.append(dump_simple_yaml(val, indent + 4))
                        else:
                            lines.append(f"{pad}  {key}: {format_scalar(val)}")
            elif isinstance(child, list):
                lines.append(f"{pad}-")
                lines.append(dump_simple_yaml(child, indent + 2))
            else:
                lines.append(f"{pad}- {format_scalar(child)}")
        return "\n".join(lines)
    return f"{pad}{format_scalar(value)}"


def format_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if text == "":
        return ""
    if re.search(r"[:#\n\r]|^\s|\s$", text):
        return json.dumps(text, ensure_ascii=False)
    return text


def csv(items: Any) -> str:
    if items is None:
        return ""
    if isinstance(items, dict):
        return ""
    if isinstance(items, str):
        return items
    if isinstance(items, (int, float)):
        return str(items)
    if isinstance(items, list):
        return ",".join(str(item) for item in items if str(item).strip())
    return str(items)


def split_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (int, float)):
        return [str(value)]
    if isinstance(value, str):
        raw_items = re.split(r"[\n,]+", value)
    elif isinstance(value, list):
        raw_items = []
        for item in value:
            raw_items.extend(split_values(item))
    else:
        raw_items = [str(value)]
    return [str(item).strip() for item in raw_items if str(item).strip()]


def named_targets(items: list[dict[str, Any]], key: str = "ip") -> str:
    targets = []
    for item in items or []:
        if isinstance(item, dict):
            values = split_values(item.get(key) or item.get("target") or "")
            name = str(item.get("name") or "").strip()
        else:
            values = split_values(item)
            name = ""
        for index, value in enumerate(values):
            if name:
                label = name if len(values) == 1 else f"{name}-{index + 1}"
                targets.append(f"{label}:{value}")
            else:
                targets.append(value)
    return ",".join(targets)


def switch_discovery_range(ranges: Any, core_ip: str = "") -> str:
    """Pick the address ranges the switch discovery loop should probe so
    operators can leave the per-switch list empty. CIDR, single IPs and
    last-octet ranges are all passed through -- the loop ICMP-gates each address
    and only SNMP-queries the live ones, so probing a sparse /24 stays cheap.
    The standalone core IP is dropped because it already has its own ping/SNMP
    target; any other already-monitored IPs are filtered by the loop itself.
    The loop keeps only addresses that answer and names each by its real SNMP
    hostname, so offline IPs never reach the big screen."""
    core_ip = str(core_ip or "").strip()
    entries = [entry for entry in split_values(ranges) if entry != core_ip]
    return ",".join(entries)


def _bandwidth_text(value: Any) -> str:
    """Return a normalized Mbps value, including optional down/up syntax."""
    parts = str(value if value is not None else "").strip().split("/", 1)
    normalized = []
    for part in parts:
        try:
            number = float(part.strip())
        except (TypeError, ValueError):
            return ""
        if number <= 0:
            return ""
        normalized.append(f"{number:g}")
    return "/".join(normalized)


def isp_bandwidth_config(isp: dict[str, Any]) -> str:
    """Render the global fallback plus safely named per-link bandwidth values.

    Per-link limits are identity metadata: they can only follow an explicit
    name that the discovery layer can bind to stable WAN evidence. Unnamed rows
    deliberately fall back to the global limit instead of binding by list or
    ifIndex position.
    """
    default = _bandwidth_text(isp.get("max_bandwidth_mbps")) or "1000"
    links = [item for item in (isp.get("links") or []) if isinstance(item, dict)]
    if not any(_bandwidth_text(item.get("bandwidth_mbps")) for item in links):
        return default

    entries = [f"*:{default}"]
    for item in links:
        label = str(item.get("name") or "").strip()
        if not label:
            continue
        bandwidth = _bandwidth_text(item.get("bandwidth_mbps")) or default
        entries.append(f"{label}:{bandwidth}")
    return ",".join(entries)


def compose_profiles(existing: Any, unifi_enabled: bool, feishu_enabled: bool = False) -> str:
    profiles = [item.strip() for item in str(existing or "").split(",") if item.strip()]
    # Both profiles are managed from the console.  Recompute them on every
    # render so removing credentials actually disables the corresponding
    # optional service instead of leaving a stale profile in .env.
    profiles = [item for item in profiles if item not in ("unifi", "feishu")]
    if unifi_enabled:
        profiles.append("unifi")
    if feishu_enabled:
        profiles.append("feishu")
    return ",".join(dict.fromkeys(profiles))


TEAM_ORDER_LAYOUTS = {
    "tournament-64-2layer",
    "tournament-64-233",
    "tournament-64-332",
}


def normalize_config(config: dict[str, Any]) -> dict[str, Any]:
    config = dict(config) if isinstance(config, dict) else {}
    for section in ("event", "networks", "devices", "isp", "unifi", "alerts", "security", "snmp", "topology"):
        value = config.get(section)
        config[section] = dict(value) if isinstance(value, dict) else {}
    event = config["event"]
    if not isinstance(event.get("team_orders", {}), dict):
        event["team_orders"] = {}
    networks = config["networks"]
    # Preserve the historical fallback only for legacy configs that predate
    # this key. An explicit blank is the safe fresh-appliance setting and must
    # not silently activate a management subnet scan.
    if "firewall_management_ranges" not in networks:
        networks["firewall_management_ranges"] = "192.168.9.0/24"
    devices = config["devices"]
    for key in ("switches", "stage_switches", "access_switches", "servers"):
        if key in devices and not isinstance(devices[key], list):
            devices[key] = []
    devices.setdefault("switches", [])
    if "stage_switches" not in devices:
        devices["stage_switches"] = devices.get("switches") or []
    devices.setdefault("access_switches", [])
    devices.setdefault("servers", [])
    for key in ("core", "firewall"):
        if key in devices and not isinstance(devices[key], dict):
            devices[key] = {}
    if not isinstance(config["isp"].get("links", []), list):
        config["isp"]["links"] = []
    return config


def validate_config(config: dict[str, Any]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    if not isinstance(config, dict):
        return [{"level": "bad", "path": "$", "message": "配置根节点必须是对象"}]

    def add(level: str, path: str, message: str) -> None:
        issues.append({"level": level, "path": path, "message": message})

    for section in ("event", "networks", "devices", "isp", "unifi", "alerts", "security", "snmp", "topology"):
        if section in config and not isinstance(config.get(section), dict):
            add("bad", section, f"{section} 必须是对象")
    raw_event = config.get("event") if isinstance(config.get("event"), dict) else {}
    if "team_orders" in raw_event and not isinstance(raw_event.get("team_orders"), dict):
        add("bad", "event.team_orders", "队号位置必须是对象")
    raw_devices = config.get("devices") if isinstance(config.get("devices"), dict) else {}
    for key in ("core", "firewall"):
        if key in raw_devices and not isinstance(raw_devices.get(key), dict):
            add("bad", f"devices.{key}", f"devices.{key} 必须是对象")
    for key in ("switches", "stage_switches", "access_switches", "servers"):
        if key in raw_devices and not isinstance(raw_devices.get(key), list):
            add("bad", f"devices.{key}", f"devices.{key} 必须是列表")
    raw_isp = config.get("isp") if isinstance(config.get("isp"), dict) else {}
    if "links" in raw_isp and not isinstance(raw_isp.get("links"), list):
        add("bad", "isp.links", "isp.links 必须是列表")
    raw_topology = config.get("topology") if isinstance(config.get("topology"), dict) else {}
    topology_source = str(raw_topology.get("data_source") or "hybrid").strip().lower()
    if topology_source not in ("hybrid", "librenms", "direct-snmp"):
        add("bad", "topology.data_source", "拓扑数据源必须是 hybrid、librenms 或 direct-snmp")
    for key, label in (
        ("poll_max_age_seconds", "拓扑 poll 数据最大年龄"),
        ("discovery_max_age_seconds", "拓扑 discovery 数据最大年龄"),
    ):
        if key not in raw_topology or raw_topology.get(key) in (None, ""):
            continue
        value = raw_topology.get(key)
        if type(value) is not int or value < 0:
            add("bad", f"topology.{key}", f"{label}必须是非负整数秒")

    config = normalize_config(config)
    devices = config["devices"]
    networks = config["networks"]
    isp = config["isp"]
    event = config["event"]

    for layout_id, order in (event.get("team_orders") or {}).items():
        path = f"event.team_orders.{layout_id}"
        if layout_id not in TEAM_ORDER_LAYOUTS:
            add("bad", path, "不支持的比赛布局")
            continue
        if (
            not isinstance(order, list)
            or len(order) != 16
            or any(type(team) is not int for team in order)
            or sorted(order) != list(range(1, 17))
        ):
            add("bad", path, "队号位置必须完整包含第 1-16 队，且不能重复")

    def valid_ip(value: Any) -> bool:
        try:
            return ipaddress.ip_address(str(value or "").strip()).version == 4
        except ValueError:
            return False

    def valid_cidr(value: Any) -> bool:
        try:
            return ipaddress.ip_network(str(value or "").strip(), strict=False).version == 4
        except ValueError:
            return False

    def range_size(value: Any) -> int | None:
        text = str(value or "").strip()
        try:
            if "/" in text:
                return ipaddress.ip_network(text, strict=False).num_addresses
            if "-" not in text:
                ipaddress.IPv4Address(text)
                return 1
            start_raw, end_raw = [part.strip() for part in text.split("-", 1)]
            start = ipaddress.IPv4Address(start_raw)
            if re.fullmatch(r"\d{1,3}", end_raw):
                end = ipaddress.IPv4Address(f"{start_raw.rsplit('.', 1)[0]}.{end_raw}")
            else:
                end = ipaddress.IPv4Address(end_raw)
            size = int(end) - int(start) + 1
            return size if size > 0 else None
        except ValueError:
            return None

    def check_ip(value: Any, path: str, label: str, required: bool = False) -> None:
        text = str(value or "").strip()
        if not text:
            if required:
                add("bad", path, f"{label}必填")
            return
        if not valid_ip(text):
            add("bad", path, f"{label}不是有效 IPv4 地址")

    def check_ip_values(value: Any, path: str, label: str) -> None:
        for index, item in enumerate(split_values(value)):
            check_ip(item, f"{path}[{index}]", label)

    def check_subnets(value: Any, path: str, label: str) -> None:
        for index, item in enumerate(split_values(value)):
            if not valid_cidr(item):
                add("bad", f"{path}[{index}]", f"{label}不是有效 IPv4 CIDR")

    def check_ranges(value: Any, path: str, label: str, max_hosts: int = 4096) -> None:
        for index, item in enumerate(split_values(value)):
            size = range_size(item)
            item_path = f"{path}[{index}]"
            if size is None:
                add("bad", item_path, f"{label}不是有效 IP、CIDR 或 IP 范围")
            elif size > max_hosts:
                add("bad", item_path, f"{label}展开为 {size} 个地址，超过上限 {max_hosts}")

    def check_positive(value: Any, path: str, label: str, minimum: float = 0, maximum: float | None = None) -> None:
        if value in (None, ""):
            return
        try:
            number = float(value)
        except (TypeError, ValueError):
            add("bad", path, f"{label}必须是数字")
            return
        if number <= minimum:
            add("bad", path, f"{label}必须大于 {minimum:g}")
        elif maximum is not None and number > maximum:
            add("bad", path, f"{label}不能大于 {maximum:g}")

    check_ip((devices.get("core") or {}).get("ip"), "devices.core.ip", "核心交换机 IP ", required=True)
    stage_switches = devices.get("stage_switches") if "stage_switches" in devices else devices.get("switches")
    stage_switches = stage_switches or []
    access_switches = devices.get("access_switches") or []
    if not stage_switches:
        add(
            "warn",
            "devices.stage_switches",
            "没有配置赛事交换机：普通交换机仍会按管理网段自动发现，但选手座位识别会跳过",
        )

    explicit_ips: dict[str, str] = {}
    core_ip = str((devices.get("core") or {}).get("ip") or "").strip()
    if core_ip and valid_ip(core_ip):
        explicit_ips[core_ip] = "devices.core.ip"
    for group, label in ((stage_switches, "赛事交换机"), (access_switches, "普通接入交换机"), (devices.get("servers") or [], "服务器")):
        group_path = "stage_switches" if group is stage_switches else "access_switches" if group is access_switches else "servers"
        for idx, item in enumerate(group):
            path = f"devices.{group_path}[{idx}]"
            if not isinstance(item, dict):
                add("bad", path, f"{label}条目必须是对象")
                continue
            ip = str(item.get("ip") or "").strip()
            check_ip(ip, f"{path}.ip", f"{label} IP ", required=True)
            if ip and valid_ip(ip):
                if ip in explicit_ips:
                    add("bad", f"{path}.ip", f"IP {ip} 与 {explicit_ips[ip]} 重复")
                else:
                    explicit_ips[ip] = f"{path}.ip"

    firewall = devices.get("firewall") or {}
    if firewall:
        if not isinstance(firewall, dict):
            add("bad", "devices.firewall", "防火墙必须是对象")
        else:
            check_ip_values(firewall.get("ip"), "devices.firewall.ip", "防火墙 IP")
            check_ip_values(firewall.get("snmp"), "devices.firewall.snmp", "防火墙 SNMP IP")
            check_ip_values(firewall.get("unit_snmp"), "devices.firewall.unit_snmp", "防火墙物理节点 IP")
    if (devices.get("firewall") or {}).get("ip") and not (devices.get("firewall") or {}).get("unit_snmp"):
        add("warn", "devices.firewall.unit_snmp", "建议填写两台物理防火墙 SNMP IP，HA 单机状态和单机离线告警都靠它")

    check_subnets(networks.get("player_subnets"), "networks.player_subnets", "选手有线网段")
    check_subnets(networks.get("wireless_subnets"), "networks.wireless_subnets", "选手无线网段")
    check_ip_values(networks.get("player_gateways"), "networks.player_gateways", "选手网关")
    check_ranges(networks.get("switch_management_ranges"), "networks.switch_management_ranges", "交换机管理范围")
    check_ranges(networks.get("firewall_management_ranges"), "networks.firewall_management_ranges", "防火墙管理范围")
    for field, label in (("player_vlan", "选手 VLAN"), ("wireless_vlan", "无线 VLAN")):
        for index, vlan in enumerate(split_values(networks.get(field))):
            try:
                number = int(vlan)
            except ValueError:
                number = 0
            if not 1 <= number <= 4094:
                add("bad", f"networks.{field}[{index}]", f"{label}必须在 1-4094 之间")
    if not networks.get("player_subnets"):
        add("warn", "networks.player_subnets", "没有配置选手有线网段")
    if not networks.get("switch_management_ranges"):
        add("warn", "networks.switch_management_ranges", "建议填写交换机管理网段，方便 LibreNMS 自动发现")
    # auto_discovery 缺省视为开启——所有读取处必须用同一个默认值，
    # 否则同一份配置在相邻两条校验里被同时当成"开"和"关"。
    isp_auto_discovery = bool(isp.get("auto_discovery", True))
    if not isp_auto_discovery and not isp.get("links"):
        add("warn", "isp.links", "关闭自动发现时建议配置 ISP 探测目标")
    for idx, item in enumerate(isp.get("links") or []):
        path = f"isp.links[{idx}]"
        if not isinstance(item, dict):
            add("bad", path, "ISP 条目必须是对象")
            continue
        if item and not item.get("ip"):
            # 自动发现开着时，只填"名称+带宽"来绑定 WAN 口带宽是合法用法：
            # 网关 ping 目标从防火墙路由表自动发现，公网 IP 只影响拓扑展示。
            # 关闭自动发现时，带 ping 探测目标的行同样不该被公网 IP 卡死
            # （IP 只影响拓扑展示，预检会把 bad 升级成部署阻塞项）。
            if isp_auto_discovery or item.get("ping"):
                # Automatic mode discovers the WAN interface and gateway from
                # the firewall. Name + bandwidth is the normal configuration,
                # so an omitted public IP must not produce a warning banner.
                pass
            else:
                add("bad", f"{path}.ip", "运营商公网 IP 必填，用于拓扑展示并加入 LibreNMS")
        else:
            check_ip(item.get("ip"), f"{path}.ip", "运营商公网 IP ")
        check_ip(item.get("ping"), f"{path}.ping", "运营商探测 IP ")
        if item.get("bandwidth_mbps") not in (None, "") and not _bandwidth_text(item.get("bandwidth_mbps")):
            add("bad", f"{path}.bandwidth_mbps", "ISP 带宽必须为正数，或使用 下行/上行 格式")
        elif (
            isp_auto_discovery
            and item.get("bandwidth_mbps") not in (None, "")
            and not str(item.get("name") or "").strip()
        ):
            add(
                "warn",
                f"{path}.bandwidth_mbps",
                "自动发现模式下未命名线路无法安全绑定独立带宽，将使用默认 ISP 带宽",
            )
    if isp.get("max_bandwidth_mbps") not in (None, "") and not _bandwidth_text(isp.get("max_bandwidth_mbps")):
        add("bad", "isp.max_bandwidth_mbps", "默认 ISP 带宽必须为正数，或使用 下行/上行 格式")
    check_positive(isp.get("saturation_percent"), "isp.saturation_percent", "ISP 饱和阈值", 0, 100)
    check_positive(isp.get("down_for_seconds"), "isp.down_for_seconds", "ISP 断线确认时间", 0)
    check_positive(config["alerts"].get("cpu_alert_percent"), "alerts.cpu_alert_percent", "交换机 CPU 告警阈值", 0, 100)
    check_positive(config["alerts"].get("memory_alert_percent"), "alerts.memory_alert_percent", "交换机内存告警阈值", 0, 100)
    check_positive(config["alerts"].get("mac_flap_window_seconds"), "alerts.mac_flap_window_seconds", "MAC 漂移统计窗口", 0)
    check_positive(config["alerts"].get("mac_flap_threshold"), "alerts.mac_flap_threshold", "MAC 漂移次数阈值", 0)

    unifi = config["unifi"]
    if unifi.get("enabled"):
        controller_url = str(unifi.get("controller_url") or "").strip()
        parsed = urlparse(controller_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            add("bad", "unifi.controller_url", "启用 UniFi 时必须填写完整控制器 URL")
        if not str(unifi.get("user") or "").strip():
            add("bad", "unifi.user", "启用 UniFi 时必须填写只读账号")
        if not str(unifi.get("password") or "").strip():
            add("bad", "unifi.password", "启用 UniFi 时必须填写密码")
    return issues


def render_env(config: dict[str, Any], existing: dict[str, str] | None = None) -> dict[str, str]:
    schema = inspect_config_schema(config)
    if schema["config_too_new"]:
        raise ConfigTooNewError(
            f"Refusing to render schema {schema['original_version']}; "
            f"software supports schema {schema['current_supported']}."
        )
    config = migrate_config(config)
    config = normalize_config(config)
    existing = dict(existing or {})
    event = config["event"]
    networks = config["networks"]
    devices = config["devices"]
    isp = config["isp"]
    unifi = config["unifi"]
    alerts = config["alerts"]
    security = config["security"]
    snmp = config["snmp"]
    topology = config["topology"]

    core = devices.get("core") or {}
    core_ip = str(core.get("ip") or "").strip()
    firewall = devices.get("firewall") or {}
    stage_switches = devices.get("stage_switches") if "stage_switches" in devices else devices.get("switches")
    stage_switches = stage_switches or []
    access_switches = devices.get("access_switches") or []
    all_switches = [*stage_switches, *access_switches]
    servers = devices.get("servers") or []
    isp_links = isp.get("links") or []
    snmp_community = snmp.get("community") or existing.get("SNMP_COMMUNITY", "global")
    # Legacy installs kept these only in .env.  Missing YAML keys preserve that
    # value, while an explicitly blank console field intentionally clears it.
    feishu_app_id = alerts.get("feishu_app_id") if "feishu_app_id" in alerts else existing.get("FEISHU_APP_ID", "")
    feishu_app_secret = alerts.get("feishu_app_secret") if "feishu_app_secret" in alerts else existing.get("FEISHU_APP_SECRET", "")
    feishu_chat_id = alerts.get("feishu_chat_id") if "feishu_chat_id" in alerts else existing.get("FEISHU_CHAT_ID", "")
    cpu_alert_percent = float(alerts.get("cpu_alert_percent") if alerts.get("cpu_alert_percent") not in (None, "") else 70)
    memory_alert_percent = float(alerts.get("memory_alert_percent") if alerts.get("memory_alert_percent") not in (None, "") else 80)
    legacy_syslog_alert_types = "native_vlan_mismatch,errdisable,bpduguard,loopback"
    default_syslog_alert_types = "native_vlan_mismatch,mac_flap,errdisable,bpduguard,loopback"
    syslog_alert_types = alerts.get("syslog_alert_types", default_syslog_alert_types)
    # Existing YAML created before MAC-flap aggregation should receive the new
    # safe default (ordinary MACs still require the frequency threshold).
    if str(syslog_alert_types).strip() == legacy_syslog_alert_types:
        syslog_alert_types = default_syslog_alert_types
    core_ping = named_targets([{"ip": core.get("ip")}], "ip") if core.get("ip") else ""
    firewall_ping = named_targets([firewall], "ip")
    firewall_snmp = named_targets([firewall], "snmp")

    # General infrastructure switches reach monitoring through an explicit list
    # and/or SNMP discovery of the management range. Tournament player discovery
    # is deliberately narrower: only stage_switches are rendered into
    # TOURNAMENT_SWITCHES. This keeps unrelated access switches out of the event
    # console while the range continues to feed topology and LibreNMS. Once a
    # range-discovered switch is confirmed, an offline device remains monitored
    # for the 24-hour topology retention window.
    discovery_range = switch_discovery_range(networks.get("switch_management_ranges"), core_ip)
    dist_ping = named_targets(all_switches)
    tournament_switches = named_targets(stage_switches)
    # On a Cisco core the player L3 gateway is the core switch itself, so default
    # the gateway and LibreNMS core hint to the core IP when not set explicitly.
    player_gateways = csv(networks.get("player_gateways")) or core_ip
    topology_source = topology.get("data_source") or existing.get(
        "TOPOLOGY_DATA_SOURCE", "hybrid"
    )
    topology_poll_max_age = (
        topology.get("poll_max_age_seconds")
        if topology.get("poll_max_age_seconds") not in (None, "")
        else existing.get("TOPOLOGY_LIBRENMS_POLL_MAX_AGE_SECONDS", "600")
    )
    topology_discovery_max_age = (
        topology.get("discovery_max_age_seconds")
        if topology.get("discovery_max_age_seconds") not in (None, "")
        else existing.get(
            "TOPOLOGY_LIBRENMS_DISCOVERY_MAX_AGE_SECONDS", "28800"
        )
    )

    env = {
        "EVENT_NAME": event.get("name", ""),
        "BIGSCREEN_DEFAULT_LAYOUT": event.get("default_layout", "tournament-64-2layer"),
        "BIGSCREEN_TEAM_ORDERS": json.dumps(event.get("team_orders") or {}, ensure_ascii=False, separators=(",", ":")),
        "SNMP_COMMUNITY": snmp_community,
        "TOPOLOGY_DATA_SOURCE": topology_source,
        "TOPOLOGY_LIBRENMS_POLL_MAX_AGE_SECONDS": topology_poll_max_age,
        "TOPOLOGY_LIBRENMS_DISCOVERY_MAX_AGE_SECONDS": topology_discovery_max_age,
        "FIREWALL_SNMP_COMMUNITY": snmp.get("firewall_community") or snmp_community,
        "CORE_SWITCH_PING": core_ping,
        "DIST_SWITCH_PING": dist_ping,
        "SWITCH_DISCOVERY_RANGE": discovery_range,
        "TOURNAMENT_SWITCHES": tournament_switches,
        "FIREWALL_PING": firewall_ping,
        "FIREWALL_SNMP_TARGETS": firewall_snmp or firewall_ping,
        "FIREWALL_UNIT_SNMP_TARGETS": named_targets([firewall], "unit_snmp"),
        "SERVER_PING": named_targets(servers),
        "PLAYER_SUBNETS": csv(networks.get("player_subnets")),
        "WIRELESS_SUBNETS": csv(networks.get("wireless_subnets")),
        "PLAYER_GATEWAYS": player_gateways,
        "LIBRENMS_CORE_IP": core_ip,
        "PLAYER_VLAN_IDS": csv(networks.get("player_vlan")),
        "LIBRENMS_DISCOVERY_TARGETS": csv(networks.get("switch_management_ranges")),
        "FIREWALL_DISCOVERY_RANGE": csv(networks.get("firewall_management_ranges")),
        "BIGSCREEN_ISP_AUTO_DISCOVER": str(bool(isp.get("auto_discovery", True))).lower(),
        # 同一个开关也控制“从防火墙路由表自动发现 ISP 网关 ping 目标”。
        "ISP_GATEWAY_AUTO_DISCOVER": str(bool(isp.get("auto_discovery", True))).lower(),
        "FIREWALL_WAN_IF_FILTER": isp.get("wan_if_filter") or "telecom,telcom,unicom,isp,WAN",
        "BIGSCREEN_ISP_NAMES": ",".join(str(item.get("name")) for item in isp_links if item.get("name")),
        "ISP_PING": ",".join(
            f"{item.get('name')}:{item.get('ping')}" if item.get("name") else str(item.get("ping"))
            for item in isp_links if item.get("ping")
        ),
        "BIGSCREEN_ISP_IPS": ",".join(
            f"{item.get('name')}:{item.get('ip')}" if item.get("name") else str(item.get("ip"))
            for item in isp_links if item.get("ip")
        ),
        "BIGSCREEN_ISP_MAX_BANDWIDTH": isp_bandwidth_config(isp),
        "ISP_SATURATION_PERCENT": str(isp.get("saturation_percent") if isp.get("saturation_percent") not in (None, "") else "90"),
        "ISP_DOWN_FOR_SECONDS": str(isp.get("down_for_seconds") if isp.get("down_for_seconds") not in (None, "") else "10"),
        "COMPOSE_PROFILES": compose_profiles(
            existing.get("COMPOSE_PROFILES", ""),
            bool(unifi.get("enabled")),
            bool(feishu_app_id),
        ),
        "UNIFI_CONTROLLER_URL": unifi.get("controller_url", ""),
        "UNIFI_CONTROLLER_USER": unifi.get("user", ""),
        "UNIFI_CONTROLLER_PASS": unifi.get("password") or existing.get("UNIFI_CONTROLLER_PASS", ""),
        "UNIFI_CONTROLLER_SITES": unifi.get("sites", "all"),
        "UNIFI_CONTROLLER_VERIFY_SSL": str(bool(unifi.get("verify_ssl", False))).lower(),
        "FEISHU_ROBOT_TOKEN": alerts.get("feishu_robot_token") or existing.get("FEISHU_ROBOT_TOKEN", ""),
        "FEISHU_APP_ID": feishu_app_id,
        "FEISHU_APP_SECRET": feishu_app_secret,
        "FEISHU_CHAT_ID": feishu_chat_id,
        "DEVICE_CPU_ALERT_PERCENT": f"{cpu_alert_percent:g}",
        "DEVICE_CPU_RECOVER_PERCENT": f"{max(0, cpu_alert_percent - 10):g}",
        "DEVICE_MEMORY_ALERT_PERCENT": f"{memory_alert_percent:g}",
        "DEVICE_MEMORY_RECOVER_PERCENT": f"{max(0, memory_alert_percent - 10):g}",
        "SYSLOG_ALERT_TYPES": syslog_alert_types,
        "SYSLOG_GATEWAY_MACS": alerts.get("gateway_macs", ""),
        "SYSLOG_GATEWAY_UPLINK_PORTS": alerts.get("gateway_uplink_ports", ""),
        "SYSLOG_MAC_FLAP_WINDOW_SECONDS": str(alerts.get("mac_flap_window_seconds") or 60),
        "SYSLOG_MAC_FLAP_THRESHOLD": str(alerts.get("mac_flap_threshold") or 3),
        "GRAFANA_ANONYMOUS_ENABLED": str(bool(security.get("grafana_anonymous", True))).lower(),
    }
    return {key: "" if value is None else str(value) for key, value in env.items()}


def _strip_env_inline_comment(value: str) -> str:
    quote = ""
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if quote and character == "\\":
            escaped = True
            continue
        if quote:
            if character == quote:
                quote = ""
            continue
        if character in ('"', "'"):
            quote = character
        elif character == "#" and index > 0 and value[index - 1].isspace():
            return value[:index].strip()
    return value.strip()


def read_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = _strip_env_inline_comment(value)
        if value.startswith('"') and value.endswith('"'):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                # Preserve malformed input so validation/callers can surface it;
                # never silently reinterpret a broken quoted value.
                pass
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        env[key.strip()] = str(value)
    return env


def shell_env_exports(path: Path, keys: list[str]) -> str:
    invalid_keys = [key for key in keys if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)]
    if invalid_keys:
        raise ValueError(f"invalid environment key: {invalid_keys[0]}")
    values = read_env(path)
    return "\n".join(
        f"export {key}={shlex.quote(values[key])}" for key in keys if key in values
    )


def env_value(value: str) -> str:
    if value == "":
        return ""
    if re.search(r"\s|#|\"|'", value):
        return json.dumps(value, ensure_ascii=False)
    return value


def merge_env_file(path: Path, updates: dict[str, str]) -> str:
    existing_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen = set()
    lines = []
    for line in existing_lines:
        if "=" not in line or line.lstrip().startswith("#"):
            lines.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in updates:
            lines.append(f"{key}={env_value(updates[key])}")
            seen.add(key)
        else:
            lines.append(line)
    new_keys = [key for key in sorted(updates) if key not in seen]
    if new_keys:
        if lines and lines[-1] != "":
            lines.append("")
        lines.append("# Generated from event-config.yml by platform-api")
        for key in new_keys:
            lines.append(f"{key}={env_value(updates[key])}")
    return "\n".join(lines).rstrip() + "\n"


def default_config_text(example_path: Path) -> str:
    if example_path.exists():
        return example_path.read_text(encoding="utf-8")
    return dump_simple_yaml({"schema_version": CURRENT_SCHEMA_VERSION, "event": {"name": ""}}) + "\n"


def stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def atomic_write_config(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        if mode is not None:
            try:
                os.fchmod(fd, mode)
            except (AttributeError, OSError):
                os.chmod(temporary, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def migrate_file(path: Path, write: bool = False) -> int:
    try:
        config = parse_simple_yaml(path.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ConfigSchemaError("event config must be a mapping")
        status = inspect_config_schema(config)
        if status["config_too_new"]:
            raise ConfigTooNewError(
                f"Refusing to downgrade schema {status['original_version']}; "
                f"software supports schema {status['current_supported']}."
            )
        migrated = migrate_config(config)
        issues = validate_config(migrated)
        bad = [item for item in issues if item.get("level") == "bad"]
        if bad:
            for item in bad:
                print(f"{item.get('path')}: {item.get('message')}", file=sys.stderr)
            raise ConfigSchemaError("configuration has blocking validation errors")
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    original = int(status["original_version"])
    if not write:
        print(f"Current schema: {original}")
        print(f"Supported schema: {CURRENT_SCHEMA_VERSION}")
        print(f"Migration required: {'yes' if status['migration_required'] else 'no'}")
        print("Validation: ok")
        print("No files changed.")
        return 0
    if not status["migration_required"]:
        print(f"Already at schema {CURRENT_SCHEMA_VERSION}")
        return 0

    atomic_write_config(path, dump_simple_yaml(migrated) + "\n")
    print(f"Migrated schema {original} -> {CURRENT_SCHEMA_VERSION}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "env-get":
        values = read_env(Path(sys.argv[2]))
        key = sys.argv[3]
        if key not in values:
            raise SystemExit(1)
        sys.stdout.write(values[key])
    elif len(sys.argv) >= 4 and sys.argv[1] == "env-export-shell":
        try:
            sys.stdout.write(shell_env_exports(Path(sys.argv[2]), sys.argv[3:]))
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    elif len(sys.argv) in (3, 4) and sys.argv[1] == "migrate":
        if len(sys.argv) == 4 and sys.argv[3] != "--write":
            raise SystemExit("usage: platform_config.py migrate EVENT_CONFIG [--write]")
        raise SystemExit(migrate_file(Path(sys.argv[2]), write=len(sys.argv) == 4))
    else:
        raise SystemExit(
            "usage: platform_config.py env-get ENV_FILE KEY\n"
            "       platform_config.py env-export-shell ENV_FILE KEY [KEY ...]\n"
            "       platform_config.py migrate EVENT_CONFIG [--write]"
        )
