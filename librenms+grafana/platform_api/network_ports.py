"""Bounded, read-only node interface inventory from existing collected sources."""
from __future__ import annotations

import ipaddress
import json
import math
import re
import time
from dataclasses import replace
from typing import Any

from librenms_client import LibreNMSError

from .network_read import (NetworkReadContext, NetworkReadError, _isp_pair_consistent,
                           _prometheus_query, _read_isp_pair, read_topology)


PORT_LIMIT = 2048
RESPONSE_BYTE_LIMIT = 4 * 1024 * 1024
TEXT_LIMIT = 160
VLAN_LIMIT = 128
NEIGHBOR_LIMIT = 8
METRIC_STALE_SECONDS = 120
# Existing LibreNMS 300-second poll cadence; allow two intervals, never unbounded.
POLLER_STALE_SECONDS = 600
CURRENT_FIELDS = (
    "rxBps", "txBps", "inputErrorsTotal", "outputErrorsTotal",
    "inputDiscardsTotal", "outputDiscardsTotal",
)
# device-ports validates against ports, not ports_statistics; discards are
# Prometheus-only here. Do not request a second statistics read.
POLLER_FIELDS = {
    "rxBps": "ifInOctets_rate", "txBps": "ifOutOctets_rate",
    "inputErrorsTotal": "ifInErrors", "outputErrorsTotal": "ifOutErrors",
}
PORT_COLUMNS = ("device_id,ifIndex,ifName,ifDescr,ifAlias,ifAdminStatus,ifOperStatus,ifSpeed,"
                "poll_time,poll_period," + ",".join(POLLER_FIELDS.values()))


def _fresh_metric(metric: str, *, rate: bool = False) -> str:
    series = f'{metric}{{job="infra-switch-ifmib",target_ip="{{ip}}"}}'
    value = f'rate({series}[5m]) * 8' if rate else series
    # Instant-query result timestamps are evaluation times, not scrape times.
    return (f'({value}) and on(job,target_ip,ifIndex) '
            f'(timestamp({series}) >= time() - {METRIC_STALE_SECONDS})')


def _fresh_firewall_metric(metric: str, *, rate: bool = False) -> str:
    series = f'{metric}{{job="firewall-snmp",instance="{{ip}}"}}'
    value = f'rate({series}[5m]) * 8' if rate else series
    return (f'({value}) and on(job,instance,ifIndex) '
            f'(timestamp({series}) >= time() - {METRIC_STALE_SECONDS})')


METRICS = {
    "rxBps": _fresh_metric("ifHCInOctets", rate=True),
    "txBps": _fresh_metric("ifHCOutOctets", rate=True),
    "highSpeedMbps": _fresh_metric("ifHighSpeed"),
    "inputErrorsTotal": _fresh_metric("ifInErrors"),
    "outputErrorsTotal": _fresh_metric("ifOutErrors"),
    "inputDiscardsTotal": _fresh_metric("ifInDiscards"),
    "outputDiscardsTotal": _fresh_metric("ifOutDiscards"),
}
FIREWALL_METRICS = {
    "rxBps": _fresh_firewall_metric("ifHCInOctets", rate=True),
    "txBps": _fresh_firewall_metric("ifHCOutOctets", rate=True),
    "highSpeedMbps": _fresh_firewall_metric("ifHighSpeed"),
    "inputErrorsTotal": _fresh_firewall_metric("ifInErrors"),
    "outputErrorsTotal": _fresh_firewall_metric("ifOutErrors"),
    "inputDiscardsTotal": _fresh_firewall_metric("ifInDiscards"),
    "outputDiscardsTotal": _fresh_firewall_metric("ifOutDiscards"),
}
STACK_PORT = re.compile(r"^(?:Gi|Te|Fa|Hu|Fo|Twe|Eth)([1-9]\d?)/\d{1,2}/([1-9]\d{0,2})$")
CISCO_OS = frozenset(("ios", "iosxe", "iosxr", "nxos"))
SMALL_BUSINESS_OS = frozenset(("ciscosb", "cisco-access"))
HILLSTONE_OS = frozenset(("hillstone", "stoneos"))


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value).strip()[:TEXT_LIMIT] or None


