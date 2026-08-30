import copy
import json

import pytest

from feishu_bridge import interconnect_watcher as interconnect


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


def load_json_dict(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def save_json_dict(path, values):
    path.write_text(json.dumps(values, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return True


def as_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_label(value):
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def audit_port_key(value):
    return normalize_label(value)


def make_watcher(
    state_file,
    *,
    load_state=load_json_dict,
    save_state=save_json_dict,
    send=lambda _card: True,
    find_merge=lambda _event, _now: None,
    complete_merge=lambda _event, _cause, _now: None,
    sleep=lambda _seconds: None,
    now=lambda: 0.0,
    logs=None,
):
    logs = logs if logs is not None else []
    return interconnect.InterconnectWatcher(
        enabled=True,
        alert_for_seconds=5,
        poll_interval=5,
        jobs="infra-switch-ifmib",
        port_filter="port-channel,po",
        state_file=state_file,
        prometheus_query=lambda _query: [],
        fetch_name_cache=lambda: {},
        load_topology_edges=lambda: [],
        load_json_dict=load_state,
        save_json_dict=save_state,
        as_float=as_float,
        normalize_label=normalize_label,
        audit_port_key=audit_port_key,
        build_card=lambda event, recovered=False: {
            "event": copy.deepcopy(event),
            "recovered": recovered,
        },
        send=send,
        find_merge_candidate=find_merge,
        complete_merge=complete_merge,
        mark_watcher_health=lambda *_args: None,
        log=logs.append,
        sleep=sleep,
        now=now,
    )


def read_state(path):
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def run_watcher(
    tmp_path,
    batches,
    times,
    *,
    initial=None,
    send_results=(),
    events=None,
    save_state=save_json_dict,
    find_merge=None,
    complete_merge=None,
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

    def send(card):
        events.append("send")
        sent.append(copy.deepcopy(card))
        return next(results, True)

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= len(batches) + 1:
            raise StopWatcher

    watcher = make_watcher(
        state_file,
        send=send,
        save_state=save_state,
        find_merge=find_merge or (lambda _event, _now: None),
        complete_merge=complete_merge or (lambda _event, _cause, _now: None),
        sleep=sleep,
        now=lambda: next(clock),
        logs=logs,
    )
    watcher.fetch_interconnect_ports = lambda _jobs: next(pending_batches)

    with pytest.raises(StopWatcher):
        watcher.run()

    return {
        "state_file": state_file,
        "state": read_state(state_file),
        "sent": sent,
        "sleeps": sleeps,
        "logs": logs,
        "events": events,
    }


def test_clean_start_loads_empty_state_before_original_startup_sleep(tmp_path):
    events = []
    state_file = tmp_path / "missing.json"
    watcher = make_watcher(
        state_file,
        load_state=lambda path: events.append(("load", path)) or {},
        sleep=lambda seconds: events.append(("sleep", seconds)) or (_ for _ in ()).throw(StopWatcher()),
    )

    with pytest.raises(StopWatcher):
        watcher.run()

    assert events == [("load", state_file), ("sleep", 25)]


def test_new_down_waits_for_hold_then_persists_only_after_success(tmp_path):
    observed = run_watcher(
        tmp_path,
        [[interconnect_port("down")], [interconnect_port("down")]],
        [100.0, 105.0],
        send_results=[True],
    )

    assert len(observed["sent"]) == 1
    assert observed["sent"][0]["recovered"] is False
    assert observed["sent"][0]["event"]["duration"] == 5
    assert observed["sleeps"] == [25, 5, 5]
    assert observed["state"][interconnect_port()["key"]] == {
        "alerting": True,
        "down_members": ["Te1/0/4", "Te2/0/4"],
        "down_since": 100.0,
        "last_port": interconnect_port("down"),
        "peer_switch": "",
    }


def test_alert_send_failure_does_not_persist_and_next_poll_retries(tmp_path):
    saves = []

    def record_save(path, values):
        saves.append(copy.deepcopy(values))
        return save_json_dict(path, values)

    observed = run_watcher(
        tmp_path,
        [
            [interconnect_port("down")],
            [interconnect_port("down")],
            [interconnect_port("down")],
        ],
        [100.0, 105.0, 110.0],
        send_results=[False, True],
        save_state=record_save,
    )

    assert len(observed["sent"]) == 2
    assert [card["event"]["duration"] for card in observed["sent"]] == [5, 10]
    assert len(saves) == 1
    assert saves[0][interconnect_port()["key"]]["alerting"] is True


def test_restart_same_active_down_does_not_resend_alert(tmp_path):
    observed = run_watcher(
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
    tmp_path, persisted_status, current_status,
):
    observed = run_watcher(
        tmp_path,
        [[interconnect_port(current_status)]],
        [200.0],
        initial=active_state(persisted_status),
    )

    assert observed["sent"] == []


def test_restored_alert_healthy_recovery_success_clears_persistence(tmp_path):
    observed = run_watcher(
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


def test_recovery_send_failure_retains_persisted_active_alert(tmp_path):
    initial = active_state("down", down_since=100.0)
    observed = run_watcher(
        tmp_path,
        [[interconnect_port("healthy")]],
        [200.0],
        initial=initial,
        send_results=[False],
    )

    assert len(observed["sent"]) == 1
    assert observed["sent"][0]["recovered"] is True
    assert observed["state"] == initial


def test_restored_vanished_alert_uses_original_hold_and_clears_after_recovery(tmp_path):
    observed = run_watcher(
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


def test_corrupt_missing_and_non_active_state_follow_generic_loader_behavior(tmp_path):
    state_file = tmp_path / "interconnect-alerts.json"

    assert interconnect.load_interconnect_alert_states(state_file, load_json_dict, as_float) == {}
    state_file.write_text("{broken", encoding="utf-8")
    assert interconnect.load_interconnect_alert_states(state_file, load_json_dict, as_float) == {}
    state_file.write_text(json.dumps({"key": {"alerting": False}}), encoding="utf-8")
    assert interconnect.load_interconnect_alert_states(state_file, load_json_dict, as_float) == {}


def test_alert_keeps_syslog_merge_order_before_active_state_persistence(tmp_path):
    events = []

    def save(path, values):
        events.append("persist")
        return save_json_dict(path, values)

    run_watcher(
        tmp_path,
        [[interconnect_port("down")], [interconnect_port("down")]],
        [100.0, 105.0],
        send_results=[True],
        events=events,
        save_state=save,
        find_merge=lambda _event, _now: events.append("find") or {"id": "cause-1"},
        complete_merge=lambda _event, _cause, _now: events.append("complete"),
    )

    assert events == ["find", "send", "complete", "persist"]


def test_send_failure_does_not_complete_merge_or_persist_active_state(tmp_path):
    events = []
    run_watcher(
        tmp_path,
        [[interconnect_port("down")], [interconnect_port("down")]],
        [100.0, 105.0],
        send_results=[False],
        events=events,
        save_state=lambda _path, _values: events.append("persist"),
        find_merge=lambda _event, _now: events.append("find") or {"id": "cause-1"},
        complete_merge=lambda _event, _cause, _now: events.append("complete"),
    )

    assert events == ["find", "send"]
