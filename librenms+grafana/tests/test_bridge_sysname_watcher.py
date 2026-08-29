import copy
import ipaddress

import pytest

from bridge_sysname_watcher import SysnameChangeWatcher


class StopWatcher(BaseException):
    pass


def meaningful_sysname(value):
    text = str(value or "").strip()
    if not text or text.isdigit():
        return ""
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return text
    return ""


def sysname_changed(old_name, new_name):
    old_text = meaningful_sysname(old_name)
    new_text = meaningful_sysname(new_name)
    return bool(old_text and new_text and old_text.casefold() != new_text.casefold())


def device(device_id, sys_name, *, ip="192.0.2.1", hostname="switch.example"):
    return {
        "device_id": device_id,
        "sysName": sys_name,
        "ip": ip,
        "hostname": hostname,
    }


def state(name="old-name", *, ip="192.0.2.1", hostname="switch.example"):
    return {"sysName": name, "ip": ip, "hostname": hostname}


def make_watcher(*, batches=None, token_values=None, **overrides):
    batches = list(batches or [])
    tokens = iter(token_values or (["token"] * max(1, len(batches))))
    pending_batches = iter(batches)
    events = overrides.pop("events", [])
    initial_state = copy.deepcopy(overrides.pop("initial_state", {}))
    loads = overrides.pop("loads", [])
    saves = overrides.pop("saves", [])
    logs = overrides.pop("logs", [])
    health = overrides.pop("health", [])
    cards = overrides.pop("cards", [])
    sent = overrides.pop("sent", [])
    send_results = iter(overrides.pop("send_results", []))

    def load_state(path):
        events.append(f"load:{path}")
        loads.append(path)
        return copy.deepcopy(initial_state)

    def save_state(path, snapshot):
        events.append(f"save:{path}")
        saves.append(copy.deepcopy(snapshot))

    def get_token():
        value = next(tokens)
        events.append(f"token:{value}")
        return value

    def fetch_devices(token):
        events.append(f"fetch:{token}")
        value = next(pending_batches)
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    def build_card(old_name, new_name, *, ip, hostname):
        card = {
            "old_name": old_name,
            "new_name": new_name,
            "ip": ip,
            "hostname": hostname,
        }
        cards.append(copy.deepcopy(card))
        return card

    def send(card):
        sent.append(copy.deepcopy(card))
        return next(send_results, True)

    def mark_watcher_health(*args):
        events.append(f"health:{args[0]}:{args[1]}")
        health.append(args)

    options = {
        "enabled": True,
        "librenms_url": "http://librenms",
        "poll_interval": 60,
        "confirm_polls": 2,
        "state_file": "/state/device-sysnames.json",
        "load_state": load_state,
        "save_state": save_state,
        "get_token": get_token,
        "fetch_devices": fetch_devices,
        "meaningful_sysname": meaningful_sysname,
        "sysname_changed": sysname_changed,
        "build_card": build_card,
        "send": send,
        "mark_watcher_health": mark_watcher_health,
        "log": logs.append,
    }
    options.update(overrides)
    watcher = SysnameChangeWatcher(**options)
    return watcher, {
        "events": events,
        "loads": loads,
        "saves": saves,
        "logs": logs,
        "health": health,
        "cards": cards,
        "sent": sent,
    }


def run_polls(watcher, poll_count, *, events=None):
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if events is not None:
            events.append(f"sleep:{seconds}")
        if len(sleeps) >= poll_count + 1:
            raise StopWatcher

    watcher.sleep = sleep
    with pytest.raises(StopWatcher):
        watcher.run()
    return sleeps


def test_disabled_guard_returns_without_sleeping_or_loading():
    sleeps = []
    watcher, observed = make_watcher(enabled=False, sleep=sleeps.append)

    assert watcher.run() is None
    assert sleeps == []
    assert observed["loads"] == []
    assert observed["logs"] == ["[SYSNAME] sysName change watcher disabled"]


def test_missing_librenms_url_guard_returns_without_sleeping_or_loading():
    sleeps = []
    watcher, observed = make_watcher(librenms_url="", sleep=sleeps.append)

    assert watcher.run() is None
    assert sleeps == []
    assert observed["loads"] == []
    assert observed["logs"] == [
        "[SYSNAME] LIBRENMS_URL not set, sysName change watcher disabled"
    ]


