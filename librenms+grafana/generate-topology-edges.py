#!/usr/bin/env python3
"""
Collect LLDP/CDP adjacency for every configured infrastructure device to build
the real network graph.  LibreNMS discovery data is preferred by default and
direct SNMP remains the bounded per-device/per-component fallback.  Emit:

  edges.json              (consumed by the bigscreen /topology page)
  server-attachments.json (durable last-confirmed server/FDB locations)

Env vars:
  TOPOLOGY_DATA_SOURCE       hybrid (default), librenms, or direct-snmp.
  TOPOLOGY_LIBRENMS_POLL_MAX_AGE_SECONDS
                             max explicit device last_polled age (default: 600).
  TOPOLOGY_LIBRENMS_DISCOVERY_MAX_AGE_SECONDS
                             max explicit last_discovered age (default: 28800).
  TOPOLOGY_DEVICES           comma-separated device IPs to poll. Empty -> union of
                             CORE_SWITCH_PING + DIST_SWITCH_PING + FIREWALL_PING +
                             TOURNAMENT_SWITCHES + auto-discovered switches from
                             SWITCH_TARGETS_FILE (default /targets/switch_targets.json).
  TOPOLOGY_SNMP_COMMUNITY    SNMPv2c community (default: SNMP_COMMUNITY).
  TOPOLOGY_SNMP_TIMEOUT      per-request timeout seconds (default: 2).
  TOPOLOGY_SNMP_RETRIES      retries per request (default: 0).
  TOPOLOGY_POLL_WORKERS      devices polled concurrently (default: 1).
  TOPOLOGY_SNMP_DELAY_MS     pause after each SNMP request (default: 500).
  TOPOLOGY_ARP_DEVICES       comma-separated L3 devices whose ARP tables may
                             contain configured servers. Empty -> core and
                             firewall targets; if those are also empty, all.
  TOPOLOGY_EDGE_RETENTION_SECONDS  keep last confirmed missing/down links in
                             edges.json (default: 86400 / 24 hours).
  TOPOLOGY_OUTPUT_DIR        where to write topology state files
                             (default: /etc/prometheus/targets/topology).
  SERVER_PING                named server targets; ARP/FDB resolves their real
                             access switch and port when SNMP exposes both.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
import json
import os
import re
import subprocess
import sys
import threading
import time
from ipaddress import IPv4Address

try:
    import fcntl
except ImportError:  # pragma: no cover - only used by Linux containers
    fcntl = None

from target_utils import (
    expand_ipv4_entry,
    normalize_mac,
    parse_if_oper_status,
    parse_named_ipv4_targets,
    write_json_atomic,
)
from librenms_client import LibreNMSClient, LibreNMSError, age_seconds

SYS_NAME_OID = "1.3.6.1.2.1.1.5.0"
IF_NAME_OID = "1.3.6.1.2.1.31.1.1.1.1"
IF_OPER_STATUS_OID = "1.3.6.1.2.1.2.2.1.8"
IF_STACK_STATUS_OID = "1.3.6.1.2.1.31.1.2.1.3"
# CISCO-PAGP-MIB pagpGroupIfIndex. Despite the MIB name, Cisco also exposes
# manually configured/static EtherChannels here (pagpEthcOperationMode=manual).
PAGP_GROUP_IFINDEX_OID = "1.3.6.1.4.1.9.9.98.1.1.1.1.8"
# IEEE8023-LAG-MIB dot3adAggPortAttachedAggID for LACP member -> aggregator.
DOT3AD_ATTACHED_AGG_ID_OID = "1.2.840.10006.300.43.1.2.1.1.13"
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

TOPOLOGY_DATA_SOURCES = frozenset({"hybrid", "librenms", "direct-snmp"})
DEFAULT_LIBRENMS_POLL_MAX_AGE_SECONDS = 600
DEFAULT_LIBRENMS_DISCOVERY_MAX_AGE_SECONDS = 8 * 60 * 60

_collection_stats_lock = threading.Lock()
_collection_stats = {
    "direct_snmp_gets": 0,
    "direct_snmp_walks": 0,
    "server_snmp_gets": 0,
    "server_snmp_walks": 0,
}
_snmp_context = threading.local()


def reset_collection_stats():
    with _collection_stats_lock:
        for key in _collection_stats:
            _collection_stats[key] = 0


def collection_stats_snapshot():
    with _collection_stats_lock:
        return dict(_collection_stats)


def _record_snmp_call(kind):
    phase = getattr(_snmp_context, "phase", "topology")
    prefix = "server" if phase == "server" else "direct"
    key = f"{prefix}_snmp_{kind}"
    with _collection_stats_lock:
        _collection_stats[key] += 1


@contextmanager
def snmp_phase(phase):
    previous = getattr(_snmp_context, "phase", "topology")
    _snmp_context.phase = phase
    try:
        yield
    finally:
        _snmp_context.phase = previous


def _snmp_limits(timeout=None, retries=None):
    timeout = float(timeout if timeout is not None else os.environ.get("TOPOLOGY_SNMP_TIMEOUT", "2"))
    retries = int(retries if retries is not None else os.environ.get("TOPOLOGY_SNMP_RETRIES", "0"))
    return max(0.2, timeout), max(0, retries)


def _snmp_request_delay():
    try:
        delay_ms = float(os.environ.get("TOPOLOGY_SNMP_DELAY_MS", "500") or "500")
    except ValueError:
        delay_ms = 500
    if delay_ms > 0:
        time.sleep(min(delay_ms, 2000) / 1000)


def _topology_poll_workers():
    try:
        workers = int(os.environ.get("TOPOLOGY_POLL_WORKERS", "1") or "1")
    except ValueError:
        workers = 1
    return max(1, min(workers, 32))


class TopologyDataIncomplete(RuntimeError):
    """LibreNMS returned a valid response that cannot safely build topology."""


def topology_data_source():
    value = os.environ.get("TOPOLOGY_DATA_SOURCE", "hybrid").strip().lower()
    if value in TOPOLOGY_DATA_SOURCES:
        return value
    print(
        f"[WARN] unsupported TOPOLOGY_DATA_SOURCE={value!r}; using hybrid",
        file=sys.stderr,
    )
    return "hybrid"


def _env_nonnegative_seconds(name, default):
    try:
        value = int(os.environ.get(name, str(default)) or default)
    except (TypeError, ValueError):
        value = default
    return max(0, value)


def topology_librenms_poll_max_age():
    return _env_nonnegative_seconds(
        "TOPOLOGY_LIBRENMS_POLL_MAX_AGE_SECONDS",
        DEFAULT_LIBRENMS_POLL_MAX_AGE_SECONDS,
    )


def topology_librenms_discovery_max_age():
    return _env_nonnegative_seconds(
        "TOPOLOGY_LIBRENMS_DISCOVERY_MAX_AGE_SECONDS",
        DEFAULT_LIBRENMS_DISCOVERY_MAX_AGE_SECONDS,
    )


def librenms_freshness(timestamp, max_age, now=None):
    """Return fresh/stale/unknown without treating absent metadata as stale."""
    age = age_seconds(timestamp, now=now)
    if age is None or age < 0:
        return "unknown"
    return "fresh" if age <= max_age else "stale"


def _as_positive_int(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _if_oper_status_value(value):
    if value is None or value == "":
        return None
    text = str(value).strip().lower()
    named = {
        "up": 1,
        "down": 2,
        "testing": 3,
        "unknown": 4,
        "dormant": 5,
        "notpresent": 6,
        "lowerlayerdown": 7,
    }
    name = text.split("(", 1)[0].strip().replace("-", "")
    if name in named:
        return named[name]
    match = re.search(r"\b([1-7])\b", text)
    return int(match.group(1)) if match else None


def _link_is_inactive(link):
    if "active" not in link or link.get("active") is None:
        return False
    value = link.get("active")
    if isinstance(value, bool):
        return not value
    return str(value).strip().lower() in {"0", "false", "no", "inactive", "down"}


def snmpwalk(host, community, oid, timeout=None, retries=None):
    _record_snmp_call("walks")
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
        print(
            f"[WARN] snmpwalk {host} {oid} failed ({type(exc).__name__})",
            file=sys.stderr,
        )
        return ""
    finally:
        _snmp_request_delay()


def snmpget(host, community, oid, timeout=None, retries=None):
    _record_snmp_call("gets")
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
        print(
            f"[WARN] snmpget {host} {oid} failed ({type(exc).__name__})",
            file=sys.stderr,
        )
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


def parse_if_stack_status(output):
    """Active ifStackTable rows -> {higher_ifindex: [lower_ifindex, ...]}.

    Cisco IOS/C1000 commonly advertises only one LLDP/CDP neighbor row for a
    Port-channel. The IF-MIB stack is the device-local authority that tells us
    which other physical ports belong to that same aggregate.
    """
    mapping = {}
    for line in output.strip().split("\n"):
        parts, value = parse_oid_value(line)
        if not parts or len(parts) < 2:
            continue
        try:
            higher = int(parts[-2])
            lower = int(parts[-1])
        except ValueError:
            continue
        parenthesized = re.search(r"\(([0-9]+)\)", value)
        numeric = re.search(r"(?:INTEGER:\s*)?([0-9]+)\s*$", value, re.IGNORECASE)
        match = parenthesized or numeric
        if not match or int(match.group(1)) != 1 or higher == 0 or lower == 0:
            continue
        bucket = mapping.setdefault(higher, [])
        if lower not in bucket:
            bucket.append(lower)
    return mapping


def parse_member_aggregate_ifindex(output):
    """Member-indexed aggregation column -> {aggregate_ifindex: [members]}.

    Both CISCO-PAGP-MIB::pagpGroupIfIndex and
    IEEE8023-LAG-MIB::dot3adAggPortAttachedAggID use the physical member
    ifIndex as the row index and return the aggregate interface's ifIndex.
    Zero/self references are not multi-port attachments and are ignored.
    """
    mapping = {}
    for line in output.strip().split("\n"):
        parts, value = parse_oid_value(line)
        if not parts:
            continue
        try:
            member_ifindex = int(parts[-1])
        except ValueError:
            continue
        parenthesized = re.search(r"\(([0-9]+)\)", value)
        numeric = re.search(r"(?:INTEGER:\s*)?([0-9]+)\s*$", value, re.IGNORECASE)
        match = parenthesized or numeric
        if not match:
            continue
        aggregate_ifindex = int(match.group(1))
        if aggregate_ifindex <= 0 or aggregate_ifindex == member_ifindex:
            continue
        bucket = mapping.setdefault(aggregate_ifindex, [])
        if member_ifindex not in bucket:
            bucket.append(member_ifindex)
    return mapping


def merge_aggregate_member_maps(*mappings):
    """Union IF-MIB, Cisco static/PAgP, and IEEE LACP member relations."""
    merged = {}
    for mapping in mappings:
        for aggregate_ifindex, member_ifindexes in (mapping or {}).items():
            bucket = merged.setdefault(aggregate_ifindex, [])
            for member_ifindex in member_ifindexes:
                if member_ifindex not in bucket:
                    bucket.append(member_ifindex)
    return merged


def incomplete_active_aggregate_ifindexes(ifnames, ifoper, member_map):
    """Return active Port-channels without at least two known physical members.

    Some Catalyst stacks publish just one lower-layer row in IF-MIB even when
    two links are bundled. A single row is therefore incomplete, not sufficient
    evidence to skip the Cisco/IEEE aggregation tables.
    """
    return {
        ifindex
        for ifindex, name in (ifnames or {}).items()
        if normalize_port_name(name).startswith("agg")
        and (ifoper or {}).get(ifindex) == 1
        and len(set((member_map or {}).get(ifindex, []))) < 2
    }


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


def is_physical_interface_name(name):
    """Return True only for a recognisable physical Ethernet interface.

    FDB bridge-port numbers and IF-MIB ifIndexes live in different namespaces.
    On Cisco they can accidentally share a small integer (for example bridge
    port 5 and ifIndex 5 == VLAN-1002).  Requiring an Ethernet-shaped ifName
    prevents an SVI, loopback or aggregate from becoming a server attachment.
    """
    text = re.sub(r"[\s_-]+", "", str(name or "").strip().lower())
    return re.fullmatch(
        r"(?:"
        r"fastethernet|fa|gigabitethernet|gi|"
        r"tengigabitethernet|tengige|te|"
        r"twentyfivegigabitethernet|twentyfivegige|twe|"
        r"fortygigabitethernet|fortygige|fo|"
        r"hundredgigabitethernet|hundredgige|hu|"
        r"ethernet|eth"
        r")\d+(?:/\d+)*",
        text,
    ) is not None


def _has_physical_endpoint_evidence(edge, side):
    """Whether an endpoint contains a real interface identity.

    Some malformed LLDP rows advertise a chassis/MAC octet string as the
    remote port while neither side resolves to an IF-MIB ifIndex.  Such a row
    is too weak to emit or preserve: it otherwise becomes a false
    switch-to-switch link for the entire retention window.

    Keep explicitly resolved ifIndexes and recognisable interface names.  A
    vendor description such as ``To-Core`` is deliberately not sufficient on
    its own, while a normalised name (Gi1/0/1, Te2/0/2, Po11, Gi24, ...) is.
    """
    if edge.get(f"{side}_ifindex") not in (None, ""):
        return True
    raw = str(edge.get(f"{side}_port") or "").strip().lower()
    if not raw:
        return False
    normalized = normalize_port_name(raw)
    return bool(normalized) and (
        normalized != raw or
        re.fullmatch(r"\d+(?:/\d+)+", raw) is not None
    )


_INTERFACE_TYPE_ALIASES = {
    "fastethernet": "fa",
    "fa": "fa",
    "gigabitethernet": "gi",
    "gi": "gi",
    "tengigabitethernet": "te",
    "te": "te",
    "twentyfivegige": "twe",
    "twentyfivegigabitethernet": "twe",
    "twe": "twe",
    "fortygige": "fo",
    "fortygigabitethernet": "fo",
    "fo": "fo",
    "hundredgige": "hu",
    "hundredgigabitethernet": "hu",
    "hu": "hu",
}


def typed_interface_identity(name):
    """Preserve interface speed while matching Cisco long/short names.

    A Catalyst stack may contain both Gi1/0/2 and Te1/0/2. The generic
    topology key intentionally reduces both to 1/0/2, but endpoint resolution
    must retain the Gi/Te distinction so LLDP/CDP observations are unambiguous.
    """
    if not name:
        return ""
    text = re.sub(r"[\s_-]+", "", str(name).strip().lower())
    match = re.fullmatch(r"([a-z]+)(\d+(?:/\d+)+)", text)
    if not match:
        return ""
    interface_type = _INTERFACE_TYPE_ALIASES.get(match.group(1))
    if not interface_type:
        return ""
    return f"{interface_type}:{match.group(2)}"


def resolve_ifindex_by_name(port_name, ifname_map):
    typed_target = typed_interface_identity(port_name)
    if typed_target:
        typed_matches = [
            ifindex for ifindex, name in ifname_map.items()
            if typed_interface_identity(name) == typed_target
        ]
        if len(typed_matches) == 1:
            return typed_matches[0]

    exact_target = str(port_name or "").strip().lower()
    exact_matches = [
        ifindex for ifindex, name in ifname_map.items()
        if str(name or "").strip().lower() == exact_target
    ]
    if len(exact_matches) == 1:
        return exact_matches[0]

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


def _empty_device(ip):
    return {
        "ip": ip,
        "device_id": None,
        "sysname": "",
        "ifname": {},
        "ifoper": {},
        "ifstack": {},
        "port_by_id": {},
        "arp": {},
        "neighbors": [],
        "loc_port_desc": {},
        "rem_sys": {},
        "rem_port_desc": {},
        "rem_port_id": {},
        "cdp_device_id": {},
        "cdp_device_port": {},
        "cdp_address": {},
        "source": {"ports": "unavailable", "links": "unavailable", "lag": "unavailable"},
        "freshness": {"poll": "unknown", "discovery": "unknown"},
        "poll_seconds": 0,
    }


def poll_snmp_lag(ip, community, ifname, ifoper, initial=None):
    """Collect only aggregate membership, preserving all existing fallbacks."""
    ifstack = merge_aggregate_member_maps(
        initial or {},
        parse_if_stack_status(snmpwalk(ip, community, IF_STACK_STATUS_OID)),
    )
    if incomplete_active_aggregate_ifindexes(ifname, ifoper, ifstack):
        ifstack = merge_aggregate_member_maps(
            ifstack,
            parse_member_aggregate_ifindex(
                snmpwalk(ip, community, PAGP_GROUP_IFINDEX_OID)
            ),
        )
    if incomplete_active_aggregate_ifindexes(ifname, ifoper, ifstack):
        ifstack = merge_aggregate_member_maps(
            ifstack,
            parse_member_aggregate_ifindex(
                snmpwalk(ip, community, DOT3AD_ATTACHED_AGG_ID_OID)
            ),
        )
    return ifstack


def poll_snmp_neighbors(ip, community):
    """Collect LLDP and CDP tables without repeating IF-MIB port walks."""
    return {
        "loc_port_desc": parse_lldp_loc_port_desc(
            snmpwalk(ip, community, LLDP_LOC_PORT_DESC_OID)
        ),
        "rem_sys": parse_lldp_rem_field(
            snmpwalk(ip, community, LLDP_REM_SYS_NAME_OID)
        ),
        "rem_port_desc": parse_lldp_rem_field(
            snmpwalk(ip, community, LLDP_REM_PORT_DESC_OID)
        ),
        "rem_port_id": parse_lldp_rem_field(
            snmpwalk(ip, community, LLDP_REM_PORT_ID_OID)
        ),
        "cdp_device_id": parse_cdp_field(
            snmpwalk(ip, community, CDP_CACHE_DEVICE_ID_OID)
        ),
        "cdp_device_port": parse_cdp_field(
            snmpwalk(ip, community, CDP_CACHE_DEVICE_PORT_OID)
        ),
        "cdp_address": parse_cdp_address(
            snmpwalk(ip, community, CDP_CACHE_ADDRESS_OID)
        ),
    }


def poll_snmp_arp(ip, community, ifname):
    """Keep server attachment ARP collection separate from adjacency stats."""
    with snmp_phase("server"):
        output = snmpwalk(ip, community, IP_NET_TO_MEDIA_PHYS_ADDRESS_OID)
    return parse_arp_table(output, ifname)


def poll_device_snmp(ip, community, collect_arp=True):
    """Original direct-SNMP collector, split only into composable stages."""
    started = time.monotonic()
    sysname = snmpget(ip, community, SYS_NAME_OID)
    ifname = parse_ifname(snmpwalk(ip, community, IF_NAME_OID))
    ifoper = parse_if_oper_status(snmpwalk(ip, community, IF_OPER_STATUS_OID))
    ifstack = poll_snmp_lag(ip, community, ifname, ifoper)
    arp = poll_snmp_arp(ip, community, ifname) if collect_arp else {}
    device = _empty_device(ip)
    device.update({
        "sysname": sysname,
        "ifname": ifname,
        "ifoper": ifoper,
        "ifstack": ifstack,
        "arp": arp,
        "source": {
            "ports": "direct-snmp",
            "links": "direct-snmp",
            "lag": "direct-snmp",
        },
        "poll_seconds": round(time.monotonic() - started, 3),
    })
    device.update(poll_snmp_neighbors(ip, community))
    return device


def poll_device(ip, community, collect_arp=True):
    """Backward-compatible name for the direct collector used by old callers."""
    return poll_device_snmp(ip, community, collect_arp=collect_arp)


def _librenms_ports(client, device):
    columns = "port_id,device_id,ifIndex,ifName,ifDescr,ifAlias,ifOperStatus"
    records = client.get_device_ports(device, columns=columns)
    ifname = {}
    ifoper = {}
    port_by_id = {}
    for record in records:
        port_id = record.get("port_id")
        ifindex = _as_positive_int(record.get("ifIndex"))
        if port_id not in (None, ""):
            port_by_id[str(port_id)] = dict(record)
        if ifindex is None:
            continue
        name = str(
            record.get("ifName") or record.get("ifDescr") or ""
        ).strip()
        if name:
            ifname[ifindex] = name
        status = _if_oper_status_value(record.get("ifOperStatus"))
        if status is not None:
            ifoper[ifindex] = status
    if not ifname:
        raise TopologyDataIncomplete("LibreNMS ports have no usable ifIndex mapping")
    return records, port_by_id, ifname, ifoper


def _librenms_neighbors(client, device, port_by_id):
    links = client.get_device_links(device)
    if not links:
        raise TopologyDataIncomplete("LibreNMS links are empty")
    neighbors = []
    for link in links:
        if _link_is_inactive(link):
            continue
        local = port_by_id.get(str(link.get("local_port_id")))
        local_ifindex = _as_positive_int((local or {}).get("ifIndex"))
        if local_ifindex is None:
            raise TopologyDataIncomplete(
                "LibreNMS link local_port_id cannot be mapped to ifIndex"
            )
        neighbors.append({
            "protocol": str(link.get("protocol") or "xdp").strip().lower(),
            "active": link.get("active"),
            "local_ifindex": local_ifindex,
            "local_port": str(
                (local or {}).get("ifName") or (local or {}).get("ifDescr") or ""
            ).strip(),
            "neighbor_name": str(link.get("remote_hostname") or "").strip(),
            "neighbor_device_id": link.get("remote_device_id"),
            "neighbor_port_id": link.get("remote_port_id"),
            "neighbor_port": str(link.get("remote_port") or "").strip(),
        })
    return neighbors


def _librenms_ifstack(client, device, port_by_id):
    mappings = client.get_device_port_stack(device, valid_mappings=True)
    ifstack = {}
    for mapping in mappings:
        status = mapping.get("ifStackStatus")
        if status not in (None, "") and str(status).strip().lower() not in {
            "1", "active", "up"
        }:
            continue
        high = port_by_id.get(str(mapping.get("port_id_high")))
        low = port_by_id.get(str(mapping.get("port_id_low")))
        high_ifindex = _as_positive_int((high or {}).get("ifIndex"))
        low_ifindex = _as_positive_int((low or {}).get("ifIndex"))
        if high_ifindex is None or low_ifindex is None or high_ifindex == low_ifindex:
            continue
        bucket = ifstack.setdefault(high_ifindex, [])
        if low_ifindex not in bucket:
            bucket.append(low_ifindex)
    return ifstack


def _log_librenms_fallback(ip, component, exc):
    print(
        f"[WARN] {ip}: LibreNMS {component} unavailable "
        f"({type(exc).__name__}); applying configured fallback",
        file=sys.stderr,
    )


def poll_device_librenms(ip, community, client, collect_arp=True, mode="hybrid"):
    """Collect one device with per-component LibreNMS/SNMP fallback."""
    started = time.monotonic()
    try:
        metadata = client.resolve_device(ip)
    except LibreNMSError as exc:
        _log_librenms_fallback(ip, "device", exc)
        if mode == "hybrid":
            return poll_device_snmp(ip, community, collect_arp=collect_arp)
        failed = _empty_device(ip)
        failed["poll_seconds"] = round(time.monotonic() - started, 3)
        return failed

    poll_freshness = librenms_freshness(
        metadata.get("last_polled"), topology_librenms_poll_max_age()
    )
    discovery_freshness = librenms_freshness(
        metadata.get("last_discovered"), topology_librenms_discovery_max_age()
    )
    if poll_freshness == "stale":
        exc = TopologyDataIncomplete("LibreNMS poll data is stale")
        _log_librenms_fallback(ip, "ports", exc)
        if mode == "hybrid":
            return poll_device_snmp(ip, community, collect_arp=collect_arp)
        failed = _empty_device(ip)
        failed.update({
            "device_id": metadata.get("device_id"),
            "sysname": metadata.get("sysName") or metadata.get("hostname") or "",
            "freshness": {"poll": poll_freshness, "discovery": discovery_freshness},
            "poll_seconds": round(time.monotonic() - started, 3),
        })
        return failed

    try:
        _records, port_by_id, ifname, ifoper = _librenms_ports(client, metadata)
    except (LibreNMSError, TopologyDataIncomplete) as exc:
        _log_librenms_fallback(ip, "ports", exc)
        if mode == "hybrid":
            return poll_device_snmp(ip, community, collect_arp=collect_arp)
        failed = _empty_device(ip)
        failed.update({
            "device_id": metadata.get("device_id"),
            "sysname": metadata.get("sysName") or metadata.get("hostname") or "",
            "freshness": {"poll": poll_freshness, "discovery": discovery_freshness},
            "poll_seconds": round(time.monotonic() - started, 3),
        })
        return failed

    result = _empty_device(ip)
    result.update({
        "device_id": metadata.get("device_id"),
        "sysname": metadata.get("sysName") or metadata.get("hostname") or ip,
        "ifname": ifname,
        "ifoper": ifoper,
        "port_by_id": port_by_id,
        "freshness": {"poll": poll_freshness, "discovery": discovery_freshness},
        "source": {"ports": "librenms", "links": "librenms", "lag": "librenms"},
    })
    if collect_arp:
        result["arp"] = poll_snmp_arp(ip, community, ifname)

    if discovery_freshness == "stale":
        if mode == "hybrid":
            result.update(poll_snmp_neighbors(ip, community))
            result["ifstack"] = poll_snmp_lag(ip, community, ifname, ifoper)
            result["source"].update({"links": "direct-snmp", "lag": "direct-snmp"})
        else:
            result["source"].update({"links": "unavailable", "lag": "unavailable"})
        result["poll_seconds"] = round(time.monotonic() - started, 3)
        return result

    try:
        result["neighbors"] = _librenms_neighbors(client, metadata, port_by_id)
    except (LibreNMSError, TopologyDataIncomplete) as exc:
        _log_librenms_fallback(ip, "links", exc)
        if mode == "hybrid":
            result.update(poll_snmp_neighbors(ip, community))
            result["source"]["links"] = "direct-snmp"
        else:
            result["source"]["links"] = "unavailable"

    try:
        result["ifstack"] = _librenms_ifstack(client, metadata, port_by_id)
    except LibreNMSError as exc:
        _log_librenms_fallback(ip, "port_stack", exc)
        if mode == "hybrid":
            result["ifstack"] = poll_snmp_lag(ip, community, ifname, ifoper)
            result["source"]["lag"] = "direct-snmp"
        else:
            result["source"]["lag"] = "unavailable"
    else:
        if incomplete_active_aggregate_ifindexes(ifname, ifoper, result["ifstack"]):
            if mode == "hybrid":
                result["ifstack"] = poll_snmp_lag(
                    ip, community, ifname, ifoper, initial=result["ifstack"]
                )
                result["source"]["lag"] = "hybrid"
            else:
                result["source"]["lag"] = "incomplete"

    result["poll_seconds"] = round(time.monotonic() - started, 3)
    return result


def collect_device_by_source(ip, community, collect_arp, mode, client=None,
                             librenms_ready=False):
    """Select one device's collector without introducing a global fallback."""
    if mode == "direct-snmp" or (mode == "hybrid" and not librenms_ready):
        return poll_device_snmp(ip, community, collect_arp=collect_arp)
    if mode == "librenms" and not librenms_ready:
        return _empty_device(ip)
    return poll_device_librenms(
        ip,
        community,
        client,
        collect_arp=collect_arp,
        mode=mode,
    )


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


