"""LibreNMS-first topology adapter and fallback behavior tests."""
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys

import pytest

from librenms_client import LibreNMSUnavailable


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "librenms"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location(
    "generate_topology_edges_librenms", ROOT / "generate-topology-edges.py"
)
gte = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(gte)


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FixtureClient:
    def __init__(self):
        self.devices = fixture("devices.json")["devices"]
        self.payloads = {
            ("192.168.10.254", "ports"): fixture("core-ports.json")["ports"],
            ("192.168.10.254", "links"): fixture("core-links.json")["links"],
            ("192.168.10.254", "stack"): fixture("core-port-stack.json")["mappings"],
            ("192.168.10.45", "ports"): fixture("access-ports.json")["ports"],
            ("192.168.10.45", "links"): fixture("access-links.json")["links"],
            ("192.168.10.45", "stack"): fixture("access-port-stack.json")["mappings"],
        }
        self.failures = {}
        self.calls = []
        self.request_count = 0

    @staticmethod
    def _ip(device):
        return str(device.get("ip") or device.get("hostname"))

    def _read(self, device, component):
        ip = self._ip(device)
        self.calls.append((ip, component))
        self.request_count += 1
        failure = self.failures.get((ip, component))
        if failure:
            raise failure
        return [dict(item) for item in self.payloads[(ip, component)]]

    def list_devices(self):
        self.calls.append(("*", "devices"))
        self.request_count += 1
        return [dict(item) for item in self.devices]

    def resolve_device(self, identifier):
        raw = str(identifier)
        for device in self.devices:
            if raw in {
                str(device.get("device_id")),
                str(device.get("hostname")),
                str(device.get("ip")),
                str(device.get("sysName")),
            }:
                return dict(device)
        raise LibreNMSUnavailable("device missing")

    def get_device_ports(self, device, columns=None, with_vlans=False):
        assert columns == "port_id,device_id,ifIndex,ifName,ifDescr,ifAlias,ifOperStatus"
        assert with_vlans is False
        return self._read(device, "ports")

    def get_device_links(self, device):
        return self._read(device, "links")

    def get_device_port_stack(self, device, valid_mappings=True):
        assert valid_mappings is True
        return self._read(device, "stack")


def collect_fixture_devices(client, monkeypatch, mode="hybrid"):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("complete LibreNMS fixture must not use adjacency SNMP")

    monkeypatch.setattr(gte, "poll_snmp_neighbors", forbidden)
    monkeypatch.setattr(
        gte,
        "poll_snmp_lag",
        lambda _ip, _community, _ifname, _ifoper, initial=None:
            gte.resolve_aggregate_member_maps(initial or {})["members_by_aggregate"],
    )
    return {
        ip: gte.poll_device_librenms(
            ip, "secret-community", client, collect_arp=False, mode=mode
        )
        for ip in ("192.168.10.254", "192.168.10.45")
    }


def topology_device(ip, sysname, device_id, ifname):
    device = gte._empty_device(ip)
    device.update({
        "device_id": device_id,
        "sysname": sysname,
        "ifname": dict(ifname),
        "ifoper": {ifindex: 1 for ifindex in ifname},
    })
    return device


def unified_neighbor(local_ifindex, local_port, remote_name, remote_device_id,
                     remote_port, remote_port_id=None):
    return {
        "protocol": "lldp",
        "active": 1,
        "local_ifindex": local_ifindex,
        "local_port": local_port,
        "neighbor_name": remote_name,
        "neighbor_device_id": remote_device_id,
        "neighbor_port": remote_port,
        "neighbor_port_id": remote_port_id,
    }


def test_hybrid_is_default_and_invalid_value_falls_back(monkeypatch, capsys):
    monkeypatch.delenv("TOPOLOGY_DATA_SOURCE", raising=False)
    assert gte.topology_data_source() == "hybrid"
    monkeypatch.setenv("TOPOLOGY_DATA_SOURCE", "future-mode")
    assert gte.topology_data_source() == "hybrid"
    assert "future-mode" in capsys.readouterr().err


def test_direct_snmp_mode_never_calls_librenms(monkeypatch):
    sentinel = gte._empty_device("192.168.10.254")
    sentinel["source"] = {key: "direct-snmp" for key in ("ports", "links", "lag")}
    monkeypatch.setattr(gte, "poll_device_snmp", lambda *_args, **_kwargs: sentinel)

    class ForbiddenClient:
        def __getattr__(self, _name):
            raise AssertionError("direct-snmp must not inspect LibreNMS")

    result = gte.collect_device_by_source(
        "192.168.10.254", "community", False, "direct-snmp", ForbiddenClient(), True
    )
    assert result is sentinel


