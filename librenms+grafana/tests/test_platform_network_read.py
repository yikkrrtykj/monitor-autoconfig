import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from platform_api import network_read


class FakeResponse:
    def __init__(self, payload):
        self.raw = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit=-1):
        return self.raw if limit < 0 else self.raw[:limit]


class FakeLibreNMS:
    def __init__(self, devices=None, error=None):
        self.devices = [] if devices is None else devices
        self.error = error
        self.strict = None

    def list_devices(self, strict=False):
        self.strict = strict
        if self.error:
            raise self.error
        return list(self.devices)


def make_context(tmp_path: Path, *, devices=None, prom=None, now=2_000.0):
    topology = tmp_path / "edges.json"
    inventory = tmp_path / "isp_targets.json"
    state = tmp_path / "isp-discovery-state.json"
    env_path = tmp_path / ".env"
    topology.write_text("[]", encoding="utf-8")
    inventory_raw = b"[]"
    inventory.write_bytes(inventory_raw)
    state.write_text(json.dumps({
        "status": "disabled",
        "inventory_count": 0,
        "inventory_sha256": hashlib.sha256(inventory_raw).hexdigest(),
    }), encoding="utf-8")
    env_path.write_text("", encoding="utf-8")
    client = FakeLibreNMS(devices=devices)
    prom_payload = prom if prom is not None else {
        "status": "success", "data": {"result": []},
    }
    context = network_read.NetworkReadContext(
        librenms_client_factory=lambda: client,
        prometheus_url="http://prometheus:9090",
        topology_path=topology,
        isp_inventory_path=inventory,
        isp_state_path=state,
        env_path=env_path,
        urlopen=lambda *_args, **_kwargs: FakeResponse(prom_payload),
        clock=lambda: now,
        topology_stale_seconds=300,
    )
    return context, client


def test_devices_require_strict_inventory_and_map_probe_status(tmp_path):
    context, client = make_context(
        tmp_path,
        devices=[{
            "device_id": 7,
            "hostname": "192.0.2.7",
            "ip": "192.0.2.7",
            "sysName": "edge-7",
        }],
        prom={
            "status": "success",
            "data": {"result": [{
                "metric": {"target_ip": "192.0.2.7", "job": "infra-dist-ping"},
                "value": [1, "1"],
            }]},
        },
    )

    payload = network_read.read_devices(context)

    assert client.strict is True
    assert payload == {
        "ok": True,
        "degraded": False,
        "count": 1,
        "devices": [{
            "id": 7,
            "name": "edge-7",
            "hostname": "192.0.2.7",
            "ip": "192.0.2.7",
            "status": "up",
        }],
        "warnings": [],
    }


def test_devices_prometheus_failure_is_degraded_with_unknown_status(tmp_path):
    context, _ = make_context(
        tmp_path,
        devices=[{"device_id": 1, "hostname": "192.0.2.1", "ip": "192.0.2.1"}],
    )
    context = network_read.NetworkReadContext(
        **{**context.__dict__, "urlopen": lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("down"))}
    )

    payload = network_read.read_devices(context)

    assert payload["degraded"] is True
    assert payload["devices"][0]["status"] == "unknown"


@pytest.mark.parametrize(
    ("vip_probe", "primary_probe", "backup_probe", "expected"),
    [
        (1, 1, 0, ["up", "up", "down"]),
        (0, 0, 1, ["down", "down", "up"]),
        (1, 0, None, ["up", "down", "unknown"]),
    ],
)
def test_devices_ha_vip_and_physical_units_keep_independent_probe_status(
    tmp_path, vip_probe, primary_probe, backup_probe, expected
):
    addresses = ("192.168.9.1", "192.168.9.11", "192.168.9.12")
    context, client = make_context(tmp_path, devices=[
        {"device_id": index, "hostname": ip, "ip": ip, "sysName": name}
        for index, (ip, name) in enumerate(zip(addresses, ("global-FW-VIP", "global-FW", "global-FW-backup")), 1)
    ])
    samples = [
        (addresses[0], "infra-fw-ping", vip_probe),
        (addresses[1], "infra-fw-unit-ping", primary_probe),
        (addresses[2], "infra-fw-unit-ping", backup_probe),
    ]
    observed_queries = []

    def filtered_prometheus(request, *, timeout):
        assert timeout == context.http_timeout
        query = parse_qs(urlsplit(request.full_url).query)["query"][0]
        observed_queries.append(query)
        match = re.search(r'job=~"([^"]+)"', query)
        assert match, query
        jobs = set(match.group(1).split("|"))
        return FakeResponse({"status": "success", "data": {"result": [
            {"metric": {"target_ip": ip, "job": job}, "value": [1, str(value)]}
            for ip, job, value in samples if value is not None and job in jobs
        ]}})

    context = network_read.NetworkReadContext(
        **{**context.__dict__, "urlopen": filtered_prometheus}
    )
    payload = network_read.read_devices(context)

    assert client.strict is True
    assert len(observed_queries) == 1
    assert payload["ok"] is True
    assert payload["degraded"] is False
    assert [device["ip"] for device in payload["devices"]] == list(addresses)
    assert [device["status"] for device in payload["devices"]] == expected


