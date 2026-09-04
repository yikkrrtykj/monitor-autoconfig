#!/usr/bin/env python3
"""Discover ISP ping targets with LibreNMS inventory and live route evidence.

By default LibreNMS supplies the firewall port/address inventory already
collected by its poller. The firewall is queried directly only for live carrier
gateway evidence (IP-FORWARD-MIB ipCidrRouteTable, falling back to the RFC1213
ipRouteTable), or as a per-device compatibility fallback. WAN aliases remain
useful hints, but generic names such as ethernet0/0 also work when the route
ifIndex points at a public interface.
Discovered gateways are written to a Prometheus file_sd. Console ISP metadata
is applied only when its public WAN IP or an IF-MIB label matches exactly, so
ifIndex changes cannot move a name or bandwidth limit onto another carrier.
Some firewalls do not expose either standard route table over SNMP.  In that
case the public address and subnet on each WAN interface are used to derive the
usual first-host carrier gateway, so topology keeps the current WAN address
instead of showing an empty placeholder.

Manual entries win: any discovered gateway whose IP is already listed in
ISP_PING is skipped, so hand-tuned names/targets are never duplicated. Not
every firewall exposes its routing table over SNMP, and a standby line whose
default route is inactive has no next hop to read -- keep manual entries for
those; discovery only ever adds targets.

Env vars:
  ISP_GATEWAY_AUTO_DISCOVER   true = enabled (default true)
  FIREWALL_SNMP_TARGETS       firewall SNMP address(es), NAME:IP comma list
  FIREWALL_UNIT_SNMP_TARGETS  non-empty marks HA physical-unit mode
  FIREWALL_SNMP_COMMUNITY     community (falls back to SNMP_COMMUNITY)
  FIREWALL_WAN_IF_FILTER      WAN interface keywords (same as the bridge)
  BIGSCREEN_ISP_NAMES         console ISP metadata names
  BIGSCREEN_ISP_IPS           NAME:public-IP metadata used for stable matching
  ISP_PING                    manual targets, their IPs are excluded here
  ISP_TARGETS_FILE            output path (default /targets/isp_targets.json)
  ISP_DISCOVERY_SNMP_TIMEOUT  per-walk SNMP timeout seconds (default 2)
  ISP_DISCOVERY_SOURCE        hybrid (default), librenms, or direct-snmp
  ISP_LIBRENMS_POLL_MAX_AGE_SECONDS
                              max explicit last_polled age (default 600)
  LIBRENMS_URL                API URL (default http://librenms:8000)
  LIBRENMS_API_TOKEN          API token when token file is absent
  LIBRENMS_TOKEN_FILE         token file (default /librenms-data/librenms-api-token)
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from librenms_client import LibreNMSClient, LibreNMSError, age_seconds
from target_utils import (
    expand_ipv4_targets,
    is_ipv4 as looks_like_ip,
    write_json_atomic,
)

write_file_sd = write_json_atomic

OID_IF_DESCR = ".1.3.6.1.2.1.2.2.1.2"
OID_IF_NAME = ".1.3.6.1.2.1.31.1.1.1.1"
OID_IF_ALIAS = ".1.3.6.1.2.1.31.1.1.1.18"
OID_IP_AD_ENT_IFINDEX = ".1.3.6.1.2.1.4.20.1.2"
OID_IP_AD_ENT_NETMASK = ".1.3.6.1.2.1.4.20.1.3"
# ipCidrRouteTable rows for dest 0.0.0.0 mask 0.0.0.0 only (default routes;
# supports several next hops for multi-WAN).
OID_CIDR_DEFAULT_NEXTHOP = ".1.3.6.1.2.1.4.24.4.1.4.0.0.0.0.0.0.0.0"
OID_CIDR_DEFAULT_IFINDEX = ".1.3.6.1.2.1.4.24.4.1.5.0.0.0.0.0.0.0.0"
# RFC1213 ipRouteTable fallback (single default route).
OID_ROUTE_DEFAULT_NEXTHOP = ".1.3.6.1.2.1.4.21.1.7.0.0.0.0"
OID_ROUTE_DEFAULT_IFINDEX = ".1.3.6.1.2.1.4.21.1.2.0.0.0.0"

_WALK_LINE = re.compile(r"^(\.[\d.]+)\s*=\s*(?:[A-Za-z0-9-]+:\s*)?(.*)$")
ISP_DISCOVERY_SOURCES = frozenset({"hybrid", "librenms", "direct-snmp"})
DEFAULT_ISP_LIBRENMS_POLL_MAX_AGE_SECONDS = 600
_collection_stats = {
    "snmp_walks": 0,
    "snmp_gets": 0,
    "snmp_successes": 0,
    "snmp_failures": 0,
    "snmp_label_successes": 0,
    "snmp_address_successes": 0,
}


class ISPDataIncomplete(RuntimeError):
    """LibreNMS inventory cannot safely drive the existing ISP mapping."""


def reset_collection_stats() -> None:
    for key in _collection_stats:
        _collection_stats[key] = 0


def collection_stats() -> dict[str, int]:
    return dict(_collection_stats)


def direct_collection_succeeded(before: dict[str, int], results: list[dict]) -> bool:
    """Require both IF-MIB identity and IP-MIB address responses for valid zero."""
    if results:
        return True
    after = collection_stats()
    return (
        after["snmp_label_successes"] > before["snmp_label_successes"]
        and after["snmp_address_successes"] > before["snmp_address_successes"]
    )


def isp_discovery_source() -> str:
    value = os.environ.get("ISP_DISCOVERY_SOURCE", "hybrid").strip().lower()
    if value in ISP_DISCOVERY_SOURCES:
        return value
    print(
        f"[isp-discovery] unsupported ISP_DISCOVERY_SOURCE={value!r}; using hybrid",
        file=sys.stderr,
    )
    return "hybrid"


def isp_librenms_poll_max_age() -> int:
    try:
        value = int(os.environ.get(
            "ISP_LIBRENMS_POLL_MAX_AGE_SECONDS",
            str(DEFAULT_ISP_LIBRENMS_POLL_MAX_AGE_SECONDS),
        ) or DEFAULT_ISP_LIBRENMS_POLL_MAX_AGE_SECONDS)
    except (TypeError, ValueError):
        return DEFAULT_ISP_LIBRENMS_POLL_MAX_AGE_SECONDS
    return max(0, value)


def inventory_freshness(metadata: dict) -> str:
    age = age_seconds((metadata or {}).get("last_polled"))
    if age is None:
        return "unknown"
    return "fresh" if 0 <= age <= isp_librenms_poll_max_age() else "stale"


def parse_walk(text: str) -> dict[str, str]:
    """snmpwalk -On output -> {oid: value} with quotes/whitespace stripped."""
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        match = _WALK_LINE.match(line.strip())
        if not match:
            continue
        value = match.group(2).strip().strip('"').strip()
        if value.lower().startswith(("no such", "no more")):
            continue
        out[match.group(1)] = value
    return out


def suffix_of(oid: str, base: str) -> str:
    return oid[len(base) + 1:] if oid.startswith(base + ".") else ""


def wan_keywords(raw: str) -> list[str]:
    return [part.strip().lower() for part in (raw or "").split(",") if part.strip()]


def is_wan_label(label: str, keywords: list[str]) -> bool:
    # Same matching as the bridge: keywords ending in a digit bind on a
    # boundary so eth1 does not also claim eth10~eth15.
    lower = (label or "").lower()
    for keyword in keywords:
        if keyword[-1:].isdigit():
            if re.search(re.escape(keyword) + r"(?:\D|$)", lower):
                return True
        elif keyword in lower:
            return True
    return False


def _ip_int(ip: str) -> int:
    return sum(int(part) << (8 * (3 - idx)) for idx, part in enumerate(ip.split(".")))


def same_subnet(ip_a: str, ip_b: str, mask: str) -> bool:
    try:
        m = _ip_int(mask)
        return (_ip_int(ip_a) & m) == (_ip_int(ip_b) & m)
    except (ValueError, IndexError):
        return False


def target_ips(raw: str) -> list[str]:
    return expand_ipv4_targets(raw)


def named_ipv4_targets(raw: str) -> dict[str, str]:
    """Parse explicit NAME:IPv4 rows without inventing positional identity."""
    targets: dict[str, str] = {}
    for entry in str(raw or "").replace("\n", ",").split(","):
        name, separator, value = entry.strip().partition(":")
        name = name.strip()
        value = value.strip()
        if separator and name and looks_like_ip(value):
            targets[name] = value
    return targets


def _public_wan_address(value: str) -> bool:
    """True for a routable WAN address, false for LAN/link-local addresses.

    Interface descriptions are not reliable on every firewall (some expose only
    ``ethernet0/0`` etc.).  A public address tied to a default-route ifIndex is
    strong enough evidence that the interface is a WAN link.
    """
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (
        address.is_private or address.is_loopback or address.is_link_local
        or address.is_multicast or address.is_unspecified
    )


def _subnet_gateway(wan_ip: str, mask: str) -> str:
    """Best-effort carrier next hop for route-table-less static WAN links."""
    try:
        network = ipaddress.IPv4Network((wan_ip, mask), strict=False)
        if network.prefixlen <= 30:
            candidate = ipaddress.IPv4Address(int(network.network_address) + 1)
            if str(candidate) != wan_ip and candidate < network.broadcast_address:
                return str(candidate)
    except (ipaddress.AddressValueError, ipaddress.NetmaskValueError, ValueError):
        pass
    # /31, /32, and malformed masks do not contain a safely inferable carrier
    # gateway. Never turn the firewall's own address into a fake ping target.
    return ""


def bind_manual_metadata(results: list[dict], configured_names: list[str] | None,
                         configured_ips: dict[str, str] | None = None) -> set[str]:
    """Apply manual display identity only through stable, unambiguous evidence.

    Public WAN IP is authoritative when configured. Otherwise any exact,
    case-insensitive IF-MIB alias/name/description match is accepted. The
    return value contains metadata names that were safely bound.
    """
    names = list(dict.fromkeys(
        str(name).strip() for name in (configured_names or []) if str(name).strip()
    ))
    ip_by_name = {
        str(name).strip().casefold(): str(value).strip()
        for name, value in (configured_ips or {}).items()
        if str(name).strip() and looks_like_ip(str(value).strip())
    }
    claimed: set[int] = set()
    matched: set[str] = set()
    for name in names:
        configured_ip = ip_by_name.get(name.casefold(), "")
        if configured_ip:
            candidates = [
                index for index, item in enumerate(results)
                if item.get("wan_ip") == configured_ip
            ]
            evidence = f"WAN IP {configured_ip}"
        else:
            key = name.casefold()
            candidates = [
                index for index, item in enumerate(results)
                if key in {
                    str(label).strip().casefold()
                    for label in [item.get("name"), *(item.get("_labels") or [])]
                    if str(label or "").strip()
                }
            ]
            evidence = "IF-MIB label"

        available = [index for index in candidates if index not in claimed]
        if len(candidates) == 1 and len(available) == 1:
            index = available[0]
            results[index]["name"] = name
            claimed.add(index)
            matched.add(name)
            continue

        reason = "ambiguous" if len(candidates) > 1 or (candidates and not available) else "unmatched"
        print(
            f'[isp-discovery] WARNING: manual ISP metadata "{name}" {reason} '
            f"by {evidence}; discovered interface kept with native identity",
            file=sys.stderr,
        )
    return matched


def finalize_discovered_results(results: list[dict], configured_names: list[str] | None,
                                configured_ips: dict[str, str] | None = None) -> list[dict[str, str]]:
    """Bind configured metadata, then assign deterministic native duplicate labels."""
    bind_manual_metadata(results, configured_names, configured_ips)
    groups: dict[str, list[dict]] = {}
    for item in results:
        groups.setdefault(item["name"].casefold(), []).append(item)
    for items in groups.values():
        if len(items) > 1:
            # WAN IP/gateway are stable across ifIndex churn. They are only used
            # to make otherwise identical native labels unique. Embedding that
            # evidence avoids a numeric rank that could shift when a line is
            # added or removed.
            native_name = items[0]["name"]
            for item in items:
                stable_evidence = item.get("wan_ip") or item.get("gateway")
                item["name"] = f"{native_name}@{stable_evidence}"
    for item in results:
        item.pop("_ifindex", None)
        item.pop("_labels", None)
    return sorted(results, key=lambda item: item["name"])


def discover_from_walks(walks: dict[str, dict[str, str]], keywords: list[str],
                        configured_names: list[str] | None = None,
                        configured_ips: dict[str, str] | None = None) -> list[dict[str, str]]:
    """Pure mapping from raw SNMP walks to [{gateway, name, wan_ip}]."""
    labels: dict[int, str] = {}
    all_labels: dict[int, list[str]] = {}
    for base in (OID_IF_ALIAS, OID_IF_NAME, OID_IF_DESCR):
        for oid, value in (walks.get(base) or {}).items():
            suffix = suffix_of(oid, base)
            if not suffix.isdigit() or not value:
                continue
            index = int(suffix)
            if value.casefold() not in {item.casefold() for item in all_labels.setdefault(index, [])}:
                all_labels[index].append(value)
            if index not in labels:
                labels[index] = value

    wan_ifindexes = {index for index, label in labels.items() if is_wan_label(label, keywords)}

    addr_ifindex: dict[str, int] = {}
    for oid, value in (walks.get(OID_IP_AD_ENT_IFINDEX) or {}).items():
        ip = suffix_of(oid, OID_IP_AD_ENT_IFINDEX)
        if looks_like_ip(ip) and value.isdigit():
            addr_ifindex[ip] = int(value)
    addr_mask: dict[str, str] = {}
    for oid, value in (walks.get(OID_IP_AD_ENT_NETMASK) or {}).items():
        ip = suffix_of(oid, OID_IP_AD_ENT_NETMASK)
        if looks_like_ip(ip) and looks_like_ip(value):
            addr_mask[ip] = value
    interface_ips = {index: ip for ip, index in addr_ifindex.items()}
    wan_ips = {index: ip for index, ip in interface_ips.items() if index in wan_ifindexes}

    # Default-route next hops: ipCidrRouteTable rows first (multi-WAN capable),
    # RFC1213 single default route as fallback.
    next_hops: list[tuple[str, int | None]] = []
    cidr_ifindex = {
        suffix_of(oid, OID_CIDR_DEFAULT_IFINDEX): int(value)
        for oid, value in (walks.get(OID_CIDR_DEFAULT_IFINDEX) or {}).items()
        if value.lstrip("-").isdigit()
    }
    for oid, value in (walks.get(OID_CIDR_DEFAULT_NEXTHOP) or {}).items():
        if looks_like_ip(value):
            next_hops.append((value, cidr_ifindex.get(suffix_of(oid, OID_CIDR_DEFAULT_NEXTHOP))))
    if not next_hops:
        legacy = walks.get(OID_ROUTE_DEFAULT_NEXTHOP) or {}
        legacy_if = walks.get(OID_ROUTE_DEFAULT_IFINDEX) or {}
        for value in legacy.values():
            if looks_like_ip(value):
                index = next((int(v) for v in legacy_if.values() if v.isdigit()), None)
                next_hops.append((value, index))

    results: list[dict[str, str]] = []
    seen_gateways: set[str] = set()
    represented_ifindexes: set[int] = set()
    for gateway, route_ifindex in next_hops:
        if gateway in seen_gateways or gateway in ("0.0.0.0",):
            continue
        # Prefer the configured name/alias filter, but do not require it.  Some
        # firewalls expose generic names (ethernet0/0) while still returning an
        # unambiguous route ifIndex and public address.
        ifindex = route_ifindex if route_ifindex in wan_ifindexes else None
        if ifindex is None and route_ifindex in interface_ips:
            if _public_wan_address(interface_ips[route_ifindex]):
                ifindex = route_ifindex
        if ifindex is None:
            # Route table gave no usable ifIndex -- find the WAN interface whose
            # subnet contains the next hop. Prefer labelled WANs, then accept a
            # public interface address when the firewall has only generic names.
            candidates = list(wan_ips.items()) + [
                (index, address) for index, address in interface_ips.items()
                if index not in wan_ips and _public_wan_address(address)
            ]
            for index, wan_ip in candidates:
                if same_subnet(gateway, wan_ip, addr_mask.get(wan_ip, "255.255.255.255")):
                    ifindex = index
                    break
        if ifindex is None:
            continue  # default route not on a WAN interface -- not an ISP line
        seen_gateways.add(gateway)
        represented_ifindexes.add(ifindex)
        results.append({
            "gateway": gateway,
            "name": labels.get(ifindex) or gateway,
            "wan_ip": interface_ips.get(ifindex, ""),
            "_ifindex": ifindex,
            "_labels": all_labels.get(ifindex, []),
            "source": "gateway",
        })

    # Hillstone and a number of other firewalls expose IP-MIB but hide both
    # standard route tables.  The old behaviour then discarded four perfectly
    # readable public WAN addresses and left four "无数据" placeholders.  Keep
    # every public WAN interface in the inventory. Static carrier subnets in
    # this installation use the conventional first usable address as gateway;
    # derive that from the SNMP netmask rather than probing the firewall's own
    # WAN address (which may reject hairpin ICMP). A later poll automatically
    # replaces this estimate with the real gateway if the route table appears.
    for ifindex, wan_ip in sorted(interface_ips.items()):
        if ifindex in represented_ifindexes or not _public_wan_address(wan_ip):
            continue
        results.append({
            "gateway": _subnet_gateway(wan_ip, addr_mask.get(wan_ip, "255.255.255.255")),
            "name": labels.get(ifindex) or wan_ip,
            "wan_ip": wan_ip,
            "_ifindex": ifindex,
            "_labels": all_labels.get(ifindex, []),
            "source": "subnet_gateway",
        })
    return finalize_discovered_results(results, configured_names, configured_ips)


def snmp_walk(ip: str, community: str, oid: str, timeout: int = 2) -> dict[str, str]:
    _collection_stats["snmp_walks"] += 1
    try:
        result = subprocess.run(
            ["snmpwalk", "-v2c", "-c", community, "-On", "-t", str(timeout), "-r", "1", ip, oid],
            capture_output=True, text=True, timeout=timeout * 4 + 5,
        )
    except Exception:
        _collection_stats["snmp_failures"] += 1
        return {}
    if result.returncode != 0:
        _collection_stats["snmp_failures"] += 1
        return {}
    _collection_stats["snmp_successes"] += 1
    if oid in (OID_IF_ALIAS, OID_IF_NAME, OID_IF_DESCR):
        _collection_stats["snmp_label_successes"] += 1
    elif oid in (OID_IP_AD_ENT_IFINDEX, OID_IP_AD_ENT_NETMASK):
        _collection_stats["snmp_address_successes"] += 1
    return parse_walk(result.stdout)


def collect(ip: str, community: str, keywords: list[str], timeout: int = 2,
            walk=snmp_walk, configured_names: list[str] | None = None,
            configured_ips: dict[str, str] | None = None) -> list[dict[str, str]]:
    walks = {}
    for oid in (
        OID_IF_ALIAS, OID_IF_NAME, OID_IF_DESCR,
        OID_IP_AD_ENT_IFINDEX, OID_IP_AD_ENT_NETMASK,
        OID_CIDR_DEFAULT_NEXTHOP, OID_CIDR_DEFAULT_IFINDEX,
    ):
        walks[oid] = walk(ip, community, oid, timeout)
    if not (walks.get(OID_CIDR_DEFAULT_NEXTHOP) or {}):
        walks[OID_ROUTE_DEFAULT_NEXTHOP] = walk(ip, community, OID_ROUTE_DEFAULT_NEXTHOP, timeout)
        walks[OID_ROUTE_DEFAULT_IFINDEX] = walk(ip, community, OID_ROUTE_DEFAULT_IFINDEX, timeout)
    return discover_from_walks(walks, keywords, configured_names, configured_ips)


def collect_route_walks(ip: str, community: str, timeout: int = 2,
                        walk=snmp_walk) -> dict[str, dict[str, str]]:
    """Read only live default-route evidence, retaining both MIB fallbacks."""
    walks = {
        OID_CIDR_DEFAULT_NEXTHOP: walk(
            ip, community, OID_CIDR_DEFAULT_NEXTHOP, timeout
        ),
        OID_CIDR_DEFAULT_IFINDEX: walk(
            ip, community, OID_CIDR_DEFAULT_IFINDEX, timeout
        ),
    }
    if not walks[OID_CIDR_DEFAULT_NEXTHOP]:
        walks[OID_ROUTE_DEFAULT_NEXTHOP] = walk(
            ip, community, OID_ROUTE_DEFAULT_NEXTHOP, timeout
        )
        walks[OID_ROUTE_DEFAULT_IFINDEX] = walk(
            ip, community, OID_ROUTE_DEFAULT_IFINDEX, timeout
        )
    return walks


def _known_down(value: object) -> bool:
    if value in (2, "2", False):
        return True
    return str(value or "").strip().lower() in {"down", "lowerlayerdown", "notpresent"}


def librenms_inventory_walks(addresses: list[dict], ports: list[dict]) -> dict[str, dict[str, str]]:
    """Adapt official API rows to the existing, well-tested WAN algorithm.

    LibreNMS ``port_id`` is used only as a join key. Every emitted IP-MIB row
    carries the port's explicit IF-MIB ``ifIndex``.
    """
    if not ports:
        raise ISPDataIncomplete("LibreNMS ports are empty")
    if not addresses:
        raise ISPDataIncomplete("LibreNMS IP inventory is empty")

    port_by_id = {
        str(port.get("port_id")): port
        for port in ports if port.get("port_id") not in (None, "")
    }
    walks = {
        OID_IF_ALIAS: {}, OID_IF_NAME: {}, OID_IF_DESCR: {},
        OID_IP_AD_ENT_IFINDEX: {}, OID_IP_AD_ENT_NETMASK: {},
    }
    usable_ports = {}
    for port_id, port in port_by_id.items():
        if _known_down(port.get("ifOperStatus")):
            continue
        try:
            ifindex = int(port.get("ifIndex"))
        except (TypeError, ValueError):
            continue
        if ifindex <= 0:
            continue
        usable_ports[port_id] = (ifindex, port)
        for field, oid in (
            ("ifAlias", OID_IF_ALIAS),
            ("ifName", OID_IF_NAME),
            ("ifDescr", OID_IF_DESCR),
        ):
            label = str(port.get(field) or "").strip()
            if label:
                walks[oid][f"{oid}.{ifindex}"] = label

    mapped_addresses = 0
    missing_public_mapping = False
    for address in addresses:
        ip = str(address.get("ipv4_address") or "").strip()
        if not looks_like_ip(ip):
            continue
        port_id = str(address.get("port_id"))
        mapping = usable_ports.get(port_id)
        if mapping is None:
            raw_port = port_by_id.get(port_id)
            if raw_port and _known_down(raw_port.get("ifOperStatus")):
                continue
            if _public_wan_address(ip):
                missing_public_mapping = True
            continue
        ifindex, _port = mapping
        try:
            prefix = int(address.get("ipv4_prefixlen"))
            mask = str(ipaddress.IPv4Network(f"0.0.0.0/{prefix}").netmask)
        except (TypeError, ValueError, ipaddress.NetmaskValueError):
            mask = "255.255.255.255"
        walks[OID_IP_AD_ENT_IFINDEX][f"{OID_IP_AD_ENT_IFINDEX}.{ip}"] = str(ifindex)
        walks[OID_IP_AD_ENT_NETMASK][f"{OID_IP_AD_ENT_NETMASK}.{ip}"] = mask
        mapped_addresses += 1
    if missing_public_mapping:
        raise ISPDataIncomplete("LibreNMS public address cannot be mapped to ifIndex")
    if not mapped_addresses:
        raise ISPDataIncomplete("LibreNMS IP inventory has no usable port mapping")
    return walks


def _prefix_gateway(wan_ip: str, prefixlen: object) -> str:
    try:
        prefix = int(prefixlen)
    except (TypeError, ValueError):
        prefix = 32
    # PPPoE/point-to-point addresses are commonly /32. Their session peer is
    # not the first address of an IP subnet, so do not ping the firewall's own
    # public address and claim that it is a carrier gateway.
    if prefix >= 31:
        return ""
    return _subnet_gateway(wan_ip, str(prefix))


def discover_from_librenms(addresses: list[dict], ports: list[dict],
                            configured_names: list[str] | None = None,
                            configured_ips: dict[str, str] | None = None) -> list[dict[str, str]]:
    """Map LibreNMS' device IP inventory to the same records as SNMP discovery.

    Hillstone can expose interface counters through SNMP while hiding both
    IP-MIB and the standard route tables. LibreNMS already has the current
    interface addresses from its device discovery, so use that inventory as a
    second source instead of retaining stale public IPs in configuration.
    """
    port_by_id = {str(item.get("port_id")): item for item in ports if item.get("port_id") is not None}
    rows = []
    seen = set()
    for address in addresses:
        wan_ip = str(address.get("ipv4_address") or "").strip()
        if wan_ip in seen or not _public_wan_address(wan_ip):
            continue
        port = port_by_id.get(str(address.get("port_id")))
        if not port or _known_down(port.get("ifOperStatus")):
            continue
        try:
            order = int(port.get("ifIndex"))
        except (TypeError, ValueError):
            continue
        if order <= 0:
            continue
        seen.add(wan_ip)
        labels = [str(port.get(field) or "").strip() for field in
                  ("ifAlias", "ifName", "ifDescr") if str(port.get(field) or "").strip()]
        label = next((str(port.get(field) or "").strip() for field in
                      ("ifAlias", "ifName", "ifDescr") if str(port.get(field) or "").strip()), wan_ip)
        gateway = _prefix_gateway(wan_ip, address.get("ipv4_prefixlen"))
        rows.append({
            "gateway": gateway,
            "name": label,
            "wan_ip": wan_ip,
            "_ifindex": order,
            "_labels": labels,
            "source": "librenms_subnet_gateway" if gateway else "librenms_interface_only",
        })

    return finalize_discovered_results(rows, configured_names, configured_ips)


def fetch_librenms_inventory(client: LibreNMSClient, target: str) -> tuple[dict, list[dict], list[dict]]:
    """Fetch and validate one firewall inventory without global fallback."""
    metadata = client.resolve_device(target)
    freshness = inventory_freshness(metadata)
    if freshness == "stale":
        raise ISPDataIncomplete("LibreNMS device poll is stale")
    ports = client.get_device_ports(
        metadata,
        columns="port_id,ifIndex,ifName,ifDescr,ifAlias,ifOperStatus",
    )
    addresses = client.get_device_ip_addresses(metadata)
    # Validate the join now so missing port mappings are handled per device.
    librenms_inventory_walks(addresses, ports)
    return metadata, addresses, ports


def collect_hybrid(ip: str, community: str, keywords: list[str], timeout: int,
                   addresses: list[dict], ports: list[dict],
                   configured_names: list[str] | None = None,
                   configured_ips: dict[str, str] | None = None,
                   walk=snmp_walk) -> list[dict[str, str]]:
    """Use API inventory while retaining live standard-MIB route evidence."""
    walks = librenms_inventory_walks(addresses, ports)
    walks.update(collect_route_walks(ip, community, timeout, walk=walk))
    results = discover_from_walks(walks, keywords, configured_names, configured_ips)
    for item in results:
        if item.get("source") == "subnet_gateway":
            item["source"] = "librenms_subnet_gateway"
    return results


def collect_from_librenms(firewall_targets: list[str], configured_names: list[str] | None = None,
                           configured_ips: dict[str, str] | None = None,
                           client: LibreNMSClient | None = None) -> list[dict[str, str]]:
    """Read the current WAN address inventory from LibreNMS' official API."""
    client = client or LibreNMSClient()
    if not client.base_url or not client.token:
        return []
    try:
        # Load once per discovery cycle. resolve_device() reuses this cache for
        # every configured firewall instead of repeating /api/v0/devices.
        client.list_devices()
    except LibreNMSError as exc:
        print(
            f"[isp-discovery] LibreNMS device lookup failed: {type(exc).__name__}",
            file=sys.stderr,
        )
        return []

    for target in firewall_targets:
        try:
            _device, addresses, ports = fetch_librenms_inventory(client, target)
            results = discover_from_librenms(
                addresses, ports, configured_names, configured_ips
            )
        except (LibreNMSError, ISPDataIncomplete) as exc:
            print(
                f"[isp-discovery] LibreNMS WAN inventory failed for {target}: "
                f"{type(exc).__name__}",
                file=sys.stderr,
            )
            continue
        if results:
            return results
    return []