def build_device_id_index(devices):
    """Map LibreNMS device_id values without ever treating them as IP/ifIndex."""
    index = {}
    for ip, device in devices.items():
        device_id = device.get("device_id")
        if device_id not in (None, ""):
            index.setdefault(str(device_id), ip)
    return index


def configured_core_neighbor_ip(devices, neighbor_name):
    """Map a core CDP SVI alias to the console's canonical core IP."""
    full_name = str(neighbor_name or "").strip().lower()
    names = {name for name in (full_name, normalize_hostname(full_name)) if name}
    if not names:
        return None
    core_ips = {
        ip
        for entry in os.environ.get("CORE_SWITCH_PING", "").split(",")
        for ip in expand_device_entry(entry)
    }
    matches = []
    for ip in core_ips:
        sysname = (devices.get(ip) or {}).get("sysname")
        aliases = {
            name for name in (
                str(sysname or "").strip().lower(), normalize_hostname(sysname)
            ) if name
        }
        if names & aliases:
            matches.append(ip)
    return matches[0] if len(matches) == 1 else None


def canonical_edge_key(edge):
    a = (edge["from_ip"] or "", edge["from_ifindex"] or 0)
    b = (edge["to_ip"] or "", edge["to_ifindex"] or 0)
    return tuple(sorted([a, b]))