def _index(value: Any) -> int | None:
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return None
    return number if 0 < number <= 2_147_483_647 else None


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _speed(value: Any) -> int | None:
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return None
    return number if 0 < number <= 1_000_000_000_000 and number != 4_294_967_295 else None


def _state(value: Any) -> str:
    state = str(value or "").strip().lower()
    return "up" if state in ("1", "up") else "down" if state in ("2", "down") else "unknown"


def _stack_parts(name: str | None) -> tuple[int | None, int | None]:
    match = STACK_PORT.fullmatch(name or "")
    if not match:
        return None, None
    member, port = int(match.group(1)), int(match.group(2))
    return (member, port) if member <= 16 and port <= 256 else (None, None)


def _vlans(value: Any, warnings: list[str]) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > VLAN_LIMIT:
        warnings.append("VLAN 观测资料超出范围")
        return None
    observed = []
    for item in value:
        if not isinstance(item, dict):
            continue
        vlan_id = _index(item.get("vlan"))
        if vlan_id is None or vlan_id > 4094:
            continue
        raw = str(item.get("untagged") if item.get("untagged") is not None else "").strip().lower()
        untagged = True if raw in ("1", "true") else False if raw in ("0", "false") else None
        observed.append({"vlanId": vlan_id, "untagged": untagged})
    return {"authority": "observed", "memberships": observed}


def _current_metrics(context: NetworkReadContext, ip: str, warnings: list[str],
                     kind: str) -> dict[str, dict[int, float]]:
    output: dict[str, dict[int, float]] = {}
    failed = False
    covered = False
    deadline = time.monotonic() + context.http_timeout
    templates = FIREWALL_METRICS if kind == "hillstone" else METRICS
    identity_label = "instance" if kind == "hillstone" else "target_ip"
    for field, template in templates.items():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            failed = True
            break
        try:
            rows = _prometheus_query(replace(context, http_timeout=remaining), template.replace("{ip}", ip))
        except NetworkReadError:
            failed = True
            continue
        if time.monotonic() >= deadline:
            failed = True
            break
        values: dict[int, float] = {}
        duplicate: set[int] = set()
        for row in rows:
            metric = row.get("metric")
            sample = row.get("value")
            if not isinstance(metric, dict) or metric.get(identity_label) != ip:
                continue
            if kind == "hillstone" and metric.get("job") != "firewall-snmp":
                continue
            index = _index(metric.get("ifIndex"))
            numeric = _number(sample[1]) if isinstance(sample, list) and len(sample) >= 2 else None
            if index is None or numeric is None:
                continue
            if index in values:
                duplicate.add(index)
            else:
                values[index] = numeric
        for index in duplicate:
            values.pop(index, None)
        covered |= bool(values)
        output[field] = values
    if failed:
        warnings.append("当前防火墙 IF-MIB 指标部分不可用" if kind == "hillstone" else "当前 IF-MIB 指标部分不可用")
    if not covered:
        warnings.append("当前防火墙 IF-MIB 无有效覆盖" if kind == "hillstone" else "当前 IF-MIB 无有效覆盖")
    return output


def _wan_evidence(context: NetworkReadContext, ip: str, warnings: list[str]) -> dict[int, dict[str, Any]]:
    try:
        inventory, state, raw = _read_isp_pair(context)
        if not _isp_pair_consistent(inventory, state, raw):
            inventory, state, raw = _read_isp_pair(context)
    except NetworkReadError:
        warnings.append("WAN / ISP 资料暂不可用")
        return {}
    state_status = str(state.get("status") or "").lower()
    if not _isp_pair_consistent(inventory, state, raw) or state_status not in ("ok", "disabled") or (state_status == "disabled" and inventory):
        warnings.append("WAN / ISP 资料已过期或不一致")
        return {}
    matches: dict[int, list[dict[str, Any]]] = {}
    for item in inventory:
        labels = item.get("labels")
        targets = item.get("targets")
        if not isinstance(labels, dict) or not isinstance(targets, list):
            warnings.append("WAN / ISP 资料格式无效")
            return {}
        if labels.get("metric_target") != ip:
            continue
        index = _index(labels.get("metric_ifindex"))
        if index is None:
            continue
        name = _text(labels.get("display_name") or labels.get("metric_name"))
        if not name:
            continue
        gateway = _text(targets[0]) if targets and isinstance(targets[0], str) else None
        matches.setdefault(index, []).append({
            "authority": "isp-inventory", "name": name,
            "wanIp": _text(labels.get("wan_ip")), "gateway": gateway,
        })
    if any(len(entries) > 1 for entries in matches.values()):
        warnings.append("WAN / ISP 映射存在冲突，已省略")
    return {index: entries[0] for index, entries in matches.items() if len(entries) == 1}


