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
  TOPOLOGY_OUTPUT_DIR        where to write edges.json / legacy empty files
                             (default: /etc/prometheus/targets/topology).
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import re
import subprocess
import sys
from ipaddress import IPv4Address

SYS_NAME_OID = "1.3.6.1.2.1.1.5.0"
IF_NAME_OID = "1.3.6.1.2.1.31.1.1.1.1"
LLDP_LOC_PORT_DESC_OID = "1.0.8802.1.1.2.1.3.7.1.3"
LLDP_REM_PORT_ID_OID = "1.0.8802.1.1.2.1.4.1.1.7"
LLDP_REM_PORT_DESC_OID = "1.0.8802.1.1.2.1.4.1.1.8"
LLDP_REM_SYS_NAME_OID = "1.0.8802.1.1.2.1.4.1.1.9"

# CISCO-CDP-MIB cdpCacheEntry (row index = cdpCacheIfIndex.cdpCacheDeviceIndex).
# Cisco gear that only runs CDP (not LLDP) is discovered through these.
CDP_CACHE_ADDRESS_OID = "1.3.6.1.4.1.9.9.23.1.2.1.1.4"
CDP_CACHE_DEVICE_ID_OID = "1.3.6.1.4.1.9.9.23.1.2.1.1.6"
CDP_CACHE_DEVICE_PORT_OID = "1.3.6.1.4.1.9.9.23.1.2.1.1.7"


def snmpwalk(host, community, oid, timeout=15):
    cmd = ["snmpwalk", "-v2c", "-c", community, "-O", "n", "-t", str(timeout), host, oid]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
        return result.stdout
    except Exception as exc:
        print(f"[WARN] snmpwalk {host} {oid}: {exc}", file=sys.stderr)
        return ""


def snmpget(host, community, oid, timeout=8):
    cmd = ["snmpget", "-v2c", "-c", community, "-O", "qv", "-t", str(timeout), host, oid]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
        return result.stdout.strip().strip('"')
    except Exception as exc:
        print(f"[WARN] snmpget {host} {oid}: {exc}", file=sys.stderr)
        return ""


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
    entry = entry.strip()
    if not entry:
        return []
    ip_part = entry.split(":", 1)[1].strip() if ":" in entry else entry
    if not ip_part:
        return []
    if "-" not in ip_part:
        try:
            IPv4Address(ip_part)
        except ValueError:
            return []
        return [ip_part]

    start_ip, end_part = [part.strip() for part in ip_part.split("-", 1)]
    try:
        start = IPv4Address(start_ip)
    except ValueError:
        return []
    if "." in end_part:
        try:
            end = IPv4Address(end_part)
        except ValueError:
            return []
    else:
        octets = start_ip.split(".")
        octets[-1] = end_part
        try:
            end = IPv4Address(".".join(octets))
        except ValueError:
            return []
    if int(end) < int(start):
        return []
    return [str(IPv4Address(value)) for value in range(int(start), int(end) + 1)]


def poll_device(ip, community):
    sysname = snmpget(ip, community, SYS_NAME_OID)
    ifname = parse_ifname(snmpwalk(ip, community, IF_NAME_OID))
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
            if addr_ip and addr_ip in devices:
                neighbor_ip = addr_ip
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


def atomic_write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def main():
    community = os.environ.get("TOPOLOGY_SNMP_COMMUNITY", "").strip() or os.environ.get("SNMP_COMMUNITY", "global").strip()
    output_dir = os.environ.get("TOPOLOGY_OUTPUT_DIR", "/etc/prometheus/targets/topology")

    device_ips = load_device_list()
    if not device_ips:
        print("[INFO] TOPOLOGY_DEVICES empty and no infra ping envs set; nothing to poll", file=sys.stderr)
        atomic_write_json(os.path.join(output_dir, "edges.json"), [])
        atomic_write_json(os.path.join(output_dir, "uplink-targets.json"), [])
        atomic_write_json(os.path.join(output_dir, "rates.json"), [])
        return 0

    print(f"[INFO] polling LLDP+CDP on {len(device_ips)} device(s) with community={community}", file=sys.stderr)
    devices = {}
    with ThreadPoolExecutor(max_workers=min(16, len(device_ips))) as executor:
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

    name_index = build_name_index(devices)
    edges, placeholders = build_edges(devices, name_index)
    uplink_targets = []

    atomic_write_json(os.path.join(output_dir, "edges.json"), edges)
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

