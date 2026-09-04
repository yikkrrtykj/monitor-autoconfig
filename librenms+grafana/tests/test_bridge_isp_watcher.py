import copy
import json

import pytest

from feishu_bridge.isp_watcher import (
    IspBandwidthWatcher,
    _bandwidth_for_label,
    _counter_glitch_limit_bps,
    _dedupe_wan_labels,
    _is_wan_port,
    _parse_bandwidth_config,
)


class StopWatcher(BaseException):
    pass


def normalize_label(value):
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def rate(value_bps, *, label="telecom", direction="in", if_index="1",
         target_ip="192.0.2.1"):
    return {
        "key": f"{label}|{direction}",
        "label": label,
        "direction": direction,
        "value_bps": value_bps,
        "if_index": if_index,
        "target_ip": target_ip,
    }


def make_watcher(**overrides):
    logs = overrides.pop("logs", [])
    health = overrides.pop("health", [])
    sent = overrides.pop("sent", [])
    send_results = iter(overrides.pop("send_results", []))

    def build_bandwidth_card(event, recovered=False):
        return {"kind": "bandwidth", "event": copy.deepcopy(event), "recovered": recovered}

    def build_data_missing_card(duration, recovered=False):
        return {"kind": "missing", "duration": duration, "recovered": recovered}

    def send(card):
        sent.append(copy.deepcopy(card))
        return next(send_results, True)

    options = {
        "enabled": True,
        "alert_for_seconds": 10,
        "poll_interval": 5,
        "rate_window": "1m",
        "resolve_seconds": 30,
        "status_interval": 30,
        "spike_ignore_factor": 5,
        "data_missing_alert_seconds": 120,
        "wan_filter": "telecom,WAN,eth0,eth1",
        "bandwidth_config": "*:100",
        "saturation_percent": 90,
        "prometheus_url": "http://prometheus:9090",
        "prometheus_query": lambda _query: [],
        "normalize_label": normalize_label,
        "format_bps": lambda value: f"{value:g}bps",
        "build_bandwidth_card": build_bandwidth_card,
        "build_data_missing_card": build_data_missing_card,
        "send": send,
        "mark_watcher_health": lambda *args: health.append(args),
        "log": logs.append,
    }
    options.update(overrides)
    return IspBandwidthWatcher(**options), logs, health, sent


def run_polls(watcher, rate_batches, times):
    batches = iter(rate_batches)
    clock = iter(times)
    sleeps = []

    def fetch():
        value = next(batches)
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= len(rate_batches) + 1:
            raise StopWatcher

    watcher._fetch_wan_rates = fetch
    watcher.time = lambda: next(clock)
    watcher.sleep = sleep
    with pytest.raises(StopWatcher):
        watcher.run()
    return sleeps


def test_disabled_watcher_logs_and_returns_without_sleeping():
    sleeps = []
    watcher, logs, health, sent = make_watcher(enabled=False, sleep=sleeps.append)

    assert watcher.run() is None
    assert sleeps == []
    assert health == []
    assert sent == []
    assert logs == ["[ISP] realtime bandwidth watcher disabled"]


def test_startup_wait_and_poll_cadence_are_unchanged():
    watcher, _logs, health, _sent = make_watcher()

    sleeps = run_polls(watcher, [[]], [100])

    assert sleeps == [30, 5]
    assert health == [("isp-bandwidth", True)]


def test_poll_failure_marks_unhealthy_then_waits_one_poll_interval():
    watcher, logs, health, _sent = make_watcher()

    sleeps = run_polls(watcher, [RuntimeError("prometheus down")], [100])

    assert sleeps == [30, 5]
    assert len(health) == 1
    assert health[0][0:2] == ("isp-bandwidth", False)
    assert str(health[0][2]) == "prometheus down"
    assert "[ISP] poll failed: prometheus down" in logs