def merge_device_results(collections: list[list[dict[str, str]]]) -> list[dict[str, str]]:
    """Combine multi-firewall evidence without duplicating one carrier target."""
    merged = []
    seen = set()
    for results in collections:
        for item in results:
            key = item.get("gateway") or f"interface:{item.get('wan_ip')}:{item.get('name')}"
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return merged


def build_file_sd(results: list[dict[str, str]], exclude: set[str]) -> list[dict]:
    payload = []
    for item in results:
        if not item.get("gateway"):
            continue  # PPPoE /31-/32: interface is monitored, but no fake gateway ping
        if item["gateway"] in exclude:
            continue  # already a manual ISP_PING target -- manual naming wins
        labels = {"display_name": item["name"]}
        if item.get("wan_ip"):
            labels["wan_ip"] = item["wan_ip"]
        if item.get("source"):
            labels["discovery_source"] = item["source"]
        payload.append({"targets": [item["gateway"]], "labels": labels})
    return payload


def discovery_state_path(target_path: str) -> str:
    configured = os.environ.get("ISP_DISCOVERY_STATE_FILE", "").strip()
    if configured:
        return configured
    return str(Path(target_path).with_name("isp-discovery-state.json"))


def load_valid_file_sd(path: str) -> list[dict] | None:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, list):
        return None
    for entry in payload:
        labels = entry.get("labels") if isinstance(entry, dict) else None
        targets = entry.get("targets") if isinstance(entry, dict) else None
        if (
            not isinstance(labels, dict)
            or not str(labels.get("display_name") or "").strip()
            or not isinstance(targets, list)
            or len(targets) != 1
            or not looks_like_ip(str(targets[0]))
        ):
            return None
    return payload