def test_topology_empty_snapshot_uses_snapshot_mtime(tmp_path):
    context, _ = make_context(tmp_path, now=2_000.0)
    os.utime(context.topology_path, (1_900.0, 1_900.0))

    payload = network_read.read_topology(context)

    assert payload["generatedAt"] == "1970-01-01T00:31:40Z"
    assert payload["nodeCount"] == 0
    assert payload["edgeCount"] == 0
    assert payload["stale"] is False


@pytest.mark.parametrize("content", [b"{", b"{}", b'["bad"]'])
def test_topology_malformed_is_explicit_failure(tmp_path, content):
    context, _ = make_context(tmp_path)
    context.topology_path.write_bytes(content)

    with pytest.raises(network_read.NetworkReadError) as raised:
        network_read.read_topology(context)

    assert raised.value.status == 503
    assert raised.value.payload["code"] == "topology_malformed"


@pytest.mark.parametrize("edge", [
    {"from_ip": [], "to_ip": "192.0.2.2"},
    {"from_ip": {}, "to_ip": "192.0.2.2"},
    {"from_ip": 123, "to_ip": "192.0.2.2"},
    {"from_ip": True, "to_ip": "192.0.2.2"},
    {"from_ip": "192.0.2.1", "to_ip": []},
    {"from_ip": "192.0.2.1", "to_ip": {}},
    {"from_ip": "192.0.2.1", "to_ip": 123},
    {"from_ip": "", "to_ip": "192.0.2.2"},
    {"from_ip": "   ", "to_ip": "192.0.2.2"},
    {"from_ip": "192.0.2.1", "to_ip": ""},
    {"from_ip": "192.0.2.1", "to_ip": "   "},
])
def test_topology_rejects_non_string_or_blank_endpoints(tmp_path, edge):
    context, _ = make_context(tmp_path)
    context.topology_path.write_text(json.dumps([edge]), encoding="utf-8")

    with pytest.raises(network_read.NetworkReadError) as raised:
        network_read.read_topology(context)

    assert raised.value.status == 503
    assert raised.value.payload["code"] == "topology_malformed"


def test_topology_accepts_nonempty_string_endpoints(tmp_path):
    context, _ = make_context(tmp_path)
    edge = {"from_ip": " 192.0.2.1 ", "to_ip": "192.0.2.2"}
    context.topology_path.write_text(json.dumps([edge]), encoding="utf-8")

    payload = network_read.read_topology(context)

    assert payload["edgeCount"] == 1
    assert payload["nodes"] == ["192.0.2.1", "192.0.2.2"]
    assert payload["edges"] == [edge]


def test_isp_keeps_targetless_auto_and_reads_manual_applied_env(tmp_path):
    context, _ = make_context(tmp_path)
    inventory = [{
        "targets": [],
        "labels": {
            "display_name": "PPPoE-A",
            "metric_name": "pppoe0",
            "metric_target": "192.0.2.1",
            "metric_ifindex": "7",
            "wan_ip": "203.0.113.20",
            "discovery_source": "direct-snmp",
        },
    }]
    raw = json.dumps(inventory, separators=(",", ":")).encode("utf-8")
    context.isp_inventory_path.write_bytes(raw)
    context.isp_state_path.write_text(json.dumps({
        "status": "disabled",
        "inventory_count": 1,
        "inventory_sha256": hashlib.sha256(raw).hexdigest(),
    }), encoding="utf-8")
    context.env_path.write_text("ISP_PING=Backup:198.51.100.1\n", encoding="utf-8")

    payload = network_read.read_isp(context)

    assert payload["count"] == 2
    assert payload["isps"][0]["target"] is None
    assert payload["isps"][0]["status"] == "unknown"
    assert payload["isps"][1] == {
        "name": "Backup",
        "target": "198.51.100.1",
        "status": "unknown",
        "source": "manual",
        "wanIp": None,
        "metricTarget": None,
        "metricIfindex": None,
    }