def _neighbors(context: NetworkReadContext, ip: str, port_names: set[str],
               warnings: list[str]) -> dict[str, list[dict[str, Any]]] | None:
    try:
        snapshot = read_topology(context)
    except NetworkReadError:
        warnings.append("邻接资料暂不可用")
        return None
    if snapshot["stale"]:
        warnings.append("拓扑快照已过期")
        return None
    result: dict[str, list[dict[str, Any]]] = {}
    omitted_stale = False
    truncated = False
    for edge in snapshot["edges"]:
        if edge.get("from_ip") == ip:
            local, peer = "from", "to"
        elif edge.get("to_ip") == ip:
            local, peer = "to", "from"
        else:
            continue
        if edge.get("stale") is True:
            omitted_stale = True
            continue
        local_port = _text(edge.get(f"{local}_port"))
        aggregate = _text(edge.get(f"{local}_aggregate_port"))
        members = edge.get(f"{local}_member_ports")
        names = {name for name in (local_port, aggregate) if name}
        if isinstance(members, list):
            names.update(_text(name) for name in members[:PORT_LIMIT] if _text(name))
        names.intersection_update(port_names)
        if not names:
            continue
        protocols = edge.get("protocols")
        evidence = {
            "peerIp": _text(edge.get(f"{peer}_ip")),
            "peerName": _text(edge.get(f"{peer}_sysname")),
            "peerPort": _text(edge.get(f"{peer}_port")),
            "protocols": [name for name in protocols[:3] if name in ("lldp", "cdp")]
            if isinstance(protocols, list) else [],
            "aggregatePort": aggregate,
            "memberPorts": [_text(name) for name in members[:16] if _text(name)]
            if isinstance(members, list) else [],
        }
        for name in names:
            bucket = result.setdefault(name, [])
            if len(bucket) >= NEIGHBOR_LIMIT:
                truncated = True
            else:
                bucket.append(evidence)
    if omitted_stale:
        warnings.append("已省略过期邻接")
    if truncated:
        warnings.append("部分邻接超出展示上限")
    return result


