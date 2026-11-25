"""#104 B/C: bounded, shared IF-MIB counter read model (not a collector).

One on-demand Prometheus range request at most every 30s for *all* configured
remote tunnels and business SVIs.  This deliberately does not infer SA, ISP
egress or Internet/VLAN L2 throughput from interface counters.
"""
from __future__ import annotations

import ipaddress
import json
import re
import threading
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MAX_ITEMS = 8
MAX_SERIES = 160
MAX_BYTES = 1024 * 1024
STEP = 30
WINDOW = 900
TTL = 30
MAX_NAME = 96
JOBS = {"remote_ob": ("firewall-snmp", "instance", 40), "business_vlan": ("infra-switch-ifmib", "target_ip", 95)}


class TrafficError(ValueError):
    pass


def configuration(config):
    section = config.get("traffic") or {}
    if not isinstance(section, dict):
        raise TrafficError("traffic must be an object")
    output = {}
    for domain, key in (("remote_ob", "tunnels"), ("business_vlan", "business_vlans")):
        items = section.get(key) or []
        if not isinstance(items, list) or len(items) > MAX_ITEMS:
            raise TrafficError(key + " must be a list of at most 8")
        parsed = []
        seen = set()
        for item in items:
            if not isinstance(item, dict):
                raise TrafficError("traffic item must be an object")
            name = str(item.get("name") or "").strip()
            target = str(item.get("instance" if domain == "remote_ob" else "target_ip") or "").strip()
            if not name or len(name) > MAX_NAME or any(ord(c) < 32 for c in name):
                raise TrafficError("invalid traffic display name")
            try:
                ipaddress.IPv4Address(target)
            except ValueError as exc:
                raise TrafficError("traffic target must be IPv4") from exc
            if domain == "remote_ob":
                interface = str(item.get("if_name") or "").strip()
                if not re.fullmatch(r"[A-Za-z][A-Za-z0-9._/-]{0,62}", interface):
                    raise TrafficError("invalid tunnel interface name")
                if item.get("egress_isp"):
                    # A configured ISP is a label, never a measured runtime egress.
                    label = str(item["egress_isp"]).strip()[:MAX_NAME]
                else:
                    label = None
            else:
                vlan = item.get("vlan")
                if isinstance(vlan, bool) or not str(vlan).isdigit() or not 1 <= int(vlan) <= 4094:
                    raise TrafficError("invalid business VLAN ID")
                interface = "Vlan" + str(int(vlan))
                label = None
            identity = (target, interface.casefold())
            if identity in seen:
                raise TrafficError("duplicate traffic interface mapping")
            seen.add(identity)
            parsed.append({"name": name, "target": target, "interface": interface, "configured_isp": label})
        output[domain] = parsed
    return output


def _pattern(values):
    return json.dumps("|".join(re.escape(x) for x in sorted(set(values))), ensure_ascii=True)


def build_query(mapping):
    """PromQL query_range: two jobs, one HTTP request, no rate() reset healing."""
    chunks = []
    for domain, rows in mapping.items():
        if not rows:
            continue
        job, label, _ = JOBS[domain]
        instances = _pattern(r["target"] for r in rows)
        ifnames = []
        for row in rows:
            if domain == "business_vlan":
                number = row["interface"][4:]
                ifnames.extend(("Vl" + number, "Vlan" + number))
            else:
                ifnames.append(row["interface"])
        names = _pattern(ifnames)
        base = f'job="{job}",{label}=~{instances}'
        for field, metric in (("in", "ifHCInOctets"), ("out", "ifHCOutOctets"),
                              ("oper", "ifOperStatus"), ("disc", "ifCounterDiscontinuityTime")):
            expr = f'({metric}{{{base},ifName=~{names}}} or {metric}{{{base},ifDescr=~{names}}})'
            for suffix, source in ((field, expr), (field + "_at", f"timestamp({expr})")):
                chunks.append(f'label_replace({source},"bc_field","{suffix}","job",".+")')
        for suffix, source in (("up", f"up{{{base}}}"), ("up_at", f"timestamp(up{{{base}}})")):
            chunks.append(f'label_replace({source},"bc_field","{suffix}","job",".+")')
    return " or ".join(chunks)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_range(url, query, end, timeout=7):
    parsed = urlsplit(url)
    if (parsed.scheme != "http" or parsed.hostname != "prometheus" or
            parsed.port != 9090 or parsed.path not in ("", "/") or
            parsed.username or parsed.password or parsed.query or parsed.fragment):
        # No userinfo, external hosts, redirect targets, arbitrary paths or proxy.
        raise TrafficError("invalid internal Prometheus URL")
    params = urlencode({"query": query, "start": end - WINDOW, "end": end,
                        "step": STEP, "timeout": "5s"})
    req = Request(url.rstrip("/") + "/api/v1/query_range?" + params,
                  headers={"Accept": "application/json"}, method="GET")
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    with opener.open(req, timeout=timeout) as response:
        if response.status != 200:
            raise TrafficError("Prometheus request failed")
        raw = response.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise TrafficError("Prometheus response too large")
    payload = json.loads(raw)
    if payload.get("status") != "success" or payload.get("warnings"):
        raise TrafficError("Prometheus query not complete")
    data = payload.get("data") or {}
    rows = data.get("result")
    if data.get("resultType") != "matrix" or not isinstance(rows, list) or len(rows) > MAX_SERIES:
        raise TrafficError("Prometheus series malformed or excessive")
    return rows


