#!/usr/bin/env python3
"""
Walk LLDP-MIB on every configured infrastructure device to build the real
network adjacency graph, then emit artifacts for downstream:

  edges.json          (consumed by the bigscreen /topology page)
  uplink-targets.json (legacy cleanup file, intentionally empty)
  rates.json          (legacy cleanup file, intentionally empty)

Env vars:
  TOPOLOGY_DEVICES           comma-separated device IPs to poll. Empty -> union of
                             CORE_SWITCH_PING + DIST_SWITCH_PING + FIREWALL_PING +
                             TOURNAMENT_SWITCHES + auto-discovered switches from
                             SWITCH_TARGETS_FILE (default /targets/switch_targets.json).
  TOPOLOGY_SNMP_COMMUNITY    SNMPv2c community (default: SNMP_COMMUNITY).
  TOPOLOGY_SNMP_TIMEOUT      per-request timeout seconds (default: 2).
  TOPOLOGY_SNMP_RETRIES      retries per request (default: 0).
  TOPOLOGY_POLL_WORKERS      devices polled concurrently (default: 4).
  TOPOLOGY_SNMP_DELAY_MS     pause after each SNMP request (default: 100).
  TOPOLOGY_OUTPUT_DIR        where to write edges.json / legacy empty files
                             (default: /etc/prometheus/targets/topology).
  SERVER_PING                named server targets; ARP/FDB resolves their real
                             access switch and port when SNMP exposes both.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import re
import subprocess
import sys
import time
from ipaddress import IPv4Address

from target_utils import expand_ipv4_entry, parse_named_ipv4_targets, write_json_atomic

SYS_NAME_OID = "1.3.6.1.2.1.1.5.0"
IF_NAME_OID = "1.3.6.1.2.1.31.1.1.1.1"
IF_OPER_STATUS_OID = "1.3.6.1.2.1.2.2.1.8"
LLDP_LOC_PORT_DESC_OID = "1.0.8802.1.1.2.1.3.7.1.3"
LLDP_REM_PORT_ID_OID = "1.0.8802.1.1.2.1.4.1.1.7"
LLDP_REM_PORT_DESC_OID = "1.0.8802.1.1.2.1.4.1.1.8"
LLDP_REM_SYS_NAME_OID = "1.0.8802.1.1.2.1.4.1.1.9"

# CISCO-CDP-MIB cdpCacheEntry (row index = cdpCacheIfIndex.cdpCacheDeviceIndex).
# Cisco gear that only runs CDP (not LLDP) is discovered through these.
CDP_CACHE_ADDRESS_OID = "1.3.6.1.4.1.9.9.23.1.2.1.1.4"
CDP_CACHE_DEVICE_ID_OID = "1.3.6.1.4.1.9.9.23.1.2.1.1.6"
CDP_CACHE_DEVICE_PORT_OID = "1.3.6.1.4.1.9.9.23.1.2.1.1.7"

# Server attachment discovery.  The L3 gateway supplies IP -> MAC/VLAN through
# its ARP table; exact FDB lookups on the switches then identify the access
# port without walking every switch's full MAC table.
IP_NET_TO_MEDIA_PHYS_ADDRESS_OID = "1.3.6.1.2.1.4.22.1.2"
DOT1Q_TP_FDB_PORT_OID = "1.3.6.1.2.1.17.7.1.2.2.1.2"
DOT1D_TP_FDB_PORT_OID = "1.3.6.1.2.1.17.4.3.1.2"
DOT1D_BASE_PORT_IFINDEX_OID = "1.3.6.1.2.1.17.1.4.1.2"


def _snmp_limits(timeout=None, retries=None):
    timeout = float(timeout if timeout is not None else os.environ.get("TOPOLOGY_SNMP_TIMEOUT", "2"))
    retries = int(retries if retries is not None else os.environ.get("TOPOLOGY_SNMP_RETRIES", "0"))
    return max(0.2, timeout), max(0, retries)


def _snmp_request_delay():
    try:
        delay_ms = float(os.environ.get("TOPOLOGY_SNMP_DELAY_MS", "100") or "100")
    except ValueError:
        delay_ms = 100
    if delay_ms > 0:
        time.sleep(min(delay_ms, 2000) / 1000)


def _topology_poll_workers():
    try:
        workers = int(os.environ.get("TOPOLOGY_POLL_WORKERS", "4") or "4")
    except ValueError:
        workers = 4
    return max(1, min(workers, 32))


def snmpwalk(host, community, oid, timeout=None, retries=None):
    timeout, retries = _snmp_limits(timeout, retries)
    cmd = [
        "snmpwalk", "-v2c", "-c", community, "-O", "n",
        "-t", str(timeout), "-r", str(retries), host, oid,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout * (retries + 1) + 2,
        )
        return result.stdout
    except Exception as exc:
        print(f"[WARN] snmpwalk {host} {oid}: {exc}", file=sys.stderr)
        return ""
    finally:
        _snmp_request_delay()


def snmpget(host, community, oid, timeout=None, retries=None):
    timeout, retries = _snmp_limits(timeout, retries)
    cmd = [
        "snmpget", "-v2c", "-c", community, "-O", "qv",
        "-t", str(timeout), "-r", str(retries), host, oid,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout * (retries + 1) + 2,
        )
        return result.stdout.strip().strip('"')
    except Exception as exc:
        print(f"[WARN] snmpget {host} {oid}: {exc}", file=sys.stderr)
        return ""
    finally:
        _snmp_request_delay()


def strip_string_value(value):
    text = value.strip()
    if ":" in text:
        prefix, _, rest = text.partition(":")
        if prefix.strip().lower() in ("string", "hex-string", "stringnamed", "octets"):
            text = rest.strip()
    return text.strip('"')


def parse_oid_value(line):
    if "=" not in line:
        return None, None
    oid_str, value = line.split("=", 1)
    parts = oid_str.strip().strip(".").split(".")
    return parts, value.strip()


def parse_ifname(output):
    """ifName walk -> {ifIndex: name}."""
    mapping = {}
    for line in output.strip().split("\n"):
        parts, value = parse_oid_value(line)
        if not parts:
            continue
        try:
            ifindex = int(parts[-1])
        except ValueError:
            continue
        text = strip_string_value(value)
        if text:
            mapping[ifindex] = text
    return mapping


def parse_if_oper_status(output):
    """ifOperStatus walk -> {ifIndex: status}; 1 means operationally up."""
    mapping = {}
    for line in output.strip().split("\n"):
        parts, value = parse_oid_value(line)
        if not parts:
            continue
        try:
            ifindex = int(parts[-1])
        except ValueError:
            continue
        parenthesized = re.search(r"\(([0-9]+)\)", value)
        numeric = re.search(r"(?:INTEGER:\s*)?([0-9]+)\s*$", value, re.IGNORECASE)
        match = parenthesized or numeric
        if match:
            mapping[ifindex] = int(match.group(1))
    return mapping


def parse_lldp_loc_port_desc(output):
    """lldpLocPortDesc walk -> {locPortNum: port description}."""
    mapping = {}
    for line in output.strip().split("\n"):
        parts, value = parse_oid_value(line)
        if not parts:
            continue
        try:
            loc_port = int(parts[-1])
        except ValueError:
            continue
        text = strip_string_value(value)
        if text:
            mapping[loc_port] = text
    return mapping


def parse_lldp_rem_field(output):
    """Generic walk parser for lldpRem* tables.

    The OID suffix for each row is (timeMark, lldpRemLocalPortNum, lldpRemIndex).
    Returns {(timeMark, locPort, remIdx): value_str}.
    """
    entries = {}
    for line in output.strip().split("\n"):
        parts, value = parse_oid_value(line)
        if not parts or len(parts) < 3:
            continue
        try:
            time_mark = int(parts[-3])
            loc_port = int(parts[-2])
            rem_idx = int(parts[-1])
        except ValueError:
            continue
        text = strip_string_value(value)
        if text:
            entries[(time_mark, loc_port, rem_idx)] = text
    return entries


def hexstr_to_ipv4(text):
    """CDP cdpCacheAddress hex ('C0 A8 0A 17') -> '192.168.10.23', else None."""
    tokens = re.findall(r"[0-9a-fA-F]{1,2}", str(text).strip())
    if len(tokens) != 4:
        return None
    try:
        octets = [int(token, 16) for token in tokens]
    except ValueError:
        return None
    if any(octet < 0 or octet > 255 for octet in octets):
        return None
    ip = ".".join(str(octet) for octet in octets)
    try:
        IPv4Address(ip)
    except ValueError:
        return None
    return ip


def parse_cdp_field(output):
    """Generic CDP cache walk -> {(cdpCacheIfIndex, cdpCacheDeviceIndex): value}."""
    entries = {}
    for line in output.strip().split("\n"):
        parts, value = parse_oid_value(line)
        if not parts or len(parts) < 2:
            continue
        try:
            if_index = int(parts[-2])
            dev_index = int(parts[-1])
        except ValueError:
            continue
        text = strip_string_value(value)
        if text:
            entries[(if_index, dev_index)] = text
    return entries


def parse_cdp_address(output):
    """cdpCacheAddress walk -> {(cdpCacheIfIndex, cdpCacheDeviceIndex): neighbor_ip}."""
    out = {}
    for line in output.strip().split("\n"):
        parts, value = parse_oid_value(line)
        if not parts or len(parts) < 2:
            continue
        try:
            if_index = int(parts[-2])
            dev_index = int(parts[-1])
        except ValueError:
            continue
        ip = hexstr_to_ipv4(strip_string_value(value))
        if ip:
            out[(if_index, dev_index)] = ip
    return out


def normalize_mac(value):
    """Return a lower-case colon-separated MAC, or None for invalid values."""
    text = strip_string_value(str(value or ""))
    tokens = re.findall(r"(?i)(?<![0-9a-f])[0-9a-f]{2}(?![0-9a-f])", text)
    if len(tokens) != 6:
        compact = re.sub(r"[^0-9a-fA-F]", "", text)
        if len(compact) != 12:
            return None
        tokens = [compact[index:index + 2] for index in range(0, 12, 2)]
    return ":".join(token.lower() for token in tokens)


def parse_arp_table(output, ifname_map):
    """ipNetToMediaPhysAddress walk -> {IPv4: {mac, ifindex, vlan}}."""
    records = {}
    for line in output.strip().split("\n"):
        parts, value = parse_oid_value(line)
        if not parts or len(parts) < 5:
            continue
        try:
            ifindex = int(parts[-5])
            ip = str(IPv4Address(".".join(parts[-4:])))
        except ValueError:
            continue
        mac = normalize_mac(value)
        if not mac:
            continue
        ifname = str(ifname_map.get(ifindex) or "")
        vlan_match = re.search(r"(?i)\b(?:vlanif|vlan|vl)[\s_-]*([0-9]+)\b", ifname)
        records[ip] = {
            "mac": mac,
            "ifindex": ifindex,
            "vlan": int(vlan_match.group(1)) if vlan_match else None,
        }
    return records


def _positive_int(value):
    match = re.search(r"\b([0-9]+)\b", str(value or ""))
    if not match:
        return None
    number = int(match.group(1))
    return number if number > 0 else None


def mac_oid_suffix(mac):
    normalized = normalize_mac(mac)
    if not normalized:
        return ""
    return ".".join(str(int(token, 16)) for token in normalized.split(":"))


def normalize_hostname(name):
    if not name:
        return ""
    stripped = name.strip().lower()
    base = stripped.split(".", 1)[0]
    return base


def normalize_port_name(name):
    """Reduce vendor-specific interface names to a comparable key.

    "GigabitEthernet1/0/19" -> "1/0/19"
    "Gi1/0/19"              -> "1/0/19"
    "Port-channel1"         -> "agg1"
    "Po1"                   -> "agg1"
    Anything without a known shape returns the trimmed lowercase original.
    """
    if not name:
        return ""
    text = str(name).strip().lower()
    agg = re.search(
        r"(?:port[\s_-]*channel|bundle[\s_-]*ether|eth[\s_-]*trunk|po|lag|trk|ae|be)\s*([0-9]+)",
        text,
    )
    if agg:
        return f"agg{int(agg.group(1))}"
    match = re.search(r"(\d+(?:/\d+)+)", text)
    if match:
        return match.group(1)
    # Cisco Small Business / SG switches use flat interface numbers and expose
    # the same port with different spellings across IF-MIB and CDP-MIB:
    # "GigabitEthernet24" vs "gi24".  Treat those as the same key so the
    # bidirectional LLDP/CDP observations collapse into one physical edge.
    flat = re.fullmatch(
        r"(?:gigabitethernet|gi|fastethernet|fa|tengigabitethernet|te|ethernet|eth)(\d+)",
        text,
    )
    if flat:
        return str(int(flat.group(1)))
    return text


def resolve_ifindex_by_name(port_name, ifname_map):
    target = normalize_port_name(port_name)
    if not target:
        return None
    matches = [ifindex for ifindex, name in ifname_map.items()
               if normalize_port_name(name) == target]
    if len(matches) == 1:
        return matches[0]
    return None


def resolve_ifindex(loc_port, ifname_map, loc_port_desc_map):
    """LLDP's local port number is usually ifIndex on Cisco, but some platforms
    use a separate bridge port id. Try identity first, then match the loc port
    description against ifName values (normalized) for a single hit.
    """
    desc = loc_port_desc_map.get(loc_port)
    if desc:
        resolved = resolve_ifindex_by_name(desc, ifname_map)
        if resolved is not None:
            return resolved

    # On IOS the LLDP local port number normally is the ifIndex.  Keep that as
    # the fallback, but only after trying lldpLocPortDesc: SG220 uses a bridge
    # port number here (for example 23) while the real IF-MIB port is 24.
    if loc_port in ifname_map:
        return loc_port
    return None


def load_discovered_switch_ips():
    """自动发现的交换机（discover-switch-targets.py 写的 file_sd JSON）也要参与
    LLDP/CDP 采集：运维只填交换机管理网段时 DIST/TOURNAMENT 列表是空的，只轮询
    核心会看不到接入交换机之间的边，拓扑就退化成一排平铺。"""
    path = os.environ.get("SWITCH_TARGETS_FILE", "/targets/switch_targets.json")
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    ips = []
    if isinstance(data, list):
        for entry in data:
            if not isinstance(entry, dict):
                continue
            for target in entry.get("targets") or []:
                ip = str(target).strip()
                if ip:
                    ips.append(ip)
    return ips


def load_device_list():
    raw = os.environ.get("TOPOLOGY_DEVICES", "").strip()
    if raw:
        devices = []
        seen = set()
        for item in raw.split(","):
            for ip in expand_device_entry(item):
                if ip not in seen:
                    devices.append(ip)
                    seen.add(ip)
        return devices

    union = []
    seen = set()
    for env_var in ("CORE_SWITCH_PING", "DIST_SWITCH_PING", "FIREWALL_PING", "TOURNAMENT_SWITCHES"):
        for entry in os.environ.get(env_var, "").split(","):
            for ip in expand_device_entry(entry):
                if ip in seen:
                    continue
                union.append(ip)
                seen.add(ip)
    for ip in load_discovered_switch_ips():
        if ip not in seen:
            union.append(ip)
            seen.add(ip)
    return union


def expand_device_entry(entry):
    targets = expand_ipv4_entry(entry)
    if not targets and entry.strip():
        print(f"[WARN] invalid/oversized topology target skipped: {entry}", file=sys.stderr)
    return targets


def poll_device(ip, community):
    sysname = snmpget(ip, community, SYS_NAME_OID)
    ifname = parse_ifname(snmpwalk(ip, community, IF_NAME_OID))
    ifoper = parse_if_oper_status(snmpwalk(ip, community, IF_OPER_STATUS_OID))
    arp = parse_arp_table(snmpwalk(ip, community, IP_NET_TO_MEDIA_PHYS_ADDRESS_OID), ifname)
    loc_port_desc = parse_lldp_loc_port_desc(snmpwalk(ip, community, LLDP_LOC_PORT_DESC_OID))
    rem_sys = parse_lldp_rem_field(snmpwalk(ip, community, LLDP_REM_SYS_NAME_OID))
    rem_port_desc = parse_lldp_rem_field(snmpwalk(ip, community, LLDP_REM_PORT_DESC_OID))
    rem_port_id = parse_lldp_rem_field(snmpwalk(ip, community, LLDP_REM_PORT_ID_OID))
    cdp_device_id = parse_cdp_field(snmpwalk(ip, community, CDP_CACHE_DEVICE_ID_OID))
    cdp_device_port = parse_cdp_field(snmpwalk(ip, community, CDP_CACHE_DEVICE_PORT_OID))
    cdp_address = parse_cdp_address(snmpwalk(ip, community, CDP_CACHE_ADDRESS_OID))
    return {
        "ip": ip,
        "sysname": sysname,
        "ifname": ifname,
        "ifoper": ifoper,
        "arp": arp,
        "loc_port_desc": loc_port_desc,
        "rem_sys": rem_sys,
        "rem_port_desc": rem_port_desc,
        "rem_port_id": rem_port_id,
        "cdp_device_id": cdp_device_id,
        "cdp_device_port": cdp_device_port,
        "cdp_address": cdp_address,
    }


def build_name_index(devices):
    """{hostname: ip}. Stores both full hostname and first-dot-stripped variant."""
    index = {}
    for device in devices.values():
        if not device["sysname"]:
            continue
        full = device["sysname"].strip().lower()
        base = normalize_hostname(device["sysname"])
        if full:
            index.setdefault(full, device["ip"])
        if base:
            index.setdefault(base, device["ip"])
    return index


def canonical_edge_key(edge):
    a = (edge["from_ip"] or "", edge["from_ifindex"] or 0)
    b = (edge["to_ip"] or "", edge["to_ifindex"] or 0)
    return tuple(sorted([a, b]))


def merge_edge(edges_by_key, edge):
    """Insert an edge, or backfill missing fields on an existing one (so an LLDP
    and a CDP view of the same link, or both directions, collapse into one)."""
    key = canonical_edge_key(edge)
    existing = edges_by_key.get(key)
    if existing is None:
        edge["_observations"] = 1
        edges_by_key[key] = edge
        return
    existing["_observations"] = existing.get("_observations", 1) + 1
    for field in ("from_port", "from_ifindex", "to_port", "to_ifindex"):
        if not existing.get(field) and edge.get(field):
            existing[field] = edge[field]


def resolve_endpoint_conflicts(edges):
    """Keep one physical neighbor per resolved interface.

    SG220 can expose an off-by-one LLDP bridge-port row alongside the correct
    CDP row. After both directions are polled that yields 24<->24 twice plus
    one 23->24 row from each side. A physical ifIndex cannot terminate two
    different links, so keep the bidirectionally-confirmed edge.
    """
    ranked = sorted(
        edges,
        key=lambda edge: (
            edge.get("_observations", 1),
            int(edge.get("from_ifindex") is not None) + int(edge.get("to_ifindex") is not None),
            int(bool(edge.get("from_port"))) + int(bool(edge.get("to_port"))),
        ),
        reverse=True,
    )
    occupied = set()
    kept = []
    for edge in ranked:
        endpoints = [
            (edge.get("from_ip"), edge.get("from_ifindex")),
            (edge.get("to_ip"), edge.get("to_ifindex")),
        ]
        resolved = [endpoint for endpoint in endpoints if endpoint[0] and endpoint[1] is not None]
        if len(resolved) == 2 and any(endpoint in occupied for endpoint in resolved):
            continue
        if len(resolved) == 2:
            occupied.update(resolved)
        edge.pop("_observations", None)
        kept.append(edge)
    return kept


def build_edges(devices, name_index):
    edges_by_key = {}
    placeholder_neighbors = []

    def interface_is_usable(device, ifindex):
        if ifindex is None:
            return True
        status = device.get("ifoper", {}).get(ifindex)
        return status is None or status == 1

    for ip, device in devices.items():
        for (tm, loc_port, rem_idx), neighbor_name in device["rem_sys"].items():
            neighbor_ip = name_index.get(neighbor_name.strip().lower()) or \
                          name_index.get(normalize_hostname(neighbor_name))
            local_ifindex = resolve_ifindex(loc_port, device["ifname"], device["loc_port_desc"])
            local_port_name = device["ifname"].get(local_ifindex) if local_ifindex else device["loc_port_desc"].get(loc_port)
            remote_port_name = device["rem_port_desc"].get((tm, loc_port, rem_idx)) or \
                               device["rem_port_id"].get((tm, loc_port, rem_idx))

            if neighbor_ip is None:
                placeholder_neighbors.append({
                    "from_ip": ip,
                    "from_port": local_port_name,
                    "neighbor_name": neighbor_name,
                    "neighbor_port": remote_port_name,
                })
                continue

            remote_ifindex = None
            remote = devices.get(neighbor_ip)
            if remote and remote_port_name:
                remote_ifindex = resolve_ifindex_by_name(remote_port_name, remote["ifname"])
                if remote_ifindex is not None:
                    remote_port_name = remote["ifname"].get(remote_ifindex, remote_port_name)

            # LLDP/CDP tables may retain a stale management-port neighbor after
            # the physical interface has gone down. Keep unknown status for
            # compatibility, but never emit an edge known to be down at either
            # resolved endpoint.
            if not interface_is_usable(device, local_ifindex):
                continue
            if remote and not interface_is_usable(remote, remote_ifindex):
                continue

            edge = {
                "from_ip": ip,
                "from_sysname": device["sysname"],
                "from_port": local_port_name,
                "from_ifindex": local_ifindex,
                "to_ip": neighbor_ip,
                "to_sysname": neighbor_name,
                "to_port": remote_port_name,
                "to_ifindex": remote_ifindex,
            }
            merge_edge(edges_by_key, edge)

        # --- CDP neighbors (Cisco). cdpCacheIfIndex in the OID is the real local
        # ifIndex, and cdpCacheAddress gives the neighbor's IP directly. ---
        for (if_index, dev_index), neighbor_name in device.get("cdp_device_id", {}).items():
            addr_ip = device.get("cdp_address", {}).get((if_index, dev_index))
            if addr_ip:
                # The advertised management address is authoritative. Do not
                # fall back to hostname matching when that address belongs to
                # an unmonitored AP/phone: it may share a name with a monitored
                # switch and create a convincing but false switch-to-switch edge.
                neighbor_ip = addr_ip if addr_ip in devices else None
            else:
                neighbor_ip = name_index.get((neighbor_name or "").strip().lower()) or \
                              name_index.get(normalize_hostname(neighbor_name))
            local_port_name = device.get("ifname", {}).get(if_index)
            remote_port_name = device.get("cdp_device_port", {}).get((if_index, dev_index))

            if neighbor_ip is None:
                placeholder_neighbors.append({
                    "from_ip": ip,
                    "from_port": local_port_name,
                    "neighbor_name": neighbor_name,
                    "neighbor_port": remote_port_name,
                })
                continue

            remote_ifindex = None
            remote = devices.get(neighbor_ip)
            if remote and remote_port_name:
                remote_ifindex = resolve_ifindex_by_name(remote_port_name, remote["ifname"])
                if remote_ifindex is not None:
                    remote_port_name = remote["ifname"].get(remote_ifindex, remote_port_name)

            if not interface_is_usable(device, if_index):
                continue
            if remote and not interface_is_usable(remote, remote_ifindex):
                continue

            merge_edge(edges_by_key, {
                "from_ip": ip,
                "from_sysname": device.get("sysname"),
                "from_port": local_port_name,
                "from_ifindex": if_index,
                "to_ip": neighbor_ip,
                "to_sysname": neighbor_name,
                "to_port": remote_port_name,
                "to_ifindex": remote_ifindex,
            })

    return resolve_endpoint_conflicts(list(edges_by_key.values())), placeholder_neighbors


def _env_target_ips(name):
    ips = []
    for entry in os.environ.get(name, "").split(","):
        ips.extend(expand_device_entry(entry))
    return ips


def _graph_depths(edges, root_ips):
    adjacency = {}
    for edge in edges:
        left = edge.get("from_ip")
        right = edge.get("to_ip")
        if not left or not right:
            continue
        adjacency.setdefault(left, set()).add(right)
        adjacency.setdefault(right, set()).add(left)
    depths = {}
    queue = []
    for ip in root_ips:
        depths[ip] = 0
        queue.append(ip)
    while queue:
        ip = queue.pop(0)
        for neighbor in adjacency.get(ip, ()):
            if neighbor in depths:
                continue
            depths[neighbor] = depths[ip] + 1
            queue.append(neighbor)
    return depths


def lookup_fdb_ifindex(ip, community, vlan, mac, ifname_map):
    """Resolve one MAC to a switch ifIndex using exact Q-BRIDGE/BRIDGE GETs."""
    suffix = mac_oid_suffix(mac)
    if not suffix:
        return None

    lookups = []
    if vlan is not None:
        lookups.append((community, f"{DOT1Q_TP_FDB_PORT_OID}.{vlan}.{suffix}"))
        # Classic Cisco IOS exposes per-VLAN BRIDGE-MIB through community@vlan.
        lookups.append((f"{community}@{vlan}", f"{DOT1D_TP_FDB_PORT_OID}.{suffix}"))
    else:
        lookups.append((community, f"{DOT1D_TP_FDB_PORT_OID}.{suffix}"))

    for query_community, oid in lookups:
        bridge_port = _positive_int(snmpget(ip, query_community, oid))
        if bridge_port is None:
            continue
        if bridge_port in ifname_map:
            return bridge_port
        for mapping_community in (query_community, community):
            ifindex = _positive_int(snmpget(
                ip,
                mapping_community,
                f"{DOT1D_BASE_PORT_IFINDEX_OID}.{bridge_port}",
            ))
            if ifindex is not None:
                return ifindex
    return None


def discover_server_edges(devices, edges, servers, community):
    """Locate configured servers at their real access switch via ARP + FDB."""
    if not servers or not devices:
        return []

    arp_by_server = {}
    for device in devices.values():
        for server_ip in servers:
            record = device.get("arp", {}).get(server_ip)
            if record:
                arp_by_server.setdefault(server_ip, []).append(record)

    switch_link_endpoints = set()
    for edge in edges:
        if edge.get("from_ip") in devices and edge.get("from_ifindex") is not None:
            switch_link_endpoints.add((edge["from_ip"], edge["from_ifindex"]))
        if edge.get("to_ip") in devices and edge.get("to_ifindex") is not None:
            switch_link_endpoints.add((edge["to_ip"], edge["to_ifindex"]))

    firewall_ips = set(_env_target_ips("FIREWALL_PING"))
    switch_devices = {
        ip: device for ip, device in devices.items()
        if ip not in firewall_ips and device.get("ifname")
    }
    core_ips = _env_target_ips("CORE_SWITCH_PING")
    depths = _graph_depths(edges, core_ips)
    found = []

    for server_ip, server_name in servers.items():
        records = arp_by_server.get(server_ip, [])
        unique_records = {
            (record["mac"], record.get("vlan"))
            for record in records if record.get("mac")
        }
        if not unique_records:
            print(
                f"[WARN] server {server_name} ({server_ip}): no ARP entry; "
                "keeping core fallback",
                file=sys.stderr,
            )
            continue

        candidates = []
        tasks = {}
        poll_workers = _topology_poll_workers()
        with ThreadPoolExecutor(max_workers=min(poll_workers, max(1, len(switch_devices) * len(unique_records)))) as executor:
            for switch_ip, device in switch_devices.items():
                for mac, vlan in unique_records:
                    future = executor.submit(
                        lookup_fdb_ifindex,
                        switch_ip,
                        community,
                        vlan,
                        mac,
                        device.get("ifname", {}),
                    )
                    tasks[future] = (switch_ip, mac, vlan)
            for future in as_completed(tasks):
                switch_ip, mac, vlan = tasks[future]
                try:
                    ifindex = future.result()
                except Exception as exc:
                    print(f"[WARN] FDB lookup {switch_ip} {server_ip}: {exc}", file=sys.stderr)
                    continue
                if ifindex is None:
                    continue
                # ``device`` above belongs to the task-submission loop and may
                # point at a completely different switch by the time futures
                # finish.  Always resolve the interface from the switch that
                # produced this FDB result.
                switch_device = switch_devices[switch_ip]
                port_name = switch_device.get("ifname", {}).get(ifindex)
                candidates.append({
                    "switch_ip": switch_ip,
                    "ifindex": ifindex,
                    "port_name": port_name,
                    "mac": mac,
                    "vlan": vlan,
                    "is_uplink": (switch_ip, ifindex) in switch_link_endpoints,
                    # A server MAC learned on Po/LAG is commonly a transit copy
                    # from another switch. Prefer a physical access interface
                    # when both observations exist at the same graph depth.
                    "is_aggregate": normalize_port_name(port_name).startswith("agg"),
                    "depth": depths.get(switch_ip, -1),
                })

        access_candidates = [candidate for candidate in candidates if not candidate["is_uplink"]]
        if not access_candidates:
            # Seeing a MAC only on switch-to-switch uplinks does not identify its
            # physical attachment. Keep the UI fallback rather than inventing one.
            print(
                f"[WARN] server {server_name} ({server_ip}): MAC was not found "
                "on a confirmed access port; keeping core fallback",
                file=sys.stderr,
            )
            continue

        # A MAC learned on a logical Po/LAG is not proof that the server is
        # attached there: every downstream switch can learn the same MAC on
        # its transit port-channel.  This was the source of servers jumping
        # between unrelated switches whenever graph depth/order changed.  An
        # exact physical-port FDB hit is authoritative; aggregate-only results
        # are deliberately left unresolved instead of inventing a parent.
        physical_candidates = [
            candidate for candidate in access_candidates
            if not candidate["is_aggregate"]
        ]
        if not physical_candidates:
            print(
                f"[WARN] server {server_name} ({server_ip}): MAC was learned "
                "only on unconfirmed Po/LAG interfaces; keeping core fallback",
                file=sys.stderr,
            )
            continue
        best = max(
            physical_candidates,
            key=lambda candidate: (
                candidate["depth"],
                candidate["switch_ip"],
                -candidate["ifindex"],
            ),
        )
        parent = switch_devices[best["switch_ip"]]
        found.append({
            "from_ip": best["switch_ip"],
            "from_sysname": parent.get("sysname"),
            "from_port": parent.get("ifname", {}).get(best["ifindex"]),
            "from_ifindex": best["ifindex"],
            "to_ip": server_ip,
            "to_sysname": server_name,
            "to_port": None,
            "to_ifindex": None,
            "source": "fdb",
        })
        print(
            f"[INFO] server {server_name} ({server_ip}) attached to "
            f"{best['switch_ip']} {parent.get('ifname', {}).get(best['ifindex']) or best['ifindex']}",
            file=sys.stderr,
        )
    return found


def atomic_write_json(path, data):
    write_json_atomic(path, data, sort_keys=True)


def load_cached_edges(path):
    """Read the last emitted topology without making collection depend on it."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, TypeError):
        return []
    return data if isinstance(data, list) else []