def load_discovery_state(path: str) -> dict:
    try:
        state = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return state if isinstance(state, dict) else {}


def write_discovery_state(path: str, status: str, count: int, *,
                          last_success_at: int | None = None,
                          last_error_at: int | None = None) -> None:
    write_json_atomic(path, {
        "status": status,
        "last_success_at": last_success_at,
        "last_error_at": last_error_at,
        "count": count,
    })


def mark_collection_failure(out: str, state_path: str) -> None:
    now = int(time.time())
    previous = load_valid_file_sd(out)
    state = load_discovery_state(state_path)
    if previous is not None:
        last_success = state.get("last_success_at")
        if not isinstance(last_success, (int, float)):
            try:
                last_success = int(Path(out).stat().st_mtime)
            except OSError:
                last_success = None
        write_discovery_state(
            state_path, "stale", len(previous),
            last_success_at=int(last_success) if last_success is not None else None,
            last_error_at=now,
        )
        print(
            f"[isp-discovery] WARNING: collection failed; preserving "
            f"last-known-good inventory ({len(previous)} target(s))",
            file=sys.stderr,
        )
    else:
        write_discovery_state(
            state_path, "error", 0, last_success_at=None, last_error_at=now
        )
        print(
            "[isp-discovery] WARNING: collection failed and no valid "
            "last-known-good inventory exists",
            file=sys.stderr,
        )