def test_direct_snmp_baseline_counts_authoritative_lag_walks(monkeypatch):
    class Result:
        stdout = ""

    monkeypatch.setattr(gte.subprocess, "run", lambda *_args, **_kwargs: Result())
    monkeypatch.setenv("TOPOLOGY_SNMP_DELAY_MS", "0")
    gte.reset_collection_stats()
    gte.poll_device_snmp("192.168.10.254", "community", collect_arp=False)
    stats = gte.collection_stats_snapshot()
    assert stats["direct_snmp_gets"] == 1
    assert stats["direct_snmp_walks"] == 14
    assert stats["server_snmp_gets"] == 0
    assert stats["server_snmp_walks"] == 0


def test_librenms_fixture_reaches_final_edge_without_adjacency_snmp(monkeypatch):
    client = FixtureClient()
    gte.reset_collection_stats()
    devices = collect_fixture_devices(client, monkeypatch, mode="librenms")

    edges, placeholders = gte.build_edges(devices, gte.build_name_index(devices))

    assert placeholders == []
    assert len(edges) == 1
    edge = edges[0]
    assert edge == {
        "from_ip": "192.168.10.254",
        "from_sysname": "Core",
        "from_port": "Po11",
        "from_ifindex": 400,
        "to_ip": "192.168.10.45",
        "to_sysname": "Access-Stack",
        "to_port": "Po11",
        "to_ifindex": 500,
        "from_aggregate_port": "Po11",
        "from_member_ports": ["Te1/0/2", "Te2/0/2"],
        "to_aggregate_port": "Po11",
        "to_member_ports": ["Gi1/0/24", "Gi2/0/24"],
    }
    # Fixture IDs deliberately differ: LibreNMS port_id 1001/2001 maps to
    # IF-MIB ifIndex 400/500 and is never copied into the edge as ifIndex.
    assert edge["from_ifindex"] != 1001
    assert edge["to_ifindex"] != 2001
    assert gte.collection_stats_snapshot()["direct_snmp_walks"] == 0
    assert client.calls.count(("192.168.10.254", "ports")) == 1
    assert client.calls.count(("192.168.10.254", "links")) == 1
    assert client.calls.count(("192.168.10.254", "stack")) == 1


def test_external_device_id_does_not_fallback_to_same_named_local_switch():
    studio = topology_device(
        "192.168.10.53", "Studio-3", 23, {37: "Gi1/0/37"},
    )
    studio["neighbors"] = [unified_neighbor(
        37, "Gi1/0/37", "Studio-3", 32, "24:5A:4C:59:CF:8D",
    )]
    devices = {studio["ip"]: studio}

    edges, placeholders = gte.build_edges(
        devices, gte.build_name_index(devices),
    )

    assert edges == []
    assert placeholders == [{
        "from_ip": "192.168.10.53",
        "from_port": "Gi1/0/37",
        "neighbor_name": "Studio-3",
        "neighbor_port": "24:5A:4C:59:CF:8D",
        "neighbor_device_id": "32",
        "reason": "external-device-id",
    }]


def test_external_ap_device_id_does_not_collide_with_ob_switch_name():
    source = topology_device(
        "192.168.10.11", "Aggregation", 7, {601: "Gi6/0/1"},
    )
    ob_switch = topology_device(
        "192.168.10.49", "OB-1", 9, {49: "Gi1/0/49"},
    )
    source["neighbors"] = [unified_neighbor(
        601, "Gi6/0/1", "ob-1", 14, "78:45:58:4B:6B:A8",
    )]
    devices = {source["ip"]: source, ob_switch["ip"]: ob_switch}

    edges, placeholders = gte.build_edges(
        devices, gte.build_name_index(devices),
    )

    assert edges == []
    assert placeholders[0]["neighbor_device_id"] == "14"
    assert placeholders[0]["reason"] == "external-device-id"
    assert placeholders[0]["neighbor_port"] == "78:45:58:4B:6B:A8"


def test_configured_remote_device_id_still_builds_infrastructure_edge():
    core = topology_device(
        "192.168.10.254", "Core", 1, {6: "Te1/0/6"},
    )
    ob_switch = topology_device(
        "192.168.10.49", "OB-1", 9, {49: "Gi1/0/49"},
    )
    core["neighbors"] = [unified_neighbor(
        6, "Te1/0/6", "OB-1", "9", "Gi1/0/49",
    )]
    devices = {core["ip"]: core, ob_switch["ip"]: ob_switch}

    edges, placeholders = gte.build_edges(
        devices, gte.build_name_index(devices),
    )

    assert placeholders == []
    assert len(edges) == 1
    assert (edges[0]["from_ip"], edges[0]["to_ip"]) == (
        "192.168.10.254", "192.168.10.49",
    )


def test_missing_remote_device_id_keeps_legacy_hostname_fallback():
    core = topology_device(
        "192.168.10.254", "Core", 1, {6: "Te1/0/6"},
    )
    ob_switch = topology_device(
        "192.168.10.49", "OB-1", 9, {49: "Gi1/0/49"},
    )
    core["neighbors"] = [unified_neighbor(
        6, "Te1/0/6", "ob-1", "", "Gi1/0/49",
    )]
    devices = {core["ip"]: core, ob_switch["ip"]: ob_switch}

    edges, placeholders = gte.build_edges(
        devices, gte.build_name_index(devices),
    )

    assert placeholders == []
    assert len(edges) == 1
    assert edges[0]["to_ip"] == "192.168.10.49"


