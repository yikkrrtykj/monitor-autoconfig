"""Bounded, read-only aggregation for the platform network endpoints."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from ipaddress import IPv4Address
import json
import math
import os
from pathlib import Path
import time
from typing import Any, Callable
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from librenms_client import LibreNMSError
from target_utils import normalize_mac, parse_named_ipv4_target_rows


DEVICE_LIMIT = 5000
TOPOLOGY_EDGE_LIMIT = 20000
ISP_LIMIT = 1024
HTTP_BYTE_LIMIT = 4 * 1024 * 1024
TOPOLOGY_BYTE_LIMIT = 16 * 1024 * 1024
ISP_BYTE_LIMIT = 2 * 1024 * 1024
AP_PLACEMENT_SCHEMA_VERSION = 1
AP_PLACEMENT_BYTE_LIMIT = 4 * 1024 * 1024
AP_PLACEMENT_LIMIT = 512
AP_PLACEMENT_MAX_AGE_LIMIT = 86400
AP_PLACEMENT_FUTURE_SKEW_SECONDS = 60
AP_PLACEMENT_WARNING_LIMIT = 32
AP_PLACEMENT_TEXT_LIMIT = 256
AP_PLACEMENT_COVERAGE_REASON_LIMIT = 16
AP_PLACEMENT_LEVELS = (
    "confirmed-direct",
    "observed-attachment",
    "ambiguous",
    "unresolved",
)
AP_PLACEMENT_ONLINE_STATES = ("online", "offline", "unknown")
AP_PLACEMENT_COVERAGE_STATES = ("complete", "partial", "unknown")
AP_PLACEMENT_COUNT_KEYS = (
    "confirmed_direct",
    "observed_attachment",
    "ambiguous",
    "unresolved",
    "online",
    "offline",
    "unknown",
    "total",
)
AP_PLACEMENT_LEVEL_COUNT_KEY = {
    "confirmed-direct": "confirmed_direct",
    "observed-attachment": "observed_attachment",
    "ambiguous": "ambiguous",
    "unresolved": "unresolved",
}
AP_PLACEMENT_INVALID_REASONS = frozenset({
    "ap_identity_unavailable",
    "no_ap_identity",
    "no_topology_devices",
    "invalid",
})
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
    ap_placement_path: Path = Path(
        "/etc/prometheus/targets/topology/ap-placement.json")
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
    rrd_base_path: Path = Path("/data/db")
    librenms_history_client_factory: Callable[[], Any] | None = None


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
        'probe_success{job=~"infra-isp-ping|infra-core-ping|infra-dist-ping|infra-fw-ping|infra-fw-unit-ping|infra-srv-ping"}',
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


def _ap_number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _ap_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _ap_text(value, limit=AP_PLACEMENT_TEXT_LIMIT):
    if value is None:
        return ""
    return str(value).strip()[:limit]


def _ap_warning(warnings, message):
    if len(warnings) < AP_PLACEMENT_WARNING_LIMIT:
        warnings.append(_ap_text(message, AP_PLACEMENT_TEXT_LIMIT))


def _ap_malformed(message):
    return NetworkReadError(503, "ap_placement_malformed", message)


def _ap_ipv4(value):
    try:
        return str(IPv4Address(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _ap_placement_counts(counts, placements):
    """Validate the raw snapshot counts against the raw placements."""
    if not isinstance(counts, dict):
        raise _ap_malformed("AP placement counts are malformed")
    validated = {}
    for key in AP_PLACEMENT_COUNT_KEYS:
        value = counts.get(key)
        if not _ap_int(value) or value < 0:
            raise _ap_malformed("AP placement counts are malformed")
        validated[key] = value
    level_counts = {level: 0 for level in AP_PLACEMENT_LEVELS}
    online_counts = {state: 0 for state in AP_PLACEMENT_ONLINE_STATES}
    for item in placements:
        if not isinstance(item, dict):
            raise _ap_malformed("AP placement snapshot is malformed")
        level = item.get("level")
        online = item.get("online")
        if level not in AP_PLACEMENT_LEVELS or online not in AP_PLACEMENT_ONLINE_STATES:
            raise _ap_malformed("AP placement snapshot is malformed")
        level_counts[level] += 1
        online_counts[online] += 1
    expected = {
        AP_PLACEMENT_LEVEL_COUNT_KEY[level]: count
        for level, count in level_counts.items()
    }
    expected.update(online_counts)
    expected["total"] = len(placements)
    if validated != expected:
        raise _ap_malformed("AP placement counts do not match placements")
    return validated


def _ap_placement_evidence(value, warnings, label):
    """Whitelist the evidence summary; unknown nested fields are dropped."""
    result = {}
    if value is None:
        return result
    if not isinstance(value, dict):
        _ap_warning(warnings, f"{label}: evidence summary is not an object")
        return result
    fdb = value.get("fdb")
    if isinstance(fdb, dict):
        result["fdb"] = {
            "status": _ap_text(fdb.get("status"), 64),
            "candidate_age_seconds": (
                float(fdb["candidate_age_seconds"])
                if _ap_number(fdb.get("candidate_age_seconds")) else None
            ),
            "raw_candidates": (
                int(fdb["raw_candidates"])
                if _ap_int(fdb.get("raw_candidates")) and fdb["raw_candidates"] >= 0
                else None
            ),
        }
    exact = value.get("exact_snmp")
    if isinstance(exact, dict):
        result["exact_snmp"] = {
            "status": _ap_text(exact.get("status"), 64),
            "gets": (
                int(exact["gets"])
                if _ap_int(exact.get("gets")) and exact["gets"] >= 0 else None
            ),
            "lookup_attempts": (
                int(exact["lookup_attempts"])
                if _ap_int(exact.get("lookup_attempts")) and exact["lookup_attempts"] >= 0
                else None
            ),
            "validated_at": (
                float(exact["validated_at"])
                if _ap_number(exact.get("validated_at")) else None
            ),
            "validation_age_seconds": (
                float(exact["validation_age_seconds"])
                if _ap_number(exact.get("validation_age_seconds")) else None
            ),
        }
    direct = value.get("direct_neighbor")
    if isinstance(direct, dict):
        result["direct_neighbor"] = {
            "status": _ap_text(direct.get("status"), 64),
            "source": _ap_text(direct.get("source"), 64),
        }
    manual = value.get("manual_confirmation")
    if isinstance(manual, dict):
        result["manual_confirmation"] = {"status": _ap_text(manual.get("status"), 64)}
    online = value.get("online")
    if isinstance(online, dict):
        result["online"] = {
            "status": _ap_text(online.get("status"), 32),
            "source": _ap_text(online.get("source"), 64),
            "reason": _ap_text(online.get("reason"), 64),
        }
    coverage = value.get("coverage")
    if isinstance(coverage, dict):
        reasons = coverage.get("reasons")
        result["coverage"] = {
            "status": _ap_text(coverage.get("status"), 32),
            "reasons": (
                [_ap_text(reason, 128) for reason in reasons[:8]
                 if isinstance(reason, str)]
                if isinstance(reasons, list) else []
            ),
        }
    port = value.get("port")
    if isinstance(port, dict):
        result["port"] = {"verified": port.get("verified") is True}
    return result


def _ap_placement_last_observed(value, warnings, label):
    if value is None:
        return None
    if not isinstance(value, dict):
        _ap_warning(warnings, f"{label}: last_observed is not an object")
        return None
    result = {}
    switch_ip = _ap_ipv4(value.get("switch_ip"))
    if switch_ip:
        result["switch_ip"] = switch_ip
    if _ap_int(value.get("switch_ifindex")) and value["switch_ifindex"] > 0:
        result["switch_ifindex"] = int(value["switch_ifindex"])
    switch_port = value.get("switch_port")
    if isinstance(switch_port, str) and switch_port.strip():
        result["switch_port"] = _ap_text(switch_port, 128)
    if _ap_int(value.get("vlan")) and 1 <= value["vlan"] <= 4094:
        result["vlan"] = int(value["vlan"])
    if _ap_number(value.get("evidence_age_seconds")) and value["evidence_age_seconds"] >= 0:
        result["evidence_age_seconds"] = float(value["evidence_age_seconds"])
    if _ap_number(value.get("validated_at")):
        result["validated_at"] = float(value["validated_at"])
    if (_ap_number(value.get("fdb_candidate_age_seconds"))
            and value["fdb_candidate_age_seconds"] >= 0):
        result["fdb_candidate_age_seconds"] = float(value["fdb_candidate_age_seconds"])
    return result or None


def _ap_placement_coverage(value):
    result = {
        "status": "unknown",
        "expected_switches": None,
        "polled_switches": None,
        "missing_switches": 0,
        "poll_failed_switches": 0,
        "fdb_switches_usable": 0,
        "fdb_switches_failed": 0,
        "reasons": [],
    }
    if not isinstance(value, dict):
        return result
    status = value.get("status")
    if status in AP_PLACEMENT_COVERAGE_STATES:
        result["status"] = status
    for key in ("expected_switches", "polled_switches"):
        if _ap_int(value.get(key)) and value[key] >= 0:
            result[key] = int(value[key])
    for key in ("missing_switches", "poll_failed_switches",
                "fdb_switches_usable", "fdb_switches_failed"):
        if _ap_int(value.get(key)) and value[key] >= 0:
            result[key] = int(value[key])
    reasons = value.get("reasons")
    if isinstance(reasons, list):
        result["reasons"] = [
            _ap_text(reason, 128) for reason in reasons[:AP_PLACEMENT_COVERAGE_REASON_LIMIT]
            if isinstance(reason, str)
        ]
    return result


def _ap_placement_entry(item, index, now, max_age, file_age, seen_macs, seen_ips, warnings):
    """Validate and sanitize one placement; downgrade only, never promote."""
    if not isinstance(item, dict):
        raise _ap_malformed("AP placement snapshot is malformed")
    label = f"placement[{index}]"
    mac = normalize_mac(item.get("ap_mac"))
    if not mac:
        raise _ap_malformed("AP placement contains an unrecognised AP identity")
    if mac in seen_macs:
        raise _ap_malformed("AP placement contains duplicate AP MAC identities")
    seen_macs.add(mac)
    ip = _ap_ipv4(item.get("ap_ip"))
    if not ip:
        raise _ap_malformed("AP placement contains an invalid AP IPv4 identity")
    if ip in seen_ips:
        raise _ap_malformed("AP placement contains duplicate AP IP identities")
    seen_ips.add(ip)
    level = item.get("level")
    online = item.get("online")
    if level not in AP_PLACEMENT_LEVELS or online not in AP_PLACEMENT_ONLINE_STATES:
        raise _ap_malformed("AP placement snapshot is malformed")
    entry = {
        "ap_ip": ip,
        "ap_name": _ap_text(item.get("ap_name"), 128),
        "ap_mac": mac,
        "site": _ap_text(item.get("site"), 128),
        "online": online,
        "level": level,
        "switch_ip": None,
        "switch_ifindex": None,
        "switch_port": None,
        "vlan": None,
        "evidence_age_seconds": None,
        "last_observed": _ap_placement_last_observed(
            item.get("last_observed"), warnings, label),
        "coverage_status": (
            item.get("coverage_status")
            if item.get("coverage_status") in AP_PLACEMENT_COVERAGE_STATES
            else "unknown"
        ),
        "reason": _ap_text(item.get("reason")),
        "evidence": _ap_placement_evidence(item.get("evidence"), warnings, label),
    }
    current = level in ("confirmed-direct", "observed-attachment")
    raw_current = (
        item.get("switch_ip"),
        item.get("switch_ifindex"),
        item.get("switch_port"),
        item.get("vlan"),
        item.get("evidence_age_seconds"),
    )
    if not current:
        if any(value is not None for value in raw_current):
            _ap_warning(warnings, f"{label}: non-current placement fields cleared")
        return entry

    problems = []
    switch_ip = _ap_ipv4(item.get("switch_ip"))
    if not switch_ip:
        problems.append("invalid_switch_ip")
    switch_ifindex = item.get("switch_ifindex")
    if not _ap_int(switch_ifindex) or switch_ifindex <= 0:
        problems.append("invalid_switch_ifindex")
    switch_port = item.get("switch_port")
    if not isinstance(switch_port, str) or not switch_port.strip():
        problems.append("invalid_switch_port")
    vlan = item.get("vlan")
    if not _ap_int(vlan) or not 1 <= vlan <= 4094:
        problems.append("invalid_vlan")
    evidence_age = item.get("evidence_age_seconds")
    if not _ap_number(evidence_age) or evidence_age < 0:
        problems.append("invalid_evidence_age")
    if level == "confirmed-direct" and online != "online":
        problems.append("online_gate_conflict")
    if online == "offline":
        problems.append("offline_current_placement")

    effective_age = None
    if not problems:
        effective_age = float(evidence_age) + file_age
        validated_at = entry["evidence"].get("exact_snmp", {}).get("validated_at")
        if validated_at is not None:
            cross_age = max(0.0, now - float(validated_at))
            if abs(cross_age - effective_age) > 120:
                _ap_warning(warnings, f"{label}: exact evidence timestamps differ; "
                                       "using conservative age")
            effective_age = max(effective_age, cross_age)
        if effective_age > max_age:
            problems.append("exact_evidence_stale")

    if problems:
        entry["level"] = "unresolved"
        entry["reason"] = problems[0]
        _ap_warning(warnings, f"{label}: downgraded to unresolved ({problems[0]})")
        return entry

    entry.update({
        "switch_ip": switch_ip,
        "switch_ifindex": int(switch_ifindex),
        "switch_port": switch_port.strip()[:128],
        "vlan": int(vlan),
        "evidence_age_seconds": round(float(effective_age), 3),
    })
    return entry


def _ap_placement_recount(placements):
    counts = {key: 0 for key in AP_PLACEMENT_COUNT_KEYS}
    for entry in placements:
        counts[AP_PLACEMENT_LEVEL_COUNT_KEY[entry["level"]]] += 1
        counts[entry["online"]] += 1
        counts["total"] += 1
    return counts


def read_ap_placement(context: NetworkReadContext) -> dict[str, Any]:
    """Read and validate the P1 AP placement shadow snapshot (read-only)."""
    raw, _modified = _read_bytes(
        context.ap_placement_path, AP_PLACEMENT_BYTE_LIMIT, "ap_placement")
    payload = _decode_json(raw, "ap_placement")
    if not isinstance(payload, dict):
        raise _ap_malformed("AP placement snapshot is malformed")
    schema = payload.get("schema_version")
    if not _ap_int(schema) or schema != AP_PLACEMENT_SCHEMA_VERSION:
        raise NetworkReadError(
            503, "ap_placement_schema_unsupported",
            "AP placement schema is not supported")
    valid = payload.get("valid")
    if not isinstance(valid, bool):
        raise _ap_malformed("AP placement snapshot is malformed")
    if not valid:
        reason = payload.get("invalid_reason")
        safe_reason = (
            reason if isinstance(reason, str) and reason in AP_PLACEMENT_INVALID_REASONS
            else "invalid"
        )
        raise NetworkReadError(
            503, "ap_placement_invalidated",
            f"AP placement snapshot is invalidated ({safe_reason})")
    generated_at = payload.get("generated_at")
    if not _ap_number(generated_at):
        raise _ap_malformed("AP placement snapshot is malformed")
    max_age = payload.get("max_age_seconds")
    if not _ap_int(max_age) or not 0 < max_age <= AP_PLACEMENT_MAX_AGE_LIMIT:
        raise _ap_malformed("AP placement max_age_seconds is invalid")
    now = float(context.clock())
    file_age = now - float(generated_at)
    if file_age < -AP_PLACEMENT_FUTURE_SKEW_SECONDS:
        raise NetworkReadError(
            503, "ap_placement_stale",
            "AP placement generated_at is in the future")
    file_age = max(0.0, file_age)
    if file_age > max_age:
        raise NetworkReadError(
            503, "ap_placement_stale", "AP placement snapshot is stale")
    placements = payload.get("placements")
    if not isinstance(placements, list):
        raise _ap_malformed("AP placement snapshot is malformed")
    if len(placements) > AP_PLACEMENT_LIMIT:
        raise _ap_malformed("AP placement contains too many placements")
    source_counts = _ap_placement_counts(payload.get("counts"), placements)
    coverage = _ap_placement_coverage(payload.get("coverage"))
    warnings = []
    seen_macs = set()
    seen_ips = set()
    sanitized = [
        _ap_placement_entry(item, index, now, max_age, file_age,
                            seen_macs, seen_ips, warnings)
        for index, item in enumerate(placements)
    ]
    counts = _ap_placement_recount(sanitized)
    if counts != source_counts:
        _ap_warning(warnings, "counts were recomputed after entry sanitization")
    result = {
        "ok": True,
        "schema_version": AP_PLACEMENT_SCHEMA_VERSION,
        "generated_at": float(generated_at),
        "max_age_seconds": int(max_age),
        "valid": True,
        "file_age_seconds": round(file_age, 3),
        "counts": counts,
        "coverage": coverage,
        "placements": sanitized,
        "warnings": warnings,
        "degraded": bool(warnings),
    }
    if counts != source_counts:
        result["source_counts"] = source_counts
    return result


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