def merge_edge(edges_by_key, edge):
    """Insert an edge, or backfill missing fields on an existing one (so an LLDP
    and a CDP view of the same link, or both directions, collapse into one)."""
    if not any(
        _has_physical_endpoint_evidence(edge, side)
        for side in ("from", "to")
    ):
        return
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


def _aggregate_member_details(device, endpoint_ifindex, endpoint_port=None):
    """Return (aggregate name, configured member names) for one edge endpoint."""
    ifnames = device.get("ifname", {})
    if endpoint_ifindex is None and endpoint_port:
        endpoint_ifindex = resolve_ifindex_by_name(endpoint_port, ifnames)
    if endpoint_ifindex is None:
        return "", []
    for higher, lowers in device.get("ifstack", {}).items():
        aggregate_name = ifnames.get(higher, "")
        if not normalize_port_name(aggregate_name).startswith("agg"):
            continue
        if endpoint_ifindex != higher and endpoint_ifindex not in lowers:
            continue
        members = [
            ifnames[index]
            for index in lowers
            if ifnames.get(index)
        ]
        members = sorted(
            dict.fromkeys(members),
            key=lambda name: [int(part) for part in re.findall(r"\d+", name)] or [0],
        )
        return aggregate_name, members
    return "", []


def enrich_aggregate_members(edges, devices):
    """Attach all local LAG members to an edge without inventing member pairing.

    Some switches (notably Catalyst 1000 variants) report two physical members
    locally but repeat only one remote port in LLDP/CDP. Keeping per-endpoint
    member arrays lets the UI show both truthful sides and lets alert correlation
    map either member to the same peer.
    """
    enriched = []
    for source in edges:
        edge = dict(source)
        for side in ("from", "to"):
            device = devices.get(edge.get(f"{side}_ip")) or {}
            aggregate, members = _aggregate_member_details(
                device,
                edge.get(f"{side}_ifindex"),
                edge.get(f"{side}_port"),
            )
            if aggregate:
                edge[f"{side}_aggregate_port"] = aggregate
            if members:
                edge[f"{side}_member_ports"] = members
        enriched.append(edge)
    return enriched


