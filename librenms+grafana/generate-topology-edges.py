#!/usr/bin/env python3
"""
Collect LLDP/CDP adjacency for every configured infrastructure device to build
the real network graph.  LibreNMS discovery data is preferred by default and
direct SNMP remains the bounded per-device/per-component fallback.  Emit:

  edges.json              (consumed by the bigscreen /topology page)
  server-attachments.json (durable last-confirmed server/FDB locations)
  topology-diagnostics.json (current-cycle resolver diagnostics)

Env vars:
  TOPOLOGY_DATA_SOURCE       hybrid (default), librenms, or direct-snmp.
  TOPOLOGY_LIBRENMS_POLL_MAX_AGE_SECONDS
                             max explicit device last_polled age (default: 600).
  TOPOLOGY_LIBRENMS_DISCOVERY_MAX_AGE_SECONDS
                             max explicit last_discovered age (default: 28800).
  TOPOLOGY_SERVER_ATTACHMENT_SOURCE
                             hybrid (default), librenms, or direct-snmp.
  TOPOLOGY_LIBRENMS_ARP_MAX_AGE_SECONDS
                             max explicit ARP evidence age (default: 900).
  TOPOLOGY_LIBRENMS_FDB_MAX_AGE_SECONDS
                             max explicit FDB evidence age (default: 900).
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
from lag_ownership import resolve_lag_ownership

SYS_NAME_OID = "1.3.6.1.2.1.1.5.0"
IF_NAME_OID = "1.3.6.1.2.1.31.1.1.1.1"
IF_OPER_STATUS_OID = "1.3.6.1.2.1.2.2.1.8"
IF_STACK_STATUS_OID = "1.3.6.1.2.1.31.1.2.1.3"
# CISCO-PAGP-MIB pagpGroupIfIndex. Despite the MIB name, Cisco also exposes
# manually configured/static EtherChannels here (pagpEthcOperationMode=manual).
PAGP_GROUP_IFINDEX_OID = "1.3.6.1.4.1.9.9.98.1.1.1.1.8"
# IEEE8023-LAG-MIB dot3adAggPortAttachedAggID for LACP member -> aggregator.
DOT3AD_ATTACHED_AGG_ID_OID = "1.2.840.10006.300.43.1.2.1.1.13"
DOT3AD_AGG_ACTOR_ADMIN_KEY_OID = "1.2.840.10006.300.43.1.1.1.1.6"
DOT3AD_PORT_ACTOR_ADMIN_KEY_OID = "1.2.840.10006.300.43.1.2.1.1.4"
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
SERVER_ATTACHMENT_SOURCES = frozenset({"hybrid", "librenms", "direct-snmp"})
DEFAULT_LIBRENMS_POLL_MAX_AGE_SECONDS = 600
DEFAULT_LIBRENMS_DISCOVERY_MAX_AGE_SECONDS = 8 * 60 * 60
DEFAULT_LIBRENMS_ARP_MAX_AGE_SECONDS = 15 * 60
DEFAULT_LIBRENMS_FDB_MAX_AGE_SECONDS = 15 * 60

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


def topology_server_attachment_source():
    value = os.environ.get(
        "TOPOLOGY_SERVER_ATTACHMENT_SOURCE", "hybrid"
    ).strip().lower()
    if value in SERVER_ATTACHMENT_SOURCES:
        return value
    print(
        f"[WARN] unsupported TOPOLOGY_SERVER_ATTACHMENT_SOURCE={value!r}; "
        "using hybrid",
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


def topology_librenms_arp_max_age():
    return _env_nonnegative_seconds(
        "TOPOLOGY_LIBRENMS_ARP_MAX_AGE_SECONDS",
        DEFAULT_LIBRENMS_ARP_MAX_AGE_SECONDS,
    )


def topology_librenms_fdb_max_age():
    return _env_nonnegative_seconds(
        "TOPOLOGY_LIBRENMS_FDB_MAX_AGE_SECONDS",
        DEFAULT_LIBRENMS_FDB_MAX_AGE_SECONDS,
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


def merge_ifstack_claims(*mappings):
    """Union raw ifStack observations before ownership is resolved."""
    merged = {}
    for mapping in mappings:
        for aggregate_ifindex, member_ifindexes in (mapping or {}).items():
            bucket = merged.setdefault(aggregate_ifindex, [])
            for member_ifindex in member_ifindexes:
                if member_ifindex not in bucket:
                    bucket.append(member_ifindex)
    return merged


def parse_indexed_integer(output):
    """Parse a one-index integer SNMP column into ``{row_ifindex: value}``."""
    parsed = {}
    for line in output.strip().split("\n"):
        parts, value = parse_oid_value(line)
        if not parts:
            continue
        try:
            row_index = int(parts[-1])
        except ValueError:
            continue
        parenthesized = re.search(r"\(([0-9]+)\)", value)
        numeric = re.search(r"(?:INTEGER:\s*)?([0-9]+)\s*$", value, re.IGNORECASE)
        match = parenthesized or numeric
        if match:
            parsed[row_index] = int(match.group(1))
    return parsed


def member_to_aggregate(mapping):
    """Invert ``aggregate -> members`` while preserving only unique rows."""
    direct = {}
    for aggregate, members in (mapping or {}).items():
        for member in members or []:
            direct[member] = aggregate
    return direct


def resolve_aggregate_member_maps(
    ifstack,
    pagp=None,
    attached=None,
    aggregate_admin_keys=None,
    physical_admin_keys=None,
):
    """Apply the shared authoritative ownership rules to topology inputs."""
    return resolve_lag_ownership(
        ifstack_claims=ifstack,
        pagp_group_ifindex=member_to_aggregate(pagp),
        attached_aggregate_id=member_to_aggregate(attached),
        aggregate_admin_keys=aggregate_admin_keys,
        physical_admin_keys=physical_admin_keys,
    )


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
    "tengige": "te",
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
    "ethernet": "eth",
    "eth": "eth",
}

_INTERFACE_TYPE_DISPLAY = {
    "fa": "Fa",
    "gi": "Gi",
    "te": "Te",
    "twe": "Twe",
    "fo": "Fo",
    "hu": "Hu",
    "eth": "Eth",
}


def _canonical_topology_port_parts(name):
    """Return a reliable physical identity and clean display label.

    Parenthesized chassis/MAC metadata is removed only when the prefix itself
    is a complete known interface name. A pure MAC therefore remains unknown
    and can never be guessed into a physical port.
    """
    raw = str(name or "").strip()
    if not raw:
        return "", ""
    candidate = raw
    annotation = re.fullmatch(r"(.+?)\s*\(([^()]*)\)\s*", raw)
    if annotation and normalize_mac(annotation.group(2)):
        candidate = annotation.group(1).strip()

    compact = re.sub(r"[\s_-]+", "", candidate.lower())
    aggregate = re.fullmatch(r"(?:portchannel|po)([0-9]+)", compact)
    if aggregate:
        number = str(int(aggregate.group(1)))
        return f"agg:{number}", f"Po{number}"

    physical = re.fullmatch(r"([a-z]+)([0-9]+(?:/[0-9]+)*)", compact)
    if not physical:
        return "", raw
    interface_type = _INTERFACE_TYPE_ALIASES.get(physical.group(1))
    if not interface_type:
        return "", raw
    path = "/".join(str(int(part)) for part in physical.group(2).split("/"))
    display_type = _INTERFACE_TYPE_DISPLAY[interface_type]
    return f"{interface_type}:{path}", f"{display_type}{path}"


def canonical_topology_port_identity(name):
    """Stable typed identity for a recognisable physical/aggregate port."""
    return _canonical_topology_port_parts(name)[0]


def canonical_topology_port_label(name):
    """Clean display label without inventing an interface for unknown data."""
    _, display = _canonical_topology_port_parts(name)
    return display


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


def _candidate_add(index, identity, value):
    identity = str(identity or "").strip()
    if identity and value not in (None, ""):
        index.setdefault(identity, set()).add(value)


def _candidate_sort_key(value):
    if isinstance(value, int):
        return (0, value)
    return (1, str(value))


def _resolution(state, value=None, strategy="", reason="", candidates=None,
                evidence=None):
    """Return the common deterministic device/port resolution contract."""
    ordered = sorted(set(candidates or []), key=_candidate_sort_key)
    return {
        "state": state,
        "value": value if state == "resolved" else None,
        "strategy": strategy,
        "reason": reason,
        "candidates": ordered,
        "evidence": list(evidence or []),
    }


def _safe_ifalias_identity(alias):
    """Accept an ifAlias only when its complete value is an interface name."""
    raw = str(alias or "").strip()
    compact = re.sub(r"[\s_-]+", "", raw.lower())
    if not (
        re.fullmatch(r"(?:portchannel|po)[0-9]+", compact) or
        re.fullmatch(r"[a-z]+[0-9]+(?:/[0-9]+)*", compact)
    ):
        return ""
    return canonical_topology_port_identity(raw)


def build_port_identity_indexes(device):
    """Build candidate-set indexes from already collected IF-MIB/LibreNMS data."""
    indexes = {
        "by_port_id": {},
        "by_ifindex": {},
        "by_exact_ifname": {},
        "by_ifdescr_identity": {},
        "by_typed_identity": {},
        "by_normalized_identity": {},
        "by_safe_ifalias": {},
        "by_unique_suffix": {},
    }

    records = [
        dict(record) for record in device.get("port_records", [])
        if isinstance(record, dict)
    ]
    if not records:
        records = [
            dict(record) for record in device.get("port_by_id", {}).values()
            if isinstance(record, dict)
        ]
    for record in records:
        ifindex = _as_positive_int(record.get("ifIndex"))
        if ifindex is None:
            continue
        _candidate_add(indexes["by_ifindex"], str(ifindex), ifindex)
        _candidate_add(indexes["by_port_id"], record.get("port_id"), ifindex)

        ifname = str(record.get("ifName") or "").strip()
        ifdescr = str(record.get("ifDescr") or "").strip()
        ifalias = str(record.get("ifAlias") or "").strip()
        if ifname:
            _candidate_add(indexes["by_exact_ifname"], ifname.lower(), ifindex)
        if ifdescr:
            _candidate_add(indexes["by_ifdescr_identity"], ifdescr.lower(), ifindex)
        for identity in (ifname, ifdescr):
            typed = canonical_topology_port_identity(identity)
            normalized = normalize_port_name(identity)
            _candidate_add(indexes["by_typed_identity"], typed, ifindex)
            _candidate_add(indexes["by_normalized_identity"], normalized, ifindex)
            lowered = identity.lower()
            _candidate_add(indexes["by_unique_suffix"], lowered, ifindex)
            if "/" in lowered:
                _candidate_add(
                    indexes["by_unique_suffix"], lowered.rsplit("/", 1)[-1], ifindex
                )
        # An alias is identity evidence only when the entire alias is itself a
        # recognisable interface. Descriptive text and token extraction are unsafe.
        safe_alias = _safe_ifalias_identity(ifalias)
        _candidate_add(indexes["by_safe_ifalias"], safe_alias, ifindex)

    for raw_ifindex, ifname in device.get("ifname", {}).items():
        ifindex = _as_positive_int(raw_ifindex)
        if ifindex is None:
            continue
        _candidate_add(indexes["by_ifindex"], str(ifindex), ifindex)
        name = str(ifname or "").strip()
        if not name:
            continue
        _candidate_add(indexes["by_exact_ifname"], name.lower(), ifindex)
        _candidate_add(
            indexes["by_typed_identity"],
            canonical_topology_port_identity(name),
            ifindex,
        )
        _candidate_add(
            indexes["by_normalized_identity"], normalize_port_name(name), ifindex
        )
        lowered = name.lower()
        _candidate_add(indexes["by_unique_suffix"], lowered, ifindex)
        if "/" in lowered:
            _candidate_add(
                indexes["by_unique_suffix"], lowered.rsplit("/", 1)[-1], ifindex
            )
    return indexes


def _resolve_port_layer(indexes, index_name, identity, strategy, evidence):
    if not identity:
        return None
    candidates = indexes.get(index_name, {}).get(str(identity), set())
    ordered = sorted(set(candidates), key=_candidate_sort_key)
    evidence.append({
        "kind": strategy,
        "identity": str(identity),
        "candidates": ordered,
    })
    if len(ordered) == 1:
        return _resolution(
            "resolved", ordered[0], strategy, "unique-port-identity",
            ordered, evidence,
        )
    if len(ordered) > 1:
        return _resolution(
            "ambiguous", strategy=strategy, reason="ambiguous-port-identity",
            candidates=ordered, evidence=evidence,
        )
    return None


def _resolve_port_exact_layer(indexes, identity, evidence):
    if not identity:
        return None
    candidates = set(indexes.get("by_exact_ifname", {}).get(identity, set()))
    candidates.update(
        indexes.get("by_ifdescr_identity", {}).get(identity, set())
    )
    ordered = sorted(candidates, key=_candidate_sort_key)
    evidence.append({
        "kind": "exact-ifname-or-ifdescr",
        "identity": identity,
        "candidates": ordered,
    })
    if len(ordered) == 1:
        return _resolution(
            "resolved", ordered[0], "exact-ifname-or-ifdescr",
            "unique-port-identity", ordered, evidence,
        )
    if len(ordered) > 1:
        return _resolution(
            "ambiguous", strategy="exact-ifname-or-ifdescr",
            reason="ambiguous-port-identity", candidates=ordered,
            evidence=evidence,
        )
    return None


def resolve_port_identity(device_or_indexes, port_name=None, port_id=None,
                          ifindex=None):
    """Resolve a port by precedence, stopping at the first ambiguous layer."""
    if "by_ifindex" in device_or_indexes:
        indexes = device_or_indexes
    else:
        indexes = build_port_identity_indexes(device_or_indexes)
    evidence = []

    explicit_layers = (
        ("by_port_id", str(port_id).strip() if port_id not in (None, "") else "",
         "librenms-port-id"),
        ("by_ifindex", str(_as_positive_int(ifindex) or ""), "explicit-ifindex"),
    )
    for index_name, identity, strategy in explicit_layers:
        result = _resolve_port_layer(
            indexes, index_name, identity, strategy, evidence
        )
        if result is not None:
            return result

    raw_name = str(port_name or "").strip()
    if not raw_name:
        if evidence:
            return _resolution(
                "not_found", strategy=evidence[-1]["kind"],
                reason="explicit-port-not-found", evidence=evidence,
            )
        return _resolution(
            "no_strategy", reason="no-port-identity", evidence=evidence
        )
    result = _resolve_port_layer(
        indexes, "by_typed_identity",
        canonical_topology_port_identity(raw_name), "typed-canonical", evidence
    )
    if result is not None:
        return result
    result = _resolve_port_exact_layer(indexes, raw_name.lower(), evidence)
    if result is not None:
        return result
    for index_name, identity, strategy in (
        ("by_normalized_identity", normalize_port_name(raw_name), "normalized"),
        ("by_safe_ifalias", canonical_topology_port_identity(raw_name),
         "safe-ifalias"),
    ):
        result = _resolve_port_layer(
            indexes, index_name, identity, strategy, evidence
        )
        if result is not None:
            return result

    # Vendor bridge names such as bridge/ether4 are accepted only as a final,
    # unique suffix match. Ordinary Cisco numeric path components are never
    # extracted as tokens.
    suffix = raw_name.lower().rsplit("/", 1)[-1] if "/" in raw_name else ""
    result = _resolve_port_layer(
        indexes, "by_unique_suffix", suffix, "unique-slash-suffix", evidence
    )
    if result is not None:
        return result
    return _resolution(
        "not_found", strategy="all-port-identities", reason="unknown-port",
        evidence=evidence,
    )


def resolve_ifindex_by_name(port_name, ifname_map):
    """Compatibility wrapper: return only an unambiguously resolved ifIndex."""
    result = resolve_port_identity({"ifname": ifname_map}, port_name=port_name)
    return result["value"] if result["state"] == "resolved" else None


def resolve_ifindex(loc_port, ifname_map, loc_port_desc_map):
    """LLDP's local port number is usually ifIndex on Cisco, but some platforms
    use a separate bridge port id. Try identity first, then match the loc port
    description against ifName values (normalized) for a single hit.
    """
    desc = loc_port_desc_map.get(loc_port)
    if desc:
        result = resolve_port_identity({"ifname": ifname_map}, port_name=desc)
        if result["state"] == "resolved":
            return result["value"]
        if result["state"] == "ambiguous":
            return None

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
        "port_records": [],
        "librenms_metadata": {},
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
    """Collect and authoritatively resolve aggregate membership over SNMP."""
    ifstack = merge_ifstack_claims(
        initial or {},
        parse_if_stack_status(snmpwalk(ip, community, IF_STACK_STATUS_OID)),
    )
    resolution = resolve_aggregate_member_maps(
        ifstack,
        pagp=parse_member_aggregate_ifindex(
            snmpwalk(ip, community, PAGP_GROUP_IFINDEX_OID)
        ),
        attached=parse_member_aggregate_ifindex(
            snmpwalk(ip, community, DOT3AD_ATTACHED_AGG_ID_OID)
        ),
        aggregate_admin_keys=parse_indexed_integer(
            snmpwalk(ip, community, DOT3AD_AGG_ACTOR_ADMIN_KEY_OID)
        ),
        physical_admin_keys=parse_indexed_integer(
            snmpwalk(ip, community, DOT3AD_PORT_ACTOR_ADMIN_KEY_OID)
        ),
    )
    if resolution["conflicts"]:
        details = ", ".join(
            f"ifIndex {member}: {data['reason']} {data.get('candidates', [])}"
            for member, data in sorted(resolution["conflicts"].items())
        )
        print(f"[WARN] {ip}: isolated ambiguous LAG ownership ({details})", file=sys.stderr)
    return resolution["members_by_aggregate"]


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
        "librenms_metadata": metadata,
        "sysname": metadata.get("sysName") or metadata.get("hostname") or ip,
        "ifname": ifname,
        "ifoper": ifoper,
        "port_by_id": port_by_id,
        "port_records": _records,
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
        librenms_ifstack = _librenms_ifstack(client, metadata, port_by_id)
    except LibreNMSError as exc:
        _log_librenms_fallback(ip, "port_stack", exc)
        if mode == "hybrid":
            result["ifstack"] = poll_snmp_lag(ip, community, ifname, ifoper)
            result["source"]["lag"] = "direct-snmp"
        else:
            result["source"]["lag"] = "unavailable"
    else:
        if mode == "hybrid":
            # Always consult direct Cisco/IEEE aggregation tables. A stale
            # ifStack can look "complete" while assigning one member to two
            # Port-channels, so member count is not a validity check.
            result["ifstack"] = poll_snmp_lag(
                ip, community, ifname, ifoper, initial=librenms_ifstack
            )
            result["source"]["lag"] = "hybrid"
        else:
            result["ifstack"] = resolve_aggregate_member_maps(
                librenms_ifstack
            )["members_by_aggregate"]

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


def build_device_identity_indexes(devices):
    """Build deterministic candidate sets for every supported device identity."""
    indexes = {
        "by_device_id": {},
        "by_management_ip": {},
        "by_full_name": {},
        "by_short_name": {},
        "by_scoped_core_alias": {},
    }
    core_ips = {
        ip
        for entry in os.environ.get("CORE_SWITCH_PING", "").split(",")
        for ip in expand_device_entry(entry)
    }
    for key_ip in sorted(devices):
        device = devices[key_ip]
        ip = str(device.get("ip") or key_ip).strip()
        _candidate_add(indexes["by_management_ip"], ip, ip)
        device_id = device.get("device_id")
        if device_id not in (None, ""):
            _candidate_add(indexes["by_device_id"], str(device_id).strip(), ip)
        full = str(device.get("sysname") or "").strip().lower()
        short = normalize_hostname(full)
        _candidate_add(indexes["by_full_name"], full, ip)
        _candidate_add(indexes["by_short_name"], short, ip)
        if ip in core_ips:
            _candidate_add(indexes["by_scoped_core_alias"], full, ip)
            _candidate_add(indexes["by_scoped_core_alias"], short, ip)
    return indexes


def build_name_index(devices):
    """Compatibility name for the Phase 1 candidate-set identity indexes."""
    return build_device_identity_indexes(devices)


def build_device_id_index(devices):
    """Return all LibreNMS device_id candidates; duplicate IDs stay ambiguous."""
    return build_device_identity_indexes(devices)["by_device_id"]


def _device_evidence(indexes, index_name, identity, kind):
    identity = str(identity or "").strip().lower() \
        if "name" in index_name or "alias" in index_name \
        else str(identity or "").strip()
    candidates = sorted(
        indexes.get(index_name, {}).get(identity, set()), key=str
    )
    return identity, candidates, {
        "kind": kind,
        "identity": identity,
        "candidates": candidates,
    }


def resolve_device_identity(indexes, remote_device_id=None, management_ip=None,
                            name=None, allow_scoped_core=False):
    """Resolve current-cycle device evidence without guessing through ambiguity."""
    evidence = []
    strong = []
    if remote_device_id not in (None, ""):
        identity, candidates, item = _device_evidence(
            indexes, "by_device_id", remote_device_id, "librenms-device-id"
        )
        evidence.append(item)
        strong.append(("librenms-device-id", identity, candidates))
    if management_ip not in (None, ""):
        identity, candidates, item = _device_evidence(
            indexes, "by_management_ip", management_ip, "management-ip"
        )
        evidence.append(item)
        strong.append(("management-ip", identity, candidates))

    if strong:
        all_candidates = sorted(
            {candidate for _, _, candidates in strong for candidate in candidates},
            key=str,
        )
        if any(len(candidates) > 1 for _, _, candidates in strong):
            return _resolution(
                "ambiguous", strategy="strong-identity",
                reason="ambiguous-strong-device-identity",
                candidates=all_candidates, evidence=evidence,
            )
        resolved = [candidates[0] for _, _, candidates in strong if candidates]
        missing = [kind for kind, _, candidates in strong if not candidates]
        if len(set(resolved)) > 1 or (resolved and missing):
            return _resolution(
                "conflict", strategy="strong-identity",
                reason="conflicting-strong-device-identity",
                candidates=all_candidates, evidence=evidence,
            )
        if not resolved:
            # Cisco may advertise a core SVI other than the configured
            # management address. This is the only scoped exception.
            if allow_scoped_core and name:
                full = str(name).strip().lower()
                short = normalize_hostname(full)
                scoped = set()
                for alias in (full, short):
                    _, candidates, item = _device_evidence(
                        indexes, "by_scoped_core_alias", alias,
                        "configured-core-alias",
                    )
                    evidence.append(item)
                    scoped.update(candidates)
                if len(scoped) == 1:
                    value = sorted(scoped, key=str)[0]
                    return _resolution(
                        "resolved", value, "configured-core-alias",
                        "unique-configured-core-alias", [value], evidence,
                    )
                if len(scoped) > 1:
                    return _resolution(
                        "ambiguous", strategy="configured-core-alias",
                        reason="ambiguous-configured-core-alias",
                        candidates=scoped, evidence=evidence,
                    )
            kind = strong[0][0]
            reason = "external-device-id" if kind == "librenms-device-id" \
                else "unknown-management-ip"
            return _resolution(
                "not_found", strategy=kind, reason=reason, evidence=evidence
            )

        value = resolved[0]
        reason = "unique-strong-device-identity"
        if name:
            full = str(name).strip().lower()
            _, full_candidates, item = _device_evidence(
                indexes, "by_full_name", full, "full-name"
            )
            evidence.append(item)
            if full_candidates:
                if len(full_candidates) != 1 or full_candidates[0] != value:
                    reason = "identity-name-mismatch"
            else:
                _, short_candidates, item = _device_evidence(
                    indexes, "by_short_name", normalize_hostname(full),
                    "short-name",
                )
                evidence.append(item)
                if short_candidates and value not in short_candidates:
                    reason = "identity-name-mismatch"
        return _resolution(
            "resolved", value, strong[0][0], reason, [value], evidence
        )

    full = str(name or "").strip().lower()
    if not full:
        return _resolution("no_strategy", reason="no-device-identity")
    for index_name, identity, strategy in (
        ("by_full_name", full, "full-name"),
        ("by_short_name", normalize_hostname(full), "short-name"),
    ):
        identity, candidates, item = _device_evidence(
            indexes, index_name, identity, strategy
        )
        evidence.append(item)
        if len(candidates) == 1:
            return _resolution(
                "resolved", candidates[0], strategy, "unique-device-name",
                candidates, evidence,
            )
        if len(candidates) > 1:
            return _resolution(
                "ambiguous", strategy=strategy,
                reason="ambiguous-device-name", candidates=candidates,
                evidence=evidence,
            )
    return _resolution(
        "not_found", strategy="device-name", reason="unknown-device",
        evidence=evidence,
    )


def configured_core_neighbor_ip(devices, neighbor_name):
    """Map a core CDP SVI alias to the console's canonical core IP."""
    indexes = build_device_identity_indexes(devices)
    full = str(neighbor_name or "").strip().lower()
    candidates = set()
    for alias in (full, normalize_hostname(full)):
        candidates.update(indexes["by_scoped_core_alias"].get(alias, set()))
    return sorted(candidates, key=str)[0] if len(candidates) == 1 else None


