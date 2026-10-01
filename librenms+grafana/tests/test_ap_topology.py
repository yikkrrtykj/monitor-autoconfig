"""T-05 当前 AP 身份、独立窗口与共享 FDB 回归。"""
import json
import time
from unittest.mock import patch

import pytest
import ap_topology as ap
from .test_topology_server_librenms import gte, device, ServerClient


def identity(n=1, **labels):
    return {"metric": {"type": "uap", "ip": f"10.1.0.{n}",
                       "mac": f"02:00:00:00:00:{n:02x}", "name": f"AP-{n}", **labels}}


def candidate(mac, ip="10.0.0.11", index=1, age=6000, **fields):
    return {"mac": mac, "switch_ip": ip, "ifindex": index,
            "port_name": "Gi1/0/1", "vlan": 42,
            "evidence_age_seconds": age, **fields}


def test_current_identity_rejects_duplicate_missing_and_non_uap():
    rows = [identity(), identity(2), identity(2, mac="02:00:00:00:00:03"),
            identity(4, mac=""), identity(5, type="usw"), identity(6, ip="bad"),
            identity(7, mac="02:00:00:00:00:01")]
    assert ap.current_ap_identities(rows) == []
    assert ap.current_ap_identities([identity()])[0]["mac"] == "02:00:00:00:00:01"
    with pytest.raises(ValueError):
        ap.current_ap_identities([identity()] * 513)


@pytest.mark.parametrize("age,resolved", [(0, True), (7200, True), (7201, False), (None, False), (-1, False)])
def test_ap_freshness_known_bounded(age, resolved):
    aps = ap.current_ap_identities([identity()])
    result = ap.build_ap_artifact(aps, {aps[0]["mac"]: [candidate(aps[0]["mac"], age=age)]})
    assert len(result["attachments"]) == int(resolved)


def test_dedup_multiple_exclusion_and_no_cached_owner():
    aps = ap.current_ap_identities([identity()]); mac = aps[0]["mac"]
    row = candidate(mac)
    assert len(ap.build_ap_artifact(aps, {mac: [row, row]})["attachments"]) == 1
    result = ap.build_ap_artifact(aps, {mac: [row, candidate(mac, index=2)]})
    assert result["attachments"] == [] and result["ambiguous_count"] == 1
    assert ap.build_ap_artifact(aps, {mac: [row]}, [row["switch_ip"]])["attachments"] == []
    for field in ("firewall", "aggregate_member", "confirmed_down"):
        assert ap.build_ap_artifact(aps, {mac: [candidate(mac, **{field: True})]})["attachments"] == []
    assert ap.build_ap_artifact(aps, {})["attachments"] == []


def test_shared_inventory_independent_server_and_ap_windows(monkeypatch):
    monkeypatch.setenv("FIREWALL_PING", "")
    monkeypatch.setenv("CORE_SWITCH_PING", "")
    monkeypatch.delenv("TOPOLOGY_LIBRENMS_FDB_MAX_AGE_SECONDS", raising=False)
    monkeypatch.delenv("TOPOLOGY_AP_FDB_MAX_AGE_SECONDS", raising=False)
    aps = ap.current_ap_identities([identity()]); mac = aps[0]["mac"]
    switch = device("10.0.0.11", "access", [(1, 1, "Gi1/0/1", 1)])
    devices = {switch["ip"]: switch}
    client = ServerClient(devices, fdb={switch["ip"]: [
        {"port_id": 1, "mac_address": mac, "updated_at": time.time() - 6000}]})
    with patch.object(gte, "snmpget", side_effect=AssertionError("SNMP forbidden")), \
         patch.object(gte, "snmpwalk", side_effect=AssertionError("SNMP forbidden")):
        shared = gte.collect_librenms_fdb_inventory(devices, [], client, True)
        assert gte.build_librenms_fdb_candidates(devices, [], client, True, inventory=shared)[0] == {}
        assert len(ap.build_ap_artifact(aps, shared[0])["attachments"]) == 1
    assert client.calls == [("10.0.0.11", "fdb")]
    assert gte.topology_librenms_fdb_max_age() == 900 and ap.ap_max_age() == 7200


def test_structural_physical_uplink_lag_down_filters(monkeypatch):
    monkeypatch.setenv("FIREWALL_PING", "")
    aps = ap.current_ap_identities([identity()]); mac = aps[0]["mac"]
    switch = device("10.0.0.11", "access", [(1, 1, "Po1", 1), (2, 2, "Gi1/0/2", 2),
                    (3, 3, "Gi1/0/3", 1), (4, 4, "Vlan4", 1), (5, 5, "Gi1/0/5", 1)])
    devices = {switch["ip"]: switch}
    rows = [{"port_id": n, "mac_address": mac, "updated_at": time.time()} for n in range(1, 6)]
    shared = gte.collect_librenms_fdb_inventory(devices,
        [{"from_ip": switch["ip"], "from_ifindex": 3}], ServerClient(devices, fdb={switch["ip"]: rows}), True)
    assert [r["switch_ifindex"] for r in ap.build_ap_artifact(aps, shared[0])["attachments"]] == [5]


