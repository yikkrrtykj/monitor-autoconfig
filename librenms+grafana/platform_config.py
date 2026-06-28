"""Structured event configuration helpers for the monitoring platform.

The deployment still consumes .env, but operators edit event-config.yml.  This
module parses a small YAML subset (dict/list/scalar), validates the platform
objects, renders .env keys, and keeps comments/secrets outside the browser UI.
It intentionally uses only the Python standard library so the offline package
does not need PyYAML.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any


DEFAULT_ALERT_PROFILE = {
    "UNIFI_AP_DOWN_FOR_SECONDS": "180",
    "SYSLOG_EVENT_RATE_LIMIT": "60",
    "DEVICE_DOWN_FOR_SECONDS": "10",
    "INTERCONNECT_ALERT_FOR_SECONDS": "5",
}

MODE_PROFILES = {
    "monitor": DEFAULT_ALERT_PROFILE,
    "setup": DEFAULT_ALERT_PROFILE,
    "rehearsal": DEFAULT_ALERT_PROFILE,
    "match": DEFAULT_ALERT_PROFILE,
    "incident": DEFAULT_ALERT_PROFILE,
}


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
        return value[1:-1]
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


def normalize_config(config: dict[str, Any]) -> dict[str, Any]:
    config = dict(config or {})
    config.setdefault("event", {})
    config.setdefault("networks", {})
    config.setdefault("devices", {})
    config.setdefault("isp", {})
    config.setdefault("unifi", {})
    config.setdefault("alerts", {})
    config.setdefault("security", {})
    config.setdefault("snmp", {})
    devices = config["devices"]
    devices.setdefault("switches", [])
    if "stage_switches" not in devices:
        devices["stage_switches"] = devices.get("switches") or []
    devices.setdefault("access_switches", [])
    devices.setdefault("servers", [])
    return config


def validate_config(config: dict[str, Any]) -> list[dict[str, str]]:
    config = normalize_config(config)
    issues: list[dict[str, str]] = []
    event = config["event"]
    devices = config["devices"]
    networks = config["networks"]
    isp = config["isp"]

    if not (devices.get("core") or {}).get("ip"):
        issues.append({"level": "bad", "path": "devices.core.ip", "message": "核心交换机 IP 必填"})
    stage_switches = devices.get("stage_switches") or devices.get("switches") or []
    access_switches = devices.get("access_switches") or []
    if not stage_switches:
        issues.append({"level": "warn", "path": "devices.stage_switches", "message": "没有配置舞台交换机，选手自动识别会跳过"})
    for idx, item in enumerate(stage_switches):
        if not item.get("ip"):
            issues.append({"level": "bad", "path": f"devices.stage_switches[{idx}].ip", "message": "舞台交换机 IP 必填"})
    for idx, item in enumerate(access_switches):
        if not item.get("ip"):
            issues.append({"level": "bad", "path": f"devices.access_switches[{idx}].ip", "message": "接入交换机 IP 必填"})
    if not networks.get("player_subnets"):
        issues.append({"level": "warn", "path": "networks.player_subnets", "message": "没有配置选手有线网段"})
    if not networks.get("switch_management_ranges"):
        issues.append({"level": "warn", "path": "networks.switch_management_ranges", "message": "建议填写交换机管理网段，方便 LibreNMS 自动发现"})
    if not isp.get("auto_discovery") and not isp.get("links"):
        issues.append({"level": "warn", "path": "isp.links", "message": "关闭自动发现时建议配置 ISP 探测目标"})
    if config["security"].get("public_enabled") and not event.get("public_base_url"):
        issues.append({"level": "bad", "path": "event.public_base_url", "message": "公网模式必须填写 public_base_url"})
    return issues


def alert_profile(config: dict[str, Any]) -> dict[str, str]:
    event = config.get("event") or {}
    alerts = config.get("alerts") or {}
    mode = str(alerts.get("mode") or event.get("mode") or "monitor").lower()
    return MODE_PROFILES.get(mode, DEFAULT_ALERT_PROFILE)


def render_env(config: dict[str, Any], existing: dict[str, str] | None = None) -> dict[str, str]:
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

    core = devices.get("core") or {}
    firewall = devices.get("firewall") or {}
    stage_switches = devices.get("stage_switches") or devices.get("switches") or []
    access_switches = devices.get("access_switches") or []
    all_switches = [*stage_switches, *access_switches]
    servers = devices.get("servers") or []
    isp_links = isp.get("links") or []
    snmp_community = snmp.get("community") or existing.get("SNMP_COMMUNITY", "global")
    firewall_ping = named_targets([firewall], "ip")
    firewall_snmp = named_targets([firewall], "snmp")

    env = {
        "EVENT_NAME": event.get("name", ""),
        "BIGSCREEN_EVENT_MODE": "monitor",
        "BIGSCREEN_DEFAULT_LAYOUT": event.get("default_layout", "tournament-64-2layer"),
        "BIGSCREEN_SECURITY_MODE": event.get("security_mode", "internal"),
        "BIGSCREEN_PUBLIC_BASE_URL": event.get("public_base_url", ""),
        "SNMP_COMMUNITY": snmp_community,
        "FIREWALL_SNMP_COMMUNITY": snmp.get("firewall_community") or snmp_community,
        "CORE_SWITCH_PING": named_targets([core]) if core.get("ip") else "",
        "DIST_SWITCH_PING": named_targets(all_switches),
        "TOURNAMENT_SWITCHES": named_targets(stage_switches),
        "FIREWALL_PING": firewall_ping,
        "FIREWALL_SNMP_TARGETS": firewall_snmp or firewall_ping,
        "FIREWALL_UNIT_SNMP_TARGETS": named_targets([firewall], "unit_snmp"),
        "SERVER_PING": named_targets(servers),
        "PLAYER_SUBNETS": csv(networks.get("player_subnets")),
        "WIRELESS_SUBNETS": csv(networks.get("wireless_subnets")),
        "PLAYER_GATEWAYS": csv(networks.get("player_gateways") or core.get("ip")),
        "PLAYER_VLAN_IDS": csv(networks.get("player_vlan")),
        "LIBRENMS_DISCOVERY_TARGETS": csv(networks.get("switch_management_ranges")),
        "FIREWALL_DISCOVERY_RANGE": csv(networks.get("firewall_management_ranges")),
        "BIGSCREEN_ISP_AUTO_DISCOVER": str(bool(isp.get("auto_discovery", True))).lower(),
        "FIREWALL_WAN_IF_FILTER": isp.get("wan_if_filter") or "telecom,telcom,unicom,isp,WAN",
        "BIGSCREEN_ISP_NAMES": ",".join(str(item.get("name")) for item in isp_links if item.get("name")),
        "ISP_PING": ",".join(
            f"{item.get('name')}:{item.get('ping')}" if item.get("name") else str(item.get("ping"))
            for item in isp_links if item.get("ping")
        ),
        "BIGSCREEN_ISP_IPS": "",
        "BIGSCREEN_ISP_MAX_BANDWIDTH": str(isp.get("max_bandwidth_mbps") or "1000"),
        "ISP_SATURATION_PERCENT": str(isp.get("saturation_percent") or "90"),
        "ISP_DOWN_FOR_SECONDS": str(isp.get("down_for_seconds") or "10"),
        "COMPOSE_PROFILES": "unifi" if unifi.get("enabled") else existing.get("COMPOSE_PROFILES", ""),
        "UNIFI_CONTROLLER_URL": unifi.get("controller_url", ""),
        "UNIFI_CONTROLLER_USER": unifi.get("user", ""),
        "UNIFI_CONTROLLER_PASS": unifi.get("password") or existing.get("UNIFI_CONTROLLER_PASS", ""),
        "UNIFI_CONTROLLER_SITES": unifi.get("sites", "all"),
        "UNIFI_CONTROLLER_VERIFY_SSL": str(bool(unifi.get("verify_ssl", False))).lower(),
        "FEISHU_ROBOT_TOKEN": alerts.get("feishu_robot_token") or existing.get("FEISHU_ROBOT_TOKEN", ""),
        "SYSLOG_ALERT_TYPES": alerts.get("syslog_alert_types", "native_vlan_mismatch,errdisable,loopback,dhcp_snooping"),
        "GRAFANA_ANONYMOUS_ENABLED": str(bool(security.get("grafana_anonymous", True))).lower(),
    }
    env.update(alert_profile(config))
    return {key: "" if value is None else str(value) for key, value in env.items()}


def read_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        env[key.strip()] = value
    return env


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
    if updates:
        if lines and lines[-1] != "":
            lines.append("")
        lines.append("# Generated from event-config.yml by platform-api")
        for key in sorted(updates):
            if key not in seen:
                lines.append(f"{key}={env_value(updates[key])}")
    return "\n".join(lines).rstrip() + "\n"


def default_config_text(example_path: Path) -> str:
    if example_path.exists():
        return example_path.read_text(encoding="utf-8")
    return dump_simple_yaml({"event": {"name": ""}}) + "\n"


def stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")
