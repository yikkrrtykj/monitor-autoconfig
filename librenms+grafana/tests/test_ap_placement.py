"""P1 AP 定位影子证据：等级、离线保护、覆盖率、双层预算与在线证据降级。"""
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
    for key in ("TOPOLOGY_AP_SHADOW_MAX_LOOKUPS", "TOPOLOGY_AP_SHADOW_MAX_GETS"):
        monkeypatch.delenv(key, raising=False)
    for key in ("TOPOLOGY_AP_FDB_CANDIDATE_MAX_AGE_SECONDS", "TOPOLOGY_AP_FDB_CANDIDATE_CAP",
                "TOPOLOGY_AP_FDB_MAX_AGE_SECONDS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TOPOLOGY_SNMP_DELAY_MS", "0")
    gte.reset_collection_stats()


def unknown_online(mac, source="unpoller-uptime", reason="uptime_not_heartbeat"):
    return {mac: {"status": "unknown", "source": source, "reason": reason}}


def online_online(mac):
    return {mac: {"status": "online", "source": "fixture-online", "reason": "fixture"}}


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


def build(aps, validated, shadow_result, online_evidence=None, *, fdb_status=None,
          librenms_ready=True, manual_core_ports=(), core_ips=("10.0.0.11",),
          expected_switches=None, polled_switches=None, poll_failed_switches=None,
          now=1700000000):
    return placement.build_placement_artifact(
        aps, validated, shadow_result, online_evidence or {},
        fdb_status=fdb_status or {"usable_switches": 1, "failed_switches": 0},
        librenms_ready=librenms_ready,
        max_age_seconds=7200,
        manual_core_ports=manual_core_ports,
        core_ips=core_ips,
        expected_switches=expected_switches,
        polled_switches=polled_switches,
        poll_failed_switches=poll_failed_switches,
        now=now,
    )


def one(artifact):
    assert len(artifact["placements"]) == 1
    return artifact["placements"][0]


def observe(monkeypatch, port=1):
    calls = []
    def lookup(ip, community, vlan, mac, names, get_budget):
        calls.append((ip, vlan, mac))
        if get_budget is not None:
            get_budget[0] -= 1
        return port
    monkeypatch.setattr(gte, "_shadow_exact_lookup", lookup)
    return calls


# ---------- 基础定位等级 ----------

def test_core_shadow_observed_without_allowlist(monkeypatch):
    aps, mac, devices, api, shared, status = core_setup(monkeypatch)
    calls = observe(monkeypatch)
    validated = {}  # 正式流程因核心未白名单而不验证
    result = shadow(aps, shared, validated, devices, api)
    assert result["validated"][mac][0]["port_name"] == "Gi1/0/1"
    artifact = build(aps, validated, result, unknown_online(mac))
    item = one(artifact)
    assert item["level"] == "observed-attachment"
    assert item["switch_ip"] == "10.0.0.11"
    assert item["switch_port"] == "Gi1/0/1"
    assert item["vlan"] == 200
    assert item["online"] == "unknown"
    assert item["reason"] == "online_unknown_observed"
    assert item["evidence"]["exact_snmp"]["status"] == "matched"
    assert item["evidence"]["direct_neighbor"]["status"] == "unknown"
    assert calls == [("10.0.0.11", 200, mac)]
    assert gte.collection_stats_snapshot()["ap_snmp_walks"] == 0


def test_core_shadow_is_never_confirmed_without_manual_confirmation(monkeypatch):
    aps, mac, devices, api, shared, _status = core_setup(monkeypatch)
    observe(monkeypatch)
    artifact = build(aps, {}, shadow(aps, shared, {}, devices, api), unknown_online(mac))
    item = one(artifact)
    assert item["level"] != "confirmed-direct"
    assert item["evidence"]["manual_confirmation"]["status"] == "none"
    assert item["evidence"]["direct_neighbor"]["status"] == "unknown"