def build_edges(devices, name_index):
    edges_by_key = {}
    placeholder_neighbors = []
    device_id_index = build_device_id_index(devices)

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
                # An ordinary neighbor's advertised address is authoritative;
                # hostname fallback could turn a same-named AP into a switch.
                # The explicitly configured core is the sole exception because
                # Cisco may advertise one of its other gateway SVIs over CDP.
                neighbor_ip = addr_ip if addr_ip in devices else configured_core_neighbor_ip(
                    devices, neighbor_name
                )
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

        # --- Unified LibreNMS xDP neighbors.  local_port_id/remote_port_id
        # were resolved through each device's port table by the adapter; they
        # are never interpreted as IF-MIB ifIndex values directly. ---
        for neighbor in device.get("neighbors", []):
            if _link_is_inactive(neighbor):
                continue
            neighbor_name = str(neighbor.get("neighbor_name") or "").strip()
            neighbor_device_id = neighbor.get("neighbor_device_id")
            neighbor_ip = None
            if neighbor_device_id not in (None, ""):
                neighbor_ip = device_id_index.get(str(neighbor_device_id))
            if neighbor_ip is None:
                neighbor_ip = name_index.get(neighbor_name.lower()) or \
                              name_index.get(normalize_hostname(neighbor_name))
            if neighbor_ip is None:
                neighbor_ip = configured_core_neighbor_ip(devices, neighbor_name)

            local_ifindex = _as_positive_int(neighbor.get("local_ifindex"))
            local_port_name = str(neighbor.get("local_port") or "").strip()
            if local_ifindex is None and local_port_name:
                local_ifindex = resolve_ifindex_by_name(
                    local_port_name, device.get("ifname", {})
                )
            if local_ifindex is not None:
                local_port_name = device.get("ifname", {}).get(
                    local_ifindex, local_port_name
                )

            remote_port_name = str(neighbor.get("neighbor_port") or "").strip()
            remote_ifindex = None
            remote = devices.get(neighbor_ip) if neighbor_ip else None
            remote_port_id = neighbor.get("neighbor_port_id")
            if remote and remote_port_id not in (None, ""):
                remote_record = remote.get("port_by_id", {}).get(str(remote_port_id))
                remote_ifindex = _as_positive_int(
                    (remote_record or {}).get("ifIndex")
                )
                if remote_ifindex is not None:
                    remote_port_name = remote.get("ifname", {}).get(
                        remote_ifindex,
                        (remote_record or {}).get("ifName") or remote_port_name,
                    )
            if remote and remote_ifindex is None and remote_port_name:
                remote_ifindex = resolve_ifindex_by_name(
                    remote_port_name, remote.get("ifname", {})
                )
                if remote_ifindex is not None:
                    remote_port_name = remote.get("ifname", {}).get(
                        remote_ifindex, remote_port_name
                    )

            if neighbor_ip is None:
                placeholder_neighbors.append({
                    "from_ip": ip,
                    "from_port": local_port_name,
                    "neighbor_name": neighbor_name,
                    "neighbor_port": remote_port_name,
                })
                continue
            if not interface_is_usable(device, local_ifindex):
                continue
            if remote and not interface_is_usable(remote, remote_ifindex):
                continue
            merge_edge(edges_by_key, {
                "from_ip": ip,
                "from_sysname": device.get("sysname"),
                "from_port": local_port_name,
                "from_ifindex": local_ifindex,
                "to_ip": neighbor_ip,
                "to_sysname": neighbor_name,
                "to_port": remote_port_name,
                "to_ifindex": remote_ifindex,
            })

    edges = resolve_endpoint_conflicts(list(edges_by_key.values()))
    return enrich_aggregate_members(edges, devices), placeholder_neighbors


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


