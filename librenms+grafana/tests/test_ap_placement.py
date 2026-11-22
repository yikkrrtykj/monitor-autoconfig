"""P1 AP 定位影子证据：等级、覆盖率、预算、失败降级与契约回归。"""
import json
import time

import pytest

import ap_placement as placement
import ap_topology as ap
from .test_ap_topology import identity
from .test_topology_server_librenms import gte, device, ServerClient


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    for key in ("CORE_SWITCH_PING", "FIREWALL_PING", "FIREWALL_UNIT_SNMP_TARGETS",
                "TOPOLOGY_CORE_AP_PORTS"):
        monkeypatch.setenv(key, "")
    monkeypatch.delenv("TOPOLOGY_AP_SHADOW_MAX_GETS", raising=False)
    for key in ("TOPOLOGY_AP_FDB_CANDIDATE_MAX_AGE_SECONDS", "TOPOLOGY_AP_FDB_CANDIDATE_CAP",
                "TOPOLOGY_AP_FDB_MAX_AGE_SECONDS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TOPOLOGY_SNMP_DELAY_MS", "0")
    gte.reset_collection_stats()


def core_setup(monkeypatch, *, rows=None, ports=None, core="10.0.0.11", edges=()):
    aps = ap.current_ap_identities([identity()])
    monkeypatch.setenv("CORE_SWITCH_PING", core)
    mac = aps[0]["mac"]
    core_device = device(core, "core", ports or [(1, 1, "Gi1/0/1", 1), (2, 2, "Gi1/0/2", 1)],
                         device_id=10)
    devices = {core: core_device}
    api = ServerClient(devices)
    api.get_vlan_inventory = lambda: [{"device_id": 10, "vlan_id": 142, "vlan_vlan": 200}]
    if rows is None:
        rows = [{"port_id": 1, "mac_address": mac, "vlan_id": 142, "updated_at": time.time()}]
    api.fdb = {core: rows}
    shared, status = gte.collect_librenms_fdb_inventory(devices, list(edges), api, True)
    return aps, mac, devices, api, shared, status


def shadow(aps, shared, validated, devices, api, edges=()):
    return gte.collect_ap_shadow_candidates(
        aps, shared, validated, devices, list(edges), api, "fixture-community")


def build(aps, validated, shadow_result, online=None, *, fdb_status=None,
          librenms_ready=True, manual_core_ports=(), core_ips=("10.0.0.11",)):
    return placement.build_placement_artifact(
        aps, validated, shadow_result, online or {},
        fdb_status=fdb_status or {"usable_switches": 1, "failed_switches": 0},
        librenms_ready=librenms_ready,
        max_age_seconds=7200,
        manual_core_ports=manual_core_ports,
        core_ips=core_ips,
        now=1700000000,
    )


def one(artifact):
    assert len(artifact["placements"]) == 1
    return artifact["placements"][0]


def test_core_shadow_observed_without_allowlist(monkeypatch):
    aps, mac, devices, api, shared, status = core_setup(monkeypatch)
    calls = []
    monkeypatch.setattr(gte, "lookup_fdb_ifindex",
                        lambda ip, community, vlan, m, names: calls.append((ip, vlan, m)) or 1)
    validated = {}  # 正式流程因核心未白名单而不验证
    result = shadow(aps, shared, validated, devices, api)
    assert result["validated"][mac][0]["port_name"] == "Gi1/0/1"
    artifact = build(aps, validated, result, online={mac: "online"})
    item = one(artifact)
    assert item["level"] == "observed-attachment"
    assert item["switch_ip"] == "10.0.0.11"
    assert item["switch_port"] == "Gi1/0/1"
    assert item["vlan"] == 200
    assert item["online"] == "online"
    assert item["evidence"]["exact_snmp"]["status"] == "matched"
    assert item["evidence"]["direct_neighbor"]["status"] == "unknown"
    assert item["reason"] == "verified_fdb_egress"
    assert calls == [("10.0.0.11", 200, mac)]
    assert result["shadow_snmp_gets"] == 1
    assert gte.collection_stats_snapshot()["ap_snmp_walks"] == 0