def canonical_edge_key(edge):
    a = (edge["from_ip"] or "", edge["from_ifindex"] or 0)
    b = (edge["to_ip"] or "", edge["to_ifindex"] or 0)
    return tuple(sorted([a, b]))


def merge_edge(edges_by_key, edge):
    """Insert an edge, or backfill missing fields on an existing one (so an LLDP
    and a CDP view of the same link, or both directions, collapse into one)."""
    if edge.get("from_ip") and edge.get("from_ip") == edge.get("to_ip"):
        return
    device_level_identity = edge.pop("_device_identity_resolved", False)
    if not any(
        _has_physical_endpoint_evidence(edge, side)
        for side in ("from", "to")
    ) and not device_level_identity:
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


def _edge_evidence_rank(edge):
    return (
        edge.get("_observations", 1),
        int(edge.get("from_ifindex") is not None) +
        int(edge.get("to_ifindex") is not None),
        int(bool(edge.get("from_port"))) + int(bool(edge.get("to_port"))),
    )


def _stable_edge_signature(edge):
    endpoints = []
    for side in ("from", "to"):
        endpoints.append((
            str(edge.get(f"{side}_ip") or ""),
            str(edge.get(f"{side}_ifindex") or ""),
            str(edge.get(f"{side}_port") or ""),
        ))
    return tuple(sorted(endpoints))


