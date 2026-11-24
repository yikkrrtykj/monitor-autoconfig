"""T-05.1 VLAN FK、当前 exact GET 权威、代价与隔离回归。"""
import json
import time
from types import SimpleNamespace

import pytest
import ap_topology as ap
from .test_ap_topology import identity, candidate
from .test_topology_server_librenms import gte, device, ServerClient
from .test_librenms_client import make_client, attach_sequence, FakeResponse
from librenms_client import LibreNMSInvalidResponse, LibreNMSUnavailable


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    for key in ("CORE_SWITCH_PING", "FIREWALL_PING", "FIREWALL_UNIT_SNMP_TARGETS", "TOPOLOGY_CORE_AP_PORTS"):
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
    monkeypatch.setattr(gte, "lookup_fdb_ifindex", lambda *args, **kwargs: calls.append(args) or 1)
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


@pytest.mark.parametrize("count,vlan,has_vlan_layer", [(4, 200, False), (5, 60, True)])
def test_current_candidates_keep_exact_get_budget_and_share_vlan_inventory(monkeypatch, count, vlan, has_vlan_layer):
    aps = ap.current_ap_identities([identity(n) for n in range(1, count + 1)])
    switch = device("10.0.0.11", "access", [(n, n, f"Gi1/0/{n}", 1) for n in range(1, count + 1)], device_id=10)
    if has_vlan_layer:
        switch["ifname"][60] = "Vlan60"
        switch["ifstack"] = {60: list(range(1, count + 1))}
    devices = {switch["ip"]: switch}
    api = ServerClient({switch["ip"]: switch})
    api.fdb = {switch["ip"]: [
        {"port_id": n, "mac_address": row["mac"], "vlan_id": 142, "updated_at": time.time()}
        for n, row in enumerate(aps, 1)
    ]}
    inventory, _ = gte.collect_librenms_fdb_inventory(devices, [], api, True)
    vlan_calls = []
    api.get_vlan_inventory = lambda: vlan_calls.append(1) or [{"device_id": 10, "vlan_id": 142, "vlan_vlan": vlan}]
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        assert command[0] == "snmpget", "AP never walks"
        assert "@142" not in command and not command[-1].startswith(gte.DOT1Q_TP_FDB_PORT_OID + ".142.")
        oid = command[-1]
        value = "" if oid.startswith(gte.DOT1Q_TP_FDB_PORT_OID) else oid.split(".")[-1]
        return SimpleNamespace(stdout=value)
    monkeypatch.setattr(gte.subprocess, "run", run)
    artifact = ap.build_ap_artifact(aps, validate(aps, inventory, devices, api))
    assert [(row["ap_mac"], row["switch_port"], row["vlan"]) for row in artifact["attachments"]] == [
        (row["mac"], f"Gi1/0/{n}", vlan) for n, row in enumerate(aps, 1)
    ]
    assert vlan_calls == [1] and api.calls == [(switch["ip"], "fdb")]
    assert len(commands) == 3 * count
    assert gte.collection_stats_snapshot() == {"ap_snmp_gets": 3 * count, "ap_snmp_walks": 0,
        "direct_snmp_gets": 0, "direct_snmp_walks": 0, "server_snmp_gets": 0, "server_snmp_walks": 0}
    gte.lookup_fdb_ifindex(switch["ip"], "fixture", vlan, aps[0]["mac"], switch["ifname"])
    assert gte.collection_stats_snapshot()["server_snmp_gets"] == 3
    assert gte.collection_stats_snapshot()["ap_snmp_gets"] == 3 * count


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


def test_core_direct_ap_survives_validation_and_artifact_output(tmp_path, monkeypatch):
    aps, _, devices, api = setup_inventory()
    monkeypatch.setenv("CORE_SWITCH_PING", "core:10.0.0.11")
    monkeypatch.setenv("TOPOLOGY_CORE_AP_PORTS", "10.0.0.11:gi1/0/1")
    api.fdb = {"10.0.0.11": [{"port_id": 1, "mac_address": aps[0]["mac"],
                             "vlan_id": 142, "updated_at": time.time()}]}
    shared, _ = gte.collect_librenms_fdb_inventory(devices, [], api, True)
    calls = []
    monkeypatch.setattr(gte, "lookup_fdb_ifindex",
                        lambda ip, community, vlan, mac, names: calls.append((ip, vlan, mac)) or 1)
    current = validate(aps, shared, devices, api)
    gte.write_current_ap_artifact(str(tmp_path), aps, current)
    artifact = json.loads((tmp_path / "ap-attachments.json").read_text())
    assert calls == [("10.0.0.11", 200, aps[0]["mac"])]
    assert len(artifact["attachments"]) == 1
    assert artifact["attachments"][0]["switch_ip"] == "10.0.0.11"
    assert artifact["attachments"][0]["switch_port"] == "Gi1/0/1"


