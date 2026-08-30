from pathlib import Path

import pytest

import bridge_interconnect_watcher as interconnect


class StopWatcher(BaseException):
    pass


def normalize_label(value):
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def make_watcher(**overrides):
    calls = {
        "loads": [],
        "saves": [],
        "health": [],
        "logs": [],
        "names": 0,
        "topology": 0,
    }

    def load_state(path):
        calls["loads"].append(path)
        return {}

    def save_state(path, values):
        calls["saves"].append((path, values))
        return True

    def fetch_names():
        calls["names"] += 1
        return {"10.0.0.1": "core"}

    def load_topology():
        calls["topology"] += 1
        return []

    kwargs = {
        "enabled": True,
        "alert_for_seconds": 5,
        "poll_interval": 5,
        "jobs": "infra-switch-ifmib",
        "port_filter": "port-channel,po",
        "state_file": "interconnect-alerts.json",
        "prometheus_query": lambda _query: [],
        "fetch_name_cache": fetch_names,
        "load_topology_edges": load_topology,
        "load_json_dict": load_state,
        "save_json_dict": save_state,
        "as_float": lambda value, default=None: default if value is None else float(value),
        "normalize_label": normalize_label,
        "audit_port_key": normalize_label,
        "build_card": lambda event, recovered=False: (event, recovered),
        "send": lambda _card: True,
        "find_merge_candidate": lambda _event, _now: None,
        "complete_merge": lambda _event, _cause, _now: None,
        "mark_watcher_health": lambda *args: calls["health"].append(args),
        "log": calls["logs"].append,
        "sleep": lambda _seconds: None,
        "now": lambda: 100.0,
    }
    kwargs.update(overrides)
    return interconnect.InterconnectWatcher(**kwargs), calls


def test_disabled_returns_without_loading_or_sleeping():
    sleeps = []
    watcher, calls = make_watcher(enabled=False, sleep=sleeps.append)

    watcher.run()

    assert calls["loads"] == []
    assert sleeps == []
    assert calls["logs"] == ["[LINK] interconnect watcher disabled"]


def test_invalid_jobs_return_without_loading_or_sleeping():
    sleeps = []
    watcher, calls = make_watcher(jobs="bad job,$oops", sleep=sleeps.append)

    watcher.run()

    assert calls["loads"] == []
    assert sleeps == []
    assert calls["logs"] == ["[LINK] no valid SNMP jobs configured, watcher disabled"]


def test_singleton_run_reloads_persisted_active_state_after_each_supervisor_restart():
    loads = []

    def load_state(path):
        loads.append(path)
        return {}

    watcher, _calls = make_watcher(
        load_json_dict=load_state,
        sleep=lambda _seconds: (_ for _ in ()).throw(StopWatcher()),
    )

    for _attempt in range(2):
        with pytest.raises(StopWatcher):
            watcher.run()

    assert loads == ["interconnect-alerts.json", "interconnect-alerts.json"]


def test_lag_conflict_log_dedupe_survives_poll_and_run_boundaries():
    rows = [
        {
            "metric": {
                "target_ip": "10.0.0.1",
                "ifStackHigherLayer": "47",
                "ifStackLowerLayer": "10",
            },
            "value": [0, "1"],
        },
        {
            "metric": {
                "target_ip": "10.0.0.1",
                "ifStackHigherLayer": "48",
                "ifStackLowerLayer": "10",
            },
            "value": [0, "1"],
        },
    ]
    watcher, calls = make_watcher(
        prometheus_query=lambda query: rows if query.startswith("ifStackStatus") else [],
    )

    assert watcher.fetch_interconnect_members("infra-switch-ifmib") == {}
    assert watcher.fetch_interconnect_members("infra-switch-ifmib") == {}

    conflict_logs = [line for line in calls["logs"] if "ambiguous LAG ownership" in line]
    assert len(conflict_logs) == 1
    assert watcher._last_conflicts["10.0.0.1"][10]["reason"] == "ambiguous-ifstack"


def test_successful_poll_refreshes_names_topology_and_marks_health_true():
    sleeps = []
    watcher, calls = make_watcher(
        sleep=lambda seconds: sleeps.append(seconds),
    )
    watcher.fetch_interconnect_ports = lambda _jobs: []

    def stop_after_poll(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise StopWatcher

    watcher.sleep = stop_after_poll

    with pytest.raises(StopWatcher):
        watcher.run()

    assert calls["names"] == 1
    assert calls["topology"] == 1
    assert calls["health"] == [("interconnect", True)]
    assert sleeps == [25, 5]


def test_poll_exception_marks_health_false_and_retries_at_original_cadence():
    sleeps = []
    error = RuntimeError("prometheus unavailable")
    watcher, calls = make_watcher()
    watcher.fetch_interconnect_ports = lambda _jobs: (_ for _ in ()).throw(error)

    def stop_after_retry(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise StopWatcher

    watcher.sleep = stop_after_retry

    with pytest.raises(StopWatcher):
        watcher.run()

    assert calls["health"] == [("interconnect", False, error)]
    assert any("[LINK] poll failed: prometheus unavailable" in line for line in calls["logs"])
    assert sleeps == [25, 5]


def test_module_has_no_bridge_import_thread_or_environment_parsing():
    source = (
        Path(__file__).resolve().parent.parent / "bridge_interconnect_watcher.py"
    ).read_text(encoding="utf-8")

    assert "alertmanager-feishu-bridge" not in source
    assert "import bridge" not in source
    assert "threading" not in source
    assert "os.environ" not in source
    assert "INTERCONNECT_STATE_FILE" not in source