def test_operator_confirmed_core_port_is_confirmed_direct_when_online(monkeypatch):
    aps, mac, devices, api, shared, _status = core_setup(monkeypatch)
    monkeypatch.setenv("TOPOLOGY_CORE_AP_PORTS", "10.0.0.11:Gi1/0/1")
    monkeypatch.setattr(gte, "lookup_fdb_ifindex", lambda *args: 1)
    validated = gte.collect_validated_ap_inventory(
        aps, shared, devices, [], api, "fixture-community")
    artifact = build(
        aps, validated, shadow(aps, shared, validated, devices, api),
        online_online(mac),
        manual_core_ports={("10.0.0.11", "gi1/0/1")})
    item = one(artifact)
    assert item["level"] == "confirmed-direct"
    assert item["reason"] == "operator_confirmed_core_port"
    assert item["online"] == "online"
    assert item["evidence"]["manual_confirmation"]["status"] == "confirmed"
    assert item["evidence"]["direct_neighbor"] == {
        "status": "operator-attested", "source": "manual-allowlist"}


def test_nonmanaged_downstream_switch_cannot_be_confirmed_direct(monkeypatch):
    aps, mac, devices, api, shared, _status = core_setup(monkeypatch)
    observe(monkeypatch)
    artifact = build(aps, {}, shadow(aps, shared, {}, devices, api), unknown_online(mac))
    assert one(artifact)["level"] == "observed-attachment"


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
    def lookup(ip, community, vlan, m, names, get_budget):
        if get_budget is not None:
            get_budget[0] -= 1
        return next(answers)
    monkeypatch.setattr(gte, "_shadow_exact_lookup", lookup)
    result = shadow(aps, shared, {}, devices, api)
    assert len(result["validated"][mac]) == 2
    item = one(build(aps, {}, result, unknown_online(mac)))
    assert item["level"] == "ambiguous"
    assert item["reason"] == "multiple_verified_candidates"
    assert item["switch_ip"] is None and item["switch_port"] is None


def test_stale_fdb_candidate_is_unresolved(monkeypatch):
    aps, mac, devices, api, shared, _status = core_setup(
        monkeypatch,
        rows=[{"port_id": 1, "mac_address": "02:00:00:00:00:01", "vlan_id": 142,
               "updated_at": time.time() - 30000}],
    )
    monkeypatch.setattr(gte, "_shadow_exact_lookup",
                        lambda *args: pytest.fail("stale candidate must not query SNMP"))
    result = shadow(aps, shared, {}, devices, api)
    assert result["validated"] == {}
    item = one(build(aps, {}, result, unknown_online(mac)))
    assert item["level"] == "unresolved"
    assert item["reason"] == "no_eligible_fdb_candidate"
    assert item["switch_ip"] is None


def test_exact_lookup_failure_is_unresolved_and_counted(monkeypatch):
    aps, mac, devices, api, shared, _status = core_setup(monkeypatch)
    def fail(*args):
        raise RuntimeError("fixture snmp failure")
    monkeypatch.setattr(gte, "_shadow_exact_lookup", fail)
    result = shadow(aps, shared, {}, devices, api)
    assert result["validated"] == {}
    assert result["diagnostics"][mac]["lookup_errors"] == 1
    item = one(build(aps, {}, result, unknown_online(mac)))
    assert item["level"] == "unresolved"
    assert item["reason"] == "exact_lookup_failed"
    assert item["evidence"]["exact_snmp"]["status"] == "failed"


def test_ap_move_recomputes_without_sticky_position(monkeypatch):
    aps, mac, devices, api, shared, _status = core_setup(monkeypatch)
    observe(monkeypatch, port=1)
    first = build(aps, {}, shadow(aps, shared, {}, devices, api), unknown_online(mac))
    assert one(first)["switch_port"] == "Gi1/0/1"

    api.fdb = {"10.0.0.11": [{"port_id": 2, "mac_address": mac, "vlan_id": 142,
                              "updated_at": time.time()}]}
    moved, _status = gte.collect_librenms_fdb_inventory(devices, [], api, True)
    observe(monkeypatch, port=2)
    second = build(aps, {}, shadow(aps, moved, {}, devices, api), unknown_online(mac))
    item = one(second)
    assert item["switch_port"] == "Gi1/0/2"
    assert item["switch_ifindex"] == 2


