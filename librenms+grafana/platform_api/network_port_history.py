"""Exact, bounded reads of existing LibreNMS port RRD history; no device polling."""
from __future__ import annotations

import ipaddress
import math
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from librenms_client import LibreNMSError
from .network_read import NetworkReadContext, NetworkReadError
from .network_ports import CISCO_OS, SMALL_BUSINESS_OS, HILLSTONE_OS, PORT_LIMIT, _index

PORT_COLUMNS = "device_id,ifIndex,port_id"
RRD_TIMEOUT = 5
OUTPUT_LIMIT = 256 * 1024
ROW_LIMIT = 902  # At most one-second storage resolution over the fixed 15 minutes.
DAEMON = "librenms-rrdcached:42217"


def _empty(ip: str, index: int, start: int, end: int, reason: str = "empty") -> dict[str, Any]:
    return {"ok": True, "source": "LibreNMS RRD", "ip": ip, "ifIndex": index,
            "start": start, "end": end, "step": None, "rx": [], "tx": [],
            "coverage": {"rx": reason, "tx": reason}}


def _rrd_path(base: Path, hostname: Any, port_id: int) -> Path | None:
    # One strict directory component, never a caller-supplied path or symlink escape.
    if not isinstance(hostname, str) or len(hostname) > 253 or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]*", hostname
    ) or hostname in (".", ".."):
        return None
    try:
        root = base.resolve(strict=True)
        candidate = root / hostname / f"port-id{port_id}.rrd"
        resolved = candidate.resolve(strict=True)
        if resolved != candidate or not resolved.is_relative_to(root) or not resolved.is_file():
            return None
        return resolved
    except (OSError, RuntimeError, ValueError):
        return None


def _fetch(path: Path, base: Path, start: int, end: int) -> str | None:
    # stdout goes to a temporary file to avoid unbounded in-memory capture.
    # fetch has no DS selector; only the two traffic datasets are parsed below.
    try:
        root = base.resolve(strict=True)
        relative = path.relative_to(root)
        # Remote rrdcached rejects absolute names; local reads must use its base too.
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(
                ["rrdtool", "fetch", relative.as_posix(), "AVERAGE", "--daemon", DAEMON,
                 "--start", str(start), "--end", str(end)],
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
                timeout=RRD_TIMEOUT, check=False, shell=False, cwd=root,
            )
            if result.returncode:
                return None
            output.seek(0)
            raw = output.read(OUTPUT_LIMIT + 1)
            if len(raw) > OUTPUT_LIMIT:
                return None
            return raw.decode("ascii", errors="strict")
    except (OSError, subprocess.TimeoutExpired, UnicodeError, ValueError, RuntimeError):
        return None


def _parse(raw: str, payload: dict[str, Any]) -> dict[str, Any]:
    lines = raw.splitlines()
    if not lines or len(lines) > ROW_LIMIT + 4:
        return payload
    columns = lines[0].split()
    if len(columns) > 64 or len(set(columns)) != len(columns):
        return payload
    datasets = {key: columns.index(ds) for key, ds in (("rx", "INOCTETS"), ("tx", "OUTOCTETS")) if ds in columns}
    rows = {}
    for line in lines[1:]:
        if not line.strip():
            continue
        match = re.fullmatch(r"\s*(\d{1,12}):\s+(.+)", line)
        if not match:
            return payload
        timestamp = int(match[1])
        values = match[2].split()
        if len(values) != len(columns) or timestamp in rows:
            return payload
        rows[timestamp] = values
    times = sorted(rows)
    gaps = {b - a for a, b in zip(times, times[1:])}
    if len(gaps) != 1:
        return payload
    payload["step"] = gaps.pop()
    for key, position in datasets.items():
        points = []
        for timestamp in times:
            if not payload["start"] <= timestamp <= payload["end"]:
                continue
            try:
                rate = float(rows[timestamp][position])
                bits = rate * 8
            except ValueError:
                continue
            if math.isfinite(bits) and rate >= 0:
                points.append({"t": timestamp, "v": bits})
        payload[key] = points
        payload["coverage"][key] = "available" if points else "empty"
    return payload


def read_port_history(context: NetworkReadContext, management_ip: str, ifindex: str) -> dict[str, Any]:
    try:
        ip = str(ipaddress.IPv4Address(management_ip))
    except ipaddress.AddressValueError as exc:
        raise NetworkReadError(400, "invalid_node", "A valid management IPv4 address is required") from exc
    index = _index(ifindex)
    if index is None:
        raise NetworkReadError(400, "invalid_ifindex", "A positive numeric ifIndex is required")
    end = int(context.clock())
    payload = _empty(ip, index, end - 900, end)
    client = (context.librenms_history_client_factory or context.librenms_client_factory)()
    try:
        device = client.get_device(ip)  # Real client rejects multiple device matches.
        if not isinstance(device, dict) or device.get("ip") != ip:
            return payload
        device_id = _index(device.get("device_id"))
        if device_id is None or str(device.get("os") or "").lower() not in CISCO_OS | SMALL_BUSINESS_OS | HILLSTONE_OS:
            return payload
        ports = client.get_device_ports(device, columns=PORT_COLUMNS)
    except LibreNMSError:
        return payload
    if not isinstance(ports, list) or len(ports) > PORT_LIMIT or not all(isinstance(row, dict) for row in ports):
        return payload
    matches = [row for row in ports if _index(row.get("ifIndex")) == index]
    if len(matches) > 1:
        return _empty(ip, index, end - 900, end, "ambiguous")
    if not matches or _index(matches[0].get("device_id")) != device_id:
        return payload
    port_id = _index(matches[0].get("port_id"))
    if port_id is None:
        return payload
    path = _rrd_path(context.rrd_base_path, device.get("hostname"), port_id)
    if path is None:
        return payload
    raw = _fetch(path, context.rrd_base_path, payload["start"], end)
    return _parse(raw, payload) if raw is not None else payload
