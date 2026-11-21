#!/usr/bin/env python3
"""Discover confirmed switches in a management range and emit a Prometheus file_sd.

Operators only fill a switch management range in the control console. ICMP only
proves reachability, so it is never used as switch identity. Every candidate is
probed with one bounded SNMP request (sysDescr, sysObjectID, sysName,
sysServices). A device is admitted to the ordinary switch inventory only when
its identity can be confirmed:

  * a switch family/type is visible in sysDescr (Catalyst, NX-OS, ProCurve,
    EdgeSwitch, C29xx/C35xx/C92xx/C93xx ... families, S-series access models), or
  * a known network vendor answers with an L2-only sysServices signature
    (datalink bit set, network bit clear).

Everything else is recorded as `unverified` with a reason and is *not* written
to the target file, so ping-only hosts (servers, printers, unmanaged APs) can
no longer enter switch monitoring or the topology collector just because they
answer ICMP.

Confirmed devices keep their identity when SNMP temporarily fails (Ping
monitoring continues) and keep the existing 24-hour retention while offline.
Identity confirmation time and last-seen time are tracked separately, so an
unknown host answering Ping can never refresh a switch identity.

State file compatibility: schema v1 (`{ip: {name, last_seen}}`) is migrated on
read. Entries named by IP were only ping placeholders and become `unverified`;
entries with a real hostname keep monitoring as `legacy-state` confirmed
entries until SNMP re-verification decides their real type.

Env vars:
  SWITCH_DISCOVERY_RANGE      IPs / last-octet ranges / CIDR to probe
                              (e.g. 192.168.10.0/24 or 192.168.10.11-30).
  SNMP_COMMUNITY              SNMPv2c community (default: global).
  SWITCH_TARGETS_FILE         output path (default: /targets/switch_targets.json).
  SWITCH_DISCOVERY_WORKERS    parallel probes (default: 8).
  SWITCH_DISCOVERY_PING_TIMEOUT  ICMP timeout seconds (default: 1).
  SWITCH_DISCOVERY_SNMP_TIMEOUT  SNMP timeout seconds (default: 1).
  SWITCH_DISCOVERY_MAX_HOSTS  safety cap on addresses probed (default: 1024).
  SWITCH_DISCOVERY_RETENTION_SECONDS  confirmed offline target retention
                              (default: 86400 / 24 hours).
  SWITCH_DISCOVERY_STATE_FILE  durable discovery ledger (default:
                              SWITCH_TARGETS_FILE + ".state.json").
  CORE_SWITCH_PING/DIST_SWITCH_PING/FIREWALL_PING/TOURNAMENT_SWITCHES
                              already-monitored targets, excluded from results.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from target_utils import (
    build_file_sd,
    expand_ipv4_targets as expand_targets,
    is_ipv4 as looks_like_ip,
    load_file_sd_targets,
    write_json_atomic as write_file_sd,
)

SYS_DESCR_OID = "1.3.6.1.2.1.1.1.0"
SYS_OBJECT_ID_OID = "1.3.6.1.2.1.1.2.0"
SYS_NAME_OID = "1.3.6.1.2.1.1.5.0"
SYS_SERVICES_OID = "1.3.6.1.2.1.1.7.0"
SNMP_IDENTITY_OIDS = (
    SYS_DESCR_OID, SYS_OBJECT_ID_OID, SYS_NAME_OID, SYS_SERVICES_OID,
)
STATE_SCHEMA_VERSION = 2
DEFAULT_MAX_HOSTS = 1024
DEFAULT_RETENTION_SECONDS = 24 * 60 * 60
DEFAULT_LEGACY_GRACE_SECONDS = 7 * 24 * 60 * 60
UNVERIFIED_LOG_LIMIT = 10

# Enterprise OIDs of vendors that actually ship switching products. The vendor
# alone never confirms a switch; it only enables the sysServices signature rule.
ENTERPRISE_VENDORS = {
    "9": "cisco",
    "11": "hp",
    "171": "dlink",
    "207": "allied-telesis",
    "674": "dell",
    "890": "zyxel",
    "1588": "brocade",
    "1916": "extreme",
    "2011": "huawei",
    "2636": "juniper",
    "4526": "netgear",
    "4881": "ruijie",
    "11863": "tp-link",
    "25506": "h3c",
    "30065": "arista",
}

# Explicit switch evidence in sysDescr. Checked after the non-switch markers.
SWITCH_DESCR_MARKERS = (
    "catalyst",
    "nexus",
    "nx-os",
    "procurve",
    "arubaos-switch",
    "arubaos-cx",
    "edgeswitch",
    "powerswitch",
    "powerconnect",
    "ethernet switch",
    "gigabit switch",
    "switch software",
)
_SWITCH_DESCR_WORD = re.compile(r"\bswitch\b", re.IGNORECASE)
_CISCO_SWITCH_MODEL = re.compile(
    r"\bc(?:1000|2960|3560|3650|3750|3850|9200|9300|9400|9500|9600)[a-z0-9-]*\b",
    re.IGNORECASE,
)
_SERIES_SWITCH_MODEL = re.compile(
    r"\bs(?:5130|5560|5720|5735|6520|6720|6800|12500|12700)[a-z0-9-]*\b",
    re.IGNORECASE,
)
# Explicit non-switch evidence. Word boundaries keep "ASA" from matching
# unrelated text such as "LAN BASE".
_NON_SWITCH_DESCR = re.compile(
    r"\b(?:router|isr|asr|asa|firepower|adaptive security|srx|usg|fortigate|"
    r"fortios|wireless|access point|wlc|aironet|controller|printer|jetdirect|"
    r"windows|linux|vmware|esxi|server)\b|\bap[- ]?\d",
    re.IGNORECASE,
)


def excluded_ips(*raws: str) -> set[str]:
    out: set[str] = set()
    for raw in raws:
        out.update(expand_targets(raw))
    return out


def ping_alive(ip: str, timeout: int = 1) -> bool:
    try:
        return subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout), ip],
            capture_output=True,
        ).returncode == 0
    except Exception:
        return False


_SNMP_RECORD = re.compile(r"^(?P<oid>\.?\d+(?:\.\d+)+)\s*=\s*(?P<body>.*)$")
_SNMP_TYPE_PREFIX = re.compile(r"^[A-Za-z][A-Za-z0-9 -]*:\s")


def _normalize_oid(value: str) -> str:
    return str(value or "").strip().lstrip(".")


def parse_snmpget_output(text: str) -> dict[str, str]:
    """Map numeric OIDs to their values from one snmpget response.

    `snmpget` prints one ``OID = value`` record per varbind, but a STRING value
    may contain embedded newlines; continuation lines do not start with an OID.
    Records are therefore split on OID boundaries, never on physical lines, so
    a multi-line sysDescr cannot shift sysObjectID/sysName/sysServices.
    """
    records: dict[str, str] = {}
    current = None
    for raw_line in str(text or "").splitlines():
        match = _SNMP_RECORD.match(raw_line)
        if match:
            current = _normalize_oid(match.group("oid"))
            records[current] = match.group("body")
            continue
        if current is not None:
            records[current] += "\n" + raw_line
    values: dict[str, str] = {}
    for oid, body in records.items():
        first, separator, rest = body.partition("\n")
        first = _SNMP_TYPE_PREFIX.sub("", first, count=1).strip()
        values[oid] = first if not separator else f"{first}\n{rest}".strip()
    return values


def _usable_value(value: str) -> str:
    """Collapse one parsed value into a single line, dropping SNMP sentinels."""
    text = " ".join(str(value or "").split())
    lowered = text.lower()
    if not text or lowered.startswith((
        "no such", "no more", "timeout", "end of mib", "endofmibview",
    )):
        return ""
    return text


def snmp_identity(ip: str, community: str, timeout: int = 1) -> dict[str, str]:
    """Return one bounded identity probe (4 varbinds, one request)."""
    try:
        result = subprocess.run(
            ["snmpget", "-v2c", "-c", community, "-On", "-t", str(timeout),
             "-r", "0", ip, *SNMP_IDENTITY_OIDS],
            capture_output=True, text=True, timeout=timeout + 3,
        )
    except Exception:
        return {}
    if result.returncode != 0:
        return {}
    records = parse_snmpget_output(result.stdout)
    if not records:
        return {}
    descr = _usable_value(records.get(SYS_DESCR_OID, ""))
    object_id = _normalize_oid(_usable_value(records.get(SYS_OBJECT_ID_OID, "")))
    name = _usable_value(records.get(SYS_NAME_OID, ""))
    services = _usable_value(records.get(SYS_SERVICES_OID, ""))
    if not any((descr, object_id, name, services)):
        return {}
    return {
        "sys_descr": descr,
        "sys_object_id": object_id,
        "sys_name": name,
        "sys_services": services,
    }


def vendor_for_object_id(object_id: str) -> str:
    parts = _normalize_oid(object_id).split(".")
    if len(parts) < 7 or parts[:6] != ["1", "3", "6", "1", "4", "1"]:
        return ""
    return ENTERPRISE_VENDORS.get(parts[6], "")


def _switch_description(descr: str) -> bool:
    if not descr:
        return False
    lowered = descr.lower()
    if any(marker in lowered for marker in SWITCH_DESCR_MARKERS):
        return True
    if _SWITCH_DESCR_WORD.search(lowered):
        return True
    return bool(
        _CISCO_SWITCH_MODEL.search(descr) or _SERIES_SWITCH_MODEL.search(descr)
    )


def _sys_services(value: str) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = int(text)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def classify_identity(identity: dict[str, str]) -> tuple[str, str, str]:
    """Classify one SNMP identity probe.

    Returns ``(verdict, source, reason)`` where verdict is ``switch``,
    ``not-switch`` or ``unknown``. ``not-switch`` is reserved for explicit
    evidence; an inconclusive probe stays ``unknown`` so a previously
    confirmed device is never demoted by a weak reading.
    """
    descr = str(identity.get("sys_descr") or "").strip()
    object_id = str(identity.get("sys_object_id") or "").strip()
    name = str(identity.get("sys_name") or "").strip()
    services = _sys_services(identity.get("sys_services") or "")
    if not any((descr, object_id, name, services is not None)):
        return "unknown", "", "no-snmp-identity"
    if descr and _NON_SWITCH_DESCR.search(descr):
        return "not-switch", "", "non-switch-description"
    if _switch_description(descr):
        return "switch", "snmp-description", ""
    vendor = vendor_for_object_id(object_id)
    if not vendor:
        return "unknown", "", "unknown-vendor"
    if services is None:
        return "unknown", "", "missing-sys-services"
    if not services & 2:  # datalink layer is absent: host-like device
        return "not-switch", "", "host-signature"
    if services & 4:  # bridging plus routing: cannot confirm a switch cheaply
        return "unknown", "", "l3-signature"
    return "switch", "snmp-l2-services", ""


def _display_name(value: object) -> str:
    name = str(value or "").strip()
    return "" if not name or looks_like_ip(name) else name


def _float_or_none(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _entry(ip: str, name: str, status: str, source: str | None,
           confirmed_at: float | None, last_seen: float | None,
           object_id: str | None = None,
           reason: str | None = None,
           pending_verification: bool = False,
           legacy_deadline: float | None = None) -> dict:
    return {
        "name": name or ip,
        "status": status,
        "source": source,
        "identity_confirmed_at": confirmed_at,
        "last_seen": last_seen,
        "sys_object_id": object_id or None,
        "reason": reason or None,
        "pending_verification": bool(pending_verification),
        "legacy_deadline": legacy_deadline,
    }


def discover(candidates, community, probe_identity=snmp_identity,
             probe_ping=ping_alive, workers=8, ping_timeout=1,
             snmp_timeout=1) -> dict[str, dict]:
    """Probe every candidate once and return observed identities.

    ICMP and SNMP run independently: an ACL may block ICMP on a healthy switch,
    and a reachable host may not answer SNMP. Only observed candidates are
    returned; the caller decides identity and retention.
    """
    candidates = list(dict.fromkeys(candidates))
    if not candidates:
        return {}
    workers = max(1, int(workers))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        ping_ok = dict(zip(
            candidates,
            executor.map(lambda ip: probe_ping(ip, ping_timeout), candidates),
        ))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        identities = dict(zip(
            candidates,
            executor.map(
                lambda ip: probe_identity(ip, community, snmp_timeout),
                candidates,
            ),
        ))
    observations = {}
    for ip in candidates:
        identity = identities.get(ip) or {}
        if ping_ok.get(ip) or identity:
            observations[ip] = {
                "ping": bool(ping_ok.get(ip)),
                "snmp": identity,
            }
    return observations


def normalize_state(payload) -> dict[str, dict]:
    """Read schema v1 or v2 into the current entry shape.

    v1 only stored ``name``/``last_seen``. An IP-shaped name was a ping
    placeholder and never proved switch identity. A real hostname proves the old
    scanner received an SNMP sysName, which any printer/server/AP can provide,
    so it becomes a **legacy pending** entry: monitoring continues for a bounded
    verification window, but it is never treated as an SNMP-confirmed switch.
    """
    if not isinstance(payload, dict):
        return {}
    entries: dict[str, dict] = {}
    if payload.get("schema_version") == STATE_SCHEMA_VERSION:
        for ip, item in payload.items():
            if not looks_like_ip(ip) or not isinstance(item, dict):
                continue
            name = str(item.get("name") or ip).strip() or ip
            status = str(item.get("status") or "unverified").strip().lower()
            if status not in ("confirmed", "unverified"):
                status = "unverified"
            source = str(item.get("source") or "").strip() or None
            pending = bool(item.get("pending_verification")) or (
                source == "legacy-state"
            )
            entries[ip] = _entry(
                ip, name, status, source,
                _float_or_none(item.get("identity_confirmed_at")),
                _float_or_none(item.get("last_seen")),
                str(item.get("sys_object_id") or "").strip() or None,
                str(item.get("reason") or "").strip() or None,
                pending_verification=pending,
                legacy_deadline=_float_or_none(item.get("legacy_deadline")),
            )
        return entries
    for ip, item in payload.items():
        if not looks_like_ip(ip) or not isinstance(item, dict):
            continue
        name = str(item.get("name") or ip).strip() or ip
        last_seen = _float_or_none(item.get("last_seen"))
        if looks_like_ip(name):
            entries[ip] = _entry(
                ip, name, "unverified", None, None, last_seen,
                None, "legacy-ping-placeholder",
            )
        else:
            entries[ip] = _entry(
                ip, name, "confirmed", "legacy-state", None, last_seen,
                None, "awaiting-verification",
                pending_verification=True,
            )
    return entries


def read_state_payload(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def load_state(path: str) -> dict[str, dict]:
    return normalize_state(read_state_payload(path))


def state_payload(entries: dict[str, dict]) -> dict:
    payload = {"schema_version": STATE_SCHEMA_VERSION}
    payload.update(entries)
    return payload


def update_entries(observations, candidates, previous, now,
                   retention_seconds,
                   legacy_grace_seconds=DEFAULT_LEGACY_GRACE_SECONDS
                   ) -> tuple[dict[str, str], dict[str, dict]]:
    """Merge this cycle's observations with the durable identity ledger.

    Returns ``(targets, entries)``. Only ``confirmed`` entries become targets;
    unverified devices are kept for diagnostics and never enter switch
    monitoring. Identity confirmation time and last-seen time never share a
    field, so a Ping-only unknown host cannot refresh a switch identity.

    ``legacy-state`` entries (migrated from v1, where a sysName alone admitted a
    device) keep monitoring only for a bounded verification window. They are
    provisional: an SNMP switch verdict promotes them to a real source, explicit
    non-switch evidence removes them immediately, and the deadline retires
    devices that never prove themselves. Already SNMP-confirmed switches are
    unaffected and keep the 24-hour outage retention.
    """
    now = float(now)
    retention_seconds = max(0, int(retention_seconds))
    grace_seconds = max(0, int(legacy_grace_seconds))
    candidates = list(dict.fromkeys(candidates))
    entries: dict[str, dict] = {}
    targets: dict[str, str] = {}
    for ip in candidates:
        prev = previous.get(ip) or {}
        observation = observations.get(ip) or {}
        identity = observation.get("snmp") or {}
        ping_ok = bool(observation.get("ping"))
        prev_status = str(prev.get("status") or "").strip().lower()
        prev_confirmed = prev_status == "confirmed"
        prev_name = str(prev.get("name") or "").strip()
        prev_confirmed_at = _float_or_none(prev.get("identity_confirmed_at"))
        prev_last_seen = _float_or_none(prev.get("last_seen"))
        prev_source = str(prev.get("source") or "").strip() or None
        pending = bool(prev.get("pending_verification")) or (
            prev_source == "legacy-state"
        )
        deadline = _float_or_none(prev.get("legacy_deadline"))

        if identity:
            verdict, source, reason = classify_identity(identity)
            name = (_display_name(identity.get("sys_name"))
                    or (prev_name if prev_confirmed else "") or ip)
            object_id = str(identity.get("sys_object_id") or "").strip() or None
            if verdict == "switch":
                entries[ip] = _entry(ip, name, "confirmed", source, now, now,
                                     object_id)
                targets[ip] = entries[ip]["name"]
                continue
            if verdict == "unknown" and prev_confirmed:
                # SNMP answered but cannot confirm the type. Keep the already
                # confirmed identity and its Ping monitoring instead of
                # degrading it on a weak reading.
                if pending:
                    deadline = deadline if deadline is not None else now + grace_seconds
                    if now > deadline:
                        entries[ip] = _entry(
                            ip, name, "unverified", None, None, now,
                            object_id, "legacy-grace-expired",
                        )
                        continue
                    entries[ip] = _entry(
                        ip, name, "confirmed", prev_source, None, now,
                        object_id, "awaiting-verification",
                        pending_verification=True, legacy_deadline=deadline,
                    )
                    targets[ip] = entries[ip]["name"]
                    continue
                confirmed_at = prev_confirmed_at if prev_confirmed_at is not None else now
                entries[ip] = _entry(
                    ip, name, "confirmed", prev_source, confirmed_at,
                    now, object_id, reason,
                )
                targets[ip] = entries[ip]["name"]
                continue
            entries[ip] = _entry(ip, name, "unverified", None, None, now,
                                 object_id, reason)
            continue

        if prev_confirmed:
            last_seen = now if ping_ok else (
                prev_last_seen if prev_last_seen is not None else prev_confirmed_at
            )
            if not ping_ok:
                if last_seen is None or now - last_seen > retention_seconds:
                    continue
            if pending:
                deadline = deadline if deadline is not None else now + grace_seconds
                if now > deadline:
                    # Bounded legacy verification window is over: a device that
                    # never proved itself as a switch must not keep the slot.
                    if ping_ok:
                        entries[ip] = _entry(
                            ip, prev_name or ip, "unverified", None, None, now,
                            prev.get("sys_object_id"), "legacy-grace-expired",
                        )
                    continue
                entries[ip] = _entry(
                    ip, prev_name or ip, "confirmed", prev_source, None,
                    last_seen, prev.get("sys_object_id"), "awaiting-verification",
                    pending_verification=True, legacy_deadline=deadline,
                )
                targets[ip] = entries[ip]["name"]
                continue
            entries[ip] = _entry(
                ip, prev_name or ip, "confirmed", prev_source,
                prev_confirmed_at, last_seen, prev.get("sys_object_id"),
            )
            targets[ip] = entries[ip]["name"]
            continue

        if ping_ok:
            entries[ip] = _entry(
                ip, prev_name or ip, "unverified", None, None, now,
                prev.get("sys_object_id"), "no-snmp",
            )

    return targets, entries


def _log_unverified(entries: dict[str, dict]) -> None:
    unverified = sorted(
        (ip, entry) for ip, entry in entries.items()
        if entry.get("status") == "unverified"
    )
    if not unverified:
        return
    print(
        f"[switch-discovery] unverified candidates: {len(unverified)} "
        "(kept for diagnostics, not monitored)",
        file=sys.stderr,
    )
    for ip, entry in unverified[:UNVERIFIED_LOG_LIMIT]:
        print(
            f"[switch-discovery]   {ip} {entry.get('name') or '-'} "
            f"reason={entry.get('reason') or 'unknown'}",
            file=sys.stderr,
        )
    omitted = len(unverified) - UNVERIFIED_LOG_LIMIT
    if omitted > 0:
        print(
            f"[switch-discovery]   ... {omitted} more unverified candidate(s)",
            file=sys.stderr,
        )


def _log_legacy_pending(entries: dict[str, dict], now: float) -> None:
    """Expose the bounded migration window and each retirement."""
    expired = sorted(
        (ip, entry) for ip, entry in entries.items()
        if entry.get("reason") == "legacy-grace-expired"
    )
    for ip, entry in expired:
        print(
            f"[switch-discovery] legacy grace expired: {ip} "
            f"{entry.get('name') or '-'} (identity never proven)",
            file=sys.stderr,
        )
    pending = sorted(
        (ip, entry) for ip, entry in entries.items()
        if entry.get("pending_verification")
    )
    if not pending:
        return
    deadlines = [
        entry["legacy_deadline"] for _, entry in pending
        if entry.get("legacy_deadline") is not None
    ]
    nearest = ""
    if deadlines:
        nearest = f", nearest deadline in {max(0.0, min(deadlines) - now) / 3600:.1f}h"
    print(
        f"[switch-discovery] legacy hosts awaiting SNMP verification: "
        f"{len(pending)}{nearest}",
        file=sys.stderr,
    )
    for ip, entry in pending[:UNVERIFIED_LOG_LIMIT]:
        print(
            f"[switch-discovery]   {ip} {entry.get('name') or '-'} "
            f"source={entry.get('source') or '-'}",
            file=sys.stderr,
        )


def main() -> None:
    raw = os.environ.get("SWITCH_DISCOVERY_RANGE", "").strip()
    out = os.environ.get("SWITCH_TARGETS_FILE", "/targets/switch_targets.json")
    community = os.environ.get("SNMP_COMMUNITY", "global")
    workers = int(os.environ.get("SWITCH_DISCOVERY_WORKERS", "8") or "8")
    ping_timeout = int(os.environ.get("SWITCH_DISCOVERY_PING_TIMEOUT", "1") or "1")
    snmp_timeout = int(os.environ.get("SWITCH_DISCOVERY_SNMP_TIMEOUT", "1") or "1")
    max_hosts = int(os.environ.get(
        "SWITCH_DISCOVERY_MAX_HOSTS", str(DEFAULT_MAX_HOSTS)
    ) or DEFAULT_MAX_HOSTS)
    retention_seconds = int(os.environ.get(
        "SWITCH_DISCOVERY_RETENTION_SECONDS", str(DEFAULT_RETENTION_SECONDS)
    ) or DEFAULT_RETENTION_SECONDS)
    legacy_grace_seconds = int(os.environ.get(
        "SWITCH_DISCOVERY_LEGACY_GRACE_SECONDS",
        str(DEFAULT_LEGACY_GRACE_SECONDS),
    ) or DEFAULT_LEGACY_GRACE_SECONDS)
    state_file = os.environ.get(
        "SWITCH_DISCOVERY_STATE_FILE", f"{out}.state.json"
    )

    exclude = excluded_ips(
        os.environ.get("CORE_SWITCH_PING", ""),
        os.environ.get("DIST_SWITCH_PING", ""),
        os.environ.get("FIREWALL_PING", ""),
        os.environ.get("TOURNAMENT_SWITCHES", ""),
    )
    ips = [ip for ip in expand_targets(raw) if ip not in exclude]
    if len(ips) > max_hosts:
        print(
            f"[switch-discovery] range expands to {len(ips)} addresses; "
            f"capping at {max_hosts}",
            file=sys.stderr,
        )
        ips = ips[:max_hosts]

    if not ips:
        write_file_sd(out, [])
        write_file_sd(state_file, state_payload({}), sort_keys=True)
        print(
            "[switch-discovery] no range configured; wrote empty target file",
            file=sys.stderr,
        )
        return
    observations = discover(
        ips, community, workers=workers,
        ping_timeout=ping_timeout, snmp_timeout=snmp_timeout,
    )
    now = time.time()
    previous_payload = read_state_payload(state_file)
    previous = normalize_state(previous_payload)
    if previous_payload and previous_payload.get(
        "schema_version"
    ) != STATE_SCHEMA_VERSION:
        pending = sum(
            1 for entry in previous.values()
            if entry.get("pending_verification")
        )
        placeholders = sum(
            1 for entry in previous.values()
            if entry.get("status") == "unverified"
        )
        print(
            "[switch-discovery] migrated legacy state: "
            f"{pending} legacy hosts awaiting SNMP verification, "
            f"{placeholders} ping placeholders unverified",
            file=sys.stderr,
        )
    targets, entries = update_entries(
        observations, ips, previous, now, retention_seconds,
        legacy_grace_seconds=legacy_grace_seconds,
    )
    write_file_sd(state_file, state_payload(entries), sort_keys=True)
    write_file_sd(out, build_file_sd(targets))
    confirmed = sum(
        1 for entry in entries.values() if entry.get("status") == "confirmed"
    )
    unverified = len(entries) - confirmed
    retained = len(targets) - sum(
        1 for ip in targets if observations.get(ip, {}).get("snmp")
        or observations.get(ip, {}).get("ping")
    )
    print(
        f"[switch-discovery] probed {len(ips)} addresses -> "
        f"{confirmed} confirmed, {unverified} unverified, {retained} offline retained",
        file=sys.stderr,
    )
    _log_unverified(entries)
    _log_legacy_pending(entries, now)


if __name__ == "__main__":
    main()