@pytest.mark.parametrize("core_result,expected", [(2, "downstream"), (1, "ambiguous")])
def test_core_conflict_or_downstream_outcome_follows_current_validation(
        monkeypatch, core_result, expected):
    aps, _, devices, api = setup_inventory(ports=[(1, 1, "Gi1/0/1", 1), (2, 2, "Gi1/0/2", 1)])
    monkeypatch.setenv("CORE_SWITCH_PING", "10.0.0.11")
    monkeypatch.setenv("TOPOLOGY_CORE_AP_PORTS", "10.0.0.11:Gi1/0/1,10.0.0.11:Gi1/0/2")
    devices["10.0.0.12"] = device("10.0.0.12", "access", [(3, 3, "Gi0/3", 1)], device_id=11)
    api.get_vlan_inventory = lambda: [
        {"device_id": 10, "vlan_id": 142, "vlan_vlan": 200},
        {"device_id": 11, "vlan_id": 143, "vlan_vlan": 200}]
    api.fdb = {
        "10.0.0.11": [{"port_id": n, "mac_address": aps[0]["mac"], "vlan_id": 142,
                       "updated_at": time.time()} for n in (1, 2)],
        "10.0.0.12": [{"port_id": 3, "mac_address": aps[0]["mac"], "vlan_id": 143,
                       "updated_at": time.time()}]}
    edges = [{"from_ip": "10.0.0.11", "from_ifindex": 2,
              "to_ip": "10.0.0.12", "to_ifindex": 9}]
    shared, _ = gte.collect_librenms_fdb_inventory(devices, edges, api, True)
    assert len(shared[aps[0]["mac"]]) == 2, "known inter-switch port excluded before GET"
    monkeypatch.setattr(gte, "lookup_fdb_ifindex",
                        lambda ip, *args: core_result if ip == "10.0.0.11" else 3)
    artifact = ap.build_ap_artifact(aps, validate(aps, shared, devices, api, edges),
                                    core_ips={"10.0.0.11"})
    if expected == "downstream":
        assert len(artifact["attachments"]) == 1
        assert artifact["attachments"][0]["switch_ip"] == "10.0.0.12"
        assert artifact["attachments"][0].get("level") is None
    else:
        assert artifact["attachments"] == []
        assert artifact["ambiguous_count"] == 1


@pytest.mark.parametrize("fdb_unavailable", [True, False])
def test_core_forwarding_is_observed_but_never_confirmed_until_access_fdb_recovers(
        monkeypatch, fdb_unavailable):
    aps, _, devices, api = setup_inventory(ports=[(1, 1, "Gi1/0/1", 1), (2, 2, "Gi1/0/2", 1)])
    core, access = "10.0.0.11", "10.0.0.12"
    monkeypatch.setenv("CORE_SWITCH_PING", core)
    monkeypatch.setenv("TOPOLOGY_CORE_AP_PORTS", f"{core}:Gi1/0/1")
    devices[access] = device(access, "access", [(3, 3, "Gi0/3", 1)], device_id=11)
    api.get_vlan_inventory = lambda: [
        {"device_id": 10, "vlan_id": 142, "vlan_vlan": 200},
        {"device_id": 11, "vlan_id": 143, "vlan_vlan": 200}]
    mac = aps[0]["mac"]
    api.fdb = {core: [{"port_id": 2, "mac_address": mac, "vlan_id": 142, "updated_at": time.time()}]}
    if fdb_unavailable:
        api.failures[(access, "fdb")] = LibreNMSUnavailable("fixture FDB unavailable")
    calls = []
    monkeypatch.setattr(gte, "lookup_fdb_ifindex",
                        lambda ip, *args: calls.append(ip) or (2 if ip == core else 3))

    shared, status = gte.collect_librenms_fdb_inventory(devices, [], api, True)
    assert status["failed_switches"] == int(fdb_unavailable)
    assert [row["switch_ip"] for row in shared[mac]] == [core]
    artifact = ap.build_ap_artifact(aps, validate(aps, shared, devices, api),
                                    core_ips={core})
    assert len(artifact["attachments"]) == 1
    attachment = artifact["attachments"][0]
    assert (attachment["switch_ip"], attachment["switch_port"]) == (core, "Gi1/0/2")
    assert attachment["level"] == "observed-attachment"
    assert attachment["level"] != "confirmed-direct"
    assert calls == [core]

    api.failures.clear()
    api.fdb[access] = [{"port_id": 3, "mac_address": mac, "vlan_id": 143, "updated_at": time.time()}]
    calls.clear()
    shared, _ = gte.collect_librenms_fdb_inventory(devices, [], api, True)
    artifact = ap.build_ap_artifact(aps, validate(aps, shared, devices, api),
                                    core_ips={core})
    # Both current exact positions are valid; #97 contract requires ambiguous.
    assert artifact["attachments"] == []
    assert artifact["ambiguous_count"] == 1
    assert calls == [core, access]