def test_fetch_wan_rates_uses_exact_promql_and_extracts_both_directions():
    queries = []

    def prometheus_query(query):
        queries.append(query)
        if "ifHCInOctets" in query:
            return [
                {"metric": {"ifAlias": "WAN1", "ifName": "ignored", "ifIndex": "7", "target_ip": "10.0.0.1"},
                 "value": [1, "123.5"]},
                {"metric": {"ifName": "eth10"}, "value": [1, "999"]},
                {"metric": {"ifName": "eth1"}, "value": [1, "bad"]},
                {"metric": {"ifName": "eth1"}, "value": [1, "-1"]},
            ]
        return [
            {"metric": {"ifName": "eth1", "ifIndex": "8", "instance": "10.0.0.2"},
             "value": [1, "456"]},
        ]

    watcher, _logs, _health, _sent = make_watcher(prometheus_query=prometheus_query)

    rates = watcher._fetch_wan_rates()

    assert queries == [
        'rate(ifHCInOctets{job="firewall-snmp"}[1m]) * 8',
        'rate(ifHCOutOctets{job="firewall-snmp"}[1m]) * 8',
    ]
    assert rates == [
        {
            "key": "WAN1|in", "label": "WAN1", "direction": "in",
            "value_bps": 123.5, "if_index": "7", "target_ip": "10.0.0.1",
            "metric_name": "WAN1",
        },
        {
            "key": "eth1|out", "label": "eth1", "direction": "out",
            "value_bps": 456.0, "if_index": "8", "target_ip": "10.0.0.2",
            "metric_name": "eth1",
        },
    ]


def test_wan_keyword_digit_suffix_requires_boundary():
    wan_filter = "telecom,WAN,eth0,eth1"
    assert _is_wan_port("eth0", wan_filter) is True
    assert _is_wan_port("eth1", wan_filter) is True
    assert _is_wan_port("eth10", wan_filter) is False
    assert _is_wan_port("eth15", wan_filter) is False
    assert _is_wan_port("WAN1", wan_filter) is True
    assert _is_wan_port("telecom-200M", wan_filter) is True
    assert _is_wan_port("lan-port", wan_filter) is False


def test_bandwidth_config_uses_exact_names_or_global_fallback():
    cfg = _parse_bandwidth_config(
        "*:1000/300,__link_1:200/100,__link_2:500/250", normalize_label,
    )
    assert _bandwidth_for_label("__LINK_1", "in", cfg, normalize_label) == 200
    assert _bandwidth_for_label("__LINK_1", "out", cfg, normalize_label) == 100
    assert _bandwidth_for_label("eth0", "in", cfg, normalize_label) == 1000
    assert _bandwidth_for_label("eth1", "out", cfg, normalize_label) == 300
    assert _bandwidth_for_label("unknown", "out", cfg, normalize_label) == 300
    single = _parse_bandwidth_config("800", normalize_label)
    assert _bandwidth_for_label("unknown", "in", single, normalize_label) == 800


def test_bandwidth_names_do_not_use_substring_or_punctuation_fuzzy_matching():
    cfg = _parse_bandwidth_config("电信:500/100,电信2:200/50", normalize_label)

    assert _bandwidth_for_label("电信2", "in", cfg, normalize_label) == 200
    assert _bandwidth_for_label("电信2", "out", cfg, normalize_label) == 50
    assert _bandwidth_for_label("电信1", "in", cfg, normalize_label) == 1000
    assert _bandwidth_for_label("电信-2", "in", cfg, normalize_label) == 1000


def test_duplicate_wan_labels_do_not_manufacture_ifindex_ordered_identity():
    rates = _dedupe_wan_labels([
        rate(0, label="电信", if_index="7"),
        rate(0, label="电信", if_index="3"),
        rate(0, label="电信", if_index="7", direction="out"),
        rate(0, label="电信", if_index="3", direction="out"),
        rate(0, label="联通", if_index="5"),
    ])

    telecom = [item for item in rates if item["label"] == "电信"]
    assert len({item["key"] for item in telecom}) == 4
    assert all(item["_identity_ambiguous"] is True for item in telecom)
    assert next(item for item in rates if item["label"] == "联通")["key"] == "联通|in"


def test_duplicate_wan_labels_without_ifindex_remain_distinct_but_ambiguous():
    rates = _dedupe_wan_labels([
        rate(0, label="电信", if_index=None),
        rate(0, label="电信", if_index=None),
        rate(0, label="电信", if_index=None, direction="out"),
        rate(0, label="电信", if_index=None, direction="out"),
    ])

    assert len({item["key"] for item in rates}) == 4
    assert {item["label"] for item in rates} == {"电信"}
    assert all(item["_identity_ambiguous"] is True for item in rates)


def test_same_label_and_ifindex_on_different_targets_are_distinct_and_ambiguous():
    rates = _dedupe_wan_labels([
        rate(0, label="WAN", if_index="1", target_ip="192.0.2.1"),
        rate(0, label="WAN", if_index="1", target_ip="192.0.2.2"),
    ])

    assert len({item["key"] for item in rates}) == 2
    assert all(item["_identity_ambiguous"] is True for item in rates)


