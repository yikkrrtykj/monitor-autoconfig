import copy
import importlib.util
from pathlib import Path

import pytest

from feishu_bridge.resource_watcher import (
    RESOURCE_QUERY,
    ResourceWatcher,
    evaluate_resource_alert_state,
    fetch_cisco_resource_usage,
    parse_cisco_resource_samples,
)


class StopWatcher(BaseException):
    pass


class RecordingLock:
    def __init__(self, events):
        self.events = events

    def __enter__(self):
        self.events.append("lock-enter")
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.events.append("lock-exit")


def resource_sample(name, target="10.0.0.1", value=1, **labels):
    metric = {
        "__name__": name,
        "job": "infra-switch-resources",
        "target_ip": target,
        "instance": "RTS1",
        "ciscoMemoryPoolType": "1",
    }
    metric.update(labels)
    return {"metric": metric, "value": [1, str(value)]}


def usage_sample(kind="cpu", value=75, ip="10.0.0.1", name="RTS1", **extra):
    sample = {
        "key": f"{kind}|{ip}",
        "kind": kind,
        "ip": ip,
        "name": name,
        "value": value,
    }
    sample.update(extra)
    return sample


def make_watcher(**overrides):
    events = overrides.pop("events", [])
    initial_state = copy.deepcopy(overrides.pop("initial_state", {}))
    loads = overrides.pop("loads", [])
    saves = overrides.pop("saves", [])
    logs = overrides.pop("logs", [])
    health = overrides.pop("health", [])
    sent = overrides.pop("sent", [])
    send_results = iter(overrides.pop("send_results", []))
    lock = overrides.pop("state_lock", RecordingLock(events))

    def load_state(path):
        events.append(f"load:{path}")
        loads.append(path)
        return copy.deepcopy(initial_state)

    def save_state(path, state):
        events.append(f"save:{path}")
        saves.append(copy.deepcopy(state))

    def build_card(sample, recovered=False, duration=0):
        return {
            "sample": copy.deepcopy(sample),
            "recovered": recovered,
            "duration": duration,
        }

    def send(card):
        sent.append(copy.deepcopy(card))
        return next(send_results, True)

    options = {
        "enabled": True,
        "poll_interval": 30,
        "cpu_alert_percent": 70,
        "cpu_alert_for_seconds": 10,
        "cpu_recover_percent": 60,
        "memory_alert_percent": 80,
        "memory_alert_for_seconds": 20,
        "memory_recover_percent": 70,
        "recover_seconds": 120,
        "state_file": "/state/device-resource-alerts.json",
        "state_lock": lock,
        "prometheus_query": lambda _query: [],
        "load_state": load_state,
        "save_state": save_state,
        "build_card": build_card,
        "send": send,
        "mark_watcher_health": lambda *args: health.append(args),
        "log": logs.append,
    }
    options.update(overrides)
    watcher = ResourceWatcher(**options)
    return watcher, {
        "events": events,
        "loads": loads,
        "saves": saves,
        "logs": logs,
        "health": health,
        "sent": sent,
        "lock": lock,
    }


def run_polls(watcher, batches, times):
    pending_batches = iter(batches)
    clock = iter(times)
    sleeps = []

    def fetch():
        value = next(pending_batches)
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= len(batches) + 1:
            raise StopWatcher

    watcher.fetch_cisco_resource_usage = fetch
    watcher.time = lambda: next(clock)
    watcher.sleep = sleep
    with pytest.raises(StopWatcher):
        watcher.run()
    return sleeps


def test_disabled_watcher_logs_and_returns_without_loading_or_sleeping():
    sleeps = []
    watcher, observed = make_watcher(enabled=False, sleep=sleeps.append)

    assert watcher.run() is None
    assert sleeps == []
    assert observed["loads"] == []
    assert observed["health"] == []
    assert observed["sent"] == []
    assert observed["logs"] == ["[RESOURCE] Cisco CPU/memory watcher disabled"]


