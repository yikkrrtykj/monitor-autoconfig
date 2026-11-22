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
# unpoller v2.15.3 的 UAP 只导出 uptime；它不是当前心跳，只用于证明
# 状态序列存在与否，不能据此判定 online。
ONLINE_SAMPLE_MAX_AGE_SECONDS = 90
ONLINE_INFO_QUERY = 'unpoller_device_info{type="uap"}'
ONLINE_UPTIME_QUERY = 'unpoller_device_uptime_seconds{type="uap"}'

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


def _online_default(source, reason):
    return {"status": "unknown", "source": source, "reason": reason}


def online_unavailable_evidence(aps, reason="online_evidence_unavailable"):
    """所有 AP 统一记录 unknown + 证据不可用原因，不强行解释为 online。"""
    evidence = {}
    for ap in aps or []:
        if not isinstance(ap, dict):
            continue
        mac = normalize_mac(ap.get("mac") or "")
        if mac:
            evidence[mac] = _online_default("unavailable", reason)
    return evidence


def ap_online_evidence(aps, identity_rows, uptime_rows, now=None,
                       max_age=ONLINE_SAMPLE_MAX_AGE_SECONDS):
    """用上游真实标签关联 AP 身份与 uptime，但结论只能是 unknown。

    `unpoller_device_info` 才有 mac/ip/id；`unpoller_device_uptime_seconds`
    只有 type/site_name/name/source。严格按 site_name+name 关联并拒绝重名/
    重复样本。uptime 是历史运行时长而非当前心跳，即使样本新鲜、数值 > 0
    也不得据此判 online；这里只把 source/reason 写进证据。
    """
    evidence = {}
    ap_macs = {}
    for ap in aps or []:
        if not isinstance(ap, dict):
            continue
        mac = normalize_mac(ap.get("mac") or "")
        if mac:
            ap_macs[mac] = ap

    identities = {}
    for row in _prom_rows(identity_rows):
        metric = row.get("metric") or {}
        if metric.get("type") != "uap":
            continue
        mac = normalize_mac(metric.get("mac") or "")
        site = str(metric.get("site_name") or "").strip()
        name = str(metric.get("name") or "").strip()
        if not mac or not site or not name:
            continue
        identities.setdefault((site, name), []).append(mac)
    mac_keys = {}
    for key, macs in identities.items():
        for mac in macs:
            mac_keys.setdefault(mac, []).append(key)

    samples = {}
    for row in _prom_rows(uptime_rows):
        metric = row.get("metric") or {}
        if metric.get("type") != "uap":
            continue
        site = str(metric.get("site_name") or "").strip()
        name = str(metric.get("name") or "").strip()
        if not site or not name:
            continue
        try:
            timestamp = float((row.get("value") or [0])[0])
            value = float((row.get("value") or [None, ""])[1])
        except (TypeError, ValueError, IndexError):
            continue
        if not math.isfinite(timestamp) or not math.isfinite(value):
            continue
        samples.setdefault((site, name), []).append((value, timestamp))

    current = time.time() if now is None else float(now)
    for mac in ap_macs:
        keys = mac_keys.get(mac) or []
        if not keys:
            evidence[mac] = _online_default("unavailable", "identity_missing")
            continue
        if len(keys) > 1 or len(identities[keys[0]]) > 1:
            evidence[mac] = _online_default("unpoller-uptime", "duplicate_identity")
            continue
        series = samples.get(keys[0]) or []
        if not series:
            evidence[mac] = _online_default("unavailable", "no_status_sample")
            continue
        if len(series) != 1:
            evidence[mac] = _online_default("unpoller-uptime", "ambiguous_status_sample")
            continue
        value, timestamp = series[0]
        if timestamp <= 0 or current - timestamp > max_age:
            evidence[mac] = _online_default("unpoller-uptime", "stale_status_sample")
            continue
        # 样本新鲜、uptime >= 0 仍然不是在线证明。
        evidence[mac] = _online_default(
            "unpoller-uptime",
            "invalid_status_sample" if value < 0 else "uptime_not_heartbeat",
        )
    return evidence


def _prom_rows(rows):
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _prom_query(query, timeout):
    base = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090").rstrip("/")
    url = base + "/api/v1/query?" + urlencode({"query": query})
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
    return payload.get("result")


