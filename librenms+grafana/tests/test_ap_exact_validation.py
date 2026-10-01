"""T-05.1 VLAN FK、当前 exact GET 权威、代价与隔离回归。"""
import json
import time
from types import SimpleNamespace

import pytest
import ap_topology as ap
from .test_ap_topology import identity, candidate
from .test_topology_server_librenms import gte, device, ServerClient
from .test_librenms_client import make_client, attach_sequence, FakeResponse
from librenms_client import LibreNMSInvalidResponse


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    for key in ("CORE_SWITCH_PING", "FIREWALL_PING", "FIREWALL_UNIT_SNMP_TARGETS"):
        monkeypatch.setenv(key, "")
    for key in ("TOPOLOGY_AP_FDB_CANDIDATE_MAX_AGE_SECONDS", "TOPOLOGY_AP_FDB_CANDIDATE_CAP", "TOPOLOGY_AP_FDB_MAX_AGE_SECONDS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TOPOLOGY_SNMP_DELAY_MS", "0")
    gte.reset_collection_stats()


def seed(index=1, ip="10.0.0.11", age=20000, **fields):
    return candidate("02:00:00:00:00:01", ip=ip, index=index, age=age,
                     device_id=10, librenms_vlan_id=142, **fields)


def setup_inventory(rows=None, ports=None):
    aps = ap.current_ap_identities([identity()])
    switch = device("10.0.0.11", "access", ports or [(1, 1, "Gi1/0/1", 1)], device_id=10)
    api = ServerClient({switch["ip"]: switch})
    api.get_vlan_inventory = lambda: [{"device_id": 10, "vlan_id": 142, "vlan_vlan": 200}]
    return aps, {aps[0]["mac"]: rows or [seed()]}, {switch["ip"]: switch}, api


def validate(aps, inventory, devices, api, edges=()):
    return gte.collect_validated_ap_inventory(aps, inventory, devices, edges, api, "fixture-community")


def test_actual_librenms_vlan_url_has_fk_and_bounded_response():
    api = make_client(max_attempts=1)
    calls = attach_sequence(api, [FakeResponse({"vlans": [{"device_id": "10", "vlan_id": "142", "vlan_vlan": "200"}]})])
    mapping = ap.vlan_fk_mapping(api.get_vlan_inventory())
    assert mapping == {(10, 142): 200}
    assert calls[0]["url"] == "http://librenms:8000/api/v0/resources/vlans"
    assert api.max_response_bytes is None, "per-call bound must not mutate shared client configuration"
    limits = []
    class Oversized(FakeResponse):
        def read(self, limit=-1):
            limits.append(limit)
            return b"x" * limit
    attach_sequence(api, [Oversized()])
    with pytest.raises(LibreNMSInvalidResponse): api.get_vlan_inventory()
    assert limits == [4 * 1024 * 1024 + 1]
    attach_sequence(api, [FakeResponse({"vlans": [{"device_id": 10, "vlan_id": 142, "vlan_vlan": 200}] * 16385})])
    with pytest.raises(LibreNMSInvalidResponse): api.get_vlan_inventory()


def test_device_scoped_fk_conflicts_and_invalid_real_vlan_fail_closed():
    assert ap.vlan_fk_mapping([
        {"device_id": 10, "vlan_id": 142, "vlan_vlan": 200},
        {"device_id": 11, "vlan_id": 142, "vlan_vlan": 300}]) == {(10, 142): 200, (11, 142): 300}
    assert ap.vlan_fk_mapping([
        {"device_id": 10, "vlan_id": 142, "vlan_vlan": 200},
        {"device_id": 10, "vlan_id": 142, "vlan_vlan": 300},
        {"device_id": 10, "vlan_id": 143, "vlan_vlan": 4095}]) == {}


