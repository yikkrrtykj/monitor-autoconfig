"""Bounded, single-node summary assembled from existing read-only sources."""
from __future__ import annotations

import ipaddress
import math
from typing import Any

from librenms_client import LibreNMSError

from .network_read import (
    NetworkReadContext, NetworkReadError, _prometheus_query, read_topology,
)


PORT_COLUMNS = "ifName,ifDescr,ifAlias,ifAdminStatus,ifOperStatus,ifSpeed"
PORT_LIMIT = 2048
NEIGHBOR_LIMIT = 12
TEXT_LIMIT = 160


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text[:TEXT_LIMIT] or None


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _state(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in ("1", "up", "online", "true"):
        return "up"
    if text in ("2", "down", "offline", "false"):
        return "down"
    return "unknown"


def _sample(item: dict[str, Any]) -> float | None:
    value = item.get("value")
    return _number(value[1]) if isinstance(value, list) and len(value) >= 2 else None


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _ap_label_state(metric: dict[str, Any]) -> str | None:
    for field in ("state", "status", "stat", "connected", "up", "disabled"):
        raw = str(metric.get(field) or "").strip().lower()
        if field == "disabled" and raw in ("1", "true", "yes"):
            return "down"
        if raw == "unknown":
            return "unknown"
        if any(token in raw for token in ("offline", "disconnect", "down", "false")) or raw == "0":
            return "down"
        if any(token in raw for token in ("online", "connected", "active", "adopted", "true")) or raw == "1":
            return "up"
    return None


def _query_one(context: NetworkReadContext, query: str, ip: str) -> list[dict[str, Any]]:
    return [item for item in _prometheus_query(context, query)
            if isinstance(item.get("metric"), dict)
            and str(item["metric"].get("target_ip") or item["metric"].get("ip")
                    or item["metric"].get("instance") or "") == ip][:2]


def _ports(client: Any, device: dict[str, Any]) -> dict[str, Any]:
    rows = client.get_device_ports(device, columns=PORT_COLUMNS)
    if not rows or len(rows) > PORT_LIMIT or not all(isinstance(row, dict) for row in rows):
        raise ValueError("port inventory is unavailable or exceeds the inspector limit")
    up = down = unknown = 0
    for row in rows:
        state = _state(row.get("ifOperStatus"))
        if state == "up":
            up += 1
        elif state == "down":
            down += 1
        else:
            unknown += 1
    return {"up": up, "down": down, "unknown": unknown, "total": len(rows)}


def _neighbors(context: NetworkReadContext, ip: str) -> tuple[list[dict[str, Any]], bool, bool]:
    topology = read_topology(context)
    edges = topology["edges"]
    selected = []
    omitted_stale = False
    for edge in edges:
        if edge.get("from_ip") == ip:
            local, peer = "from", "to"
        elif edge.get("to_ip") == ip:
            local, peer = "to", "from"
        else:
            continue
        if edge.get("stale") is True:
            omitted_stale = True
            continue
        if len(selected) >= NEIGHBOR_LIMIT:
            continue
        members = edge.get(f"{local}_member_ports")
        protocols = edge.get("protocols")
        selected.append({
            "peerIp": _text(edge.get(f"{peer}_ip")),
            "localPort": _text(edge.get(f"{local}_port")),
            "peerPort": _text(edge.get(f"{peer}_port")),
            "aggregatePort": _text(edge.get(f"{local}_aggregate_port")),
            "members": [_text(name) for name in members[:8]] if isinstance(members, list) else [],
            "protocols": [name for name in (protocols[:3] if isinstance(protocols, list) else []) if isinstance(name, str)
                          if name in ("lldp", "cdp")],
        })
    return selected, bool(topology["stale"]), omitted_stale


def _ap(context: NetworkReadContext, ip: str, warnings: list[str]) -> dict[str, Any] | None:
    try:
        infos = _query_one(context, f'unpoller_device_info{{type="uap",ip="{ip}"}}', ip)
    except NetworkReadError:
        warnings.append("UniFi 数据暂不可用")
        return None
    if not infos:
        return None
    metric = infos[0]["metric"]
    name = _text(metric.get("name"))
    if not name:
        warnings.append("AP 名称缺失")
    clients = None
    online = "unknown"
    uptime_failed = False
    name_filter = f'name="{_label(name)}"' if name else 'name=""'
    # Match the wireless panel's uptime-first and info-label fallback order.
    for metric_name in ("unpoller_device_uptime_seconds", "unpoller_device_uptime"):
        try:
            uptime = _prometheus_query(context, f'{metric_name}{{type="uap",{name_filter}}}')
        except NetworkReadError:
            warnings.append("AP 在线指标暂不可用")
            uptime_failed = True
            continue
        matching = [row for row in uptime if isinstance(row.get("metric"), dict)
                    and row["metric"].get("name") == name]
        if matching and _sample(matching[0]) is not None:
            online = "up" if _sample(matching[0]) > 0 else "down"
            break
    if online == "unknown" and not uptime_failed:
        online = _ap_label_state(metric) or "up"
        warnings.append("AP 在线指标缺失，使用现有无线面板回退语义")
    try:
        stations = _prometheus_query(
            context, f'sum by (name) (unpoller_device_stations{{type="uap",{name_filter}}})'
        )
        matching = [row for row in stations if isinstance(row.get("metric"), dict)
                    and row["metric"].get("name") == name]
        if matching:
            clients = _sample(matching[0])
    except NetworkReadError:
        warnings.append("AP 客户端数据暂不可用")
    radio = None
    try:
        radios = _prometheus_query(context,
            f'unpoller_device_radio_channel_utilization_total_ratio{{type="uap",{name_filter}}}')
        values = [_sample(row) for row in radios if isinstance(row.get("metric"), dict)
                  and row["metric"].get("name") == name]
        valid = [value for value in values if value is not None and 0 <= value <= 1]
        if valid:
            radio = f"频道利用率最高 {round(max(valid) * 100)}%"
    except NetworkReadError:
        warnings.append("AP 射频数据暂不可用")
    return {"kind": "unifi-ap", "name": name, "model": _text(metric.get("model")),
            "online": online, "clients": int(clients) if clients is not None and clients >= 0 else None,
            "uplink": _text(metric.get("uplink")), "radio": radio}


def read_inspector(context: NetworkReadContext, management_ip: str) -> dict[str, Any]:
    try:
        ip = str(ipaddress.IPv4Address(management_ip))
    except ipaddress.AddressValueError as exc:
        raise NetworkReadError(400, "invalid_node", "A valid management IPv4 address is required") from exc
    warnings: list[str] = []
    device = None
    client = context.librenms_client_factory()
    try:
        candidate = client.get_device(ip)
        if candidate and str(candidate.get("ip") or candidate.get("hostname") or "") != ip:
            raise ValueError("device identity does not match requested address")
        device = candidate
    except (LibreNMSError, ValueError):
        warnings.append("设备资料暂不可用")

    os_name = str((device or {}).get("os") or "").lower()
    is_ap = any(marker in os_name for marker in ("ubnt", "unifi", "ubiquiti"))
    ap = _ap(context, ip, warnings) if device is None or is_ap else None
    if device is None and ap is None:
        known = False
        try:
            known = ip in read_topology(context)["nodes"]
        except NetworkReadError:
            warnings.append("拓扑资料暂不可用")
        if not known:
            try:
                known = bool(_query_one(context,
                    f'probe_success{{job=~"infra-core-ping|infra-dist-ping|infra-fw-ping|infra-fw-unit-ping",target_ip="{ip}"}}', ip))
            except NetworkReadError:
                warnings.append("当前探测状态暂不可用")
        if not known:
            if warnings:
                raise NetworkReadError(503, "inspector_unavailable", "Node identity is unavailable")
            raise NetworkReadError(404, "node_not_found", "Node was not found")
        warnings.append("设备身份和端口汇总暂不可用")

    kind = "unifi-ap" if ap or is_ap else ("hillstone" if "hillstone" in os_name else "cisco" if device else "unknown")
    result: dict[str, Any] = {
        "ok": True, "ip": ip, "kind": kind,
        "name": (ap or {}).get("name") or _text((device or {}).get("sysName") or (device or {}).get("hostname")),
        "hostname": _text((device or {}).get("hostname")) if device else (ap or {}).get("name"),
        "model": (ap or {}).get("model") or _text((device or {}).get("hardware") or (device or {}).get("model")),
        "online": "unknown", "latencySeconds": None,
        "ports": None, "neighbors": [], "haRole": "unavailable" if kind == "hillstone" else None,
        "clients": ap["clients"] if ap else None,
        "uplink": ap["uplink"] if ap else None, "radio": ap["radio"] if ap else None,
    }
    if ap:
        result["online"] = ap["online"]
    elif is_ap:
        warnings.append("AP 指标暂不可用")
    else:
        try:
            rows = _query_one(context,
                f'probe_success{{job=~"infra-core-ping|infra-dist-ping|infra-fw-ping|infra-fw-unit-ping",target_ip="{ip}"}}', ip)
            if rows and _sample(rows[0]) is not None:
                result["online"] = "up" if _sample(rows[0]) >= 1 else "down"
            else:
                warnings.append("当前探测状态未知")
        except NetworkReadError:
            warnings.append("当前探测状态暂不可用")
        try:
            rows = _query_one(context,
                f'probe_icmp_duration_seconds{{job=~"infra-core-ping|infra-dist-ping|infra-fw-ping|infra-fw-unit-ping",phase="rtt",target_ip="{ip}"}}', ip)
            if rows and result["online"] == "up":
                result["latencySeconds"] = _sample(rows[0])
            if result["online"] == "up" and result["latencySeconds"] is None:
                warnings.append("延迟数据未知")
        except NetworkReadError:
            warnings.append("延迟数据暂不可用")
        if device and device.get("device_id") is not None:
            try:
                result["ports"] = _ports(client, device)
            except (LibreNMSError, ValueError):
                warnings.append("端口汇总暂不可用")
        else:
            warnings.append("端口汇总缺少设备标识")
        try:
            result["neighbors"], stale, omitted_stale = _neighbors(context, ip)
            if stale:
                warnings.append("拓扑快照已过期")
            if omitted_stale:
                warnings.append("已省略过期邻接")
        except NetworkReadError:
            warnings.append("邻接资料暂不可用")
    result["warnings"] = warnings[:8]
    result["degraded"] = bool(warnings)
    return result
