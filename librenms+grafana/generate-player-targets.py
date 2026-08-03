#!/usr/bin/env python3
"""
Query stage switches via SNMP to map IP addresses to Team/Seat labels,
then generate a Prometheus file_sd JSON file for blackbox-exporter ICMP targets.

Usage (inside container):
  python3 generate-player-targets.py

Environment variables:
  TOURNAMENT_SWITCHES    comma-separated stage switch IPs/ranges (e.g. 192.168.10.11-22)
  SNMP_COMMUNITY         SNMP v2c community string for stage switches
  PLAYER_GATEWAYS        comma-separated L3 gateway IPs/ranges whose ARP table maps
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
  PLAYER_SWITCH_PROBE_TIMEOUT
                         SNMP timeout used while finding switches that have
                         team X-Y descriptions (default: 2 seconds).
  PLAYER_SWITCH_PROBE_WORKERS
                         concurrent description probes (default: 8).
  PLAYER_SWITCH_FULL_SCAN_INTERVAL
                         seconds between full TOURNAMENT_SWITCHES description
                         scans (default: 21600 / 6 hours). Between full scans, only the
                         previously confirmed stage switches are queried. A
                         cached-switch failure triggers an immediate full scan.
  PLAYER_SWITCH_CACHE_FILE
                         confirmed stage-switch cache path (default:
                         /targets/player_team_switches.json).
                         The cache is automatically scoped to the event name,
                         switch candidates, player gateways, VLANs and subnets;
                         applying a new project configuration invalidates it.
  PLAYER_SWITCH_FORCE_FULL_SCAN
                         true/false; force one full description scan. The
                         container sets this for manual immediate rescans.
  PLAYER_SNMP_DELAY_MS   pause after every switch SNMP request (default: 100
                         in Docker Compose; 0 when the script is run alone).
  PLAYER_REFRESH_FDB     true/false (default true). Ping player-subnet IPs
                         already present in gateway ARP before reading stage
                         FDB tables, so quiet but live clients are relearned.
  PLAYER_REFRESH_FDB_TIMEOUT
                         timeout for each FDB-refresh ping (default: 1 second).
  PLAYER_REFRESH_FDB_WORKERS
                         concurrent FDB-refresh pings (default: 64).
  PLAYER_VERIFY_PING     true/false (default true). Emit only addresses that
                         answer the generator's current ping check. This drops
                         stale historical IPs and follows player IP changes.
  PLAYER_OFFLINE_GRACE_SECONDS
                         Keep a previously successful but currently offline
                         player target for this many seconds (default: 300),
                         so rebooting PCs stay red/clickable instead of
                         disappearing from Prometheus during the outage.
  PLAYER_TARGET_HISTORY_LOOKBACK
                         Prometheus lookback used to recover last-known seat/IP
                         mappings after the target file was emptied (default: 24h).
  PROMETHEUS_URL         internal Prometheus URL used only for that recovery
                         (default: http://prometheus:9090).
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
                         true/false, skip the last host in each
                         wireless subnet (default: true)
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import re
import subprocess
import sys
import time
from ipaddress import IPv4Address, IPv4Network
from urllib import parse as urlparse, request as urlrequest

IF_ALIAS_OID = "1.3.6.1.2.1.31.1.1.1.18"
IF_OPER_STATUS_OID = "1.3.6.1.2.1.2.2.1.8"
ARP_IFINDEX_OID = "1.3.6.1.2.1.4.22.1.1"
ARP_NETADDR_OID = "1.3.6.1.2.1.4.22.1.3"
ARP_PHYSADDR_OID = "1.3.6.1.2.1.4.22.1.2"
BRIDGE_MIB_FDB_PORT_OID = "1.3.6.1.2.1.17.4.3.1.2"
BRIDGE_MIB_BASEPORT_OID = "1.3.6.1.2.1.17.1.4.1.2"
Q_BRIDGE_MIB_FDB_PORT_OID = "1.3.6.1.2.1.17.7.1.2.2.1.2"
Q_BRIDGE_MIB_VLAN_FDB_ID_OID = "1.3.6.1.2.1.17.7.1.4.2.1.3"
IF_OPER_STATUS_UP = 1

TEAM_RE = re.compile(r"team\s*0*(\d+)\s*[-_]\s*0*(\d+)", re.IGNORECASE)
STATIC_TEAM_RE = re.compile(r"(?:team\s*)?0*(\d+)\s*[-_]\s*0*(\d+)$", re.IGNORECASE)
VALID_NETWORKS = {"wired", "wireless"}
HEX_BYTE_RE = re.compile(r"[0-9a-fA-F]{1,2}")

TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}


def snmpwalk(host, community, oid, timeout=15):
    # One bounded attempt is enough on the local management network. Net-SNMP's
    # retry default multiplied every dead address in a discovery range into a
    # multi-minute pause, and a /24 player scan could consequently take hours.
    cmd = [
        "snmpwalk", "-v2c", "-c", community, "-O", "n",
        "-r", "0", "-t", str(timeout), host, oid,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
        return result.stdout
    except Exception as exc:
        print(f"[WARN] snmpwalk {host} {oid}: {exc}", file=sys.stderr)
        return ""
    finally:
        try:
            delay_ms = float(os.environ.get("PLAYER_SNMP_DELAY_MS", "0") or "0")
        except ValueError:
            delay_ms = 0
        if delay_ms > 0:
            time.sleep(min(delay_ms, 2000) / 1000)


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


def parse_dot1q_vlan_fdb_ids(output):
    """dot1qVlanFdbId -> {vlan_id: fdb_id}."""
    out = {}
    prefix = Q_BRIDGE_MIB_VLAN_FDB_ID_OID.strip(".").split(".")
    for line in output.strip().split("\n"):
        if "=" not in line:
            continue
        oid_str, value = line.split("=", 1)
        parts = oid_str.strip().strip(".").split(".")
        if parts[: len(prefix)] != prefix:
            continue
        suffix = parts[len(prefix):]
        if len(suffix) != 1:
            continue
        try:
            vlan_id = int(suffix[0])
        except ValueError:
            continue
        fdb_id = _int_from_snmp_value(value)
        if fdb_id is None or fdb_id < 0:
            continue
        out[vlan_id] = fdb_id
    return out


def parse_dot1q_fdb(output, fdb_id=None):
    """dot1qTpFdbPort -> {mac: bridgePort}.

    OID layout: <prefix>.<fdb-id>.<6 decimal mac octets> = INTEGER: bridgePort
    When ``fdb_id`` is provided, entries from every other forwarding database
    are ignored. Q-BRIDGE-MIB exposes the VLAN-to-FDB mapping separately via
    dot1qVlanFdbId, so callers must not assume this index is always the VLAN ID.
    """
    out = {}
    prefix = Q_BRIDGE_MIB_FDB_PORT_OID.strip(".").split(".")
    for line in output.strip().split("\n"):
        if "=" not in line:
            continue
        oid_str, value = line.split("=", 1)
        parts = oid_str.strip().strip(".").split(".")
        if parts[: len(prefix)] != prefix:
            continue
        suffix = parts[len(prefix):]
        if len(suffix) != 7:
            continue
        try:
            entry_fdb = int(suffix[0])
        except ValueError:
            continue
        if fdb_id is not None and entry_fdb != int(fdb_id):
            continue
        mac = mac_from_decimal_suffix(suffix[1:])
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


def collect_gateway_arp(gateways, community):
    """Collect and merge gateway ARP tables; the first gateway wins."""
    gateway_arp = {}
    for gateway in gateways:
        arp_out = snmpwalk(gateway, community, ARP_PHYSADDR_OID)
        entries = parse_arp_macaddr(arp_out)
        print(
            f"[INFO] gateway {gateway}: ARP entries = {len(entries)}",
            file=sys.stderr,
        )
        for ip, mac in entries.items():
            # For HA pairs, list the active gateway first so a stale standby
            # entry cannot mask the current IP-to-MAC mapping.
            gateway_arp.setdefault(ip, mac)
    return gateway_arp


def refresh_player_fdb(gateway_arp, wired_nets, timeout=1, workers=64,
                       probe_ping=ping_host):
    """Make live player clients talk before stage-switch FDB collection.

    A gateway ARP entry can outlive the access-switch CAM entry. Pinging the
    player from the monitoring host causes a live client to reply, which makes
    the stage switch relearn that client's source MAC on its ``team X-Y``
    access port. Only existing ARP entries inside PLAYER_SUBNETS are touched;
    this is a refresh of known hosts, not a subnet scan.
    """
    if not gateway_arp:
        return set()
    if not wired_nets:
        print(
            "[INFO] FDB refresh skipped: PLAYER_SUBNETS is empty",
            file=sys.stderr,
        )
        return set()

    candidates = sorted(
        (ip for ip in gateway_arp if ip_in_subnets(ip, wired_nets)),
        key=IPv4Address,
    )
    if not candidates:
        print(
            "[INFO] FDB refresh skipped: gateway ARP has no PLAYER_SUBNETS hosts",
            file=sys.stderr,
        )
        return set()

    alive = set()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(probe_ping, ip, timeout): ip for ip in candidates
        }
        for future in as_completed(futures):
            ip = futures[future]
            try:
                if future.result():
                    alive.add(ip)
            except Exception:
                pass

    print(
        f"[INFO] FDB refresh ping: {len(alive)}/{len(candidates)} "
        "known PLAYER_SUBNETS host(s) answered",
        file=sys.stderr,
    )
    return alive


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


def expand_ip_list(raw, label="IP list"):
    items = []
    seen = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            item = item.split(":", 1)[1].strip()
        try:
            values = expand_ip_range(item) if "-" in item else {str(IPv4Address(item))}
        except ValueError:
            print(f"[WARN] invalid {label} IP/range: {item}", file=sys.stderr)
            continue
        for ip in sorted(values, key=lambda value: int(IPv4Address(value))):
            if ip in seen:
                continue
            seen.add(ip)
            items.append(ip)
    return items


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


def _walk_vlan_mac_table(
    sw,
    community,
    vlan_context=False,
    qbridge_vlan_id=None,
    qbridge_community=None,
    allow_unfiltered_qbridge=True,
):
    """Walk one SNMP context for MAC->ifIndex.

    Returns (mac_to_ifindex, source_label, bridgeport_count).

    Tries BRIDGE-MIB (dot1dTpFdbPort + dot1dBasePortIfIndex) first since on
    Cisco the per-VLAN context exposes it directly. When `vlan_context` is
    False (the default-context call), it can fall back to Q-BRIDGE-MIB for
    vendors that only expose dot1qTpFdbPort. When player VLANs are configured,
    callers disable that unfiltered fallback and instead pass an explicit
    ``qbridge_vlan_id`` through the base community. This supports devices that
    do not implement community-indexed BRIDGE-MIB without mixing MAC addresses
    learned in other VLANs into the player result.
    """
    bp_out = snmpwalk(sw, community, BRIDGE_MIB_BASEPORT_OID)
    bp_map = parse_dot1d_baseport(bp_out)

    fdb_out = snmpwalk(sw, community, BRIDGE_MIB_FDB_PORT_OID)
    fdb_map = parse_dot1d_fdb(fdb_out)
    source = "BRIDGE-MIB"
    if not fdb_map and (allow_unfiltered_qbridge or qbridge_vlan_id is not None):
        fallback_community = qbridge_community or community
        if fallback_community != community:
            fallback_bp_out = snmpwalk(
                sw, fallback_community, BRIDGE_MIB_BASEPORT_OID
            )
            fallback_bp_map = parse_dot1d_baseport(fallback_bp_out)
            if fallback_bp_map:
                bp_map = fallback_bp_map
        qbridge_fdb_id = qbridge_vlan_id
        if qbridge_vlan_id is not None:
            vlan_fdb_out = snmpwalk(
                sw, fallback_community, Q_BRIDGE_MIB_VLAN_FDB_ID_OID
            )
            vlan_fdb_ids = parse_dot1q_vlan_fdb_ids(vlan_fdb_out)
            qbridge_fdb_id = vlan_fdb_ids.get(qbridge_vlan_id, qbridge_vlan_id)
        fdb_out = snmpwalk(sw, fallback_community, Q_BRIDGE_MIB_FDB_PORT_OID)
        fdb_map = parse_dot1q_fdb(fdb_out, fdb_id=qbridge_fdb_id)
        source = "Q-BRIDGE-MIB"
        if qbridge_vlan_id is not None:
            source += f" VLAN {qbridge_vlan_id}/FDB {qbridge_fdb_id}"

    mac_to_ifindex = {mac: bp_map.get(bp, bp) for mac, bp in fdb_map.items()}
    return mac_to_ifindex, source, len(bp_map)


def discover_team_switches(switches, community, timeout=2, workers=8,
                           probe_snmp=snmpwalk):
    """Find switches with at least one authoritative ``team X-Y`` ifAlias.

    Probe the explicit tournament-switch allowlist concurrently and do the
    expensive FDB/VLAN walks only on devices whose descriptions prove they
    serve player seats. General switch-management discovery is intentionally a
    separate pipeline and never becomes a player-seat candidate source.
    """
    candidates = list(dict.fromkeys(switches or []))
    if not candidates:
        return [], {}

    aliases = {}

    def probe(sw):
        output = probe_snmp(sw, community, IF_ALIAS_OID, timeout=timeout)
        return parse_ifalias(output)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {executor.submit(probe, sw): sw for sw in candidates}
        for future in as_completed(futures):
            sw = futures[future]
            try:
                mapping = future.result()
            except Exception as exc:
                print(f"[WARN] team-description probe {sw}: {exc}", file=sys.stderr)
                continue
            if mapping:
                aliases[sw] = mapping

    matched = [sw for sw in candidates if sw in aliases]
    described_ports = sum(len(aliases[sw]) for sw in matched)
    print(
        f"[INFO] team-description prefilter: {len(matched)}/{len(candidates)} "
        f"switches, {described_ports} team seat port(s)",
        file=sys.stderr,
    )
    return matched, aliases


def load_team_switch_cache(path):
    """Return ``(updated_at, switches, scope_key)`` from a JSON cache."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        updated_at = float(data.get("updated_at", 0))
        switches = [
            str(ip) for ip in data.get("switches", [])
            if isinstance(ip, str)
        ]
        scope_key = str(data.get("scope_key") or "")
        return updated_at, list(dict.fromkeys(switches)), scope_key
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return 0.0, [], ""