# ---------- 离线 / unknown 当前定位保护 ----------

def validated_core_row(validated_at=1699999998):
    return {"switch_ip": "10.0.0.11", "ifindex": 1, "port_name": "Gi1/0/1",
            "vlan": 200, "evidence_age_seconds": 3.0, "validated_at": validated_at}


def test_offline_ap_with_live_exact_get_is_not_current(monkeypatch):
    aps, mac, _devices, _api, shared, _status = core_setup(monkeypatch)
    artifact = build(
        aps, {mac: [validated_core_row()]}, {"validated": {}, "diagnostics": {}},
        {mac: {"status": "offline", "source": "fixture-offline", "reason": "fixture"}},
        manual_core_ports={("10.0.0.11", "gi1/0/1")})
    item = one(artifact)
    assert item["online"] == "offline"
    assert item["level"] == "unresolved"
    assert item["reason"] == "ap_offline"
    assert item["switch_ip"] is None and item["switch_port"] is None
    assert item["last_observed"]["switch_port"] == "Gi1/0/1"
    assert item["last_observed"]["evidence_age_seconds"] == 2.0
    assert item["last_observed"]["fdb_candidate_age_seconds"] == 3.0
    assert item["last_observed"]["validated_at"] == 1699999998.0
    assert item["evidence_age_seconds"] is None
    assert item["evidence"]["fdb"]["status"] == "matched"
    assert item["evidence"]["manual_confirmation"]["status"] == "none"
    assert artifact["counts"]["offline"] == 1
    assert artifact["counts"]["unresolved"] == 1


def test_offline_ap_without_evidence_has_no_last_observed(monkeypatch):
    aps, mac, _devices, _api, _shared, _status = core_setup(monkeypatch)
    artifact = build(aps, {}, {"validated": {}, "diagnostics": {}},
                     {mac: {"status": "offline", "source": "fixture", "reason": "fixture"}})
    item = one(artifact)
    assert item["level"] == "unresolved"
    assert item["reason"] == "ap_offline"
    assert item["last_observed"] is None
    assert item["switch_ip"] is None


def test_online_unknown_is_not_treated_as_online(monkeypatch):
    aps, mac, _devices, _api, _shared, _status = core_setup(monkeypatch)
    artifact = build(
        aps, {mac: [validated_core_row()]}, {"validated": {}, "diagnostics": {}},
        unknown_online(mac),
        manual_core_ports={("10.0.0.11", "gi1/0/1")})
    item = one(artifact)
    assert item["online"] == "unknown"
    assert item["level"] == "observed-attachment"
    assert item["level"] != "confirmed-direct"
    assert item["reason"] == "online_unknown_observed"
    assert item["evidence"]["manual_confirmation"]["status"] == "none"


# ---------- 双层 shadow 预算 ----------

def test_failed_lookups_consume_attempt_budget_across_aps(monkeypatch):
    aps = ap.current_ap_identities([identity(), identity(2)])
    macs = [ap_item["mac"] for ap_item in aps]
    monkeypatch.setenv("CORE_SWITCH_PING", "10.0.0.11")
    core_device = device("10.0.0.11", "core", [(1, 1, "Gi1/0/1", 1)], device_id=10)
    devices = {"10.0.0.11": core_device}
    api = ServerClient(devices)
    api.get_vlan_inventory = lambda: [{"device_id": 10, "vlan_id": 142, "vlan_vlan": 200}]
    api.fdb = {"10.0.0.11": [
        {"port_id": 1, "mac_address": mac, "vlan_id": 142, "updated_at": time.time()}
        for mac in macs]}
    shared, _status = gte.collect_librenms_fdb_inventory(devices, [], api, True)
    calls = []
    def fail(ip, community, vlan, mac, names, get_budget):
        calls.append(mac)
        raise RuntimeError("fixture snmp failure")
    monkeypatch.setattr(gte, "_shadow_exact_lookup", fail)
    monkeypatch.setenv("TOPOLOGY_AP_SHADOW_MAX_LOOKUPS", "1")
    result = shadow(aps, shared, {}, devices, api)
    assert calls == [macs[0]], "budget=1 must attempt only once even on exceptions"
    assert result["shadow_lookups"] == 1
    assert result["diagnostics"][macs[0]]["lookup_errors"] == 1
    assert result["diagnostics"][macs[1]]["budget_skipped"] == 1