def _server_ip_for_edge(edge, servers):
    """Return the configured server endpoint carried by one topology edge."""
    for ip in (edge.get("from_ip"), edge.get("to_ip")):
        if ip in servers:
            return ip
    return None


def preserve_cached_server_edges(edges, cached_edges, servers, configured_device_ips):
    """Keep a confirmed server attachment through transient ARP/FDB misses.

    A fresh FDB result is authoritative.  The cached edge is considered only
    when the current cycle could not locate that server.  Do not require its
    previous parent to appear in this cycle's auto-discovery list: a single
    failed ICMP/SNMP discovery used to remove the attachment permanently and
    made servers jump back beside the core until a later FDB lookup succeeded.

    ``configured_device_ips`` remains in the signature for compatibility with
    callers/tests from older deployments.  Server membership is the lifecycle
    authority; removing a server from SERVER_PING removes its cached edge.
    """
    linked_servers = {
        server_ip for edge in edges
        if edge.get("source") == "fdb"
        for server_ip in [_server_ip_for_edge(edge, servers)]
        if server_ip
    }
    preserved = []
    for server_ip, server_name in servers.items():
        if server_ip in linked_servers:
            continue
        for edge in cached_edges:
            if edge.get("source") != "fdb":
                continue
            if edge.get("to_ip") == server_ip:
                parent_ip = edge.get("from_ip")
            elif edge.get("from_ip") == server_ip:
                parent_ip = edge.get("to_ip")
            else:
                continue
            preserved.append(dict(edge))
            linked_servers.add(server_ip)
            print(
                f"[INFO] server {server_name} ({server_ip}): current ARP/FDB "
                f"lookup missed; preserving confirmed attachment to {parent_ip}",
                file=sys.stderr,
            )
            break
    return preserved


