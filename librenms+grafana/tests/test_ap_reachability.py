import json

import pytest

import ap_reachability as shadow


AP = {"ip": "192.0.2.15", "mac": "02:00:00:00:00:15"}


def series(name, value, stamp=97, ip=AP["ip"], **labels):
    return {
        "metric": {"__name__": name, "job": "infra-ap-ping",
                   "target_ip": ip, "instance": ip, **labels},
        "values": [[stamp, str(value)]],
    }


def build(rows, *, online="unknown", published_at=90, aps=None):
    return shadow.build_artifact(
        [AP] if aps is None else aps, {AP["mac"]: {"status": online}}, rows,
        published_at=published_at, now=100,
    )


def test_icmp_reachability_and_online_evidence_are_independent():
    evidence = build(
        [series("probe_success", 1), series("up", 1),
         series("probe_icmp_duration_seconds", 0.0015, phase="rtt")],
        online="offline",
    )
    row, = evidence["aps"]
    assert evidence["schema_version"] == 2
    assert row["online"]["status"] == "offline"
    assert row["reachability"] == {
        "status": "reachable", "source": shadow.SOURCE, "reason": "icmp_reply",
        "sample_at": 97.0, "valid_until": 117.0, "rtt_ms": 1.5,
    }
    assert row["conflict"] == "online_and_icmp_disagree"


def test_probe_failure_from_current_scrape_keeps_online_unknown():
    row, = build([series("probe_success", 0), series("up", 1)])["aps"]
    assert row["reachability"]["status"] == "unreachable"
    assert row["online"]["status"] == "unknown"
    assert row["conflict"] is None


@pytest.mark.parametrize("rows,published_at", [
    ([series("probe_success", 1, stamp=79), series("up", 1, stamp=79)], 50),
    ([series("probe_success", 1), series("up", 1)], 98),
    ([series("probe_success", 1), series("up", 0, stamp=99)], 90),
    ([series("probe_success", 1), series("up", 1, stamp=99)], 90),
    ([series("probe_success", 1), series("up", 1), series("up", 1)], 90),
    ([series("probe_success", 1, ip="192.0.2.16"), series("up", 1)], 90),
    ([series("probe_success", 1, stamp=101), series("up", 1, stamp=101)], 90),
    ([], 90),
])
def test_missing_stale_or_ambiguous_samples_are_unknown(rows, published_at):
    row, = build(rows, published_at=published_at)["aps"]
    assert row["reachability"]["status"] == "unknown"
    assert row["reachability"]["sample_at"] is None


def test_duplicate_ip_or_mac_cannot_borrow_probe_results():
    samples = [series("probe_success", 1), series("up", 1)]
    assert not build(samples, aps=[AP, {**AP, "mac": "02:00:00:00:00:16"}])["aps"]
    assert not build(samples, aps=[AP, {**AP, "ip": "192.0.2.16"}])["aps"]


def test_failed_query_or_unpublished_targets_replace_shadow_with_unknown(tmp_path, monkeypatch):
    file = tmp_path / "shadow.json"
    file.write_text('{"old":true}', encoding="utf-8")

    def fail():
        raise TimeoutError("service unavailable")

    monkeypatch.setattr(shadow, "fetch_probe_series", fail)
    for publication, expected in [(None, "ping_targets_unavailable"),
                                  (90, "prometheus_unavailable")]:
        shadow.write_shadow(file, [AP], {}, publication)
        result = json.loads(file.read_text(encoding="utf-8"))
        assert result["valid"] is False
        assert result["invalid_reason"] == expected
        assert result["aps"][0]["reachability"]["status"] == "unknown"