def test_isp_mismatch_rereads_once_then_marks_degraded(monkeypatch, tmp_path):
    context, _ = make_context(tmp_path)
    calls = []

    def inconsistent(_context):
        calls.append(True)
        return [], {"status": "ok", "inventory_count": 1, "inventory_sha256": "bad"}, b"[]"

    monkeypatch.setattr(network_read, "_read_isp_pair", inconsistent)

    payload = network_read.read_isp(context)

    assert len(calls) == 2
    assert payload["degraded"] is True
    assert payload["stale"] is True


def test_overview_keeps_failed_domain_null_and_fails_when_all_unavailable(monkeypatch, tmp_path):
    context, _ = make_context(tmp_path)
    monkeypatch.setattr(network_read, "read_devices", lambda _context: {"ok": True, "degraded": False, "warnings": [], "count": 0})
    monkeypatch.setattr(network_read, "read_topology", lambda _context: (_ for _ in ()).throw(network_read.NetworkReadError(503, "topology_unavailable", "missing")))
    monkeypatch.setattr(network_read, "read_isp", lambda _context: {"ok": True, "degraded": False, "warnings": [], "count": 0})

    payload = network_read.read_overview(context)

    assert payload["degraded"] is True
    assert payload["topology"] is None
    assert payload["devices"]["count"] == 0

    failure = lambda _context: (_ for _ in ()).throw(network_read.NetworkReadError(503, "unavailable", "down"))
    monkeypatch.setattr(network_read, "read_devices", failure)
    monkeypatch.setattr(network_read, "read_topology", failure)
    monkeypatch.setattr(network_read, "read_isp", failure)
    with pytest.raises(network_read.NetworkReadError) as raised:
        network_read.read_overview(context)
    assert raised.value.payload["code"] == "network_unavailable"


# ---------- P2-A: AP placement read-only snapshot ----------


def ap_counts(placements):
    counts = {key: 0 for key in network_read.AP_PLACEMENT_COUNT_KEYS}
    for item in placements:
        counts[network_read.AP_PLACEMENT_LEVEL_COUNT_KEY[item["level"]]] += 1
        counts[item["online"]] += 1
        counts["total"] += 1
    return counts


def ap_entry(**overrides):
    entry = {
        "ap_ip": "10.1.0.1",
        "ap_name": "AP-1",
        "ap_mac": "02:00:00:00:00:01",
        "site": "default",
        "online": "unknown",
        "level": "observed-attachment",
        "switch_ip": "10.0.0.11",
        "switch_ifindex": 10101,
        "switch_port": "Gi1/0/1",
        "vlan": 40,
        "evidence_age_seconds": 30.0,
        "last_observed": None,
        "coverage_status": "complete",
        "reason": "verified_fdb_egress",
        "evidence": {
            "fdb": {"status": "matched", "raw_candidates": 1, "eligible_candidates": 1,
                    "candidate_age_seconds": 10000.0},
            "exact_snmp": {"status": "matched", "gets": 1, "lookup_attempts": 1,
                           "validated_at": 1870.0, "validation_age_seconds": 30.0},
            "direct_neighbor": {"status": "unknown", "source": "none"},
            "manual_confirmation": {"status": "none"},
            "online": {"status": "unknown", "source": "unpoller-uptime",
                       "reason": "uptime_not_heartbeat"},
            "coverage": {"status": "complete", "reasons": []},
            "port": {"verified": True},
        },
    }
    entry.update(overrides)
    return entry


def confirmed_entry(**overrides):
    entry = ap_entry(level="confirmed-direct", online="online",
                     reason="operator_confirmed_core_port")
    entry["evidence"]["manual_confirmation"] = {"status": "confirmed"}
    entry["evidence"]["direct_neighbor"] = {
        "status": "operator-attested", "source": "manual-allowlist"}
    entry["evidence"]["online"]["status"] = "online"
    entry.update(overrides)
    return entry


def ap_payload(placements=None, **overrides):
    placements = [ap_entry()] if placements is None else placements
    payload = {
        "schema_version": network_read.AP_PLACEMENT_SCHEMA_VERSION,
        "generated_at": 1900.0,
        "source": "ap-placement-shadow:librenms-fdb+snmp-exact",
        "max_age_seconds": 7200,
        "valid": True,
        "invalid_reason": None,
        "counts": ap_counts(placements),
        "coverage": {
            "status": "complete",
            "expected_switches": 1,
            "polled_switches": 1,
            "missing_switches": 0,
            "poll_failed_switches": 0,
            "fdb_switches_usable": 1,
            "fdb_switches_failed": 0,
            "reasons": [],
        },
        "placements": placements,
    }
    payload.update(overrides)
    return payload