def test_self_edge_is_recorded_but_never_emitted():
    studio = topology_device(
        "192.168.10.53", "Studio-3", 23, {37: "Gi1/0/37"},
    )
    studio["neighbors"] = [unified_neighbor(
        37, "Gi1/0/37", "Studio-3", 23, "24:5A:4C:59:CF:8D",
    )]
    devices = {studio["ip"]: studio}

    edges, placeholders = gte.build_edges(
        devices, gte.build_name_index(devices),
    )
    merged = {}
    gte.merge_edge(merged, {
        "from_ip": studio["ip"], "from_ifindex": 37,
        "to_ip": studio["ip"], "to_ifindex": None,
    })

    assert edges == []
    assert merged == {}
    assert placeholders[0]["reason"] == "self-edge"


def test_normal_core_ob_cdp_and_librenms_observations_still_dedupe():
    core = topology_device(
        "192.168.10.254", "Core", 1, {6: "Te1/0/6"},
    )
    ob_switch = topology_device(
        "192.168.10.49", "OB-1", 9, {49: "Gi1/0/49"},
    )
    core["cdp_device_id"] = {(6, 1): "OB-1"}
    core["cdp_device_port"] = {(6, 1): "Gi1/0/49"}
    core["cdp_address"] = {(6, 1): "192.168.10.49"}
    ob_switch["neighbors"] = [unified_neighbor(
        49, "Gi1/0/49", "Core", 1, "Te1/0/6",
    )]
    devices = {core["ip"]: core, ob_switch["ip"]: ob_switch}

    edges, placeholders = gte.build_edges(
        devices, gte.build_name_index(devices),
    )

    assert placeholders == []
    assert len(edges) == 1
    assert edges[0]["from_ip"] == "192.168.10.254"
    assert edges[0]["from_port"] == "Te1/0/6"
    assert edges[0]["to_ip"] == "192.168.10.49"
    assert edges[0]["to_port"] == "Gi1/0/49"


@pytest.mark.parametrize(
    ("long_name", "short_name", "identity", "label"),
    [
        ("GigabitEthernet27", "gi27 (00b1e3cff5ba)", "gi:27", "Gi27"),
        ("GigabitEthernet1/0/27", "Gi1/0/27", "gi:1/0/27", "Gi1/0/27"),
        ("TenGigabitEthernet1/0/6", "Te1/0/6", "te:1/0/6", "Te1/0/6"),
        ("TwentyFiveGigE1/0/6", "Twe1/0/6", "twe:1/0/6", "Twe1/0/6"),
        ("Port-channel1", "Po1", "agg:1", "Po1"),
    ],
)
def test_topology_port_identity_normalizes_known_interface_spellings(
    long_name, short_name, identity, label,
):
    assert gte.canonical_topology_port_identity(long_name) == identity
    assert gte.canonical_topology_port_identity(short_name) == identity
    assert gte.canonical_topology_port_label(long_name) == label
    assert gte.canonical_topology_port_label(short_name) == label


def test_pure_mac_port_id_is_not_guessed_as_an_interface():
    mac = "00 B1 E3 CF F5 BA"

    assert gte.canonical_topology_port_identity(mac) == ""
    assert gte.canonical_topology_port_label(mac) == mac


def test_canonical_slash_port_spellings_merge_as_one_physical_edge():
    edges = [
        {
            "from_ip": "192.168.10.254",
            "from_port": "TenGigabitEthernet1/0/7",
            "to_ip": "192.168.10.31",
            "to_port": "GigabitEthernet1/0/27",
        },
        {
            "from_ip": "192.168.10.31",
            "from_port": "Gi1/0/27",
            "to_ip": "192.168.10.254",
            "to_port": "Te1/0/7",
        },
    ]

    deduped = gte.dedupe_canonical_physical_edges(edges)

    assert len(deduped) == 1
    assert deduped[0]["from_port"] == "Te1/0/7"
    assert deduped[0]["to_port"] == "Gi1/0/27"


def test_pure_mac_endpoint_observations_are_not_guessed_or_deduped():
    edges = [
        {
            "from_ip": "192.168.10.254",
            "from_port": "Te1/0/7",
            "to_ip": "192.168.10.31",
            "to_port": "00 B1 E3 CF F5 BA",
        },
        {
            "from_ip": "192.168.10.31",
            "from_port": "00 B1 E3 CF F5 BA",
            "to_ip": "192.168.10.254",
            "to_port": "Te1/0/7",
        },
    ]

    deduped = gte.dedupe_canonical_physical_edges(edges)

    assert len(deduped) == 2
    assert all(
        "00 B1 E3 CF F5 BA" in (edge["from_port"], edge["to_port"])
        for edge in deduped
    )