def test_startup_sleep_is_exactly_thirty_seconds_and_precedes_state_load():
    events = []
    watcher, observed = make_watcher(batches=[[]], events=events)

    sleeps = run_polls(watcher, 1, events=events)

    assert sleeps == [30, 60]
    assert events.index("sleep:30") < events.index("load:/state/device-sysnames.json")
    assert observed["loads"] == ["/state/device-sysnames.json"]


def test_empty_snapshot_seeds_silently_and_logs_baseline_once():
    watcher, observed = make_watcher(batches=[[
        device(1, "core-1"),
    ], [
        device(1, "core-1"),
    ]])

    sleeps = run_polls(watcher, 2)

    expected = {"1": state("core-1")}
    assert sleeps == [30, 60, 60]
    assert observed["saves"] == [expected, expected]
    assert observed["cards"] == []
    assert observed["sent"] == []
    assert sum("baseline recorded" in line for line in observed["logs"]) == 1


def test_existing_snapshot_is_seeded_and_confirm_polls_one_alerts_immediately():
    watcher, observed = make_watcher(
        initial_state={"1": state()},
        batches=[[device(1, "new-name")]],
        confirm_polls=1,
    )

    run_polls(watcher, 1)

    assert observed["sent"] == [{
        "old_name": "old-name",
        "new_name": "new-name",
        "ip": "192.0.2.1",
        "hostname": "switch.example",
    }]
    assert observed["saves"] == [{"1": state("new-name")}]
    assert not any("baseline recorded" in line for line in observed["logs"])


def test_device_id_identity_skip_and_ip_field_precedence_are_preserved():
    watcher, observed = make_watcher(batches=[[
        device(None, "ignored"),
        device(7, "seven", ip="", hostname="host-seven"),
        device(8, "eight", ip="198.51.100.8", hostname="host-eight"),
        device(9, "nine", ip="", hostname=""),
    ]])

    run_polls(watcher, 1)

    assert observed["saves"] == [{
        "7": state("seven", ip="host-seven", hostname="host-seven"),
        "8": state("eight", ip="198.51.100.8", hostname="host-eight"),
        "9": state("nine", ip="", hostname=""),
    }]


@pytest.mark.parametrize("invalid_name", ["2", "192.168.71.8"])
def test_invalid_sysname_preserves_previous_baseline(invalid_name):
    old = state()
    watcher, observed = make_watcher(
        initial_state={"1": old},
        batches=[[device(1, invalid_name)]],
    )

    run_polls(watcher, 1)

    assert observed["saves"] == [{"1": old}]
    assert observed["sent"] == []


def test_invalid_sysname_cancels_pending_candidate():
    old = state()
    watcher, observed = make_watcher(
        initial_state={"1": old},
        batches=[
            [device(1, "new-name")],
            [device(1, "2")],
            [device(1, "new-name")],
            [device(1, "new-name")],
        ],
    )

    run_polls(watcher, 4)

    assert observed["saves"][:3] == [{"1": old}, {"1": old}, {"1": old}]
    assert len(observed["sent"]) == 1
    assert observed["saves"][-1] == {"1": state("new-name")}


def test_case_only_change_is_ignored_and_latest_case_is_saved():
    watcher, observed = make_watcher(
        initial_state={"1": state("AVL")},
        batches=[[device(1, "avl")]],
    )

    run_polls(watcher, 1)

    assert observed["sent"] == []
    assert observed["saves"] == [{"1": state("avl")}]


def test_same_candidate_confirms_at_exact_boundary_and_advances_snapshot():
    old = state()
    watcher, observed = make_watcher(
        initial_state={"1": old},
        batches=[[device(1, "new-name")], [device(1, "new-name")], [device(1, "new-name")]],
    )

    run_polls(watcher, 3)

    assert observed["saves"] == [
        {"1": old},
        {"1": state("new-name")},
        {"1": state("new-name")},
    ]
    assert len(observed["sent"]) == 1
    assert "old-name -> new-name (1/2)" in observed["logs"][1]


