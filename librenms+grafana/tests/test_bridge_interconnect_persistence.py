import copy
import importlib.util
import json
from pathlib import Path

import pytest


_spec = importlib.util.spec_from_file_location(
    "feishu_bridge_interconnect_persistence",
    Path(__file__).resolve().parent.parent / "alertmanager-feishu-bridge.py",
)
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)


class StopWatcher(BaseException):
    pass


def interconnect_port(status="down"):
    if status == "healthy":
        lag_up, member_ups = True, [True, True]
    elif status == "degraded":
        lag_up, member_ups = True, [True, False]
    else:
        lag_up, member_ups = False, [False, False]
    return {
        "key": "infra-switch-ifmib|192.168.10.10|3",
        "device": "core-1",
        "ip": "192.168.10.10",
        "port": "Po3",
        "ifindex": "3",
        "alias": "pgs-stage1",
        "lag_up": lag_up,
        "members": [
            {
                "name": "Te1/0/4",
                "ifindex": "4",
                "up": member_ups[0],
                "alias": "pgs-stage1-a",
                "descr": "Te1/0/4",
            },
            {
                "name": "Te2/0/4",
                "ifindex": "48",
                "up": member_ups[1],
                "alias": "pgs-stage1-b",
                "descr": "Te2/0/4",
            },
        ],
    }


def active_state(status="down", down_since=100.0):
    port = interconnect_port(status)
    return {
        port["key"]: {
            "alerting": True,
            "down_since": down_since,
            "down_members": [member["name"] for member in port["members"] if not member["up"]],
            "peer_switch": "pgs-stage1",
            "last_port": port,
        },
    }


def read_state(path):
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def run_watcher(
    monkeypatch,
    tmp_path,
    batches,
    times,
    *,
    initial=None,
    send_results=(),
    events=None,
):
    state_file = tmp_path / "interconnect-alerts.json"
    if initial is not None:
        state_file.write_text(json.dumps(initial), encoding="utf-8")
    pending_batches = iter(copy.deepcopy(batches))
    clock = iter(times)
    results = iter(send_results)
    sent = []
    sleeps = []
    logs = []
    events = events if events is not None else []

    monkeypatch.setattr(bridge, "INTERCONNECT_ALERT_ENABLED", True)
    monkeypatch.setattr(bridge, "INTERCONNECT_ALERT_FOR_SECONDS", 5)
    monkeypatch.setattr(bridge, "INTERCONNECT_ALERT_POLL_INTERVAL", 5)
    monkeypatch.setattr(bridge, "INTERCONNECT_ALERT_JOBS", "infra-switch-ifmib")
    monkeypatch.setattr(bridge, "INTERCONNECT_STATE_FILE", str(state_file))
    monkeypatch.setattr(bridge, "fetch_interconnect_ports", lambda _jobs: next(pending_batches))
    monkeypatch.setattr(bridge, "fetch_librenms_name_cache", lambda: {})
    monkeypatch.setattr(bridge, "load_topology_edges", lambda: [])
    monkeypatch.setattr(bridge, "mark_watcher_health", lambda *_args: None)
    monkeypatch.setattr(bridge, "log", logs.append)
    monkeypatch.setattr(bridge.time, "time", lambda: next(clock))
    monkeypatch.setattr(
        bridge,
        "build_interconnect_card",
        lambda event, recovered=False: {
            "event": copy.deepcopy(event),
            "recovered": recovered,
        },
    )

    def send(card):
        events.append("send")
        sent.append(copy.deepcopy(card))
        return next(results, True)

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= len(batches) + 1:
            raise StopWatcher

    monkeypatch.setattr(bridge, "send_feishu", send)
    monkeypatch.setattr(bridge.time, "sleep", sleep)

    with pytest.raises(StopWatcher):
        bridge.interconnect_watcher()

    return {
        "state_file": state_file,
        "state": read_state(state_file),
        "sent": sent,
        "sleeps": sleeps,
        "logs": logs,
        "events": events,
    }


def test_clean_start_loads_empty_state_before_original_startup_sleep(monkeypatch, tmp_path):
    events = []
    monkeypatch.setattr(bridge, "INTERCONNECT_ALERT_ENABLED", True)
    monkeypatch.setattr(bridge, "INTERCONNECT_ALERT_JOBS", "infra-switch-ifmib")
    monkeypatch.setattr(bridge, "INTERCONNECT_STATE_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setattr(
        bridge,
        "_load_json_dict",
        lambda path: events.append(("load", path)) or {},
    )

    def stop_at_sleep(seconds):
        events.append(("sleep", seconds))
        raise StopWatcher

    monkeypatch.setattr(bridge.time, "sleep", stop_at_sleep)

    with pytest.raises(StopWatcher):
        bridge.interconnect_watcher()

    assert events == [
        ("load", str(tmp_path / "missing.json")),
        ("sleep", 25),
    ]


