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

import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from ipaddress import IPv4Address

from target_utils import (
    build_file_sd,
    load_file_sd_targets as load_file_sd,
    merge_display_names,
    parse_named_ipv4_targets as configured_targets,
    write_json_atomic as write_file_sd,
)

STACK_MEMBER_NUMBER_OID = "1.3.6.1.4.1.9.9.500.1.2.1.1.1"
DEFAULT_OUTPUT = "/targets/stackwise_targets.json"


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


def main() -> None:
    output = os.environ.get("STACKWISE_TARGETS_FILE", DEFAULT_OUTPUT)
    switch_file = os.environ.get("SWITCH_TARGETS_FILE", "/targets/switch_targets.json")
    community = os.environ.get("SNMP_COMMUNITY", "global")
    timeout = max(1, int(os.environ.get("STACKWISE_DISCOVERY_TIMEOUT", "1") or "1"))
    workers = max(1, int(os.environ.get("STACKWISE_DISCOVERY_WORKERS", "8") or "8"))

    # Auto-discovered targets are the lowest-priority source: their IP
    # placeholder must never overwrite a configured/known device name.
    candidates = load_file_sd(switch_file)
    for key in ("CORE_SWITCH_PING", "DIST_SWITCH_PING", "TOURNAMENT_SWITCHES"):
        candidates = merge_display_names(candidates, configured_targets(os.environ.get(key, "")))

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