@pytest.mark.parametrize("age,expected", [(7201, 1), (20000, 1), (28800, 1), (28801, 0), (None, 0), (-1, 0)])
def test_historical_window_only_seeds_validator(monkeypatch, age, expected):
    aps, inventory, devices, api = setup_inventory([seed(age=age)])
    calls = []
    monkeypatch.setattr(gte, "lookup_fdb_ifindex", lambda ip, community, vlan, mac, names: calls.append(vlan) or 1)
    assert ap.build_ap_artifact(aps, inventory)["attachments"] == [], "raw FDB can never draw an AP edge"
    current = validate(aps, inventory, devices, api)
    artifact = ap.build_ap_artifact(aps, current)
    assert len(artifact["attachments"]) == expected and len(calls) == expected
    if expected:
        assert calls == [200], "internal DB id 142 must never reach exact lookup"
        row = artifact["attachments"][0]
        assert row["vlan"] == 200 and row["librenms_vlan_id"] == 142
        assert 0 <= row["evidence_age_seconds"] < 1
        assert artifact["source"] == "librenms-fdb+snmp-exact"
        assert artifact["candidate_source"] == "librenms-fdb"


@pytest.mark.parametrize("kind,accepted", [("physical", True), ("down", False), ("lag", False), ("member", False), ("uplink", False), ("missing", False)])
def test_same_switch_move_rechecks_returned_port(monkeypatch, kind, accepted):
    name = "Po2" if kind == "lag" else "Gi1/0/2"
    aps, inventory, devices, api = setup_inventory(ports=[(1, 1, "Gi1/0/1", 1), (2, 2, name, 2 if kind == "down" else 1)])
    if kind == "member": devices["10.0.0.11"]["ifstack"] = {100: [2]}
    edges = [{"from_ip": "10.0.0.11", "from_ifindex": 2, "to_ip": "10.0.0.12"}] if kind == "uplink" else []
    monkeypatch.setattr(gte, "lookup_fdb_ifindex", lambda *args: 999 if kind == "missing" else 2)
    artifact = ap.build_ap_artifact(aps, validate(aps, inventory, devices, api, edges))
    assert len(artifact["attachments"]) == int(accepted)
    if accepted: assert artifact["attachments"][0]["switch_ifindex"] == 2


def test_miss_ambiguity_dedup_cap_and_no_owner_choice(monkeypatch):
    aps, inventory, devices, api = setup_inventory([seed(), seed(index=2)])
    devices["10.0.0.11"]["ifname"][2] = "Gi1/0/2"
    outcomes = iter([1, 2])
    monkeypatch.setattr(gte, "lookup_fdb_ifindex", lambda *args: next(outcomes))
    artifact = ap.build_ap_artifact(aps, validate(aps, inventory, devices, api))
    assert artifact["attachments"] == [] and artifact["ambiguous_count"] == 1
    monkeypatch.setattr(gte, "lookup_fdb_ifindex", lambda *args: None)
    assert ap.build_ap_artifact(aps, validate(aps, inventory, devices, api))["attachments"] == []
    calls = []
    monkeypatch.setattr(gte, "lookup_fdb_ifindex", lambda *args: calls.append(args) or 1)
    inventory[aps[0]["mac"]] = [seed()] * 9
    assert len(ap.build_ap_artifact(aps, validate(aps, inventory, devices, api))["attachments"]) == 1
    assert len(calls) == 1, "identical rows deduped before GET"
    calls.clear()
    inventory[aps[0]["mac"]] = [seed(index=n) for n in range(1, 10)]
    assert validate(aps, inventory, devices, api) == {} and calls == []


@pytest.mark.parametrize("failure", ["vlan-api", "missing-fk", "snmp"])
def test_validation_failures_omit_edge(monkeypatch, failure):
    aps, inventory, devices, api = setup_inventory()
    if failure == "vlan-api": api.get_vlan_inventory = lambda: (_ for _ in ()).throw(ValueError("fixture"))
    if failure == "missing-fk": api.get_vlan_inventory = lambda: []
    monkeypatch.setattr(gte, "lookup_fdb_ifindex", lambda *args: (_ for _ in ()).throw(ValueError("fixture")))
    assert ap.build_ap_artifact(aps, validate(aps, inventory, devices, api))["attachments"] == []