def resolve_endpoint_conflicts(edges, diagnostics=None, evidence_seen_at=None,
                               invalidation_hints=None):
    """Keep one physical neighbor per resolved interface.

    SG220 can expose an off-by-one LLDP bridge-port row alongside the correct
    CDP row. After both directions are polled that yields 24<->24 twice plus
    one 23->24 row from each side. A physical ifIndex cannot terminate two
    different links, so keep the bidirectionally-confirmed edge.
    """
    diagnostics = diagnostics if diagnostics is not None else []
    invalidation_hints = (
        invalidation_hints if invalidation_hints is not None else []
    )
    evidence_seen_at = time.time() if evidence_seen_at is None else evidence_seen_at
    endpoint_edges = {}
    for edge in edges:
        for side in ("from", "to"):
            endpoint = (
                str(edge.get(f"{side}_ip") or ""),
                edge.get(f"{side}_ifindex"),
            )
            if endpoint[0] and endpoint[1] is not None:
                endpoint_edges.setdefault(endpoint, []).append(edge)

    contested = set()
    for endpoint in sorted(endpoint_edges, key=lambda item: (item[0], str(item[1]))):
        candidates = endpoint_edges[endpoint]
        top_rank = max(_edge_evidence_rank(edge) for edge in candidates)
        top_edges = [
            edge for edge in candidates if _edge_evidence_rank(edge) == top_rank
        ]
        signatures = {_stable_edge_signature(edge) for edge in top_edges}
        if len(signatures) < 2:
            continue
        contested.add(endpoint)
        candidate_edges = []
        top_remote_ips = set()
        remote_ports = set()
        local_port = ""
        for edge in sorted(top_edges, key=_stable_edge_signature):
            if (
                str(edge.get("from_ip") or "") == endpoint[0] and
                edge.get("from_ifindex") == endpoint[1]
            ):
                local_side, remote_side = "from", "to"
            else:
                local_side, remote_side = "to", "from"
            local_port = local_port or str(edge.get(f"{local_side}_port") or "")
            remote_ip = str(edge.get(f"{remote_side}_ip") or "")
            remote_port = str(edge.get(f"{remote_side}_port") or "")
            if remote_ip:
                top_remote_ips.add(remote_ip)
            if remote_port:
                remote_ports.add(remote_port)
            candidate_edges.append({
                "remote_ip": remote_ip,
                "remote_ifindex": edge.get(f"{remote_side}_ifindex"),
                "remote_port": remote_port,
                "rank": list(top_rank),
            })
        diagnostics.append({
            "from_ip": endpoint[0],
            "from_port": local_port or None,
            "from_ifindex": endpoint[1],
            "protocol": "endpoint-conflict",
            "raw_remote_identity": {"candidate_edges": candidate_edges},
            "raw_remote_port": None,
            "resolution_state": "endpoint_conflict",
            "resolution_reason": "equal-ranked-resolved-endpoint",
            "candidate_devices": sorted(top_remote_ips),
            "candidate_ports": sorted(remote_ports),
            "evidence_seen_at": evidence_seen_at,
        })

        all_remote_ips = set()
        for edge in candidates:
            if (
                str(edge.get("from_ip") or "") == endpoint[0] and
                edge.get("from_ifindex") == endpoint[1]
            ):
                remote_side = "to"
            else:
                remote_side = "from"
            remote_ip = str(edge.get(f"{remote_side}_ip") or "")
            if remote_ip:
                all_remote_ips.add(remote_ip)
        if all_remote_ips:
            invalidation_hints.append({
                "kind": "endpoint-candidates",
                "local_ip": endpoint[0],
                "local_ifindex": endpoint[1],
                "local_port": local_port,
                "remote_ips": sorted(all_remote_ips),
            })

    ranked = sorted(
        [
            edge for edge in edges
            if not any(
                (str(edge.get(f"{side}_ip") or ""), edge.get(f"{side}_ifindex"))
                in contested
                for side in ("from", "to")
            )
        ],
        key=lambda edge: (
            tuple(-part for part in _edge_evidence_rank(edge)),
            _stable_edge_signature(edge),
        ),
    )
    occupied = set()
    kept = []
    for edge in ranked:
        endpoints = [
            (edge.get("from_ip"), edge.get("from_ifindex")),
            (edge.get("to_ip"), edge.get("to_ifindex")),
        ]
        resolved = [endpoint for endpoint in endpoints if endpoint[0] and endpoint[1] is not None]
        if any(endpoint in occupied for endpoint in resolved):
            continue
        occupied.update(resolved)
        edge.pop("_observations", None)
        kept.append(edge)
    return kept


