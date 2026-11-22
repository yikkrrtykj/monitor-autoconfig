"""AP 定位影子证据（P1）：只读模型，不驱动连线、告警或自动删除。

`ap-placement.json` 只用于离线比对与后续阶段评审。它复用现有 AP 身份、
LibreNMS FDB inventory 和精确 SNMP GET 结果，不新增全量 walk，也不接入
UniFi Controller。正式拓扑仍只消费 `ap-attachments.json`。
"""
from __future__ import annotations

import json
import math
import os
import time
from urllib.parse import urlencode
from urllib.request import urlopen

from target_utils import normalize_mac

PLACEMENT_SCHEMA_VERSION = 1
PLACEMENT_SOURCE = "ap-placement-shadow:librenms-fdb+snmp-exact"
MAX_PLACEMENTS = 512
MAX_ONLINE_RESPONSE_BYTES = 1024 * 1024
ONLINE_QUERY_TIMEOUT = 5

LEVEL_CONFIRMED_DIRECT = "confirmed-direct"
LEVEL_OBSERVED_ATTACHMENT = "observed-attachment"
LEVEL_AMBIGUOUS = "ambiguous"
LEVEL_UNRESOLVED = "unresolved"
LEVELS = (
    LEVEL_CONFIRMED_DIRECT,
    LEVEL_OBSERVED_ATTACHMENT,
    LEVEL_AMBIGUOUS,
    LEVEL_UNRESOLVED,
)

ONLINE_STATES = ("online", "offline", "unknown")
COVERAGE_STATES = ("complete", "partial", "unknown")

# 在线状态只看明确的 unpoller 设备状态系列；身份记录存在不代表在线。
ONLINE_QUERY = (
    'max by (mac) ({__name__=~"unpoller_device_(up|connected|state|status)",type="uap"}) '
    "or "
    'max by (mac) ({__name__=~"unpoller_device_uptime(_seconds)?",type="uap"} > bool 0)'
)