def _downstream_core_inventory(aps, devices, api):
    core, access = "10.0.0.11", "10.0.0.12"
    api.get_vlan_inventory = lambda: [
        {"device_id": 10, "vlan_id": 142, "vlan_vlan": 200},
        {"device_id": 11, "vlan_id": 143, "vlan_vlan": 200}]
    mac = aps[0]["mac"]
    core_row = seed(ip=core, index=1)
    access_row = seed(ip=access, index=3)
    access_row.update({"device_id": 11, "librenms_vlan_id": 143})
    devices[access] = device(access, "access", [(3, 3, "Gi0/3", 1)], device_id=11)
    return mac, {mac: [core_row, access_row]}


def test_downstream_exact_miss_does_not_block_core_current_success(monkeypatch):
    aps, _, devices, api = setup_inventory(ports=[(1, 1, "Gi1/0/1", 1)])
    monkeypatch.setenv("CORE_SWITCH_PING", "10.0.0.11")
    mac, inventory = _downstream_core_inventory(aps, devices, api)
    calls = []
    monkeypatch.setattr(gte, "lookup_fdb_ifindex",
                        lambda ip, *args, **kwargs: calls.append(ip) or (None if ip == "10.0.0.12" else 1))
    artifact = ap.build_ap_artifact(aps, validate(aps, inventory, devices, api),
                                    core_ips={"10.0.0.11"})
    assert calls == ["10.0.0.11", "10.0.0.12"]
    assert len(artifact["attachments"]) == 1
    assert artifact["attachments"][0]["switch_ip"] == "10.0.0.11"
    assert artifact["attachments"][0]["level"] == "observed-attachment"


def test_downstream_query_exception_blocks_core_selection(monkeypatch):
    aps, _, devices, api = setup_inventory(ports=[(1, 1, "Gi1/0/1", 1)])
    monkeypatch.setenv("CORE_SWITCH_PING", "10.0.0.11")
    _mac, inventory = _downstream_core_inventory(aps, devices, api)
    calls = []
    def lookup(ip, *args, **kwargs):
        calls.append(ip)
        if ip == "10.0.0.12":
            raise TimeoutError("fixture downstream timeout")
        return 1
    monkeypatch.setattr(gte, "lookup_fdb_ifindex", lookup)
    artifact = ap.build_ap_artifact(aps, validate(aps, inventory, devices, api),
                                    core_ips={"10.0.0.11"})
    assert calls == ["10.0.0.11", "10.0.0.12"]
    assert artifact["attachments"] == []


@pytest.mark.parametrize("allowlist", ["", "10.0.0.11:Po1", "10.0.0.12:Gi1/0/1", "10.0.0.11:Gi1/0/1,bad"])
def test_core_without_manual_allowlist_still_gets_current_exact_observed(monkeypatch, allowlist):
    aps, inventory, devices, api = setup_inventory()
    monkeypatch.setenv("CORE_SWITCH_PING", "10.0.0.11")
    monkeypatch.setenv("TOPOLOGY_CORE_AP_PORTS", allowlist)
    calls = []
    monkeypatch.setattr(gte, "lookup_fdb_ifindex", lambda *args, **kwargs: calls.append(args) or 1)
    validated = validate(aps, inventory, devices, api)
    assert calls == [("10.0.0.11", "fixture-community", 200, aps[0]["mac"], {1: "Gi1/0/1"})]
    artifact = ap.build_ap_artifact(aps, validated, core_ips={"10.0.0.11"})
    assert len(artifact["attachments"]) == 1
    assert artifact["attachments"][0]["level"] == "observed-attachment"
    assert artifact["attachments"][0]["reason"] == "core_forwarding_egress_observed"