def _number(value):
    try:
        n = Decimal(str(value))
        return n if n.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _identity(metric, domain):
    job, label, _ = JOBS[domain]
    if metric.get("job") != job:
        return None
    return str(metric.get(label) or "")


def _matching(metric, row, domain):
    name = str(metric.get("ifName") or "")
    descr = str(metric.get("ifDescr") or "")
    expected = row["interface"].casefold()
    if domain == "business_vlan":
        number = expected[4:]
        choices = {"vl" + number, "vlan" + number}
    else:
        choices = {expected}
    return name.casefold() in choices or descr.casefold() in choices


def _timeseries(series, field):
    # Duplicate labels/series must never be silently selected or summed.
    found = [s for s in series if s["metric"].get("bc_field") == field]
    if len(found) != 1:
        return None
    points = {}
    for sample in found[0].get("values") or []:
        if len(sample) != 2:
            return None
        at, val = _number(sample[0]), _number(sample[1])
        if at is None or val is None or at in points:
            return None
        points[at] = val
    return points


def _direction(counter, counter_at, oper, oper_at, up, up_at,
               disc, disc_at, now, max_age, require_disc):
    times = sorted(set(counter or {}) & set(counter_at or {}))
    points = []
    previous = None
    for t in times:
        sample_at = counter_at[t]
        active = (oper or {}).get(t) == 1 and (up or {}).get(t) == 1
        active = active and (oper_at or {}).get(t) == (up_at or {}).get(t) == sample_at
        active = active and 0 <= t - sample_at <= max_age
        if require_disc and (disc is None or disc_at is None):
            active = False
        if disc is not None:
            active = active and t in disc and (disc_at or {}).get(t) == sample_at
        rate = None
        if active and previous is not None:
            old_at, old_value, old_disc = previous
            elapsed = sample_at - old_at
            if (elapsed > 0 and elapsed <= max_age and counter[t] >= old_value
                    and (disc is None or disc[t] == old_disc)
                    and counter[t] == counter[t].to_integral_value()
                    and old_value == old_value.to_integral_value()):
                rate = float((counter[t] - old_value) * 8 / elapsed)
                if not (rate >= 0 and rate < 1e13):
                    rate = None
        points.append({"t": int(t), "bps": rate})
        previous = (sample_at, counter[t], disc[t] if disc is not None and t in disc else None) if active else None
    last = points[-1] if points else None
    valid = bool(last and last["bps"] is not None and
                 now - last["t"] <= max_age and
                 now - counter_at[Decimal(last["t"])] <= max_age)
    return {"bps": last["bps"] if valid else None, "points": points,
            "sample_at": float(counter_at[Decimal(last["t"])]) if valid else None,
            "validity": "VALID" if valid else "UNKNOWN"}


