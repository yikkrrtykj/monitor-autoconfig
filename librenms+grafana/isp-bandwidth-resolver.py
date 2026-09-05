#!/usr/bin/env python3
"""Resolve LibreNMS WAN speed overrides using stable, exact identity evidence."""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict


def _manual_ip_rows(raw: str) -> list[tuple[str, str]]:
    rows = []
    for item in str(raw or "").replace("\n", ",").split(","):
        name, separator, value = item.strip().partition(":")
        if separator and name.strip() and value.strip():
            rows.append((name.strip(), value.strip()))
    return rows


def _bandwidth_config(raw: str) -> tuple[float | None, list[tuple[str, float]], set[str]]:
    text = str(raw or "").strip()
    if not text:
        return None, [], set()
    try:
        return float(text), [], set()
    except ValueError:
        pass
    default = None
    named = []
    for item in text.split(","):
        name, separator, value = item.strip().rpartition(":")
        if not separator or not name.strip():
            continue
        parts = [part.strip() for part in value.split("/")]
        try:
            down = float(parts[0])
            up = float(parts[1]) if len(parts) > 1 else down
        except (ValueError, IndexError):
            continue
        mbps = max(down, up)
        if mbps <= 0:
            continue
        if name.strip() == "*":
            default = mbps
        else:
            named.append((name.strip(), mbps))
    counts = Counter(name.casefold() for name, _mbps in named)
    duplicates = {name for name, count in counts.items() if count > 1}
    return default, named, duplicates


def resolve_port_bandwidth(payload: dict) -> dict:
    ports = [port for port in payload.get("ports", []) if isinstance(port, dict)]
    default, named, duplicate_bandwidth_names = _bandwidth_config(
        payload.get("bandwidth", "")
    )
    manual_rows = _manual_ip_rows(payload.get("manual_ips", ""))
    manual_name_counts = Counter(name.casefold() for name, _value in manual_rows)
    duplicate_manual_names = {
        name for name, count in manual_name_counts.items() if count > 1
    }
    manual_ip_by_name = {
        name.casefold(): value for name, value in manual_rows
    }
    owners_by_ip = defaultdict(set)
    for name, value in manual_rows:
        owners_by_ip[value].add(name.casefold())
    duplicate_manual_ips = {
        value for value, owners in owners_by_ip.items() if len(owners) > 1
    }

    plans = []
    candidate_owners = defaultdict(list)
    warnings = []
    for plan_index, (name, mbps) in enumerate(named):
        key = name.casefold()
        configured_ip = manual_ip_by_name.get(key, "")
        ip_candidates = [] if configured_ip in duplicate_manual_ips else [
            index for index, port in enumerate(ports)
            if configured_ip and configured_ip in {
                str(value).strip() for value in port.get("ips", [])
            }
        ]
        label_candidates = [
            index for index, port in enumerate(ports)
            if key in {
                str(value).strip().casefold() for value in port.get("labels", [])
                if str(value).strip()
            }
        ]
        evidence = [items for items in (ip_candidates, label_candidates) if items]
        unique = {items[0] for items in evidence if len(items) == 1}
        ambiguous = any(len(items) > 1 for items in evidence)
        conflict = len(unique) > 1
        duplicate_name = key in duplicate_bandwidth_names
        duplicate_manual_name = key in duplicate_manual_names
        duplicate_ip = bool(configured_ip in duplicate_manual_ips)
        candidate = (
            next(iter(unique), None)
            if not ambiguous and not conflict and not duplicate_name
            and not duplicate_manual_name and not duplicate_ip
            else None
        )
        plans.append({
            "name": name,
            "mbps": mbps,
            "candidate": candidate,
            "ambiguous": ambiguous,
            "conflict": conflict,
            "duplicate_name": duplicate_name,
            "duplicate_manual_name": duplicate_manual_name,
            "duplicate_ip": duplicate_ip,
        })
        if candidate is not None:
            candidate_owners[candidate].append(plan_index)

    for candidate, owners in candidate_owners.items():
        if len(owners) > 1:
            for plan_index in owners:
                plans[plan_index]["candidate"] = None
                plans[plan_index]["ambiguous"] = True

    named_by_port = {}
    for plan in plans:
        candidate = plan["candidate"]
        if candidate is not None:
            named_by_port[candidate] = plan
            continue
        reason = (
            "duplicate manual WAN IP"
            if plan["duplicate_ip"] else
            "duplicate manual identity"
            if plan["duplicate_manual_name"] else
            "duplicate bandwidth identity"
            if plan["duplicate_name"] else
            "conflicting IP and label evidence"
            if plan["conflict"] else
            "ambiguous identity evidence"
            if plan["ambiguous"] else
            "no exact stable identity match"
        )
        warnings.append(f"{plan['name']}: {reason}; named override skipped")

    decisions = []
    for index, port in enumerate(ports):
        plan = named_by_port.get(index)
        if plan is not None:
            decisions.append({
                "port_id": port.get("port_id"),
                "mbps": plan["mbps"],
                "source": "named",
                "identity": plan["name"],
            })
        elif default is not None:
            decisions.append({
                "port_id": port.get("port_id"),
                "mbps": default,
                "source": "global",
                "identity": "*",
            })
        else:
            warnings.append(
                f"port {port.get('port_id')}: no stable named or global override; existing speed kept"
            )
    return {"decisions": decisions, "warnings": warnings}


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("input must be an object")
        print(json.dumps(resolve_port_bandwidth(payload), ensure_ascii=False))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"ISP bandwidth resolver failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