def test_cdp_and_lldp_mac_annotated_reciprocal_port_merge_cleanly():
    core = topology_device(
        "192.168.10.254", "Core", 1, {7: "Te1/0/7"},
    )
    remote = topology_device(
        "192.168.10.31", "VCR", 31, {27: "Gi27"},
    )
    core["cdp_device_id"] = {(7, 1): "VCR"}
    core["cdp_device_port"] = {(7, 1): "GigabitEthernet27"}
    core["cdp_address"] = {(7, 1): remote["ip"]}
    core["neighbors"] = [unified_neighbor(
        7, "Te1/0/7", "VCR", 31, "gi27 (00b1e3cff5ba)",
    )]
    devices = {core["ip"]: core, remote["ip"]: remote}

    edges, placeholders = gte.build_edges(
        devices, gte.build_name_index(devices),
    )

    assert placeholders == []
    assert len(edges) == 1
    assert edges[0]["from_port"] == "Te1/0/7"
    assert edges[0]["to_port"] == "Gi27"
    assert "00b1e3cff5ba" not in json.dumps(edges)


def test_distinct_reciprocal_physical_pairs_remain_two_links():
    core = topology_device(
        "192.168.10.254", "Core", 1,
        {101: "Te1/0/1", 201: "Te2/0/1"},
    )
    stack = topology_device(
        "192.168.10.11", "Global-new-stack", 11,
        {102: "Te1/0/2", 202: "Te2/0/2"},
    )
    core["neighbors"] = [
        unified_neighbor(101, "Te1/0/1", stack["sysname"], 11, "Te1/0/2"),
        unified_neighbor(201, "Te2/0/1", stack["sysname"], 11, "Te2/0/2"),
    ]
    stack["neighbors"] = [
        unified_neighbor(102, "Te1/0/2", core["sysname"], 1, "Te1/0/1"),
        unified_neighbor(202, "Te2/0/2", core["sysname"], 1, "Te2/0/1"),
    ]
    devices = {core["ip"]: core, stack["ip"]: stack}

    edges, placeholders = gte.build_edges(
        devices, gte.build_name_index(devices),
    )

    assert placeholders == []
    assert len(edges) == 2
    assert {
        (edge["from_port"], edge["to_port"])
        for edge in edges
    } == {
        ("Te1/0/1", "Te1/0/2"),
        ("Te2/0/1", "Te2/0/2"),
    }


def test_aggregation_to_studio_link_remains_normal():
    aggregation = topology_device(
        "192.168.10.11", "Aggregation", 7, {451: "Gi4/0/51"},
    )
    studio = topology_device(
        "192.168.10.53", "Studio-3", 23, {49: "Gi1/0/49"},
    )
    aggregation["neighbors"] = [unified_neighbor(
        451, "Gi4/0/51", studio["sysname"], 23, "Gi1/0/49",
    )]
    studio["neighbors"] = [unified_neighbor(
        49, "Gi1/0/49", aggregation["sysname"], 7, "Gi4/0/51",
    )]
    devices = {aggregation["ip"]: aggregation, studio["ip"]: studio}

    edges, placeholders = gte.build_edges(
        devices, gte.build_name_index(devices),
    )

    assert placeholders == []
    assert len(edges) == 1
    assert edges[0]["from_ip"] == "192.168.10.11"
    assert edges[0]["from_port"] == "Gi4/0/51"
    assert edges[0]["to_ip"] == "192.168.10.53"
    assert edges[0]["to_port"] == "Gi1/0/49"


def test_external_identity_collision_is_summarized_without_unresolved_warning(
    capsys,
):
    source = topology_device(
        "192.168.10.11", "Aggregation", 7, {601: "Gi6/0/1"},
    )
    ob_switch = topology_device(
        "192.168.10.49", "OB-1", 9, {49: "Gi1/0/49"},
    )
    source["neighbors"] = [unified_neighbor(
        601, "Gi6/0/1", "ob-1", 14, "78:45:58:4B:6B:A8",
    )]
    devices = {source["ip"]: source, ob_switch["ip"]: ob_switch}

    edges, placeholders = gte.build_edges(
        devices, gte.build_name_index(devices),
    )
    counts = gte.log_unmatched_neighbors(placeholders)
    log = capsys.readouterr().err

    assert edges == []
    assert counts == {
        "external-device-id": 1,
        "unmanaged-endpoint": 0,
        "unresolved": 0,
        "invalid-response": 0,
    }
    assert "total=1 external-device-id=1" in log
    assert "unresolved infrastructure neighbor" not in log
    assert "78:45:58:4B:6B:A8" not in log