def canonical_physical_edge_key(edge):
    """Undirected device+port key only when both endpoints are reliable."""
    endpoints = []
    for side in ("from", "to"):
        ip = str(edge.get(f"{side}_ip") or "").strip()
        port = canonical_topology_port_identity(edge.get(f"{side}_port"))
        if not ip or not port:
            return None
        endpoints.append((ip, port))
    return tuple(sorted(endpoints))


def _orient_edge_like(edge, reference):
    """Orient a reciprocal observation like the first retained observation."""
    if (
        edge.get("from_ip") != reference.get("to_ip") or
        edge.get("to_ip") != reference.get("from_ip")
    ):
        return edge
    oriented = dict(edge)
    for field in ("ip", "sysname", "port", "ifindex"):
        oriented[f"from_{field}"] = edge.get(f"to_{field}")
        oriented[f"to_{field}"] = edge.get(f"from_{field}")
    return oriented


def dedupe_canonical_physical_edges(edges):
    """Merge reciprocal rows only for the same proven physical port pair."""
    output = []
    positions = {}
    for source in edges:
        edge = dict(source)
        for side in ("from", "to"):
            field = f"{side}_port"
            label = canonical_topology_port_label(edge.get(field))
            if label:
                edge[field] = label
        key = canonical_physical_edge_key(edge)
        if key is None:
            output.append(edge)
            continue
        position = positions.get(key)
        if position is None:
            positions[key] = len(output)
            output.append(edge)
            continue

        existing = output[position]
        incoming = _orient_edge_like(edge, existing)
        for field in (
            "from_sysname", "from_port", "from_ifindex",
            "to_sysname", "to_port", "to_ifindex",
        ):
            if not existing.get(field) and incoming.get(field):
                existing[field] = incoming[field]
    return output


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


def _diagnostic_state_for_device(result):
    if result["state"] == "ambiguous":
        return "ambiguous_device"
    if result["state"] == "conflict":
        return "conflicting_identity"
    if result.get("reason") == "external-device-id":
        return "external_device"
    return "unknown_device"


def _diagnostic_record(device, from_port, from_ifindex, protocol,
                       neighbor_name, remote_port, result, observed_at,
                       neighbor_device_id="", management_ip="",
                       candidate_ports=None, partial=False):
    state = result.get("state", "not_found")
    if state in {"ambiguous", "not_found", "no_strategy"} and candidate_ports is not None:
        resolution_state = "ambiguous_port" if state == "ambiguous" else "unknown_port"
    else:
        resolution_state = _diagnostic_state_for_device(result)
    record = {
        "from_ip": str(device.get("ip") or ""),
        "from_sysname": str(device.get("sysname") or ""),
        "from_port": from_port or None,
        "from_ifindex": from_ifindex,
        "protocol": str(protocol or "").lower(),
        "raw_remote_identity": {
            "device_id": str(neighbor_device_id or ""),
            "management_ip": str(management_ip or ""),
            "name": str(neighbor_name or ""),
        },
        "raw_remote_port": remote_port or None,
        "resolution_state": resolution_state,
        "resolution_reason": result.get("reason") or state,
        "candidate_devices": sorted(result.get("candidates") or [], key=str),
        "candidate_ports": sorted(candidate_ports or [], key=_candidate_sort_key),
        "evidence_seen_at": observed_at,
        # Legacy names keep bounded unmatched-neighbor logging and the existing
        # external-device cache rule compatible while diagnostics are upgraded.
        "neighbor_name": neighbor_name,
        "neighbor_port": remote_port,
    }
    if neighbor_device_id not in (None, ""):
        record["neighbor_device_id"] = str(neighbor_device_id)
    if result.get("reason") == "external-device-id":
        record["reason"] = "external-device-id"
    elif result.get("reason") == "self-edge":
        record["reason"] = "self-edge"
    if partial:
        record["_partial_edge"] = True
    return record


def _local_lldp_port_resolution(device, loc_port, port_indexes):
    desc = device.get("loc_port_desc", {}).get(loc_port)
    if desc:
        result = resolve_port_identity(port_indexes, port_name=desc)
        if result["state"] in {"resolved", "ambiguous"}:
            return result, desc
    result = resolve_port_identity(port_indexes, ifindex=loc_port)
    return result, desc


