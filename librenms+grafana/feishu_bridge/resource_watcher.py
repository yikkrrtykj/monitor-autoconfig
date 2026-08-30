"""Cisco device CPU and memory watcher for the Feishu alert bridge.

The bridge owns environment parsing, shared persistence helpers, card
presentation, Feishu delivery, watcher health, and thread supervision.  This
module owns only resource sample normalization and the alert state machine.
"""

import time


RESOURCE_QUERY = (
    '{job="infra-switch-resources",'
    '__name__=~"cpmCPUTotal5minRev|cpmCPUTotal5min|ciscoMemoryPoolUsed|ciscoMemoryPoolFree"}'
)


def parse_cisco_resource_samples(samples):
    """Collapse Cisco CPU cores and memory pools to one worst value/device."""
    devices = {}
    for item in samples or []:
        metric = item.get("metric") or {}
        name = str(metric.get("__name__") or "")
        ip = str(metric.get("target_ip") or metric.get("instance") or "").strip()
        if not ip:
            continue
        try:
            value = float((item.get("value") or [None, "nan"])[1])
        except (TypeError, ValueError, IndexError):
            continue
        row = devices.setdefault(ip, {
            "ip": ip,
            "name": str(metric.get("display_name") or metric.get("instance") or ip),
            "cpu_rev": [],
            "cpu_legacy": [],
            "pools": {},
        })
        if name == "cpmCPUTotal5minRev" and 0 <= value <= 100:
            row["cpu_rev"].append(value)
        elif name == "cpmCPUTotal5min" and 0 <= value <= 100:
            row["cpu_legacy"].append(value)
        elif name in ("ciscoMemoryPoolUsed", "ciscoMemoryPoolFree") and value >= 0:
            pool = str(metric.get("ciscoMemoryPoolType") or "default")
            row["pools"].setdefault(pool, {})[
                "used" if name.endswith("Used") else "free"
            ] = value

    result = []
    for row in devices.values():
        cpu_values = row["cpu_rev"] or row["cpu_legacy"]
        if cpu_values:
            result.append({
                "key": f"cpu|{row['ip']}",
                "kind": "cpu",
                "ip": row["ip"],
                "name": row["name"],
                "value": max(cpu_values),
            })
        pools = []
        for pool, values in row["pools"].items():
            used = values.get("used")
            free = values.get("free")
            if used is None or free is None or used + free <= 0:
                continue
            pools.append((100.0 * used / (used + free), pool))
        if pools:
            percent, pool = max(pools)
            result.append({
                "key": f"memory|{row['ip']}",
                "kind": "memory",
                "ip": row["ip"],
                "name": row["name"],
                "value": percent,
                "pool": pool,
            })
    return result


def fetch_cisco_resource_usage(prometheus_query):
    return parse_cisco_resource_samples(prometheus_query(RESOURCE_QUERY))


def evaluate_resource_alert_state(
    state, value, now, alert_percent, alert_for, recover_percent, recover_for,
):
    """Advance sustained-threshold hysteresis and return ``(state, action)``."""
    state = dict(state or {})
    state["last_value"] = float(value)
    state["last_seen"] = float(now)
    action = None
    if not state.get("alerting"):
        state["recover_since"] = None
        if value >= alert_percent:
            if state.get("active_since") is None:
                state["active_since"] = float(now)
            if now - state["active_since"] >= alert_for:
                action = "alert"
        else:
            state["active_since"] = None
    else:
        state["active_since"] = (
            state.get("active_since") or state.get("alert_started") or float(now)
        )
        if value <= recover_percent:
            if state.get("recover_since") is None:
                state["recover_since"] = float(now)
            if now - state["recover_since"] >= recover_for:
                action = "recover"
        else:
            state["recover_since"] = None
    return state, action