def test_state_load_is_locked_and_happens_before_twenty_second_startup_sleep():
    events = []

    def sleep(seconds):
        events.append(f"sleep:{seconds}")
        raise StopWatcher

    watcher, observed = make_watcher(events=events, sleep=sleep)

    with pytest.raises(StopWatcher):
        watcher.run()

    assert watcher.state_lock is observed["lock"]
    assert events == [
        "lock-enter",
        "load:/state/device-resource-alerts.json",
        "lock-exit",
        "sleep:20",
    ]


def test_poll_cadence_and_success_health_are_unchanged():
    watcher, observed = make_watcher()

    sleeps = run_polls(watcher, [[]], [100])

    assert sleeps == [20, 30]
    assert observed["health"] == [("device-resources", True)]
    assert observed["saves"] == []


def test_poll_failure_marks_unhealthy_logs_and_waits_one_interval():
    watcher, observed = make_watcher()

    sleeps = run_polls(watcher, [RuntimeError("prometheus down")], [100])

    assert sleeps == [20, 30]
    assert len(observed["health"]) == 1
    assert observed["health"][0][0:2] == ("device-resources", False)
    assert str(observed["health"][0][2]) == "prometheus down"
    assert "[RESOURCE] poll failed: prometheus down" in observed["logs"]


def test_fetch_uses_exact_promql_and_collapses_results():
    queries = []

    def query(promql):
        queries.append(promql)
        return [resource_sample("cpmCPUTotal5minRev", value=42)]

    assert fetch_cisco_resource_usage(query) == [usage_sample(value=42)]
    assert queries == [RESOURCE_QUERY]
    assert RESOURCE_QUERY == (
        '{job="infra-switch-resources",'
        '__name__=~"cpmCPUTotal5minRev|cpmCPUTotal5min|ciscoMemoryPoolUsed|ciscoMemoryPoolFree"}'
    )


def test_cpu_prefers_revised_samples_uses_worst_and_falls_back_to_legacy():
    rows = parse_cisco_resource_samples([
        resource_sample("cpmCPUTotal5min", value=99),
        resource_sample("cpmCPUTotal5minRev", value=72),
        resource_sample("cpmCPUTotal5minRev", value=81),
        resource_sample("cpmCPUTotal5min", target="10.0.0.2", value=37,
                        instance="RTS2"),
    ])

    by_key = {row["key"]: row for row in rows}
    assert by_key["cpu|10.0.0.1"] == usage_sample(value=81)
    assert by_key["cpu|10.0.0.2"] == usage_sample(
        value=37, ip="10.0.0.2", name="RTS2",
    )


def test_cpu_ignores_invalid_or_out_of_range_values_and_uses_identity_fallbacks():
    rows = parse_cisco_resource_samples([
        resource_sample("cpmCPUTotal5minRev", value=-1),
        resource_sample("cpmCPUTotal5minRev", value=101),
        resource_sample("cpmCPUTotal5minRev", value="bad"),
        resource_sample("cpmCPUTotal5minRev", target="", value=40,
                        instance="10.0.0.3", display_name="Core"),
        resource_sample("cpmCPUTotal5minRev", target="", value=41,
                        instance="10.0.0.4", display_name=""),
        {"metric": {"__name__": "cpmCPUTotal5minRev"}, "value": [1, "50"]},
    ])

    assert rows == [
        usage_sample(value=40, ip="10.0.0.3", name="Core"),
        usage_sample(value=41, ip="10.0.0.4", name="10.0.0.4"),
    ]


def test_memory_requires_both_halves_positive_total_and_selects_worst_pool():
    rows = parse_cisco_resource_samples([
        resource_sample("ciscoMemoryPoolUsed", value=800, ciscoMemoryPoolType="1"),
        resource_sample("ciscoMemoryPoolFree", value=200, ciscoMemoryPoolType="1"),
        resource_sample("ciscoMemoryPoolUsed", value=90, ciscoMemoryPoolType="2"),
        resource_sample("ciscoMemoryPoolFree", value=10, ciscoMemoryPoolType="2"),
        resource_sample("ciscoMemoryPoolUsed", value=50, ciscoMemoryPoolType="missing"),
        resource_sample("ciscoMemoryPoolUsed", value=0, ciscoMemoryPoolType="zero"),
        resource_sample("ciscoMemoryPoolFree", value=0, ciscoMemoryPoolType="zero"),
        resource_sample("ciscoMemoryPoolUsed", value=-1, ciscoMemoryPoolType="negative"),
        resource_sample("ciscoMemoryPoolFree", value=1, ciscoMemoryPoolType="negative"),
    ])

    assert rows == [{
        "key": "memory|10.0.0.1",
        "kind": "memory",
        "ip": "10.0.0.1",
        "name": "RTS1",
        "value": 90.0,
        "pool": "2",
    }]


