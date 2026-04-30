#!/usr/bin/env python3
"""
Query stage switches via SNMP to map IP addresses to Team/Seat labels,
then generate a Prometheus file_sd JSON file for blackbox-exporter ICMP targets.

Usage (inside container):
  python3 generate-player-targets.py

Environment variables:
  TOURNAMENT_SWITCHES    comma-separated switch IPs (e.g. 192.168.10.11,192.168.10.12)
  SNMP_COMMUNITY         SNMP v2c community string
  PLAYER_SUBNETS         comma-separated subnets to filter (e.g. 192.168.35.0/24)
  PLAYER_TARGETS_FILE    output path (default: /etc/prometheus/player_targets.json)
  WIRELESS_SUBNETS       comma-separated wireless subnets (e.g. 192.168.66.0/24)
"""

import json
import os
import re
import subprocess
import sys
from ipaddress import IPv4Address, IPv4Network

IF_ALIAS_OID = "1.3.6.1.2.1.31.1.1.1.18"
ARP_IFINDEX_OID = "1.3.6.1.2.1.4.22.1.1"
ARP_NETADDR_OID = "1.3.6.1.2.1.4.22.1.3"

TEAM_RE = re.compile(r"team\s*0*(\d+)\s*[-_]\s*0*(\d+)", re.IGNORECASE)


def snmpwalk(host, community, oid, timeout=15):
    cmd = ["snmpwalk", "-v2c", "-c", community, "-O", "n", "-t", str(timeout), host, oid]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
        return result.stdout
    except Exception as exc:
        print(f"[WARN] snmpwalk {host} {oid}: {exc}", file=sys.stderr)
        return ""


def parse_ifalias(output):
    """Parse snmpwalk ifAlias output -> {ifIndex: {'team': N, 'seat': M}}"""
    mapping = {}
    for line in output.strip().split("\n"):
        if "=" not in line:
            continue
        oid_str, value = line.split("=", 1)
        value = value.strip()
        if value.startswith("STRING:"):
            value = value[7:].strip().strip('"')
        elif ":" in value:
            value = value.split(":", 1)[1].strip().strip('"')
        else:
            continue

        m = TEAM_RE.search(value)
        if not m:
            continue

        parts = oid_str.strip().strip(".").split(".")
        try:
            ifindex = int(parts[-1])
        except (ValueError, IndexError):
            continue

        mapping[ifindex] = {"team": int(m.group(1)), "seat": int(m.group(2))}
    return mapping


def parse_arp_ifindex(output):
    """Parse snmpwalk ipNetToMediaIfIndex -> {(ifIndex, ip): True}"""
    entries = {}
    for line in output.strip().split("\n"):
        if "=" not in line:
            continue
        oid_str, value = line.split("=", 1)
        parts = oid_str.strip().strip(".").split(".")
        if len(parts) < 15:
            continue
        try:
            ifindex = int(parts[10])
            ip = ".".join(parts[11:15])
            IPv4Address(ip)
        except (ValueError, IndexError):
            continue
        entries[(ifindex, ip)] = True
    return entries


def load_subnets(env_var):
    raw = os.environ.get(env_var, "")
    if not raw:
        return []
    nets = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            nets.append(IPv4Network(item, strict=False))
        except ValueError:
            print(f"[WARN] invalid subnet: {item}", file=sys.stderr)
    return nets


def ip_in_subnets(ip_str, subnets):
    if not subnets:
        return True
    try:
        addr = IPv4Address(ip_str)
    except ValueError:
        return False
    return any(addr in net for net in subnets)


def main():
    switches_raw = os.environ.get("TOURNAMENT_SWITCHES", "")
    community = os.environ.get("SNMP_COMMUNITY", "global")
    wired_nets = load_subnets("PLAYER_SUBNETS")
    wireless_nets = load_subnets("WIRELESS_SUBNETS")
    output_file = os.environ.get("PLAYER_TARGETS_FILE", "/etc/prometheus/player_targets.json")

    if not switches_raw:
        print("[INFO] TOURNAMENT_SWITCHES not set, generating empty targets", file=sys.stderr)
        with open(output_file, "w") as f:
            json.dump([], f)
        return 0

    switches = [s.strip() for s in switches_raw.split(",") if s.strip()]
    all_targets = []

    for sw_ip in switches:
        print(f"[INFO] querying switch {sw_ip}", file=sys.stderr)

        alias_out = snmpwalk(sw_ip, community, IF_ALIAS_OID)
        if not alias_out:
            print(f"[WARN] no ifAlias response from {sw_ip}", file=sys.stderr)
            continue

        ifalias_map = parse_ifalias(alias_out)
        if not ifalias_map:
            print(f"[WARN] no team descriptions found on {sw_ip}", file=sys.stderr)
            continue

        arp_out = snmpwalk(sw_ip, community, ARP_IFINDEX_OID)
        if not arp_out:
            print(f"[WARN] no ARP response from {sw_ip}, trying netAddress", file=sys.stderr)
            arp_out = snmpwalk(sw_ip, community, ARP_NETADDR_OID)

        arp_entries = parse_arp_ifindex(arp_out)

        for (ifindex, ip), _ in arp_entries.items():
            if ifindex not in ifalias_map:
                continue

            team_info = ifalias_map[ifindex]

            network_type = "wired"
            if ip_in_subnets(ip, wireless_nets):
                network_type = "wireless"
            elif not ip_in_subnets(ip, wired_nets):
                continue

            all_targets.append({
                "targets": [ip],
                "labels": {
                    "team": str(team_info["team"]),
                    "seat": str(team_info["seat"]),
                    "switch": sw_ip,
                    "network": network_type,
                    "role": "player",
                },
            })

    all_targets.sort(key=lambda t: (
        int(t["labels"]["team"]),
        int(t["labels"]["seat"]),
        t["labels"]["network"],
    ))

    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    with open(output_file, "w") as f:
        json.dump(all_targets, f, indent=2)

    print(f"[INFO] generated {len(all_targets)} player targets -> {output_file}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