def test_core_shadow_is_never_confirmed_direct_without_manual_confirmation(monkeypatch):
    aps, mac, devices, api, shared, _status = core_setup(monkeypatch)
    monkeypatch.setattr(gte, "lookup_fdb_ifindex", lambda *args: 1)
    artifact = build(aps, {}, shadow(aps, shared, {}, devices, api), online={mac: "online"})
    item = one(artifact)
    assert item["level"] == "observed-attachment"
    assert item["level"] != "confirmed-direct"
    assert item["evidence"]["manual_confirmation"]["status"] == "none"
    assert item["evidence"]["direct_neighbor"]["status"] == "unknown"


def test_operator_confirmed_core_port_is_confirmed_direct(monkeypatch):
    aps, mac, devices, api, shared, _status = core_setup(monkeypatch)
    monkeypatch.setenv("TOPOLOGY_CORE_AP_PORTS", "10.0.0.11:Gi1/0/1")
    monkeypatch.setattr(gte, "lookup_fdb_ifindex", lambda *args: 1)
    validated = gte.collect_validated_ap_inventory(
        aps, shared, devices, [], api, "fixture-community")
    artifact = build(
        aps, validated, shadow(aps, shared, validated, devices, api),
        manual_core_ports={("10.0.0.11", "gi1/0/1")})
    item = one(artifact)
    assert item["level"] == "confirmed-direct"
    assert item["reason"] == "operator_confirmed_core_port"
    assert item["evidence"]["manual_confirmation"]["status"] == "confirmed"
    assert item["evidence"]["direct_neighbor"] == {
        "status": "operator-attested", "source": "manual-allowlist"}


def test_nonmanaged_downstream_switch_cannot_be_confirmed_direct(monkeypatch):
    aps, mac, devices, api, shared, _status = core_setup(monkeypatch)
    monkeypatch.setattr(gte, "lookup_fdb_ifindex", lambda *args: 1)
    artifact = build(aps, {}, shadow(aps, shared, {}, devices, api))
    item = one(artifact)
    assert item["level"] == "observed-attachment"


def test_multiple_valid_candidates_are_ambiguous(monkeypatch):
    aps, mac, devices, api, shared, _status = core_setup(
        monkeypatch,
        rows=[{"port_id": 1, "mac_address": "02:00:00:00:00:01", "vlan_id": 142,
               "updated_at": time.time()},
              {"port_id": 2, "mac_address": "02:00:00:00:00:01", "vlan_id": 143,
               "updated_at": time.time()}],
    )
    api.get_vlan_inventory = lambda: [
        {"device_id": 10, "vlan_id": 142, "vlan_vlan": 200},
        {"device_id": 10, "vlan_id": 143, "vlan_vlan": 201}]
    answers = iter([1, 2])
    monkeypatch.setattr(gte, "lookup_fdb_ifindex", lambda *args: next(answers))
    result = shadow(aps, shared, {}, devices, api)
    assert len(result["validated"][mac]) == 2
    artifact = build(aps, {}, result)
    item = one(artifact)
    assert item["level"] == "ambiguous"
    assert item["reason"] == "multiple_verified_candidates"
    assert item["switch_ip"] is None and item["switch_port"] is None


def test_stale_fdb_candidate_is_unresolved(monkeypatch):
    aps, mac, devices, api, shared, _status = core_setup(
        monkeypatch,
        rows=[{"port_id": 1, "mac_address": "02:00:00:00:00:01", "vlan_id": 142,
               "updated_at": time.time() - 30000}],
    )
    monkeypatch.setattr(gte, "lookup_fdb_ifindex",
                        lambda *args: pytest.fail("stale candidate must not query SNMP"))
    result = shadow(aps, shared, {}, devices, api)
    assert result["validated"] == {}
    artifact = build(aps, {}, result, online={mac: "online"})
    item = one(artifact)
    assert item["level"] == "unresolved"
    assert item["reason"] == "no_eligible_fdb_candidate"
    assert item["online"] == "online"
    assert item["switch_ip"] is None


