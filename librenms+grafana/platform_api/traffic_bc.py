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
MAX_EXACT_COUNTER = (1 << 53) - 1
DEFAULT_FIREWALL_SCRAPE_INTERVAL = "10s"
_DURATION_UNITS_MS = (("y", 31_536_000_000), ("w", 604_800_000), ("d", 86_400_000),
                      ("h", 3_600_000), ("m", 60_000), ("s", 1000), ("ms", 1))
_DURATION_PATTERN = re.compile("".join(f"(?:([0-9]+){suffix})?" for suffix, _ in _DURATION_UNITS_MS))
JOBS = {"remote_ob": ("firewall-snmp", "instance", 40), "business_vlan": ("infra-switch-ifmib", "target_ip", 95)}


class TrafficError(ValueError):
    pass


def configuration(config):
    section = config.get("traffic", {})
    if not isinstance(section, dict):
        raise TrafficError("traffic must be an object")
    output = {}
    for domain, key in (("remote_ob", "tunnels"), ("business_vlan", "business_vlans")):
        items = section.get(key, [])
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
                if not re.fullmatch(r"[0-9]{1,4}", str(vlan)) or not 1 <= int(vlan) <= 4094:
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


def _pattern(values, *, ignore_case=False):
    pattern = "|".join(re.escape(x) for x in sorted(set(values)))
    return json.dumps(f"(?i:{pattern})" if ignore_case else pattern, ensure_ascii=True)


def _scrape_interval_ms(interval: str) -> int:
    match = _DURATION_PATTERN.fullmatch(interval) if len(interval) <= 40 else None
    if match:
        value = sum(int(part) * scale for part, (_, scale) in zip(match.groups(), _DURATION_UNITS_MS) if part)
        if 0 < value <= (1 << 63) // 1_000_000:
            return value
    raise TrafficError("invalid firewall scrape interval")


def build_query(mapping, firewall_scrape_interval: str = DEFAULT_FIREWALL_SCRAPE_INTERVAL):
    """PromQL query_range: two jobs, one HTTP request, no rate() reset healing."""
    chunks = []
    for domain, rows in mapping.items():
        if not rows:
            continue
        job, label, _ = JOBS[domain]
        by_target = {}
        for row in rows:
            ifnames = by_target.setdefault(row["target"], [])
            if domain == "business_vlan":
                number = row["interface"][4:]
                ifnames.extend(("Vl" + number, "Vlan" + number))
            else:
                ifnames.append(row["interface"])
        base = f'job="{job}"'
        selectors = []
        for target, ifnames in sorted(by_target.items()):
            names = _pattern(ifnames, ignore_case=True)
            target_scope = f'{base},{label}="{target}"'
            selectors.extend((f'{{{target_scope},ifName=~{names}}}',
                              f'{{{target_scope},ifName="",ifDescr=~{names}}}'))
        for field, metric in (("in", "ifHCInOctets"), ("out", "ifHCOutOctets"),
                              ("oper", "ifOperStatus"), ("disc", "ifCounterDiscontinuityTime")):
            raw = [metric + selector for selector in selectors]
            # PromQL timestamps computed vectors at the evaluation step. Apply
            # timestamp() directly to each selector to retain the scrape time.
            values = raw
            if field in ("in", "out"):
                # Cover both raw samples plus intermediate scrapes even if a
                # counter recovers above its previous 30-second endpoint.
                values = [f'({s} and (resets({s}[66s]) == 0))' for s in raw]
                if domain == "remote_ob":
                    interval_ms = _scrape_interval_ms(firewall_scrape_interval)
                    # The step window contains the observations between the
                    # latest scrapes at its endpoints. Their actual timestamps
                    # determine the expected count even when cadence and step differ.
                    values = [
                        f'({checked} and (count_over_time({s}[{STEP}s]) == round('
                        f'(timestamp({s}) - timestamp({s} offset {STEP}s)) * 1000 / {interval_ms})))'
                        for s, checked in zip(raw, values)
                    ]
            values = " or ".join(values)
            timestamps = " or ".join(f"timestamp({selector})" for selector in raw)
            for suffix, source in ((field, values), (field + "_at", timestamps)):
                chunks.append(f'label_replace(({source}),"bc_field","{suffix}","job",".+")')
        up = f'up{{{base},{label}=~{_pattern(by_target)}}}'
        for suffix, source in (("up", up), ("up_at", f"timestamp({up})")):
            chunks.append(f'label_replace({source},"bc_field","{suffix}","job",".+")')
    return " or ".join(chunks)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_range(url, query, end, timeout=7):
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise TrafficError("invalid internal Prometheus URL") from exc
    if (parsed.scheme != "http" or parsed.hostname != "prometheus" or
            port != 9090 or parsed.path not in ("", "/") or
            parsed.username or parsed.password or parsed.query or parsed.fragment):
        # No userinfo, external hosts, redirect targets, arbitrary paths or proxy.
        raise TrafficError("invalid internal Prometheus URL")
    return _query_range(url, query, end, timeout)


