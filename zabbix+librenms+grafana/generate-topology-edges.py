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
import time
from ipaddress import IPv4Address

SYS_NAME_OID = "1.3.6.1.2.1.1.5.0"
IF_NAME_OID = "1.3.6.1.2.1.31.1.1.1.1"
LLDP_LOC_PORT_DESC_OID = "1.0.8802.1.1.2.1.3.7.1.3"
LLDP_REM_PORT_ID_OID = "1.0.8802.1.1.2.1.4.1.1.7"
LLDP_REM_PORT_DESC_OID = "1.0.8802.1.1.2.1.4.1.1.8"
LLDP_REM_SYS_NAME_OID = "1.0.8802.1.1.2.1.4.1.1.9"
IF_HC_IN_OCTETS_OID = "1.3.6.1.2.1.31.1.1.1.6"
IF_HC_OUT_OCTETS_OID = "1.3.6.1.2.1.31.1.1.1.10"


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


def parse_numeric_value(value):
    text = value.strip()
    if ":" in text:
        _, _, text = text.partition(":")
        text = text.strip()
    match = re.search(r"(-?\d+)", text)
    return int(match.group(1)) if match else None


def snmpget_oids(host, community, oids, timeout=8):
    if not oids:
        return {}
    cmd = ["snmpget", "-v2c", "-c", community, "-O", "n", "-t", str(timeout), host, *oids]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
    except Exception as exc:
        print(f"[WARN] snmpget {host} {len(oids)} oid(s): {exc}", file=sys.stderr)
        return {}

    values = {}
    for line in result.stdout.strip().split("\n"):
        parts, value = parse_oid_value(line)
        if not parts:
            continue
        parsed = parse_numeric_value(value)
        if parsed is not None:
            values[".".join(parts)] = parsed
    return values


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
    return text


def is_aggregate_port_name(name):
    return normalize_port_name(name).startswith("agg")


def is_optical_port_name(name):
    text = str(name or "").strip().lower()
    return bool(re.search(
        r"^(?:te|twe|fo|hu|xe|xge|sfp|qsfp|ten|twenty|forty|hundred|fiber)",
        text,
    ))


def physical_port_group(name):
    text = str(name or "").strip().lower()
    match = re.search(r"(\d+(?:/\d+)+)", text)
    if not match:
        return None
    nums = tuple(int(part) for part in match.group(1).split("/"))
    if len(nums) < 2:
        return None
    prefix = re.sub(r"[^a-z]+", "", text[:match.start()])
    return prefix, nums[:-1], nums[-1]


def build_likely_uplink_ifindexes(ifname_map):
    candidates = set()
    groups = {}
    for ifindex, name in ifname_map.items():
        if is_aggregate_port_name(name) or is_optical_port_name(name):
            candidates.add(ifindex)
        group = physical_port_group(name)
        if group:
            key = (group[0], group[1])
            groups.setdefault(key, []).append((group[2], ifindex))

    for ports in groups.values():
        for _, ifindex in sorted(ports, reverse=True)[:2]:
            candidates.add(ifindex)
    return candidates


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
    if loc_port in ifname_map:
        return loc_port

    desc = loc_port_desc_map.get(loc_port)
    if not desc:
        return None

    return resolve_ifindex_by_name(desc, ifname_map)


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
    """Each device with at least one resolved edge interface becomes one file_sd entry.

    Prometheus scrapes if_mib once per device and the frontend picks the edge's
    real metric ifIndex from that scrape. Emitting one target per (device,
    ifIndex) made the same core switch walk if_mib many times per scrape and
    also collided with the exporter-provided ifIndex label.
    """
    seen = set()
    targets = []
    for edge in edges:
        for side in ("from", "to"):
            ip = edge.get(f"{side}_ip")
            ifindex = edge.get(f"{side}_ifindex")
            if not ip or ifindex is None:
                continue
            if ip in seen:
                continue
            seen.add(ip)
            targets.append({
                "targets": [ip],
                "labels": {
                    "display_name": ip,
                },
            })
    return targets


