"""历史 FDB 仅作候选；AP 权威来自当前精确 GET，不扫描 MAC 表。"""
import json
import math
import os
import re
import time
from collections import Counter
from ipaddress import IPv4Address
from urllib.parse import urlencode
from urllib.request import urlopen

from target_utils import normalize_mac

MAX_APS = 512
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
AP_QUERY = 'unpoller_device_info{type="uap"}'


def _bounded_setting(name, default, maximum):
    try:
        return min(maximum, max(0, int(os.environ.get(name, str(default)))))
    except ValueError:
        return default


def ap_candidate_max_age() -> int:
    return _bounded_setting("TOPOLOGY_AP_FDB_CANDIDATE_MAX_AGE_SECONDS", 28800, 28800)


def ap_candidate_cap() -> int:
    return _bounded_setting("TOPOLOGY_AP_FDB_CANDIDATE_CAP", 8, 8)


def vlan_fk_mapping(rows: list[dict]) -> dict:
    mapping = {}
    conflicts = set()
    for row in rows:
        try:
            device_id, internal, vlan = (int(row[key]) for key in ("device_id", "vlan_id", "vlan_vlan"))
        except (KeyError, ValueError, TypeError):
            continue
        if device_id <= 0 or internal <= 0 or not 1 <= vlan <= 4094:
            continue
        key = (device_id, internal)
        if key in mapping and mapping[key] != vlan:
            conflicts.add(key)
        mapping[key] = vlan
    return {key: value for key, value in mapping.items() if key not in conflicts}


def ap_candidates_with_status(aps: list[dict], inventory: dict, excluded_ips=(), now=None,
                              *, port_allowed=None) -> tuple[dict, dict]:
    """Return candidates and an explicit per-AP candidate status.

    The status sets distinguish a genuinely empty AP, an AP whose raw candidate
    count exceeded the hard cap, and an AP with normal candidates.  The caller
    must use `candidate_cap_exceeded` to suppress historical hint fallback; a
    missing key is not evidence that the AP simply has no candidates.
    """
    now = time.time() if now is None else now
    result = {}
    no_candidate = set()
    cap_exceeded = set()
    normal_candidates = set()
    for ap in aps:
        mac = ap["mac"]
        unique = {}
        for row in inventory.get(mac, []):
            elapsed = now - row.get("evidence_observed_at", now)
            age = row.get("evidence_age_seconds")
            if (age is None or not math.isfinite(age) or elapsed < 0 or age + elapsed < 0
                    or age + elapsed > ap_candidate_max_age() or row["switch_ip"] in excluded_ips
                    or row.get("firewall") or row.get("ap_aggregate_member", row.get("aggregate_member")) or row.get("confirmed_down")):
                continue
            if port_allowed is not None and not port_allowed(row["switch_ip"], row["ifindex"]):
                continue
            key = (row["switch_ip"], row["ifindex"], row.get("librenms_vlan_id"))
            # shared inventory 的旧 vlan 字段属于服务器契约，不能作为 AP VLAN。
            unique.setdefault(key, {field: value for field, value in row.items() if field != "vlan"})
        if not unique:
            no_candidate.add(mac)
        elif len(unique) > ap_candidate_cap():
            # Never truncate to the cap and never fall back to historical hints.
            cap_exceeded.add(mac)
        else:
            result[mac] = list(unique.values())
            normal_candidates.add(mac)
    return result, {
        "no_candidate": no_candidate,
        "candidate_cap_exceeded": cap_exceeded,
        "normal_candidates": normal_candidates,
    }


def ap_candidates(aps: list[dict], inventory: dict, excluded_ips=(), now=None, *,
                  port_allowed=None) -> dict:
    """Compatibility wrapper returning only candidates under the hard cap."""
    result, _status = ap_candidates_with_status(
        aps, inventory, excluded_ips, now, port_allowed=port_allowed)
    return result


