#!/usr/bin/env python3
"""Discover live switches in a management range and emit a Prometheus file_sd.

Operators only fill a switch management range in the control console. This loop
SNMP-probes every address in that range and writes a blackbox/SNMP target file
containing just the switches that actually answer:

  * answers SNMP  -> kept, named by its real sysName (hostname)
  * answers ICMP only (no SNMP) -> kept, named by its IP as a placeholder
  * answers neither -> left out, so offline/unused IPs never reach the big screen

The file is a standard Prometheus file_sd document, e.g.

  [{"targets": ["192.168.10.11"], "labels": {"display_name": "core-sw-01"}}]

Env vars:
  SWITCH_DISCOVERY_RANGE   comma/newline separated IPs or last-octet ranges
                           (e.g. 192.168.10.11-30). CIDR blocks are ignored --
                           those are for LibreNMS discovery, not ICMP/SNMP sweeps.
  SNMP_COMMUNITY           SNMPv2c community (default: global).
  SWITCH_TARGETS_FILE      output path (default: /targets/switch_targets.json).
  SWITCH_DISCOVERY_WORKERS parallel probes (default: 16).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from ipaddress import IPv4Address

SYS_NAME_OID = "1.3.6.1.2.1.1.5.0"


def expand_range(item: str) -> list[str]:
    """Expand one entry ("name:" prefix optional) into individual IPs. CIDR and
    junk yield nothing; last-octet ("a.b.c.11-30") and full ("a.b.c.11-a.b.c.30")
    ranges both work, mirroring the other generators."""
    item = (item or "").strip()
    if not item:
        return []
    if ":" in item:
        item = item.split(":", 1)[1].strip()
    if "/" in item or not item:
        return []
    if "-" not in item:
        try:
            return [str(IPv4Address(item))]
        except ValueError:
            return []
    start_raw, end_raw = [part.strip() for part in item.split("-", 1)]
    try:
        start = IPv4Address(start_raw)
    except ValueError:
        return []
    if re.fullmatch(r"\d{1,3}", end_raw):
        try:
            end = IPv4Address(f"{start_raw.rsplit('.', 1)[0]}.{end_raw}")
        except ValueError:
            return []
    else:
        try:
            end = IPv4Address(end_raw)
        except ValueError:
            return []
    if int(end) < int(start) or int(end) - int(start) > 4096:
        return []
    return [str(IPv4Address(int(start) + offset)) for offset in range(int(end) - int(start) + 1)]


def expand_targets(raw: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for part in re.split(r"[,\n]+", raw or ""):
        for ip in expand_range(part):
            if ip not in seen:
                seen.add(ip)
                out.append(ip)
    return out


def looks_like_ip(value: str) -> bool:
    return bool(re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", str(value or "")))


def snmp_sysname(ip: str, community: str, timeout: int = 2) -> str:
    """Return the device sysName, or "" when SNMP does not answer."""
    try:
        result = subprocess.run(
            ["snmpget", "-v2c", "-c", community, "-Ovq", "-t", str(timeout), "-r", "1", ip, SYS_NAME_OID],
            capture_output=True, text=True, timeout=timeout + 3,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    name = result.stdout.strip().strip('"').strip()
    if not name or name.lower().startswith(("no such", "no more")):
        return ""
    return name


def ping_alive(ip: str, timeout: int = 1) -> bool:
    try:
        return subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout), ip],
            capture_output=True,
        ).returncode == 0
    except Exception:
        return False


def discover(ips, community, probe_snmp=snmp_sysname, probe_ping=ping_alive, workers=16) -> dict[str, str]:
    """Map each live IP to its display name. SNMP hostname wins; an ICMP-only host
    falls back to its IP; an unreachable host is dropped entirely."""
    def classify(ip):
        name = probe_snmp(ip, community)
        if name and not looks_like_ip(name):
            return ip, name
        if probe_ping(ip):
            return ip, ip
        return ip, None

    results: dict[str, str] = {}
    if not ips:
        return results
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        for ip, name in executor.map(classify, ips):
            if name is not None:
                results[ip] = name
    return results


def build_file_sd(results: dict[str, str]) -> list[dict]:
    return [
        {"targets": [ip], "labels": {"display_name": name}}
        for ip, name in sorted(results.items(), key=lambda kv: int(IPv4Address(kv[0])))
    ]


def write_file_sd(path: str, payload: list[dict]) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def main() -> None:
    raw = os.environ.get("SWITCH_DISCOVERY_RANGE", "").strip()
    out = os.environ.get("SWITCH_TARGETS_FILE", "/targets/switch_targets.json")
    community = os.environ.get("SNMP_COMMUNITY", "global")
    workers = int(os.environ.get("SWITCH_DISCOVERY_WORKERS", "16") or "16")

    ips = expand_targets(raw)
    if not ips:
        write_file_sd(out, [])
        print("[switch-discovery] no range configured; wrote empty target file", file=sys.stderr)
        return
    results = discover(ips, community, workers=workers)
    write_file_sd(out, build_file_sd(results))
    print(f"[switch-discovery] scanned {len(ips)} addresses -> {len(results)} live switches", file=sys.stderr)


if __name__ == "__main__":
    main()