@pytest.mark.parametrize("block,port,level", [
    ("moved", "Te1/0/1", "observed-attachment"),
    ("aged", "Gi1/0/1", "observed-attachment"),
    ("aggregate-member", None, None),
    ("down", None, None),
])
def test_core_move_uses_current_exact_and_never_auto_confirms(monkeypatch, block, port, level):
    # Gi and Te may share their numeric suffix; confirmation identifies the typed ifName.
    aps, inventory, devices, api = setup_inventory(
        rows=[seed(age=28801 if block == "aged" else 0)],
        ports=[(1, 1, "Gi1/0/1", 2 if block == "down" else 1), (2, 2, "Te1/0/1", 1)],
    )
    monkeypatch.setenv("CORE_SWITCH_PING", "10.0.0.11")
    # A moved port is the only case that needs a manual old-port allowlist.
    # The current port must still be revalidated; it must not inherit confirmation.
    monkeypatch.setenv("TOPOLOGY_CORE_AP_PORTS",
                       "10.0.0.11:Gi1/0/1" if block == "moved" else "")
    if block == "aggregate-member":
        devices["10.0.0.11"]["ifstack"] = {4: [1]}
    calls = []
    monkeypatch.setattr(gte, "lookup_fdb_ifindex",
                        lambda *args, **kwargs: calls.append(args) or (2 if block == "moved" else 1))
    artifact = ap.build_ap_artifact(aps, validate(aps, inventory, devices, api),
                                    core_ips={"10.0.0.11"})
    if port is None:
        assert artifact["attachments"] == []
        return
    assert len(artifact["attachments"]) == 1
    row = artifact["attachments"][0]
    assert (row["switch_ip"], row["switch_port"]) == ("10.0.0.11", port)
    assert row["level"] == level
    assert row["level"] != "confirmed-direct"


def test_candidate_status_distinguishes_empty_cap_and_normal():
    aps = ap.current_ap_identities([identity()])
    mac = aps[0]["mac"]
    normal = {mac: [seed(index=1)]}
    candidates, status = ap.ap_candidates_with_status(aps, normal)
    assert mac in candidates and mac in status["normal_candidates"]
    assert mac not in status["candidate_cap_exceeded"] and mac not in status["no_candidate"]

    empty, status = ap.ap_candidates_with_status(aps, {})
    assert empty == {} and mac in status["no_candidate"]

    too_many = {mac: [seed(index=n) for n in range(1, 10)]}
    candidates, status = ap.ap_candidates_with_status(aps, too_many)
    assert candidates == {} and mac in status["candidate_cap_exceeded"]


def test_candidate_cap_exceeded_never_falls_back_to_hint(monkeypatch):
    aps, inventory, devices, api = setup_inventory(
        rows=[seed(index=n) for n in range(1, 10)],
        ports=[(n, n, f"Gi1/0/{n}", 1) for n in range(1, 10)],
    )
    monkeypatch.setenv("CORE_SWITCH_PING", "10.0.0.11")
    calls = []
    monkeypatch.setattr(gte, "lookup_fdb_ifindex",
                        lambda *args, **kwargs: calls.append(args) or 1)
    artifact = ap.build_ap_artifact(aps, validate(aps, inventory, devices, api),
                                    core_ips={"10.0.0.11"})
    assert calls == []
    assert artifact["attachments"] == []
    assert artifact["ambiguous_count"] == 0