def build_edges(devices, name_index, evidence_seen_at=None,
                invalidation_hints=None):
    edges_by_key = {}
    diagnostics = []
    invalidation_hints = (
        invalidation_hints if invalidation_hints is not None else []
    )
    strong_endpoint_claims = {}
    observed_at = time.time() if evidence_seen_at is None else evidence_seen_at
    identity_indexes = (
        name_index if isinstance(name_index, dict) and "by_device_id" in name_index
        else build_device_identity_indexes(devices)
    )
    # Collection completes concurrently, so dict insertion order is unstable.
    # LibreNMS device IDs preserve the established inventory/core-first order;
    # numeric IPv4 is the deterministic tie-breaker and direct-SNMP fallback.
    def device_traversal_key(ip):
        device_id = _as_positive_int(devices[ip].get("device_id"))
        return (
            device_id is None,
            device_id if device_id is not None else 0,
            IPv4Address(ip),
        )

    device_ips = sorted(devices, key=device_traversal_key)
    port_indexes = {
        ip: build_port_identity_indexes(devices[ip]) for ip in device_ips
    }

    def interface_is_usable(device, ifindex):
        if ifindex is None:
            return True
        status = device.get("ifoper", {}).get(ifindex)
        return status is None or status == 1

    def add_device_diagnostic(device, protocol, local_name, local_ifindex,
                              neighbor_name, remote_name, result,
                              neighbor_device_id="", management_ip=""):
        record = _diagnostic_record(
            device, local_name, local_ifindex, protocol, neighbor_name,
            remote_name, result, observed_at, neighbor_device_id,
            management_ip,
        )
        if (
            result["state"] in {"ambiguous", "conflict"} and
            local_ifindex is not None and result.get("candidates")
        ):
            invalidation_hints.append({
                "kind": "endpoint-candidates",
                "local_ip": device.get("ip"),
                "local_ifindex": local_ifindex,
                "local_port": local_name,
                "remote_ips": sorted(result["candidates"], key=str),
            })
        elif result["state"] in {"ambiguous", "conflict"}:
            record["resolution_reason"] += ":cache-invalidation-unsafe"
        if result.get("reason") == "external-device-id":
            invalidation_hints.append({
                "kind": "external-neighbor",
                "local_ip": device.get("ip"),
                "local_port": local_name,
                "remote_name": neighbor_name,
                "remote_port": remote_name,
            })
        diagnostics.append(record)

    def add_port_diagnostic(device, protocol, local_name, local_ifindex,
                            neighbor_name, remote_name, remote_ip, result,
                            is_remote=True, neighbor_device_id="",
                            management_ip=""):
        record = _diagnostic_record(
            device, local_name, local_ifindex, protocol, neighbor_name,
            remote_name, result, observed_at,
            neighbor_device_id=neighbor_device_id,
            management_ip=management_ip,
            candidate_ports=result.get("candidates"), partial=True,
        )
        record["candidate_devices"] = [remote_ip] if remote_ip else []
        if (
            is_remote and result["state"] == "ambiguous" and
            local_ifindex is not None and remote_ip
        ):
            invalidation_hints.append({
                "kind": "endpoint-candidates",
                "local_ip": device.get("ip"),
                "local_ifindex": local_ifindex,
                "local_port": local_name,
                "remote_ips": [remote_ip],
            })
        elif is_remote and result["state"] == "ambiguous":
            record["resolution_reason"] += ":cache-invalidation-unsafe"
        diagnostics.append(record)

    def add_name_mismatch(device, protocol, local_name, local_ifindex,
                          neighbor_name, remote_name, result,
                          neighbor_device_id="", management_ip=""):
        if result.get("reason") != "identity-name-mismatch":
            return
        record = _diagnostic_record(
            device, local_name, local_ifindex, protocol, neighbor_name,
            remote_name, result, observed_at, neighbor_device_id,
            management_ip,
        )
        record["resolution_state"] = "partial"
        record["candidate_devices"] = [result["value"]]
        diagnostics.append(record)

    def emit_resolved_observation(device, protocol, local_port_name,
                                  local_result, neighbor_name,
                                  remote_port_name, device_result,
                                  remote_port_id=None, neighbor_device_id="",
                                  management_ip=""):
        ip = device["ip"]
        neighbor_ip = device_result["value"]
        local_ifindex = local_result["value"] \
            if local_result["state"] == "resolved" else None
        if neighbor_ip == ip:
            self_result = _resolution(
                "not_found", strategy="self-check", reason="self-edge",
                candidates=[ip], evidence=device_result.get("evidence"),
            )
            add_device_diagnostic(
                device, protocol, local_port_name, local_ifindex,
                neighbor_name, remote_port_name, self_result,
            )
            diagnostics[-1]["resolution_state"] = "invalid_response"
            return

        if (
            local_ifindex is not None and
            device_result.get("strategy") in {
                "librenms-device-id", "management-ip"
            }
        ):
            strong_endpoint_claims.setdefault((ip, local_ifindex), []).append({
                "remote_ip": neighbor_ip,
                "strategy": device_result["strategy"],
                "neighbor_name": neighbor_name,
                "neighbor_device_id": str(neighbor_device_id or ""),
                "management_ip": str(management_ip or ""),
                "remote_port": remote_port_name or None,
            })

        remote = devices.get(neighbor_ip)
        remote_result = resolve_port_identity(
            port_indexes[neighbor_ip], port_name=remote_port_name,
            port_id=remote_port_id,
        )
        remote_ifindex = remote_result["value"] \
            if remote_result["state"] == "resolved" else None
        if local_ifindex is not None:
            local_port_name = device.get("ifname", {}).get(
                local_ifindex, local_port_name
            )
        if remote_ifindex is not None:
            remote_port_name = remote.get("ifname", {}).get(
                remote_ifindex, remote_port_name
            )

        if local_result["state"] != "resolved":
            add_port_diagnostic(
                device, protocol, local_port_name, local_ifindex,
                neighbor_name, remote_port_name, neighbor_ip, local_result,
                is_remote=False,
                neighbor_device_id=neighbor_device_id,
                management_ip=management_ip,
            )
        if remote_result["state"] != "resolved":
            add_port_diagnostic(
                device, protocol, local_port_name, local_ifindex,
                neighbor_name, remote_port_name, neighbor_ip, remote_result,
                is_remote=True,
                neighbor_device_id=neighbor_device_id,
                management_ip=management_ip,
            )
        add_name_mismatch(
            device, protocol, local_port_name, local_ifindex,
            neighbor_name, remote_port_name, device_result,
            neighbor_device_id, management_ip,
        )
        if not interface_is_usable(device, local_ifindex):
            return
        if not interface_is_usable(remote, remote_ifindex):
            return
        merge_edge(edges_by_key, {
            "from_ip": ip,
            "from_sysname": device.get("sysname"),
            "from_port": local_port_name,
            "from_ifindex": local_ifindex,
            "to_ip": neighbor_ip,
            "to_sysname": neighbor_name,
            "to_port": remote_port_name,
            "to_ifindex": remote_ifindex,
            "_device_identity_resolved": True,
        })

    for ip in device_ips:
        device = devices[ip]
        local_indexes = port_indexes[ip]
        for (tm, loc_port, rem_idx), neighbor_name in sorted(
            device.get("rem_sys", {}).items()
        ):
            local_result, local_desc = _local_lldp_port_resolution(
                device, loc_port, local_indexes
            )
            local_ifindex = local_result["value"] \
                if local_result["state"] == "resolved" else None
            local_port_name = device.get("ifname", {}).get(
                local_ifindex, local_desc
            )
            remote_port_name = (
                device.get("rem_port_desc", {}).get((tm, loc_port, rem_idx)) or
                device.get("rem_port_id", {}).get((tm, loc_port, rem_idx))
            )
            result = resolve_device_identity(identity_indexes, name=neighbor_name)
            if result["state"] != "resolved":
                add_device_diagnostic(
                    device, "lldp", local_port_name, local_ifindex,
                    neighbor_name, remote_port_name, result,
                )
                continue
            emit_resolved_observation(
                device, "lldp", local_port_name, local_result, neighbor_name,
                remote_port_name, result,
            )

        # cdpCacheIfIndex is a real local IF-MIB ifIndex. An advertised CDP
        # address is strong evidence and only the configured-core SVI rule may
        # recover an address outside the managed inventory.
        for (if_index, dev_index), neighbor_name in sorted(
            device.get("cdp_device_id", {}).items()
        ):
            addr_ip = device.get("cdp_address", {}).get((if_index, dev_index))
            local_result = resolve_port_identity(
                local_indexes, ifindex=if_index
            )
            local_ifindex = local_result["value"] \
                if local_result["state"] == "resolved" else None
            local_port_name = device.get("ifname", {}).get(local_ifindex)
            remote_port_name = device.get("cdp_device_port", {}).get(
                (if_index, dev_index)
            )
            result = resolve_device_identity(
                identity_indexes, management_ip=addr_ip if addr_ip else None,
                name=neighbor_name, allow_scoped_core=bool(addr_ip),
            )
            if result["state"] != "resolved":
                add_device_diagnostic(
                    device, "cdp", local_port_name, local_ifindex,
                    neighbor_name, remote_port_name, result,
                    management_ip=addr_ip or "",
                )
                continue
            emit_resolved_observation(
                device, "cdp", local_port_name, local_result, neighbor_name,
                remote_port_name, result, management_ip=addr_ip or "",
            )

        # LibreNMS remote_device_id and port_id stay typed identities and are
        # never reinterpreted as management IPs or IF-MIB indexes.
        for neighbor in device.get("neighbors", []):
            if _link_is_inactive(neighbor):
                continue
            protocol = str(neighbor.get("protocol") or "xdp").strip().lower()
            neighbor_name = str(neighbor.get("neighbor_name") or "").strip()
            neighbor_device_id = neighbor.get("neighbor_device_id")
            local_port_name = str(neighbor.get("local_port") or "").strip()
            local_ifindex_hint = _as_positive_int(neighbor.get("local_ifindex"))
            if local_ifindex_hint is not None:
                local_result = resolve_port_identity(
                    local_indexes, ifindex=local_ifindex_hint
                )
            else:
                local_result = resolve_port_identity(
                    local_indexes, port_name=local_port_name
                )
            local_ifindex = local_result["value"] \
                if local_result["state"] == "resolved" else None
            if local_ifindex is not None:
                local_port_name = device.get("ifname", {}).get(
                    local_ifindex, local_port_name
                )
            remote_port_name = str(neighbor.get("neighbor_port") or "").strip()
            result = resolve_device_identity(
                identity_indexes,
                remote_device_id=neighbor_device_id
                if neighbor_device_id not in (None, "") else None,
                name=neighbor_name,
            )
            if result["state"] != "resolved":
                add_device_diagnostic(
                    device, protocol, local_port_name, local_ifindex,
                    neighbor_name, remote_port_name, result,
                    neighbor_device_id=neighbor_device_id or "",
                )
                continue
            emit_resolved_observation(
                device, protocol, local_port_name, local_result, neighbor_name,
                remote_port_name, result,
                remote_port_id=neighbor.get("neighbor_port_id"),
                neighbor_device_id=neighbor_device_id or "",
            )

    strong_conflicted_endpoints = set()
    for endpoint in sorted(
        strong_endpoint_claims, key=lambda item: (item[0], str(item[1]))
    ):
        claims = strong_endpoint_claims[endpoint]
        remote_ips = sorted({claim["remote_ip"] for claim in claims})
        strategies = {claim["strategy"] for claim in claims}
        if len(remote_ips) < 2 or strategies != {
            "librenms-device-id", "management-ip"
        }:
            continue
        strong_conflicted_endpoints.add(endpoint)
        ordered_claims = sorted(
            claims,
            key=lambda claim: (
                claim["strategy"], claim["remote_ip"],
                claim["neighbor_name"], str(claim["remote_port"] or ""),
            ),
        )
        local_device = devices.get(endpoint[0]) or {}
        local_port = local_device.get("ifname", {}).get(endpoint[1])
        diagnostics.append({
            "from_ip": endpoint[0],
            "from_sysname": local_device.get("sysname") or "",
            "from_port": local_port,
            "from_ifindex": endpoint[1],
            "protocol": "cross-protocol",
            "raw_remote_identity": {"strong_claims": ordered_claims},
            "raw_remote_port": None,
            "resolution_state": "conflicting_identity",
            "resolution_reason": "conflicting-strong-device-identity",
            "candidate_devices": remote_ips,
            "candidate_ports": sorted({
                claim["remote_port"] for claim in claims
                if claim["remote_port"]
            }),
            "evidence_seen_at": observed_at,
        })
        invalidation_hints.append({
            "kind": "endpoint-candidates",
            "local_ip": endpoint[0],
            "local_ifindex": endpoint[1],
            "local_port": local_port,
            "remote_ips": remote_ips,
        })

    conflict_candidates = [
        edge for edge in edges_by_key.values()
        if not any(
            (str(edge.get(f"{side}_ip") or ""), edge.get(f"{side}_ifindex"))
            in strong_conflicted_endpoints
            for side in ("from", "to")
        )
    ]
    edges = dedupe_canonical_physical_edges(resolve_endpoint_conflicts(
        conflict_candidates, diagnostics=diagnostics,
        evidence_seen_at=observed_at,
        invalidation_hints=invalidation_hints,
    ))
    return enrich_aggregate_members(edges, devices), diagnostics


