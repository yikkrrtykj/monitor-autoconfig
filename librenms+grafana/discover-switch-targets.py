#!/usr/bin/env python3
"""Discover live switches in a management range and emit a Prometheus file_sd.

Operators only fill a switch management range in the control console. This loop
finds the switches that are actually present and writes a blackbox/SNMP target
file containing just those:

  * reachable + answers SNMP  -> kept, named by its real sysName (hostname)
  * reachable (ICMP) but no SNMP -> kept, named by its IP as a placeholder
  * never observed -> left out, so unused addresses never reach the big screen
  * previously confirmed but unreachable -> retained for 24 hours so its node,
    last uplink and red DOWN state remain visible during an incident

Efficiency: a /24 with only a handful of switches stays cheap because every
address is first checked with a short, parallel ICMP probe and only the live
ones are asked for SNMP. Addresses already monitored explicitly (core,
firewall, listed switches) are skipped so they are not double-counted.

Env vars:
  SWITCH_DISCOVERY_RANGE      IPs / last-octet ranges / CIDR to probe
                              (e.g. 192.168.10.0/24 or 192.168.10.11-30).
  SNMP_COMMUNITY              SNMPv2c community (default: global).
  SWITCH_TARGETS_FILE         output path (default: /targets/switch_targets.json).
  SWITCH_DISCOVERY_WORKERS    parallel probes (default: 8).
  SWITCH_DISCOVERY_PING_TIMEOUT  ICMP timeout seconds (default: 1).
  SWITCH_DISCOVERY_SNMP_TIMEOUT  SNMP timeout seconds (default: 1).
  SWITCH_DISCOVERY_MAX_HOSTS  safety cap on addresses probed (default: 1024).
  SWITCH_DISCOVERY_RETENTION_SECONDS  confirmed offline target retention
                              (default: 86400 / 24 hours).
  SWITCH_DISCOVERY_STATE_FILE  durable discovery ledger (default:
                              SWITCH_TARGETS_FILE + ".state.json").
  CORE_SWITCH_PING/DIST_SWITCH_PING/FIREWALL_PING/TOURNAMENT_SWITCHES
                              already-monitored targets, excluded from results.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from target_utils import (
    build_file_sd,
    expand_ipv4_targets as expand_targets,
    is_ipv4 as looks_like_ip,
    load_file_sd_targets,
    write_json_atomic as write_file_sd,
)

SYS_NAME_OID = "1.3.6.1.2.1.1.5.0"
DEFAULT_MAX_HOSTS = 1024
DEFAULT_RETENTION_SECONDS = 24 * 60 * 60


def excluded_ips(*raws: str) -> set[str]:
    out: set[str] = set()
    for raw in raws:
        out.update(expand_targets(raw))
    return out


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
             workers=8, ping_timeout=1, snmp_timeout=1) -> dict[str, str]:
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


def load_discovery_state(path: str) -> dict[str, dict]:
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    state: dict[str, dict] = {}
    for ip, item in payload.items():
        if not looks_like_ip(ip) or not isinstance(item, dict):
            continue
        try:
            last_seen = float(item.get("last_seen"))
        except (TypeError, ValueError):
            continue
        name = str(item.get("name") or ip).strip() or ip
        state[ip] = {"name": name, "last_seen": last_seen}
    return state


def retain_confirmed_targets(results: dict[str, str], candidate_ips, state,
                             now: float, retention_seconds: int):
    """Merge this probe cycle with the durable confirmed-device ledger.

    Only addresses still present in the configured discovery range are
    eligible. This makes removing/changing a range authoritative while a
    transient outage keeps an already-known switch monitored for 24 hours.
    """
    candidates = set(candidate_ips)
    retained_state: dict[str, dict] = {}
    targets: dict[str, str] = {}
    retention_seconds = max(0, int(retention_seconds))
    for ip in candidates:
        if ip in results:
            name = str(results[ip] or ip).strip() or ip
            previous = state.get(ip) if isinstance(state, dict) else None
            previous_name = str((previous or {}).get("name") or "").strip()
            if looks_like_ip(name) and previous_name and not looks_like_ip(previous_name):
                name = previous_name
            retained_state[ip] = {"name": name, "last_seen": float(now)}
            targets[ip] = name
            continue
        previous = state.get(ip) if isinstance(state, dict) else None
        if not isinstance(previous, dict):
            continue
        try:
            last_seen = float(previous.get("last_seen"))
        except (TypeError, ValueError):
            continue
        if now - last_seen > retention_seconds:
            continue
        name = str(previous.get("name") or ip).strip() or ip
        retained_state[ip] = {"name": name, "last_seen": last_seen}
        targets[ip] = name
    return targets, retained_state


def main() -> None:
    raw = os.environ.get("SWITCH_DISCOVERY_RANGE", "").strip()
    out = os.environ.get("SWITCH_TARGETS_FILE", "/targets/switch_targets.json")
    community = os.environ.get("SNMP_COMMUNITY", "global")
    workers = int(os.environ.get("SWITCH_DISCOVERY_WORKERS", "8") or "8")
    ping_timeout = int(os.environ.get("SWITCH_DISCOVERY_PING_TIMEOUT", "1") or "1")
    snmp_timeout = int(os.environ.get("SWITCH_DISCOVERY_SNMP_TIMEOUT", "1") or "1")
    max_hosts = int(os.environ.get("SWITCH_DISCOVERY_MAX_HOSTS", str(DEFAULT_MAX_HOSTS)) or DEFAULT_MAX_HOSTS)
    retention_seconds = int(os.environ.get(
        "SWITCH_DISCOVERY_RETENTION_SECONDS", str(DEFAULT_RETENTION_SECONDS)
    ) or DEFAULT_RETENTION_SECONDS)
    state_file = os.environ.get("SWITCH_DISCOVERY_STATE_FILE", f"{out}.state.json")

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
        write_file_sd(state_file, {})
        print("[switch-discovery] no range configured; wrote empty target file", file=sys.stderr)
        return
    results = discover(ips, community, workers=workers, ping_timeout=ping_timeout, snmp_timeout=snmp_timeout)
    now = time.time()
    discovery_state = load_discovery_state(state_file)
    if not discovery_state:
        # Upgrade migration: the old file_sd contains only devices that were
        # previously live, so it is safe to seed the new 24-hour ledger from it.
        discovery_state = {
            ip: {"name": name, "last_seen": now}
            for ip, name in load_file_sd_targets(out).items()
        }
    targets, state = retain_confirmed_targets(
        results,
        ips,
        discovery_state,
        now,
        retention_seconds,
    )
    write_file_sd(state_file, state, sort_keys=True)
    write_file_sd(out, build_file_sd(targets))
    retained = len(targets) - len(results)
    print(
        f"[switch-discovery] probed {len(ips)} addresses -> {len(results)} live, "
        f"{retained} offline retained",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
