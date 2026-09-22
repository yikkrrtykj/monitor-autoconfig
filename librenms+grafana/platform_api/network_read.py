"""Bounded, read-only aggregation for the platform network endpoints."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any, Callable
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from librenms_client import LibreNMSError
from target_utils import parse_named_ipv4_target_rows


DEVICE_LIMIT = 5000
TOPOLOGY_EDGE_LIMIT = 20000
ISP_LIMIT = 1024
HTTP_BYTE_LIMIT = 4 * 1024 * 1024
TOPOLOGY_BYTE_LIMIT = 16 * 1024 * 1024
ISP_BYTE_LIMIT = 2 * 1024 * 1024
ENV_BYTE_LIMIT = 1024 * 1024


class NetworkReadError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.payload = {"ok": False, "code": code, "error": message}


@dataclass(frozen=True)
class NetworkReadContext:
    librenms_client_factory: Callable[[], Any]
    prometheus_url: str
    topology_path: Path
    isp_inventory_path: Path
    isp_state_path: Path
    env_path: Path
    urlopen: Callable[..., Any] = urlrequest.urlopen
    clock: Callable[[], float] = time.time
    http_timeout: float = 5.0
    topology_stale_seconds: int = 1800
    http_byte_limit: int = HTTP_BYTE_LIMIT
    topology_byte_limit: int = TOPOLOGY_BYTE_LIMIT
    isp_byte_limit: int = ISP_BYTE_LIMIT
    env_byte_limit: int = ENV_BYTE_LIMIT
    device_limit: int = DEVICE_LIMIT
    topology_edge_limit: int = TOPOLOGY_EDGE_LIMIT
    isp_limit: int = ISP_LIMIT


def _iso_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _read_bytes(path: Path, limit: int, label: str) -> tuple[bytes, float]:
    try:
        with path.open("rb") as handle:
            raw = handle.read(limit + 1)
            modified = os.fstat(handle.fileno()).st_mtime
    except OSError as exc:
        raise NetworkReadError(503, f"{label}_unavailable", f"{label} data is unavailable") from exc
    if len(raw) > limit:
        raise NetworkReadError(503, f"{label}_oversize", f"{label} data exceeds the byte limit")
    return raw, float(modified)


def _decode_json(raw: bytes, label: str) -> Any:
    try:
        return json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NetworkReadError(503, f"{label}_malformed", f"{label} data is malformed") from exc


def _prometheus_query(context: NetworkReadContext, query: str) -> list[dict[str, Any]]:
    base = str(context.prometheus_url or "").strip().rstrip("/")
    if not base:
        raise NetworkReadError(503, "prometheus_unavailable", "Prometheus is unavailable")
    url = f"{base}/api/v1/query?{urlparse.urlencode({'query': query})}"
    request = urlrequest.Request(url, headers={"Accept": "application/json"})
    try:
        with context.urlopen(request, timeout=context.http_timeout) as response:
            raw = response.read(context.http_byte_limit + 1)
    except (OSError, TimeoutError, urlerror.URLError) as exc:
        raise NetworkReadError(503, "prometheus_unavailable", "Prometheus is unavailable") from exc
    if len(raw) > context.http_byte_limit:
        raise NetworkReadError(503, "prometheus_oversize", "Prometheus response exceeds the byte limit")
    payload = _decode_json(raw, "prometheus")
    result = payload.get("data", {}).get("result") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or payload.get("status") != "success" or not isinstance(result, list):
        raise NetworkReadError(503, "prometheus_malformed", "Prometheus returned malformed data")
    if len(result) > context.device_limit + context.isp_limit:
        raise NetworkReadError(503, "prometheus_oversize", "Prometheus returned too many records")
    if not all(isinstance(item, dict) for item in result):
        raise NetworkReadError(503, "prometheus_malformed", "Prometheus returned malformed data")
    return result


def _probe_statuses(context: NetworkReadContext) -> dict[str, str]:
    result = _prometheus_query(
        context,
        'probe_success{job=~"infra-isp-ping|infra-core-ping|infra-dist-ping|infra-fw-ping|infra-srv-ping"}',
    )
    statuses: dict[str, str] = {}
    for item in result:
        metric = item.get("metric")
        value = item.get("value")
        if not isinstance(metric, dict) or not isinstance(value, list) or len(value) < 2:
            raise NetworkReadError(503, "prometheus_malformed", "Prometheus returned malformed data")
        target = str(metric.get("target_ip") or metric.get("instance") or "").strip()
        if not target:
            continue
        try:
            numeric = float(value[1])
        except (TypeError, ValueError):
            raise NetworkReadError(503, "prometheus_malformed", "Prometheus returned malformed data") from None
        statuses[target] = "up" if numeric >= 1 else "down"
    return statuses


def read_devices(context: NetworkReadContext) -> dict[str, Any]:
    try:
        devices = context.librenms_client_factory().list_devices(strict=True)
    except LibreNMSError as exc:
        raise NetworkReadError(503, "librenms_unavailable", "LibreNMS device inventory is unavailable") from exc
    if len(devices) > context.device_limit:
        raise NetworkReadError(503, "librenms_oversize", "LibreNMS returned too many devices")
    if not all(isinstance(item, dict) for item in devices):
        raise NetworkReadError(503, "librenms_malformed", "LibreNMS device inventory is malformed")
    if any(
        not any(str(device.get(field) or "").strip() for field in (
            "device_id", "hostname", "ip", "sysName",
        ))
        for device in devices
    ):
        raise NetworkReadError(503, "librenms_malformed", "LibreNMS device inventory is malformed")

    degraded = False
    warnings: list[str] = []
    try:
        statuses = _probe_statuses(context)
    except NetworkReadError as exc:
        statuses = {}
        degraded = True
        warnings.append(exc.payload["error"])

    output = []
    for device in devices:
        ip = str(device.get("ip") or device.get("hostname") or "").strip()
        hostname = str(device.get("hostname") or "").strip()
        sys_name = str(device.get("sysName") or "").strip()
        output.append({
            "id": device.get("device_id"),
            "name": sys_name or hostname or ip or str(device.get("device_id") or ""),
            "hostname": hostname or None,
            "ip": ip or None,
            "status": statuses.get(ip, "unknown"),
        })
    return {
        "ok": True,
        "degraded": degraded,
        "count": len(output),
        "devices": output,
        "warnings": warnings,
    }


def read_topology(context: NetworkReadContext) -> dict[str, Any]:
    raw, modified = _read_bytes(
        context.topology_path, context.topology_byte_limit, "topology"
    )
    edges = _decode_json(raw, "topology")
    if not isinstance(edges, list) or not all(isinstance(edge, dict) for edge in edges):
        raise NetworkReadError(503, "topology_malformed", "topology data is malformed")
    if len(edges) > context.topology_edge_limit:
        raise NetworkReadError(503, "topology_oversize", "topology contains too many edges")
    if any(
        not isinstance(edge.get("from_ip"), str)
        or not edge["from_ip"].strip()
        or not isinstance(edge.get("to_ip"), str)
        or not edge["to_ip"].strip()
        for edge in edges
    ):
        raise NetworkReadError(503, "topology_malformed", "topology data is malformed")
    nodes = sorted({
        edge[field].strip()
        for edge in edges
        for field in ("from_ip", "to_ip")
    })
    age = max(0.0, context.clock() - modified)
    stale = age > context.topology_stale_seconds
    return {
        "ok": True,
        "degraded": stale,
        "stale": stale,
        "generatedAt": _iso_timestamp(modified),
        "nodeCount": len(nodes),
        "edgeCount": len(edges),
        "nodes": nodes,
        "edges": edges,
        "warnings": ["topology snapshot is stale"] if stale else [],
    }


def _read_isp_pair(context: NetworkReadContext) -> tuple[list[dict[str, Any]], dict[str, Any], bytes]:
    inventory_raw, _ = _read_bytes(
        context.isp_inventory_path, context.isp_byte_limit, "isp_inventory"
    )
    state_raw, _ = _read_bytes(
        context.isp_state_path, context.isp_byte_limit, "isp_state"
    )
    inventory = _decode_json(inventory_raw, "isp_inventory")
    state = _decode_json(state_raw, "isp_state")
    if not isinstance(inventory, list) or not all(isinstance(item, dict) for item in inventory):
        raise NetworkReadError(503, "isp_inventory_malformed", "ISP inventory is malformed")
    if len(inventory) > context.isp_limit:
        raise NetworkReadError(503, "isp_inventory_oversize", "ISP inventory contains too many records")
    if not isinstance(state, dict):
        raise NetworkReadError(503, "isp_state_malformed", "ISP discovery state is malformed")
    return inventory, state, inventory_raw


def _isp_pair_consistent(inventory: list[dict[str, Any]], state: dict[str, Any], raw: bytes) -> bool:
    count = state.get("inventory_count", state.get("count"))
    digest = str(state.get("inventory_sha256") or "").strip()
    return (
        isinstance(count, int)
        and not isinstance(count, bool)
        and count == len(inventory)
        and bool(digest)
        and digest == hashlib.sha256(raw).hexdigest()
    )


def _read_applied_isp_ping(context: NetworkReadContext) -> str:
    try:
        raw, _ = _read_bytes(context.env_path, context.env_byte_limit, "runtime_config")
    except NetworkReadError as exc:
        if not context.env_path.exists():
            return ""
        raise exc
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise NetworkReadError(503, "runtime_config_malformed", "runtime configuration is malformed") from exc
    result = ""
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() != "ISP_PING":
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise NetworkReadError(503, "runtime_config_malformed", "runtime configuration is malformed") from exc
        elif len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1]
        result = str(value)
    return result


def read_isp(context: NetworkReadContext) -> dict[str, Any]:
    inventory, state, raw = _read_isp_pair(context)
    consistent = _isp_pair_consistent(inventory, state, raw)
    if not consistent:
        inventory, state, raw = _read_isp_pair(context)
        consistent = _isp_pair_consistent(inventory, state, raw)

    state_status = str(state.get("status") or "").lower()
    degraded = (
        not consistent
        or state_status not in {"ok", "disabled"}
        or (state_status == "disabled" and bool(inventory))
    )
    stale = degraded
    warnings = [] if not degraded else ["ISP inventory and discovery state are stale or inconsistent"]
    try:
        statuses = _probe_statuses(context)
    except NetworkReadError as exc:
        statuses = {}
        degraded = True
        warnings.append(exc.payload["error"])

    entries: list[dict[str, Any]] = []
    known_targets: set[str] = set()
    for index, item in enumerate(inventory):
        targets = item.get("targets")
        labels = item.get("labels")
        if not isinstance(targets, list) or not all(isinstance(value, str) for value in targets) or not isinstance(labels, dict):
            raise NetworkReadError(503, "isp_inventory_malformed", "ISP inventory is malformed")
        target = str(targets[0]).strip() if targets else ""
        if target:
            known_targets.add(target)
        entries.append({
            "name": str(labels.get("display_name") or labels.get("metric_name") or f"ISP {index + 1}"),
            "target": target or None,
            "status": statuses.get(target, "unknown") if target else "unknown",
            "source": str(labels.get("discovery_source") or "auto"),
            "wanIp": str(labels.get("wan_ip") or "").strip() or None,
            "metricTarget": str(labels.get("metric_target") or "").strip() or None,
            "metricIfindex": str(labels.get("metric_ifindex") or "").strip() or None,
        })

    for name, target in parse_named_ipv4_target_rows(_read_applied_isp_ping(context)):
        if target in known_targets:
            continue
        entries.append({
            "name": name or target,
            "target": target,
            "status": statuses.get(target, "unknown"),
            "source": "manual",
            "wanIp": None,
            "metricTarget": None,
            "metricIfindex": None,
        })
        known_targets.add(target)
    if len(entries) > context.isp_limit:
        raise NetworkReadError(503, "isp_inventory_oversize", "ISP inventory contains too many records")
    return {
        "ok": True,
        "degraded": degraded,
        "stale": stale,
        "count": len(entries),
        "isps": entries,
        "warnings": warnings,
    }


def read_overview(context: NetworkReadContext) -> dict[str, Any]:
    domains = {
        "devices": read_devices,
        "topology": read_topology,
        "isp": read_isp,
    }
    payload: dict[str, Any] = {"ok": True, "degraded": False, "warnings": []}
    available = 0
    for name, reader in domains.items():
        try:
            result = reader(context)
        except NetworkReadError as exc:
            payload[name] = None
            payload["degraded"] = True
            payload["warnings"].append({"domain": name, **exc.payload})
            continue
        available += 1
        payload[name] = result
        if result.get("degraded"):
            payload["degraded"] = True
            payload["warnings"].extend(
                {"domain": name, "error": warning} for warning in result.get("warnings", [])
            )
    if not available:
        raise NetworkReadError(503, "network_unavailable", "All network data sources are unavailable")
    return payload