def validate_ap_candidates_detailed(candidates: dict, vlan_map: dict,
                                    exact_lookup, port_check, annotate=None) -> dict:
    """Validate every raw candidate and keep explicit outcome categories.

    ``successful`` rows are the only rows that may become attachments.  A missing
    VLAN, exact lookup exception/budget loss, or port_check exception is
    ``incomplete`` (fail-closed); an exact miss, down/aggregate/interconnect
    port is an explicit ``mismatched`` row.  Keeping these categories separate
    prevents historical downstream rows from pre-empting a valid current core
    exact lookup and prevents partial hint results from becoming authoritative.
    """
    result = {}
    for mac, rows in candidates.items():
        successful = []
        mismatched = []
        incomplete = []
        for row in rows:
            vlan = vlan_map.get((row.get("device_id"), row.get("librenms_vlan_id")))
            if vlan is None:
                incomplete.append({**row, "reason": "vlan_mapping_missing"})
                continue
            try:
                ifindex = exact_lookup(row["switch_ip"], vlan, mac)
                checked_at = time.time()
                port = port_check(row["switch_ip"], ifindex) if ifindex is not None else None
            except Exception as exc:
                incomplete.append({**row, "vlan": vlan,
                                   "reason": type(exc).__name__})
                continue
            if ifindex is None:
                mismatched.append({**row, "vlan": vlan, "reason": "exact_miss"})
                continue
            if not port:
                mismatched.append({**row, "vlan": vlan, "ifindex": ifindex,
                                   "reason": "port_rejected"})
                continue
            validated_row = {**row, "ifindex": ifindex, "port_name": port,
                             "vlan": vlan, "validated_at": checked_at}
            if annotate is not None:
                validated_row.update(annotate(validated_row) or {})
            successful.append(validated_row)
        result[mac] = {
            "raw": len(rows),
            "successful": successful,
            "mismatched": mismatched,
            "incomplete": incomplete,
        }
    return result


def validate_ap_candidates(candidates: dict, vlan_map: dict, exact_lookup, port_check,
                           annotate=None) -> dict:
    """Compatibility wrapper returning only complete successful rows."""
    detailed = validate_ap_candidates_detailed(
        candidates, vlan_map, exact_lookup, port_check, annotate=annotate)
    return {mac: info["successful"] for mac, info in detailed.items()
            if not info["incomplete"]}


def ap_max_age() -> int:
    try:
        return max(0, int(os.environ.get("TOPOLOGY_AP_FDB_MAX_AGE_SECONDS", "7200")))
    except ValueError:
        return 7200


def current_ap_identities(rows: list) -> list[dict]:
    """缺失/重复 IP 或 MAC 均拒绝，不通过排序选择身份。"""
    if not isinstance(rows, list) or len(rows) > MAX_APS:
        raise ValueError("AP inventory limit")
    parsed = []
    for row in rows:
        metric = row.get("metric", {}) if isinstance(row, dict) else {}
        if metric.get("type") != "uap":
            continue
        try:
            ip = str(IPv4Address(metric.get("ip", "")))
        except (ValueError, TypeError):
            ip = ""
        raw_mac = str(metric.get("mac", ""))
        compact = re.sub(r"[:.\-]", "", raw_mac)
        mac = normalize_mac(raw_mac) if re.fullmatch(r"[0-9a-fA-F]{12}", compact) else None
        if mac in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"):
            mac = None
        parsed.append({"ip": ip, "mac": mac,
                       "name": str(metric.get("name") or "")[:128],
                       "model": str(metric.get("model") or "")[:128],
                       "site": str(metric.get("site") or "")[:128]})
    ips = Counter(ap["ip"] for ap in parsed if ap["ip"])
    macs = Counter(ap["mac"] for ap in parsed if ap["mac"])
    return [ap for ap in parsed if ap["ip"] and ap["mac"]
            and ips[ap["ip"]] == 1 and macs[ap["mac"]] == 1]