def test_lookup_budget_zero_never_queries_snmp(monkeypatch):
    aps, mac, devices, api, shared, _status = core_setup(monkeypatch)
    monkeypatch.setenv("TOPOLOGY_AP_SHADOW_MAX_LOOKUPS", "0")
    monkeypatch.setattr(gte, "_shadow_exact_lookup",
                        lambda *args: pytest.fail("lookup budget 0 must not query"))
    result = shadow(aps, shared, {}, devices, api)
    assert result["shadow_lookups"] == 0
    assert result["diagnostics"][mac]["budget_skipped"] == 1
    item = one(build(aps, {}, result, unknown_online(mac)))
    assert item["reason"] == "shadow_budget_exhausted"


def test_real_get_budget_is_enforced_at_bottom_layer(monkeypatch):
    aps, mac, devices, api, shared, _status = core_setup(monkeypatch)
    calls = []
    def fake_snmpget(ip, community, oid):
        calls.append(oid)
        return ""
    monkeypatch.setattr(gte, "snmpget", fake_snmpget)
    monkeypatch.setenv("TOPOLOGY_AP_SHADOW_MAX_GETS", "1")
    result = shadow(aps, shared, {}, devices, api)
    assert len(calls) == 1, "one real SNMP GET must be hard-stopped by the budget"
    assert result["shadow_snmp_gets"] == 1
    assert result["diagnostics"][mac]["snmp_gets"] == 1
    assert result["diagnostics"][mac]["budget_exhausted"] == 1
    item = one(build(aps, {}, result, unknown_online(mac)))
    assert item["reason"] == "shadow_budget_exhausted"


def test_get_budget_zero_never_queries_snmp(monkeypatch):
    aps, mac, devices, api, shared, _status = core_setup(monkeypatch)
    monkeypatch.setenv("TOPOLOGY_AP_SHADOW_MAX_GETS", "0")
    monkeypatch.setattr(gte, "snmpget",
                        lambda *args: pytest.fail("get budget 0 must not call snmpget"))
    result = shadow(aps, shared, {}, devices, api)
    assert result["shadow_snmp_gets"] == 0
    assert result["diagnostics"][mac]["budget_skipped"] == 1


# ---------- 覆盖率：预期 vs 实际采集 ----------

def test_coverage_missing_polled_switches_is_partial(monkeypatch):
    aps, mac, _devices, _api, _shared, _status = core_setup(monkeypatch)
    artifact = build(
        aps, {}, {"validated": {}, "diagnostics": {}},
        unknown_online(mac),
        fdb_status={"usable_switches": 6, "failed_switches": 0},
        expected_switches=10, polled_switches=6, poll_failed_switches=4)
    assert artifact["coverage"]["status"] == "partial"
    assert artifact["coverage"]["expected_switches"] == 10
    assert artifact["coverage"]["polled_switches"] == 6
    assert artifact["coverage"]["missing_switches"] == 4
    assert "poll_missing:4" in artifact["coverage"]["reasons"]
    assert "poll_failed:4" in artifact["coverage"]["reasons"]
    item = one(artifact)
    assert item["coverage_status"] == "partial"
    assert "poll_missing:4" in item["evidence"]["coverage"]["reasons"]