def test_phase_0f_partial_authority_and_failure_clears_ap_only(tmp_path, monkeypatch):
    aps = ap.current_ap_identities([identity(n) for n in range(1, 18)])
    inventory = {a["mac"]: [candidate(a["mac"], index=n)] for n, a in enumerate(aps[:7], 1)}
    artifact = ap.build_ap_artifact(aps, inventory)
    assert (len(artifact["attachments"]), artifact["ambiguous_count"], artifact["unresolved_count"]) == (7, 0, 10)
    monkeypatch.setenv("CORE_SWITCH_PING", "")
    monkeypatch.setenv("FIREWALL_PING", "")
    wired = tmp_path / "edges.json"; wired.write_text('[{"sentinel":true}]')
    gte.write_current_ap_artifact(str(tmp_path), aps, inventory)
    gte.write_current_ap_artifact(str(tmp_path), [], {})
    assert json.loads((tmp_path / "ap-attachments.json").read_text())["attachments"] == []
    assert wired.read_text() == '[{"sentinel":true}]'


def test_single_bounded_prometheus_request(monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, bound):
            assert bound == ap.MAX_RESPONSE_BYTES + 1
            return json.dumps({"status": "success", "data": {"resultType": "vector", "result": [identity()]}}).encode()
    calls = []
    def request(url, timeout):
        calls.append((url, timeout)); return Response()
    monkeypatch.setattr(ap, "urlopen", request)
    assert len(ap.fetch_current_aps()) == 1
    assert len(calls) == 1 and calls[0][1] == 5
    assert "/api/v1/query?" in calls[0][0]


def test_no_uap_inert():
    assert ap.build_ap_artifact([], {})["attachments"] == []


@pytest.mark.parametrize("failure", [None, "prometheus", "fdb"])
def test_full_cycle_shared_fdb_no_snmp_and_failure_isolation(tmp_path, monkeypatch, failure):
    aps = ap.current_ap_identities([identity()]); mac = aps[0]["mac"]
    switch = device("10.0.0.11", "access", [(1, 1, "Gi1/0/1", 1)])
    api = ServerClient({switch["ip"]: switch}, fdb={switch["ip"]: [
        {"port_id": 1, "mac_address": mac, "updated_at": time.time() - 6000}]})
    api.list_devices = lambda: [switch["librenms_metadata"]]
    if failure == "fdb":
        from librenms_client import LibreNMSUnavailable
        api.failures[(switch["ip"], "fdb")] = LibreNMSUnavailable("fixture")
    def fetch():
        if failure == "prometheus": raise ValueError("fixture")
        return aps
    monkeypatch.setattr(gte, "fetch_current_aps", fetch)
    monkeypatch.setattr(gte, "LibreNMSClient", lambda: api)
    monkeypatch.setattr(gte, "collect_device_by_source", lambda *args: switch)
    monkeypatch.setattr(gte.subprocess, "run", lambda *args, **kwargs: pytest.fail("SNMP forbidden"))
    for key, value in {"TOPOLOGY_OUTPUT_DIR": str(tmp_path), "TOPOLOGY_DEVICES": switch["ip"],
        "TOPOLOGY_DATA_SOURCE": "librenms", "TOPOLOGY_SERVER_ATTACHMENT_SOURCE": "librenms",
        "SERVER_PING": "server:10.2.0.1", "CORE_SWITCH_PING": "", "FIREWALL_PING": "",
        "TOPOLOGY_ARP_DEVICES": switch["ip"]}.items():
        monkeypatch.setenv(key, value)
    assert gte._run_collection() == 0
    assert api.calls.count((switch["ip"], "fdb")) == 1
    result = json.loads((tmp_path / "ap-attachments.json").read_text())
    assert len(result["attachments"]) == int(failure is None)
    assert json.loads((tmp_path / "edges.json").read_text()) == []
    assert json.loads((tmp_path / "server-attachments.json").read_text()) == []
    assert all(v == 0 for v in gte.collection_stats_snapshot().values())


def test_age_includes_collection_to_write_delay():
    aps = ap.current_ap_identities([identity()]); mac = aps[0]["mac"]
    row = candidate(mac, age=7190, evidence_observed_at=10000)
    assert ap.build_ap_artifact(aps, {mac: [row]}, now=10011)["attachments"] == []
    result = ap.build_ap_artifact(aps, {mac: [row]}, now=10005)
    assert result["attachments"][0]["evidence_age_seconds"] == 7195


def test_ap_unknown_port_allowed_without_changing_server_filter(monkeypatch):
    monkeypatch.setenv("FIREWALL_PING", "")
    aps = ap.current_ap_identities([identity()]); mac = aps[0]["mac"]
    switch = device("10.0.0.11", "access", [(1, 1, "Gi1/0/1", 4)])
    devices = {switch["ip"]: switch}
    api = ServerClient(devices, fdb={switch["ip"]: [{"port_id": 1, "mac_address": mac, "updated_at": time.time()}]})
    shared = gte.collect_librenms_fdb_inventory(devices, [], api, True)
    assert len(ap.build_ap_artifact(aps, shared[0])["attachments"]) == 1
    assert gte.build_librenms_fdb_candidates(devices, [], api, True, inventory=shared)[0] == {}