def _query_range(url, query, end, timeout):
    params = urlencode({"query": query, "start": end - WINDOW, "end": end,
                        "step": STEP, "lookback_delta": "35s", "timeout": "5s"})
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
    if not isinstance(payload, dict) or payload.get("status") != "success" or payload.get("warnings"):
        raise TrafficError("Prometheus query not complete")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise TrafficError("Prometheus response data malformed")
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
    # When ifName is available, it is authoritative: an old matching ifDescr
    # cannot silently keep a renamed/deleted Tunnel or SVI identity alive.
    return name.casefold() in choices if name else descr.casefold() in choices


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
               disc, disc_at, now, end, max_age, require_disc):
    counter, counter_at = counter or {}, counter_at or {}
    times = sorted(counter.keys() | counter_at.keys())
    points = []
    previous = None
    for t in times:
        if t not in counter or t not in counter_at:
            points.append({"t": int(t), "bps": None})
            previous = None
            continue
        sample_at = counter_at[t]
        active = (oper or {}).get(t) == 1 and (up or {}).get(t) == 1
        active = active and (oper_at or {}).get(t) == (up_at or {}).get(t) == sample_at
        value = counter[t]
        active = active and 0 <= t - sample_at <= min(STEP + 5, max_age)
        # Prometheus stores float64. Above 2^53 - 1 a one-byte change can
        # disappear before JSON encoding, so it cannot establish a true zero.
        active = active and 0 <= value <= MAX_EXACT_COUNTER and value == value.to_integral_value()
        if require_disc and (disc is None or disc_at is None):
            active = False
        if disc is not None:
            active = active and t in disc and (disc_at or {}).get(t) == sample_at
        rate = None
        if active and previous is not None:
            old_step, old_at, old_value, old_disc = previous
            elapsed = sample_at - old_at
            if (t - old_step == STEP and 0 < elapsed <= min(max_age, STEP * 1.5)
                    and value >= old_value and (disc is None or disc[t] == old_disc)):
                rate = float((value - old_value) * 8 / elapsed)
                if not (rate >= 0 and rate < 1e13):
                    rate = None
        points.append({"t": int(t), "bps": rate})
        if active:
            previous = (t, sample_at, value, disc[t] if disc is not None else None)
        else:
            previous = None
    last = points[-1] if points else None
    valid = bool(last and last["t"] == end and last["bps"] is not None and
                 now - last["t"] <= max_age and
                 0 <= now - float(counter_at[Decimal(last["t"])]) <= max_age)
    return {"bps": last["bps"] if valid else None, "points": points,
            "sample_at": float(counter_at[Decimal(last["t"])]) if valid else None,
            "validity": "VALID" if valid else "UNKNOWN"}


