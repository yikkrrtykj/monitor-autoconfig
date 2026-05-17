#!/usr/bin/env python3
"""
Walk LLDP-MIB on every configured infrastructure device to build the real
network adjacency graph, then emit two artifacts for downstream:

  edges.json          (consumed by the bigscreen /topology page)
  uplink-targets.json (consumed by Prometheus file_sd for if_mib scraping
                       of the LLDP-discovered uplink interfaces)

Env vars:
  TOPOLOGY_DEVICES           comma-separated device IPs to poll. Empty -> union of
                             CORE_SWITCH_PING + DIST_SWITCH_PING + FIREWALL_PING +
                             TOURNAMENT_SWITCHES.
  TOPOLOGY_SNMP_COMMUNITY    SNMPv2c community (default: SNMP_COMMUNITY).
  TOPOLOGY_OUTPUT_DIR        where to write edges.json / uplink-targets.json
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


def normalize_hostname(name):
    if not name:
        return ""
    stripped = name.strip().lower()
    base = stripped.split(".", 1)[0]
    return base


def normalize_port_name(name):
    """Reduce a Cisco port name to its slash-numeric tail.

    "GigabitEthernet1/0/19" -> "1/0/19"
    "Gi1/0/19"              -> "1/0/19"
    "Te0/1"                 -> "0/1"
    Anything without a digit/slash path returns the trimmed lowercase original.
    """
    if not name:
        return ""
    text = str(name).strip()
    match = re.search(r"(\d+(?:/\d+)+)", text)
    if match:
        return match.group(1)
    return text.lower()


def resolve_ifindex(loc_port, ifname_map, loc_port_desc_map):
    """LLDP's local port number is usually ifIndex on Cisco, but some platforms
    use a separate bridge port id. Try identity first, then match the loc port
    description against ifName values (normalized) for a single hit.
    """
    if loc_port in ifname_map:
        return loc_port

    desc = loc_port_desc_map.get(loc_port)
    if not desc:
        return None

    target = normalize_port_name(desc)
    if not target:
        return None
    matches = [ifindex for ifindex, name in ifname_map.items()
               if normalize_port_name(name) == target]
    if len(matches) == 1:
        return matches[0]
    return None


def load_device_list():
    raw = os.environ.get("TOPOLOGY_DEVICES", "").strip()
    if raw:
        return [item.strip() for item in raw.split(",") if item.strip()]

    union = []
    seen = set()
    for env_var in ("CORE_SWITCH_PING", "DIST_SWITCH_PING", "FIREWALL_PING", "TOURNAMENT_SWITCHES"):
        for entry in os.environ.get(env_var, "").split(","):
            entry = entry.strip()
            if not entry:
                continue
            ip = entry.split(":", 1)[1].strip() if ":" in entry else entry
            if ip and ip not in seen:
                try:
                    IPv4Address(ip)
                except ValueError:
                    continue
                union.append(ip)
                seen.add(ip)
    return union


def poll_device(ip, community):
    sysname = snmpget(ip, community, SYS_NAME_OID)
    ifname = parse_ifname(snmpwalk(ip, community, IF_NAME_OID))
    loc_port_desc = parse_lldp_loc_port_desc(snmpwalk(ip, community, LLDP_LOC_PORT_DESC_OID))
    rem_sys = parse_lldp_rem_field(snmpwalk(ip, community, LLDP_REM_SYS_NAME_OID))
    rem_port_desc = parse_lldp_rem_field(snmpwalk(ip, community, LLDP_REM_PORT_DESC_OID))
    rem_port_id = parse_lldp_rem_field(snmpwalk(ip, community, LLDP_REM_PORT_ID_OID))
    return {
        "ip": ip,
        "sysname": sysname,
        "ifname": ifname,
        "loc_port_desc": loc_port_desc,
        "rem_sys": rem_sys,
        "rem_port_desc": rem_port_desc,
        "rem_port_id": rem_port_id,
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
                target = normalize_port_name(remote_port_name)
                if target:
                    for idx, name in remote["ifname"].items():
                        if normalize_port_name(name) == target:
                            remote_ifindex = idx
                            break

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
            key = canonical_edge_key(edge)
            existing = edges_by_key.get(key)
            if existing is None:
                edges_by_key[key] = edge
            else:
                for field in ("from_port", "from_ifindex", "to_port", "to_ifindex"):
                    if not existing.get(field) and edge.get(field):
                        existing[field] = edge[field]

    return list(edges_by_key.values()), placeholder_neighbors


def build_uplink_targets(edges):
    """Each (device, ifIndex) used by an edge becomes one file_sd entry."""
    seen = set()
    targets = []
    for edge in edges:
        for side in ("from", "to"):
            ip = edge.get(f"{side}_ip")
            ifindex = edge.get(f"{side}_ifindex")
            port = edge.get(f"{side}_port") or ""
            if not ip or ifindex is None:
                continue
            key = (ip, ifindex)
            if key in seen:
                continue
            seen.add(key)
            peer_side = "to" if side == "from" else "from"
            peer_ip = edge.get(f"{peer_side}_ip") or ""
            targets.append({
                "targets": [ip],
                "labels": {
                    "ifIndex": str(ifindex),
                    "port": port,
                    "peer_ip": peer_ip,
                    "display_name": ip,
                },
            })
    return targets


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
        return 0

    print(f"[INFO] polling LLDP on {len(device_ips)} device(s) with community={community}", file=sys.stderr)
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
            if result["rem_sys"]:
                print(f"[INFO] {ip}: sysname='{result['sysname']}' lldp_neighbors={len(result['rem_sys'])}", file=sys.stderr)
            else:
                print(f"[WARN] {ip}: no LLDP neighbors (check 'lldp run' on the device)", file=sys.stderr)

    name_index = build_name_index(devices)
    edges, placeholders = build_edges(devices, name_index)
    uplink_targets = build_uplink_targets(edges)

    atomic_write_json(os.path.join(output_dir, "edges.json"), edges)
    atomic_write_json(os.path.join(output_dir, "uplink-targets.json"), uplink_targets)

    print(f"[INFO] wrote {len(edges)} edge(s), {len(uplink_targets)} uplink target(s)", file=sys.stderr)
    if placeholders:
        print(f"[WARN] {len(placeholders)} neighbor(s) could not be matched to a configured device IP:", file=sys.stderr)
        for entry in placeholders[:10]:
            print(f"         {entry['from_ip']} {entry['from_port']} -> {entry['neighbor_name']} {entry['neighbor_port']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