def test_new_down_waits_for_hold_then_persists_only_after_success(monkeypatch, tmp_path):
    observed = run_watcher(
        monkeypatch,
        tmp_path,
        [[interconnect_port("down")], [interconnect_port("down")]],
        [100.0, 105.0],
        send_results=[True],
    )

    assert len(observed["sent"]) == 1
    assert observed["sent"][0]["recovered"] is False
    assert observed["sent"][0]["event"]["duration"] == 5
    assert observed["sleeps"] == [25, 5, 5]
    persisted = observed["state"][interconnect_port()["key"]]
    assert persisted == {
        "alerting": True,
        "down_members": ["Te1/0/4", "Te2/0/4"],
        "down_since": 100.0,
        "last_port": interconnect_port("down"),
        "peer_switch": "",
    }


def test_alert_send_failure_does_not_persist_and_next_poll_retries(monkeypatch, tmp_path):
    saves = []
    original_save = bridge._save_json_dict

    def record_save(path, values):
        saves.append(copy.deepcopy(values))
        return original_save(path, values)

    monkeypatch.setattr(bridge, "_save_json_dict", record_save)
    observed = run_watcher(
        monkeypatch,
        tmp_path,
        [
            [interconnect_port("down")],
            [interconnect_port("down")],
            [interconnect_port("down")],
        ],
        [100.0, 105.0, 110.0],
        send_results=[False, True],
    )

    assert len(observed["sent"]) == 2
    assert [card["event"]["duration"] for card in observed["sent"]] == [5, 10]
    assert len(saves) == 1
    assert saves[0][interconnect_port()["key"]]["alerting"] is True


def test_restart_same_active_down_does_not_resend_alert(monkeypatch, tmp_path):
    observed = run_watcher(
        monkeypatch,
        tmp_path,
        [[interconnect_port("down")]],
        [200.0],
        initial=active_state("down"),
    )

    assert observed["sent"] == []
    assert observed["state"] == active_state("down")


@pytest.mark.parametrize(
    ("persisted_status", "current_status"),
    [("degraded", "down"), ("down", "degraded")],
)
def test_restart_status_change_within_active_failure_does_not_resend(
    monkeypatch, tmp_path, persisted_status, current_status,
):
    observed = run_watcher(
        monkeypatch,
        tmp_path,
        [[interconnect_port(current_status)]],
        [200.0],
        initial=active_state(persisted_status),
    )

    assert observed["sent"] == []


def test_restored_alert_healthy_recovery_success_clears_persistence(monkeypatch, tmp_path):
    observed = run_watcher(
        monkeypatch,
        tmp_path,
        [[interconnect_port("healthy")]],
        [200.0],
        initial=active_state("down", down_since=100.0),
        send_results=[True],
    )

    assert len(observed["sent"]) == 1
    assert observed["sent"][0]["recovered"] is True
    assert observed["sent"][0]["event"]["duration"] == 100
    assert observed["state"] == {}


def test_recovery_send_failure_retains_persisted_active_alert(monkeypatch, tmp_path):
    initial = active_state("down", down_since=100.0)
    observed = run_watcher(
        monkeypatch,
        tmp_path,
        [[interconnect_port("healthy")]],
        [200.0],
        initial=initial,
        send_results=[False],
    )

    assert len(observed["sent"]) == 1
    assert observed["sent"][0]["recovered"] is True
    assert observed["state"] == initial


def test_restored_vanished_alert_uses_original_hold_and_clears_after_recovery(
    monkeypatch, tmp_path,
):
    observed = run_watcher(
        monkeypatch,
        tmp_path,
        [[], [], []],
        [200.0, 204.0, 205.0],
        initial=active_state("down", down_since=100.0),
        send_results=[True],
    )

    assert len(observed["sent"]) == 1
    assert observed["sent"][0]["recovered"] is True
    assert observed["sent"][0]["event"]["port"] == "Po3"
    assert observed["sent"][0]["event"]["duration"] == 105
    assert observed["state"] == {}


def test_corrupt_missing_and_non_active_state_follow_generic_loader_behavior(
    monkeypatch, tmp_path,
):
    state_file = tmp_path / "interconnect-alerts.json"
    monkeypatch.setattr(bridge, "INTERCONNECT_STATE_FILE", str(state_file))

    assert bridge.load_interconnect_alert_states() == {}
    state_file.write_text("{broken", encoding="utf-8")
    assert bridge.load_interconnect_alert_states() == {}
    state_file.write_text(json.dumps({"key": {"alerting": False}}), encoding="utf-8")
    assert bridge.load_interconnect_alert_states() == {}


def test_alert_keeps_syslog_merge_order_before_active_state_persistence(monkeypatch, tmp_path):
    events = []
    original_save = bridge._save_json_dict
    monkeypatch.setattr(
        bridge,
        "find_errdisable_merge_candidate",
        lambda _event, _now: events.append("find") or {"id": "cause-1"},
    )
    monkeypatch.setattr(
        bridge,
        "complete_interconnect_merge",
        lambda _event, _cause, _now: events.append("complete"),
    )

    def save(path, values):
        events.append("persist")
        return original_save(path, values)

    monkeypatch.setattr(bridge, "_save_json_dict", save)
    run_watcher(
        monkeypatch,
        tmp_path,
        [[interconnect_port("down")], [interconnect_port("down")]],
        [100.0, 105.0],
        send_results=[True],
        events=events,
    )

    assert events == ["find", "send", "complete", "persist"]