def test_different_candidate_resets_confirmation_count():
    old = state()
    watcher, observed = make_watcher(
        initial_state={"1": old},
        batches=[
            [device(1, "candidate-a")],
            [device(1, "candidate-b")],
            [device(1, "candidate-b")],
        ],
    )

    run_polls(watcher, 3)

    assert [line for line in observed["logs"] if "candidate device_id" in line] == [
        "[SYSNAME] candidate device_id=1 old-name -> candidate-a (1/2)",
        "[SYSNAME] candidate device_id=1 old-name -> candidate-b (1/2)",
    ]
    assert [card["new_name"] for card in observed["sent"]] == ["candidate-b"]


def test_candidate_reverting_to_old_name_is_cancelled():
    old = state()
    watcher, observed = make_watcher(
        initial_state={"1": old},
        batches=[
            [device(1, "new-name")],
            [device(1, "old-name")],
            [device(1, "new-name")],
        ],
    )

    run_polls(watcher, 3)

    assert observed["sent"] == []
    assert observed["saves"] == [{"1": old}, {"1": old}, {"1": old}]


def test_send_failure_keeps_old_snapshot_and_retries_next_successful_poll():
    old = state()
    watcher, observed = make_watcher(
        initial_state={"1": old},
        batches=[[device(1, "new-name")]] * 3,
        send_results=[False, True],
    )

    run_polls(watcher, 3)

    assert len(observed["sent"]) == 2
    assert observed["saves"] == [
        {"1": old},
        {"1": old},
        {"1": state("new-name")},
    ]


def test_token_unavailable_marks_unhealthy_without_fetch_or_save():
    watcher, observed = make_watcher(
        initial_state={"1": state()},
        batches=[],
        token_values=[""],
    )

    sleeps = run_polls(watcher, 1)

    assert sleeps == [30, 60]
    assert observed["health"] == [
        ("sysname-change", False, "LibreNMS token unavailable")
    ]
    assert observed["saves"] == []
    assert not any(event.startswith("fetch:") for event in observed["events"])


def test_fetch_failure_marks_unhealthy_and_preserves_pending_for_retry():
    old = state()
    watcher, observed = make_watcher(
        initial_state={"1": old},
        batches=[
            [device(1, "new-name")],
            RuntimeError("LibreNMS down"),
            [device(1, "new-name")],
        ],
    )

    sleeps = run_polls(watcher, 3)

    assert sleeps == [30, 60, 60, 60]
    assert observed["health"][0] == ("sysname-change", True)
    assert observed["health"][1][0:2] == ("sysname-change", False)
    assert str(observed["health"][1][2]) == "LibreNMS down"
    assert observed["health"][2] == ("sysname-change", True)
    assert observed["saves"] == [{"1": old}, {"1": state("new-name")}]
    assert len(observed["sent"]) == 1


def test_success_health_is_recorded_before_device_processing_and_save():
    events = []
    watcher, _observed = make_watcher(batches=[[]], events=events)

    run_polls(watcher, 1, events=events)

    assert events.index("health:sysname-change:True") < events.index(
        "save:/state/device-sysnames.json"
    )


def test_snapshot_replacement_removes_absent_devices():
    watcher, observed = make_watcher(
        initial_state={"1": state("one"), "2": state("two")},
        batches=[[device(1, "one")]],
    )

    run_polls(watcher, 1)

    assert observed["saves"] == [{"1": state("one")}]


def test_removed_then_readded_with_new_device_id_does_not_false_alert():
    watcher, observed = make_watcher(
        initial_state={"1": state("old-name")},
        batches=[[], [device(2, "new-name")]],
    )

    run_polls(watcher, 2)

    assert observed["sent"] == []
    assert observed["saves"] == [{}, {"2": state("new-name")}]


def test_every_successful_poll_saves_even_when_snapshot_is_unchanged():
    existing = {"1": state()}
    watcher, observed = make_watcher(
        initial_state=existing,
        batches=[[device(1, "old-name")], [device(1, "old-name")]],
    )

    run_polls(watcher, 2)

    assert observed["saves"] == [existing, existing]
    assert observed["health"] == [
        ("sysname-change", True),
        ("sysname-change", True),
    ]