def test_exact_get_failure_is_unresolved_and_never_fabricated(monkeypatch):
    aps, mac, devices, api, shared, _status = core_setup(monkeypatch)
    def fail(*args):
        raise RuntimeError("fixture snmp failure")
    monkeypatch.setattr(gte, "lookup_fdb_ifindex", fail)
    result = shadow(aps, shared, {}, devices, api)
    assert result["validated"] == {}
    assert result["diagnostics"][mac]["exact_errors"] == 1
    artifact = build(aps, {}, result)
    item = one(artifact)
    assert item["level"] == "unresolved"
    assert item["reason"] == "exact_lookup_failed"
    assert item["switch_ip"] is None
    assert gte.collection_stats_snapshot()["ap_snmp_gets"] == 0


def test_shadow_budget_is_bounded(monkeypatch):
    aps, mac, devices, api, shared, _status = core_setup(
        monkeypatch,
        rows=[{"port_id": 1, "mac_address": "02:00:00:00:00:01", "vlan_id": 142,
               "updated_at": time.time()},
              {"port_id": 2, "mac_address": "02:00:00:00:00:01", "vlan_id": 143,
               "updated_at": time.time()}],
    )
    api.get_vlan_inventory = lambda: [
        {"device_id": 10, "vlan_id": 142, "vlan_vlan": 200},
        {"device_id": 10, "vlan_id": 143, "vlan_vlan": 201}]
    monkeypatch.setenv("TOPOLOGY_AP_SHADOW_MAX_GETS", "1")
    monkeypatch.setattr(gte, "lookup_fdb_ifindex", lambda *args: 1)
    result = shadow(aps, shared, {}, devices, api)
    assert result["shadow_snmp_gets"] == 1
    assert result["diagnostics"][mac]["budget_skipped"] == 1
    assert result["validated"] == {}
    artifact = build(aps, {}, result)
    assert one(artifact)["reason"] == "shadow_budget_exhausted"
    assert gte.collection_stats_snapshot()["ap_snmp_walks"] == 0


def test_missing_downstream_collection_sets_partial_coverage(monkeypatch):
    aps, mac, devices, api, shared, _status = core_setup(monkeypatch)
    monkeypatch.setattr(gte, "lookup_fdb_ifindex", lambda *args: 1)
    artifact = build(
        aps, {}, shadow(aps, shared, {}, devices, api),
        fdb_status={"usable_switches": 1, "failed_switches": 1})
    item = one(artifact)
    assert artifact["coverage"]["status"] == "partial"
    assert item["coverage_status"] == "partial"
    assert item["level"] == "observed-attachment"


def test_no_fdb_evidence_keeps_offline_separate(monkeypatch):
    aps, mac, devices, api, shared, _status = core_setup(monkeypatch, rows=[])
    artifact = build(aps, {}, shadow(aps, shared, {}, devices, api),
                     online={mac: "offline"})
    item = one(artifact)
    assert item["online"] == "offline"
    assert item["level"] == "unresolved"
    assert item["reason"] == "no_fdb_candidate"
    assert item["switch_ip"] is None
    assert artifact["counts"]["offline"] == 1


def test_unknown_uplink_does_not_invent_wired_location(monkeypatch):
    aps, mac, devices, api, shared, _status = core_setup(monkeypatch, rows=[])
    artifact = build(aps, {}, shadow(aps, shared, {}, devices, api), online={mac: "online"})
    item = one(artifact)
    assert item["level"] == "unresolved"
    assert item["evidence"]["direct_neighbor"]["status"] == "unknown"
    assert item["evidence"]["direct_neighbor"]["source"] == "none"
    assert item["switch_ip"] is None