def test_unmatched_summary_separates_endpoint_invalid_and_unresolved(capsys):
    observations = [
        {
            "from_ip": "192.168.10.11",
            "from_port": "Gi6/0/22",
            "neighbor_name": "U6-Lite",
            "neighbor_port": "78:45:58:4B:6B:A8",
            "neighbor_device_id": "14",
            "reason": "external-device-id",
        },
        {
            "from_ip": "192.168.10.20",
            "from_port": "Gi1/0/20",
            "neighbor_name": "24:5A:4C:59:CF:8D",
            "neighbor_port": "24 5A 4C 59 CF 8D",
        },
        {
            "from_ip": "192.168.10.31",
            "from_port": "GigabitEthernet6",
            "neighbor_name": "VCR",
            "neighbor_port": "p02",
        },
        {
            "from_ip": "192.168.9.1",
            "from_port": None,
            "neighbor_name": "No Such Object available on this agent at this OID",
            "neighbor_port": None,
        },
    ]

    grouped = gte.classify_unmatched_neighbors(observations)
    counts = gte.log_unmatched_neighbors(observations)
    log = capsys.readouterr().err

    assert {category: len(items) for category, items in grouped.items()} == {
        "external-device-id": 1,
        "unmanaged-endpoint": 1,
        "unresolved": 1,
        "invalid-response": 1,
    }
    assert counts == {category: 1 for category in grouped}
    assert (
        "unmatched neighbor summary: total=4 external-device-id=1 "
        "unmanaged-endpoint=1 unresolved=1 invalid-response=1"
    ) in log
    assert "[WARN] 1 unresolved infrastructure neighbor(s):" in log
    assert "192.168.10.31 GigabitEthernet6 -> VCR p02" in log
    assert "U6-Lite" not in log
    assert "24:5A:4C:59:CF:8D" not in log
    assert "No Such Object" not in log


def test_unmatched_logging_limits_unresolved_details(capsys):
    observations = [
        {
            "from_ip": f"192.0.2.{index + 1}",
            "from_port": f"Gi1/0/{index + 1}",
            "neighbor_name": f"UNKNOWN-{index:02d}",
            "neighbor_port": f"p{index:02d}",
        }
        for index in range(12)
    ]

    gte.log_unmatched_neighbors(observations)
    log = capsys.readouterr().err

    assert "unresolved=12" in log
    assert log.count(" -> UNKNOWN-") == gte.UNMATCHED_NEIGHBOR_LOG_LIMIT
    assert "... 2 more unresolved neighbor(s) omitted" in log
    assert "-> UNKNOWN-10 p10" not in log
    assert "-> UNKNOWN-11 p11" not in log


def test_unmatched_classification_does_not_change_edge_output(capsys):
    core = topology_device(
        "192.168.10.254", "Core", 1, {6: "Te1/0/6"},
    )
    ob_switch = topology_device(
        "192.168.10.49", "OB-1", 9, {49: "Gi1/0/49"},
    )
    source = topology_device(
        "192.168.10.11", "Aggregation", 7, {601: "Gi6/0/1"},
    )
    core["neighbors"] = [unified_neighbor(
        6, "Te1/0/6", "OB-1", 9, "Gi1/0/49",
    )]
    source["neighbors"] = [unified_neighbor(
        601, "Gi6/0/1", "ob-1", 14, "78:45:58:4B:6B:A8",
    )]
    devices = {
        core["ip"]: core,
        ob_switch["ip"]: ob_switch,
        source["ip"]: source,
    }

    edges_before, placeholders = gte.build_edges(
        devices, gte.build_name_index(devices),
    )
    placeholders_before = json.loads(json.dumps(placeholders))
    gte.log_unmatched_neighbors(placeholders)
    edges_after, placeholders_after = gte.build_edges(
        devices, gte.build_name_index(devices),
    )
    capsys.readouterr()

    assert edges_before == edges_after
    assert placeholders == placeholders_before == placeholders_after
    assert len(edges_after) == 1
    assert (edges_after[0]["from_ip"], edges_after[0]["to_ip"]) == (
        "192.168.10.254", "192.168.10.49",
    )


def test_lldp_and_cdp_fixture_observations_dedupe_to_one_edge(monkeypatch):
    client = FixtureClient()
    devices = collect_fixture_devices(client, monkeypatch)
    assert devices["192.168.10.254"]["neighbors"][0]["protocol"] == "lldp"
    assert devices["192.168.10.45"]["neighbors"][0]["protocol"] == "cdp"
    edges, _ = gte.build_edges(devices, gte.build_name_index(devices))
    assert len(edges) == 1


def test_remote_port_name_fallback_uses_existing_endpoint_resolver(monkeypatch):
    client = FixtureClient()
    for key in (("192.168.10.254", "links"), ("192.168.10.45", "links")):
        for link in client.payloads[key]:
            link.pop("remote_port_id", None)
    devices = collect_fixture_devices(client, monkeypatch)
    edges, _ = gte.build_edges(devices, gte.build_name_index(devices))
    assert [(edge["from_ifindex"], edge["to_ifindex"]) for edge in edges] == [(400, 500)]