def fetch_ap_online_evidence(aps, timeout=ONLINE_QUERY_TIMEOUT):
    """两次有界 Prometheus 查询（身份 + uptime）；失败全部降级 unknown。"""
    try:
        identities = _prom_query(ONLINE_INFO_QUERY, timeout)
        uptimes = _prom_query(ONLINE_UPTIME_QUERY, timeout)
    except Exception:
        return online_unavailable_evidence(aps, "prometheus_unavailable")
    return ap_online_evidence(aps, identities, uptimes)


def placement_coverage(fdb_status, librenms_ready, has_aps, *,
                       expected_switches=None, polled_switches=None,
                       poll_failed_switches=None):
    """预期/实际采集覆盖率；任何缺失或失败都不得返回 complete。"""
    coverage = {
        "status": "unknown",
        "expected_switches": None,
        "polled_switches": None,
        "missing_switches": 0,
        "poll_failed_switches": 0,
        "fdb_switches_usable": 0,
        "fdb_switches_failed": 0,
        "reasons": [],
    }
    if not has_aps:
        return coverage
    try:
        coverage["fdb_switches_usable"] = max(0, int((fdb_status or {}).get("usable_switches", 0)))
        coverage["fdb_switches_failed"] = max(0, int((fdb_status or {}).get("failed_switches", 0)))
        poll_failed = max(0, int(poll_failed_switches or 0))
        coverage["poll_failed_switches"] = poll_failed
        if expected_switches is not None:
            expected = max(0, int(expected_switches))
            polled = max(0, int(polled_switches or 0))
            coverage["expected_switches"] = expected
            coverage["polled_switches"] = polled
            coverage["missing_switches"] = max(0, expected - polled)
        else:
            polled = (
                coverage["fdb_switches_usable"] + coverage["fdb_switches_failed"]
            )
            coverage["polled_switches"] = polled
    except (TypeError, ValueError):
        return coverage

    usable = coverage["fdb_switches_usable"]
    failed = coverage["fdb_switches_failed"]
    missing = coverage["missing_switches"]
    poll_failed = coverage["poll_failed_switches"]
    if usable <= 0 or not librenms_ready:
        coverage["reasons"].append("no_usable_switch_collection")
        return coverage
    if coverage["expected_switches"] is not None and coverage["expected_switches"] <= 0:
        coverage["reasons"].append("no_expected_switches")
        return coverage
    if coverage["polled_switches"] is not None and usable < coverage["polled_switches"]:
        coverage["reasons"].append(
            f"fdb_unverified:{coverage['polled_switches'] - usable}")
    if missing > 0:
        coverage["reasons"].append(f"poll_missing:{missing}")
    if poll_failed > 0:
        coverage["reasons"].append(f"poll_failed:{poll_failed}")
    if failed > 0:
        coverage["reasons"].append(f"fdb_api_failed:{failed}")
    if missing > 0 or poll_failed > 0 or failed > 0 or (
            coverage["polled_switches"] is not None
            and usable < coverage["polled_switches"]):
        coverage["status"] = "partial"
    else:
        coverage["status"] = "complete"
    return coverage


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


def _last_observed(row):
    if not row:
        return None
    return {
        "switch_ip": str(row.get("switch_ip") or "") or None,
        "switch_ifindex": _as_positive_int(row.get("ifindex")),
        "switch_port": str(row.get("port_name") or "") or None,
        "vlan": _as_positive_int(row.get("vlan")),
        "evidence_age_seconds": _finite_age(row.get("evidence_age_seconds")),
    }


def _unresolved_reason(diagnostics, coverage_status):
    diagnostics = diagnostics or {}
    if diagnostics.get("lookup_errors"):
        return "exact_lookup_failed"
    if diagnostics.get("budget_skipped") or diagnostics.get("budget_exhausted"):
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


def _evidence(row, manual, diagnostics, online, coverage):
    diagnostics = diagnostics or {}
    fdb_status = "matched" if row else (
        "no_candidate" if int(diagnostics.get("raw_candidates") or 0) <= 0
        else "filtered_or_stale"
    )
    if row:
        exact_status = "matched"
    elif diagnostics.get("lookup_errors"):
        exact_status = "failed"
    elif diagnostics.get("budget_skipped") or diagnostics.get("budget_exhausted"):
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
            "lookup_attempts": max(0, int(diagnostics.get("lookup_attempts") or 0)),
            "gets": max(0, int(diagnostics.get("snmp_gets") or 0)),
        },
        "direct_neighbor": {
            "status": "operator-attested" if manual else "unknown",
            "source": "manual-allowlist" if manual else "none",
        },
        "manual_confirmation": {"status": "confirmed" if manual else "none"},
        "online": dict(online),
        "coverage": {
            "status": coverage["status"],
            "expected_switches": coverage.get("expected_switches"),
            "polled_switches": coverage.get("polled_switches"),
            "missing_switches": coverage.get("missing_switches", 0),
            "poll_failed_switches": coverage.get("poll_failed_switches", 0),
            "fdb_switches_usable": coverage.get("fdb_switches_usable", 0),
            "fdb_switches_failed": coverage.get("fdb_switches_failed", 0),
            "reasons": list(coverage.get("reasons") or []),
        },
        "port": {"verified": bool(row)},
    }


