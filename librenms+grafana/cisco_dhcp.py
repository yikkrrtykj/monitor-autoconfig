"""Pure parsers for the read-only Cisco IOS/IOS-XE DHCP dashboard.

Keeping device-output parsing separate from Telnet and HTTP orchestration makes
it possible to test real, sanitized switch fixtures without starting services
or connecting to network equipment.
"""

from __future__ import annotations

import ipaddress
import re


def _number(block: str, label: str) -> int:
    match = re.search(rf"(?im)^\s*{re.escape(label)}\s*:\s*(\d+)\s*$", block)
    return int(match.group(1)) if match else 0


def parse_cisco_dhcp_pools(text: str) -> list[dict]:
    """Parse stable fields from Cisco IOS/IOS-XE ``show ip dhcp pool``."""
    source = str(text or "").replace("\r", "")
    starts = list(re.finditer(r"(?im)^\s*Pool\s+(.+?)\s*:\s*$", source))
    pools: list[dict] = []
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(source)
        block = source[match.end():end]
        total = _number(block, "Total addresses")
        leased = _number(block, "Leased addresses")
        excluded = _number(block, "Excluded addresses")
        usable = max(0, total - excluded)
        available = max(0, usable - leased)
        address_range = ""
        range_match = re.search(
            r"(?m)(\d{1,3}(?:\.\d{1,3}){3})\s*-\s*(\d{1,3}(?:\.\d{1,3}){3})",
            block,
        )
        if range_match:
            address_range = f"{range_match.group(1)} - {range_match.group(2)}"
        utilization = round((leased / usable * 100) if usable else 0, 1)
        pools.append({
            "name": match.group(1).strip(),
            "range": address_range,
            "total": total,
            "leased": leased,
            "excluded": excluded,
            "available": available,
            "utilization": utilization,
            "level": "bad" if utilization >= 90 else "warn" if utilization >= 80 else "good",
        })
    return pools


def parse_cisco_dhcp_conflicts(text: str) -> list[str]:
    source = str(text or "").replace("\r", "")
    addresses = re.findall(r"(?m)^\s*(\d{1,3}(?:\.\d{1,3}){3})\s+", source)
    return list(dict.fromkeys(addresses))


def parse_cisco_dhcp_excluded(text: str) -> list[str]:
    """Expand IOS exclusions while refusing pathological ranges."""
    addresses: set[ipaddress.IPv4Address] = set()
    for match in re.finditer(
        r"(?im)^\s*ip\s+dhcp\s+excluded-address\s+"
        r"(\d{1,3}(?:\.\d{1,3}){3})(?:\s+(\d{1,3}(?:\.\d{1,3}){3}))?\s*$",
        str(text or "").replace("\r", ""),
    ):
        try:
            start = ipaddress.IPv4Address(match.group(1))
            end = ipaddress.IPv4Address(match.group(2) or match.group(1))
        except ipaddress.AddressValueError:
            continue
        if end < start:
            start, end = end, start
        if int(end) - int(start) > 65535:
            continue
        addresses.update(ipaddress.IPv4Address(value) for value in range(int(start), int(end) + 1))
    return [str(value) for value in sorted(addresses)]


def attach_dhcp_pool_exclusions(pools: list[dict], excluded_addresses: list[str]) -> None:
    """Attach exact exclusions that fall inside every returned pool range."""
    parsed = []
    for value in excluded_addresses:
        try:
            parsed.append(ipaddress.IPv4Address(value))
        except ipaddress.AddressValueError:
            continue
    for pool in pools:
        bounds = re.match(
            r"^\s*(\d{1,3}(?:\.\d{1,3}){3})\s*-\s*(\d{1,3}(?:\.\d{1,3}){3})\s*$",
            str(pool.get("range") or ""),
        )
        if not bounds:
            pool["excludedAddresses"] = []
            continue
        try:
            start = ipaddress.IPv4Address(bounds.group(1))
            end = ipaddress.IPv4Address(bounds.group(2))
        except ipaddress.AddressValueError:
            pool["excludedAddresses"] = []
            continue
        pool["excludedAddresses"] = [str(value) for value in parsed if start <= value <= end]


def parse_cisco_dhcp_statistics(text: str) -> dict:
    source = str(text or "").replace("\r", "")

    def value(label: str) -> int:
        match = re.search(rf"(?im)^\s*{re.escape(label)}\s+(\d+)\s*$", source)
        return int(match.group(1)) if match else 0

    return {
        "automaticBindings": value("Automatic bindings"),
        "manualBindings": value("Manual bindings"),
        "expiredBindings": value("Expired bindings"),
        "malformedMessages": value("Malformed messages"),
    }


def parse_cisco_dhcp_bindings(text: str) -> list[dict]:
    """Parse active addresses from IOS/IOS-XE ``show ip dhcp binding``."""
    bindings = []
    seen = set()
    for line in str(text or "").replace("\r", "").splitlines():
        match = re.match(r"^\s*(\d{1,3}(?:\.\d{1,3}){3})(?:\s+(.+?))?\s*$", line)
        if not match:
            continue
        try:
            address = str(ipaddress.IPv4Address(match.group(1)))
        except ipaddress.AddressValueError:
            continue
        if address in seen:
            continue
        seen.add(address)
        bindings.append({
            "ip": address,
            "detail": re.sub(r"\s+", " ", match.group(2) or "").strip()[:512],
        })
        if len(bindings) >= 65536:
            break
    return bindings


def parse_cisco_arp_entries(text: str) -> list[dict]:
    """Parse complete IPv4 neighbours from Cisco ``show ip arp`` output."""
    entries = []
    seen = set()
    for line in str(text or "").replace("\r", "").splitlines():
        if re.search(r"(?i)\bincomplete\b", line):
            continue
        match = re.match(
            r"^\s*(?:Internet|IP)\s+"
            r"(\d{1,3}(?:\.\d{1,3}){3})\s+"
            r"(\S+)\s+([0-9A-Fa-f.:-]{11,})\s+\S+(?:\s+(.+?))?\s*$",
            line,
        )
        if not match:
            continue
        try:
            address = str(ipaddress.IPv4Address(match.group(1)))
        except ipaddress.AddressValueError:
            continue
        if address in seen:
            continue
        seen.add(address)
        interface = re.sub(r"\s+", " ", match.group(4) or "").strip()
        detail = f"ARP {match.group(3)}"
        if interface:
            detail += f" · {interface}"
        entries.append({"ip": address, "detail": detail})
        if len(entries) >= 65536:
            break
    return entries