def test_coverage_complete_requires_all_expected_and_fdb_success(monkeypatch):
    aps, mac, _devices, _api, _shared, _status = core_setup(monkeypatch)
    artifact = build(
        aps, {}, {"validated": {}, "diagnostics": {}},
        unknown_online(mac),
        fdb_status={"usable_switches": 10, "failed_switches": 0},
        expected_switches=10, polled_switches=10, poll_failed_switches=0)
    assert artifact["coverage"]["status"] == "complete"
    assert artifact["coverage"]["missing_switches"] == 0
    assert artifact["coverage"]["reasons"] == []


def test_coverage_fdb_api_failure_is_partial(monkeypatch):
    aps, mac, _devices, _api, _shared, _status = core_setup(monkeypatch)
    artifact = build(
        aps, {}, {"validated": {}, "diagnostics": {}},
        unknown_online(mac),
        fdb_status={"usable_switches": 9, "failed_switches": 1},
        expected_switches=10, polled_switches=10, poll_failed_switches=0)
    assert artifact["coverage"]["status"] == "partial"
    assert "fdb_api_failed:1" in artifact["coverage"]["reasons"]


def test_coverage_unit_states_are_fail_closed():
    assert placement.placement_coverage({}, False, True)["status"] == "unknown"
    assert placement.placement_coverage(
        {"usable_switches": 0, "failed_switches": 0}, True, True,
        expected_switches=10, polled_switches=0)["status"] == "unknown"
    assert placement.placement_coverage(
        {"usable_switches": 10, "failed_switches": 0}, True, True,
        expected_switches=10, polled_switches=10)["status"] == "complete"
    assert placement.placement_coverage(
        {"usable_switches": 6, "failed_switches": 0}, True, True,
        expected_switches=10, polled_switches=6,
        poll_failed_switches=4)["status"] == "partial"
    assert placement.placement_coverage(
        {"usable_switches": 10, "failed_switches": 1}, True, True,
        expected_switches=10, polled_switches=10)["status"] == "partial"
    assert placement.placement_coverage(
        {"usable_switches": 10, "failed_switches": 0}, True, False)["status"] == "unknown"


# ---------- 上游真实在线指标 ----------

def info_row(mac, name="AP-1", site="default", source="unifi", ip="10.1.0.1"):
    return {"metric": {"type": "uap", "site_name": site, "name": name,
                       "source": source, "mac": mac, "ip": ip, "id": "id-1",
                       "version": "6.0", "model": "U6", "serial": "s-1"},
            "value": [1700000000, "1"]}


def uptime_row(name="AP-1", site="default", value="3600", ts=None):
    return {"metric": {"type": "uap", "site_name": site, "name": name,
                       "source": "unifi"},
            "value": [time.time() if ts is None else ts, value]}


def test_online_uptime_uses_real_labels_and_never_means_online(monkeypatch):
    aps, mac, _devices, _api, _shared, _status = core_setup(monkeypatch)
    evidence = placement.ap_online_evidence(
        aps, [info_row(mac)], [uptime_row()], now=1700000010)
    assert "mac" not in uptime_row()["metric"], "upstream uptime metric has no mac label"
    assert evidence[mac]["status"] == "unknown"
    assert evidence[mac]["source"] == "unpoller-uptime"
    assert evidence[mac]["reason"] == "uptime_not_heartbeat"


def test_online_uptime_positive_value_is_not_online(monkeypatch):
    aps, mac, _devices, _api, _shared, _status = core_setup(monkeypatch)
    evidence = placement.ap_online_evidence(
        aps, [info_row(mac)], [uptime_row(value="999999")], now=1700000010)
    assert evidence[mac]["status"] == "unknown", "uptime > 0 must not be interpreted as online"