def ap_context(tmp_path, payload, now=2000.0):
    context, _client = make_context(tmp_path, now=now)
    path = tmp_path / "ap-placement.json"
    if isinstance(payload, bytes):
        path.write_bytes(payload)
    elif isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")
    return network_read.NetworkReadContext(
        **{**context.__dict__, "ap_placement_path": path})


def ap_error(context):
    with pytest.raises(network_read.NetworkReadError) as raised:
        network_read.read_ap_placement(context)
    return raised.value


def test_ap_placement_valid_snapshot_returns_whitelisted_payload(tmp_path):
    payload = ap_payload()
    payload["secret"] = "hidden"
    payload["placements"][0]["secret"] = "hidden"
    payload["placements"][0]["evidence"]["token"] = "hidden"
    context = ap_context(tmp_path, payload)

    result = network_read.read_ap_placement(context)

    assert result["ok"] is True
    assert result["valid"] is True
    assert result["counts"] == ap_counts(result["placements"])
    assert "hidden" not in json.dumps(result)
    entry = result["placements"][0]
    assert entry["level"] == "observed-attachment"
    assert entry["switch_ip"] == "10.0.0.11"
    assert entry["vlan"] == 40
    # evidence_age 30 + file_age(2000-1900) 100 = 130
    assert entry["evidence_age_seconds"] == 130.0
    assert entry["evidence"]["fdb"]["candidate_age_seconds"] == 10000.0


def test_ap_placement_missing_file_is_unavailable(tmp_path):
    context, _client = make_context(tmp_path, now=2000.0)
    error = ap_error(network_read.NetworkReadContext(
        **{**context.__dict__, "ap_placement_path": tmp_path / "missing.json"}))
    assert error.payload["code"] == "ap_placement_unavailable"
    assert error.status == 503


@pytest.mark.parametrize("raw", [
    b"",
    b"\xff\xfe",
    b"{not json",
    b"[]",
    b"null",
])
def test_ap_placement_invalid_json_and_shape_are_malformed(tmp_path, raw):
    error = ap_error(ap_context(tmp_path, raw))
    assert error.payload["code"] == "ap_placement_malformed"
    assert error.status == 503


@pytest.mark.parametrize(("overrides", "code"), [
    ({"schema_version": 2}, "ap_placement_schema_unsupported"),
    ({"schema_version": True}, "ap_placement_schema_unsupported"),
    ({"valid": None}, "ap_placement_malformed"),
    ({"valid": "true"}, "ap_placement_malformed"),
    ({"valid": False, "invalid_reason": "no_ap_identity"}, "ap_placement_invalidated"),
    ({"valid": False, "invalid_reason": "<script>"}, "ap_placement_invalidated"),
    ({"placements": {}}, "ap_placement_malformed"),
    ({"max_age_seconds": 0}, "ap_placement_malformed"),
    ({"max_age_seconds": True}, "ap_placement_malformed"),
    ({"max_age_seconds": 86401}, "ap_placement_malformed"),
    ({"generated_at": float("nan")}, "ap_placement_malformed"),
])
def test_ap_placement_schema_validity_and_time_contract(tmp_path, overrides, code):
    error = ap_error(ap_context(tmp_path, ap_payload(**overrides)))
    assert error.payload["code"] == code


def test_ap_placement_invalidated_reason_is_whitelisted(tmp_path):
    payload = ap_payload(valid=False, invalid_reason="<script>alert(1)</script>")
    error = ap_error(ap_context(tmp_path, payload))
    assert error.payload["code"] == "ap_placement_invalidated"
    assert "<script>" not in error.payload["error"]
    assert "(invalid)" in error.payload["error"]


@pytest.mark.parametrize(("generated_at", "code"), [
    (5000.0, "ap_placement_stale"),      # future beyond skew
    (2000.0 - 7201, "ap_placement_stale"),  # expired
])
def test_ap_placement_file_time_window(tmp_path, generated_at, code):
    error = ap_error(ap_context(tmp_path, ap_payload(generated_at=generated_at)))
    assert error.payload["code"] == code


def test_ap_placement_small_future_skew_is_tolerated(tmp_path):
    result = network_read.read_ap_placement(
        ap_context(tmp_path, ap_payload(generated_at=2030.0)))
    assert result["ok"] is True
    assert result["file_age_seconds"] == 0.0


