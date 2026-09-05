"""ISP WAN bandwidth watcher extracted from the Feishu alert bridge.

The bridge continues to own environment parsing, Prometheus transport, card
presentation, Feishu delivery, watcher supervision, and shared health state.
"""

import json
import re
import time
from pathlib import Path


def _parse_bandwidth_config(raw, _normalize_label=None):
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


def _bandwidth_entry_for_label(label, cfg, identity_ambiguous=False):
    if identity_ambiguous:
        return None
    lower = label.lower()
    for entry in cfg["per"]:
        if entry["label"] == lower:
            return entry
    return None


def _bandwidth_for_label(label, direction, cfg, _normalize_label=None,
                         identity_ambiguous=False):
    entry = _bandwidth_entry_for_label(label, cfg, identity_ambiguous)
    if entry is not None:
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
    """Keep native labels and isolate duplicate-series state without renaming.

    ifIndex is useful for distinguishing simultaneous Prometheus series, but it
    is not stable identity evidence and must never manufacture the label used
    to bind per-ISP bandwidth metadata.
    """
    occ = {}
    for sample in results:
        try:
            ifi = int(sample.get("if_index"))
        except (TypeError, ValueError):
            ifi = None
        target = sample.get("target_ip") or "unknown-target"
        dir_key = (sample["label"], target, sample["direction"])
        seq = occ.get(dir_key, 0)
        occ[dir_key] = seq + 1
        sample["_line"] = (0, target, ifi) if ifi is not None else (1, target, seq)

    idents = {}
    for sample in results:
        idents.setdefault(sample["label"].casefold(), set()).add(sample["_line"])
    for sample in results:
        label = sample["label"]
        ambiguous = len(idents[label.casefold()]) > 1
        sample["_identity_ambiguous"] = ambiguous
        if ambiguous:
            target = sample.get("target_ip") or "unknown-target"
            sample["key"] = (
                f"{label}|{target}|{sample['_line']}|{sample['direction']}"
            )
        else:
            sample["key"] = f"{label}|{sample['direction']}"
        sample.pop("_line", None)
    return results


def _load_isp_identity_map(path):
    """Return canonical target/ifIndex mappings plus legacy native mappings."""
    if not path:
        return {}, set()
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}, set()
    if not isinstance(payload, list):
        return {}, set()
    candidates = {}
    conflicts = set()
    for entry in payload:
        labels = entry.get("labels") if isinstance(entry, dict) else None
        if not isinstance(labels, dict):
            continue
        metric_name = str(labels.get("metric_name") or "").strip()
        metric_target = str(labels.get("metric_target") or "").strip()
        metric_ifindex = str(labels.get("metric_ifindex") or "").strip()
        display_name = str(labels.get("display_name") or "").strip()
        identity = (
            (metric_target, metric_ifindex)
            if metric_target and metric_ifindex.isdigit() and int(metric_ifindex) > 0
            else metric_name.casefold()
        )
        if str(labels.get("metadata_conflict") or "").strip().casefold() == "true":
            if identity:
                conflicts.add(identity)
            continue
        if identity and display_name:
            candidates.setdefault(identity, set()).add(display_name)
    conflicts.update(
        metric_name for metric_name, display_names in candidates.items()
        if len(display_names) != 1
    )
    return ({
        metric_name: next(iter(display_names))
        for metric_name, display_names in candidates.items()
        if len(display_names) == 1
    }, conflicts)


def _apply_inventory_identity(results, identity_map, conflicts):
    for sample in results:
        native_name = sample["label"]
        sample["metric_name"] = native_name
        target = str(sample.get("target_ip") or "").strip()
        ifindex = str(sample.get("if_index") or "").strip()
        canonical_key = (target, ifindex) if target and ifindex else None
        if canonical_key in conflicts or native_name.casefold() in conflicts:
            sample["_identity_ambiguous"] = True
            continue
        display_name = identity_map.get(canonical_key) if canonical_key else None
        if display_name:
            sample["label"] = display_name
            sample["_identity_ambiguous"] = False
            continue
        display_name = identity_map.get(native_name.casefold())
        if display_name and not sample.get("_identity_ambiguous", False):
            sample["label"] = display_name
    return results


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
        inventory_file=None,
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
        self.inventory_file = inventory_file
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
        rates = _dedupe_wan_labels(results)
        identity_map, conflicts = _load_isp_identity_map(self.inventory_file)
        return _dedupe_wan_labels(
            _apply_inventory_identity(
                rates, identity_map, conflicts
            )
        )

    def _log_status(self, rates, bandwidth_cfg):
        if not rates:
            self.log(
                "[ISP] no WAN traffic series matched "
                f"FIREWALL_WAN_IF_FILTER={self.wan_filter!r}; "
                "check Prometheus job=firewall-snmp labels ifAlias/ifName/ifDescr"
            )
            return

        rows = []
        for sample in sorted(rates, key=lambda item: item["value_bps"], reverse=True)[:6]:
            capacity_mbps = _bandwidth_for_label(
                sample["label"], sample["direction"], bandwidth_cfg,
                self.normalize_label,
                sample.get("_identity_ambiguous", False),
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
        warned_unmatched_bandwidth = set()
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
            for sample in rates:
                seen.add(sample["key"])
                if (
                    bandwidth_cfg["per"]
                    and _bandwidth_entry_for_label(
                        sample["label"], bandwidth_cfg,
                        sample.get("_identity_ambiguous", False),
                    ) is None
                    and sample["label"].lower() not in warned_unmatched_bandwidth
                ):
                    warned_unmatched_bandwidth.add(sample["label"].lower())
                    self.log(
                        f"[ISP] WARNING no exact bandwidth metadata match for "
                        f"{sample['label']}; using global fallback"
                    )
                capacity_mbps = _bandwidth_for_label(
                    sample["label"], sample["direction"], bandwidth_cfg,
                    self.normalize_label,
                    sample.get("_identity_ambiguous", False),
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