def project(mapping, series, now, end=None):
    if end is None:
        end = int(now // STEP) * STEP
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
            valid_identity = len(indexes) == 1 and all(
                re.fullmatch(r"[1-9][0-9]{0,9}", i) and int(i) <= 2**31 - 1 for i in indexes)
            port = [s for s in ports if s["metric"].get("ifIndex") in indexes] if valid_identity else []
            fields = {k: _timeseries(port, k) for k in
                      ("in", "in_at", "out", "out_at", "oper", "oper_at", "disc", "disc_at")}
            check_disc = domain == "business_vlan" or any(
                s["metric"].get("bc_field") in ("disc", "disc_at") for s in port)
            up = _timeseries(up_rows, "up")
            up_at = _timeseries(up_rows, "up_at")
            def output(direction):
                if not valid_identity:
                    return {"bps": None, "points": [], "sample_at": None, "validity": "UNKNOWN"}
                return _direction(fields[direction], fields[direction + "_at"],
                                  fields["oper"], fields["oper_at"], up, up_at,
                                  fields["disc"], fields["disc_at"], now, end, max_age,
                                  check_disc)
            receive, transmit = output("in"), output("out")
            oper = fields["oper"] or {}
            status = "UNKNOWN"
            interface_sample_at = None
            oper_at = fields["oper_at"] or {}
            if valid_identity and oper and oper_at and up and up_at:
                common = sorted(set(oper) & set(oper_at) & set(up) & set(up_at))
                if common:
                    t = common[-1]
                    sample_at = oper_at[t]
                    if (t == end and 0 <= now - float(t) <= max_age and
                            oper_at[t] == up_at[t] == sample_at and
                            0 <= now - float(sample_at) <= max_age and up[t] == 1):
                        if oper[t] == 1:
                            status = "UP"
                        elif oper[t] in (2, 6, 7):
                            status = "DOWN"
                        if status != "UNKNOWN":
                            interface_sample_at = float(sample_at)
            cards.append({"name": row["name"],
                          "interface": row["interface"],
                          "interface_status": status,
                          "interface_sample_at": interface_sample_at,
                          "sa_status": "UNKNOWN" if domain == "remote_ob" else None,
                          "egress_isp": "UNKNOWN" if domain == "remote_ob" else None,
                          "configured_isp": row["configured_isp"] if domain == "remote_ob" else None,
                          "traffic_validity": "VALID" if receive["validity"] == transmit["validity"] == "VALID" else "UNKNOWN",
                          "rx": receive, "tx": transmit,
                          "reason": "" if valid_identity else "接口未找到、同名或 ifIndex 变化/歧义",
                          "unit": "SVI 三层网关接口" if domain == "business_vlan" else "Tunnel 接口",
                          "source": job})
        domains[domain] = {"state": "ready" if rows else "unconfigured",
                           "max_age_seconds": max_age, "items": cards}
    return {"ok": True, "generated_at": int(now), "served_at": now, "interval_seconds": TTL,
            "window_seconds": WINDOW, "domains": domains,
            "note": "RX/TX 为设备接口方向；不代表互联网出口或完整二层业务流量"}


def _serve_cached(payload, now):
    if not payload["ok"]:
        return payload

    domains = {}
    for domain, snapshot in payload["domains"].items():
        max_age = snapshot["max_age_seconds"]
        cards = []
        for card in snapshot["items"]:
            card = dict(card)
            for direction in ("rx", "tx"):
                sample = card[direction]
                at = sample["sample_at"]
                if at is not None and not 0 <= now - at <= max_age:
                    card[direction] = {**sample, "bps": None, "sample_at": None, "validity": "UNKNOWN"}
                    card["traffic_validity"] = "UNKNOWN"
            at = card["interface_sample_at"]
            if at is not None and not 0 <= now - at <= max_age:
                card.update(interface_status="UNKNOWN", interface_sample_at=None)
            cards.append(card)
        domains[domain] = {**snapshot, "items": cards}
    return {**payload, "served_at": now, "domains": domains}


@dataclass
class SharedSampler:
    """Single process-wide, single in-flight 30s cache shared by all viewers."""
    url: str = "http://prometheus:9090"
    clock: object = time.time
    monotonic: object = time.monotonic
    fetch: object = fetch_range
    firewall_scrape_interval: str = DEFAULT_FIREWALL_SCRAPE_INTERVAL

    def __post_init__(self):
        self.lock = threading.Lock()
        self.cached = None

    def snapshot(self, config):
        try:
            mapping = configuration(config)
            key = json.dumps(mapping, sort_keys=True, ensure_ascii=True)
        except (TrafficError, TypeError) as exc:
            return {"ok": False, "state": "invalid_configuration", "error": str(exc)}
        if not any(mapping.values()):
            return project(mapping, [], self.clock())
        now = self.monotonic()
        cached = self.cached
        if cached and cached[0] == key and now - cached[1] < TTL:
            return _serve_cached(cached[2], self.clock())
        if not self.lock.acquire(blocking=False):
            # The previous payload can have expired or belong to another event.
            return {"ok": False, "state": "refreshing", "error": "采集进行中"}
        try:
            now = self.monotonic()
            cached = self.cached
            if cached and cached[0] == key and now - cached[1] < TTL:
                return _serve_cached(cached[2], self.clock())
            at = int(self.clock() // STEP) * STEP
            stage = "query"
            try:
                query = build_query(mapping, self.firewall_scrape_interval)
                stage = "fetch"
                series = self.fetch(self.url, query, at)
                stage = "project"
                payload = project(mapping, series, self.clock(), end=at)
            except (OSError, ValueError, TimeoutError, TypeError, KeyError, OverflowError) as exc:
                # Report safe failure classification only; never include credentials,
                # device IPs, raw PromQL or exception text in public responses.
                payload = {"ok": False, "state": "error", "error": "Prometheus B/C 数据不可用",
                           "stage": stage, "error_kind": type(exc).__name__,
                           "generated_at": int(self.clock())}
            self.cached = (key, now, payload)
            return payload
        finally:
            self.lock.release()