@pytest.mark.parametrize("counts", [
    {"confirmed_direct": 0, "observed_attachment": 2, "ambiguous": 0, "unresolved": 0,
     "online": 0, "offline": 0, "unknown": 2, "total": 2},
    {"confirmed_direct": 0, "observed_attachment": True, "ambiguous": 0, "unresolved": 0,
     "online": 0, "offline": 0, "unknown": 1, "total": True},
    {"confirmed_direct": 0, "observed_attachment": -1, "ambiguous": 0, "unresolved": 0,
     "online": 0, "offline": 0, "unknown": 1, "total": 1},
])
def test_ap_placement_invalid_counts_are_malformed(tmp_path, counts):
    error = ap_error(ap_context(tmp_path, ap_payload(counts=counts)))
    assert error.payload["code"] == "ap_placement_malformed"


def test_ap_placement_oversize_and_placement_limit(tmp_path):
    too_large = b"{" + b" " * (network_read.AP_PLACEMENT_BYTE_LIMIT + 1)
    error = ap_error(ap_context(tmp_path, too_large))
    assert error.payload["code"] == "ap_placement_oversize"

    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        from pathlib import Path as _Path
        directory = _Path(directory)
        context, _client = make_context(directory, now=2000.0)
        payload = ap_payload()
        payload["placements"] = [{} for _ in range(network_read.AP_PLACEMENT_LIMIT + 1)]
        payload["counts"] = {key: 0 for key in network_read.AP_PLACEMENT_COUNT_KEYS}
        path = directory / "ap-placement.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        context = network_read.NetworkReadContext(
            **{**context.__dict__, "ap_placement_path": path})
        error = ap_error(context)
        assert error.payload["code"] == "ap_placement_malformed"


@pytest.mark.parametrize(("placements", "message"), [
    ([ap_entry(), ap_entry(ap_mac="02-00-00-00-00-01", ap_ip="10.1.0.2")],
     "duplicate AP MAC"),
    ([ap_entry(), ap_entry(ap_ip="10.1.0.1", ap_mac="02:00:00:00:00:02")],
     "duplicate AP IP"),
    ([ap_entry(ap_ip="999.1.1.1")], "invalid AP IPv4"),
    ([ap_entry(ap_mac="not-a-mac")], "unrecognised AP identity"),
])
def test_ap_placement_identity_rejections(tmp_path, placements, message):
    error = ap_error(ap_context(tmp_path, ap_payload(placements)))
    assert error.payload["code"] == "ap_placement_malformed"
    assert message in error.payload["error"]


@pytest.mark.parametrize(("overrides", "reason"), [
    ({"switch_ifindex": 0}, "invalid_switch_ifindex"),
    ({"switch_ifindex": True}, "invalid_switch_ifindex"),
    ({"switch_port": ""}, "invalid_switch_port"),
    ({"vlan": 0}, "invalid_vlan"),
    ({"vlan": 4095}, "invalid_vlan"),
    ({"vlan": True}, "invalid_vlan"),
    ({"evidence_age_seconds": "30"}, "invalid_evidence_age"),
    ({"evidence_age_seconds": True}, "invalid_evidence_age"),
    ({"evidence_age_seconds": -1}, "invalid_evidence_age"),
])
def test_ap_placement_entry_field_errors_downgrade_only(tmp_path, overrides, reason):
    result = network_read.read_ap_placement(
        ap_context(tmp_path, ap_payload([ap_entry(**overrides)])))
    entry = result["placements"][0]
    assert entry["level"] == "unresolved"
    assert entry["reason"] == reason
    assert entry["switch_ip"] is None
    assert entry["switch_ifindex"] is None
    assert entry["switch_port"] is None
    assert entry["vlan"] is None
    assert entry["evidence_age_seconds"] is None
    assert result["counts"] == ap_counts(result["placements"])
    assert result["source_counts"] == ap_payload([ap_entry(**overrides)])["counts"]
    assert result["warnings"]


def test_ap_placement_exact_evidence_age_includes_file_age(tmp_path):
    stale = ap_entry(evidence_age_seconds=7150.0)
    stale["evidence"]["exact_snmp"]["validated_at"] = 9900.0 - 7150.0
    payload = ap_payload([stale], generated_at=9900.0)
    result = network_read.read_ap_placement(ap_context(tmp_path, payload, now=10000.0))
    entry = result["placements"][0]
    assert entry["level"] == "unresolved"
    assert entry["reason"] == "exact_evidence_stale"
    assert entry["switch_ip"] is None
    assert result["counts"]["observed_attachment"] == 0
    assert result["counts"]["unresolved"] == 1
    assert result["source_counts"]["observed_attachment"] == 1
    assert result["degraded"] is True