def test_candidate_cap_zero_never_falls_back_to_hint(monkeypatch):
    aps, inventory, devices, api = setup_inventory(
        rows=[seed(index=45, port_name="Gi2/0/45")],
        ports=[(45, 45, "Gi2/0/45", 1)],
    )
    old = seed(age=86400, index=46)
    old["evidence_observed_at"] = time.time()
    inventory[aps[0]["mac"]].append(old)
    monkeypatch.setenv("CORE_SWITCH_PING", "10.0.0.11")
    monkeypatch.setenv("TOPOLOGY_AP_FDB_CANDIDATE_CAP", "0")
    calls = []
    monkeypatch.setattr(gte, "lookup_fdb_ifindex",
                        lambda *args, **kwargs: calls.append(args) or 45)
    artifact = ap.build_ap_artifact(aps, validate(aps, inventory, devices, api),
                                    core_ips={"10.0.0.11"})
    assert calls == []
    assert artifact["attachments"] == []


def test_no_current_candidate_one_legal_hint_still_attaches(monkeypatch):
    aps, inventory, devices, api = setup_inventory(
        rows=[seed(age=86400, index=46)],
        ports=[(45, 45, "Gi2/0/45", 1), (46, 46, "Gi2/0/46", 1)],
    )
    inventory[aps[0]["mac"]][0]["evidence_observed_at"] = time.time()
    monkeypatch.setenv("CORE_SWITCH_PING", "10.0.0.11")
    calls = []
    monkeypatch.setattr(gte, "lookup_fdb_ifindex",
                        lambda *args, **kwargs: calls.append(args) or 45)
    artifact = ap.build_ap_artifact(aps, validate(aps, inventory, devices, api),
                                    core_ips={"10.0.0.11"})
    assert len(calls) == 1
    assert len(artifact["attachments"]) == 1
    assert artifact["attachments"][0]["switch_port"] == "Gi2/0/45"
    assert artifact["attachments"][0]["level"] == "observed-attachment"


def test_hint_old_down_row_does_not_taint_current_up_port(monkeypatch):
    aps, inventory, devices, api = setup_inventory(
        rows=[seed(age=86400, index=46, port_name="Gi2/0/46", confirmed_down=True)],
        ports=[(45, 45, "Gi2/0/45", 1), (46, 46, "Gi2/0/46", 2)],
    )
    inventory[aps[0]["mac"]][0]["evidence_observed_at"] = time.time()
    monkeypatch.setenv("CORE_SWITCH_PING", "10.0.0.11")
    monkeypatch.setenv("TOPOLOGY_CORE_AP_PORTS", "")
    calls = []
    monkeypatch.setattr(gte, "lookup_fdb_ifindex",
                        lambda *args, **kwargs: calls.append(args) or 45)
    artifact = ap.build_ap_artifact(aps, validate(aps, inventory, devices, api),
                                    core_ips={"10.0.0.11"})
    assert calls == [("10.0.0.11", "fixture-community", 200, aps[0]["mac"], {45: "Gi2/0/45", 46: "Gi2/0/46"})]
    validated = validate(aps, inventory, devices, api)
    current = validated[aps[0]["mac"]][0]
    assert current["ifindex"] == 45
    assert current["port_name"] == "Gi2/0/45"
    assert "confirmed_down" not in current, "old port status must not taint the current row"
    artifact = ap.build_ap_artifact(aps, validated, core_ips={"10.0.0.11"})
    assert len(artifact["attachments"]) == 1
    row = artifact["attachments"][0]
    assert (row["switch_ip"], row["switch_port"]) == ("10.0.0.11", "Gi2/0/45")
    assert row["level"] == "observed-attachment"


def test_partial_hint_lookup_budget_never_emits_attachment(monkeypatch):
    aps, inventory, devices, api = setup_inventory(
        rows=[seed(age=86400, index=45, port_name="Gi2/0/45")],
        ports=[(45, 45, "Gi2/0/45", 1), (46, 46, "Gi2/0/46", 1),
               (47, 47, "Gi2/0/47", 1)],
    )
    mac = aps[0]["mac"]
    inventory[mac][0].update({"device_id": 10, "librenms_vlan_id": 142})
    inventory[mac][0]["evidence_observed_at"] = time.time()
    for index, internal, vlan in ((46, 143, 201), (47, 144, 202)):
        row = seed(age=86400, index=index)
        row.update({"device_id": 10, "librenms_vlan_id": internal,
                    "port_name": f"Gi2/0/{index}", "evidence_observed_at": time.time()})
        inventory[mac].append(row)
    api.get_vlan_inventory = lambda: [
        {"device_id": 10, "vlan_id": 142, "vlan_vlan": 200},
        {"device_id": 10, "vlan_id": 143, "vlan_vlan": 201},
        {"device_id": 10, "vlan_id": 144, "vlan_vlan": 202},
    ]
    monkeypatch.setenv("CORE_SWITCH_PING", "10.0.0.11")
    monkeypatch.setenv("TOPOLOGY_CORE_AP_HINT_MAX_LOOKUPS", "2")
    calls = []
    monkeypatch.setattr(gte, "lookup_fdb_ifindex",
                        lambda *args, **kwargs: calls.append(args) or 45)
    artifact = ap.build_ap_artifact(aps, validate(aps, inventory, devices, api),
                                    core_ips={"10.0.0.11"})
    assert len(calls) == 2, "only part of the legal hints fit the hard budget"
    assert artifact["attachments"] == []


