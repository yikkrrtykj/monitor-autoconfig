"""Read Hillstone HA authority for configured physical units only."""
from __future__ import annotations

from dataclasses import replace
import json
import math
import time
from typing import Any

from target_utils import expand_ipv4_entry
from .network_read import NetworkReadContext, NetworkReadError, _prometheus_query, _read_bytes


UNIT_LIMIT = 16
FRESH_SECONDS = 180  # Three intervals of the dedicated 60-second scalar job.
HA_JOB = "infra-fw-ha-snmp"
STATES = {0: "none", 1: "init", 2: "hello", 3: "backup", 4: "master",
          5: "vendor-unknown", 6: "AA-mode"}


def _targets(context: NetworkReadContext) -> list[tuple[str, str]]:
    raw, _ = _read_bytes(context.env_path, context.env_byte_limit, "runtime_config")
    value = ""
    for line in raw.decode("utf-8", errors="strict").splitlines():
        if line.lstrip().startswith("#") or "=" not in line:
            continue
        key, candidate = line.split("=", 1)
        if key.strip() != "FIREWALL_UNIT_SNMP_TARGETS":
            continue
        value = candidate.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = json.loads(value)
        elif len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1]
        if not isinstance(value, str):
            raise ValueError("invalid physical targets")
    rows = []
    for entry in value.replace("\n", ",").split(","):
        if not entry.strip():
            continue
        ips = expand_ipv4_entry(entry, max_hosts=UNIT_LIMIT)
        if not ips or len(rows) + len(ips) > UNIT_LIMIT:
            raise ValueError("invalid or excessive physical targets")
        name = entry.split(":", 1)[0].strip() if ":" in entry else ""
        for index, ip in enumerate(ips, 1):
            display = f"{name}{index}" if name and len(ips) > 1 else name or ip
            rows.append((display[:160], ip))
    if len({ip for _, ip in rows}) != len(rows):
        raise ValueError("duplicate physical targets")
    return rows


def _value(row: dict[str, Any]) -> float | None:
    value = row.get("value")
    if not isinstance(value, list) or len(value) != 2:
        return None
    try:
        number = float(value[1])
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _identity(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.get("metric", {}).items() if key != "__name__"}


def read_ha(context: NetworkReadContext) -> dict[str, Any]:
    """Return at most 16 units; never substitute instance/name/peer identity."""
    result: dict[str, Any] = {"source": "Hillstone sysHAStatus", "fresh": False, "units": []}
    try:
        targets = _targets(context)
    except (NetworkReadError, UnicodeError, ValueError):
        return result
    if not targets:
        return result
    units = [{"ip": ip, "name": name, "code": None, "state": "unknown", "fresh": False}
             for name, ip in targets]
    result["units"] = units
    # One bounded pair of queries for the configured units, not one per unit.
    regex = "|".join(ip.replace(".", "\\.") for _, ip in targets)
    # Pin both selectors to the same instant across a concurrent HA transition.
    evaluated = context.clock()
    selector = f'sysHAStatus{{job="{HA_JOB}",target_ip=~{json.dumps(regex)}}} @ {evaluated}'
    deadline = time.monotonic() + context.http_timeout
    try:
        values = _prometheus_query(context, selector)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return result
        timestamps = _prometheus_query(replace(context, http_timeout=remaining), f"timestamp({selector})")
    except NetworkReadError:
        return result
    if time.monotonic() > deadline:
        return result
    now = context.clock()
    for unit in units:
        def exact(rows):
            return [row for row in rows if isinstance(row.get("metric"), dict)
                    and row["metric"].get("target_ip") == unit["ip"]
                    and row["metric"].get("job") == HA_JOB]
        matched, stamped = exact(values), exact(timestamps)
        if len(matched) != 1 or len(stamped) != 1 or _identity(matched[0]) != _identity(stamped[0]):
            continue
        code, collected = _value(matched[0]), _value(stamped[0])
        if (collected is None or not 0 <= now - collected <= FRESH_SECONDS
                or code is None or code not in STATES):
            continue
        unit.update(code=int(code), state=STATES[int(code)], fresh=True)
        if code == 5:
            unit["vendorState"] = "slase"
    result["fresh"] = all(unit["fresh"] for unit in units)
    return result