def _as_positive_int(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _finite_age(value):
    try:
        age = float(value)
    except (TypeError, ValueError):
        return None
    return age if math.isfinite(age) and age >= 0 else None


def ap_online_states(rows):
    """把 Prometheus vector 结果解析成 `{mac: online|offline}`。"""
    states = {}
    if not isinstance(rows, list):
        return states
    for item in rows:
        if not isinstance(item, dict):
            continue
        metric = item.get("metric") or {}
        mac = normalize_mac(metric.get("mac") or "")
        if not mac:
            continue
        try:
            value = float((item.get("value") or [None, ""])[1])
        except (TypeError, ValueError, IndexError):
            continue
        if not math.isfinite(value):
            continue
        states[mac] = "online" if value > 0 else "offline"
    return states


def fetch_ap_online_states(timeout=ONLINE_QUERY_TIMEOUT):
    """单次有界 Prometheus 查询；失败抛异常，由调用方降级为 unknown。"""
    base = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090").rstrip("/")
    url = base + "/api/v1/query?" + urlencode({"query": ONLINE_QUERY})
    with urlopen(url, timeout=timeout) as response:
        body = response.read(MAX_ONLINE_RESPONSE_BYTES + 1)
    if len(body) > MAX_ONLINE_RESPONSE_BYTES:
        raise ValueError("AP online response limit")
    data = json.loads(body)
    if data.get("status") != "success":
        raise ValueError("AP online query unavailable")
    payload = data.get("data") or {}
    if payload.get("resultType") != "vector":
        raise ValueError("AP online query unavailable")
    return ap_online_states(payload.get("result"))


def placement_coverage(fdb_status, librenms_ready, has_aps):
    """FDB 采集覆盖率；缺采集只能让证据更保守，不能提升等级。"""
    status = {"status": "unknown", "fdb_switches_usable": 0, "fdb_switches_failed": 0}
    if not has_aps:
        return status
    try:
        status["fdb_switches_usable"] = max(0, int((fdb_status or {}).get("usable_switches", 0)))
        status["fdb_switches_failed"] = max(0, int((fdb_status or {}).get("failed_switches", 0)))
    except (TypeError, ValueError):
        return status
    if not librenms_ready or status["fdb_switches_usable"] <= 0:
        return status
    status["status"] = "partial" if status["fdb_switches_failed"] > 0 else "complete"
    return status


def _manual_core_confirmation(row, core_ips, manual_core_ports):
    if not row:
        return False
    switch_ip = str(row.get("switch_ip") or "").strip()
    port_name = str(row.get("port_name") or "").strip()
    if switch_ip not in core_ips or not port_name:
        return False
    return (switch_ip, port_name.casefold()) in manual_core_ports


def _placement_rows(ap_mac, validated, shadow):
    """合并正式 exact 结果与 shadow exact 结果；同一 switch/ifindex 只留一条。"""
    merged = []
    seen = set()
    sources = (
        ("formal", validated or {}),
        ("shadow", (shadow or {}).get("validated") or {}),
    )
    for source, table in sources:
        for row in table.get(ap_mac, []) or []:
            if not isinstance(row, dict):
                continue
            switch_ip = str(row.get("switch_ip") or "").strip()
            ifindex = _as_positive_int(row.get("ifindex"))
            if not switch_ip or ifindex is None:
                continue
            key = (switch_ip, ifindex)
            if key in seen:
                continue
            seen.add(key)
            merged.append((source, row))
    return merged


def _unresolved_reason(diagnostics, coverage_status):
    diagnostics = diagnostics or {}
    if diagnostics.get("exact_errors"):
        return "exact_lookup_failed"
    if diagnostics.get("budget_skipped"):
        return "shadow_budget_exhausted"
    if diagnostics.get("missing_vlan"):
        return "vlan_mapping_missing"
    if diagnostics.get("incomplete"):
        return "validation_incomplete"
    raw = max(0, int(diagnostics.get("raw_candidates") or 0))
    eligible = max(0, int(diagnostics.get("eligible_candidates") or 0))
    if raw <= 0:
        return "no_fdb_candidate"
    if eligible <= 0:
        return "no_eligible_fdb_candidate"
    if coverage_status == "unknown":
        return "coverage_unknown"
    return "port_rejected_after_exact_get"


def _evidence(row, manual, diagnostics, online, coverage_status):
    diagnostics = diagnostics or {}
    fdb_status = "matched" if row else (
        "no_candidate" if int(diagnostics.get("raw_candidates") or 0) <= 0
        else "filtered_or_stale"
    )
    if row:
        exact_status = "matched"
    elif diagnostics.get("exact_errors"):
        exact_status = "failed"
    elif diagnostics.get("budget_skipped"):
        exact_status = "budget_exhausted"
    elif diagnostics.get("missing_vlan"):
        exact_status = "vlan_mapping_missing"
    else:
        exact_status = "not_attempted"
    return {
        "fdb": {
            "status": fdb_status,
            "raw_candidates": max(0, int(diagnostics.get("raw_candidates") or 0)),
            "eligible_candidates": max(0, int(diagnostics.get("eligible_candidates") or 0)),
        },
        "exact_snmp": {
            "status": exact_status,
            "gets": max(0, int(diagnostics.get("exact_gets") or 0)),
        },
        "direct_neighbor": {
            "status": "operator-attested" if manual else "unknown",
            "source": "manual-allowlist" if manual else "none",
        },
        "manual_confirmation": {"status": "confirmed" if manual else "none"},
        "online": {"status": online, "source": "unpoller-device-metrics"},
        "coverage": {
            "status": coverage_status,
            "fdb_switches_usable": 0,
            "fdb_switches_failed": 0,
        },
        "port": {"verified": bool(row)},
    }


def build_placement_artifact(
    aps,
    validated,
    shadow=None,
    online_states=None,
    *,
    fdb_status=None,
    librenms_ready=True,
    max_age_seconds=7200,
    manual_core_ports=(),
    core_ips=(),
    now=None,
):
    """生成影子产物；纯函数，不写文件、不发请求。

    observed-attachment 只声明“当前已验证的转发出口（egress toward AP）”，
    不承诺物理直连。confirmed-direct 仅接受人工白名单核心口的
    operator-attested 证据（P1 没有已验证的 controller uplink/LLDP）。
    """
    if not isinstance(aps, list):
        raise ValueError("AP list required")
    if len(aps) > MAX_PLACEMENTS:
        raise ValueError("AP placement limit")
    online_states = online_states or {}
    manual_core_ports = set(manual_core_ports or ())
    core_ips = set(core_ips or ())
    coverage = placement_coverage(fdb_status, librenms_ready, bool(aps))
    diagnostics_by_mac = (shadow or {}).get("diagnostics") or {}

    counts = {
        "confirmed_direct": 0,
        "observed_attachment": 0,
        "ambiguous": 0,
        "unresolved": 0,
        "online": 0,
        "offline": 0,
        "unknown": 0,
        "total": 0,
    }
    placements = []
    ordered = sorted(
        (ap for ap in aps if isinstance(ap, dict)),
        key=lambda ap: (str(ap.get("ip") or ""), str(ap.get("mac") or "")),
    )
    for ap in ordered:
        mac = str(ap.get("mac") or "").strip()
        diagnostics = diagnostics_by_mac.get(mac) or {}
        rows = _placement_rows(mac, validated, shadow) if mac else []
        manual_row = None
        row = None
        if len(rows) > 1:
            level = LEVEL_AMBIGUOUS
            reason = "multiple_verified_candidates"
        elif len(rows) == 1:
            row = rows[0][1]
            manual = _manual_core_confirmation(row, core_ips, manual_core_ports)
            manual_row = row if manual else None
            level = LEVEL_CONFIRMED_DIRECT if manual else LEVEL_OBSERVED_ATTACHMENT
            reason = "operator_confirmed_core_port" if manual else "verified_fdb_egress"
        else:
            level = LEVEL_UNRESOLVED
            reason = _unresolved_reason(diagnostics, coverage["status"])

        online = online_states.get(mac) if mac else None
        online = online if online in ONLINE_STATES else "unknown"
        counts["online" if online == "online" else
               "offline" if online == "offline" else "unknown"] += 1
        counts[{
            LEVEL_CONFIRMED_DIRECT: "confirmed_direct",
            LEVEL_OBSERVED_ATTACHMENT: "observed_attachment",
            LEVEL_AMBIGUOUS: "ambiguous",
            LEVEL_UNRESOLVED: "unresolved",
        }[level]] += 1
        counts["total"] += 1

        evidence = _evidence(row, manual_row is not None, diagnostics, online, coverage["status"])
        evidence["coverage"]["fdb_switches_usable"] = coverage["fdb_switches_usable"]
        evidence["coverage"]["fdb_switches_failed"] = coverage["fdb_switches_failed"]

        placements.append({
            "ap_ip": str(ap.get("ip") or ""),
            "ap_name": str(ap.get("name") or ""),
            "ap_mac": mac,
            "site": str(ap.get("site") or ""),
            "online": online,
            "level": level,
            "switch_ip": str(row.get("switch_ip") or "") if row else None,
            "switch_ifindex": _as_positive_int(row.get("ifindex")) if row else None,
            "switch_port": str(row.get("port_name") or "") if row else None,
            "vlan": _as_positive_int(row.get("vlan")) if row else None,
            "evidence_age_seconds": _finite_age(row.get("evidence_age_seconds")) if row else None,
            "coverage_status": coverage["status"],
            "reason": reason,
            "evidence": evidence,
        })

    return {
        "schema_version": PLACEMENT_SCHEMA_VERSION,
        "generated_at": time.time() if now is None else float(now),
        "source": PLACEMENT_SOURCE,
        "max_age_seconds": max(0, int(max_age_seconds)),
        "counts": counts,
        "coverage": dict(coverage),
        "placements": placements,
    }