UNMATCHED_NEIGHBOR_CATEGORIES = (
    "external-device-id",
    "unmanaged-endpoint",
    "unresolved",
    "invalid-response",
)
UNMATCHED_NEIGHBOR_LOG_LIMIT = 10
INVALID_NEIGHBOR_RESPONSE_MARKERS = (
    "no such object",
    "no such instance",
    "no such name",
    "no more variables",
    "end of mib",
    "endofmibview",
)


def _is_mac_endpoint_identity(value):
    """Whether a value is entirely a MAC/chassis identity, not just a port ID."""
    text = str(value or "").strip().strip('"')
    text = re.sub(
        r"^(?:hex-string|string)\s*:\s*", "", text, flags=re.IGNORECASE
    ).strip()
    return bool(text) and re.fullmatch(r"[0-9a-fA-F\s:.-]+", text) is not None \
        and normalize_mac(text) is not None


def classify_unmatched_neighbor(observation):
    """Classify existing debug data without attempting endpoint resolution."""
    if not isinstance(observation, dict):
        return "invalid-response"
    response_text = " ".join(
        str(observation.get(field) or "").strip().lower()
        for field in ("neighbor_name", "neighbor_port")
    )
    if any(marker in response_text for marker in INVALID_NEIGHBOR_RESPONSE_MARKERS):
        return "invalid-response"

    reason = str(observation.get("reason") or "").strip().lower()
    if reason == "external-device-id":
        return "external-device-id"
    if reason == "unmanaged-endpoint":
        return "unmanaged-endpoint"

    # A MAC-valued LLDP Port ID is valid for infrastructure and is not enough
    # to classify an endpoint. Only a MAC/chassis identity in the remote name,
    # paired with no port or another MAC identity, is safe to de-emphasize.
    neighbor_name = observation.get("neighbor_name")
    neighbor_port = observation.get("neighbor_port")
    if _is_mac_endpoint_identity(neighbor_name) and (
        not str(neighbor_port or "").strip() or
        _is_mac_endpoint_identity(neighbor_port)
    ):
        return "unmanaged-endpoint"
    return "unresolved"


def classify_unmatched_neighbors(observations):
    """Group unmatched observations without mutating them or topology edges."""
    grouped = {category: [] for category in UNMATCHED_NEIGHBOR_CATEGORIES}
    for observation in observations or []:
        grouped[classify_unmatched_neighbor(observation)].append(observation)
    return grouped


def log_unmatched_neighbors(observations, stream=None,
                            detail_limit=UNMATCHED_NEIGHBOR_LOG_LIMIT):
    """Log one summary and bounded details only for unresolved infrastructure."""
    grouped = classify_unmatched_neighbors(observations)
    counts = {
        category: len(grouped[category])
        for category in UNMATCHED_NEIGHBOR_CATEGORIES
    }
    total = sum(counts.values())
    if total == 0:
        return counts

    stream = sys.stderr if stream is None else stream
    print(
        "[INFO] unmatched neighbor summary: "
        f"total={total} "
        + " ".join(
            f"{category}={counts[category]}"
            for category in UNMATCHED_NEIGHBOR_CATEGORIES
        ),
        file=stream,
    )
    unresolved = grouped["unresolved"]
    if not unresolved:
        return counts

    print(
        f"[WARN] {len(unresolved)} unresolved infrastructure neighbor(s):",
        file=stream,
    )
    limit = max(0, int(detail_limit))
    for entry in unresolved[:limit]:
        if not isinstance(entry, dict):
            continue
        print(
            "         "
            f"{entry.get('from_ip') or '-'} {entry.get('from_port') or '-'} "
            f"-> {entry.get('neighbor_name') or '-'} "
            f"{entry.get('neighbor_port') or '-'}",
            file=stream,
        )
    omitted = len(unresolved) - limit
    if omitted > 0:
        print(
            f"         ... {omitted} more unresolved neighbor(s) omitted",
            file=stream,
        )
    return counts


TOPOLOGY_DIAGNOSTIC_SUMMARY_KEYS = (
    "partial",
    "ambiguous_device",
    "ambiguous_port",
    "conflicting_identity",
    "endpoint_conflict",
    "unknown_device",
    "unknown_port",
    "external_device",
    "invalid_response",
)
TOPOLOGY_DIAGNOSTIC_RECORD_FIELDS = (
    "from_ip",
    "from_sysname",
    "from_port",
    "from_ifindex",
    "protocol",
    "raw_remote_identity",
    "raw_remote_port",
    "resolution_state",
    "resolution_reason",
    "candidate_devices",
    "candidate_ports",
    "evidence_seen_at",
)


def build_topology_diagnostics(records, generated_at=None):
    """Build one deterministic, current-cycle-only diagnostics snapshot."""
    generated_at = time.time() if generated_at is None else generated_at
    summary = {key: 0 for key in TOPOLOGY_DIAGNOSTIC_SUMMARY_KEYS}
    clean_records = []
    for source in records or []:
        if not isinstance(source, dict):
            continue
        if str(source.get("resolution_state") or "").lower() == "resolved":
            continue
        record = {
            field: source.get(field)
            for field in TOPOLOGY_DIAGNOSTIC_RECORD_FIELDS
            if field in source
        }
        state = str(record.get("resolution_state") or "invalid_response")
        if classify_unmatched_neighbor(source) == "invalid-response":
            state = "invalid_response"
            record["resolution_state"] = state
        if state in summary:
            summary[state] += 1
        else:
            summary["invalid_response"] += 1
        if source.get("_partial_edge") and state != "partial":
            summary["partial"] += 1
        record["candidate_devices"] = sorted(
            {str(value) for value in record.get("candidate_devices") or []},
            key=str,
        )
        record["candidate_ports"] = sorted(
            set(record.get("candidate_ports") or []), key=_candidate_sort_key
        )
        clean_records.append(record)
    clean_records.sort(
        key=lambda record: json.dumps(
            record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    )
    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "summary": summary,
        "records": clean_records,
    }


def write_topology_diagnostics(path, records, generated_at=None):
    payload = build_topology_diagnostics(records, generated_at=generated_at)
    write_json_atomic(path, payload, sort_keys=False)
    return payload


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


def _first_row_timestamp(record, metadata, metadata_field):
    """Prefer row evidence time, falling back to the owning device cycle."""
    for key in ("updated_at", "last_seen", "last_updated", "timestamp"):
        value = record.get(key)
        if value not in (None, ""):
            return value
    return (metadata or {}).get(metadata_field)


def _record_vlan(record, port_record=None):
    for key in ("vlan_id", "vlan", "vlanId"):
        try:
            value = int(record.get(key))
        except (TypeError, ValueError):
            continue
        if 1 <= value <= 4094:
            return value
    port_name = str(
        (port_record or {}).get("ifName")
        or (port_record or {}).get("ifDescr")
        or ""
    ).strip()
    match = re.fullmatch(r"(?i)vlan\s*(\d+)", port_name)
    if match:
        value = int(match.group(1))
        if 1 <= value <= 4094:
            return value
    return None


def _cached_server_records(cached_edges, servers):
    records = {}
    for edge in cached_edges or []:
        if edge.get("source") != "fdb":
            continue
        server_ip = _server_ip_for_edge(edge, servers)
        mac = normalize_mac(edge.get("server_mac"))
        if not server_ip or not mac or server_ip in records:
            continue
        try:
            vlan = int(edge.get("server_vlan"))
        except (TypeError, ValueError):
            vlan = None
        records[server_ip] = {"mac": mac, "vlan": vlan}
    return records


def collect_server_arp(devices, arp_device_ips, servers, community, mode,
                       client=None, librenms_ready=False, cached_edges=None):
    """Collect only the ARP evidence needed for configured servers.

    LibreNMS is consulted independently of the adjacency source.  Hybrid mode
    uses a direct walk only for an L3 device whose API evidence is unusable or
    while an uncached configured server remains unresolved.
    """
    for device in devices.values():
        device["arp"] = {}
    if not servers:
        return

    l3_ips = [ip for ip in arp_device_ips if ip in devices]
    if mode == "direct-snmp":
        for ip in l3_ips:
            devices[ip]["arp"] = poll_snmp_arp(
                ip, community, devices[ip].get("ifname", {})
            )
        return

    failed_or_stale = []
    if not librenms_ready or client is None:
        failed_or_stale = list(l3_ips)
    else:
        for ip in l3_ips:
            device = devices[ip]
            try:
                metadata = device.get("librenms_metadata") or client.resolve_device(ip)
                rows = client.get_device_arp(metadata)
            except LibreNMSError as exc:
                _log_librenms_fallback(ip, "ARP", exc)
                failed_or_stale.append(ip)
                continue
            port_by_id = device.get("port_by_id", {})
            stale_seen = False
            for row in rows:
                server_ip = str(row.get("ipv4_address") or row.get("ip") or "").strip()
                if server_ip not in servers:
                    continue
                freshness = librenms_freshness(
                    _first_row_timestamp(row, metadata, "last_polled"),
                    topology_librenms_arp_max_age(),
                )
                if freshness == "stale":
                    stale_seen = True
                    continue
                mac = normalize_mac(
                    row.get("mac_address") or row.get("mac") or row.get("mac_addr")
                )
                if not mac:
                    continue
                port_record = port_by_id.get(str(row.get("port_id")))
                device["arp"][server_ip] = {
                    "mac": mac,
                    "vlan": _record_vlan(row, port_record),
                }
            if stale_seen:
                failed_or_stale.append(ip)

    if mode == "librenms":
        return

    cached = _cached_server_records(cached_edges, servers)
    resolved = {
        server_ip
        for device in devices.values()
        for server_ip in device.get("arp", {})
    } | set(cached)
    fallback_order = list(dict.fromkeys(failed_or_stale + l3_ips))
    for ip in fallback_order:
        if resolved.issuperset(servers):
            break
        direct = poll_snmp_arp(ip, community, devices[ip].get("ifname", {}))
        devices[ip]["arp"].update(direct)
        resolved.update(server_ip for server_ip in direct if server_ip in servers)