def test_four_current_candidates_twelve_gets_no_walk_shared_vlans(monkeypatch):
    aps = ap.current_ap_identities([identity(n) for n in range(1, 5)])
    switch = device("10.0.0.11", "access", [(n, n, f"Gi1/0/{n}", 1) for n in range(1, 5)], device_id=10)
    inventory = {a["mac"]: [{**seed(index=n), "mac": a["mac"]}] for n, a in enumerate(aps, 1)}
    api = ServerClient({switch["ip"]: switch})
    vlan_calls = []
    api.get_vlan_inventory = lambda: vlan_calls.append(1) or [{"device_id": 10, "vlan_id": 142, "vlan_vlan": 200}]
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        assert command[0] == "snmpget", "AP never walks"
        assert "@142" not in command and not command[-1].startswith(gte.DOT1Q_TP_FDB_PORT_OID + ".142.")
        oid = command[-1]
        value = "" if oid.startswith(gte.DOT1Q_TP_FDB_PORT_OID) else oid.split(".")[-1]
        return SimpleNamespace(stdout=value)
    monkeypatch.setattr(gte.subprocess, "run", run)
    artifact = ap.build_ap_artifact(aps, validate(aps, inventory, {switch["ip"]: switch}, api))
    assert len(artifact["attachments"]) == 4 and vlan_calls == [1]
    assert len(commands) == 12
    assert gte.collection_stats_snapshot() == {"ap_snmp_gets": 12, "ap_snmp_walks": 0,
        "direct_snmp_gets": 0, "direct_snmp_walks": 0, "server_snmp_gets": 0, "server_snmp_walks": 0}
    gte.lookup_fdb_ifindex(switch["ip"], "fixture", 200, aps[0]["mac"], switch["ifname"])
    assert gte.collection_stats_snapshot()["server_snmp_gets"] == 3
    assert gte.collection_stats_snapshot()["ap_snmp_gets"] == 12


def test_exact_evidence_still_ages_out():
    aps = ap.current_ap_identities([identity()])
    row = {**seed(), "vlan": 200, "validated_at": 10000}
    assert ap.build_ap_artifact(aps, {aps[0]["mac"]: [row]}, now=17200)["attachments"]
    assert not ap.build_ap_artifact(aps, {aps[0]["mac"]: [row]}, now=17201)["attachments"]


def test_shared_fdb_preserves_server_vlan_and_window(monkeypatch):
    aps, _, devices, api = setup_inventory()
    api.fdb = {"10.0.0.11": [{"port_id": 1, "mac_address": aps[0]["mac"], "vlan_id": 142, "updated_at": time.time()}]}
    shared = gte.collect_librenms_fdb_inventory(devices, [], api, True)
    servers, _ = gte.build_librenms_fdb_candidates(devices, [], api, True, inventory=shared)
    assert servers[aps[0]["mac"]][0]["vlan"] == 142
    ap_seed = ap.ap_candidates(aps, shared[0])[aps[0]["mac"]][0]
    assert ap_seed["librenms_vlan_id"] == 142 and "vlan" not in ap_seed
    monkeypatch.setattr(gte, "lookup_fdb_ifindex", lambda *args: 1)
    assert ap.build_ap_artifact(aps, validate(aps, shared[0], devices, api))["attachments"][0]["vlan"] == 200
    assert api.calls == [("10.0.0.11", "fdb")]
    assert gte.topology_librenms_fdb_max_age() == 900


@pytest.mark.parametrize("filter_kind", ["core", "firewall", "lag", "down", "uplink"])
def test_structural_seeds_rejected_before_exact_get(monkeypatch, filter_kind):
    aps, _, devices, api = setup_inventory(ports=[(1, 1, "Po1" if filter_kind == "lag" else "Gi1/0/1", 2 if filter_kind == "down" else 1)])
    if filter_kind == "core": monkeypatch.setenv("CORE_SWITCH_PING", "10.0.0.11")
    if filter_kind == "firewall": devices["10.0.0.11"]["librenms_metadata"]["os"] = "stoneos"
    edges = [{"from_ip": "10.0.0.11", "from_ifindex": 1, "to_ip": "10.0.0.12"}] if filter_kind == "uplink" else []
    api.fdb = {"10.0.0.11": [{"port_id": 1, "mac_address": aps[0]["mac"], "vlan_id": 142, "updated_at": time.time()}]}
    shared = gte.collect_librenms_fdb_inventory(devices, edges, api, True)
    monkeypatch.setattr(gte, "lookup_fdb_ifindex", lambda *args: pytest.fail("filtered candidate must never query SNMP"))
    assert not ap.build_ap_artifact(aps, validate(aps, shared[0], devices, api, edges))["attachments"]