def test_ap_move_recomputes_without_sticky_position(monkeypatch):
    aps, mac, devices, api, shared, _status = core_setup(monkeypatch)
    monkeypatch.setattr(gte, "lookup_fdb_ifindex", lambda *args: 1)
    first = build(aps, {}, shadow(aps, shared, {}, devices, api))
    assert one(first)["switch_port"] == "Gi1/0/1"

    api.fdb = {"10.0.0.11": [{"port_id": 2, "mac_address": mac, "vlan_id": 142,
                              "updated_at": time.time()}]}
    moved, _status = gte.collect_librenms_fdb_inventory(devices, [], api, True)
    monkeypatch.setattr(gte, "lookup_fdb_ifindex", lambda *args: 2)
    second = build(aps, {}, shadow(aps, moved, {}, devices, api))
    item = one(second)
    assert item["switch_port"] == "Gi1/0/2"
    assert item["switch_ifindex"] == 2


def test_shadow_artifact_does_not_change_ap_attachments(monkeypatch, tmp_path):
    aps = ap.current_ap_identities([identity()])
    mac = aps[0]["mac"]
    access = device("10.0.0.12", "access", [(1, 1, "Gi0/1", 1)], device_id=11)
    devices = {access["ip"]: access}
    api = ServerClient(devices)
    api.get_vlan_inventory = lambda: [{"device_id": 11, "vlan_id": 143, "vlan_vlan": 200}]
    api.fdb = {"10.0.0.12": [{"port_id": 1, "mac_address": mac, "vlan_id": 143,
                              "updated_at": time.time()}]}
    shared, status = gte.collect_librenms_fdb_inventory(devices, [], api, True)
    monkeypatch.setattr(gte, "lookup_fdb_ifindex", lambda *args: 1)
    validated = gte.collect_validated_ap_inventory(
        aps, shared, devices, [], api, "fixture-community")
    gte.write_current_ap_artifact(str(tmp_path), aps, validated)
    before = (tmp_path / "ap-attachments.json").read_text()
    expected = gte.build_ap_artifact(aps, validated, [])
    before_obj = json.loads(before)
    def bound_fields(rows):
        return [(row["ap_mac"], row["switch_ip"], row["switch_ifindex"],
                 row["switch_port"], row["vlan"]) for row in rows["attachments"]]
    assert bound_fields(before_obj) == bound_fields(expected)
    assert before_obj["inventory_count"] == expected["inventory_count"]
    assert before_obj["source"] == expected["source"]
    monkeypatch.setattr(gte, "fetch_ap_online_states", lambda: {})
    gte.write_ap_placement_artifact(
        str(tmp_path), aps, validated, shared, status, devices, [], api,
        "fixture-community", True)
    assert (tmp_path / "ap-attachments.json").read_text() == before
    shadow_artifact = json.loads((tmp_path / "ap-placement.json").read_text())
    assert one(shadow_artifact)["level"] == "observed-attachment"
    assert one(shadow_artifact)["switch_ip"] == "10.0.0.12"


def test_shadow_skips_aps_already_validated_by_formal_flow(monkeypatch):
    aps = ap.current_ap_identities([identity()])
    mac = aps[0]["mac"]
    access = device("10.0.0.12", "access", [(1, 1, "Gi0/1", 1)], device_id=11)
    devices = {access["ip"]: access}
    api = ServerClient(devices)
    api.get_vlan_inventory = lambda: [{"device_id": 11, "vlan_id": 143, "vlan_vlan": 200}]
    api.fdb = {"10.0.0.12": [{"port_id": 1, "mac_address": mac, "vlan_id": 143,
                              "updated_at": time.time()}]}
    shared, _status = gte.collect_librenms_fdb_inventory(devices, [], api, True)
    monkeypatch.setattr(gte, "lookup_fdb_ifindex", lambda *args: 1)
    validated = gte.collect_validated_ap_inventory(
        aps, shared, devices, [], api, "fixture-community")
    monkeypatch.setattr(gte, "lookup_fdb_ifindex",
                        lambda *args: pytest.fail("validated AP must not shadow-query"))
    result = shadow(aps, shared, validated, devices, api)
    assert result["validated"] == {}
    assert result["shadow_snmp_gets"] == 0


