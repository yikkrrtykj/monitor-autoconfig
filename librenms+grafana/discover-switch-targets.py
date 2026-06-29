#!/usr/bin/env python3
"""Discover live switches in a management range and emit a Prometheus file_sd.

Operators only fill a switch management range in the control console. This loop
finds the switches that are actually present and writes a blackbox/SNMP target
file containing just those:

  * reachable + answers SNMP  -> kept, named by its real sysName (hostname)
  * reachable (ICMP) but no SNMP -> kept, named by its IP as a placeholder
  * unreachable -> left out, so offline/unused addresses never reach the big
    screen or get continuously scraped

Efficiency: a /24 with only a handful of switches stays cheap because every
address is first checked with a short, parallel ICMP probe and only the live
ones are asked for SNMP. Addresses already monitored explicitly (core,
firewall, listed switches) are skipped so they are not double-counted.

Env vars:
  SWITCH_DISCOVERY_RANGE      IPs / last-octet ranges / CIDR to probe
                              (e.g. 192.168.10.0/24 or 192.168.10.11-30).
  SNMP_COMMUNITY              SNMPv2c community (default: global).
  SWITCH_TARGETS_FILE         output path (default: /targets/switch_targets.json).
  SWITCH_DISCOVERY_WORKERS    parallel probes (default: 32).
  SWITCH_DISCOVERY_PING_TIMEOUT  ICMP timeout seconds (default: 1).
  SWITCH_DISCOVERY_SNMP_TIMEOUT  SNMP timeout seconds (default: 1).
  SWITCH_DISCOVERY_MAX_HOSTS  safety cap on addresses probed (default: 1024).
  CORE_SWITCH_PING/DIST_SWITCH_PING/FIREWALL_PING/TOURNAMENT_SWITCHES
                              already-monitored targets, excluded from results.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from ipaddress import IPv4Address, IPv4Network

SYS_NAME_OID = "1.3.6.1.2.1.1.5.0"
DEFAULT_MAX_HOSTS = 1024


def expand_range(item: str) -> list[str]:
    """Expand one entry into individual IPs. Handles an optional "name:" prefix,
    single IPs, last-octet ("a.b.c.11-30") and full ranges, and CIDR blocks
    ("a.b.c.0/24" -> usable host addresses)."""
    item = (item or "").strip()
    if not item:
        return []
    if ":" in item:
        item = item.split(":", 1)[1].strip()
    if not item:
        return []
    if "/" in item:
        try:
            return [str(ip) for ip in IPv4Network(item, strict=False).hosts()]
        except ValueError:
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


def excluded_ips(*raws: str) -> set[str]:
    out: set[str] = set()
    for raw in raws:
        out.update(expand_targets(raw))
    return out


def looks_like_ip(value: str) -> bool:
    return bool(re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", str(value or "")))


def ping_alive(ip: str, timeout: int = 1) -> bool:
    try:
        return subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout), ip],
            capture_output=True,
        ).returncode == 0
    except Exception:
        return False


def snmp_sysname(ip: str, community: str, timeout: int = 1) -> str:
    """Return the device sysName, or "" when SNMP does not answer."""
    try:
        result = subprocess.run(
            ["snmpget", "-v2c", "-c", community, "-Ovq", "-t", str(timeout), "-r", "0", ip, SYS_NAME_OID],
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


def discover(ips, community, probe_snmp=snmp_sysname, probe_ping=ping_alive,
             workers=32, ping_timeout=1, snmp_timeout=1) -> dict[str, str]:
    """Map each live IP to its display name. ICMP gates liveness first (cheap),
    then only reachable hosts are asked for SNMP -- so a sparse /24 stays fast.
    SNMP hostname wins; a reachable host without SNMP keeps its IP; unreachable
    hosts are dropped. If ICMP finds nothing (e.g. unavailable in the runtime),
    fall back to an SNMP sweep so discovery still works."""
    if not ips:
        return {}
    workers = max(1, workers)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        alive = [ip for ip, ok in zip(ips, executor.map(lambda ip: probe_ping(ip, ping_timeout), ips)) if ok]

    # ICMP normally gates liveness. If it answered for nobody (e.g. ping is not
    # usable in the runtime), sweep every address with SNMP instead and keep
    # only the ones that actually respond.
    ping_gated = bool(alive)
    scan = alive if ping_gated else list(ips)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        names = list(executor.map(lambda ip: probe_snmp(ip, community, snmp_timeout), scan))

    results: dict[str, str] = {}
    for ip, name in zip(scan, names):
        if name and not looks_like_ip(name):
            results[ip] = name          # SNMP hostname wins
        elif ping_gated:
            results[ip] = ip            # reachable but no SNMP -> IP placeholder
        # else: sweep mode without SNMP -> liveness unconfirmed, drop
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
    workers = int(os.environ.get("SWITCH_DISCOVERY_WORKERS", "32") or "32")
    ping_timeout = int(os.environ.get("SWITCH_DISCOVERY_PING_TIMEOUT", "1") or "1")
    snmp_timeout = int(os.environ.get("SWITCH_DISCOVERY_SNMP_TIMEOUT", "1") or "1")
    max_hosts = int(os.environ.get("SWITCH_DISCOVERY_MAX_HOSTS", str(DEFAULT_MAX_HOSTS)) or DEFAULT_MAX_HOSTS)

    exclude = excluded_ips(
        os.environ.get("CORE_SWITCH_PING", ""),
        os.environ.get("DIST_SWITCH_PING", ""),
        os.environ.get("FIREWALL_PING", ""),
        os.environ.get("TOURNAMENT_SWITCHES", ""),
    )
    ips = [ip for ip in expand_targets(raw) if ip not in exclude]
    if len(ips) > max_hosts:
        print(f"[switch-discovery] range expands to {len(ips)} addresses; capping at {max_hosts}", file=sys.stderr)
        ips = ips[:max_hosts]

    if not ips:
        write_file_sd(out, [])
        print("[switch-discovery] no range configured; wrote empty target file", file=sys.stderr)
        return
    results = discover(ips, community, workers=workers, ping_timeout=ping_timeout, snmp_timeout=snmp_timeout)
    write_file_sd(out, build_file_sd(results))
    print(f"[switch-discovery] probed {len(ips)} addresses -> {len(results)} live switches", file=sys.stderr)


if __name__ == "__main__":
    main()