def test_inactive_librenms_links_are_not_current_edges(monkeypatch):
    client = FixtureClient()
    for key in (("192.168.10.254", "links"), ("192.168.10.45", "links")):
        client.payloads[key][0]["active"] = 0
    devices = collect_fixture_devices(client, monkeypatch)
    edges, _ = gte.build_edges(devices, gte.build_name_index(devices))
    assert edges == []


def test_missing_active_field_uses_port_oper_status(monkeypatch):
    client = FixtureClient()
    for key in (("192.168.10.254", "links"), ("192.168.10.45", "links")):
        client.payloads[key][0].pop("active", None)
    devices = collect_fixture_devices(client, monkeypatch)
    edges, _ = gte.build_edges(devices, gte.build_name_index(devices))
    assert len(edges) == 1


def test_down_librenms_port_is_not_a_current_edge(monkeypatch):
    client = FixtureClient()
    client.payloads[("192.168.10.45", "ports")][0]["ifOperStatus"] = "down"
    devices = collect_fixture_devices(client, monkeypatch)
    edges, _ = gte.build_edges(devices, gte.build_name_index(devices))
    assert edges == []


def test_complete_two_member_port_stack_still_checks_authoritative_snmp(monkeypatch):
    client = FixtureClient()
    calls = []

    def resolve(ip, community, _ifname, _ifoper, initial=None):
        calls.append((ip, community, initial))
        return gte.resolve_aggregate_member_maps(initial or {})["members_by_aggregate"]

    monkeypatch.setattr(
        gte,
        "poll_snmp_lag",
        resolve,
    )
    device = gte.poll_device_librenms(
        "192.168.10.254", "community", client, collect_arp=False, mode="hybrid"
    )
    assert calls == [("192.168.10.254", "community", {400: [102, 202]})]
    assert device["ifstack"] == {400: [102, 202]}
    assert device["source"]["lag"] == "hybrid"


def test_incomplete_port_stack_uses_only_lag_supplement(monkeypatch):
    client = FixtureClient()
    client.payloads[("192.168.10.254", "stack")] = [
        client.payloads[("192.168.10.254", "stack")][0]
    ]
    calls = []

    def supplement(ip, community, ifname, ifoper, initial=None):
        calls.append((ip, community, initial))
        return gte.resolve_aggregate_member_maps(
            gte.merge_ifstack_claims(initial, {400: [102, 202]})
        )["members_by_aggregate"]

    monkeypatch.setattr(gte, "poll_snmp_lag", supplement)
    monkeypatch.setattr(
        gte,
        "poll_snmp_neighbors",
        lambda *_args: (_ for _ in ()).throw(AssertionError("links must stay LibreNMS")),
    )
    device = gte.poll_device_librenms(
        "192.168.10.254", "community", client, collect_arp=False, mode="hybrid"
    )
    assert calls == [("192.168.10.254", "community", {400: [102]})]
    assert device["ifstack"] == {400: [102, 202]}
    assert device["source"] == {"ports": "librenms", "links": "librenms", "lag": "hybrid"}


def test_inactive_port_stack_mapping_is_not_used_as_a_member(monkeypatch):
    client = FixtureClient()
    client.payloads[("192.168.10.254", "stack")][1]["ifStackStatus"] = "inactive"
    monkeypatch.setattr(
        gte,
        "poll_snmp_lag",
        lambda _ip, _community, _ifname, _ifoper, initial=None: initial,
    )
    device = gte.poll_device_librenms(
        "192.168.10.254", "community", client, collect_arp=False, mode="hybrid"
    )
    assert device["ifstack"] == {400: [102]}
    assert device["source"]["lag"] == "hybrid"


def test_one_device_links_failure_falls_back_only_that_device(monkeypatch):
    client = FixtureClient()
    client.failures[("192.168.10.45", "links")] = LibreNMSUnavailable(
        "token=never-log-this"
    )
    fallbacks = []

    def neighbors(ip, _community):
        fallbacks.append(ip)
        return {
            "loc_port_desc": {}, "rem_sys": {}, "rem_port_desc": {},
            "rem_port_id": {}, "cdp_device_id": {},
            "cdp_device_port": {}, "cdp_address": {},
        }

    monkeypatch.setattr(gte, "poll_snmp_neighbors", neighbors)
    monkeypatch.setattr(
        gte,
        "poll_snmp_lag",
        lambda _ip, _community, _ifname, _ifoper, initial=None: initial,
    )
    core = gte.poll_device_librenms(
        "192.168.10.254", "community", client, collect_arp=False, mode="hybrid"
    )
    access = gte.poll_device_librenms(
        "192.168.10.45", "community", client, collect_arp=False, mode="hybrid"
    )
    assert fallbacks == ["192.168.10.45"]
    assert core["source"]["links"] == "librenms"
    assert access["source"]["links"] == "direct-snmp"


def test_freshness_states_are_independent_and_missing_is_unknown():
    now = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
    assert gte.librenms_freshness(now - timedelta(seconds=600), 600, now=now) == "fresh"
    assert gte.librenms_freshness(now - timedelta(seconds=601), 600, now=now) == "stale"
    assert gte.librenms_freshness(None, 600, now=now) == "unknown"