def test_hint_lookup_exception_fails_closed(monkeypatch):
    aps, inventory, devices, api = setup_inventory(
        rows=[seed(age=86400, index=45, port_name="Gi2/0/45")],
        ports=[(45, 45, "Gi2/0/45", 1)])
    inventory[aps[0]["mac"]][0]["evidence_observed_at"] = time.time()
    monkeypatch.setenv("CORE_SWITCH_PING", "10.0.0.11")
    def lookup(*args, **kwargs):
        raise TimeoutError("fixture hint timeout")
    monkeypatch.setattr(gte, "lookup_fdb_ifindex", lookup)
    artifact = ap.build_ap_artifact(aps, validate(aps, inventory, devices, api),
                                    core_ips={"10.0.0.11"})
    assert artifact["attachments"] == []


def test_current_core_miss_does_not_skip_historical_hint(monkeypatch):
    aps, inventory, devices, api = setup_inventory(
        rows=[seed(age=0, index=46)], ports=[(45, 45, "Gi2/0/45", 1), (46, 46, "Gi2/0/46", 1)]
    )
    mac = aps[0]["mac"]
    old = seed(age=86400, index=46)
    old.update({"librenms_vlan_id": 143, "evidence_observed_at": time.time()})
    inventory[mac].append(old)
    api.get_vlan_inventory = lambda: [
        {"device_id": 10, "vlan_id": 142, "vlan_vlan": 200},
        {"device_id": 10, "vlan_id": 143, "vlan_vlan": 201},
    ]
    monkeypatch.setenv("CORE_SWITCH_PING", "10.0.0.11")
    calls = []
    def lookup(ip, community, vlan, mac_addr, names, **kwargs):
        calls.append(vlan)
        return None if vlan == 200 else 45
    monkeypatch.setattr(gte, "lookup_fdb_ifindex", lookup)
    artifact = ap.build_ap_artifact(aps, validate(aps, inventory, devices, api),
                                    core_ips={"10.0.0.11"})
    assert calls == [200, 200, 201], "current core miss must not skip other historical hints"
    assert len(artifact["attachments"]) == 1
    assert artifact["attachments"][0]["switch_port"] == "Gi2/0/45"


def test_multiple_core_candidates_remain_ambiguous(monkeypatch):
    aps, inventory, devices, api = setup_inventory(
        rows=[seed(ip="10.0.0.11", index=1)],
        ports=[(1, 1, "Gi1/0/1", 1)],
    )
    mac = aps[0]["mac"]
    second = seed(ip="10.0.0.12", index=2)
    second.update({"device_id": 11, "librenms_vlan_id": 143})
    inventory[mac].append(second)
    devices["10.0.0.12"] = device("10.0.0.12", "core", [(2, 2, "Gi2/0/2", 1)], device_id=11)
    monkeypatch.setenv("CORE_SWITCH_PING", "10.0.0.11,10.0.0.12")
    api.get_vlan_inventory = lambda: [
        {"device_id": 10, "vlan_id": 142, "vlan_vlan": 200},
        {"device_id": 11, "vlan_id": 143, "vlan_vlan": 200},
    ]
    monkeypatch.setattr(gte, "lookup_fdb_ifindex",
                        lambda ip, *args, **kwargs: 1 if ip == "10.0.0.11" else 2)
    artifact = ap.build_ap_artifact(
        aps, validate(aps, inventory, devices, api),
        core_ips={"10.0.0.11", "10.0.0.12"})
    assert artifact["attachments"] == []
    assert artifact["ambiguous_count"] == 1


def _online(mac, status):
    return {mac: {"status": status, "source": "fixture-online", "reason": "fixture"}}