@pytest.mark.parametrize("failure", [None, "prometheus", "vlan", "snmp"])
def test_full_cycle_failure_isolation_and_shared_cost(tmp_path, monkeypatch, capsys, failure):
    aps, _, devices, api = setup_inventory()
    api.fdb = {"10.0.0.11": [{"port_id": 1, "mac_address": aps[0]["mac"], "vlan_id": 142, "updated_at": time.time() - 20000}]}
    api.list_devices = lambda: list(devices.values())
    calls = []
    def vlans():
        calls.append(1); api.request_count += 1
        if failure == "vlan": raise ValueError("fixture")
        return [{"device_id": 10, "vlan_id": 142, "vlan_vlan": 200}]
    api.get_vlan_inventory = vlans
    def fetch():
        if failure == "prometheus": raise ValueError("fixture")
        return aps
    monkeypatch.setattr(gte, "fetch_current_aps", fetch)
    monkeypatch.setattr(gte, "LibreNMSClient", lambda: api)
    monkeypatch.setattr(gte, "collect_device_by_source", lambda *args: devices["10.0.0.11"])
    wired_edge = {"from_ip": "10.0.0.11", "from_ifindex": 99, "from_port": "Gi1/0/99",
                  "to_ip": "10.0.0.99", "to_ifindex": 99, "to_port": "Gi1/0/99", "source": "lldp"}
    monkeypatch.setattr(gte, "build_edges", lambda *args, **kwargs: ([dict(wired_edge)], []))
    monkeypatch.setattr(gte, "lookup_fdb_ifindex", lambda *args: None if failure == "snmp" else 1)
    monkeypatch.setattr(gte.subprocess, "run", lambda *args, **kwargs: pytest.fail("no implicit SNMP walk/fallback"))
    for key, value in {"TOPOLOGY_OUTPUT_DIR": str(tmp_path), "TOPOLOGY_DEVICES": "10.0.0.11",
        "TOPOLOGY_DATA_SOURCE": "librenms", "TOPOLOGY_SERVER_ATTACHMENT_SOURCE": "librenms",
        "SERVER_PING": "server:10.2.0.1", "TOPOLOGY_ARP_DEVICES": "10.0.0.11"}.items():
        monkeypatch.setenv(key, value)
    assert gte._run_collection() == 0
    artifact = json.loads((tmp_path / "ap-attachments.json").read_text())
    assert len(artifact["attachments"]) == int(failure is None)
    edges = json.loads((tmp_path / "edges.json").read_text())
    assert len(edges) == 1 and {key: edges[0][key] for key in wired_edge} == wired_edge
    assert edges[0]["stale"] is False and edges[0]["last_seen"] > 0
    assert json.loads((tmp_path / "server-attachments.json").read_text()) == []
    assert api.calls.count(("10.0.0.11", "fdb")) == 1
    assert len(calls) == int(failure != "prometheus")
    log = capsys.readouterr().err
    assert "server attachment stats: api_requests=2" in log


def test_partial_17_ap_fixture_stays_authoritative(monkeypatch):
    aps = ap.current_ap_identities([identity(n) for n in range(1, 18)])
    switch = device("10.0.0.11", "access", [(n, n, f"Gi1/0/{n}", 1) for n in range(1, 8)], device_id=10)
    inventory = {a["mac"]: [{**seed(index=n), "mac": a["mac"]}] for n, a in enumerate(aps[:7], 1)}
    api = ServerClient({switch["ip"]: switch})
    api.get_vlan_inventory = lambda: [{"device_id": 10, "vlan_id": 142, "vlan_vlan": 200}]
    monkeypatch.setattr(gte, "lookup_fdb_ifindex", lambda ip, community, vlan, mac, names: int(mac.split(":")[-1], 16))
    artifact = ap.build_ap_artifact(aps, validate(aps, inventory, {switch["ip"]: switch}, api))
    assert (len(artifact["attachments"]), artifact["ambiguous_count"], artifact["unresolved_count"]) == (7, 0, 10)


@pytest.mark.parametrize("count,gets", [(8, 8), (9, 0)])
def test_hard_candidate_cap_never_truncates(monkeypatch, count, gets):
    aps, inventory, devices, api = setup_inventory([seed(index=n) for n in range(1, count + 1)])
    calls = []
    monkeypatch.setattr(gte, "lookup_fdb_ifindex", lambda *args: calls.append(1) or 1)
    artifact = ap.build_ap_artifact(aps, validate(aps, inventory, devices, api))
    assert len(calls) == gets
    assert len(artifact["attachments"]) == int(gets > 0)
