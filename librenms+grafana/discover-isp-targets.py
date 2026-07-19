#!/usr/bin/env python3
"""Discover ISP ping targets (carrier gateways) from the firewall via SNMP.

Instead of operators typing carrier gateway IPs by hand, ask the firewall: it
already knows its WAN interfaces (matched by FIREWALL_WAN_IF_FILTER keywords),
each WAN address (IP-MIB ipAddrTable) and the carrier gateways (default-route
next hops in IP-FORWARD-MIB ipCidrRouteTable, falling back to the RFC1213
ipRouteTable). Discovered gateways are written to a Prometheus file_sd that the
infra-isp-ping job reads alongside the manually configured ISP_PING targets;
each is named after its WAN interface (ifAlias/ifName), so alert cards and the
big screen show the carrier name.

Manual entries win: any discovered gateway whose IP is already listed in
ISP_PING is skipped, so hand-tuned names/targets are never duplicated. Not
every firewall exposes its routing table over SNMP, and a standby line whose
default route is inactive has no next hop to read -- keep manual entries for
those; discovery only ever adds targets.

Env vars:
  ISP_GATEWAY_AUTO_DISCOVER   true = enabled (default true)
  FIREWALL_SNMP_TARGETS       firewall SNMP address(es), NAME:IP comma list
  FIREWALL_SNMP_COMMUNITY     community (falls back to SNMP_COMMUNITY)
  FIREWALL_WAN_IF_FILTER      WAN interface keywords (same as the bridge)
  ISP_PING                    manual targets, their IPs are excluded here
  ISP_TARGETS_FILE            output path (default /targets/isp_targets.json)
  ISP_DISCOVERY_SNMP_TIMEOUT  per-walk SNMP timeout seconds (default 2)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

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


def looks_like_ip(value: str) -> bool:
    return bool(re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", str(value or "")))


def _ip_int(ip: str) -> int:
    return sum(int(part) << (8 * (3 - idx)) for idx, part in enumerate(ip.split(".")))


def same_subnet(ip_a: str, ip_b: str, mask: str) -> bool:
    try:
        m = _ip_int(mask)
        return (_ip_int(ip_a) & m) == (_ip_int(ip_b) & m)
    except (ValueError, IndexError):
        return False


def target_ips(raw: str) -> list[str]:
    out = []
    for part in re.split(r"[,\n]+", raw or ""):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            part = part.split(":", 1)[1].strip()
        if looks_like_ip(part):
            out.append(part)
    return out


def discover_from_walks(walks: dict[str, dict[str, str]], keywords: list[str]) -> list[dict[str, str]]:
    """Pure mapping from raw SNMP walks to [{gateway, name, wan_ip}]."""
    labels: dict[int, str] = {}
    for base in (OID_IF_ALIAS, OID_IF_NAME, OID_IF_DESCR):
        for oid, value in (walks.get(base) or {}).items():
            suffix = suffix_of(oid, base)
            if suffix.isdigit() and value and int(suffix) not in labels:
                labels[int(suffix)] = value

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
    wan_ips = {index: ip for ip, index in addr_ifindex.items() if index in wan_ifindexes}

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
    for gateway, route_ifindex in next_hops:
        if gateway in seen_gateways or gateway in ("0.0.0.0",):
            continue
        ifindex = route_ifindex if route_ifindex in wan_ifindexes else None
        if ifindex is None:
            # Route table gave no usable ifIndex -- find the WAN interface whose
            # subnet contains the next hop.
            for index, wan_ip in wan_ips.items():
                if same_subnet(gateway, wan_ip, addr_mask.get(wan_ip, "255.255.255.255")):
                    ifindex = index
                    break
        if ifindex is None:
            continue  # default route not on a WAN interface -- not an ISP line
        seen_gateways.add(gateway)
        results.append({
            "gateway": gateway,
            "name": labels.get(ifindex) or gateway,
            "wan_ip": wan_ips.get(ifindex, ""),
            "_ifindex": ifindex,
        })

    # Two lines from one carrier can share an interface label; number them by
    # ifIndex order (电信-1/电信-2) -- the same rule the bandwidth watcher uses,
    # so ping cards and bandwidth cards for one line carry one name.
    groups: dict[str, list[dict]] = {}
    for item in results:
        groups.setdefault(item["name"], []).append(item)
    for name, items in groups.items():
        if len(items) > 1:
            for position, item in enumerate(sorted(items, key=lambda x: x["_ifindex"]), start=1):
                item["name"] = f"{name}-{position}"
    for item in results:
        item.pop("_ifindex", None)
    return sorted(results, key=lambda item: item["name"])


def snmp_walk(ip: str, community: str, oid: str, timeout: int = 2) -> dict[str, str]:
    try:
        result = subprocess.run(
            ["snmpwalk", "-v2c", "-c", community, "-On", "-t", str(timeout), "-r", "1", ip, oid],
            capture_output=True, text=True, timeout=timeout * 4 + 5,
        )
    except Exception:
        return {}
    if result.returncode != 0:
        return {}
    return parse_walk(result.stdout)


def collect(ip: str, community: str, keywords: list[str], timeout: int = 2,
            walk=snmp_walk) -> list[dict[str, str]]:
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
    return discover_from_walks(walks, keywords)


def build_file_sd(results: list[dict[str, str]], exclude: set[str]) -> list[dict]:
    payload = []
    for item in results:
        if item["gateway"] in exclude:
            continue  # already a manual ISP_PING target -- manual naming wins
        labels = {"display_name": item["name"]}
        if item.get("wan_ip"):
            labels["wan_ip"] = item["wan_ip"]
        payload.append({"targets": [item["gateway"]], "labels": labels})
    return payload


def write_file_sd(path: str, payload: list[dict]) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def main() -> None:
    out = os.environ.get("ISP_TARGETS_FILE", "/targets/isp_targets.json")
    enabled = os.environ.get("ISP_GATEWAY_AUTO_DISCOVER", "true").lower() in ("1", "true", "yes", "on")
    firewall_targets = target_ips(os.environ.get("FIREWALL_SNMP_TARGETS", ""))
    community = (
        os.environ.get("FIREWALL_SNMP_COMMUNITY", "").strip()
        or os.environ.get("SNMP_COMMUNITY", "global")
    )
    keywords = wan_keywords(os.environ.get("FIREWALL_WAN_IF_FILTER", "telecom,telcom,unicom,isp,WAN"))
    timeout = int(os.environ.get("ISP_DISCOVERY_SNMP_TIMEOUT", "2") or "2")
    manual = set(target_ips(os.environ.get("ISP_PING", "")))

    if not enabled or not firewall_targets:
        write_file_sd(out, [])
        reason = "disabled" if not enabled else "no FIREWALL_SNMP_TARGETS"
        print(f"[isp-discovery] {reason}; wrote empty target file", file=sys.stderr)
        return

    results: list[dict[str, str]] = []
    for ip in firewall_targets:
        results = collect(ip, community, keywords, timeout)
        if results:
            break
    payload = build_file_sd(results, manual)
    write_file_sd(out, payload)
    if results:
        summary = ", ".join(f"{item['name']}={item['gateway']}" for item in results)
        print(f"[isp-discovery] found {len(results)} ISP gateway(s): {summary}"
              f" ({len(results) - len(payload)} already manual)", file=sys.stderr)
    else:
        print("[isp-discovery] no default-route next hops readable from firewall SNMP; "
              "keep manual ISP_PING entries", file=sys.stderr)


if __name__ == "__main__":
    main()