def test_core_manual_allowlist_confirms_current_port_only_when_online(monkeypatch):
    aps, inventory, devices, api = setup_inventory(ports=[(1, 1, "Gi1/0/1", 1)])
    monkeypatch.setenv("CORE_SWITCH_PING", "10.0.0.11")
    monkeypatch.setenv("TOPOLOGY_CORE_AP_PORTS", "10.0.0.11:Gi1/0/1")
    monkeypatch.setattr(gte, "lookup_fdb_ifindex", lambda *args: 1)
    validated = validate(aps, inventory, devices, api)
    mac = aps[0]["mac"]
    online = ap.build_ap_artifact(
        aps, validated, core_ips={"10.0.0.11"}, online_evidence=_online(mac, "online"))
    assert len(online["attachments"]) == 1
    assert online["attachments"][0]["level"] == "confirmed-direct"
    assert online["attachments"][0]["reason"] == "manual_core_port_operator_confirmed"
    assert online["attachments"][0]["online"] == "online"

    unknown = ap.build_ap_artifact(
        aps, validated, core_ips={"10.0.0.11"}, online_evidence=_online(mac, "unknown"))
    assert len(unknown["attachments"]) == 1
    assert unknown["attachments"][0]["level"] == "observed-attachment"
    assert unknown["attachments"][0]["online"] == "unknown"

    offline = ap.build_ap_artifact(
        aps, validated, core_ips={"10.0.0.11"}, online_evidence=_online(mac, "offline"))
    assert offline["attachments"] == []


@pytest.mark.parametrize("old_age,hint_limit,expected_calls,expected_attachment", [
    (86400, 604800, 1, True),
    (86400, 3600, 0, False),
])
def test_core_hint_is_bounded_and_uses_current_exact_only(
        monkeypatch, old_age, hint_limit, expected_calls, expected_attachment):
    aps, inventory, devices, api = setup_inventory(
        rows=[seed(age=old_age)], ports=[(1, 1, "Gi1/0/1", 1)])
    inventory[aps[0]["mac"]][0]["evidence_observed_at"] = time.time()
    monkeypatch.setenv("CORE_SWITCH_PING", "10.0.0.11")
    monkeypatch.setenv("TOPOLOGY_CORE_AP_HINT_MAX_AGE_SECONDS", str(hint_limit))
    calls = []
    monkeypatch.setattr(gte, "lookup_fdb_ifindex", lambda *args, **kwargs: calls.append(args) or 1)
    artifact = ap.build_ap_artifact(aps, validate(aps, inventory, devices, api),
                                    core_ips={"10.0.0.11"})
    assert len(calls) == expected_calls
    assert bool(artifact["attachments"]) is expected_attachment
    if expected_attachment:
        assert artifact["attachments"][0]["level"] == "observed-attachment"


def test_core_hint_budget_exhaustion_fails_closed(monkeypatch):
    aps, inventory, devices, api = setup_inventory(
        rows=[seed(age=86400)], ports=[(1, 1, "Gi1/0/1", 1)])
    inventory[aps[0]["mac"]][0]["evidence_observed_at"] = time.time()
    monkeypatch.setenv("CORE_SWITCH_PING", "10.0.0.11")
    monkeypatch.setenv("TOPOLOGY_CORE_AP_HINT_MAX_GETS", "0")
    calls = []
    monkeypatch.setattr(gte, "lookup_fdb_ifindex", lambda *args, **kwargs: calls.append(args) or 1)
    artifact = ap.build_ap_artifact(aps, validate(aps, inventory, devices, api),
                                    core_ips={"10.0.0.11"})
    assert calls == []
    assert artifact["attachments"] == []


@pytest.mark.parametrize("filter_kind", ["firewall", "lag", "down", "uplink"])
def test_structural_seeds_rejected_before_exact_get(monkeypatch, filter_kind):
    aps, _, devices, api = setup_inventory(ports=[(1, 1, "Po1" if filter_kind == "lag" else "Gi1/0/1", 2 if filter_kind == "down" else 1)])
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

