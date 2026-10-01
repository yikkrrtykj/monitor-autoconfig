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


def ap_candidates(aps: list[dict], inventory: dict, excluded_ips=(), now=None) -> dict:
    now = time.time() if now is None else now
    result = {}
    for ap in aps:
        unique = {}
        for row in inventory.get(ap["mac"], []):
            elapsed = now - row.get("evidence_observed_at", now)
            age = row.get("evidence_age_seconds")
            if (age is None or not math.isfinite(age) or elapsed < 0 or age + elapsed < 0
                    or age + elapsed > ap_candidate_max_age() or row["switch_ip"] in excluded_ips
                    or row.get("firewall") or row.get("aggregate_member") or row.get("confirmed_down")):
                continue
            key = (row["switch_ip"], row["ifindex"], row.get("librenms_vlan_id"))
            # shared inventory 的旧 vlan 字段属于服务器契约，不能作为 AP VLAN。
            unique.setdefault(key, {field: value for field, value in row.items() if field != "vlan"})
        # 超限整台 AP 关闭，绝不截断后验证一个子集。
        if len(unique) <= ap_candidate_cap():
            result[ap["mac"]] = list(unique.values())
    return result


def validate_ap_candidates(candidates: dict, vlan_map: dict, exact_lookup, port_check) -> dict:
    validated = {}
    for mac, rows in candidates.items():
        current = []
        complete = True
        for row in rows:
            vlan = vlan_map.get((row.get("device_id"), row.get("librenms_vlan_id")))
            if vlan is None:
                complete = False
                break
            try:
                ifindex = exact_lookup(row["switch_ip"], vlan, mac)
                checked_at = time.time()
                port = port_check(row["switch_ip"], ifindex) if ifindex is not None else None
            except Exception:
                complete = False
                break
            if port:
                current.append({**row, "ifindex": ifindex, "port_name": port,
                                "vlan": vlan, "validated_at": checked_at})
        if complete:
            validated[mac] = current
    return validated


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


def build_ap_artifact(aps: list[dict], inventory: dict, excluded_ips=(), now=None) -> dict:
    """每次仅使用当前证据；多接入点不选主，不继承旧所有权。"""
    now = time.time() if now is None else now
    maximum = ap_max_age()
    attachments = []
    ambiguous = 0
    excluded = set(excluded_ips)
    for ap in aps:
        candidates = {}
        for candidate in inventory.get(ap["mac"], []):
            checked_at = candidate.get("validated_at")
            age = now - checked_at if checked_at is not None else None
            if (age is None or not math.isfinite(age) or age < 0 or age > maximum
                    or candidate["switch_ip"] in excluded
                    or candidate.get("firewall") or candidate.get("aggregate_member")
                    or candidate.get("confirmed_down")):
                continue
            key = (ap["mac"], candidate["switch_ip"], candidate["ifindex"])
            if key not in candidates or age < candidates[key]["evidence_age_seconds"]:
                candidates[key] = {**candidate, "evidence_age_seconds": age}
        if len(candidates) != 1:
            ambiguous += int(len(candidates) > 1)
            continue
        candidate = next(iter(candidates.values()))
        attachments.append({"ap_ip": ap["ip"], "ap_mac": ap["mac"], "ap_name": ap["name"],
                            "switch_ip": candidate["switch_ip"],
                            "switch_ifindex": candidate["ifindex"],
                            "switch_port": candidate["port_name"][:128], "vlan": candidate["vlan"],
                            "librenms_vlan_id": candidate.get("librenms_vlan_id"),
                            "evidence_age_seconds": candidate["evidence_age_seconds"]})
    return {"generated_at": now, "source": "librenms-fdb+snmp-exact", "candidate_source": "librenms-fdb", "max_age_seconds": maximum,
            "attachments": attachments, "inventory_count": len(aps),
            "unresolved_count": len(aps) - len(attachments), "ambiguous_count": ambiguous}