def _server_snmpget(ip, community, oid):
    with snmp_phase("server"):
        return snmpget(ip, community, oid)


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
        bridge_port = _positive_int(_server_snmpget(ip, query_community, oid))
        if bridge_port is None:
            continue
        # dot1dTpFdbPort/dot1qTpFdbPort returns a BRIDGE-MIB base-port number,
        # not an IF-MIB ifIndex.  Always try the explicit mapping first.  The
        # old direct-number shortcut mapped bridge port 5 to ifIndex 5 on the
        # C9200L, which happens to be VLAN-1002 rather than a physical port.
        for mapping_community in dict.fromkeys((query_community, community)):
            ifindex = _positive_int(_server_snmpget(
                ip,
                mapping_community,
                f"{DOT1D_BASE_PORT_IFINDEX_OID}.{bridge_port}",
            ))
            if ifindex is not None and is_physical_interface_name(
                ifname_map.get(ifindex)
            ):
                return ifindex
        # Some agents use ifIndex directly and do not implement
        # dot1dBasePortIfIndex.  Retain that compatibility only when the
        # colliding IF-MIB row is unmistakably a physical Ethernet port.
        if is_physical_interface_name(ifname_map.get(bridge_port)):
            return bridge_port
    return None


def discover_server_edges(devices, edges, servers, community, cached_edges=None):
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
    cached_switch_by_server = {
        str(edge.get("to_ip") or ""): str(edge.get("from_ip") or "")
        for edge in (cached_edges or [])
        if edge.get("source") == "fdb" and edge.get("to_ip") and edge.get("from_ip")
    }
    cached_record_by_server = {}
    for edge in cached_edges or []:
        if edge.get("source") != "fdb":
            continue
        if edge.get("to_ip") in servers:
            cached_server_ip = str(edge.get("to_ip") or "")
        elif edge.get("from_ip") in servers:
            cached_server_ip = str(edge.get("from_ip") or "")
        else:
            continue
        cached_mac = normalize_mac(edge.get("server_mac"))
        if not cached_mac or cached_server_ip in cached_record_by_server:
            continue
        try:
            cached_vlan = int(edge.get("server_vlan"))
        except (TypeError, ValueError):
            cached_vlan = None
        cached_record_by_server[cached_server_ip] = {
            "mac": cached_mac,
            "vlan": cached_vlan,
        }
    found = []

    for server_ip, server_name in servers.items():
        records = arp_by_server.get(server_ip, [])
        unique_records = {
            (record["mac"], record.get("vlan"))
            for record in records if record.get("mac")
        }
        if not unique_records:
            cached_record = cached_record_by_server.get(server_ip)
            if cached_record:
                unique_records.add((
                    cached_record["mac"], cached_record.get("vlan")
                ))
                print(
                    f"[INFO] server {server_name} ({server_ip}): no current "
                    "ARP entry; verifying cached MAC through FDB",
                    file=sys.stderr,
                )
            else:
                print(
                    f"[WARN] server {server_name} ({server_ip}): no ARP entry "
                    "and no cached MAC; keeping core fallback",
                    file=sys.stderr,
                )
                continue

        def collect_candidates(selected_switches):
            candidates = []
            tasks = {}
            poll_workers = _topology_poll_workers()
            task_count = max(1, len(selected_switches) * len(unique_records))
            with ThreadPoolExecutor(max_workers=min(poll_workers, task_count)) as executor:
                for switch_ip, device in selected_switches.items():
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
                        print(
                            f"[WARN] FDB lookup {switch_ip} {server_ip} failed "
                            f"({type(exc).__name__})",
                            file=sys.stderr,
                        )
                        continue
                    if ifindex is None:
                        continue
                    switch_device = switch_devices[switch_ip]
                    port_name = switch_device.get("ifname", {}).get(ifindex)
                    candidates.append({
                        "switch_ip": switch_ip,
                        "ifindex": ifindex,
                        "port_name": port_name,
                        "mac": mac,
                        "vlan": vlan,
                        "is_uplink": (switch_ip, ifindex) in switch_link_endpoints,
                        "is_aggregate": normalize_port_name(port_name).startswith("agg"),
                        "is_physical": is_physical_interface_name(port_name),
                        "depth": depths.get(switch_ip, -1),
                    })
            return candidates

        # Stable projects normally keep servers on one access port for days.
        # Verify the last confirmed owner first (one exact FDB lookup) and only
        # fan out across every switch when that proof disappeared or moved.
        cached_switch = cached_switch_by_server.get(server_ip)
        preferred = {
            cached_switch: switch_devices[cached_switch]
        } if cached_switch in switch_devices else {}
        candidates = collect_candidates(preferred) if preferred else []
        preferred_physical = [
            candidate for candidate in candidates
            if (
                not candidate["is_uplink"] and
                not candidate["is_aggregate"] and
                candidate["is_physical"]
            )
        ]
        if preferred and preferred_physical:
            print(
                f"[INFO] server {server_name} ({server_ip}): cached FDB owner verified; "
                "skipped full switch fan-out",
                file=sys.stderr,
            )
        else:
            # The cached owner was already queried above. Keep any transit
            # evidence it returned and fan out only to the remaining switches,
            # avoiding an immediate duplicate request to the same old device.
            remaining_switches = {
                ip: device for ip, device in switch_devices.items()
                if ip not in preferred
            }
            candidates.extend(collect_candidates(remaining_switches))

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
            if not candidate["is_aggregate"] and candidate["is_physical"]
        ]
        if not physical_candidates:
            print(
                f"[WARN] server {server_name} ({server_ip}): MAC was learned "
                "only on logical/unconfirmed interfaces; keeping core fallback",
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
            # The monitoring host can reach a directly attached server VLAN
            # without traversing the monitored core.  Persist the last
            # confirmed IP->MAC/VLAN observation so later topology cycles can
            # verify the physical FDB port even after the core's ARP ages out.
            "server_mac": best["mac"],
            "server_vlan": best["vlan"],
        })
        print(
            f"[INFO] server {server_name} ({server_ip}) attached to "
            f"{best['switch_ip']} {parent.get('ifname', {}).get(best['ifindex']) or best['ifindex']}",
            file=sys.stderr,
        )
    return found


