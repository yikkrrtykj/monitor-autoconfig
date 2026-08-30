"""ISP WAN bandwidth watcher extracted from the Feishu alert bridge.

The bridge continues to own environment parsing, Prometheus transport, card
presentation, Feishu delivery, watcher supervision, and shared health state.
"""

import re
import time


def _parse_bandwidth_config(raw, normalize_label):
    raw = str(raw or "").strip()
    cfg = {"default": None, "per": []}
    if not raw:
        return cfg
    try:
        mbps = float(raw)
        cfg["default"] = {"down": mbps, "up": mbps}
        return cfg
    except ValueError:
        pass

    for item in raw.split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        label, bandwidth = [part.strip() for part in item.split(":", 1)]
        parts = [part.strip() for part in bandwidth.split("/", 1)]
        try:
            down = float(parts[0])
        except (TypeError, ValueError):
            continue
        try:
            up = float(parts[1]) if len(parts) > 1 else down
        except (TypeError, ValueError):
            up = down
        if label == "*":
            cfg["default"] = {"down": down, "up": up}
            continue
        cfg["per"].append({
            "label": label.lower(),
            "norm": normalize_label(label),
            "down": down,
            "up": up,
        })
    return cfg


def _wan_keywords(wan_filter):
    return [part.strip().lower() for part in wan_filter.split(",") if part.strip()]


def _wan_label(metric):
    return (metric.get("ifAlias") or metric.get("ifName") or metric.get("ifDescr") or "").strip()


def _is_wan_port(label, wan_filter):
    # A digit-ending keyword such as eth1 must not also match eth10..eth15.
    lower = label.lower()
    for keyword in _wan_keywords(wan_filter):
        if keyword[-1:].isdigit():
            if re.search(re.escape(keyword) + r"(?:\D|$)", lower):
                return True
        elif keyword in lower:
            return True
    return False


def _bandwidth_for_label(label, direction, cfg, normalize_label, index=None):
    lower = label.lower()
    norm = normalize_label(label)
    # Prefer the most specific label. 电信2 must beat the earlier 电信 fallback.
    best = None
    for entry in cfg["per"]:
        if (entry["label"] and entry["label"] in lower) or (entry["norm"] and entry["norm"] in norm):
            if best is None or len(entry["norm"]) > len(best["norm"]):
                best = entry
    if best is not None:
        return best["down"] if direction == "in" else best["up"]
    if isinstance(index, int) and 0 <= index < len(cfg["per"]):
        entry = cfg["per"][index]
        return entry["down"] if direction == "in" else entry["up"]
    default = cfg["default"] or {"down": 1000.0, "up": 1000.0}
    return default["down"] if direction == "in" else default["up"]


def _counter_glitch_limit_bps(capacity_mbps, factor):
    """Bps ceiling above which a rate sample is a counter glitch, or None if off."""
    if not factor or factor <= 0:
        return None
    try:
        capacity = float(capacity_mbps)
    except (TypeError, ValueError):
        return None
    if capacity <= 0:
        return None
    return capacity * 1000000 * factor


def _dedupe_wan_labels(results):
    """Suffix duplicate WAN names by ifIndex, with stable directional fallback."""
    occ = {}
    for sample in results:
        try:
            ifi = int(sample.get("if_index"))
        except (TypeError, ValueError):
            ifi = None
        dir_key = (sample["label"], sample["direction"])
        seq = occ.get(dir_key, 0)
        occ[dir_key] = seq + 1
        sample["_line"] = (0, ifi) if ifi is not None else (1, seq)

    idents = {}
    for sample in results:
        idents.setdefault(sample["label"], set()).add(sample["_line"])
    ranks = {
        label: {ident: pos + 1 for pos, ident in enumerate(sorted(values))}
        for label, values in idents.items()
        if len(values) > 1
    }
    for sample in results:
        label = sample["label"]
        if label in ranks:
            sample["label"] = f"{label}-{ranks[label][sample['_line']]}"
        sample["key"] = f"{sample['label']}|{sample['direction']}"
        sample.pop("_line", None)
    return results


def _bandwidth_indexes(rates):
    ports = {}
    for sample in rates:
        label = sample["label"]
        try:
            if_index = int(sample.get("if_index"))
        except (TypeError, ValueError):
            if_index = 2**31
        ports[label] = min(if_index, ports.get(label, 2**31))
    ordered = sorted(ports, key=lambda label: (ports[label], label.lower()))
    return {label: index for index, label in enumerate(ordered)}