def test_explicit_fresh_poll_and_discovery_metadata_stays_librenms(monkeypatch):
    client = FixtureClient()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    client.devices[0]["last_polled"] = now
    client.devices[0]["last_discovered"] = now
    monkeypatch.setattr(
        gte,
        "poll_device_snmp",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("fresh API data")),
    )
    monkeypatch.setattr(
        gte,
        "poll_snmp_lag",
        lambda _ip, _community, _ifname, _ifoper, initial=None: initial,
    )
    device = gte.poll_device_librenms(
        "192.168.10.254", "community", client, collect_arp=False, mode="hybrid"
    )
    assert device["freshness"] == {"poll": "fresh", "discovery": "fresh"}
    assert device["source"] == {"ports": "librenms", "links": "librenms", "lag": "hybrid"}


def test_explicit_stale_poll_data_falls_back_full_device(monkeypatch):
    client = FixtureClient()
    client.devices[0]["last_polled"] = "2000-01-01 00:00:00"
    sentinel = gte._empty_device("192.168.10.254")
    sentinel["source"] = {key: "direct-snmp" for key in ("ports", "links", "lag")}
    calls = []
    monkeypatch.setattr(
        gte,
        "poll_device_snmp",
        lambda ip, _community, collect_arp=True: calls.append((ip, collect_arp)) or sentinel,
    )
    result = gte.poll_device_librenms(
        "192.168.10.254", "community", client, collect_arp=False, mode="hybrid"
    )
    assert result is sentinel
    assert calls == [("192.168.10.254", False)]
    assert not any(component == "ports" for _ip, component in client.calls)


def test_explicit_stale_discovery_falls_back_links_and_lag_only(monkeypatch):
    client = FixtureClient()
    client.devices[0]["last_discovered"] = "2000-01-01 00:00:00"
    calls = []
    monkeypatch.setattr(
        gte, "poll_snmp_neighbors",
        lambda ip, _community: calls.append((ip, "links")) or {
            "loc_port_desc": {}, "rem_sys": {}, "rem_port_desc": {},
            "rem_port_id": {}, "cdp_device_id": {},
            "cdp_device_port": {}, "cdp_address": {},
        },
    )
    monkeypatch.setattr(
        gte, "poll_snmp_lag",
        lambda ip, _community, *_args, **_kwargs: calls.append((ip, "lag")) or {},
    )
    device = gte.poll_device_librenms(
        "192.168.10.254", "community", client, collect_arp=False, mode="hybrid"
    )
    assert calls == [("192.168.10.254", "links"), ("192.168.10.254", "lag")]
    assert device["source"] == {
        "ports": "librenms", "links": "direct-snmp", "lag": "direct-snmp"
    }
    assert not any(component in ("links", "stack") for _ip, component in client.calls)


class MissingTokenClient:
    request_count = 0

    @staticmethod
    def resolve_device(_identifier):
        raise LibreNMSUnavailable("token=secret community=secret")


def test_missing_token_hybrid_falls_back_without_logging_secret(monkeypatch, capsys):
    sentinel = gte._empty_device("192.168.10.254")
    monkeypatch.setattr(gte, "poll_device_snmp", lambda *_args, **_kwargs: sentinel)
    result = gte.poll_device_librenms(
        "192.168.10.254", "private-community", MissingTokenClient(),
        collect_arp=False, mode="hybrid",
    )
    assert result is sentinel
    log = capsys.readouterr().err
    assert "LibreNMSUnavailable" in log
    assert "secret" not in log
    assert "private-community" not in log


def test_snmp_failure_log_never_contains_community(monkeypatch, capsys):
    def timeout(command, **_kwargs):
        raise subprocess.TimeoutExpired(command, 1)

    monkeypatch.setattr(gte.subprocess, "run", timeout)
    monkeypatch.setenv("TOPOLOGY_SNMP_DELAY_MS", "0")
    assert gte.snmpwalk("192.168.10.254", "private-community", gte.IF_NAME_OID) == ""
    log = capsys.readouterr().err
    assert "TimeoutExpired" in log
    assert "private-community" not in log


def test_librenms_only_failure_never_calls_direct_snmp_and_retains_history(monkeypatch):
    monkeypatch.setattr(
        gte,
        "poll_device_snmp",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("librenms-only must not use adjacency SNMP")
        ),
    )
    device = gte.poll_device_librenms(
        "192.168.10.254", "community", MissingTokenClient(),
        collect_arp=False, mode="librenms",
    )
    assert device["ifname"] == {}
    cached = [{
        "from_ip": "192.168.10.254", "from_port": "Po11", "from_ifindex": 400,
        "to_ip": "192.168.10.45", "to_port": "Po11", "to_ifindex": 500,
        "last_seen": 100.0,
    }]
    retained = gte.retain_cached_network_edges(
        [], cached, ["192.168.10.254", "192.168.10.45"],
        now=101, retention_seconds=86400,
    )
    assert retained[0]["stale"] is True


