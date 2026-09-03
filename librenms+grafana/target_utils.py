#!/usr/bin/env python3
"""Shared target parsing, SNMP value normalization, and atomic JSON helpers."""
from __future__ import annotations

import json
import os
import re
import sys
from ipaddress import IPv4Address, IPv4Network
from typing import Any


def is_ipv4(value: Any) -> bool:
    try:
        IPv4Address(str(value or "").strip())
        return True
    except ValueError:
        return False


def normalize_mac(value: Any) -> str | None:
    """Normalize common SNMP/CLI MAC encodings to six lower-case octets."""
    if value is None:
        return None
    text = str(value).strip().strip('"')
    if ":" in text:
        prefix, _, remainder = text.partition(":")
        if prefix.strip().lower() in ("hex-string", "string"):
            text = remainder.strip()
    tokens = re.findall(r"(?i)(?<![0-9a-f])[0-9a-f]{1,2}(?![0-9a-f])", text)
    if len(tokens) == 6:
        return ":".join(token.lower().zfill(2) for token in tokens)
    compact = re.sub(r"[^0-9a-fA-F]", "", text)
    if len(compact) != 12:
        return None
    return ":".join(compact[index:index + 2].lower() for index in range(0, 12, 2))


def parse_if_oper_status(output: str) -> dict[int, int]:
    """Parse IF-MIB ifOperStatus numeric or named values by ifIndex."""
    named = {
        "up": 1, "down": 2, "testing": 3, "unknown": 4,
        "dormant": 5, "notpresent": 6, "lowerlayerdown": 7,
    }
    statuses: dict[int, int] = {}
    for line in str(output or "").splitlines():
        if "=" not in line:
            continue
        oid, value = line.split("=", 1)
        try:
            ifindex = int(oid.strip().strip(".").split(".")[-1])
        except (ValueError, IndexError):
            continue
        text = value.rsplit(":", 1)[-1].strip()
        number = re.search(r"\d+", text)
        if number:
            statuses[ifindex] = int(number.group(0))
            continue
        name = text.lower().split("(", 1)[0].strip()
        if name in named:
            statuses[ifindex] = named[name]
    return statuses


def expand_ipv4_entry(item: str, max_hosts: int = 4096) -> list[str]:
    """Expand NAME:IP, an IP/range, or CIDR into IPv4 host addresses."""
    item = (item or "").strip()
    if not item:
        return []
    if ":" in item:
        item = item.split(":", 1)[1].strip()
    if not item:
        return []
    if "/" in item:
        try:
            hosts = [str(ip) for ip in IPv4Network(item, strict=False).hosts()]
        except ValueError:
            return []
        return hosts if len(hosts) <= max_hosts else []
    if "-" not in item:
        try:
            return [str(IPv4Address(item))]
        except ValueError:
            return []

    start_raw, end_raw = [part.strip() for part in item.split("-", 1)]
    try:
        start = IPv4Address(start_raw)
        end = IPv4Address(
            f"{start_raw.rsplit('.', 1)[0]}.{end_raw}"
            if re.fullmatch(r"\d{1,3}", end_raw) else end_raw
        )
    except ValueError:
        return []
    size = int(end) - int(start) + 1
    if size < 1 or size > max_hosts:
        return []
    return [str(IPv4Address(int(start) + offset)) for offset in range(size)]


def expand_ipv4_targets(raw: str, max_hosts: int = 4096) -> list[str]:
    seen: set[str] = set()
    targets: list[str] = []
    for part in re.split(r"[,\n]+", raw or ""):
        for ip in expand_ipv4_entry(part, max_hosts=max_hosts):
            if ip not in seen:
                seen.add(ip)
                targets.append(ip)
    return targets


def parse_named_ipv4_target_rows(raw: str) -> list[tuple[str, str]]:
    """Return ``(name, IP)`` rows for bare-IP and ``NAME:IP`` targets."""
    rows: list[tuple[str, str]] = []
    for entry in re.split(r"[,\n]+", raw or ""):
        entry = entry.strip()
        if not entry:
            continue
        name = entry.split(":", 1)[0].strip() if ":" in entry else ""
        ips = expand_ipv4_entry(entry)
        for index, ip in enumerate(ips, start=1):
            display_name = name
            if name and len(ips) > 1:
                display_name = f"{name}{index}"
            rows.append((display_name, ip))
    return rows


def parse_named_ipv4_targets(raw: str) -> dict[str, str]:
    """Return IP -> display name for comma-separated NAME:IP target syntax."""
    targets: dict[str, str] = {}
    for name, ip in parse_named_ipv4_target_rows(raw):
        targets[ip] = name if name and name != ip else ip
    return targets


def _main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "named-targets":
        for name, ip in parse_named_ipv4_target_rows(argv[2]):
            print(f"{name}|{ip}")
        return 0
    print("usage: target_utils.py named-targets TARGETS", file=sys.stderr)
    return 2


def merge_display_names(base: dict[str, str], incoming: dict[str, str]) -> dict[str, str]:
    """Merge targets without allowing an IP placeholder to erase a real name."""
    merged = dict(base)
    for ip, name in incoming.items():
        current = str(merged.get(ip) or "").strip()
        candidate = str(name or ip).strip()
        if current and not is_ipv4(current) and is_ipv4(candidate):
            continue
        merged[ip] = candidate
    return merged


def load_file_sd_targets(path: str) -> dict[str, str]:
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return {}
    targets: dict[str, str] = {}
    if not isinstance(payload, list):
        return targets
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        labels = entry.get("labels") if isinstance(entry.get("labels"), dict) else {}
        name = str(labels.get("display_name") or "").strip()
        for value in entry.get("targets") or []:
            try:
                ip = str(IPv4Address(str(value).strip()))
            except ValueError:
                continue
            targets[ip] = name or ip
    return targets


def build_file_sd(results: dict[str, str]) -> list[dict[str, Any]]:
    return [
        {"targets": [ip], "labels": {"display_name": name}}
        for ip, name in sorted(results.items(), key=lambda item: int(IPv4Address(item[0])))
    ]


def write_json_atomic(path: str, payload: Any, *, sort_keys: bool = False) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=sort_keys)
    os.replace(temporary, path)


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