def replace_server_edges(edges, confirmed_edges, servers):
    """Make confirmed FDB ownership the sole edge for each located server.

    LLDP/CDP tables and discovery order can occasionally produce a weaker edge
    involving a configured server address.  Keeping both allowed the browser's
    last-seen edge to decide the parent, so the same server appeared below
    different switches after refreshes.  A physical-port FDB hit (fresh or
    cached) is authoritative and replaces those weaker observations.
    """
    confirmed_by_server = {}
    for edge in confirmed_edges:
        if edge.get("source") != "fdb":
            continue
        server_ip = _server_ip_for_edge(edge, servers)
        if server_ip and server_ip not in confirmed_by_server:
            confirmed_by_server[server_ip] = dict(edge)
    if not confirmed_by_server:
        return list(edges)
    kept = [
        edge for edge in edges
        if _server_ip_for_edge(edge, servers) not in confirmed_by_server
    ]
    kept.extend(confirmed_by_server.values())
    return kept


def main():
    community = os.environ.get("TOPOLOGY_SNMP_COMMUNITY", "").strip() or os.environ.get("SNMP_COMMUNITY", "global").strip()
    output_dir = os.environ.get("TOPOLOGY_OUTPUT_DIR", "/etc/prometheus/targets/topology")

    device_ips = load_device_list()
    if not device_ips:
        print(
            "[INFO] TOPOLOGY_DEVICES empty and no infra ping envs set; "
            "retaining the last confirmed topology",
            file=sys.stderr,
        )
        return 0

    print(f"[INFO] polling LLDP+CDP+ARP on {len(device_ips)} device(s)", file=sys.stderr)
    devices = {}
    poll_workers = _topology_poll_workers()
    with ThreadPoolExecutor(max_workers=min(poll_workers, len(device_ips))) as executor:
        futures = {executor.submit(poll_device, ip, community): ip for ip in device_ips}
        for future in as_completed(futures):
            ip = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                print(f"[WARN] poll {ip} failed: {exc}", file=sys.stderr)
                continue
            devices[ip] = result
            lldp_n = len(result.get("rem_sys", {}))
            cdp_n = len(result.get("cdp_device_id", {}))
            if lldp_n or cdp_n:
                print(f"[INFO] {ip}: sysname='{result['sysname']}' neighbors lldp={lldp_n} cdp={cdp_n}", file=sys.stderr)
            else:
                print(f"[WARN] {ip}: no LLDP/CDP neighbors (check 'lldp run' or 'cdp run' and SNMP access)", file=sys.stderr)

    edges_path = os.path.join(output_dir, "edges.json")
    attachments_path = os.path.join(output_dir, "server-attachments.json")
    cached_edges = load_cached_edges(edges_path)
    # Attachments have their own durable ledger.  edges.json is a live snapshot
    # and can legitimately be incomplete after a collector restart or a weak
    # SNMP cycle; it must not be the only memory of physical server ownership.
    cached_attachments = load_cached_edges(attachments_path)
    if not cached_attachments:
        # One-time migration for deployments that already have a confirmed FDB
        # edge in the old live snapshot.
        cached_attachments = cached_edges
    name_index = build_name_index(devices)
    edges, placeholders = build_edges(devices, name_index)
    servers = parse_named_ipv4_targets(os.environ.get("SERVER_PING", ""))
    # Always run the exact ARP+FDB ownership lookup.  A weaker LLDP/CDP edge
    # involving the same address must not suppress authoritative discovery.
    fresh_server_edges = discover_server_edges(
        devices,
        edges,
        servers,
        community,
    )
    confirmed_server_edges = list(fresh_server_edges)
    confirmed_server_edges.extend(preserve_cached_server_edges(
        fresh_server_edges,
        cached_attachments,
        servers,
        device_ips,
    ))
    edges = replace_server_edges(edges, confirmed_server_edges, servers)
    uplink_targets = []

    atomic_write_json(edges_path, edges)
    atomic_write_json(attachments_path, confirmed_server_edges)
    atomic_write_json(os.path.join(output_dir, "uplink-targets.json"), uplink_targets)
    atomic_write_json(os.path.join(output_dir, "rates.json"), [])

    print(
        f"[INFO] wrote {len(edges)} edge(s), topology rate polling disabled",
        file=sys.stderr,
    )
    if placeholders:
        print(f"[WARN] {len(placeholders)} neighbor(s) could not be matched to a configured device IP:", file=sys.stderr)
        for entry in placeholders[:10]:
            print(f"         {entry['from_ip']} {entry['from_port']} -> {entry['neighbor_name']} {entry['neighbor_port']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