def load_cached_edges(path):
    """Read the last emitted topology without making collection depend on it."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, TypeError):
        return []
    return data if isinstance(data, list) else []


def _edge_cache_key(edge):
    def endpoint(side):
        ip = str(edge.get(f"{side}_ip") or "").strip()
        ifindex = edge.get(f"{side}_ifindex")
        identity = (
            f"i:{ifindex}"
            if ifindex not in (None, "")
            else f"p:{normalize_port_name(edge.get(f'{side}_port'))}"
        )
        return ip, identity

    return tuple(sorted((endpoint("from"), endpoint("to"))))


def _edge_endpoint_identities(edge, side):
    """Return every usable identity for one physical edge endpoint.

    Prefer ifIndex for cache keys, but also retain the normalized port name as
    a secondary identity.  Some Catalyst observations resolve the local
    ifIndex in one direction while the reverse LLDP/CDP row exposes only a
    port description.
    """
    ip = str(edge.get(f"{side}_ip") or "").strip()
    if not ip:
        return set()
    identities = set()
    ifindex = edge.get(f"{side}_ifindex")
    if ifindex not in (None, ""):
        identities.add((ip, f"i:{ifindex}"))
    port = normalize_port_name(edge.get(f"{side}_port"))
    if port:
        identities.add((ip, f"p:{port}"))
    return identities


def _active_aggregate_member_identities(edge, side, devices):
    """Return confirmed-up physical members advertised on one live edge.

    A Catalyst can expose one LLDP/CDP row for a multi-member Port-channel.
    Aggregate enrichment restores the other member names, while ifOperStatus
    tells us whether each restored member is currently usable.  These
    identities are strong enough to retire an older per-member cache row for
    the same device pair, but a down/unknown member deliberately is not.
    """
    ip = str(edge.get(f"{side}_ip") or "").strip()
    members = edge.get(f"{side}_member_ports") or []
    if not ip or not members or not edge.get(f"{side}_aggregate_port"):
        return set()
    device = (devices or {}).get(ip) or {}
    ifnames = device.get("ifname", {})
    ifoper = device.get("ifoper", {})
    identities = set()
    for member in members:
        ifindex = resolve_ifindex_by_name(member, ifnames)
        if ifindex is None or ifoper.get(ifindex) != 1:
            continue
        identities.add((ip, f"i:{ifindex}"))
        normalized = normalize_port_name(ifnames.get(ifindex) or member)
        if normalized:
            identities.add((ip, f"p:{normalized}"))
    return identities


def retain_cached_network_edges(live_edges, cached_edges, configured_device_ips,
                                now=None, retention_seconds=24 * 60 * 60,
                                devices=None):
    """Keep missing confirmed LLDP/CDP edges long enough to diagnose outages.

    Live observations replace matching cache entries. A cached edge is dropped
    immediately when one of its resolved physical endpoints is now occupied by
    a different live peer; otherwise it is retained as stale for the configured
    window. Server/FDB ownership has a separate durable ledger and is excluded.
    """
    now = time.time() if now is None else float(now)
    retention_seconds = max(0, int(retention_seconds))
    configured = set(configured_device_ips or [])
    live_keys = {_edge_cache_key(edge) for edge in live_edges}
    live_endpoint_identities = set()
    active_aggregate_identities = {}
    occupied = set()
    output = []
    for source in live_edges:
        edge = dict(source)
        edge["last_seen"] = now
        edge["stale"] = False
        output.append(edge)
        for side in ("from", "to"):
            live_endpoint_identities.update(_edge_endpoint_identities(edge, side))
            pair = frozenset((
                str(edge.get("from_ip") or "").strip(),
                str(edge.get("to_ip") or "").strip(),
            ))
            aggregate_members = _active_aggregate_member_identities(
                edge, side, devices
            )
            if aggregate_members:
                active_aggregate_identities.setdefault(pair, set()).update(
                    aggregate_members
                )
            ip = str(edge.get(f"{side}_ip") or "").strip()
            ifindex = edge.get(f"{side}_ifindex")
            if ip and ifindex not in (None, ""):
                occupied.add((ip, str(ifindex)))

    for source in cached_edges or []:
        if not isinstance(source, dict) or source.get("source") == "fdb":
            continue
        left = str(source.get("from_ip") or "").strip()
        right = str(source.get("to_ip") or "").strip()
        if not left or not right or left not in configured or right not in configured:
            continue
        if _edge_cache_key(source) in live_keys:
            continue
        # Retention is for previously confirmed physical links.  Do not turn
        # an unresolved LLDP chassis/MAC row into a 24-hour yellow cable when
        # neither endpoint has any usable interface evidence.
        if not any(
            _has_physical_endpoint_evidence(source, side)
            for side in ("from", "to")
        ):
            continue
        cached_endpoint_identities = [
            _edge_endpoint_identities(source, side)
            for side in ("from", "to")
        ]
        pair = frozenset((left, right))
        # A single LLDP/CDP row can represent a complete, healthy
        # Port-channel.  Once IF-MIB confirms that an old per-member cache row
        # is one of that live aggregate's operational members, it is a shadow,
        # not a lost link.  Do not keep it yellow for the retention window.
        # Members that are actually down are absent from this set and continue
        # through the normal 24-hour stale retention path below.
        if any(
            identities & active_aggregate_identities.get(pair, set())
            for identities in cached_endpoint_identities
        ):
            continue
        # A one-sided cached row is weak topology evidence.  If its only known
        # endpoint is already present in a live observation, the row is merely
        # an incomplete reverse LLDP/CDP shadow of the current link.  Retaining
        # it made an otherwise healthy pair yellow for 24 hours.  Fully
        # identified parallel members remain eligible for retention so a real
        # degraded EtherChannel is still shown as a warning.
        if (
            any(not identities for identities in cached_endpoint_identities)
            and any(
                identities & live_endpoint_identities
                for identities in cached_endpoint_identities
            )
        ):
            continue
        conflicts = False
        for side in ("from", "to"):
            ip = str(source.get(f"{side}_ip") or "").strip()
            ifindex = source.get(f"{side}_ifindex")
            if ip and ifindex not in (None, "") and (ip, str(ifindex)) in occupied:
                conflicts = True
                break
        if conflicts:
            continue
        try:
            last_seen = float(source.get("last_seen", now))
        except (TypeError, ValueError):
            last_seen = now
        if now - last_seen > retention_seconds:
            continue
        edge = dict(source)
        edge["last_seen"] = last_seen
        edge["stale"] = True
        output.append(edge)
    return output


def _server_ip_for_edge(edge, servers):
    """Return the configured server endpoint carried by one topology edge."""
    for ip in (edge.get("from_ip"), edge.get("to_ip")):
        if ip in servers:
            return ip
    return None


def merge_cached_server_ledgers(primary_edges, fallback_edges, servers):
    """Fill holes in the durable server attachment ledger from edges.json.

    Older deployments used the live topology snapshot as the attachment
    memory.  After the dedicated ledger was introduced, migration only ran
    when that file was completely empty.  A partially written/migrated ledger
    therefore lost any server missing from it (commonly an offline server),
    even though its last confirmed FDB edge was still present in edges.json.
    Prefer the dedicated ledger per server and supplement only missing entries
    from the live snapshot.
    """
    merged = []
    linked_servers = set()
    for collection in (primary_edges, fallback_edges):
        for source in collection:
            if source.get("source") != "fdb":
                continue
            server_ip = _server_ip_for_edge(source, servers)
            if not server_ip or server_ip in linked_servers:
                continue
            merged.append(dict(source))
            linked_servers.add(server_ip)
    return merged


def preserve_cached_server_edges(edges, cached_edges, servers):
    """Keep a confirmed server attachment through transient ARP/FDB misses.

    A fresh FDB result is authoritative.  The cached edge is considered only
    when the current cycle could not locate that server.  Do not require its
    previous parent to appear in this cycle's auto-discovery list: a single
    failed ICMP/SNMP discovery used to remove the attachment permanently and
    made servers jump back beside the core until a later FDB lookup succeeded.

    Server membership is the lifecycle authority; removing a server from
    SERVER_PING removes its cached edge.
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
                parent_port = edge.get("from_port")
            elif edge.get("from_ip") == server_ip:
                parent_ip = edge.get("to_ip")
                parent_port = edge.get("to_port")
            else:
                continue
            # Older collectors could confuse a BRIDGE-MIB base-port number
            # with an IF-MIB ifIndex and cache an SVI such as VLAN-1002 as the
            # server's physical attachment.  Missing legacy labels remain
            # preservable, but an explicitly non-physical label is invalid.
            if parent_port and not is_physical_interface_name(parent_port):
                print(
                    f"[WARN] server {server_name} ({server_ip}): discarding "
                    f"non-physical cached attachment {parent_ip} {parent_port}",
                    file=sys.stderr,
                )
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


