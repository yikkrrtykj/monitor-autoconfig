#!/usr/bin/env python3
"""
Query stage switches via SNMP to map IP addresses to Team/Seat labels,
then generate a Prometheus file_sd JSON file for blackbox-exporter ICMP targets.

Usage (inside container):
  python3 generate-player-targets.py

Environment variables:
  TOURNAMENT_SWITCHES    comma-separated stage switch IPs (e.g. 192.168.10.11,192.168.10.12)
  SNMP_COMMUNITY         SNMP v2c community string for stage switches
  PLAYER_GATEWAYS        comma-separated L3 gateway IPs whose ARP table maps
                         player IPs to MACs. Required when stage switches are
                         pure L2. Falls back to LIBRENMS_CORE_IP if unset.
  PLAYER_GATEWAY_SNMP_COMMUNITY
                         SNMP community for gateways (default: same as SNMP_COMMUNITY)
  PLAYER_VLAN_IDS        comma-separated player VLAN IDs (e.g. 11,12). When set,
                         BRIDGE-MIB tables are also queried via Cisco-style
                         community-indexing (community@vlan_id) so per-VLAN MAC
                         tables become visible on Cisco IOS / IOS-XE switches
                         where the default context only exposes VLAN 1.
  PLAYER_REQUIRE_LINK_UP true/false (default true). Skip team ports whose
                         ifOperStatus is not "up". Prevents phantom targets
                         from stale MAC/ARP cache entries on disconnected ports.
  PLAYER_SUBNETS         comma-separated wired subnets (classification hint only,
                         no longer filters; team labels on ports are authoritative)
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
  PLAYER_WIRELESS_SCAN_EXCLUDE
                         comma-separated IPs/ranges to exclude from wireless
                         scan (e.g. 192.168.12.220-254)
  PLAYER_WIRELESS_SCAN_EXCLUDE_GATEWAYS
                         true/false, skip first and last host in each
                         wireless subnet (default: true)
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import re
import subprocess
import sys
from ipaddress import IPv4Address, IPv4Network

IF_ALIAS_OID = "1.3.6.1.2.1.31.1.1.1.18"
IF_OPER_STATUS_OID = "1.3.6.1.2.1.2.2.1.8"
ARP_IFINDEX_OID = "1.3.6.1.2.1.4.22.1.1"
ARP_NETADDR_OID = "1.3.6.1.2.1.4.22.1.3"
ARP_PHYSADDR_OID = "1.3.6.1.2.1.4.22.1.2"
BRIDGE_MIB_FDB_PORT_OID = "1.3.6.1.2.1.17.4.3.1.2"
BRIDGE_MIB_BASEPORT_OID = "1.3.6.1.2.1.17.1.4.1.2"
Q_BRIDGE_MIB_FDB_PORT_OID = "1.3.6.1.2.1.17.7.1.2.2.1.2"
IF_OPER_STATUS_UP = 1

TEAM_RE = re.compile(r"team\s*0*(\d+)\s*[-_]\s*0*(\d+)", re.IGNORECASE)
STATIC_TEAM_RE = re.compile(r"(?:team\s*)?0*(\d+)\s*[-_]\s*0*(\d+)$", re.IGNORECASE)
VALID_NETWORKS = {"wired", "wireless"}
HEX_BYTE_RE = re.compile(r"[0-9a-fA-F]{1,2}")

TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}


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


def parse_if_oper_status(output):
    """ifOperStatus -> {ifIndex: status_int} (1 = up, 2 = down, ...).

    Accepts both 'INTEGER: 1' and 'INTEGER: up(1)' forms.
    """
    NAMED = {"up": 1, "down": 2, "testing": 3, "unknown": 4,
             "dormant": 5, "notpresent": 6, "lowerlayerdown": 7}
    out = {}
    for line in output.strip().split("\n"):
        if "=" not in line:
            continue
        oid_str, value = line.split("=", 1)
        parts = oid_str.strip().strip(".").split(".")
        try:
            ifindex = int(parts[-1])
        except (ValueError, IndexError):
            continue
        text = value.strip()
        if ":" in text:
            text = text.rsplit(":", 1)[1].strip()
        m = re.search(r"\d+", text)
        if m:
            out[ifindex] = int(m.group(0))
            continue
        name = text.lower().split("(", 1)[0].strip()
        if name in NAMED:
            out[ifindex] = NAMED[name]
    return out


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


def normalize_mac(raw):
    """Common SNMP MAC encodings -> '00:1a:2b:3c:4d:5e' or None.

    Accepts 'Hex-STRING: 00 1a 2b 3c 4d 5e', 'STRING: 0:1a:...', or any
    string with 6 hex byte tokens separated by spaces/colons/dashes/dots.
    """
    if raw is None:
        return None
    s = str(raw).strip().strip('"')
    if ":" in s:
        head, _, tail = s.partition(":")
        if head.strip().lower() in ("hex-string", "string"):
            s = tail.strip()
    tokens = HEX_BYTE_RE.findall(s)
    if len(tokens) != 6:
        return None
    return ":".join(t.lower().zfill(2) for t in tokens)


def mac_from_decimal_suffix(parts):
    """Trailing 6 decimal OID parts -> canonical MAC, else None."""
    if len(parts) < 6:
        return None
    try:
        octets = [int(p) for p in parts[-6:]]
    except ValueError:
        return None
    if any(o < 0 or o > 255 for o in octets):
        return None
    return ":".join(f"{o:02x}" for o in octets)


def _int_from_snmp_value(value):
    """Extract trailing integer from 'INTEGER: 42', 'Gauge32: 5', '42'."""
    if value is None:
        return None
    text = value.strip()
    if ":" in text:
        text = text.rsplit(":", 1)[1].strip()
    try:
        return int(text)
    except ValueError:
        return None


def parse_dot1d_fdb(output):
    """dot1dTpFdbPort -> {mac: bridgePort}.

    OID layout: <prefix>.<6 decimal mac octets> = INTEGER: bridgePort
    """
    out = {}
    for line in output.strip().split("\n"):
        if "=" not in line:
            continue
        oid_str, value = line.split("=", 1)
        parts = oid_str.strip().strip(".").split(".")
        mac = mac_from_decimal_suffix(parts)
        if mac is None:
            continue
        port = _int_from_snmp_value(value)
        if port is None or port <= 0:
            continue
        out[mac] = port
    return out


def parse_dot1d_baseport(output):
    """dot1dBasePortIfIndex -> {bridgePort: ifIndex}."""
    out = {}
    for line in output.strip().split("\n"):
        if "=" not in line:
            continue
        oid_str, value = line.split("=", 1)
        parts = oid_str.strip().strip(".").split(".")
        try:
            bridge_port = int(parts[-1])
        except (ValueError, IndexError):
            continue
        ifindex = _int_from_snmp_value(value)
        if ifindex is None:
            continue
        out[bridge_port] = ifindex
    return out


def parse_dot1q_fdb(output):
    """dot1qTpFdbPort -> {mac: bridgePort}; VLAN dimension dropped.

    OID layout: <prefix>.<vlan>.<6 decimal mac octets> = INTEGER: bridgePort
    """
    out = {}
    for line in output.strip().split("\n"):
        if "=" not in line:
            continue
        oid_str, value = line.split("=", 1)
        parts = oid_str.strip().strip(".").split(".")
        if len(parts) < 7:
            continue
        mac = mac_from_decimal_suffix(parts)
        if mac is None:
            continue
        port = _int_from_snmp_value(value)
        if port is None or port <= 0:
            continue
        out[mac] = port
    return out


def parse_arp_macaddr(output):
    """ipNetToMediaPhysAddress -> {ip: mac}.

    OID layout: <prefix>.<ifIndex>.<ip octets> = Hex-STRING: aa bb cc dd ee ff
    """
    out = {}
    for line in output.strip().split("\n"):
        if "=" not in line:
            continue
        oid_str, value = line.split("=", 1)
        parts = oid_str.strip().strip(".").split(".")
        if len(parts) < 15:
            continue
        try:
            ip = ".".join(parts[11:15])
            IPv4Address(ip)
        except (ValueError, IndexError):
            continue
        mac = normalize_mac(value)
        if mac is None:
            continue
        out[ip] = mac
    return out


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
    value = raw.strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    return default


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


def expand_ip_range(item):
    start_raw, end_raw = [part.strip() for part in item.split("-", 1)]
    start = IPv4Address(start_raw)
    if re.fullmatch(r"\d{1,3}", end_raw):
        end_octet = int(end_raw)
        if end_octet > 255:
            raise ValueError("range end octet out of bounds")
        prefix = start_raw.rsplit(".", 1)[0]
        end = IPv4Address(f"{prefix}.{end_octet}")
    else:
        end = IPv4Address(end_raw)

    if int(end) < int(start):
        raise ValueError("range end before start")

    size = int(end) - int(start) + 1
    if size > 4096:
        raise ValueError("range too large")

    return {str(IPv4Address(int(start) + offset)) for offset in range(size)}


def parse_excluded_ip_item(item):
    if "-" in item:
        return expand_ip_range(item)
    return {str(IPv4Address(item))}


def load_excluded_ips(env_var):
    raw = os.environ.get(env_var, "")
    excluded = set()
    if not raw:
        return excluded
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            excluded.update(parse_excluded_ip_item(item))
        except ValueError:
            print(f"[WARN] invalid excluded IP/range: {item}", file=sys.stderr)
    return excluded


def gateway_like_ips(subnets):
    excluded = set()
    for net in subnets:
        hosts = list(net.hosts())
        if len(hosts) > 2:
            excluded.add(str(hosts[0]))
            excluded.add(str(hosts[-1]))
    return excluded


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


def discover_wireless_scan_ips(subnets, limit=0, timeout=1, workers=64, max_hosts=512, excluded_ips=None):
    if not subnets:
        return []

    excluded_ips = excluded_ips or set()
    candidates = []
    for net in subnets:
        hosts = [ip for ip in net.hosts() if str(ip) not in excluded_ips]
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


def _walk_vlan_mac_table(sw, community, vlan_context=False):
    """Walk one SNMP context for MAC->ifIndex.

    Returns (mac_to_ifindex, source_label, bridgeport_count).

    Tries BRIDGE-MIB (dot1dTpFdbPort + dot1dBasePortIfIndex) first since on
    Cisco the per-VLAN context exposes it directly. When `vlan_context` is
    False (the default-context call), falls back to Q-BRIDGE-MIB for vendors
    that only expose dot1qTpFdbPort. When `vlan_context` is True (community
    is `name@vlan_id`), the fallback is skipped: Cisco's community-indexing
    only applies to BRIDGE-MIB, and a Q-BRIDGE walk under an indexed
    community can return unindexed VLAN 1 data and pollute the merged map.
    """
    bp_out = snmpwalk(sw, community, BRIDGE_MIB_BASEPORT_OID)
    bp_map = parse_dot1d_baseport(bp_out)

    fdb_out = snmpwalk(sw, community, BRIDGE_MIB_FDB_PORT_OID)
    fdb_map = parse_dot1d_fdb(fdb_out)
    source = "BRIDGE-MIB"
    if not fdb_map and not vlan_context:
        fdb_out = snmpwalk(sw, community, Q_BRIDGE_MIB_FDB_PORT_OID)
        fdb_map = parse_dot1q_fdb(fdb_out)
        source = "Q-BRIDGE-MIB"

    mac_to_ifindex = {mac: bp_map.get(bp, bp) for mac, bp in fdb_map.items()}
    return mac_to_ifindex, source, len(bp_map)


def build_stage_mac_index(switches, community, vlan_ids=None):
    """Return {sw_ip: {'ifalias': {ifIndex: {team, seat}},
                       'mac_to_ifindex': {mac: ifIndex},
                       'oper_status': {ifIndex: status_int}}}.

    Queries the default SNMP context first. If vlan_ids is set, also queries
    each VLAN via Cisco's community-indexing (community@vlan_id) so the
    per-VLAN BRIDGE-MIB tables (e.g. on Cisco IOS, where the default context
    only exposes VLAN 1) become visible. Results are merged across contexts.
    ifOperStatus is queried once on the default context and used downstream
    to filter stale MAC/ARP entries on currently-disconnected ports.
    """
    vlan_ids = vlan_ids or []
    index = {}
    for sw in switches:
        alias_out = snmpwalk(sw, community, IF_ALIAS_OID)
        ifalias_map = parse_ifalias(alias_out)
        print(
            f"[INFO] {sw}: ifAlias entries with team label = {len(ifalias_map)}",
            file=sys.stderr,
        )

        oper_out = snmpwalk(sw, community, IF_OPER_STATUS_OID)
        oper_map = parse_if_oper_status(oper_out)
        team_ports_up = sum(
            1 for ifx in ifalias_map if oper_map.get(ifx) == IF_OPER_STATUS_UP
        )
        print(
            f"[INFO] {sw}: team ports with link up = {team_ports_up}/{len(ifalias_map)}",
            file=sys.stderr,
        )

        default_macs, default_source, default_bp = _walk_vlan_mac_table(sw, community)
        print(
            f"[INFO] {sw}: bridgePort->ifIndex entries = {default_bp}",
            file=sys.stderr,
        )
        print(
            f"[INFO] {sw}: default-context MAC entries ({default_source}) = {len(default_macs)}",
            file=sys.stderr,
        )

        mac_to_ifindex = dict(default_macs)
        for vlan_id in vlan_ids:
            indexed_community = f"{community}@{vlan_id}"
            vlan_macs, vlan_source, vlan_bp = _walk_vlan_mac_table(
                sw, indexed_community, vlan_context=True
            )
            print(
                f"[INFO] {sw}: VLAN {vlan_id} MAC entries ({vlan_source}) = {len(vlan_macs)} "
                f"(bridgePort->ifIndex = {vlan_bp})",
                file=sys.stderr,
            )
            for mac, ifx in vlan_macs.items():
                mac_to_ifindex.setdefault(mac, ifx)

        print(
            f"[INFO] {sw}: combined MAC table entries = {len(mac_to_ifindex)}",
            file=sys.stderr,
        )
        index[sw] = {
            "ifalias": ifalias_map,
            "mac_to_ifindex": mac_to_ifindex,
            "oper_status": oper_map,
        }
    return index


def join_gateway_arp_to_teams(gateway_arp, stage_index, wireless_nets, require_link_up=True):
    """For each (ip, mac) from gateway ARP, locate the stage switch whose MAC
    table contains that MAC and emit a player target. The team label on the
    matching port is authoritative; PLAYER_SUBNETS is intentionally not used
    to filter here. When require_link_up is True (default), stale MAC/ARP
    entries on currently-disconnected team ports are skipped via ifOperStatus.
    Returns (targets, stats).
    """
    targets = []
    matched = 0
    unmatched_macs = 0
    skipped_link_down = 0

    for ip, mac in gateway_arp.items():
        hit = None
        link_down = False
        for sw, data in stage_index.items():
            ifx = data["mac_to_ifindex"].get(mac)
            if ifx is None:
                continue
            team_info = data["ifalias"].get(ifx)
            if team_info is None:
                continue
            if require_link_up:
                oper = data.get("oper_status", {}).get(ifx)
                if oper is not None and oper != IF_OPER_STATUS_UP:
                    link_down = True
                    break
            hit = (sw, ifx, team_info)
            break

        if link_down:
            skipped_link_down += 1
            continue
        if hit is None:
            unmatched_macs += 1
            continue

        sw, _, team_info = hit
        if wireless_nets and ip_in_subnets(ip, wireless_nets):
            network_type = "wireless"
        else:
            network_type = "wired"

        targets.append({
            "targets": [ip],
            "labels": {
                "team": str(team_info["team"]),
                "seat": str(team_info["seat"]),
                "switch": sw,
                "network": network_type,
                "role": "player",
            },
        })
        matched += 1

    return targets, {
        "matched": matched,
        "unmatched_macs": unmatched_macs,
        "skipped_link_down": skipped_link_down,
    }


def collect_direct_arp_targets(switches, community, stage_index, wireless_nets, require_link_up=True):
    """Path A: query each stage switch's own ARP table (only works when the
    stage has an L3 SVI on the player VLAN). Empty on pure-L2 deployments.
    Skips team ports that are currently link-down when require_link_up is set.
    """
    targets = []
    for sw in switches:
        data = stage_index.get(sw, {})
        ifalias_map = data.get("ifalias", {})
        oper_map = data.get("oper_status", {})
        if not ifalias_map:
            continue
        arp_out = snmpwalk(sw, community, ARP_IFINDEX_OID)
        if not arp_out:
            print(
                f"[WARN] no ARP response from {sw}, trying netAddress",
                file=sys.stderr,
            )
            arp_out = snmpwalk(sw, community, ARP_NETADDR_OID)

        for (ifindex, ip), _ in parse_arp_ifindex(arp_out).items():
            team_info = ifalias_map.get(ifindex)
            if team_info is None:
                continue
            if require_link_up:
                oper = oper_map.get(ifindex)
                if oper is not None and oper != IF_OPER_STATUS_UP:
                    continue
            if wireless_nets and ip_in_subnets(ip, wireless_nets):
                network_type = "wireless"
            else:
                network_type = "wired"
            targets.append({
                "targets": [ip],
                "labels": {
                    "team": str(team_info["team"]),
                    "seat": str(team_info["seat"]),
                    "switch": sw,
                    "network": network_type,
                    "role": "player",
                },
            })
    return targets


def merge_dedup_targets(path_b_targets, path_a_targets):
    """Deduplicate by (team, seat, ip). Path B wins on conflict because the
    gateway-ARP + MAC-table join uses real bridging data."""
    seen = set()
    merged = []
    for target in list(path_b_targets) + list(path_a_targets):
        key = (target["labels"]["team"], target["labels"]["seat"], target["targets"][0])
        if key in seen:
            continue
        seen.add(key)
        merged.append(target)
    return merged


def verify_targets_alive(targets, timeout=1, workers=64):
    """Drop targets whose IP doesn't respond to ICMP within `timeout` seconds.

    Filters stale entries left by switch-MAC aging (~5 min) and gateway-ARP
    aging (~4 hours) -- both can keep a long-gone device in the join until
    the table entry finally ages out.
    """
    candidate_ips = sorted({t["targets"][0] for t in targets if t.get("targets")})
    if not candidate_ips:
        return targets

    alive = set()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {executor.submit(ping_host, ip, timeout): ip for ip in candidate_ips}
        for future in as_completed(futures):
            ip = futures[future]
            try:
                if future.result():
                    alive.add(ip)
            except Exception:
                pass

    kept = [t for t in targets if t["targets"][0] in alive]
    dropped = len(targets) - len(kept)
    print(
        f"[INFO] active ping verify: {len(alive)}/{len(candidate_ips)} IPs alive, "
        f"dropped {dropped} stale target(s)",
        file=sys.stderr,
    )
    return kept


def main():
    switches_raw = os.environ.get("TOURNAMENT_SWITCHES", "")
    community = os.environ.get("SNMP_COMMUNITY", "global")
    gateways_raw = os.environ.get("PLAYER_GATEWAYS", "").strip()
    if not gateways_raw:
        gateways_raw = os.environ.get("LIBRENMS_CORE_IP", "").strip()
    gateways = [g.strip() for g in gateways_raw.split(",") if g.strip()]
    gateway_community = os.environ.get("PLAYER_GATEWAY_SNMP_COMMUNITY", "").strip() or community
    vlan_ids_raw = os.environ.get("PLAYER_VLAN_IDS", "").strip()
    player_vlan_ids = []
    for item in vlan_ids_raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            player_vlan_ids.append(int(item))
        except ValueError:
            print(f"[WARN] invalid PLAYER_VLAN_IDS entry: {item}", file=sys.stderr)
    wired_nets = load_subnets("PLAYER_SUBNETS")
    wireless_nets = load_subnets("WIRELESS_SUBNETS")
    require_link_up = env_bool("PLAYER_REQUIRE_LINK_UP", default=True)
    static_targets_raw = os.environ.get("PLAYER_STATIC_TARGETS", "")
    static_default_network = os.environ.get("PLAYER_STATIC_NETWORK", "wireless")
    wireless_scan_enabled = env_bool("PLAYER_WIRELESS_SCAN", default=True) or env_bool("PLAYER_WIRELESS_PREVIEW")
    wireless_scan_limit = env_int_alias("PLAYER_WIRELESS_SCAN_LIMIT", "PLAYER_WIRELESS_PREVIEW_LIMIT", 0, minimum=0, maximum=4096)
    wireless_scan_team_size = env_int_alias("PLAYER_WIRELESS_SCAN_TEAM_SIZE", "PLAYER_WIRELESS_PREVIEW_TEAM_SIZE", 5, minimum=1, maximum=50)
    wireless_scan_timeout = env_int_alias("PLAYER_WIRELESS_SCAN_TIMEOUT", "PLAYER_WIRELESS_PREVIEW_TIMEOUT", 1, minimum=1, maximum=5)
    wireless_scan_workers = env_int_alias("PLAYER_WIRELESS_SCAN_WORKERS", "PLAYER_WIRELESS_PREVIEW_WORKERS", 64, minimum=1, maximum=256)
    wireless_scan_max_hosts = env_int_alias("PLAYER_WIRELESS_SCAN_MAX_HOSTS", "PLAYER_WIRELESS_PREVIEW_MAX_HOSTS", 512, minimum=1, maximum=4096)
    wireless_scan_exclude = load_excluded_ips("PLAYER_WIRELESS_SCAN_EXCLUDE")
    if env_bool("PLAYER_WIRELESS_SCAN_EXCLUDE_GATEWAYS", default=True):
        wireless_scan_exclude.update(gateway_like_ips(wireless_nets))
    output_file = os.environ.get("PLAYER_TARGETS_FILE", "/etc/prometheus/player_targets.json")

    all_targets = []

    if wireless_scan_enabled:
        scan_ips = discover_wireless_scan_ips(
            wireless_nets,
            limit=wireless_scan_limit,
            timeout=wireless_scan_timeout,
            workers=wireless_scan_workers,
            max_hosts=wireless_scan_max_hosts,
            excluded_ips=wireless_scan_exclude,
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

        stage_index = build_stage_mac_index(switches, community, player_vlan_ids)

        path_a_targets = collect_direct_arp_targets(
            switches, community, stage_index, wireless_nets, require_link_up
        )
        print(
            f"[INFO] direct-ARP-on-stage produced {len(path_a_targets)} targets",
            file=sys.stderr,
        )

        path_b_targets = []
        if gateways:
            gateway_arp = {}
            for gw in gateways:
                arp_out = snmpwalk(gw, gateway_community, ARP_PHYSADDR_OID)
                entries = parse_arp_macaddr(arp_out)
                print(
                    f"[INFO] gateway {gw}: ARP entries = {len(entries)}",
                    file=sys.stderr,
                )
                for ip, mac in entries.items():
                    # First gateway wins on conflict; for HA pairs list the
                    # active one first so stale entries on the standby don't
                    # mask live MACs.
                    gateway_arp.setdefault(ip, mac)
            path_b_targets, stats = join_gateway_arp_to_teams(
                gateway_arp, stage_index, wireless_nets, require_link_up
            )
            print(
                f"[INFO] gateway-ARP join: matched {stats['matched']} IPs, "
                f"{stats['unmatched_macs']} MACs had no stage port, "
                f"{stats['skipped_link_down']} skipped (link down)",
                file=sys.stderr,
            )
        else:
            print(
                "[INFO] PLAYER_GATEWAYS / LIBRENMS_CORE_IP not set; skipping gateway-ARP path",
                file=sys.stderr,
            )

        merged = merge_dedup_targets(path_b_targets, path_a_targets)

        per_team = {}
        for target in merged:
            key = (target["labels"]["team"], target["labels"]["network"])
            per_team[key] = per_team.get(key, 0) + 1
        for (team, net), count in sorted(per_team.items(), key=lambda kv: (int(kv[0][0]), kv[0][1])):
            print(f"[INFO] team {team} {net}: {count} target(s)", file=sys.stderr)

        all_targets.extend(merged)

    if env_bool("PLAYER_VERIFY_PING", default=True) and all_targets:
        all_targets = verify_targets_alive(all_targets)

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
