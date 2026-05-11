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
  PLAYER_STATIC_TARGETS  comma-separated manual targets for WiFi-only events
                         (e.g. 1-1=192.168.12.101,2-5=192.168.12.205)
  PLAYER_STATIC_NETWORK  default network label for manual targets (default: wireless)
  PLAYER_WIRELESS_SCAN
                         true/false, ping-scan WIRELESS_SUBNETS and create
                         synthetic network=wireless player targets
  PLAYER_WIRELESS_SCAN_LIMIT
                         max wireless scan targets to keep; 0 means unlimited
                         (default: 0)
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
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
STATIC_TEAM_RE = re.compile(r"(?:team\s*)?0*(\d+)\s*[-_]\s*0*(\d+)$", re.IGNORECASE)
VALID_NETWORKS = {"wired", "wireless"}

TRUE_VALUES = {"1", "true", "yes", "on"}


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
    """True if ip is in any of the given subnets. Empty list -> False (no match)."""
    if not subnets:
        return False
    try:
        addr = IPv4Address(ip_str)
    except ValueError:
        return False
    return any(addr in net for net in subnets)


def env_bool(name, default=False):
    raw = os.environ.get(name, "")
    if not raw:
        return default
    return raw.strip().lower() in TRUE_VALUES


def env_int(name, default, minimum=None, maximum=None):
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        print(f"[WARN] invalid integer {name}: {raw}, using {default}", file=sys.stderr)
        return default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def env_int_alias(primary, legacy, default, minimum=None, maximum=None):
    if os.environ.get(primary, ""):
        return env_int(primary, default, minimum=minimum, maximum=maximum)
    return env_int(legacy, default, minimum=minimum, maximum=maximum)


def infer_network_type(ip, wired_nets, wireless_nets, default_network):
    if wireless_nets and ip_in_subnets(ip, wireless_nets):
        return "wireless"
    if wired_nets and ip_in_subnets(ip, wired_nets):
        return "wired"
    return default_network


def parse_static_player_targets(raw, wired_nets, wireless_nets, default_network="wireless"):
    """Parse manual targets: team-seat=ip[:network] or team-seat@ip[:network]."""
    targets = []
    if not raw:
        return targets

    default_network = (default_network or "wireless").strip().lower()
    if default_network not in VALID_NETWORKS:
        print(f"[WARN] invalid PLAYER_STATIC_NETWORK: {default_network}, using wireless", file=sys.stderr)
        default_network = "wireless"

    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue

        if "=" in item:
            label, value = item.split("=", 1)
        elif "@" in item:
            label, value = item.split("@", 1)
        else:
            print(f"[WARN] invalid static player target (expected team-seat=ip): {item}", file=sys.stderr)
            continue

        m = STATIC_TEAM_RE.search(label.strip())
        if not m:
            print(f"[WARN] invalid static player label: {label}", file=sys.stderr)
            continue

        value_parts = [part.strip() for part in value.split(":", 1)]
        ip = value_parts[0]
        network_type = value_parts[1].lower() if len(value_parts) > 1 and value_parts[1] else ""

        try:
            IPv4Address(ip)
        except ValueError:
            print(f"[WARN] invalid static player IP: {ip}", file=sys.stderr)
            continue

        if network_type:
            if network_type not in VALID_NETWORKS:
                print(f"[WARN] invalid static player network: {network_type}", file=sys.stderr)
                continue
        else:
            network_type = infer_network_type(ip, wired_nets, wireless_nets, default_network)

        targets.append({
            "targets": [ip],
            "labels": {
                "team": str(int(m.group(1))),
                "seat": str(int(m.group(2))),
                "switch": "static",
                "network": network_type,
                "role": "player",
            },
        })

    return targets


def ping_host(ip, timeout=1):
    cmd = ["ping", "-c", "1", "-W", str(timeout), ip]
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout + 1,
        )
        return result.returncode == 0
    except Exception:
        return False


def limited_items(items, limit=0):
    if limit and limit > 0:
        return items[:limit]
    return items


def build_wireless_scan_targets(ips, limit=0, team_size=5):
    targets = []
    unique_ips = sorted({str(IPv4Address(ip)) for ip in ips}, key=IPv4Address)
    for idx, ip in enumerate(limited_items(unique_ips, limit)):
        team = idx // team_size + 1
        seat = idx % team_size + 1
        targets.append({
            "targets": [ip],
            "labels": {
                "team": str(team),
                "seat": str(seat),
                "switch": "wireless-scan",
                "network": "wireless",
                "role": "player",
            },
        })
    return targets