def env_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def edge_rate_endpoints(edges, devices):
    candidate_indexes = {
        ip: build_likely_uplink_ifindexes(device.get("ifname", {}))
        for ip, device in devices.items()
    }
    endpoints = {}
    for edge in edges:
        for side in ("from", "to"):
            ip = edge.get(f"{side}_ip")
            ifindex = edge.get(f"{side}_ifindex")
            port = edge.get(f"{side}_port") or ""
            if not ip or ifindex is None:
                continue
            candidates = candidate_indexes.get(ip, set())
            if candidates and ifindex not in candidates and not (
                is_aggregate_port_name(port) or is_optical_port_name(port)
            ):
                continue
            endpoints.setdefault(ip, set()).add(int(ifindex))
    return endpoints


def load_rate_state(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def counter_rate(prev_value, value, elapsed):
    if prev_value is None or value is None or elapsed <= 0:
        return None
    delta = value - prev_value
    if delta < 0:
        return None
    return delta * 8.0 / elapsed


def fetch_interface_counters(ip, community, ifindexes):
    oids = []
    for ifindex in sorted(ifindexes):
        oids.append(f"{IF_HC_IN_OCTETS_OID}.{ifindex}")
        oids.append(f"{IF_HC_OUT_OCTETS_OID}.{ifindex}")
    raw = snmpget_oids(ip, community, oids)
    counters = {}
    for ifindex in ifindexes:
        counters[ifindex] = {
            "in": raw.get(f"{IF_HC_IN_OCTETS_OID}.{ifindex}"),
            "out": raw.get(f"{IF_HC_OUT_OCTETS_OID}.{ifindex}"),
        }
    return counters


def build_rate_samples(edges, devices, community, state_path):
    endpoints = edge_rate_endpoints(edges, devices)
    previous = load_rate_state(state_path)
    now = time.time()
    next_state = {}
    samples = []

    for ip, ifindexes in endpoints.items():
        counters = fetch_interface_counters(ip, community, ifindexes)
        for ifindex, values in counters.items():
            key = f"{ip}|{ifindex}"
            prev = previous.get(key, {})
            elapsed = now - float(prev.get("ts", 0) or 0)
            in_bps = counter_rate(prev.get("in"), values.get("in"), elapsed)
            out_bps = counter_rate(prev.get("out"), values.get("out"), elapsed)
            next_state[key] = {
                "ts": now,
                "in": values.get("in"),
                "out": values.get("out"),
            }
            sample = {
                "instance": ip,
                "target_ip": ip,
                "ifIndex": str(ifindex),
            }
            if in_bps is not None:
                sample["in_bps"] = in_bps
            if out_bps is not None:
                sample["out_bps"] = out_bps
            if "in_bps" in sample or "out_bps" in sample:
                samples.append(sample)

    return samples, next_state


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
    rate_state_path = os.path.join(output_dir, "rate-state.json")
    rate_samples, rate_state = build_rate_samples(edges, devices, community, rate_state_path)
    uplink_targets = build_uplink_targets(edges) if env_bool("TOPOLOGY_ENABLE_PROMETHEUS_UPLINKS", False) else []

    atomic_write_json(os.path.join(output_dir, "edges.json"), edges)
    atomic_write_json(os.path.join(output_dir, "uplink-targets.json"), uplink_targets)
    atomic_write_json(os.path.join(output_dir, "rates.json"), rate_samples)
    atomic_write_json(rate_state_path, rate_state)

    print(
        f"[INFO] wrote {len(edges)} edge(s), {len(rate_samples)} rate sample(s), "
        f"{len(uplink_targets)} prometheus uplink target(s)",
        file=sys.stderr,
    )
    if placeholders:
        print(f"[WARN] {len(placeholders)} neighbor(s) could not be matched to a configured device IP:", file=sys.stderr)
        for entry in placeholders[:10]:
            print(f"         {entry['from_ip']} {entry['from_port']} -> {entry['neighbor_name']} {entry['neighbor_port']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