def read_ports(context: NetworkReadContext, management_ip: str) -> dict[str, Any]:
    try:
        ip = str(ipaddress.IPv4Address(management_ip))
    except ipaddress.AddressValueError as exc:
        raise NetworkReadError(400, "invalid_node", "A valid management IPv4 address is required") from exc
    client = context.librenms_client_factory()
    try:
        candidate = client.get_device(ip)
    except LibreNMSError as exc:
        raise NetworkReadError(503, "librenms_unavailable", "Device identity is unavailable") from exc
    if candidate is None:
        raise NetworkReadError(404, "node_not_found", "Node was not found")
    if str(candidate.get("ip") or candidate.get("hostname") or "") != ip:
        raise NetworkReadError(503, "identity_mismatch", "Device identity does not match requested address")
    os_name = str(candidate.get("os") or "").lower()
    if os_name in CISCO_OS:
        kind = "cisco"
    elif os_name in SMALL_BUSINESS_OS:
        kind = "generic-switch"
    elif os_name in HILLSTONE_OS:
        kind = "hillstone"
    else:
        raise NetworkReadError(404, "unsupported_os", "Interface inventory is unavailable for this node")
    try:
        rows = client.get_device_ports(candidate, columns=PORT_COLUMNS, with_vlans=True)
    except LibreNMSError as exc:
        raise NetworkReadError(503, "ports_unavailable", "Port inventory is unavailable") from exc
    if not isinstance(rows, list) or not rows or len(rows) > PORT_LIMIT or not all(isinstance(row, dict) for row in rows):
        raise NetworkReadError(503, "ports_invalid", "Port inventory is empty, malformed, or exceeds the limit")
    warnings: list[str] = []
    metrics = _current_metrics(context, ip, warnings, kind)
    wan_evidence = _wan_evidence(context, ip, warnings) if kind == "hillstone" else {}
    port_names = {_text(row.get("ifName")) for row in rows}
    neighbors = _neighbors(context, ip, {name for name in port_names if name}, warnings)
    ports = []
    for row in rows:
        index = _index(row.get("ifIndex"))
        name = _text(row.get("ifName"))
        stack_member, port_number = _stack_parts(name) if kind == "cisco" else (None, None)
        speed = _speed(row.get("ifSpeed"))
        high_speed = metrics.get("highSpeedMbps", {}).get(index) if index is not None else None
        if speed is None and high_speed is not None and 0 < high_speed <= 1_000_000:
            speed = _speed(int(high_speed * 1_000_000))
        values = {field: metrics.get(field, {}).get(index) if index is not None else None
                  for field in CURRENT_FIELDS}
        source = "Prometheus" if any(value is not None for value in values.values()) else None
        age = None
        # Do not mix sources within a row or borrow another device/HA member.
        if source is None and kind in ("hillstone", "generic-switch"):
            now = context.clock()
            raw_poll = row.get("poll_time")
            poll = _number(raw_poll) if not isinstance(raw_poll, bool) else None
            age = now - poll if poll is not None and poll > 0 else None
            if (age is not None and 0 <= age <= POLLER_STALE_SECONDS
                    and (row.get("device_id") is None or
                         str(row["device_id"]) == str(candidate.get("device_id")))):
                values = dict.fromkeys(CURRENT_FIELDS)
                values.update({field: _number(row.get(column)) if not isinstance(row.get(column), bool) else None
                               for field, column in POLLER_FIELDS.items()})
                for field in ("rxBps", "txBps"):
                    if values[field] is not None:
                        values[field] *= 8
                        if not math.isfinite(values[field]):
                            values[field] = None
                if any(value is not None for value in values.values()):
                    source = "LibreNMS poller"
            else:
                warnings.append("LibreNMS poller 时间缺失、无效或已过期，当前指标不可用")
                age = age if age is not None and age >= 0 else None
        rx, tx = values["rxBps"], values["txBps"]
        current = values.get
        port = {
            "ifIndex": index, "ifName": name,
            "ifDescr": _text(row.get("ifDescr")), "ifAlias": _text(row.get("ifAlias")),
            "stackMember": stack_member,
            "portNumber": port_number,
            "adminState": _state(row.get("ifAdminStatus")),
            "operState": _state(row.get("ifOperStatus")),
            "speedBps": speed,
            "rxBps": rx, "txBps": tx,
            "metricSource": source, "metricAgeSeconds": age,
            "rxUtilization": rx / speed if rx is not None and speed else None,
            "txUtilization": tx / speed if tx is not None and speed else None,
            "inputErrorsTotal": current("inputErrorsTotal"),
            "outputErrorsTotal": current("outputErrorsTotal"),
            "inputDiscardsTotal": current("inputDiscardsTotal"),
            "outputDiscardsTotal": current("outputDiscardsTotal"),
            "vlanEvidence": _vlans(row.get("vlans"), warnings),
            "neighbors": neighbors.get(name or "", []) if neighbors is not None else None,
        }
        if kind == "hillstone":
            port["wanEvidence"] = wan_evidence.get(index) if index is not None else None
        ports.append(port)
    unique_warnings = list(dict.fromkeys(warnings))
    result = {"ok": True, "ip": ip, "kind": kind,
              "name": _text(candidate.get("sysName") or candidate.get("hostname")),
              "model": _text(candidate.get("hardware") or candidate.get("model")),
              "count": len(ports), "ports": ports, "warnings": unique_warnings,
              "degraded": bool(unique_warnings)}
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > RESPONSE_BYTE_LIMIT:
        raise NetworkReadError(503, "ports_oversize", "Port response exceeds the byte limit")
    return result