def test_inventory_metric_name_maps_native_series_to_display_identity(tmp_path):
    inventory = tmp_path / "isp.json"
    inventory.write_text(json.dumps([{
        "targets": ["203.0.113.1"],
        "labels": {
            "display_name": "ISP-A",
            "metric_name": "eth1",
            "wan_ip": "203.0.113.2",
        },
    }]), encoding="utf-8")

    def prometheus_query(query):
        direction = "in" if "ifHCInOctets" in query else "out"
        return [{
            "metric": {"ifName": "eth1", "ifIndex": "7", "target_ip": "192.0.2.1"},
            "value": [1, "10" if direction == "in" else "20"],
        }]

    watcher, _logs, _health, _sent = make_watcher(
        prometheus_query=prometheus_query,
        inventory_file=str(inventory),
        bandwidth_config="*:1000,ISP-A:200",
    )
    rates = watcher._fetch_wan_rates()

    assert {item["label"] for item in rates} == {"ISP-A"}
    assert {item["metric_name"] for item in rates} == {"eth1"}
    assert _bandwidth_for_label("ISP-A", "in", _parse_bandwidth_config(
        "*:1000,ISP-A:200"
    )) == 200


def test_multiple_native_lines_for_one_inventory_display_name_use_global_bandwidth(tmp_path):
    inventory = tmp_path / "isp.json"
    inventory.write_text(json.dumps([
        {"targets": ["203.0.113.1"], "labels": {
            "display_name": "ISP-A", "metric_name": "eth0",
        }},
        {"targets": ["203.0.113.9"], "labels": {
            "display_name": "ISP-A", "metric_name": "eth1",
        }},
    ]), encoding="utf-8")

    def prometheus_query(_query):
        return [
            {"metric": {"ifName": "eth0", "ifIndex": "1", "target_ip": "192.0.2.1"},
             "value": [1, "10"]},
            {"metric": {"ifName": "eth1", "ifIndex": "1", "target_ip": "192.0.2.2"},
             "value": [1, "20"]},
        ]

    watcher, _logs, _health, _sent = make_watcher(
        prometheus_query=prometheus_query,
        inventory_file=str(inventory),
        bandwidth_config="*:1000,ISP-A:200",
    )
    rates = watcher._fetch_wan_rates()
    cfg = _parse_bandwidth_config("*:1000,ISP-A:200")

    assert {item["label"] for item in rates} == {"ISP-A"}
    assert all(item["_identity_ambiguous"] is True for item in rates)
    assert all(
        _bandwidth_for_label(
            item["label"], item["direction"], cfg,
            identity_ambiguous=item["_identity_ambiguous"],
        ) == 1000
        for item in rates
    )


def test_inventory_conflict_blocks_named_bandwidth_even_when_native_label_matches(tmp_path):
    inventory = tmp_path / "isp.json"
    inventory.write_text(json.dumps([{
        "targets": ["203.0.113.1"],
        "labels": {
            "display_name": "ISP-A",
            "metric_name": "ISP-A",
            "metadata_conflict": "true",
        },
    }]), encoding="utf-8")

    watcher, _logs, _health, _sent = make_watcher(
        prometheus_query=lambda _query: [{
            "metric": {"ifAlias": "ISP-A", "ifIndex": "1", "target_ip": "192.0.2.1"},
            "value": [1, "10"],
        }],
        inventory_file=str(inventory),
    )
    rates = watcher._fetch_wan_rates()
    cfg = _parse_bandwidth_config("*:1000,ISP-A:200")

    assert all(item["_identity_ambiguous"] is True for item in rates)
    assert all(
        _bandwidth_for_label(
            item["label"], item["direction"], cfg,
            identity_ambiguous=item["_identity_ambiguous"],
        ) == 1000
        for item in rates
    )


def test_ambiguous_duplicate_label_uses_global_bandwidth_not_named_override():
    cfg = _parse_bandwidth_config("*:1000/300,电信:100/50", normalize_label)

    assert _bandwidth_for_label(
        "电信", "in", cfg, normalize_label, identity_ambiguous=True,
    ) == 1000
    assert _bandwidth_for_label(
        "电信", "out", cfg, normalize_label, identity_ambiguous=True,
    ) == 300


def test_counter_glitch_limit_and_disabled_behavior():
    assert _counter_glitch_limit_bps(200, 5) == 1_000_000_000
    assert _counter_glitch_limit_bps(200, 0) is None
    assert _counter_glitch_limit_bps(0, 5) is None
    assert _counter_glitch_limit_bps("bad", 5) is None