def test_online_duplicate_identity_is_unknown(monkeypatch):
    aps = ap.current_ap_identities([identity(1), identity(2)])
    macs = [ap_item["mac"] for ap_item in aps]
    rows = [info_row(macs[0], name="AP-1", site="default"),
            info_row(macs[1], name="AP-1", site="default")]
    evidence = placement.ap_online_evidence(
        aps, rows, [uptime_row()], now=1700000010)
    for mac in macs:
        assert evidence[mac]["reason"] == "duplicate_identity"
        assert evidence[mac]["status"] == "unknown"


def test_online_multi_site_same_name_joins_by_site(monkeypatch):
    aps = ap.current_ap_identities([identity(1), identity(2)])
    macs = [ap_item["mac"] for ap_item in aps]
    rows = [info_row(macs[0], name="AP-1", site="site-a"),
            info_row(macs[1], name="AP-1", site="site-b")]
    uptimes = [uptime_row(name="AP-1", site="site-a"),
               uptime_row(name="AP-1", site="site-b")]
    evidence = placement.ap_online_evidence(
        aps, rows, uptimes, now=1700000010)
    for mac in macs:
        assert evidence[mac]["reason"] == "uptime_not_heartbeat"
        assert evidence[mac]["status"] == "unknown"


def test_online_missing_metric_degrades_unknown(monkeypatch):
    aps, mac, _devices, _api, _shared, _status = core_setup(monkeypatch)
    evidence = placement.ap_online_evidence(
        aps, [info_row(mac)], [], now=1700000010)
    assert evidence[mac] == {"status": "unknown", "source": "unavailable",
                             "reason": "no_status_sample"}


def test_online_stale_sample_degrades_unknown(monkeypatch):
    aps, mac, _devices, _api, _shared, _status = core_setup(monkeypatch)
    evidence = placement.ap_online_evidence(
        aps, [info_row(mac)], [uptime_row(ts=1700000000)], now=1700000000 + 1000)
    assert evidence[mac]["reason"] == "stale_status_sample"
    assert evidence[mac]["status"] == "unknown"


def test_online_identity_missing_degrades_unknown(monkeypatch):
    aps, mac, _devices, _api, _shared, _status = core_setup(monkeypatch)
    evidence = placement.ap_online_evidence(aps, [], [uptime_row()], now=1700000010)
    assert evidence[mac]["reason"] == "identity_missing"
    assert evidence[mac]["source"] == "unavailable"


def test_online_fetch_uses_two_bounded_real_queries(monkeypatch):
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

    aps, mac, _devices, _api, _shared, _status = core_setup(monkeypatch)
    calls = []
    def fake_urlopen(url, timeout):
        calls.append((url, timeout))
        if "unpoller_device_info" in url:
            return Response({"status": "success", "data": {"resultType": "vector",
                                                           "result": [info_row(mac)]}})
        return Response({"status": "success", "data": {"resultType": "vector",
                                                       "result": [uptime_row()]}})
    monkeypatch.setattr(placement, "urlopen", fake_urlopen)
    evidence = placement.fetch_ap_online_evidence(aps)
    assert len(calls) == 2 and all(timeout == 5 for _url, timeout in calls)
    assert any("unpoller_device_info" in url for url, _t in calls)
    assert any("unpoller_device_uptime_seconds" in url for url, _t in calls)
    assert evidence[mac]["status"] == "unknown"
    assert evidence[mac]["reason"] == "uptime_not_heartbeat"