def discover_wireless_scan_ips(subnets, limit=0, timeout=1, workers=64, max_hosts=512):
    if not subnets:
        return []

    candidates = []
    for net in subnets:
        hosts = list(net.hosts())
        if len(hosts) > max_hosts:
            print(
                f"[WARN] wireless scan subnet {net} has {len(hosts)} hosts; scanning first {max_hosts}",
                file=sys.stderr,
            )
            hosts = hosts[:max_hosts]
        candidates.extend(str(ip) for ip in hosts)

    if not candidates:
        return []

    online = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        future_to_ip = {executor.submit(ping_host, ip, timeout): ip for ip in candidates}
        for future in as_completed(future_to_ip):
            ip = future_to_ip[future]
            if future.result():
                online.append(ip)

    online = sorted(online, key=IPv4Address)
    if limit and limit > 0 and len(online) > limit:
        print(
            f"[INFO] wireless scan found {len(online)} live hosts, keeping first {limit}",
            file=sys.stderr,
        )
    else:
        print(f"[INFO] wireless scan found {len(online)} live hosts", file=sys.stderr)
    return limited_items(online, limit)


def main():
    switches_raw = os.environ.get("TOURNAMENT_SWITCHES", "")
    community = os.environ.get("SNMP_COMMUNITY", "global")
    wired_nets = load_subnets("PLAYER_SUBNETS")
    wireless_nets = load_subnets("WIRELESS_SUBNETS")
    static_targets_raw = os.environ.get("PLAYER_STATIC_TARGETS", "")
    static_default_network = os.environ.get("PLAYER_STATIC_NETWORK", "wireless")
    wireless_scan_enabled = env_bool("PLAYER_WIRELESS_SCAN", default=True) or env_bool("PLAYER_WIRELESS_PREVIEW")
    wireless_scan_limit = env_int_alias("PLAYER_WIRELESS_SCAN_LIMIT", "PLAYER_WIRELESS_PREVIEW_LIMIT", 0, minimum=0, maximum=4096)
    wireless_scan_team_size = env_int_alias("PLAYER_WIRELESS_SCAN_TEAM_SIZE", "PLAYER_WIRELESS_PREVIEW_TEAM_SIZE", 5, minimum=1, maximum=50)
    wireless_scan_timeout = env_int_alias("PLAYER_WIRELESS_SCAN_TIMEOUT", "PLAYER_WIRELESS_PREVIEW_TIMEOUT", 1, minimum=1, maximum=5)
    wireless_scan_workers = env_int_alias("PLAYER_WIRELESS_SCAN_WORKERS", "PLAYER_WIRELESS_PREVIEW_WORKERS", 64, minimum=1, maximum=256)
    wireless_scan_max_hosts = env_int_alias("PLAYER_WIRELESS_SCAN_MAX_HOSTS", "PLAYER_WIRELESS_PREVIEW_MAX_HOSTS", 512, minimum=1, maximum=4096)
    output_file = os.environ.get("PLAYER_TARGETS_FILE", "/etc/prometheus/player_targets.json")

    all_targets = []

    if wireless_scan_enabled:
        scan_ips = discover_wireless_scan_ips(
            wireless_nets,
            limit=wireless_scan_limit,
            timeout=wireless_scan_timeout,
            workers=wireless_scan_workers,
            max_hosts=wireless_scan_max_hosts,
        )
        scan_targets = build_wireless_scan_targets(
            scan_ips,
            limit=wireless_scan_limit,
            team_size=wireless_scan_team_size,
        )
        print(
            f"[INFO] wireless scan generated {len(scan_targets)} network=wireless targets from WIRELESS_SUBNETS",
            file=sys.stderr,
        )
        all_targets.extend(scan_targets)

    static_targets = parse_static_player_targets(
        static_targets_raw, wired_nets, wireless_nets, static_default_network
    )
    if static_targets:
        print(f"[INFO] loaded {len(static_targets)} static player targets", file=sys.stderr)
        all_targets.extend(static_targets)

    if not switches_raw:
        print("[INFO] TOURNAMENT_SWITCHES not set, skipping SNMP target discovery", file=sys.stderr)
    else:
        switches = [s.strip() for s in switches_raw.split(",") if s.strip()]

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

                # Default wired. Mark wireless only when WIRELESS_SUBNETS is set and ip matches.
                # If PLAYER_SUBNETS (wired) is set, drop ips outside it (likely non-player).
                if wireless_nets and ip_in_subnets(ip, wireless_nets):
                    network_type = "wireless"
                elif wired_nets and not ip_in_subnets(ip, wired_nets):
                    continue
                else:
                    network_type = "wired"

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