def build_placement_artifact(
    aps,
    validated,
    shadow=None,
    online_evidence=None,
    *,
    fdb_status=None,
    librenms_ready=True,
    max_age_seconds=7200,
    manual_core_ports=(),
    core_ips=(),
    expected_switches=None,
    polled_switches=None,
    poll_failed_switches=None,
    now=None,
):
    """生成影子产物；纯函数，不写文件、不发请求。

    observed-attachment 只声明“当前已验证的转发出口（egress toward AP）”，
    不承诺物理直连。confirmed-direct 仅接受人工白名单核心口的
    operator-attested 证据，且要求 AP 当前 online；P1 没有经过验证的
    controller uplink/LLDP。离线 AP 不产生当前定位，只保留 last_observed。
    """
    if not isinstance(aps, list):
        raise ValueError("AP list required")
    if len(aps) > MAX_PLACEMENTS:
        raise ValueError("AP placement limit")
    online_evidence = online_evidence or {}
    manual_core_ports = set(manual_core_ports or ())
    core_ips = set(core_ips or ())
    coverage = placement_coverage(
        fdb_status, librenms_ready, bool(aps),
        expected_switches=expected_switches,
        polled_switches=polled_switches,
        poll_failed_switches=poll_failed_switches,
    )
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
        online = online_evidence.get(mac) or _online_default(
            "unavailable", "online_evidence_unavailable")
        if not isinstance(online, dict) or online.get("status") not in ONLINE_STATES:
            online = _online_default("unavailable", "invalid_online_evidence")
        current = None
        evidence_row = None
        manual = False
        last_observed = None
        if len(rows) > 1:
            level = LEVEL_AMBIGUOUS
            reason = "multiple_verified_candidates"
        elif len(rows) == 1:
            row = rows[0][1]
            evidence_row = row
            if online["status"] == "offline":
                level = LEVEL_UNRESOLVED
                reason = "ap_offline"
                last_observed = _last_observed(row)
            elif online["status"] == "unknown":
                # 未知在线状态不能当作 online：保留转发证据但最多 observed。
                level = LEVEL_OBSERVED_ATTACHMENT
                reason = "online_unknown_observed"
                current = row
            else:
                manual = _manual_core_confirmation(row, core_ips, manual_core_ports)
                level = LEVEL_CONFIRMED_DIRECT if manual else LEVEL_OBSERVED_ATTACHMENT
                reason = "operator_confirmed_core_port" if manual else "verified_fdb_egress"
                current = row
        else:
            level = LEVEL_UNRESOLVED
            reason = "ap_offline" if online["status"] == "offline" else _unresolved_reason(
                diagnostics, coverage["status"])

        online_state = online["status"]
        counts["online" if online_state == "online" else
               "offline" if online_state == "offline" else "unknown"] += 1
        counts[{
            LEVEL_CONFIRMED_DIRECT: "confirmed_direct",
            LEVEL_OBSERVED_ATTACHMENT: "observed_attachment",
            LEVEL_AMBIGUOUS: "ambiguous",
            LEVEL_UNRESOLVED: "unresolved",
        }[level]] += 1
        counts["total"] += 1

        evidence = _evidence(evidence_row, manual, diagnostics, online, coverage)
        placements.append({
            "ap_ip": str(ap.get("ip") or ""),
            "ap_name": str(ap.get("name") or ""),
            "ap_mac": mac,
            "site": str(ap.get("site") or ""),
            "online": online_state,
            "level": level,
            "switch_ip": str(current.get("switch_ip") or "") if current else None,
            "switch_ifindex": _as_positive_int(current.get("ifindex")) if current else None,
            "switch_port": str(current.get("port_name") or "") if current else None,
            "vlan": _as_positive_int(current.get("vlan")) if current else None,
            "evidence_age_seconds": _finite_age(current.get("evidence_age_seconds")) if current else None,
            "last_observed": last_observed,
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
