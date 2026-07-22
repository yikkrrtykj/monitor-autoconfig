#!/usr/bin/env python3
"""Discover real Cisco StackWise stacks and write a Prometheus file_sd file.

The full StackWise SNMP module walks several CISCO-STACKWISE-MIB tables.  Some
standalone/older Cisco switches do not implement those tables cleanly and make
the exporter wait for every retry.  Running that module against every switch
every 30 seconds can therefore compete with LibreNMS polling and delay ICMP.

This detector walks only the ``cswSwitchNumCurrent`` member-number column during
the 10-minute topology discovery loop, with no retry and a one-second timeout.
Only devices reporting more than one member are assigned to the high-frequency
StackWise scrape job.  A previously confirmed stack is retained when it is
degraded or temporarily unreachable so that member-loss monitoring does not
disappear during the fault.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from ipaddress import IPv4Address, IPv4Network

STACK_MEMBER_NUMBER_OID = "1.3.6.1.4.1.9.9.500.1.2.1.1.1"
DEFAULT_OUTPUT = "/targets/stackwise_targets.json"


def expand_range(item: str) -> list[str]:
    """Expand an IP, last-octet/full range, or CIDR after an optional name."""
    item = (item or "").strip()
    if not item:
        return []
    if ":" in item:
        item = item.split(":", 1)[1].strip()
    if "/" in item:
        try:
            return [str(ip) for ip in IPv4Network(item, strict=False).hosts()]
        except ValueError:
            return []
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
    if int(end) < int(start) or int(end) - int(start) > 4096:
        return []
    return [str(IPv4Address(value)) for value in range(int(start), int(end) + 1)]


def configured_targets(raw: str) -> dict[str, str]:
    """Return IP -> display name for the project's NAME:IP target syntax."""
    targets: dict[str, str] = {}
    for entry in re.split(r"[,\n]+", raw or ""):
        entry = entry.strip()
        if not entry:
            continue
        name = entry.split(":", 1)[0].strip() if ":" in entry else ""
        ips = expand_range(entry)
        for index, ip in enumerate(ips, start=1):
            if not name or name == ip:
                display_name = ip
            elif len(ips) == 1:
                display_name = name
            else:
                display_name = f"{name}{index}"
            targets[ip] = display_name
    return targets


def load_file_sd(path: str) -> dict[str, str]:
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


def stack_member_count(ip: str, community: str, timeout: int = 1) -> int | None:
    """Read the current StackWise member count, or None on no answer/support."""
    try:
        result = subprocess.run(
            [
                "snmpwalk", "-v2c", "-c", community, "-Ovq",
                "-t", str(timeout), "-r", "0", ip, STACK_MEMBER_NUMBER_OID,
            ],
            capture_output=True,
            text=True,
            timeout=timeout + 2,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    members: set[int] = set()
    for line in result.stdout.splitlines():
        match = re.search(r"(?:^|\s)(\d+)\s*$", line.strip())
        if match and int(match.group(1)) > 0:
            members.add(int(match.group(1)))
    return len(members) if members else None


def select_stacks(
    candidates: dict[str, str],
    previous: dict[str, str],
    counts: dict[str, int | None],
) -> tuple[dict[str, str], list[str], list[str]]:
    """Select confirmed stacks and retain already-confirmed degraded stacks."""
    selected: dict[str, str] = {}
    confirmed: list[str] = []
    retained: list[str] = []
    for ip, name in candidates.items():
        if (counts.get(ip) or 0) > 1:
            selected[ip] = name
            confirmed.append(ip)
        elif ip in previous and previous[ip] == name:
            selected[ip] = name
            retained.append(ip)
    return selected, confirmed, retained


def build_file_sd(results: dict[str, str]) -> list[dict]:
    return [
        {"targets": [ip], "labels": {"display_name": name}}
        for ip, name in sorted(results.items(), key=lambda item: int(IPv4Address(item[0])))
    ]


def write_file_sd(path: str, payload: list[dict]) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, path)


def main() -> None:
    output = os.environ.get("STACKWISE_TARGETS_FILE", DEFAULT_OUTPUT)
    switch_file = os.environ.get("SWITCH_TARGETS_FILE", "/targets/switch_targets.json")
    community = os.environ.get("SNMP_COMMUNITY", "global")
    timeout = max(1, int(os.environ.get("STACKWISE_DISCOVERY_TIMEOUT", "1") or "1"))
    workers = max(1, int(os.environ.get("STACKWISE_DISCOVERY_WORKERS", "8") or "8"))

    candidates: dict[str, str] = {}
    for key in ("CORE_SWITCH_PING", "DIST_SWITCH_PING", "TOURNAMENT_SWITCHES"):
        candidates.update(configured_targets(os.environ.get(key, "")))
    candidates.update(load_file_sd(switch_file))

    previous = load_file_sd(output)
    ordered_ips = sorted(candidates, key=lambda value: int(IPv4Address(value)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        values = executor.map(lambda ip: stack_member_count(ip, community, timeout), ordered_ips)
        counts = dict(zip(ordered_ips, values))

    selected, confirmed, retained = select_stacks(candidates, previous, counts)
    write_file_sd(output, build_file_sd(selected))
    count_text = ", ".join(f"{candidates[ip]}={counts[ip]}" for ip in confirmed) or "none"
    print(
        f"[stackwise-discovery] checked {len(candidates)} switch(es); "
        f"confirmed {len(confirmed)} ({count_text}); retained {len(retained)} degraded/unreachable",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
