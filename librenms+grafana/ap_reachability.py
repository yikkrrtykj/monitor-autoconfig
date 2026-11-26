"""Private v2 AP reachability evidence from the existing ICMP scrape job."""
from __future__ import annotations

import json
import math
import os
import time
from collections import Counter, defaultdict
from urllib.parse import urlencode
from urllib.request import urlopen

from target_utils import write_json_atomic

SCHEMA_VERSION = 2
SOURCE = "prometheus:infra-ap-ping"
MAX_AGE_SECONDS = 20
MAX_RESPONSE_BYTES = 1024 * 1024
QUERY_TIMEOUT = 2
QUERY = (
    '{__name__=~"probe_success|up|probe_icmp_duration_seconds",'
    f'job="infra-ap-ping"}}[{MAX_AGE_SECONDS}s]'
)


def latest_sample(series, published_at, now):
    values = series.get("values")
    if not isinstance(values, list) or not values:
        return None
    point = values[-1]
    if not isinstance(point, list) or len(point) != 2 or any(isinstance(item, bool) for item in point):
        return None
    try:
        stamp, value = map(float, point)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(value) or not math.isfinite(stamp):
        return None
    if max(published_at, now - MAX_AGE_SECONDS) <= stamp <= now:
        return stamp, value
    return None


def reachability_for_ip(series, published_at, now, fallback):
    sample = {"status": "unknown", "source": SOURCE, "reason": fallback,
              "sample_at": None, "valid_until": None, "rtt_ms": None}
    probe, scrape = series.get("probe_success", []), series.get("up", [])
    if len(probe) > 1 or len(scrape) > 1:
        return {**sample, "reason": "duplicate_series"}
    if not (probe and scrape):
        return sample
    probe_sample = latest_sample(probe[0], published_at, now)
    scrape_sample = latest_sample(scrape[0], published_at, now)
    if not probe_sample or not scrape_sample:
        return {**sample, "reason": "sample_stale_or_before_target_publication"}
    stamp, success = probe_sample
    if scrape_sample != (stamp, 1.0) or success not in (0.0, 1.0):
        return {**sample, "reason": "scrape_unavailable_or_sample_mismatch"}

    rtt_ms = None
    rtt = series.get("probe_icmp_duration_seconds", [])
    if success == 1 and len(rtt) == 1:
        rtt_sample = latest_sample(rtt[0], published_at, now)
        if rtt_sample and rtt_sample[0] == stamp and 0 <= rtt_sample[1] <= MAX_AGE_SECONDS:
            rtt_ms = rtt_sample[1] * 1000
    return {
        **sample,
        "status": "reachable" if success == 1 else "unreachable",
        "reason": "icmp_reply" if success == 1 else "icmp_probe_failed",
        "sample_at": stamp,
        "valid_until": stamp + MAX_AGE_SECONDS,
        "rtt_ms": rtt_ms,
    }


def build_artifact(aps, online_evidence, series, *, published_at, now, reason=None):
    ip_counts = Counter(ap["ip"] for ap in aps)
    mac_counts = Counter(ap["mac"] for ap in aps)
    by_ip = defaultdict(lambda: defaultdict(list))
    for row in series if reason is None else ():
        metric = row.get("metric") if isinstance(row, dict) else None
        if not isinstance(metric, dict) or metric.get("job") != "infra-ap-ping":
            continue
        ip, series_name = metric.get("target_ip"), metric.get("__name__")
        if not isinstance(ip, str) or ip not in ip_counts or metric.get("instance") != ip:
            continue
        if series_name == "probe_icmp_duration_seconds" and metric.get("phase") != "rtt":
            continue
        if series_name in ("probe_success", "up", "probe_icmp_duration_seconds"):
            by_ip[ip][series_name].append(row)

    rows = []
    for ap in aps:
        ip, mac = ap["ip"], ap["mac"]
        if ip_counts[ip] != 1 or mac_counts[mac] != 1:
            continue
        online = online_evidence.get(mac, {"status": "unknown", "source": "unavailable"})
        reachable = reachability_for_ip(
            by_ip[ip], published_at, now, reason or "sample_missing",
        )
        conflict = (online.get("status"), reachable["status"]) in {
            ("online", "unreachable"), ("offline", "reachable"),
        }
        rows.append({"ap_mac": mac, "ap_ip": ip, "online": online, "reachability": reachable,
                     "conflict": "online_and_icmp_disagree" if conflict else None})
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now,
        "targets_published_at": published_at,
        "valid": reason is None,
        "invalid_reason": reason,
        "aps": rows,
    }


def fetch_probe_series():
    base = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090").rstrip("/")
    with urlopen(base + "/api/v1/query?" + urlencode({"query": QUERY}),
                 timeout=QUERY_TIMEOUT) as response:
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("AP reachability response limit")
    reply = json.loads(body)
    data = reply.get("data", {})
    rows = data.get("result")
    if (reply.get("status") != "success" or data.get("resultType") != "matrix"
            or not isinstance(rows, list)):
        raise ValueError("AP reachability query unavailable")
    return rows


def write_shadow(path, aps, online_evidence, published_at):
    reason = None
    series = []
    if published_at is None:
        reason = "ping_targets_unavailable"
    elif not aps:
        reason = "ap_identity_unavailable"
    else:
        try:
            series = fetch_probe_series()
        except (OSError, ValueError, TypeError, AttributeError):
            reason = "prometheus_unavailable"
    artifact = build_artifact(aps, online_evidence, series, published_at=published_at,
                              now=time.time(), reason=reason)
    write_json_atomic(path, artifact, sort_keys=True)