def test_artifact_schema_contract_and_no_sensitive_fields(monkeypatch):
    aps, mac, devices, api, shared, _status = core_setup(monkeypatch)
    monkeypatch.setattr(gte, "lookup_fdb_ifindex", lambda *args: 1)
    artifact = build(aps, {}, shadow(aps, shared, {}, devices, api), online={mac: "online"})
    assert artifact["schema_version"] == 1
    assert artifact["source"] == "ap-placement-shadow:librenms-fdb+snmp-exact"
    assert artifact["max_age_seconds"] == 7200
    assert artifact["counts"]["total"] == 1
    assert sum(artifact["counts"][key] for key in
               ("confirmed_direct", "observed_attachment", "ambiguous", "unresolved")) == 1
    item = one(artifact)
    for key in ("ap_ip", "ap_name", "ap_mac", "site", "online", "level",
                "switch_ip", "switch_ifindex", "switch_port", "vlan",
                "evidence_age_seconds", "coverage_status", "reason", "evidence"):
        assert key in item, key
    for key in ("fdb", "exact_snmp", "direct_neighbor", "manual_confirmation",
                "online", "coverage", "port"):
        assert key in item["evidence"], key
    encoded = json.dumps(artifact).lower()
    for forbidden in ("password", "client_mac", "community", "uplink_mac"):
        assert forbidden not in encoded


def test_online_query_is_single_bounded_request(monkeypatch):
    class Response:
        def __init__(self, payload):
            self.payload = payload
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self, bound):
            assert bound == placement.MAX_ONLINE_RESPONSE_BYTES + 1
            return json.dumps(self.payload).encode()

    calls = []
    def fake_urlopen(url, timeout):
        calls.append((url, timeout))
        return Response({"status": "success", "data": {"resultType": "vector", "result": [
            {"metric": {"mac": "02:00:00:00:00:01"}, "value": [1, "1"]},
            {"metric": {"mac": "02:00:00:00:00:02"}, "value": [1, "0"]},
        ]}})
    monkeypatch.setattr(placement, "urlopen", fake_urlopen)
    states = placement.fetch_ap_online_states()
    assert len(calls) == 1 and calls[0][1] == 5
    assert "unpoller_device_" in calls[0][0]
    assert states == {"02:00:00:00:00:01": "online", "02:00:00:00:00:02": "offline"}


def test_online_query_failure_is_unknown_not_online(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("prometheus unavailable")
    monkeypatch.setattr(placement, "urlopen", fail)
    with pytest.raises(OSError):
        placement.fetch_ap_online_states()
    aps, mac, devices, api, shared, _status = core_setup(monkeypatch, rows=[])
    artifact = build(aps, {}, shadow(aps, shared, {}, devices, api), online={})
    assert one(artifact)["online"] == "unknown"
    assert artifact["counts"]["unknown"] == 1


def test_coverage_states_are_fail_closed():
    assert placement.placement_coverage({}, False, True)["status"] == "unknown"
    assert placement.placement_coverage({"usable_switches": 0, "failed_switches": 0},
                                        True, True)["status"] == "unknown"
    assert placement.placement_coverage({"usable_switches": 2, "failed_switches": 0},
                                        True, True)["status"] == "complete"
    assert placement.placement_coverage({"usable_switches": 2, "failed_switches": 1},
                                        True, True)["status"] == "partial"
    assert placement.placement_coverage({"usable_switches": 2, "failed_switches": 1},
                                        True, False)["status"] == "unknown"