def test_counter_glitch_does_not_advance_alert_state():
    watcher, logs, _health, sent = make_watcher(alert_for_seconds=0)

    run_polls(watcher, [[rate(600_000_000)], [rate(600_000_000)]], [100, 200])

    assert sent == []
    assert sum("ignore counter glitch" in message for message in logs) == 2


def test_disabling_glitch_filter_allows_same_sample_to_alert():
    watcher, _logs, _health, sent = make_watcher(
        alert_for_seconds=0, spike_ignore_factor=0,
    )

    run_polls(watcher, [[rate(600_000_000)]], [100])

    assert len(sent) == 1
    assert sent[0]["kind"] == "bandwidth"
    assert sent[0]["recovered"] is False


def test_sustained_alert_retries_until_send_succeeds_then_stops_repeating():
    high = [rate(95_000_000)]
    watcher, _logs, _health, sent = make_watcher(send_results=[False, True])

    run_polls(watcher, [high, high, high, high], [100, 110, 115, 120])

    assert len(sent) == 2
    assert [item["event"]["duration"] for item in sent] == [10, 15]
    assert all(item["recovered"] is False for item in sent)


def test_recovery_debounce_duration_and_failed_send_retains_alert():
    high = [rate(95_000_000)]
    low = [rate(10_000_000)]
    watcher, _logs, _health, sent = make_watcher(send_results=[True, False, True])

    run_polls(
        watcher,
        [high, high, low, low, low, low],
        [100, 110, 120, 150, 155, 160],
    )

    assert [item["recovered"] for item in sent] == [False, True, True]
    assert sent[0]["event"]["duration"] == 10
    assert sent[1]["event"]["duration"] == 50
    assert sent[2]["event"]["duration"] == 55


def test_missing_series_never_recovers_an_active_alert():
    high = [rate(95_000_000)]
    low = [rate(10_000_000)]
    watcher, _logs, _health, sent = make_watcher(send_results=[True, True])

    run_polls(
        watcher,
        [high, high, [], [], low, low],
        [100, 110, 120, 150, 160, 190],
    )

    assert [item["recovered"] for item in sent] == [False, True]
    assert sent[1]["event"]["duration"] == 90


def test_first_ever_empty_rates_never_start_data_missing_alert():
    watcher, _logs, _health, sent = make_watcher(data_missing_alert_seconds=20)

    run_polls(watcher, [[], [], []], [100, 130, 300])

    assert sent == []


def test_data_missing_alert_and_recovery_retry_without_early_bandwidth_evaluation():
    low = [rate(10_000_000)]
    high = [rate(95_000_000)]
    watcher, _logs, _health, sent = make_watcher(
        alert_for_seconds=0,
        data_missing_alert_seconds=20,
        send_results=[False, True, False, True, True],
    )

    run_polls(
        watcher,
        [low, [], [], [], high, high],
        [100, 110, 130, 135, 140, 145],
    )

    assert [(item["kind"], item["recovered"]) for item in sent] == [
        ("missing", False),
        ("missing", False),
        ("missing", True),
        ("missing", True),
        ("bandwidth", False),
    ]
    assert sent[0]["duration"] == 20
    assert sent[1]["duration"] == 25
    assert sent[2]["duration"] == 30
    assert sent[3]["duration"] == 35


def test_status_log_runs_on_original_interval_and_reports_no_rates():
    watcher, logs, _health, _sent = make_watcher(status_interval=30)

    run_polls(watcher, [[], [], []], [100, 120, 130])

    status_logs = [message for message in logs if "no WAN traffic series matched" in message]
    assert len(status_logs) == 2
    assert all("FIREWALL_WAN_IF_FILTER='telecom,WAN,eth0,eth1'" in message for message in status_logs)


def test_status_log_sorts_descending_and_limits_to_six_rows():
    watcher, logs, _health, _sent = make_watcher()
    rates = [rate(index * 1_000_000, label=f"WAN{index}", if_index=str(index)) for index in range(1, 8)]
    cfg = _parse_bandwidth_config("*:100", normalize_label)

    watcher._log_status(rates, cfg)

    message = logs[-1]
    assert message.startswith("[ISP] rates WAN7 in=")
    assert "WAN1 in=" not in message
    assert sum(f"WAN{index} in=" in message for index in range(1, 8)) == 6