def test_online_fetch_failure_marks_evidence_unavailable(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("prometheus unavailable")
    monkeypatch.setattr(placement, "urlopen", fail)
    aps, mac, _devices, _api, _shared, _status = core_setup(monkeypatch)
    evidence = placement.fetch_ap_online_evidence(aps)
    assert evidence[mac] == {"status": "unknown", "source": "unavailable",
                             "reason": "prometheus_unavailable"}
    artifact = build(aps, {}, {"validated": {}, "diagnostics": {}}, evidence)
    item = one(artifact)
    assert item["online"] == "unknown"
    assert item["evidence"]["online"]["reason"] == "prometheus_unavailable"


# ---------- 产物契约与兼容性 ----------

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
    monkeypatch.setattr(gte, "fetch_ap_online_evidence", lambda aps: {})
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
    monkeypatch.setattr(gte, "_shadow_exact_lookup",
                        lambda *args: pytest.fail("validated AP must not shadow-query"))
    result = shadow(aps, shared, validated, devices, api)
    assert result["validated"] == {}
    assert result["shadow_lookups"] == 0


def test_artifact_schema_contract_and_no_sensitive_fields(monkeypatch):
    aps, mac, devices, api, shared, _status = core_setup(monkeypatch)
    observe(monkeypatch)
    artifact = build(aps, {}, shadow(aps, shared, {}, devices, api), unknown_online(mac))
    assert artifact["schema_version"] == 1
    assert artifact["source"] == "ap-placement-shadow:librenms-fdb+snmp-exact"
    assert artifact["max_age_seconds"] == 7200
    assert artifact["valid"] is True
    assert artifact["invalid_reason"] is None
    assert artifact["counts"]["total"] == 1
    assert sum(artifact["counts"][key] for key in
               ("confirmed_direct", "observed_attachment", "ambiguous", "unresolved")) == 1
    item = one(artifact)
    for key in ("ap_ip", "ap_name", "ap_mac", "site", "online", "level",
                "switch_ip", "switch_ifindex", "switch_port", "vlan",
                "evidence_age_seconds", "last_observed", "coverage_status",
                "reason", "evidence"):
        assert key in item, key
    for key in ("fdb", "exact_snmp", "direct_neighbor", "manual_confirmation",
                "online", "coverage", "port"):
        assert key in item["evidence"], key
    assert "candidate_age_seconds" in item["evidence"]["fdb"]
    assert "validation_age_seconds" in item["evidence"]["exact_snmp"]
    encoded = json.dumps(artifact).lower()
    for forbidden in ("password", "client_mac", "community", "uplink_mac"):
        assert forbidden not in encoded


# ---------- P1-3: 影子产物失效机制 ----------

def test_invalidated_artifact_contract():
    artifact = placement.build_invalidated_artifact(
        "no_topology_devices", max_age_seconds=7200, now=1700000000)
    assert artifact["schema_version"] == 1
    assert artifact["valid"] is False
    assert artifact["invalid_reason"] == "no_topology_devices"
    assert artifact["generated_at"] == 1700000000
    assert artifact["placements"] == []
    assert artifact["counts"]["total"] == 0
    for key in ("confirmed_direct", "observed_attachment", "ambiguous",
                "unresolved", "online", "offline", "unknown"):
        assert artifact["counts"][key] == 0
    assert artifact["coverage"]["status"] == "unknown"
    assert artifact["coverage"]["reasons"] == ["no_topology_devices"]


def test_previous_valid_snapshot_is_invalidated_by_empty_ap_identity(monkeypatch, tmp_path):
    aps, mac, devices, api, shared, _status = core_setup(monkeypatch)
    observe(monkeypatch)
    valid = build(aps, {}, shadow(aps, shared, {}, devices, api), unknown_online(mac))
    gte.write_json_atomic(str(tmp_path / "ap-placement.json"), valid, sort_keys=True)
    assert json.loads((tmp_path / "ap-placement.json").read_text())["valid"] is True
    gte.write_ap_placement_artifact(str(tmp_path), [], {}, {}, {}, {}, [], None, None,
                                    "fixture-community", True)
    after = json.loads((tmp_path / "ap-placement.json").read_text())
    assert after["valid"] is False
    assert after["invalid_reason"] == "no_ap_identity"
    assert after["placements"] == []
    assert after["counts"]["total"] == 0
    assert after["generated_at"] > 0


def test_ap_identity_error_invalidates_previous_snapshot(tmp_path):
    (tmp_path / "ap-placement.json").write_text(
        json.dumps({"valid": True, "placements": [{"old": "observed"}]}))
    gte.write_ap_placement_artifact(str(tmp_path), [], {}, {}, {}, {}, [], None, None,
                                    "fixture-community", True, identity_error=True)
    after = json.loads((tmp_path / "ap-placement.json").read_text())
    assert after["valid"] is False
    assert after["invalid_reason"] == "ap_identity_unavailable"
    assert after["placements"] == []


def test_no_topology_devices_invalidates_shadow_artifact(monkeypatch, tmp_path):
    for key in ("TOPOLOGY_DEVICES", "CORE_SWITCH_PING", "DIST_SWITCH_PING",
                "FIREWALL_PING", "TOURNAMENT_SWITCHES", "SERVER_PING"):
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("TOPOLOGY_OUTPUT_DIR", str(tmp_path))
    aps = ap.current_ap_identities([identity()])
    monkeypatch.setattr(gte, "fetch_current_aps", lambda: aps)
    monkeypatch.setattr(gte, "load_device_list", lambda: [])
    (tmp_path / "ap-placement.json").write_text(
        json.dumps({"valid": True, "placements": [{"old": "observed"}]}))
    assert gte._run_collection() == 0
    after = json.loads((tmp_path / "ap-placement.json").read_text())
    assert after["valid"] is False
    assert after["invalid_reason"] == "no_topology_devices"
    assert after["placements"] == []
    assert after["counts"]["total"] == 0


# ---------- P2-3: FDB 候选年龄 vs 本轮 exact 验证年龄 ----------

def test_top_level_age_is_exact_validation_not_fdb_candidate_age(monkeypatch):
    aps, mac, _devices, _api, _shared, _status = core_setup(monkeypatch)
    row = {"switch_ip": "10.0.0.11", "ifindex": 1, "port_name": "Gi1/0/1",
           "vlan": 200, "evidence_age_seconds": 10000.0,
           "validated_at": 1699999998.0}
    artifact = build(aps, {mac: [row]}, {"validated": {}, "diagnostics": {}},
                     unknown_online(mac))
    item = one(artifact)
    assert item["level"] == "observed-attachment"
    assert item["evidence_age_seconds"] == 2.0
    assert item["evidence"]["fdb"]["candidate_age_seconds"] == 10000.0
    assert item["evidence"]["exact_snmp"]["validation_age_seconds"] == 2.0
    assert item["evidence"]["exact_snmp"]["validated_at"] == 1699999998.0


def test_old_fdb_candidate_with_fresh_exact_get_is_not_stale(monkeypatch):
    aps, mac, devices, api, shared, _status = core_setup(
        monkeypatch,
        rows=[{"port_id": 1, "mac_address": "02:00:00:00:00:01", "vlan_id": 142,
               "updated_at": time.time() - 10800}],
    )
    observe(monkeypatch)
    result = shadow(aps, shared, {}, devices, api)
    assert result["validated"][mac]
    artifact = build(aps, {}, result, unknown_online(mac), now=None)
    item = one(artifact)
    assert item["level"] == "observed-attachment"
    assert "stale" not in item["reason"]
    assert item["evidence_age_seconds"] is not None
    assert item["evidence_age_seconds"] < 60
    assert item["evidence"]["fdb"]["candidate_age_seconds"] >= 10800
    assert item["evidence"]["exact_snmp"]["validation_age_seconds"] < 60


def test_fdb_candidate_beyond_candidate_window_is_unresolved(monkeypatch):
    aps, mac, devices, api, shared, _status = core_setup(
        monkeypatch,
        rows=[{"port_id": 1, "mac_address": "02:00:00:00:00:01", "vlan_id": 142,
               "updated_at": time.time() - 30000}],
    )
    monkeypatch.setattr(gte, "_shadow_exact_lookup",
                        lambda *args: pytest.fail("stale candidate must not query SNMP"))
    result = shadow(aps, shared, {}, devices, api)
    assert result["validated"] == {}
    item = one(build(aps, {}, result, unknown_online(mac)))
    assert item["level"] == "unresolved"
    assert item["reason"] == "no_eligible_fdb_candidate"
    assert item["evidence_age_seconds"] is None