def fetch_current_aps() -> list[dict]:
    base = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090").rstrip("/")
    url = base + "/api/v1/query?" + urlencode({"query": AP_QUERY})
    with urlopen(url, timeout=5) as response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("AP response limit")
    data = json.loads(body)
    if data.get("status") != "success" or data.get("data", {}).get("resultType") != "vector":
        raise ValueError("AP query unavailable")
    return current_ap_identities(data["data"].get("result"))


def build_ap_artifact(aps: list[dict], inventory: dict, excluded_ips=(), now=None,
                      core_ips=(), online_evidence=None) -> dict:
    """每次仅使用当前证据；多接入点不选主，不继承旧所有权。

    Core automatic placements are formal `observed-attachment` rows only after a
    current exact GET.  Manual core allowlist rows become `confirmed-direct` only
    when the existing online evidence says online; unknown stays observed and
    offline does not emit a current core attachment.  Downstream rows keep their
    existing formal behavior.  A validated downstream placement is never replaced
    here: the collector has already resolved current conflicts.
    """
    now = time.time() if now is None else now
    maximum = ap_max_age()
    attachments = []
    ambiguous = 0
    excluded = set(excluded_ips)
    core_ips = set(core_ips)
    core_levels = ("observed-attachment", "confirmed-direct")
    online_evidence = online_evidence or {}

    def online_status(mac):
        raw = online_evidence.get(mac)
        status = raw.get("status") if isinstance(raw, dict) else raw
        return status if status in ("online", "offline", "unknown") else "unknown"

    for ap in aps:
        candidates = {}
        for candidate in inventory.get(ap["mac"], []):
            checked_at = candidate.get("validated_at")
            age = now - checked_at if checked_at is not None else None
            if (age is None or not math.isfinite(age) or age < 0 or age > maximum
                    or candidate["switch_ip"] in excluded
                    or candidate.get("firewall") or candidate.get("ap_aggregate_member", candidate.get("aggregate_member"))
                    or candidate.get("confirmed_down")):
                continue
            key = (ap["mac"], candidate["switch_ip"], candidate["ifindex"])
            if key not in candidates or age < candidates[key]["evidence_age_seconds"]:
                candidates[key] = {**candidate, "evidence_age_seconds": age}
        rows = list(candidates.values())

        def is_core_row(row):
            return bool(core_ips and row.get("switch_ip") in core_ips) \
                or row.get("ap_level") in core_levels

        # Selection is completed by the collector after current validation.
        # Never choose between current successful positions here: conflicts are
        # ambiguous, not array-order or parent-kind preferences.
        if len(rows) != 1:
            ambiguous += int(len(rows) > 1)
            continue
        candidate = rows[0]
        status = online_status(ap["mac"])
        if is_core_row(candidate) and status == "offline":
            # ap-attachments.json carries current placements only; offline core
            # APs stay in last_observed in ap-placement.json.
            continue
        attachment = {
            "ap_ip": ap["ip"], "ap_mac": ap["mac"], "ap_name": ap["name"],
            "switch_ip": candidate["switch_ip"],
            "switch_ifindex": candidate["ifindex"],
            "switch_port": candidate["port_name"][:128], "vlan": candidate["vlan"],
            "librenms_vlan_id": candidate.get("librenms_vlan_id"),
            "evidence_age_seconds": candidate["evidence_age_seconds"],
        }
        if is_core_row(candidate):
            manual = candidate.get("ap_level") == "confirmed-direct"
            level = "confirmed-direct" if manual and status == "online" else "observed-attachment"
            attachment["level"] = level
            attachment["online"] = status
            attachment["reason"] = (
                "manual_core_port_operator_confirmed"
                if level == "confirmed-direct"
                else "core_forwarding_egress_observed"
            )
        attachments.append(attachment)
    return {"generated_at": now, "source": "librenms-fdb+snmp-exact", "candidate_source": "librenms-fdb", "max_age_seconds": maximum,
            "attachments": attachments, "inventory_count": len(aps),
            "unresolved_count": len(aps) - len(attachments), "ambiguous_count": ambiguous}