def build_librenms_fdb_candidates(devices, edges, client, librenms_ready):
    """Build a read-only MAC candidate index, one FDB API call per switch."""
    index = {}
    status = {"usable_switches": 0, "failed_switches": 0}
    if not librenms_ready or client is None:
        return index, status

    endpoints = set()
    for edge in edges:
        for side in ("from", "to"):
            ip = edge.get(f"{side}_ip")
            ifindex = edge.get(f"{side}_ifindex")
            if ip in devices and ifindex is not None:
                endpoints.add((ip, ifindex))
    depths = _graph_depths(edges, _env_target_ips("CORE_SWITCH_PING"))
    firewall_ips = set(_env_target_ips("FIREWALL_PING"))

    for ip, device in devices.items():
        if ip in firewall_ips:
            continue
        try:
            metadata = device.get("librenms_metadata") or client.resolve_device(ip)
            port_by_id = device.get("port_by_id") or {}
            if not port_by_id:
                _records, port_by_id, ifname, ifoper = _librenms_ports(client, metadata)
                device["port_by_id"] = port_by_id
                device["ifname"] = ifname
                device["ifoper"] = ifoper
            rows = client.get_device_fdb(metadata)
        except (LibreNMSError, TopologyDataIncomplete) as exc:
            _log_librenms_fallback(ip, "FDB", exc)
            status["failed_switches"] += 1
            continue
        status["usable_switches"] += 1
        for row in rows:
            if librenms_freshness(
                _first_row_timestamp(row, metadata, "last_discovered"),
                topology_librenms_fdb_max_age(),
            ) == "stale":
                continue
            mac = normalize_mac(
                row.get("mac_address") or row.get("mac") or row.get("mac_addr")
            )
            port = port_by_id.get(str(row.get("port_id")))
            if not mac or not port:
                continue
            ifindex = _as_positive_int(port.get("ifIndex"))
            port_name = str(port.get("ifName") or port.get("ifDescr") or "").strip()
            if (
                ifindex is None
                or not is_physical_interface_name(port_name)
                or normalize_port_name(port_name).startswith("agg")
                or device.get("ifoper", {}).get(ifindex) not in (None, 1)
                or (ip, ifindex) in endpoints
            ):
                continue
            index.setdefault(mac, []).append({
                "switch_ip": ip,
                "ifindex": ifindex,
                "port_name": port_name,
                "mac": mac,
                "vlan": _record_vlan(row, port),
                "depth": depths.get(ip, -1),
            })
    return index, status


def discover_server_edges_direct(devices, edges, servers, community, cached_edges=None):
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


def _server_edge_from_candidate(devices, server_ip, server_name, candidate):
    parent = devices[candidate["switch_ip"]]
    return {
        "from_ip": candidate["switch_ip"],
        "from_sysname": parent.get("sysname"),
        "from_port": parent.get("ifname", {}).get(
            candidate["ifindex"], candidate.get("port_name")
        ),
        "from_ifindex": candidate["ifindex"],
        "to_ip": server_ip,
        "to_sysname": server_name,
        "to_port": None,
        "to_ifindex": None,
        "source": "fdb",
        "server_mac": candidate["mac"],
        "server_vlan": candidate.get("vlan"),
    }


def discover_server_edges(devices, edges, servers, community, cached_edges=None,
                          source=None, fdb_candidates=None, server_stats=None):
    """Locate servers using the configured API index/exact-SNMP policy."""
    mode = source or topology_server_attachment_source()
    stats = server_stats if server_stats is not None else {}
    stats.setdefault("full_fallbacks", 0)
    if mode == "direct-snmp" or (mode == "hybrid" and fdb_candidates is None):
        if mode == "hybrid":
            stats["full_fallbacks"] += len(servers)
        return discover_server_edges_direct(
            devices, edges, servers, community, cached_edges
        )
    if not servers or not devices:
        return []

    arp_by_server = {}
    for device in devices.values():
        for server_ip in servers:
            record = device.get("arp", {}).get(server_ip)
            if record:
                arp_by_server.setdefault(server_ip, []).append(record)
    cached_records = _cached_server_records(cached_edges, servers)
    cached_switches = {}
    for edge in cached_edges or []:
        if edge.get("source") != "fdb":
            continue
        server_ip = _server_ip_for_edge(edge, servers)
        if not server_ip:
            continue
        switch_ip = edge.get("from_ip") if edge.get("to_ip") == server_ip else edge.get("to_ip")
        if switch_ip in devices:
            cached_switches.setdefault(server_ip, switch_ip)

    endpoints = set()
    for edge in edges:
        for side in ("from", "to"):
            ip = edge.get(f"{side}_ip")
            ifindex = edge.get(f"{side}_ifindex")
            if ip in devices and ifindex is not None:
                endpoints.add((ip, ifindex))
    firewall_ips = set(_env_target_ips("FIREWALL_PING"))
    switch_devices = {
        ip: device for ip, device in devices.items()
        if ip not in firewall_ips and device.get("ifname")
    }
    depths = _graph_depths(edges, _env_target_ips("CORE_SWITCH_PING"))

    def exact_candidate(switch_ip, mac, vlan, indexed=None):
        device = switch_devices.get(switch_ip)
        if not device:
            return None
        try:
            ifindex = lookup_fdb_ifindex(
                switch_ip, community, vlan, mac, device.get("ifname", {})
            )
        except Exception as exc:
            print(
                f"[WARN] FDB lookup {switch_ip} failed ({type(exc).__name__})",
                file=sys.stderr,
            )
            return None
        port_name = device.get("ifname", {}).get(ifindex)
        if (
            ifindex is None
            or not is_physical_interface_name(port_name)
            or normalize_port_name(port_name).startswith("agg")
            or device.get("ifoper", {}).get(ifindex) not in (None, 1)
            or (switch_ip, ifindex) in endpoints
        ):
            return None
        if indexed and indexed.get("ifindex") != ifindex:
            print(
                f"[WARN] LibreNMS FDB candidate changed on {switch_ip}; "
                "using exact SNMP result",
                file=sys.stderr,
            )
        return {
            "switch_ip": switch_ip,
            "ifindex": ifindex,
            "port_name": port_name,
            "mac": mac,
            "vlan": vlan,
            "depth": depths.get(switch_ip, -1),
        }

    found = []
    fallback_servers = {}
    for server_ip, server_name in servers.items():
        records = {
            (record.get("mac"), record.get("vlan"))
            for record in arp_by_server.get(server_ip, [])
            if record.get("mac")
        }
        if not records and server_ip in cached_records:
            cached = cached_records[server_ip]
            records.add((cached["mac"], cached.get("vlan")))
            print(
                f"[INFO] server {server_name} ({server_ip}): no current ARP "
                "entry; verifying cached MAC through FDB",
                file=sys.stderr,
            )
        if not records:
            print(
                f"[WARN] server {server_name} ({server_ip}): no ARP entry "
                "and no cached MAC; keeping core fallback",
                file=sys.stderr,
            )
            continue

        api_matches = []
        for mac, vlan in records:
            for candidate in (fdb_candidates or {}).get(mac, []):
                candidate_vlan = candidate.get("vlan")
                if vlan is not None and candidate_vlan is not None and vlan != candidate_vlan:
                    continue
                api_matches.append((candidate, mac, vlan))
        # Deduplicate repeated LibreNMS rows without hiding true multi-switch
        # ambiguity from old FDB evidence.
        unique_api = {}
        for candidate, mac, vlan in api_matches:
            unique_api.setdefault(
                (candidate["switch_ip"], candidate["ifindex"], mac, vlan),
                (candidate, mac, vlan),
            )
        api_matches = list(unique_api.values())

        if mode == "librenms":
            if len(api_matches) == 1:
                candidate, mac, vlan = api_matches[0]
                selected = dict(candidate, mac=mac, vlan=vlan)
                found.append(_server_edge_from_candidate(
                    devices, server_ip, server_name, selected
                ))
            elif len(api_matches) > 1:
                print(
                    f"[WARN] server {server_name} ({server_ip}): LibreNMS FDB "
                    "evidence is ambiguous; keeping prior topology",
                    file=sys.stderr,
                )
            continue

        validated = []
        cached_switch = cached_switches.get(server_ip)
        if cached_switch:
            for mac, vlan in records:
                candidate = exact_candidate(cached_switch, mac, vlan)
                if candidate:
                    validated.append(candidate)
                    break
        if validated:
            print(
                f"[INFO] server {server_name} ({server_ip}): cached FDB owner "
                "verified; skipped candidate fan-out",
                file=sys.stderr,
            )
        else:
            for indexed, mac, vlan in api_matches:
                if indexed["switch_ip"] == cached_switch:
                    continue
                candidate = exact_candidate(
                    indexed["switch_ip"], mac, vlan, indexed=indexed
                )
                if candidate:
                    validated.append(candidate)

        if validated:
            max_depth = max(candidate["depth"] for candidate in validated)
            best = [item for item in validated if item["depth"] == max_depth]
            owners = {(item["switch_ip"], item["ifindex"]) for item in best}
            if len(owners) > 1:
                print(
                    f"[WARN] server {server_name} ({server_ip}): multiple exact "
                    "FDB owners remain; keeping prior topology",
                    file=sys.stderr,
                )
                continue
            selected = best[0]
            found.append(_server_edge_from_candidate(
                devices, server_ip, server_name, selected
            ))
            print(
                f"[INFO] server {server_name} ({server_ip}) attached to "
                f"{selected['switch_ip']} {selected['port_name'] or selected['ifindex']}",
                file=sys.stderr,
            )
            continue

        fallback_servers[server_ip] = server_name

    if fallback_servers:
        stats["full_fallbacks"] += len(fallback_servers)
        for server_ip, server_name in fallback_servers.items():
            print(
                f"[WARN] server {server_name} ({server_ip}): LibreNMS FDB "
                "candidate unavailable; using full direct-SNMP fallback",
                file=sys.stderr,
            )
        found.extend(discover_server_edges_direct(
            devices, edges, fallback_servers, community, cached_edges
        ))
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


