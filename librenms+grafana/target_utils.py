#!/usr/bin/env python3
"""Shared IPv4 target parsing and atomic JSON/file_sd helpers."""
from __future__ import annotations

import json
import os
import re
from ipaddress import IPv4Address, IPv4Network
from typing import Any


def is_ipv4(value: Any) -> bool:
    try:
        IPv4Address(str(value or "").strip())
        return True
    except ValueError:
        return False


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


def parse_named_ipv4_targets(raw: str) -> dict[str, str]:
    """Return IP -> display name for comma-separated NAME:IP target syntax."""
    targets: dict[str, str] = {}
    for entry in re.split(r"[,\n]+", raw or ""):
        entry = entry.strip()
        if not entry:
            continue
        name = entry.split(":", 1)[0].strip() if ":" in entry else ""
        ips = expand_ipv4_entry(entry)
        for index, ip in enumerate(ips, start=1):
            if not name or name == ip:
                display_name = ip
            elif len(ips) == 1:
                display_name = name
            else:
                display_name = f"{name}{index}"
            targets[ip] = display_name
    return targets


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