def test_ap_placement_stale_even_when_file_is_fresh(tmp_path):
    entry = ap_entry(evidence_age_seconds=7201.0)
    entry["evidence"]["exact_snmp"]["validated_at"] = 10000.0 - 7201.0
    result = network_read.read_ap_placement(
        ap_context(tmp_path, ap_payload([entry], generated_at=10000.0), now=10000.0))
    output = result["placements"][0]
    assert output["level"] == "unresolved"
    assert output["reason"] == "exact_evidence_stale"


@pytest.mark.parametrize(("validated_at", "reason"), [
    (None, "validated_at_missing"),
    ("bad", "validated_at_missing"),
    (True, "validated_at_missing"),
    (-5.0, "invalid_validated_at"),
    (9999999999.0, "future_validated_at"),
])
def test_ap_placement_invalid_validated_at_downgrades(tmp_path, validated_at, reason):
    entry = ap_entry()
    entry["evidence"]["exact_snmp"]["validated_at"] = validated_at
    result = network_read.read_ap_placement(ap_context(tmp_path, ap_payload([entry])))
    output = result["placements"][0]
    assert output["level"] == "unresolved"
    assert output["reason"] == reason
    assert output["switch_ip"] is None


def test_ap_placement_timestamp_conflict_downgrades(tmp_path):
    entry = ap_entry(evidence_age_seconds=5.0)
    entry["evidence"]["exact_snmp"]["validated_at"] = 1800.0
    result = network_read.read_ap_placement(ap_context(tmp_path, ap_payload([entry])))
    output = result["placements"][0]
    assert output["level"] == "unresolved"
    assert output["reason"] == "evidence_timestamp_conflict"


def test_ap_placement_realistic_p1_snapshot_is_not_degraded(tmp_path):
    observed = ap_entry(evidence_age_seconds=2.0)
    observed["evidence"]["exact_snmp"]["validated_at"] = 1993.0
    confirmed = confirmed_entry(evidence_age_seconds=1.0,
                                ap_mac="02:00:00:00:00:02", ap_ip="10.1.0.2")
    confirmed["evidence"]["exact_snmp"]["validated_at"] = 1994.0
    result = network_read.read_ap_placement(
        ap_context(tmp_path, ap_payload([observed, confirmed], generated_at=1995.0)))
    levels = sorted(entry["level"] for entry in result["placements"])
    assert levels == ["confirmed-direct", "observed-attachment"]
    assert result["warnings"] == []
    assert result["counts"]["total"] == 2


def test_ap_placement_old_fdb_candidate_with_fresh_exact_stays_observed(tmp_path):
    entry = ap_entry(evidence_age_seconds=1.0)
    entry["evidence"]["fdb"]["candidate_age_seconds"] = 10800.0
    entry["evidence"]["exact_snmp"]["validated_at"] = 1998.0
    result = network_read.read_ap_placement(
        ap_context(tmp_path, ap_payload([entry], generated_at=1999.0)))
    output = result["placements"][0]
    assert output["level"] == "observed-attachment"
    assert output["reason"] == "verified_fdb_egress"
    assert output["evidence"]["fdb"]["candidate_age_seconds"] == 10800.0
    assert output["evidence_age_seconds"] < 60


@pytest.mark.parametrize(("path", "value", "reason"), [
    ("exact_snmp.status", "failed", "exact_evidence_failed"),
    ("exact_snmp.status", "not_attempted", "exact_evidence_missing"),
    ("port.verified", False, "port_evidence_unverified"),
    ("fdb.status", "no_candidate", "fdb_evidence_missing"),
])
def test_ap_placement_current_requires_matching_evidence(tmp_path, path, value, reason):
    entry = ap_entry()
    target = entry["evidence"]
    parts = path.split(".")
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value
    result = network_read.read_ap_placement(ap_context(tmp_path, ap_payload([entry])))
    output = result["placements"][0]
    assert output["level"] == "unresolved"
    assert output["reason"] == reason
    assert output["switch_ip"] is None
    assert output["switch_ifindex"] is None
    assert result["counts"] == ap_counts(result["placements"])
    assert result["counts"]["observed_attachment"] == 0
    assert result["source_counts"]["observed_attachment"] == 1


def test_ap_placement_missing_exact_evidence_downgrades(tmp_path):
    entry = ap_entry()
    del entry["evidence"]["exact_snmp"]
    result = network_read.read_ap_placement(ap_context(tmp_path, ap_payload([entry])))
    output = result["placements"][0]
    assert output["level"] == "unresolved"
    assert output["reason"] == "exact_evidence_missing"
    assert output["switch_ip"] is None