def _run_collection():
    cycle_started = time.monotonic()
    reset_collection_stats()
    data_source = topology_data_source()
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

    servers = parse_named_ipv4_targets(os.environ.get("SERVER_PING", ""))

    arp_raw = os.environ.get("TOPOLOGY_ARP_DEVICES", "").strip()
    arp_device_ips = set()
    if arp_raw:
        for entry in arp_raw.split(","):
            arp_device_ips.update(expand_device_entry(entry))
    else:
        for env_var in ("CORE_SWITCH_PING", "FIREWALL_PING"):
            arp_device_ips.update(_env_target_ips(env_var))
    if not arp_device_ips:
        arp_device_ips.update(device_ips)
    print(
        f"[INFO] collecting topology on {len(device_ips)} device(s), "
        f"source={data_source}; ARP only on {len(arp_device_ips)} L3 device(s)",
        file=sys.stderr,
    )
    librenms = None
    librenms_ready = False
    if data_source != "direct-snmp":
        librenms = LibreNMSClient()
        try:
            # One cached device inventory is shared by all per-device adapters.
            librenms.list_devices()
            librenms_ready = True
        except LibreNMSError as exc:
            print(
                f"[WARN] LibreNMS device inventory unavailable "
                f"({type(exc).__name__}); applying {data_source} policy",
                file=sys.stderr,
            )
    devices = {}
    poll_workers = _topology_poll_workers()
    with ThreadPoolExecutor(max_workers=min(poll_workers, len(device_ips))) as executor:
        futures = {}
        for ip in device_ips:
            # ARP is solely for server attachment.  With no configured server
            # it has no consumer and must not spoil a zero-SNMP adjacency run.
            collect_arp = bool(servers) and ip in arp_device_ips
            future = executor.submit(
                collect_device_by_source,
                ip,
                community,
                collect_arp,
                data_source,
                librenms,
                librenms_ready,
            )
            futures[future] = ip
        for future in as_completed(futures):
            ip = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                print(
                    f"[WARN] poll {ip} failed ({type(exc).__name__})",
                    file=sys.stderr,
                )
                continue
            devices[ip] = result
            unified = result.get("neighbors", [])
            lldp_n = len(result.get("rem_sys", {})) + sum(
                1 for item in unified if item.get("protocol") == "lldp"
            )
            cdp_n = len(result.get("cdp_device_id", {})) + sum(
                1 for item in unified if item.get("protocol") == "cdp"
            )
            poll_seconds = float(result.get("poll_seconds", 0))
            source = result.get("source", {})
            freshness = result.get("freshness", {})
            print(
                f"[INFO] {ip} ports={source.get('ports', 'unknown')} "
                f"links={source.get('links', 'unknown')} "
                f"lag={source.get('lag', 'unknown')} "
                f"freshness=poll:{freshness.get('poll', 'unknown')},"
                f"discovery:{freshness.get('discovery', 'unknown')}",
                file=sys.stderr,
            )
            if lldp_n or cdp_n:
                print(
                    f"[INFO] {ip}: sysname='{result['sysname']}' neighbors "
                    f"lldp={lldp_n} cdp={cdp_n} collection={poll_seconds:.1f}s",
                    file=sys.stderr,
                )
            else:
                print(
                    f"[WARN] {ip}: no LLDP/CDP neighbors, collection={poll_seconds:.1f}s "
                    "(check LibreNMS discovery, 'lldp run'/'cdp run', and access)",
                    file=sys.stderr,
                )

    source_summary = {"librenms": 0, "hybrid": 0, "direct-snmp": 0}
    for device in devices.values():
        sources = set((device.get("source") or {}).values())
        if sources == {"librenms"}:
            bucket = "librenms"
        elif sources == {"direct-snmp"}:
            bucket = "direct-snmp"
        else:
            bucket = "librenms" if data_source == "librenms" else "hybrid"
        source_summary[bucket] += 1
    print(
        "[INFO] source summary: "
        f"librenms={source_summary['librenms']} "
        f"hybrid={source_summary['hybrid']} "
        f"direct-snmp={source_summary['direct-snmp']}",
        file=sys.stderr,
    )

    edges_path = os.path.join(output_dir, "edges.json")
    attachments_path = os.path.join(output_dir, "server-attachments.json")
    cached_edges = load_cached_edges(edges_path)
    # Attachments have their own durable ledger.  edges.json is a live snapshot
    # and can legitimately be incomplete after a collector restart or a weak
    # SNMP cycle; it must not be the only memory of physical server ownership.
    name_index = build_name_index(devices)
    edges, placeholders = build_edges(devices, name_index)
    loaded_attachments = load_cached_edges(attachments_path)
    cached_attachments = merge_cached_server_ledgers(
        loaded_attachments,
        cached_edges,
        servers,
    )
    loaded_server_ips = {
        _server_ip_for_edge(edge, servers)
        for edge in loaded_attachments
        if edge.get("source") == "fdb"
    }
    recovered_attachments = sum(
        1 for edge in cached_attachments
        if _server_ip_for_edge(edge, servers) not in loaded_server_ips
    )
    if recovered_attachments > 0:
        print(
            f"[INFO] recovered {recovered_attachments} missing server "
            "attachment(s) from the last topology snapshot",
            file=sys.stderr,
        )
    # Always run the exact ARP+FDB ownership lookup.  A weaker LLDP/CDP edge
    # involving the same address must not suppress authoritative discovery.
    fresh_server_edges = discover_server_edges(
        devices,
        edges,
        servers,
        community,
        cached_attachments,
    )
    confirmed_server_edges = list(fresh_server_edges)
    confirmed_server_edges.extend(preserve_cached_server_edges(
        fresh_server_edges,
        cached_attachments,
        servers,
    ))
    edges = replace_server_edges(edges, confirmed_server_edges, servers)
    try:
        edge_retention = int(os.environ.get(
            "TOPOLOGY_EDGE_RETENTION_SECONDS", str(24 * 60 * 60)
        ) or 24 * 60 * 60)
    except ValueError:
        edge_retention = 24 * 60 * 60
    network_edges = [edge for edge in edges if edge.get("source") != "fdb"]
    server_edges = [edge for edge in edges if edge.get("source") == "fdb"]
    edges = retain_cached_network_edges(
        network_edges,
        cached_edges,
        device_ips,
        retention_seconds=edge_retention,
        devices=devices,
    ) + server_edges
    write_json_atomic(edges_path, edges, sort_keys=True)
    write_json_atomic(attachments_path, confirmed_server_edges, sort_keys=True)

    stats = collection_stats_snapshot()
    api_requests = librenms.request_count if librenms is not None else 0
    print(
        f"[INFO] collection stats: api_requests={api_requests} "
        f"snmp_walks={stats['direct_snmp_walks']} "
        f"snmp_gets={stats['direct_snmp_gets']}",
        file=sys.stderr,
    )
    print(
        f"[INFO] server attachment SNMP: walks={stats['server_snmp_walks']} "
        f"gets={stats['server_snmp_gets']}",
        file=sys.stderr,
    )

    print(
        f"[INFO] wrote {len(edges)} edge(s), "
        f"cycle={time.monotonic() - cycle_started:.1f}s",
        file=sys.stderr,
    )
    if placeholders:
        print(f"[WARN] {len(placeholders)} neighbor(s) could not be matched to a configured device IP:", file=sys.stderr)
        for entry in placeholders[:10]:
            print(f"         {entry['from_ip']} {entry['from_port']} -> {entry['neighbor_name']} {entry['neighbor_port']}", file=sys.stderr)
    return 0


def main():
    """Serialize collectors so a pre-update cycle cannot overwrite new data."""
    if fcntl is None:
        return _run_collection()
    output_dir = os.environ.get(
        "TOPOLOGY_OUTPUT_DIR", "/etc/prometheus/targets/topology"
    )
    os.makedirs(output_dir, exist_ok=True)
    lock_path = os.path.join(output_dir, ".collector.lock")
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(
                "[INFO] another topology collection is active; waiting for it",
                file=sys.stderr,
            )
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        return _run_collection()


if __name__ == "__main__":
    sys.exit(main())