def test_alert_threshold_requires_sustained_time_and_resets_below_threshold():
    state, action = evaluate_resource_alert_state({}, 75, 100, 70, 300, 60, 120)
    assert action is None and state["active_since"] == 100
    state, action = evaluate_resource_alert_state(state, 69, 399, 70, 300, 60, 120)
    assert action is None and state["active_since"] is None
    state, action = evaluate_resource_alert_state(state, 75, 400, 70, 300, 60, 120)
    assert action is None and state["active_since"] == 400
    state, action = evaluate_resource_alert_state(state, 75, 700, 70, 300, 60, 120)
    assert action == "alert"


def test_recovery_uses_distinct_threshold_debounce_and_resets_above_recover():
    state = {
        "alerting": True,
        "active_since": 100.0,
        "alert_started": 100.0,
    }
    state, action = evaluate_resource_alert_state(state, 65, 200, 70, 300, 60, 120)
    assert action is None and state["recover_since"] is None
    state, action = evaluate_resource_alert_state(state, 55, 300, 70, 300, 60, 120)
    assert action is None and state["recover_since"] == 300
    state, action = evaluate_resource_alert_state(state, 61, 350, 70, 300, 60, 120)
    assert action is None and state["recover_since"] is None
    state, action = evaluate_resource_alert_state(state, 55, 400, 70, 300, 60, 120)
    assert action is None
    state, action = evaluate_resource_alert_state(state, 55, 520, 70, 300, 60, 120)
    assert action == "recover"


def test_cpu_and_memory_keep_separate_alert_and_recovery_thresholds():
    cpu = [usage_sample("cpu", 75)]
    memory = [usage_sample("memory", 75, pool="1")]
    watcher, observed = make_watcher(
        cpu_alert_for_seconds=0,
        memory_alert_for_seconds=0,
    )

    run_polls(watcher, [cpu + memory], [100])

    assert len(observed["sent"]) == 1
    assert observed["sent"][0]["sample"]["kind"] == "cpu"


def test_quiet_memory_state_schema_keeps_the_selected_pool():
    watcher, observed = make_watcher()

    run_polls(watcher, [[usage_sample("memory", 50, pool="processor")]], [100])

    assert observed["saves"][-1] == {
        "memory|10.0.0.1": {
            "last_value": 50.0,
            "last_seen": 100.0,
            "recover_since": None,
            "active_since": None,
            "kind": "memory",
            "ip": "10.0.0.1",
            "name": "RTS1",
            "pool": "processor",
        },
    }


def test_alert_send_failure_retries_mature_timer_and_success_sets_alert_started():
    high = [usage_sample(value=75)]
    watcher, observed = make_watcher(send_results=[False, True])

    run_polls(watcher, [high, high, high, high], [100, 110, 115, 120])

    assert [card["duration"] for card in observed["sent"]] == [10, 15]
    final = observed["saves"][-1]["cpu|10.0.0.1"]
    assert final == {
        "last_value": 75.0,
        "last_seen": 120.0,
        "recover_since": None,
        "active_since": 100.0,
        "kind": "cpu",
        "ip": "10.0.0.1",
        "name": "RTS1",
        "alerting": True,
        "alert_started": 100.0,
    }