@pytest.mark.parametrize("higher_names,expected", [
    (("Vlan60",), True),
    (("VLAN-1002",), True),
    (("Vl60",), True),
    (("Vlanif60",), True),
    (("VLAN_60",), True),
    (("  vLaNiF -_ 60  ",), True),
    (("Vl60", "VLAN_61"), True),
    (("Port-channel60",), False),
    (("Po60",), False),
    (("LAG60",), False),
    (("Eth-Trunk60",), False),
    (("",), False),
    ((None,), False),
    (("Bridge60",), False),
    (("Port-channel VLAN60",), False),
    (("VLAN60 trunk",), False),
    (("VLAN60.10",), False),
    (("VLAN_60", "Po60"), False),
    (("Eth-Trunk60", "Vl60"), False),
    (("Vlanif60", None), False),
])
def test_ifstack_vlan_aliases_keep_exact_ap_attachment_without_changing_server_fdb(monkeypatch, higher_names, expected):
    aps, _, devices, api = setup_inventory()
    switch = devices["10.0.0.11"]
    switch["ifstack"] = {}
    for index, name in enumerate(higher_names, 60):
        switch["ifstack"][index] = [1]
        if name is not None:
            switch["ifname"][index] = name
    assert gte.ap_port_has_aggregate_relationship(switch, 1) is (not expected)
    mac = aps[0]["mac"]
    api.fdb = {switch["ip"]: [{"port_id": 1, "mac_address": mac,
                              "vlan_id": 142, "updated_at": time.time()}]}
    api.get_vlan_inventory = lambda: [{"device_id": 10, "vlan_id": 142, "vlan_vlan": 60}]
    shared, _ = gte.collect_librenms_fdb_inventory(devices, [], api, True)
    assert shared[mac][0]["ap_aggregate_member"] is (not expected)
    assert shared[mac][0]["aggregate_member"] is True
    assert bool(ap.ap_candidates(aps, shared).get(mac)) is expected

    replies = {
        f"{gte.DOT1Q_TP_FDB_PORT_OID}.60.2.0.0.0.0.1": "9",
        f"{gte.DOT1D_BASE_PORT_IFINDEX_OID}.9": "1",
    }
    calls = []

    def run(command, **kwargs):
        assert command[0] == "snmpget"
        calls.append(command[-1])
        return SimpleNamespace(stdout=replies[command[-1]])

    monkeypatch.setattr(gte.subprocess, "run", run)
    artifact = ap.build_ap_artifact(aps, validate(aps, shared, devices, api))
    assert [(row["switch_port"], row["vlan"]) for row in artifact["attachments"]] == (
        [("Gi1/0/1", 60)] if expected else []
    )
    assert calls == (list(replies) if expected else [])
    stats = gte.collection_stats_snapshot()
    assert stats["ap_snmp_gets"] == len(calls)
    assert stats["ap_snmp_walks"] == stats["server_snmp_gets"] == 0
    servers, _ = gte.build_librenms_fdb_candidates(devices, [], api, True, inventory=(shared, {}))
    assert [(row["port_name"], row["vlan"]) for row in servers[mac]] == [("Gi1/0/1", 142)]
    assert api.calls == [(switch["ip"], "fdb")]


def test_exact_move_into_vlan_layer_port_is_still_allowed(monkeypatch):
    aps, inventory, devices, api = setup_inventory(ports=[(1, 1, "Gi0/1", 1), (2, 2, "Gi0/2", 1)])
    switch = devices["10.0.0.11"]
    switch["ifname"][60] = "Vlanif60"
    switch["ifstack"] = {60: [2]}
    monkeypatch.setattr(gte, "lookup_fdb_ifindex", lambda *args: 2)
    artifact = ap.build_ap_artifact(aps, validate(aps, inventory, devices, api))
    assert [row["switch_port"] for row in artifact["attachments"]] == ["Gi0/2"]


def test_vlan_layer_does_not_override_real_lag_membership(monkeypatch):
    aps, inventory, devices, api = setup_inventory()
    switch = devices["10.0.0.11"]
    switch["ifname"].update({60: "Vlan60", 100: "Port-channel1"})
    switch["ifstack"] = {60: [1], 100: [1]}
    assert gte.ap_port_has_aggregate_relationship(switch, 1)
    calls = []
    monkeypatch.setattr(gte, "lookup_fdb_ifindex", lambda *args, **kwargs: calls.append(args) or 1)
    artifact = ap.build_ap_artifact(aps, validate(aps, inventory, devices, api))
    assert artifact["attachments"] == []
    assert len(calls) == 1