def save_team_switch_cache(path, switches, updated_at=None, scope_key=""):
    """Atomically persist confirmed team switches for later fast scans."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    payload = {
        "updated_at": float(time.time() if updated_at is None else updated_at),
        "switches": list(dict.fromkeys(switches or [])),
        "scope_key": str(scope_key or ""),
    }
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    os.replace(tmp, path)


def discover_team_switches_cached(
    switches,
    community,
    cache_file,
    full_scan_interval=21600,
    force_full_scan=False,
    timeout=2,
    workers=8,
    probe_snmp=snmpwalk,
    now=None,
    scope_key="",
):
    """Use confirmed switches between bounded full description scans.

    A cached switch that stops answering or loses every team description makes
    the function fall back to the complete candidate list immediately. This
    keeps transient failures self-healing without repeatedly walking the full
    tournament allowlist.
    """
    candidates = list(dict.fromkeys(switches or []))
    if not candidates:
        return [], {}

    current_time = time.time() if now is None else float(now)
    updated_at, cached, cached_scope = load_team_switch_cache(cache_file)
    if scope_key and cached_scope != scope_key:
        if cached:
            print(
                "[INFO] event/player network configuration changed; "
                "discarding the previous project stage-switch cache",
                file=sys.stderr,
            )
        updated_at, cached = 0.0, []
    candidate_set = set(candidates)
    cached = [ip for ip in cached if ip in candidate_set]
    cache_fresh = (
        cached
        and current_time >= updated_at
        and current_time - updated_at < max(0, full_scan_interval)
    )

    if cache_fresh and not force_full_scan:
        print(
            f"[INFO] team-description fast scan: probing {len(cached)} cached "
            f"switch(es); full candidate scan in "
            f"{int(full_scan_interval - (current_time - updated_at))}s",
            file=sys.stderr,
        )
        matched, aliases = discover_team_switches(
            cached,
            community,
            timeout=timeout,
            workers=min(workers, len(cached)),
            probe_snmp=probe_snmp,
        )
        if len(matched) == len(cached):
            return matched, aliases
        print(
            "[WARN] cached team switch missing/unlabeled; falling back to full scan",
            file=sys.stderr,
        )
    elif force_full_scan:
        print("[INFO] team-description full scan forced", file=sys.stderr)
    elif cached:
        print("[INFO] team-description cache expired; running full scan", file=sys.stderr)

    matched, aliases = discover_team_switches(
        candidates,
        community,
        timeout=timeout,
        workers=workers,
        probe_snmp=probe_snmp,
    )
    if matched:
        save_team_switch_cache(
            cache_file,
            matched,
            updated_at=current_time,
            scope_key=scope_key,
        )
    return matched, aliases


def build_team_switch_cache_scope(event_name, switches, gateways, vlan_ids,
                                  wired_nets):
    """Build a stable, non-secret identity for one event's player topology."""
    payload = {
        "event": str(event_name or "").strip(),
        "switches": sorted(set(switches or []), key=IPv4Address),
        "gateways": sorted(set(gateways or []), key=IPv4Address),
        "vlan_ids": sorted(set(int(item) for item in (vlan_ids or []))),
        "wired_subnets": sorted(str(net) for net in (wired_nets or [])),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def build_stage_mac_index(switches, community, vlan_ids=None,
                          prefetched_ifalias=None):
    """Return {sw_ip: {'ifalias': {ifIndex: {team, seat}},
                       'mac_to_ifindex': {mac: ifIndex},
                       'oper_status': {ifIndex: status_int}}}.

    If vlan_ids is set, queries those VLANs first via Cisco's
    community-indexing (community@vlan_id) so the
    per-VLAN BRIDGE-MIB tables (e.g. on Cisco IOS, where the default context
    only exposes VLAN 1) become visible. The default context is queried only
    when no VLAN is configured or all configured VLAN lookups return empty.
    This removes two redundant full-table walks per stage switch on the common
    Cisco deployment while preserving a safe fallback for other vendors.
    ifOperStatus is queried once on the default context and used downstream
    to filter stale MAC/ARP entries on currently-disconnected ports.
    """
    vlan_ids = vlan_ids or []
    index = {}
    prefetched_ifalias = prefetched_ifalias or {}
    for sw in switches:
        switch_started = time.monotonic()
        if sw in prefetched_ifalias:
            ifalias_map = prefetched_ifalias[sw]
        else:
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

        mac_to_ifindex = {}
        for vlan_id in vlan_ids:
            indexed_community = f"{community}@{vlan_id}"
            vlan_macs, vlan_source, vlan_bp = _walk_vlan_mac_table(
                sw,
                indexed_community,
                vlan_context=True,
                qbridge_vlan_id=vlan_id,
                qbridge_community=community,
            )
            print(
                f"[INFO] {sw}: VLAN {vlan_id} MAC entries ({vlan_source}) = {len(vlan_macs)} "
                f"(bridgePort->ifIndex = {vlan_bp})",
                file=sys.stderr,
            )
            for mac, ifx in vlan_macs.items():
                mac_to_ifindex.setdefault(mac, ifx)

        if not vlan_ids or not mac_to_ifindex:
            if vlan_ids:
                print(
                    f"[WARN] {sw}: configured VLAN FDB lookups were empty; "
                    "falling back to the default SNMP context",
                    file=sys.stderr,
                )
            default_macs, default_source, default_bp = _walk_vlan_mac_table(
                sw,
                community,
                allow_unfiltered_qbridge=not vlan_ids,
            )
            print(
                f"[INFO] {sw}: bridgePort->ifIndex entries = {default_bp}",
                file=sys.stderr,
            )
            print(
                f"[INFO] {sw}: default-context MAC entries ({default_source}) = {len(default_macs)}",
                file=sys.stderr,
            )
            for mac, ifx in default_macs.items():
                mac_to_ifindex.setdefault(mac, ifx)
        else:
            print(
                f"[INFO] {sw}: skipped redundant default-context FDB; "
                "configured VLAN data is authoritative",
                file=sys.stderr,
            )

        print(
            f"[INFO] {sw}: combined MAC table entries = {len(mac_to_ifindex)}, "
            f"snmp={time.monotonic() - switch_started:.1f}s",
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


def eligible_team_seats(stage_index, require_link_up=True):
    """Return link-eligible ``(team, seat) -> [(switch, ifIndex)]``."""
    eligible = {}
    for switch, data in stage_index.items():
        oper_status = data.get("oper_status", {})
        for ifindex, team_info in data.get("ifalias", {}).items():
            oper = oper_status.get(ifindex)
            if require_link_up and oper is not None and oper != IF_OPER_STATUS_UP:
                continue
            seat = (int(team_info["team"]), int(team_info["seat"]))
            eligible.setdefault(seat, []).append((switch, ifindex))
    return eligible


def _canonical_player_target(target):
    """Validate untrusted file/Prometheus data and return file_sd shape."""
    if not isinstance(target, dict):
        return None
    targets = target.get("targets") or []
    labels = target.get("labels") or {}
    if not targets or not isinstance(labels, dict):
        return None
    ip = str(targets[0] or "").strip()
    try:
        ip = str(IPv4Address(ip))
        team = int(labels.get("team"))
        seat = int(labels.get("seat"))
    except (TypeError, ValueError):
        return None
    if team < 1 or seat < 1:
        return None
    network = str(labels.get("network") or "wired").strip().lower()
    if network not in VALID_NETWORKS:
        return None
    return {
        "targets": [ip],
        "labels": {
            "team": str(team),
            "seat": str(seat),
            "switch": str(labels.get("switch") or "last-known").strip(),
            "network": network,
            "role": "player",
        },
    }


def load_previous_player_targets(path):
    """Load the previous file_sd output as a last-known mapping source."""
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    targets = []
    for item in raw:
        target = _canonical_player_target(item)
        if target:
            targets.append(target)
    if targets:
        print(
            f"[INFO] loaded {len(targets)} last-known target(s) from {path}",
            file=sys.stderr,
        )
    return targets


def fetch_prometheus_player_history(base_url, lookback="24h", timeout=5,
                                    opener=urlrequest.urlopen):
    """Recover file_sd labels from recently stored player-ping series."""
    lookback = str(lookback or "24h").strip()
    if not re.fullmatch(r"[1-9]\d*[smhdwy]", lookback):
        print(
            f"[WARN] invalid PLAYER_TARGET_HISTORY_LOOKBACK={lookback!r}; using 24h",
            file=sys.stderr,
        )
        lookback = "24h"
    query = f'max_over_time(probe_success{{job="player-ping"}}[{lookback}])'
    url = f"{str(base_url).rstrip('/')}/api/v1/query?{urlparse.urlencode({'query': query})}"
    try:
        with opener(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        print(f"[WARN] Prometheus player-history recovery failed: {exc}", file=sys.stderr)
        return []

    targets = []
    for row in payload.get("data", {}).get("result", []):
        metric = row.get("metric") or {}
        target = _canonical_player_target({
            "targets": [metric.get("target_ip") or metric.get("instance")],
            "labels": metric,
        })
        if target:
            targets.append(target)
    targets = dedupe_player_targets(targets, dedupe_seats=False)
    if targets:
        print(
            f"[INFO] recovered {len(targets)} historical target candidate(s) "
            f"from Prometheus [{lookback}]",
            file=sys.stderr,
        )
    return targets


def fetch_recent_successful_player_ips(base_url, grace_seconds=300, timeout=5,
                                       opener=urlrequest.urlopen):
    """Return player IPs with at least one successful probe in the grace window.

    Prometheus has the exact probe timeline, unlike the slower SNMP discovery
    loop.  ``max_over_time`` therefore lets a rebooting PC remain a red target
    for the requested window without keeping truly stale historical IPs.
    """
    grace_seconds = max(0, int(grace_seconds or 0))
    if grace_seconds <= 0:
        return set()
    query = (
        'max_over_time(probe_success{job="player-ping"}'
        f'[{grace_seconds}s])'
    )
    url = f"{str(base_url).rstrip('/')}/api/v1/query?{urlparse.urlencode({'query': query})}"
    try:
        with opener(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        print(f"[WARN] Prometheus player grace lookup failed: {exc}", file=sys.stderr)
        return set()

    recent = set()
    for row in payload.get("data", {}).get("result", []):
        value = row.get("value") or []
        try:
            successful = float(value[1]) >= 0.5
        except (IndexError, TypeError, ValueError):
            successful = False
        if not successful:
            continue
        metric = row.get("metric") or {}
        raw_ip = metric.get("target_ip") or metric.get("instance")
        try:
            recent.add(str(IPv4Address(str(raw_ip or "").strip())))
        except ValueError:
            continue
    return recent


def retain_last_known_wired_targets(current, historical, stage_index,
                                     require_link_up=True):
    """Fill currently-unmapped link-up seats with their last-known IPs.

    The gateway can retain IP/MAC ARP state long after an access switch ages
    out the same source MAC. Core ARP alone cannot identify a ``team X-Y``
    access port, so preserve the last proven association until that described
    port goes link-down or a new live FDB association replaces it.
    """
    eligible = eligible_team_seats(stage_index, require_link_up)
    current_seats = {
        (int(target["labels"]["team"]), int(target["labels"]["seat"]))
        for target in current
        if target.get("labels", {}).get("network") == "wired"
    }
    missing = set(eligible) - current_seats
    retained = []
    seen = {
        (target["targets"][0], target["labels"]["team"], target["labels"]["seat"])
        for target in current
    }
    retained_seats = set()
    for raw in historical or []:
        target = _canonical_player_target(raw)
        if not target or target["labels"]["network"] != "wired":
            continue
        seat_key = (int(target["labels"]["team"]), int(target["labels"]["seat"]))
        if seat_key not in missing:
            continue
        unique = (target["targets"][0], str(seat_key[0]), str(seat_key[1]))
        if unique in seen:
            continue
        # Refresh the switch label from today's authoritative descriptions.
        target["labels"]["switch"] = eligible[seat_key][0][0]
        retained.append(target)
        retained_seats.add(seat_key)
        seen.add(unique)
    if retained:
        print(
            f"[INFO] retained {len(retained_seats)} link-up seat(s) from "
            f"{len(retained)} last-known IP candidate(s)",
            file=sys.stderr,
        )
    return list(current) + retained


def summarize_team_mapping(stage_index, targets, require_link_up=True):
    """Log which link-up ``team X-Y`` seats still lack an IP/MAC mapping."""
    eligible = eligible_team_seats(stage_index, require_link_up)

    mapped = {
        (int(target["labels"]["team"]), int(target["labels"]["seat"]))
        for target in targets
        if target.get("labels", {}).get("switch") in stage_index
    }
    missing = sorted(set(eligible) - mapped)
    print(
        f"[INFO] team-seat mapping: {len(set(eligible) & mapped)}/"
        f"{len(eligible)} link-up described seat(s) mapped",
        file=sys.stderr,
    )
    if missing:
        details = []
        for team, seat in missing:
            locations = ",".join(
                f"{switch}/ifIndex{ifindex}"
                for switch, ifindex in eligible[(team, seat)]
            )
            details.append(f"team {team}-{seat} ({locations})")
        print(
            "[WARN] link-up seats without VLAN MAC/ARP mapping: "
            + "; ".join(details),
            file=sys.stderr,
        )
    return {
        "eligible": set(eligible),
        "mapped": set(eligible) & mapped,
        "missing": missing,
    }


def dedupe_player_targets(*priority_groups, dedupe_seats=True):
    """Merge source groups from highest to lowest priority.

    Explicit static mappings beat SNMP discovery, which beats synthetic
    wireless-scan seats. A target is unique both by IP and by
    (team, seat, network), preventing the same live host or seat from being
    scraped/counting twice when sources overlap.
    """
    seen_ips = set()
    seen_seats = set()
    merged = []
    for group in priority_groups:
        for target in group or []:
            targets = target.get("targets") or []
            labels = target.get("labels") or {}
            if not targets:
                continue
            ip = str(targets[0])
            seat_key = (
                str(labels.get("team") or ""),
                str(labels.get("seat") or ""),
                str(labels.get("network") or ""),
            )
            if ip in seen_ips or (dedupe_seats and seat_key in seen_seats):
                continue
            seen_ips.add(ip)
            if dedupe_seats:
                seen_seats.add(seat_key)
            merged.append(target)
    return merged


def filter_reachable_targets(targets, timeout=1, workers=64,
                             retain_unreachable_ips=None, grace_seconds=0):
    """Keep only candidates that answer the current ICMP verification.

    A seat can temporarily have both an old and a new IP because MAC and ARP
    caches age at different rates. Filtering before final seat de-duplication
    drops the stale address and lets the current live address take the seat.
    Historical mappings remain useful for quiet clients whose switch FDB entry
    aged out. A recently successful address may remain emitted for a short
    grace window so Prometheus can keep probing it while the PC reboots.
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

    grace_ips = set(retain_unreachable_ips or [])
    retained = [
        t for t in targets
        if t["targets"][0] not in alive and t["targets"][0] in grace_ips
    ]
    reachable = [
        t for t in targets
        if t["targets"][0] in alive or t["targets"][0] in grace_ips
    ]
    unreachable_count = len(targets) - len(reachable)
    print(
        f"[INFO] active ping verify: {len(alive)}/{len(candidate_ips)} IPs alive, "
        f"retained {len(retained)} offline target(s) in {int(grace_seconds or 0)}s red grace, "
        f"dropped {unreachable_count} unreachable/stale candidate(s)",
        file=sys.stderr,
    )
    if unreachable_count:
        dropped = []
        for target in targets:
            ip = target["targets"][0]
            if ip in alive or ip in grace_ips:
                continue
            labels = target.get("labels") or {}
            dropped.append(
                f"team {labels.get('team', '?')}-{labels.get('seat', '?')} "
                f"{labels.get('network', '?')}={ip}"
            )
        print(
            "[WARN] unreachable/stale player candidate(s) omitted "
            "(not currently eligible for red grace): "
            + "; ".join(dropped),
            file=sys.stderr,
        )
    return reachable


def atomic_write_json(path, data):
    """Write `data` as JSON to `path` atomically.

    Prometheus file_sd watches this path with inotify and re-reads it on every
    change. A plain open(path, "w") truncates the file to 0 bytes before
    json.dump writes anything, so a read landing in that window fails with
    "unexpected end of JSON input" (and spams Prometheus's logs every poll).

    Writing to a temp file in the same directory and os.replace()-ing it onto
    the final path makes the swap atomic: a reader sees either the old file or
    the fully-written new one, never a half-written or empty file. An empty
    list still serializes to a valid "[]", never a 0-byte file.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def main():
    cycle_started = time.monotonic()
    switches_raw = os.environ.get("TOURNAMENT_SWITCHES", "")
    community = os.environ.get("SNMP_COMMUNITY", "global")
    gateways_raw = os.environ.get("PLAYER_GATEWAYS", "").strip()
    if not gateways_raw:
        gateways_raw = os.environ.get("LIBRENMS_CORE_IP", "").strip()
    if not gateways_raw:
        # Last resort: derive the core switch IP from CORE_SWITCH_PING ("Core:192.168.10.254")
        core_first = os.environ.get("CORE_SWITCH_PING", "").strip().split(",")[0].strip()
        if ":" in core_first:
            core_first = core_first.split(":", 1)[1]
        gateways_raw = core_first.split("-")[0].strip()
    gateways = expand_ip_list(gateways_raw, "PLAYER_GATEWAYS")
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
    switch_probe_timeout = env_int(
        "PLAYER_SWITCH_PROBE_TIMEOUT", 2, minimum=1, maximum=15
    )
    switch_probe_workers = env_int(
        "PLAYER_SWITCH_PROBE_WORKERS", 8, minimum=1, maximum=256
    )
    switch_full_scan_interval = env_int(
        "PLAYER_SWITCH_FULL_SCAN_INTERVAL", 21600, minimum=60, maximum=86400
    )
    switch_cache_file = os.environ.get(
        "PLAYER_SWITCH_CACHE_FILE", "/targets/player_team_switches.json"
    )
    switch_candidates = (
        expand_ip_list(switches_raw, "TOURNAMENT_SWITCHES")
        if switches_raw else []
    )
    team_cache_scope = build_team_switch_cache_scope(
        os.environ.get("EVENT_NAME", ""),
        switch_candidates,
        gateways,
        player_vlan_ids,
        wired_nets,
    )
    _cache_time, cached_project_switches, cached_project_scope = (
        load_team_switch_cache(switch_cache_file)
    )
    project_scope_changed = bool(
        cached_project_switches and cached_project_scope != team_cache_scope
    )
    switch_force_full_scan = env_bool(
        "PLAYER_SWITCH_FORCE_FULL_SCAN", default=False
    )
    refresh_fdb = env_bool("PLAYER_REFRESH_FDB", default=True)
    refresh_fdb_timeout = env_int(
        "PLAYER_REFRESH_FDB_TIMEOUT", 1, minimum=1, maximum=5
    )
    refresh_fdb_workers = env_int(
        "PLAYER_REFRESH_FDB_WORKERS", 64, minimum=1, maximum=256
    )
    static_targets_raw = os.environ.get("PLAYER_STATIC_TARGETS", "")
    static_default_network = os.environ.get("PLAYER_STATIC_NETWORK", "wireless")
    wireless_scan_enabled = env_bool("PLAYER_WIRELESS_SCAN", default=True) or env_bool("PLAYER_WIRELESS_PREVIEW")
    wireless_scan_limit = env_int_alias("PLAYER_WIRELESS_SCAN_LIMIT", "PLAYER_WIRELESS_PREVIEW_LIMIT", 0, minimum=0, maximum=4096)
    wireless_scan_team_size = env_int_alias("PLAYER_WIRELESS_SCAN_TEAM_SIZE", "PLAYER_WIRELESS_PREVIEW_TEAM_SIZE", 5, minimum=1, maximum=50)
    wireless_scan_timeout = env_int_alias("PLAYER_WIRELESS_SCAN_TIMEOUT", "PLAYER_WIRELESS_PREVIEW_TIMEOUT", 1, minimum=1, maximum=5)
    wireless_scan_workers = env_int_alias("PLAYER_WIRELESS_SCAN_WORKERS", "PLAYER_WIRELESS_PREVIEW_WORKERS", 64, minimum=1, maximum=256)
    wireless_scan_max_hosts = env_int_alias("PLAYER_WIRELESS_SCAN_MAX_HOSTS", "PLAYER_WIRELESS_PREVIEW_MAX_HOSTS", 512, minimum=1, maximum=4096)
    wireless_scan_exclude = load_excluded_ips("PLAYER_WIRELESS_SCAN_EXCLUDE")
    offline_grace_seconds = env_int(
        "PLAYER_OFFLINE_GRACE_SECONDS", 300, minimum=0, maximum=3600
    )
    if env_bool("PLAYER_WIRELESS_SCAN_EXCLUDE_GATEWAYS", default=True):
        wireless_scan_exclude.update(gateway_like_ips(wireless_nets))
    output_file = os.environ.get("PLAYER_TARGETS_FILE", "/etc/prometheus/player_targets.json")
    if project_scope_changed:
        previous_targets = []
        print(
            "[INFO] new event/player network scope: ignoring previous project "
            "player targets and red-grace history",
            file=sys.stderr,
        )
    else:
        previous_targets = load_previous_player_targets(output_file)
    if not previous_targets and not project_scope_changed:
        previous_targets = fetch_prometheus_player_history(
            os.environ.get("PROMETHEUS_URL", "http://prometheus:9090"),
            os.environ.get("PLAYER_TARGET_HISTORY_LOOKBACK", "24h"),
        )

    scan_targets = []
    discovered_targets = []

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

    static_targets = parse_static_player_targets(
        static_targets_raw, wired_nets, wireless_nets, static_default_network
    )
    if static_targets:
        print(f"[INFO] loaded {len(static_targets)} static player targets", file=sys.stderr)

    if not switches_raw:
        print("[INFO] TOURNAMENT_SWITCHES not set, skipping SNMP target discovery", file=sys.stderr)
    else:
        switches = list(switch_candidates)

        if not switches:
            print("[WARN] TOURNAMENT_SWITCHES has no valid IPs, skipping SNMP target discovery", file=sys.stderr)
        else:
            gateway_arp = {}
            if gateways:
                gateway_arp = collect_gateway_arp(gateways, gateway_community)
                if refresh_fdb:
                    refresh_player_fdb(
                        gateway_arp,
                        wired_nets,
                        timeout=refresh_fdb_timeout,
                        workers=refresh_fdb_workers,
                    )
            else:
                print(
                    "[INFO] PLAYER_GATEWAYS / LIBRENMS_CORE_IP not set; "
                    "skipping gateway-ARP path",
                    file=sys.stderr,
                )

            switches, prefetched_ifalias = discover_team_switches_cached(
                switches,
                community,
                switch_cache_file,
                full_scan_interval=switch_full_scan_interval,
                force_full_scan=switch_force_full_scan,
                timeout=switch_probe_timeout,
                workers=switch_probe_workers,
                scope_key=team_cache_scope,
            )
            if not switches:
                print(
                    "[WARN] no switches with team X-Y descriptions found; "
                    "skipping wired player discovery",
                    file=sys.stderr,
                )
            stage_index = build_stage_mac_index(
                switches,
                community,
                player_vlan_ids,
                prefetched_ifalias=prefetched_ifalias,
            )

            path_a_targets = collect_direct_arp_targets(
                switches, community, stage_index, wireless_nets, require_link_up
            )
            print(
                f"[INFO] direct-ARP-on-stage produced {len(path_a_targets)} targets",
                file=sys.stderr,
            )

            path_b_targets = []
            if gateway_arp:
                path_b_targets, stats = join_gateway_arp_to_teams(
                    gateway_arp, stage_index, wireless_nets, require_link_up
                )
                print(
                    f"[INFO] gateway-ARP join: matched {stats['matched']} IPs, "
                    f"{stats['unmatched_macs']} MACs had no stage port, "
                    f"{stats['skipped_link_down']} skipped (link down)",
                    file=sys.stderr,
                )
            elif gateways:
                print(
                    "[WARN] gateway ARP tables were empty; gateway-ARP path produced no targets",
                    file=sys.stderr,
                )

            merged = merge_dedup_targets(path_b_targets, path_a_targets)
            merged = retain_last_known_wired_targets(
                merged,
                previous_targets,
                stage_index,
                require_link_up,
            )
            summarize_team_mapping(stage_index, merged, require_link_up)

            per_team = {}
            for target in merged:
                key = (target["labels"]["team"], target["labels"]["network"])
                per_team[key] = per_team.get(key, 0) + 1
            for (team, net), count in sorted(per_team.items(), key=lambda kv: (int(kv[0][0]), kv[0][1])):
                print(f"[INFO] team {team} {net}: {count} target(s)", file=sys.stderr)

            discovered_targets.extend(merged)

    verify_ping = env_bool("PLAYER_VERIFY_PING", default=True)
    recent_successful_ips = set()
    grace_targets = []
    if verify_ping and offline_grace_seconds > 0:
        recent_successful_ips = fetch_recent_successful_player_ips(
            os.environ.get("PROMETHEUS_URL", "http://prometheus:9090"),
            offline_grace_seconds,
        )
        grace_targets = [
            target for target in previous_targets
            if target.get("targets")
            and target["targets"][0] in recent_successful_ips
        ]
        if grace_targets:
            print(
                f"[INFO] carrying {len(grace_targets)} recently successful "
                f"target(s) through the {offline_grace_seconds}s red grace",
                file=sys.stderr,
            )

    # Keep alternate IPs for the same seat until the active-ping pass filters
    # stale candidates. Otherwise an old high-priority mapping can suppress the
    # current live address after a player IP change.
    all_targets = dedupe_player_targets(
        static_targets,
        discovered_targets,
        scan_targets,
        grace_targets,
        dedupe_seats=False,
    )
    dropped_duplicates = (
        len(static_targets) + len(discovered_targets) + len(scan_targets)
        + len(grace_targets) - len(all_targets)
    )
    if dropped_duplicates:
        print(f"[INFO] removed {dropped_duplicates} duplicate player target(s) across sources", file=sys.stderr)

    if verify_ping and all_targets:
        all_targets = filter_reachable_targets(
            all_targets,
            retain_unreachable_ips=recent_successful_ips,
            grace_seconds=offline_grace_seconds,
        )

    all_targets = dedupe_player_targets(all_targets)

    all_targets.sort(key=lambda t: (
        int(t["labels"]["team"]),
        int(t["labels"]["seat"]),
        t["labels"]["network"],
    ))

    atomic_write_json(output_file, all_targets)

    print(
        f"[INFO] generated {len(all_targets)} player targets -> {output_file} "
        f"in {time.monotonic() - cycle_started:.1f}s",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