def test_retention_drops_stale_self_edge():
    cached = [{
        "from_ip": "192.168.10.53", "from_sysname": "Studio-3",
        "from_port": "Gi1/0/37", "from_ifindex": 37,
        "to_ip": "192.168.10.53", "to_sysname": "Studio-3",
        "to_port": "24:5A:4C:59:CF:8D", "to_ifindex": None,
        "last_seen": 100.0,
    }]

    retained = gte.retain_cached_network_edges(
        [], cached, ["192.168.10.53"],
        now=200, retention_seconds=86400,
    )

    assert retained == []


def test_retention_drops_stale_external_device_identity_collision():
    source = topology_device(
        "192.168.10.11", "Aggregation", 7, {601: "Gi6/0/1"},
    )
    ob_switch = topology_device(
        "192.168.10.49", "OB-1", 9, {49: "Gi1/0/49"},
    )
    source["neighbors"] = [unified_neighbor(
        601, "Gi6/0/1", "ob-1", 14, "78:45:58:4B:6B:A8",
    )]
    devices = {source["ip"]: source, ob_switch["ip"]: ob_switch}
    live, invalid_neighbors = gte.build_edges(
        devices, gte.build_name_index(devices),
    )
    cached = [{
        "from_ip": source["ip"], "from_sysname": source["sysname"],
        "from_port": "Gi6/0/1", "from_ifindex": 601,
        "to_ip": ob_switch["ip"], "to_sysname": "OB-1",
        "to_port": "78:45:58:4B:6B:A8", "to_ifindex": None,
        "last_seen": 100.0,
    }]

    retained = gte.retain_cached_network_edges(
        live, cached, list(devices),
        now=200, retention_seconds=86400,
        invalid_neighbors=invalid_neighbors,
    )

    assert live == []
    assert retained == []


def test_retention_preserves_normal_stale_uplink_with_external_neighbors():
    cached = [{
        "from_ip": "192.168.10.254", "from_sysname": "Core",
        "from_port": "Te1/0/6", "from_ifindex": 6,
        "to_ip": "192.168.10.49", "to_sysname": "OB-1",
        "to_port": "Gi1/0/49", "to_ifindex": 49,
        "last_seen": 100.0,
    }]
    invalid_neighbors = [{
        "from_ip": "192.168.10.11",
        "from_port": "Gi6/0/1",
        "neighbor_name": "OB-1",
        "neighbor_port": "78:45:58:4B:6B:A8",
        "neighbor_device_id": "14",
        "reason": "external-device-id",
    }]

    retained = gte.retain_cached_network_edges(
        [], cached, ["192.168.10.254", "192.168.10.49"],
        now=200, retention_seconds=86400,
        invalid_neighbors=invalid_neighbors,
    )

    assert len(retained) == 1
    assert retained[0]["from_port"] == "Te1/0/6"
    assert retained[0]["to_port"] == "Gi1/0/49"
    assert retained[0]["stale"] is True


def test_full_librenms_cycle_reports_zero_adjacency_snmp_and_compatible_schema(
    monkeypatch, tmp_path, capsys
):
    client = FixtureClient()
    commands = []

    class Result:
        stdout = ""

    def run(command, **_kwargs):
        commands.append(command)
        return Result()

    monkeypatch.setattr(gte, "LibreNMSClient", lambda: client)
    monkeypatch.setattr(gte.subprocess, "run", run)
    monkeypatch.setenv("TOPOLOGY_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("TOPOLOGY_DEVICES", "192.168.10.254,192.168.10.45")
    monkeypatch.setenv("TOPOLOGY_DATA_SOURCE", "hybrid")
    monkeypatch.setenv("TOPOLOGY_SNMP_DELAY_MS", "0")
    monkeypatch.setenv("TOPOLOGY_POLL_WORKERS", "1")
    monkeypatch.setenv("CORE_SWITCH_PING", "")
    monkeypatch.setenv("FIREWALL_PING", "")
    monkeypatch.setenv("SERVER_PING", "")

    assert gte._run_collection() == 0

    log = capsys.readouterr().err
    assert "adjacency stats: api_requests=7 snmp_walks=10 snmp_gets=0" in log
    assert "source summary: librenms=0 hybrid=2 direct-snmp=0" in log
    # No server is configured, so only the five authoritative LAG walks per
    # topology device use direct SNMP; adjacency remains LibreNMS sourced.
    assert len(commands) == 10
    edges = json.loads((tmp_path / "edges.json").read_text(encoding="utf-8"))
    assert len(edges) == 1
    assert "source" not in edges[0]
    assert set(edges[0]) == {
        "from_ip", "from_sysname", "from_port", "from_ifindex",
        "to_ip", "to_sysname", "to_port", "to_ifindex",
        "from_aggregate_port", "from_member_ports",
        "to_aggregate_port", "to_member_ports", "last_seen", "stale",
    }
