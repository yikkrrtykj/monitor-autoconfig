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

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from target_utils import (
    build_file_sd,
    expand_ipv4_targets as expand_targets,
    is_ipv4,
    write_json_atomic as write_file_sd,
)

SYS_NAME_OID = "1.3.6.1.2.1.1.5.0"
DEFAULT_MAX_HOSTS = 1024


def excluded_ips(*raws: str) -> set[str]:
    out: set[str] = set()
    for raw in raws:
        out.update(expand_targets(raw))
    return out


def looks_like_ip(value: str) -> bool:
    return is_ipv4(value)


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
    """Map live switch candidates to their display names.

    ICMP is only a diagnostic hint: it cannot prove a host is a switch, and an
    ACL may block ping on a perfectly healthy switch. Every bounded candidate
    is therefore SNMP-probed. SNMP supplies the preferred name, but an
    ICMP-live candidate remains as an IP-labelled target when a transient SNMP
    timeout occurs so its ping/offline monitoring does not disappear.
    """
    if not ips:
        return {}
    workers = max(1, workers)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        ping_alive_map = dict(zip(ips, executor.map(lambda ip: probe_ping(ip, ping_timeout), ips)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        names = list(executor.map(lambda ip: probe_snmp(ip, community, snmp_timeout), ips))

    results: dict[str, str] = {}
    for ip, name in zip(ips, names):
        if name and not looks_like_ip(name):
            results[ip] = name          # SNMP hostname wins
        elif name:
            results[ip] = ip            # SNMP answered but sysName itself is an IP
        elif ping_alive_map.get(ip):
            results[ip] = ip
            print(f"[switch-discovery] keep ping-live SNMP-missing host {ip} as IP placeholder", file=sys.stderr)
    return results


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