def test_recovery_send_failure_keeps_alert_and_success_uses_alert_started_duration():
    initial = {
        "cpu|10.0.0.1": {
            "kind": "cpu",
            "ip": "10.0.0.1",
            "name": "RTS1",
            "last_value": 80.0,
            "last_seen": 190.0,
            "active_since": 100.0,
            "recover_since": None,
            "alerting": True,
            "alert_started": 100.0,
        },
    }
    low = [usage_sample(value=55)]
    watcher, observed = make_watcher(
        initial_state=initial,
        send_results=[False, True],
    )

    run_polls(watcher, [low, low, low], [200, 320, 325])

    assert [card["duration"] for card in observed["sent"]] == [220, 225]
    assert [card["recovered"] for card in observed["sent"]] == [True, True]
    failed = observed["saves"][-2]["cpu|10.0.0.1"]
    assert failed["alerting"] is True
    assert failed["recover_since"] == 200.0
    final = observed["saves"][-1]["cpu|10.0.0.1"]
    assert final == {
        "kind": "cpu",
        "ip": "10.0.0.1",
        "name": "RTS1",
        "last_value": 55.0,
        "last_seen": 325,
        "active_since": None,
        "recover_since": None,
        "alerting": False,
    }


@pytest.mark.parametrize("timer_field", ["active_since", "recover_since"])
def test_missing_series_breaks_in_progress_timers(timer_field):
    state = {
        "kind": "cpu",
        "ip": "10.0.0.1",
        "name": "RTS1",
        "last_value": 75.0,
        "last_seen": 100.0,
        "active_since": None,
        "recover_since": None,
        "alerting": timer_field == "recover_since",
    }
    state[timer_field] = 100.0
    if state["alerting"]:
        state["alert_started"] = 50.0
    watcher, observed = make_watcher(initial_state={"cpu|10.0.0.1": state})

    run_polls(watcher, [[]], [110])

    saved = observed["saves"][-1]["cpu|10.0.0.1"]
    assert saved["active_since"] is None
    assert saved["recover_since"] is None
    assert saved["alerting"] is state["alerting"]
    assert observed["sent"] == []


def test_missing_series_never_recovers_or_prunes_an_active_alert():
    state = {
        "kind": "cpu",
        "ip": "10.0.0.1",
        "name": "RTS1",
        "last_value": 80.0,
        "last_seen": 1.0,
        "active_since": None,
        "recover_since": None,
        "alerting": True,
        "alert_started": 1.0,
    }
    watcher, observed = make_watcher(initial_state={"cpu|10.0.0.1": state})

    run_polls(watcher, [[]], [5000])

    assert observed["sent"] == []
    assert observed["saves"] == []


def test_non_alerting_state_prunes_only_after_more_than_3600_seconds():
    state = {
        "kind": "cpu",
        "ip": "10.0.0.1",
        "name": "RTS1",
        "last_value": 10.0,
        "last_seen": 1.0,
        "active_since": None,
        "recover_since": None,
        "alerting": False,
    }
    watcher, observed = make_watcher(initial_state={"cpu|10.0.0.1": state})

    run_polls(watcher, [[], []], [3601, 3602])

    assert observed["saves"] == [{}]


def test_state_is_saved_only_when_changed_and_save_uses_supplied_lock():
    stable = {
        "kind": "cpu",
        "ip": "10.0.0.1",
        "name": "RTS1",
        "last_value": 10.0,
        "last_seen": 50.0,
        "active_since": None,
        "recover_since": None,
        "alerting": False,
    }
    watcher, observed = make_watcher(initial_state={"cpu|10.0.0.1": stable})

    run_polls(watcher, [[], [usage_sample(value=20)]], [100, 110])

    assert len(observed["saves"]) == 1
    assert observed["events"] == [
        "lock-enter",
        "load:/state/device-resource-alerts.json",
        "lock-exit",
        "lock-enter",
        "save:/state/device-resource-alerts.json",
        "lock-exit",
    ]


def test_bridge_device_resource_wrapper_delegates_to_extracted_watcher(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "feishu_bridge_resource_wrapper",
        Path(__file__).resolve().parent.parent / "alertmanager-feishu-bridge.py",
    )
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    calls = []
    monkeypatch.setattr(bridge._RESOURCE_WATCHER, "run", lambda: calls.append("run"))

    assert bridge.device_resource_watcher() is None
    assert calls == ["run"]