def test_ap_placement_confirmed_requires_online(tmp_path):
    kept = network_read.read_ap_placement(
        ap_context(tmp_path, ap_payload([confirmed_entry()])))
    assert kept["placements"][0]["level"] == "confirmed-direct"

    unknown = confirmed_entry(online="unknown")
    downgraded = network_read.read_ap_placement(
        ap_context(tmp_path, ap_payload([unknown])))
    entry = downgraded["placements"][0]
    assert entry["level"] == "unresolved"
    assert entry["reason"] == "online_gate_conflict"


def test_ap_placement_confirmed_requires_operator_attested_evidence(tmp_path):
    missing = ap_entry(level="confirmed-direct", online="online")
    missing["evidence"]["online"]["status"] = "online"
    result = network_read.read_ap_placement(ap_context(tmp_path, ap_payload([missing])))
    assert result["placements"][0]["level"] == "unresolved"
    assert result["placements"][0]["reason"] == "manual_confirmation_missing"

    untrusted = confirmed_entry()
    untrusted["evidence"]["direct_neighbor"] = {"status": "unknown", "source": "none"}
    result = network_read.read_ap_placement(ap_context(tmp_path, ap_payload([untrusted])))
    assert result["placements"][0]["reason"] == "direct_neighbor_untrusted"

    wrong_source = confirmed_entry()
    wrong_source["evidence"]["direct_neighbor"] = {
        "status": "operator-attested", "source": "somewhere-else"}
    result = network_read.read_ap_placement(
        ap_context(tmp_path, ap_payload([wrong_source])))
    assert result["placements"][0]["reason"] == "direct_neighbor_untrusted"

    conflict = confirmed_entry()
    conflict["evidence"]["online"]["status"] = "offline"
    result = network_read.read_ap_placement(ap_context(tmp_path, ap_payload([conflict])))
    assert result["placements"][0]["level"] == "unresolved"
    assert result["placements"][0]["reason"] == "online_evidence_conflict"


def test_ap_placement_confirmed_requires_matching_online_evidence(tmp_path):
    missing = confirmed_entry()
    del missing["evidence"]["online"]
    result = network_read.read_ap_placement(ap_context(tmp_path, ap_payload([missing])))
    entry = result["placements"][0]
    assert entry["level"] == "unresolved"
    assert entry["reason"] == "online_evidence_missing"
    assert entry["switch_ip"] is None
    assert entry["switch_ifindex"] is None
    assert entry["switch_port"] is None
    assert entry["vlan"] is None
    assert entry["evidence_age_seconds"] is None
    assert result["counts"] == ap_counts(result["placements"])
    assert result["counts"]["confirmed_direct"] == 0
    assert result["counts"]["unresolved"] == 1
    assert result["source_counts"]["confirmed_direct"] == 1
    assert result["warnings"]

    empty = confirmed_entry()
    empty["evidence"]["online"] = {"status": "", "source": "fixture",
                                   "reason": "fixture"}
    result = network_read.read_ap_placement(ap_context(tmp_path, ap_payload([empty])))
    assert result["placements"][0]["level"] == "unresolved"
    assert result["placements"][0]["reason"] == "online_evidence_missing"

    conflict = confirmed_entry()
    conflict["evidence"]["online"]["status"] = "unknown"
    result = network_read.read_ap_placement(ap_context(tmp_path, ap_payload([conflict])))
    assert result["placements"][0]["level"] == "unresolved"
    assert result["placements"][0]["reason"] == "online_evidence_conflict"

    valid = network_read.read_ap_placement(
        ap_context(tmp_path, ap_payload([confirmed_entry()])))
    assert valid["placements"][0]["level"] == "confirmed-direct"


def test_ap_placement_observed_keeps_matching_online_evidence(tmp_path):
    entry = ap_entry()
    result = network_read.read_ap_placement(ap_context(tmp_path, ap_payload([entry])))
    output = result["placements"][0]
    assert output["level"] == "observed-attachment"
    assert output["online"] == "unknown"
    assert output["evidence"]["online"]["status"] == "unknown"


def test_ap_placement_observed_is_never_promoted(tmp_path):
    entry = ap_entry()
    entry["evidence"]["manual_confirmation"] = {"status": "confirmed"}
    entry["evidence"]["direct_neighbor"] = {"status": "operator-attested",
                                            "source": "manual-allowlist"}
    result = network_read.read_ap_placement(ap_context(tmp_path, ap_payload([entry])))
    assert result["placements"][0]["level"] == "observed-attachment"