def main() -> None:
    reset_collection_stats()
    out = os.environ.get("ISP_TARGETS_FILE", "/targets/isp_targets.json")
    state_path = discovery_state_path(out)
    enabled = os.environ.get("ISP_GATEWAY_AUTO_DISCOVER", "true").lower() in ("1", "true", "yes", "on")
    mode = isp_discovery_source()
    firewall_targets = target_ips(os.environ.get("FIREWALL_SNMP_TARGETS", ""))
    ha_mode = bool(os.environ.get("FIREWALL_UNIT_SNMP_TARGETS", "").strip())
    ha_vip_hybrid = ha_mode and mode == "hybrid"
    community = (
        os.environ.get("FIREWALL_SNMP_COMMUNITY", "").strip()
        or os.environ.get("SNMP_COMMUNITY", "global")
    )
    keywords = wan_keywords(os.environ.get("FIREWALL_WAN_IF_FILTER", "telecom,telcom,unicom,isp,WAN"))
    configured_names = [
        item.strip() for item in os.environ.get("BIGSCREEN_ISP_NAMES", "").split(",")
        if item.strip()
    ]
    configured_ips = named_ipv4_targets(os.environ.get("BIGSCREEN_ISP_IPS", ""))
    timeout = int(os.environ.get("ISP_DISCOVERY_SNMP_TIMEOUT", "2") or "2")
    manual = set(target_ips(os.environ.get("ISP_PING", "")))

    # Manual targets are the operator's final authority. They are rendered by
    # the ordinary ISP_PING target path, so this auto-discovery file must stay
    # empty and no device/API query is useful.
    if manual:
        write_file_sd(out, [])
        write_discovery_state(
            state_path, "disabled", 0, last_success_at=None, last_error_at=None
        )
        print(
            "[isp-discovery] manual ISP_PING configured; skipped automatic discovery",
            file=sys.stderr,
        )
        print(
            "[isp-discovery] collection stats: api_requests=0 snmp_walks=0 snmp_gets=0",
            file=sys.stderr,
        )
        return

    if not enabled or not firewall_targets:
        write_file_sd(out, [])
        write_discovery_state(
            state_path, "disabled", 0, last_success_at=None, last_error_at=None
        )
        reason = "disabled" if not enabled else "no FIREWALL_SNMP_TARGETS"
        print(f"[isp-discovery] {reason}; wrote empty target file", file=sys.stderr)
        return

    client = None
    librenms_ready = False
    if mode != "direct-snmp" and not ha_vip_hybrid:
        client = LibreNMSClient()
        try:
            client.list_devices()
            librenms_ready = True
        except LibreNMSError as exc:
            print(
                f"[isp-discovery] LibreNMS device inventory unavailable: "
                f"{type(exc).__name__}",
                file=sys.stderr,
            )

    per_device_results = []
    successful_sources = 0
    if mode == "direct-snmp":
        # Preserve the previous path exactly: the first firewall exposing ISP
        # data is authoritative in direct-only compatibility mode.
        for ip in firewall_targets:
            stats_before = collection_stats()
            current = collect(
                ip, community, keywords, timeout,
                configured_names=configured_names,
                configured_ips=configured_ips,
            )
            if direct_collection_succeeded(stats_before, current):
                successful_sources += 1
            print(
                f"[isp-discovery] source=direct-snmp device={ip} "
                "inventory=direct-snmp gateway=direct-snmp",
                file=sys.stderr,
            )
            if current:
                per_device_results.append(current)
                break
    elif ha_vip_hybrid:
        # HA physical nodes are the full LibreNMS devices, while these targets
        # are logical business VIPs. Querying LibreNMS for a VIP inventory is
        # both inapplicable and noisy, so hybrid deliberately uses the VIP's
        # live SNMP inventory and routing data without making an API request.
        for ip in firewall_targets:
            stats_before = collection_stats()
            current = collect(
                ip, community, keywords, timeout,
                configured_names=configured_names,
                configured_ips=configured_ips,
            )
            if direct_collection_succeeded(stats_before, current):
                successful_sources += 1
            print(
                f"[isp-discovery] source=hybrid device={ip} mode=ha-vip "
                "inventory=direct-snmp gateway=direct-snmp",
                file=sys.stderr,
            )
            if current:
                per_device_results.append(current)
    else:
        for ip in firewall_targets:
            current = []
            inventory_succeeded = False
            inventory_source = "unavailable"
            gateway_source = "none"
            if librenms_ready:
                try:
                    _metadata, addresses, ports = fetch_librenms_inventory(client, ip)
                    inventory_succeeded = True
                    successful_sources += 1
                    inventory_source = "librenms"
                    if mode == "hybrid":
                        current = collect_hybrid(
                            ip, community, keywords, timeout, addresses, ports,
                            configured_names=configured_names,
                            configured_ips=configured_ips,
                        )
                        gateway_source = (
                            "direct-snmp"
                            if any(item.get("source") == "gateway" for item in current)
                            else "librenms-subnet" if current else "none"
                        )
                    else:
                        current = discover_from_librenms(
                            addresses, ports, configured_names, configured_ips
                        )
                        gateway_source = "librenms-subnet" if current else "none"
                except (LibreNMSError, ISPDataIncomplete) as exc:
                    print(
                        f"[isp-discovery] LibreNMS WAN inventory failed for {ip}: "
                        f"{type(exc).__name__}",
                        file=sys.stderr,
                    )
            if mode == "hybrid" and not current:
                stats_before = collection_stats()
                current = collect(
                    ip, community, keywords, timeout,
                    configured_names=configured_names,
                    configured_ips=configured_ips,
                )
                if (
                    not inventory_succeeded
                    and direct_collection_succeeded(stats_before, current)
                ):
                    successful_sources += 1
                inventory_source = "direct-snmp"
                gateway_source = "direct-snmp"
            elif mode == "librenms" and not current:
                print(
                    f"[isp-discovery] {ip}: LibreNMS-only inventory is "
                    "insufficient; skipping automatic ISP targets",
                    file=sys.stderr,
                )
            print(
                f"[isp-discovery] source={mode} device={ip} "
                f"inventory={inventory_source} gateway={gateway_source}",
                file=sys.stderr,
            )
            if current:
                per_device_results.append(current)

    results = merge_device_results(per_device_results)
    payload = build_file_sd(results, manual)
    if not results and successful_sources == 0:
        mark_collection_failure(out, state_path)
        stats = collection_stats()
        api_requests = getattr(client, "request_count", 0) if client is not None else 0
        print(
            f"[isp-discovery] collection stats: api_requests={api_requests} "
            f"snmp_walks={stats['snmp_walks']} snmp_gets={stats['snmp_gets']}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    write_file_sd(out, payload)
    write_discovery_state(
        state_path, "ok", len(payload),
        last_success_at=int(time.time()), last_error_at=None,
    )
    if results:
        summary = ", ".join(
            f"{item['name']}={item['gateway']}"
            if item.get("gateway") else f"{item['name']}={item['wan_ip']} (interface only)"
            for item in results
        )
        interface_only = sum(1 for item in results if not item.get("gateway"))
        print(f"[isp-discovery] found {len(results)} ISP interface(s): {summary}"
              f" ({len(results) - len(payload) - interface_only} already manual, "
              f"{interface_only} without a safe gateway target)", file=sys.stderr)
    else:
        print(
            "[isp-discovery] discovery succeeded, 0 matching ISP interfaces",
            file=sys.stderr,
        )
    stats = collection_stats()
    api_requests = getattr(client, "request_count", 0) if client is not None else 0
    print(
        f"[isp-discovery] collection stats: api_requests={api_requests} "
        f"snmp_walks={stats['snmp_walks']} snmp_gets={stats['snmp_gets']}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
