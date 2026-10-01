"""AP 身份与接入证据；只读 Prometheus/LibreNMS，绝不回退到 SNMP。"""
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
            age = candidate.get("evidence_age_seconds")
            elapsed = now - candidate.get("evidence_observed_at", now)
            if age is not None:
                age += elapsed
            if (age is None or not math.isfinite(age) or elapsed < 0 or age < 0 or age > maximum
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
                            "evidence_age_seconds": candidate["evidence_age_seconds"]})
    return {"generated_at": now, "source": "librenms-fdb", "max_age_seconds": maximum,
            "attachments": attachments, "inventory_count": len(aps),
            "unresolved_count": len(aps) - len(attachments), "ambiguous_count": ambiguous}