def _external_neighbor_identities(invalidation_hints):
    """Return rejected LibreNMS neighbor identities safe for cache cleanup."""
    identities = set()
    for hint in invalidation_hints or []:
        if (
            not isinstance(hint, dict) or
            hint.get("kind") != "external-neighbor"
        ):
            continue
        local_ip = str(hint.get("local_ip") or "").strip()
        local_port = normalize_port_name(hint.get("local_port"))
        remote_name = normalize_hostname(hint.get("remote_name"))
        remote_port = normalize_port_name(hint.get("remote_port"))
        if local_ip and local_port and remote_name and remote_port:
            identities.add((local_ip, local_port, remote_name, remote_port))
    return identities


def _endpoint_invalidation_hints(invalidation_hints):
    """Return only current-cycle, explicitly scoped cache invalidation hints."""
    hints = []
    for hint in invalidation_hints or []:
        if (
            not isinstance(hint, dict) or
            hint.get("kind") != "endpoint-candidates"
        ):
            continue
        local_ip = str(hint.get("local_ip") or "").strip()
        local_ifindex = hint.get("local_ifindex")
        local_port = normalize_port_name(hint.get("local_port"))
        remote_ips = sorted({
            str(ip).strip() for ip in hint.get("remote_ips", []) if str(ip).strip()
        })
        if not local_ip or (local_ifindex in (None, "") and not local_port):
            continue
        if not remote_ips:
            continue
        hints.append({
            "local_ip": local_ip,
            "local_ifindex": str(local_ifindex)
            if local_ifindex not in (None, "") else "",
            "local_port": local_port,
            "remote_ips": remote_ips,
        })
    return sorted(
        hints,
        key=lambda item: (
            item["local_ip"], item["local_ifindex"], item["local_port"],
            tuple(item["remote_ips"]),
        ),
    )


def _matches_cache_invalidation_hint(edge, hints):
    for hint in hints:
        for local_side, remote_side in (("from", "to"), ("to", "from")):
            if str(edge.get(f"{local_side}_ip") or "").strip() != hint["local_ip"]:
                continue
            if str(edge.get(f"{remote_side}_ip") or "").strip() not in hint["remote_ips"]:
                continue
            if hint["local_ifindex"]:
                if str(edge.get(f"{local_side}_ifindex") or "") == hint["local_ifindex"]:
                    return True
            elif (
                hint["local_port"] and
                normalize_port_name(edge.get(f"{local_side}_port")) ==
                hint["local_port"]
            ):
                return True
    return False


def _matches_external_neighbor_identity(edge, identities):
    """Whether a cached edge is a formerly misresolved external neighbor."""
    for local_side, remote_side in (("from", "to"), ("to", "from")):
        identity = (
            str(edge.get(f"{local_side}_ip") or "").strip(),
            normalize_port_name(edge.get(f"{local_side}_port")),
            normalize_hostname(edge.get(f"{remote_side}_sysname")),
            normalize_port_name(edge.get(f"{remote_side}_port")),
        )
        if identity in identities:
            return True
    return False


def retain_cached_network_edges(live_edges, cached_edges, configured_device_ips,
                                now=None, retention_seconds=24 * 60 * 60,
                                devices=None, invalidation_hints=None):
    """Keep missing confirmed LLDP/CDP edges long enough to diagnose outages.

    Live observations replace matching cache entries. A cached edge is dropped
    immediately when one of its resolved physical endpoints is now occupied by
    a different live peer; otherwise it is retained as stale for the configured
    window. Self-edges and current-cycle external-device identity collisions
    are never retained. Server/FDB ownership has a separate durable ledger and
    is excluded.
    """
    now = time.time() if now is None else float(now)
    retention_seconds = max(0, int(retention_seconds))
    configured = set(configured_device_ips or [])
    external_neighbor_identities = _external_neighbor_identities(
        invalidation_hints
    )
    cache_invalidation_hints = _endpoint_invalidation_hints(
        invalidation_hints
    )
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
        resolved_endpoints = []
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
                resolved_endpoints.append((ip, str(ifindex)))
        # A partial edge cannot claim its one known port against every possible
        # historical peer. Its diagnostics hint performs the narrower
        # local-endpoint + candidate-remote invalidation instead.
        if len(resolved_endpoints) == 2:
            occupied.update(resolved_endpoints)

    for source in cached_edges or []:
        if not isinstance(source, dict) or source.get("source") == "fdb":
            continue
        left = str(source.get("from_ip") or "").strip()
        right = str(source.get("to_ip") or "").strip()
        if not left or not right or left not in configured or right not in configured:
            continue
        if left == right:
            continue
        if _matches_external_neighbor_identity(
            source, external_neighbor_identities
        ):
            continue
        if _matches_cache_invalidation_hint(source, cache_invalidation_hints):
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
    server_source = topology_server_attachment_source()
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
        f"source={data_source}; server-attachment-source={server_source}; "
        f"ARP only on {len(arp_device_ips)} L3 device(s)",
        file=sys.stderr,
    )
    librenms = None
    librenms_ready = False
    needs_librenms = (
        data_source != "direct-snmp"
        or (bool(servers) and server_source != "direct-snmp")
    )
    if needs_librenms:
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
            future = executor.submit(
                collect_device_by_source,
                ip,
                community,
                False,
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

    if librenms is None:
        adjacency_api_requests = 0
        server_api_start = 0
    elif data_source == "direct-snmp":
        adjacency_api_requests = 0
        # Inventory belongs to the only API consumer in this configuration.
        server_api_start = 0
    else:
        adjacency_api_requests = librenms.request_count
        server_api_start = librenms.request_count

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
    diagnostics_path = os.path.join(output_dir, "topology-diagnostics.json")
    cached_edges = load_cached_edges(edges_path)
    # Attachments have their own durable ledger.  edges.json is a live snapshot
    # and can legitimately be incomplete after a collector restart or a weak
    # SNMP cycle; it must not be the only memory of physical server ownership.
    name_index = build_name_index(devices)
    evidence_seen_at = time.time()
    cache_invalidation_hints = []
    edges, diagnostics_records = build_edges(
        devices, name_index, evidence_seen_at=evidence_seen_at,
        invalidation_hints=cache_invalidation_hints,
    )
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
    collect_server_arp(
        devices,
        arp_device_ips,
        servers,
        community,
        server_source,
        client=librenms,
        librenms_ready=librenms_ready,
        cached_edges=cached_attachments,
    )
    fdb_candidates = None
    fdb_status = {"usable_switches": 0, "failed_switches": 0}
    if servers and server_source != "direct-snmp":
        fdb_candidates, fdb_status = build_librenms_fdb_candidates(
            devices, edges, librenms, librenms_ready
        )
    server_stats = {"full_fallbacks": 0}
    # Always run the exact ARP+FDB ownership lookup.  A weaker LLDP/CDP edge
    # involving the same address must not suppress authoritative discovery.
    fresh_server_edges = discover_server_edges(
        devices,
        edges,
        servers,
        community,
        cached_attachments,
        source=server_source,
        fdb_candidates=fdb_candidates,
        server_stats=server_stats,
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
        invalidation_hints=cache_invalidation_hints,
    ) + server_edges
    write_json_atomic(edges_path, edges, sort_keys=True)
    write_json_atomic(attachments_path, confirmed_server_edges, sort_keys=True)
    try:
        write_topology_diagnostics(
            diagnostics_path, diagnostics_records,
            generated_at=evidence_seen_at,
        )
    except Exception as exc:
        print(
            f"[WARN] topology diagnostics write failed ({type(exc).__name__}); "
            "production topology outputs remain valid",
            file=sys.stderr,
        )

    stats = collection_stats_snapshot()
    server_api_requests = (
        librenms.request_count - server_api_start if librenms is not None else 0
    )
    print(
        f"[INFO] adjacency stats: api_requests={adjacency_api_requests} "
        f"snmp_walks={stats['direct_snmp_walks']} "
        f"snmp_gets={stats['direct_snmp_gets']}",
        file=sys.stderr,
    )
    print(
        f"[INFO] server attachment stats: api_requests={server_api_requests} "
        f"arp_snmp_walks={stats['server_snmp_walks']} "
        f"fdb_snmp_gets={stats['server_snmp_gets']} "
        f"full_fallbacks={server_stats['full_fallbacks']} "
        f"fdb_switches={fdb_status['usable_switches']} "
        f"fdb_failures={fdb_status['failed_switches']}",
        file=sys.stderr,
    )

    print(
        f"[INFO] wrote {len(edges)} edge(s), "
        f"cycle={time.monotonic() - cycle_started:.1f}s",
        file=sys.stderr,
    )
    log_unmatched_neighbors(diagnostics_records)
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