class IspBandwidthWatcher:
    """Long-running, in-memory ISP WAN bandwidth alert controller."""

    def __init__(
        self,
        *,
        enabled,
        alert_for_seconds,
        poll_interval,
        rate_window,
        resolve_seconds,
        status_interval,
        spike_ignore_factor,
        data_missing_alert_seconds,
        wan_filter,
        bandwidth_config,
        saturation_percent,
        prometheus_url,
        prometheus_query,
        normalize_label,
        format_bps,
        build_bandwidth_card,
        build_data_missing_card,
        send,
        mark_watcher_health,
        log,
        sleep=None,
        time_fn=None,
    ):
        self.enabled = enabled
        self.alert_for_seconds = alert_for_seconds
        self.poll_interval = poll_interval
        self.rate_window = rate_window
        self.resolve_seconds = resolve_seconds
        self.status_interval = status_interval
        self.spike_ignore_factor = spike_ignore_factor
        self.data_missing_alert_seconds = data_missing_alert_seconds
        self.wan_filter = wan_filter
        self.bandwidth_config = bandwidth_config
        self.saturation_percent = saturation_percent
        self.prometheus_url = prometheus_url
        self.prometheus_query = prometheus_query
        self.normalize_label = normalize_label
        self.format_bps = format_bps
        self.build_bandwidth_card = build_bandwidth_card
        self.build_data_missing_card = build_data_missing_card
        self.send = send
        self.mark_watcher_health = mark_watcher_health
        self.log = log
        self.sleep = sleep or time.sleep
        self.time = time_fn or time.time

    def _fetch_wan_rates(self):
        results = []
        for direction, metric in (("in", "ifHCInOctets"), ("out", "ifHCOutOctets")):
            query = f'rate({metric}{{job="firewall-snmp"}}[{self.rate_window}]) * 8'
            for item in self.prometheus_query(query):
                metric_labels = item.get("metric") or {}
                label = _wan_label(metric_labels)
                if not label or not _is_wan_port(label, self.wan_filter):
                    continue
                try:
                    value_bps = float((item.get("value") or [None, "nan"])[1])
                except (TypeError, ValueError):
                    continue
                if value_bps < 0:
                    continue
                results.append({
                    "key": f"{label}|{direction}",
                    "label": label,
                    "direction": direction,
                    "value_bps": value_bps,
                    "if_index": metric_labels.get("ifIndex"),
                    "target_ip": metric_labels.get("target_ip") or metric_labels.get("instance") or "",
                })
        return _dedupe_wan_labels(results)

    def _log_status(self, rates, bandwidth_cfg):
        if not rates:
            self.log(
                "[ISP] no WAN traffic series matched "
                f"FIREWALL_WAN_IF_FILTER={self.wan_filter!r}; "
                "check Prometheus job=firewall-snmp labels ifAlias/ifName/ifDescr"
            )
            return

        rows = []
        indexes = _bandwidth_indexes(rates)
        for sample in sorted(rates, key=lambda item: item["value_bps"], reverse=True)[:6]:
            capacity_mbps = _bandwidth_for_label(
                sample["label"], sample["direction"], bandwidth_cfg,
                self.normalize_label, indexes.get(sample["label"]),
            )
            threshold_bps = capacity_mbps * 1000000 * (self.saturation_percent / 100.0)
            rows.append(
                f"{sample['label']} {sample['direction']}="
                f"{self.format_bps(sample['value_bps'])}/{self.format_bps(threshold_bps)}"
            )
        self.log("[ISP] rates " + "; ".join(rows))

    def run(self):
        if not self.enabled:
            self.log("[ISP] realtime bandwidth watcher disabled")
            return
        self.sleep(30)
        bandwidth_cfg = _parse_bandwidth_config(self.bandwidth_config, self.normalize_label)
        last_status_log = 0.0
        data_seen = False
        data_missing_since = None
        data_missing_alerting = False
        states = {}
        self.log(
            "[ISP] realtime bandwidth watcher enabled "
            f"(threshold={self.saturation_percent:g}%, for={self.alert_for_seconds}s, "
            f"poll={self.poll_interval}s, rate_window={self.rate_window}, "
            f"spike_ignore_factor={self.spike_ignore_factor:g}, "
            f"data_missing_after={self.data_missing_alert_seconds}s, prometheus={self.prometheus_url})"
        )

        while True:
            now = self.time()
            try:
                rates = self._fetch_wan_rates()
            except Exception as exc:
                self.mark_watcher_health("isp-bandwidth", False, exc)
                self.log(f"[ISP] poll failed: {exc}")
                self.sleep(self.poll_interval)
                continue
            self.mark_watcher_health("isp-bandwidth", True)

            if rates:
                if data_missing_alerting:
                    missing = now - data_missing_since if data_missing_since else 0
                    self.log(f"[ISP] WAN traffic series recovered after {int(missing)}s gap")
                    if not self.send(self.build_data_missing_card(missing, recovered=True)):
                        self.sleep(self.poll_interval)
                        continue
                data_seen = True
                data_missing_since = None
                data_missing_alerting = False
            elif data_seen and self.data_missing_alert_seconds > 0:
                if data_missing_since is None:
                    data_missing_since = now
                elif (
                    not data_missing_alerting
                    and now - data_missing_since >= self.data_missing_alert_seconds
                ):
                    self.log(
                        "[ISP] ALERT WAN traffic series missing for "
                        f"{int(now - data_missing_since)}s; check firewall SNMP and FIREWALL_WAN_IF_FILTER"
                    )
                    if self.send(self.build_data_missing_card(
                        now - data_missing_since, recovered=False,
                    )):
                        data_missing_alerting = True

            if now - last_status_log >= self.status_interval:
                self._log_status(rates, bandwidth_cfg)
                last_status_log = now

            seen = set()
            indexes = _bandwidth_indexes(rates)
            for sample in rates:
                seen.add(sample["key"])
                capacity_mbps = _bandwidth_for_label(
                    sample["label"], sample["direction"], bandwidth_cfg,
                    self.normalize_label, indexes.get(sample["label"]),
                )
                glitch_limit = _counter_glitch_limit_bps(
                    capacity_mbps, self.spike_ignore_factor,
                )
                if glitch_limit is not None and sample["value_bps"] >= glitch_limit:
                    self.log(
                        f"[ISP] ignore counter glitch {sample['label']} {sample['direction']} "
                        f"{self.format_bps(sample['value_bps'])} (> {self.format_bps(glitch_limit)}, "
                        f"capacity {capacity_mbps:g} Mbps)"
                    )
                    continue
                threshold_bps = capacity_mbps * 1000000 * (self.saturation_percent / 100.0)
                state = states.setdefault(sample["key"], {
                    "active_since": None,
                    "clear_since": None,
                    "alerting": False,
                    "alert_started": None,
                    "last_value": 0.0,
                })
                state["last_value"] = sample["value_bps"]

                if sample["value_bps"] >= threshold_bps:
                    if state["active_since"] is None:
                        state["active_since"] = now
                    state["clear_since"] = None
                    duration = now - state["active_since"]
                    if not state["alerting"] and duration >= self.alert_for_seconds:
                        event = {
                            **sample,
                            "threshold_bps": threshold_bps,
                            "capacity_mbps": capacity_mbps,
                            "percent": self.saturation_percent,
                            "duration": duration,
                        }
                        self.log(
                            f"[ISP] ALERT {sample['label']} {sample['direction']} "
                            f"{self.format_bps(sample['value_bps'])} >= {self.format_bps(threshold_bps)}"
                        )
                        if self.send(self.build_bandwidth_card(event, recovered=False)):
                            state["alerting"] = True
                            state["alert_started"] = state["active_since"]
                else:
                    state["active_since"] = None
                    if state["alerting"]:
                        if state["clear_since"] is None:
                            state["clear_since"] = now
                        clear_duration = now - state["clear_since"]
                        if clear_duration >= self.resolve_seconds:
                            start = (
                                state["alert_started"]
                                if state["alert_started"] is not None
                                else state["clear_since"]
                            )
                            event = {
                                **sample,
                                "threshold_bps": threshold_bps,
                                "capacity_mbps": capacity_mbps,
                                "percent": self.saturation_percent,
                                "duration": now - start,
                            }
                            self.log(
                                f"[ISP] RECOVER {sample['label']} {sample['direction']} "
                                f"{self.format_bps(sample['value_bps'])} < {self.format_bps(threshold_bps)}"
                            )
                            if self.send(self.build_bandwidth_card(event, recovered=True)):
                                state["alerting"] = False
                                state["alert_started"] = None
                    else:
                        state["clear_since"] = None

            for key, state in list(states.items()):
                if key in seen:
                    continue
                if state.get("alerting"):
                    state["clear_since"] = None
                elif state.get("clear_since") and now - state["clear_since"] >= self.resolve_seconds:
                    states.pop(key, None)

            self.sleep(self.poll_interval)