def project(mapping, series, now):
    domains = {}
    for domain, rows in mapping.items():
        job, label, max_age = JOBS[domain]
        cards = []
        for row in rows:
            matches = [s for s in series if _identity(s.get("metric") or {}, domain) == row["target"]]
            up_rows = [s for s in matches if (s.get("metric") or {}).get("bc_field") in ("up", "up_at")]
            ports = [s for s in matches if (s.get("metric") or {}).get("bc_field") not in ("up", "up_at")
                     and _matching(s.get("metric") or {}, row, domain)]
            indexes = {str((s.get("metric") or {}).get("ifIndex") or "") for s in ports}
            indexes.discard("")
            valid_identity = len(indexes) == 1 and all(
                re.fullmatch(r"[1-9][0-9]*", i) for i in indexes)
            port = [s for s in ports if s["metric"].get("ifIndex") in indexes] if valid_identity else []
            fields = {k: _timeseries(port, k) for k in
                      ("in", "in_at", "out", "out_at", "oper", "oper_at", "disc", "disc_at")}
            up = _timeseries(up_rows, "up")
            up_at = _timeseries(up_rows, "up_at")
            def output(direction):
                if not valid_identity:
                    return {"bps": None, "points": [], "sample_at": None, "validity": "UNKNOWN"}
                return _direction(fields[direction], fields[direction + "_at"],
                                  fields["oper"], fields["oper_at"], up, up_at,
                                  fields["disc"], fields["disc_at"], now, max_age,
                                  domain == "business_vlan")
            receive, transmit = output("in"), output("out")
            oper = fields["oper"] or {}
            status = "UNKNOWN"
            if valid_identity and oper and up and up_at:
                common = sorted(set(oper) & set(up) & set(up_at))
                if common and now - float(common[-1]) <= max_age:
                    status = "UP" if oper[common[-1]] == 1 and up[common[-1]] == 1 else "DOWN"
            cards.append({"name": row["name"],
                          "interface": row["interface"],
                          "interface_status": status,
                          "sa_status": "UNKNOWN" if domain == "remote_ob" else None,
                          "egress_isp": "UNKNOWN" if domain == "remote_ob" else None,
                          "configured_isp": row["configured_isp"] if domain == "remote_ob" else None,
                          "traffic_validity": "VALID" if receive["validity"] == transmit["validity"] == "VALID" else "UNKNOWN",
                          "rx": receive, "tx": transmit,
                          "reason": "" if valid_identity else "接口未找到、同名或 ifIndex 变化/歧义",
                          "unit": "SVI 三层网关接口" if domain == "business_vlan" else "Tunnel 接口",
                          "source": job})
        domains[domain] = {"state": "ready" if rows else "unconfigured", "items": cards}
    return {"ok": True, "generated_at": int(now), "interval_seconds": TTL,
            "window_seconds": WINDOW, "domains": domains,
            "note": "RX/TX 为设备接口方向；不代表互联网出口或完整二层业务流量"}


@dataclass
class SharedSampler:
    """Single process-wide, single in-flight 30s cache shared by all viewers."""
    url: str = "http://prometheus:9090"
    clock: object = time.time
    monotonic: object = time.monotonic
    fetch: object = fetch_range

    def __post_init__(self):
        self.lock = threading.Lock()
        self.cached = None
        self.cache_key = None
        self.at = float("-inf")

    def snapshot(self, config):
        try:
            mapping = configuration(config)
            key = json.dumps(mapping, sort_keys=True, ensure_ascii=True)
        except (TrafficError, TypeError) as exc:
            return {"ok": False, "state": "invalid_configuration", "error": str(exc)}
        if not any(mapping.values()):
            return project(mapping, [], self.clock())
        now = self.monotonic()
        if self.cache_key == key and self.cached is not None and now - self.at < TTL:
            return self.cached
        if not self.lock.acquire(blocking=False):
            # Never queue a request storm; don't leak a former event's cache.
            if self.cache_key == key and self.cached is not None:
                return {**self.cached, "state": "refreshing"}
            return {"ok": False, "state": "refreshing", "error": "采集进行中"}
        try:
            now = self.monotonic()
            if self.cache_key == key and self.cached is not None and now - self.at < TTL:
                return self.cached
            at = int(self.clock() // STEP) * STEP
            try:
                series = self.fetch(self.url, build_query(mapping), at)
                payload = project(mapping, series, self.clock())
            except (OSError, ValueError, TimeoutError, TypeError, KeyError) as exc:
                payload = {"ok": False, "state": "error", "error": "Prometheus B/C 数据不可用",
                           "generated_at": int(self.clock())}
            self.cache_key, self.cached, self.at = key, payload, now
            return payload
        finally:
            self.lock.release()