def test_ap_placement_offline_current_placement_is_downgraded(tmp_path):
    entry = ap_entry(online="offline")
    result = network_read.read_ap_placement(ap_context(tmp_path, ap_payload([entry])))
    assert result["placements"][0]["level"] == "unresolved"
    assert result["placements"][0]["reason"] == "offline_current_placement"


def test_ap_placement_last_observed_never_becomes_current(tmp_path):
    entry = ap_entry(level="unresolved", switch_ip=None, switch_ifindex=None,
                     switch_port=None, vlan=None, evidence_age_seconds=None,
                     last_observed={"switch_ip": "10.0.0.11", "switch_ifindex": 10101,
                                    "switch_port": "Gi1/0/1", "vlan": 40,
                                    "evidence_age_seconds": 3.0,
                                    "validated_at": 1990.0,
                                    "fdb_candidate_age_seconds": 10000.0})
    result = network_read.read_ap_placement(ap_context(tmp_path, ap_payload([entry])))
    output = result["placements"][0]
    assert output["switch_ip"] is None
    assert output["last_observed"]["switch_port"] == "Gi1/0/1"
    assert output["last_observed"]["fdb_candidate_age_seconds"] == 10000.0


def test_ap_placement_last_observed_does_not_rewrite_current_fields(tmp_path):
    entry = ap_entry(level="unresolved", switch_ip="10.0.0.11")
    result = network_read.read_ap_placement(ap_context(tmp_path, ap_payload([entry])))
    output = result["placements"][0]
    assert output["switch_ip"] is None
    assert any("cleared" in warning for warning in result["warnings"])


def test_ap_placement_partial_degradation_keeps_other_entries(tmp_path):
    good = ap_entry(evidence_age_seconds=30.0)
    good["evidence"]["exact_snmp"]["validated_at"] = 9900.0 - 30.0
    stale = ap_entry(ap_mac="02:00:00:00:00:02", ap_ip="10.1.0.2",
                     evidence_age_seconds=7150.0)
    stale["evidence"]["exact_snmp"]["validated_at"] = 9900.0 - 7150.0
    result = network_read.read_ap_placement(
        ap_context(tmp_path, ap_payload([good, stale], generated_at=9900.0), now=10000.0))
    levels = {entry["ap_ip"]: entry["level"] for entry in result["placements"]}
    assert levels == {"10.1.0.1": "observed-attachment", "10.1.0.2": "unresolved"}
    assert result["counts"] == ap_counts(result["placements"])
    assert result["counts"]["observed_attachment"] == 1
    assert result["counts"]["unresolved"] == 1


def test_ap_placement_source_file_is_not_modified(tmp_path):
    payload = ap_payload([ap_entry()])
    context = ap_context(tmp_path, payload)
    before = context.ap_placement_path.read_bytes()
    network_read.read_ap_placement(context)
    assert context.ap_placement_path.read_bytes() == before


def test_ap_placement_concurrent_atomic_replace(tmp_path):
    path = tmp_path / "ap-placement.json"
    payloads = [
        ap_payload([ap_entry(ap_mac="02:00:00:00:00:01", ap_ip="10.1.0.1")]),
        ap_payload([ap_entry(ap_mac="02:00:00:00:00:02", ap_ip="10.1.0.2")]),
    ]
    context, _client = make_context(tmp_path, now=2000.0)
    context = network_read.NetworkReadContext(
        **{**context.__dict__, "ap_placement_path": path})
    path.write_text(json.dumps(payloads[0]), encoding="utf-8")
    finished = threading.Event()
    writer_errors = []

    def writer():
        try:
            for index in range(60):
                temporary = tmp_path / f"tmp-{index}.json"
                temporary.write_text(json.dumps(payloads[index % 2]), encoding="utf-8")
                for _attempt in range(40):
                    try:
                        os.replace(temporary, path)
                        break
                    except PermissionError:
                        # Windows may briefly lock the destination while the reader
                        # has it open; retry instead of failing the atomic writer.
                        time.sleep(0.005)
                else:
                    writer_errors.append("atomic replace failed")
                    return
                time.sleep(0.001)
        finally:
            finished.set()

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    try:
        for _ in range(80):
            try:
                result = network_read.read_ap_placement(context)
            except network_read.NetworkReadError as exc:
                # A Windows reader can briefly lose the file while the writer
                # retries the atomic replace; fail-closed is the contract.
                assert exc.payload["code"] in {
                    "ap_placement_unavailable", "ap_placement_oversize",
                }
                continue
            assert result["ok"] is True
            assert result["counts"] == ap_counts(result["placements"])
    finally:
        finished.wait(timeout=5)
        thread.join(timeout=5)
    assert writer_errors == []
