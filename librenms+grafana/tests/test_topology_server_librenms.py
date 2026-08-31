"""LibreNMS candidate-index behavior for physical server attachments."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from librenms_client import LibreNMSUnavailable


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "librenms"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location(
    "generate_topology_server_librenms", ROOT / "generate-topology-edges.py"
)
gte = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(gte)


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def device(ip, name, ports, arp=None, device_id=None):
    result = gte._empty_device(ip)
    result.update({
        "device_id": device_id or ip,
        "sysname": name,
        "ifname": {ifindex: port_name for _port_id, ifindex, port_name, _status in ports},
        "ifoper": {ifindex: status for _port_id, ifindex, _port_name, status in ports},
        "port_by_id": {
            str(port_id): {
                "port_id": str(port_id), "ifIndex": ifindex,
                "ifName": port_name, "ifOperStatus": status,
            }
            for port_id, ifindex, port_name, status in ports
        },
        "arp": arp or {},
        "librenms_metadata": {"device_id": device_id or ip, "ip": ip},
    })
    return result


class ServerClient:
    def __init__(self, devices, arp=None, fdb=None):
        self.devices = devices
        self.arp = arp or {}
        self.fdb = fdb or {}
        self.failures = {}
        self.calls = []
        self.request_count = 0

    def resolve_device(self, identifier):
        ip = str(identifier.get("ip") if isinstance(identifier, dict) else identifier)
        return self.devices[ip]["librenms_metadata"]

    def _read(self, metadata, component, payloads):
        ip = str(metadata.get("ip"))
        self.calls.append((ip, component))
        self.request_count += 1
        failure = self.failures.get((ip, component))
        if failure:
            raise failure
        return [dict(row) for row in payloads.get(ip, [])]

    def get_device_arp(self, metadata):
        return self._read(metadata, "arp", self.arp)

    def get_device_fdb(self, metadata):
        return self._read(metadata, "fdb", self.fdb)


@pytest.fixture
def two_switches(monkeypatch):
    monkeypatch.setenv("CORE_SWITCH_PING", "core:10.0.0.1")
    monkeypatch.setenv("FIREWALL_PING", "")
    devices = {
        "10.0.0.1": device("10.0.0.1", "core", [
            (1, 1, "Vlan42", 1), (2, 2, "Te1/0/1", 1),
        ]),
        "10.0.0.11": device("10.0.0.11", "access-1", [
            (101, 10110, "Gi1/0/10", 1), (102, 10112, "Gi1/0/12", 1),
            (199, 10149, "Gi1/0/49", 1),
            (198, 5001, "Po1", 1), (197, 10111, "Gi1/0/11", 2),
        ]),
    }
    edges = [{
        "from_ip": "10.0.0.1", "from_ifindex": 2,
        "to_ip": "10.0.0.11", "to_ifindex": 10149,
    }]
    return devices, edges


def test_server_source_defaults_to_hybrid_and_invalid_is_safe(monkeypatch, capsys):
    monkeypatch.delenv("TOPOLOGY_SERVER_ATTACHMENT_SOURCE", raising=False)
    assert gte.topology_server_attachment_source() == "hybrid"
    monkeypatch.setenv("TOPOLOGY_SERVER_ATTACHMENT_SOURCE", "future")
    assert gte.topology_server_attachment_source() == "hybrid"
    assert "future" in capsys.readouterr().err


def test_server_freshness_defaults_and_unknown_metadata(monkeypatch):
    monkeypatch.delenv("TOPOLOGY_LIBRENMS_ARP_MAX_AGE_SECONDS", raising=False)
    monkeypatch.delenv("TOPOLOGY_LIBRENMS_FDB_MAX_AGE_SECONDS", raising=False)
    assert gte.topology_librenms_arp_max_age() == 900
    assert gte.topology_librenms_fdb_max_age() == 900
    assert gte.librenms_freshness(None, 900) == "unknown"


def test_librenms_arp_normalizes_only_configured_servers_without_snmp(two_switches, monkeypatch):
    devices, _edges = two_switches
    rows = fixture("server-arp.json")["arp"]
    client = ServerClient(devices, arp={"10.0.0.1": rows})
    monkeypatch.setattr(
        gte, "poll_snmp_arp",
        lambda *_args: (_ for _ in ()).throw(AssertionError("API ARP is usable")),
    )

    gte.collect_server_arp(
        devices, {"10.0.0.1"}, {"192.168.42.201": "server-1"},
        "secret", "hybrid", client, True,
    )

    assert devices["10.0.0.1"]["arp"] == {
        "192.168.42.201": {"mac": "fc:9d:05:1a:b5:41", "vlan": 42}
    }
    assert client.calls == [("10.0.0.1", "arp")]


def test_hybrid_arp_failure_walks_only_needed_l3_device(two_switches, monkeypatch):
    devices, _edges = two_switches
    client = ServerClient(devices)
    client.failures[("10.0.0.1", "arp")] = LibreNMSUnavailable("token=hidden")
    calls = []
    monkeypatch.setattr(
        gte, "poll_snmp_arp",
        lambda ip, *_args: calls.append(ip) or {
            "192.168.42.201": {"mac": "fc:9d:05:1a:b5:41", "vlan": 42}
        },
    )

    gte.collect_server_arp(
        devices, {"10.0.0.1"}, {"192.168.42.201": "server-1"},
        "private", "hybrid", client, True,
    )

    assert calls == ["10.0.0.1"]


def test_missing_librenms_arp_falls_back_to_one_l3_walk(two_switches, monkeypatch):
    devices, _edges = two_switches
    client = ServerClient(devices, arp={"10.0.0.1": []})
    calls = []
    monkeypatch.setattr(
        gte, "poll_snmp_arp",
        lambda ip, *_args: calls.append(ip) or {
            "192.168.42.201": {"mac": "fc:9d:05:1a:b5:41", "vlan": 42}
        },
    )
    gte.collect_server_arp(
        devices, {"10.0.0.1"}, {"192.168.42.201": "server-1"},
        "community", "hybrid", client, True,
    )
    assert calls == ["10.0.0.1"]


def test_librenms_only_arp_failure_never_uses_snmp(two_switches, monkeypatch, capsys):
    devices, _edges = two_switches
    client = ServerClient(devices)
    client.failures[("10.0.0.1", "arp")] = LibreNMSUnavailable("token=hidden")
    monkeypatch.setattr(
        gte, "poll_snmp_arp",
        lambda *_args: (_ for _ in ()).throw(AssertionError("no direct fallback")),
    )
    gte.collect_server_arp(
        devices, {"10.0.0.1"}, {"192.168.42.201": "server-1"},
        "private", "librenms", client, True,
    )
    log = capsys.readouterr().err
    assert "LibreNMSUnavailable" in log
    assert "hidden" not in log and "private" not in log


def test_stale_arp_falls_back_but_unknown_timestamp_is_accepted(two_switches, monkeypatch):
    devices, _edges = two_switches
    stale = dict(fixture("server-arp.json")["arp"][0], updated_at="2000-01-01 00:00:00")
    client = ServerClient(devices, arp={"10.0.0.1": [stale]})
    calls = []
    monkeypatch.setattr(
        gte, "poll_snmp_arp",
        lambda ip, *_args: calls.append(ip) or {
            "192.168.42.201": {"mac": "fc:9d:05:1a:b5:41", "vlan": 42}
        },
    )
    gte.collect_server_arp(
        devices, {"10.0.0.1"}, {"192.168.42.201": "server-1"},
        "community", "hybrid", client, True,
    )
    assert calls == ["10.0.0.1"]

    calls.clear()
    client.arp["10.0.0.1"] = [fixture("server-arp.json")["arp"][0]]
    gte.collect_server_arp(
        devices, {"10.0.0.1"}, {"192.168.42.201": "server-1"},
        "community", "hybrid", client, True,
    )
    assert calls == []


def test_fdb_index_maps_port_id_and_filters_uplink_lag_down_stale(two_switches):
    devices, edges = two_switches
    rows = fixture("server-fdb.json")["ports_fdb"]
    rows.extend([
        {"port_id": "199", "mac_address": "111111111111", "vlan_id": 42},
        {"port_id": "198", "mac_address": "222222222222", "vlan_id": 42},
        {"port_id": "197", "mac_address": "333333333333", "vlan_id": 42},
    ])
    client = ServerClient(devices, fdb={"10.0.0.1": [], "10.0.0.11": rows})

    index, status = gte.build_librenms_fdb_candidates(devices, edges, client, True)

    assert index["fc:9d:05:1a:b5:41"][0]["ifindex"] == 10110
    assert index["00:11:22:aa:bb:cc"][0]["port_name"] == "Gi1/0/12"
    assert "00:aa:bb:cc:dd:ee" not in index
    assert "11:11:11:11:11:11" not in index
    assert "22:22:22:22:22:22" not in index
    assert "33:33:33:33:33:33" not in index
    assert status == {"usable_switches": 2, "failed_switches": 0}
    assert client.calls.count(("10.0.0.11", "fdb")) == 1


def test_sw45_sw46_fixtures_cover_access_move_and_filtered_rows(monkeypatch):
    monkeypatch.setenv("CORE_SWITCH_PING", "192.168.10.254")
    monkeypatch.setenv("FIREWALL_PING", "")
    devices = {
        "192.168.10.254": device(
            "192.168.10.254", "core", [(1, 1, "Vlan42", 1)]
        ),
        "192.168.10.45": device("192.168.10.45", "SW45", [
            (101, 10110, "Gi1/0/10", 1), (104, 10111, "Gi1/0/11", 1),
            (199, 10149, "Gi1/0/49", 1), (198, 5001, "Po1", 1),
            (197, 10112, "Gi1/0/12", 2),
        ]),
        "192.168.10.46": device("192.168.10.46", "SW46", [
            (201, 1020, "Gi1/0/20", 1), (202, 1021, "Gi1/0/21", 1),
            (299, 1049, "Gi1/0/49", 1),
        ]),
    }
    edges = [
        {"from_ip": "192.168.10.254", "from_ifindex": 10,
         "to_ip": "192.168.10.45", "to_ifindex": 10149},
        {"from_ip": "192.168.10.254", "from_ifindex": 11,
         "to_ip": "192.168.10.46", "to_ifindex": 1049},
    ]
    client = ServerClient(devices, fdb={
        "192.168.10.254": [],
        "192.168.10.45": fixture("server-fdb-sw45.json")["ports_fdb"],
        "192.168.10.46": fixture("server-fdb-sw46.json")["ports_fdb"],
    })
    index, _status = gte.build_librenms_fdb_candidates(devices, edges, client, True)
    assert index["fc:9d:05:1a:b5:41"][0]["switch_ip"] == "192.168.10.45"
    assert index["fc:9d:05:1a:b5:41"][0]["port_name"] == "Gi1/0/10"
    assert index["00:11:22:aa:bb:cc"][0]["switch_ip"] == "192.168.10.46"
    assert index["00:11:22:aa:bb:cc"][0]["port_name"] == "Gi1/0/20"
    # The old SW45 row is explicitly stale; the unknown-time SW46 row remains
    # a candidate that hybrid mode must still validate exactly.
    assert [item["switch_ip"] for item in index["00:aa:bb:cc:dd:ee"]] == [
        "192.168.10.46"
    ]
    for filtered_mac in (
        "10:10:10:10:10:10", "20:20:20:20:20:20", "30:30:30:30:30:30"
    ):
        assert filtered_mac not in index


def test_vlan_conflict_skips_candidate_and_uses_full_fallback(two_switches, monkeypatch):
    devices, edges = two_switches
    server_ip = "192.168.42.201"
    mac = "fc:9d:05:1a:b5:41"
    devices["10.0.0.1"]["arp"][server_ip] = {"mac": mac, "vlan": 42}
    monkeypatch.setattr(
        gte, "lookup_fdb_ifindex",
        lambda *_args: (_ for _ in ()).throw(AssertionError("VLAN conflict is filtered")),
    )
    calls = []
    monkeypatch.setattr(
        gte, "discover_server_edges_direct",
        lambda _devices, _edges, selected, *_args: calls.append(selected) or [],
    )
    stats = {}
    assert gte.discover_server_edges(
        devices, edges, {server_ip: "server-1"}, "community", source="hybrid",
        fdb_candidates={mac: [{
            "switch_ip": "10.0.0.11", "ifindex": 10110,
            "port_name": "Gi1/0/10", "mac": mac, "vlan": 99, "depth": 1,
        }]}, server_stats=stats,
    ) == []
    assert calls == [{server_ip: "server-1"}]
    assert stats["full_fallbacks"] == 1


def test_cached_owner_is_validated_before_api_candidate(two_switches, monkeypatch):
    devices, edges = two_switches
    server_ip = "192.168.42.201"
    devices["10.0.0.1"]["arp"][server_ip] = {
        "mac": "fc:9d:05:1a:b5:41", "vlan": 42,
    }
    cached = [{
        "from_ip": "10.0.0.11", "from_ifindex": 10110,
        "from_port": "Gi1/0/10", "to_ip": server_ip,
        "source": "fdb", "server_mac": "fc:9d:05:1a:b5:41", "server_vlan": 42,
    }]
    calls = []
    monkeypatch.setattr(
        gte, "lookup_fdb_ifindex",
        lambda ip, *_args, **_kwargs: calls.append(ip) or 10110,
    )
    stats = {}

    found = gte.discover_server_edges(
        devices, edges, {server_ip: "server-1"}, "community", cached,
        source="hybrid", fdb_candidates={
            "fc:9d:05:1a:b5:41": [{
                "switch_ip": "10.0.0.1", "ifindex": 2, "port_name": "Te1/0/1",
                "mac": "fc:9d:05:1a:b5:41", "vlan": 42, "depth": 0,
            }]
        }, server_stats=stats,
    )

    assert found[0]["from_ip"] == "10.0.0.11"
    assert calls == ["10.0.0.11"]
    assert stats["full_fallbacks"] == 0


def test_api_candidate_is_exactly_validated_and_snmp_mismatch_wins(two_switches, monkeypatch):
    devices, edges = two_switches
    devices["10.0.0.11"]["ifname"][10112] = "Gi1/0/12"
    devices["10.0.0.11"]["ifoper"][10112] = 1
    server_ip = "192.168.42.201"
    devices["10.0.0.1"]["arp"][server_ip] = {
        "mac": "fc:9d:05:1a:b5:41", "vlan": 42,
    }
    monkeypatch.setattr(gte, "lookup_fdb_ifindex", lambda *_args, **_kwargs: 10112)
    found = gte.discover_server_edges(
        devices, edges, {server_ip: "server-1"}, "community", source="hybrid",
        fdb_candidates={"fc:9d:05:1a:b5:41": [{
            "switch_ip": "10.0.0.11", "ifindex": 10110,
            "port_name": "Gi1/0/10", "mac": "fc:9d:05:1a:b5:41",
            "vlan": 42, "depth": 1,
        }]},
    )
    assert found[0]["from_ifindex"] == 10112
    assert found[0]["from_port"] == "Gi1/0/12"


def test_server_move_uses_new_candidate_after_cached_owner_fails(two_switches, monkeypatch):
    devices, edges = two_switches
    server_ip = "192.168.42.201"
    devices["10.0.0.1"]["ifname"][3] = "Gi1/0/3"
    devices["10.0.0.1"]["ifoper"][3] = 1
    devices["10.0.0.1"]["arp"][server_ip] = {
        "mac": "fc:9d:05:1a:b5:41", "vlan": 42,
    }
    cached = [{
        "from_ip": "10.0.0.11", "from_port": "Gi1/0/10",
        "from_ifindex": 10110, "to_ip": server_ip, "source": "fdb",
        "server_mac": "fc:9d:05:1a:b5:41", "server_vlan": 42,
    }]
    calls = []
    monkeypatch.setattr(
        gte, "lookup_fdb_ifindex",
        lambda ip, *_args, **_kwargs: calls.append(ip) or (
            3 if ip == "10.0.0.1" else None
        ),
    )
    found = gte.discover_server_edges(
        devices, edges, {server_ip: "server-1"}, "community", cached,
        source="hybrid", fdb_candidates={"fc:9d:05:1a:b5:41": [{
            "switch_ip": "10.0.0.1", "ifindex": 3, "port_name": "Gi1/0/3",
            "mac": "fc:9d:05:1a:b5:41", "vlan": 42, "depth": 0,
        }]},
    )
    assert found[0]["from_ip"] == "10.0.0.1"
    assert found[0]["from_port"] == "Gi1/0/3"
    assert calls == ["10.0.0.11", "10.0.0.1"]


def test_multiple_api_candidates_keep_only_exact_current_owner(two_switches, monkeypatch):
    devices, edges = two_switches
    devices["10.0.0.1"]["ifname"][3] = "Gi1/0/3"
    devices["10.0.0.1"]["ifoper"][3] = 1
    server_ip = "192.168.42.201"
    mac = "fc:9d:05:1a:b5:41"
    devices["10.0.0.1"]["arp"][server_ip] = {"mac": mac, "vlan": 42}
    monkeypatch.setattr(
        gte, "lookup_fdb_ifindex",
        lambda ip, *_args, **_kwargs: 10110 if ip == "10.0.0.11" else None,
    )
    candidates = [
        {"switch_ip": "10.0.0.1", "ifindex": 3, "port_name": "Gi1/0/3",
         "mac": mac, "vlan": 42, "depth": 0},
        {"switch_ip": "10.0.0.11", "ifindex": 10110, "port_name": "Gi1/0/10",
         "mac": mac, "vlan": 42, "depth": 1},
    ]
    found = gte.discover_server_edges(
        devices, edges, {server_ip: "server-1"}, "community", source="hybrid",
        fdb_candidates={mac: candidates},
    )
    assert [edge["from_ip"] for edge in found] == ["10.0.0.11"]


def test_cached_mac_avoids_direct_arp_and_still_verifies_owner(two_switches, monkeypatch):
    devices, edges = two_switches
    server_ip = "192.168.42.201"
    mac = "fc:9d:05:1a:b5:41"
    cached = [{
        "from_ip": "10.0.0.11", "from_port": "Gi1/0/10",
        "from_ifindex": 10110, "to_ip": server_ip, "source": "fdb",
        "server_mac": mac, "server_vlan": 42,
    }]
    client = ServerClient(devices, arp={"10.0.0.1": []})
    monkeypatch.setattr(
        gte, "poll_snmp_arp",
        lambda *_args: (_ for _ in ()).throw(AssertionError("cached MAC is sufficient")),
    )
    gte.collect_server_arp(
        devices, {"10.0.0.1"}, {server_ip: "server-1"}, "community",
        "hybrid", client, True, cached,
    )
    monkeypatch.setattr(gte, "lookup_fdb_ifindex", lambda *_args, **_kwargs: 10110)
    found = gte.discover_server_edges(
        devices, edges, {server_ip: "server-1"}, "community", cached,
        source="hybrid", fdb_candidates={},
    )
    assert found[0]["server_mac"] == mac


def test_current_arp_mac_replaces_cached_mac(two_switches, monkeypatch):
    devices, edges = two_switches
    server_ip = "192.168.42.201"
    old_mac = "00:00:00:00:00:01"
    new_mac = "fc:9d:05:1a:b5:41"
    devices["10.0.0.1"]["arp"][server_ip] = {"mac": new_mac, "vlan": 42}
    cached = [{
        "from_ip": "10.0.0.11", "from_port": "Gi1/0/10",
        "from_ifindex": 10110, "to_ip": server_ip, "source": "fdb",
        "server_mac": old_mac, "server_vlan": 42,
    }]
    seen_macs = []
    monkeypatch.setattr(
        gte, "lookup_fdb_ifindex",
        lambda _ip, _community, _vlan, mac, _ifnames: (
            seen_macs.append(mac) or 10110
        ),
    )
    found = gte.discover_server_edges(
        devices, edges, {server_ip: "server-1"}, "community", cached,
        source="hybrid", fdb_candidates={},
    )
    assert seen_macs == [new_mac]
    assert found[0]["server_mac"] == new_mac


def test_one_fdb_api_failure_is_counted_without_aborting_other_switch(two_switches):
    devices, edges = two_switches
    client = ServerClient(devices, fdb={
        "10.0.0.11": fixture("server-fdb.json")["ports_fdb"],
    })
    client.failures[("10.0.0.1", "fdb")] = LibreNMSUnavailable("token=hidden")
    index, status = gte.build_librenms_fdb_candidates(devices, edges, client, True)
    assert "fc:9d:05:1a:b5:41" in index
    assert status == {"usable_switches": 1, "failed_switches": 1}


def test_failed_candidates_trigger_one_server_full_fallback(
    two_switches, monkeypatch, capsys
):
    devices, edges = two_switches
    server_ip = "192.168.42.201"
    devices["10.0.0.1"]["arp"][server_ip] = {
        "mac": "fc:9d:05:1a:b5:41", "vlan": 42,
    }
    monkeypatch.setattr(gte, "lookup_fdb_ifindex", lambda *_args, **_kwargs: None)
    fallback = [{"from_ip": "10.0.0.11", "to_ip": server_ip, "source": "fdb"}]
    calls = []
    monkeypatch.setattr(
        gte, "discover_server_edges_direct",
        lambda _devices, _edges, selected, *_args: calls.append(selected) or fallback,
    )
    stats = {}
    found = gte.discover_server_edges(
        devices, edges, {server_ip: "server-1"}, "community", source="hybrid",
        fdb_candidates={"fc:9d:05:1a:b5:41": [{
            "switch_ip": "10.0.0.11", "ifindex": 10110,
            "port_name": "Gi1/0/10", "mac": "fc:9d:05:1a:b5:41",
            "vlan": 42, "depth": 1,
        }]}, server_stats=stats,
    )
    assert found == fallback
    assert calls == [{server_ip: "server-1"}]
    assert stats["full_fallbacks"] == 1
    assert "candidate unavailable; using full direct-SNMP fallback" in capsys.readouterr().err


def test_librenms_only_uses_unique_candidate_and_rejects_ambiguity(two_switches, monkeypatch):
    devices, edges = two_switches
    server_ip = "192.168.42.201"
    devices["10.0.0.1"]["arp"][server_ip] = {
        "mac": "fc:9d:05:1a:b5:41", "vlan": 42,
    }
    monkeypatch.setattr(
        gte, "lookup_fdb_ifindex",
        lambda *_args: (_ for _ in ()).throw(AssertionError("no SNMP in API-only mode")),
    )
    candidate = {
        "switch_ip": "10.0.0.11", "ifindex": 10110,
        "port_name": "Gi1/0/10", "mac": "fc:9d:05:1a:b5:41",
        "vlan": 42, "depth": 1,
    }
    found = gte.discover_server_edges(
        devices, edges, {server_ip: "server-1"}, "community", source="librenms",
        fdb_candidates={"fc:9d:05:1a:b5:41": [candidate]},
    )
    assert found[0]["from_ifindex"] == 10110
    old_required_fields = {
        "from_ip", "from_sysname", "from_port", "from_ifindex",
        "to_ip", "to_sysname", "to_port", "to_ifindex", "source",
        "server_mac", "server_vlan",
    }
    allowed_additive_fields = {"edge_type"}
    assert old_required_fields <= set(found[0])
    assert set(found[0]) == old_required_fields | allowed_additive_fields
    assert found[0]["source"] == "fdb"
    assert found[0]["edge_type"] == "server_attachment"
    assert "protocols" not in found[0]
    assert gte.discover_server_edges(
        devices, edges, {server_ip: "server-1"}, "community", source="librenms",
        fdb_candidates={"fc:9d:05:1a:b5:41": [candidate, dict(candidate, ifindex=10111)]},
    ) == []


def test_ten_switch_four_server_index_reduces_exact_queries(monkeypatch):
    monkeypatch.setenv("CORE_SWITCH_PING", "10.0.0.1")
    monkeypatch.setenv("FIREWALL_PING", "")
    devices = {
        f"10.0.0.{number}": device(
            f"10.0.0.{number}", f"sw-{number}",
            [(100 + number, 1000 + number, f"Gi1/0/{number}", 1)],
        )
        for number in range(1, 11)
    }
    servers = {f"192.168.42.{number}": f"server-{number}" for number in range(1, 5)}
    index = {}
    expected = {}
    for number, server_ip in enumerate(servers, start=1):
        mac = f"00:11:22:33:44:{number:02x}"
        devices["10.0.0.1"]["arp"][server_ip] = {"mac": mac, "vlan": 42}
        switch_ip = f"10.0.0.{number + 1}"
        expected[switch_ip] = 1000 + number + 1
        index[mac] = [{
            "switch_ip": switch_ip, "ifindex": expected[switch_ip],
            "port_name": f"Gi1/0/{number + 1}", "mac": mac,
            "vlan": 42, "depth": 1,
        }]
    calls = []
    monkeypatch.setattr(
        gte, "lookup_fdb_ifindex",
        lambda ip, *_args, **_kwargs: calls.append(ip) or expected[ip],
    )
    stats = {}
    found = gte.discover_server_edges(
        devices, [], servers, "community", source="hybrid",
        fdb_candidates=index, server_stats=stats,
    )
    assert len(found) == 4
    assert len(calls) == 4
    assert len(calls) < len(devices) * len(servers)
    assert stats["full_fallbacks"] == 0


def test_server_librenms_source_is_independent_from_direct_adjacency(
    monkeypatch, tmp_path, capsys
):
    base_device = device("10.0.0.1", "core", [
        (1, 1, "Vlan42", 1), (101, 10110, "Gi1/0/10", 1),
    ])
    api = ServerClient(
        {"10.0.0.1": base_device},
        arp={"10.0.0.1": fixture("server-arp.json")["arp"][:1]},
        fdb={"10.0.0.1": fixture("server-fdb.json")["ports_fdb"][:1]},
    )

    def list_devices():
        api.request_count += 1
        api.calls.append(("*", "devices"))
        return [base_device["librenms_metadata"]]

    api.list_devices = list_devices
    monkeypatch.setattr(gte, "LibreNMSClient", lambda: api)
    monkeypatch.setattr(
        gte, "collect_device_by_source",
        lambda _ip, _community, collect_arp, mode, *_args: (
            pytest.fail("adjacency ARP must be disabled") if collect_arp
            else pytest.fail("adjacency source changed") if mode != "direct-snmp"
            else base_device
        ),
    )
    monkeypatch.setattr(
        gte, "lookup_fdb_ifindex",
        lambda *_args: (_ for _ in ()).throw(AssertionError("API-only server mode")),
    )
    monkeypatch.setenv("TOPOLOGY_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("TOPOLOGY_DEVICES", "10.0.0.1")
    monkeypatch.setenv("TOPOLOGY_DATA_SOURCE", "direct-snmp")
    monkeypatch.setenv("TOPOLOGY_SERVER_ATTACHMENT_SOURCE", "librenms")
    monkeypatch.setenv("TOPOLOGY_ARP_DEVICES", "10.0.0.1")
    monkeypatch.setenv("CORE_SWITCH_PING", "")
    monkeypatch.setenv("FIREWALL_PING", "")
    monkeypatch.setenv("SERVER_PING", "server-1:192.168.42.201")
    monkeypatch.setenv("TOPOLOGY_POLL_WORKERS", "1")

    assert gte._run_collection() == 0
    log = capsys.readouterr().err
    assert "adjacency stats: api_requests=0" in log
    assert "server attachment stats: api_requests=3" in log
    assert api.calls == [
        ("*", "devices"), ("10.0.0.1", "arp"), ("10.0.0.1", "fdb")
    ]