class ResourceWatcher:
    """Persistent Cisco CPU/memory alert controller with injected bridge I/O."""

    def __init__(
        self,
        *,
        enabled,
        poll_interval,
        cpu_alert_percent,
        cpu_alert_for_seconds,
        cpu_recover_percent,
        memory_alert_percent,
        memory_alert_for_seconds,
        memory_recover_percent,
        recover_seconds,
        state_file,
        state_lock,
        prometheus_query,
        load_state,
        save_state,
        build_card,
        send,
        mark_watcher_health,
        log,
        sleep=None,
        time_fn=None,
    ):
        self.enabled = enabled
        self.poll_interval = poll_interval
        self.cpu_alert_percent = cpu_alert_percent
        self.cpu_alert_for_seconds = cpu_alert_for_seconds
        self.cpu_recover_percent = cpu_recover_percent
        self.memory_alert_percent = memory_alert_percent
        self.memory_alert_for_seconds = memory_alert_for_seconds
        self.memory_recover_percent = memory_recover_percent
        self.recover_seconds = recover_seconds
        self.state_file = state_file
        self.state_lock = state_lock
        self.prometheus_query = prometheus_query
        self.load_state = load_state
        self.save_state = save_state
        self.build_card = build_card
        self.send = send
        self.mark_watcher_health = mark_watcher_health
        self.log = log
        self.sleep = sleep or time.sleep
        self.time = time_fn or time.time

    def fetch_cisco_resource_usage(self):
        return fetch_cisco_resource_usage(self.prometheus_query)

    def run(self):
        if not self.enabled:
            self.log("[RESOURCE] Cisco CPU/memory watcher disabled")
            return
        with self.state_lock:
            states = self.load_state(self.state_file)
        self.sleep(20)
        self.log(
            "[RESOURCE] Cisco CPU/memory watcher enabled "
            f"(cpu={self.cpu_alert_percent:g}%/{self.cpu_alert_for_seconds}s, "
            f"memory={self.memory_alert_percent:g}%/{self.memory_alert_for_seconds}s, "
            f"poll={self.poll_interval}s)"
        )
        while True:
            now = self.time()
            try:
                samples = self.fetch_cisco_resource_usage()
            except Exception as exc:
                self.mark_watcher_health("device-resources", False, exc)
                self.log(f"[RESOURCE] poll failed: {exc}")
                self.sleep(self.poll_interval)
                continue
            self.mark_watcher_health("device-resources", True)
            changed = False
            seen_keys = set()
            for sample in samples:
                seen_keys.add(sample["key"])
                kind = sample["kind"]
                if kind == "cpu":
                    alert_percent = self.cpu_alert_percent
                    alert_for = self.cpu_alert_for_seconds
                    recover_percent = self.cpu_recover_percent
                else:
                    alert_percent = self.memory_alert_percent
                    alert_for = self.memory_alert_for_seconds
                    recover_percent = self.memory_recover_percent
                previous = states.get(sample["key"], {})
                state, action = evaluate_resource_alert_state(
                    previous, sample["value"], now,
                    alert_percent, alert_for, recover_percent,
                    self.recover_seconds,
                )
                state.update({
                    key: sample[key]
                    for key in ("kind", "ip", "name")
                    if key in sample
                })
                if sample.get("pool"):
                    state["pool"] = sample["pool"]
                if action == "alert":
                    duration = max(0, now - (state.get("active_since") or now))
                    self.log(
                        f"[RESOURCE] ALERT {sample['name']} "
                        f"{kind}={sample['value']:.1f}%"
                    )
                    if self.send(self.build_card(
                        sample, recovered=False, duration=duration,
                    )):
                        state["alerting"] = True
                        state["alert_started"] = state.get("active_since") or now
                elif action == "recover":
                    duration = max(0, now - (state.get("alert_started") or now))
                    self.log(
                        f"[RESOURCE] RECOVER {sample['name']} "
                        f"{kind}={sample['value']:.1f}%"
                    )
                    if self.send(self.build_card(
                        sample, recovered=True, duration=duration,
                    )):
                        state = {
                            "kind": kind,
                            "ip": sample["ip"],
                            "name": sample["name"],
                            "last_value": float(sample["value"]),
                            "last_seen": now,
                            "active_since": None,
                            "recover_since": None,
                            "alerting": False,
                        }
                if state != previous:
                    states[sample["key"]] = state
                    changed = True

            # Missing series are never treated as recovery. Prune only quiet
            # stale bookkeeping; an active alert waits for a real low sample.
            # Missing observations also break in-progress high/recovery timers.
            for key, state in list(states.items()):
                if key not in seen_keys:
                    if (
                        state.get("active_since") is not None
                        or state.get("recover_since") is not None
                    ):
                        state["active_since"] = None
                        state["recover_since"] = None
                        changed = True
                if state.get("alerting"):
                    continue
                last_seen = float(state.get("last_seen") or now)
                if now - last_seen > 3600:
                    states.pop(key, None)
                    changed = True
            if changed:
                with self.state_lock:
                    self.save_state(self.state_file, states)
            self.sleep(self.poll_interval)
