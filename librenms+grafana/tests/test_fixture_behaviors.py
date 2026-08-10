"""Behavior checks driven by sanitized device and topology snapshots."""
import importlib.util
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader
    spec.loader.exec_module(module)
    return module


gte = load_module("fixture_generate_topology_edges", "generate-topology-edges.py")
gpt = load_module("fixture_generate_player_targets", "generate-player-targets.py")


def fixture(*parts):
    return json.loads((FIXTURES.joinpath(*parts)).read_text(encoding="utf-8"))


def test_cisco_3850_fixture_drives_interface_arp_fdb_lldp_and_cdp_parsers():
    data = fixture("snmp", "cisco-3850.json")
    ifnames = gte.parse_ifname(data["ifName"])

    assert ifnames == {
        40: "Vlan40", 10101: "Gi1/0/1", 10201: "Te1/1/1", 5001: "Po11",
    }
    assert gte.parse_if_oper_status(data["ifOperStatus"])[10201] == 2
    assert gpt.parse_ifalias(data["ifAlias"]) == {10101: {"team": 7, "seat": 3}}
    assert gte.parse_arp_table(data["arp"], ifnames) == {
        "198.51.100.10": {
            "mac": "00:11:22:33:44:55", "ifindex": 40, "vlan": 40,
        },
    }
    assert gpt.parse_dot1d_fdb(data["fdb"]) == {"00:11:22:33:44:55": 7}
    assert gpt.parse_dot1d_baseport(data["basePortIfIndex"]) == {7: 10101}
    assert gte.parse_lldp_loc_port_desc(data["lldpLocPortDesc"]) == {
        10101: "GigabitEthernet1/0/1",
    }
    assert gte.parse_lldp_rem_field(data["lldpRemSysName"]) == {
        (0, 10101, 1): "stage-switch-a",
    }
    assert gte.parse_cdp_field(data["cdpDevicePort"])[(10102, 1)] == (
        "TenGigabitEthernet1/0/2"
    )
    assert gte.parse_cdp_address(data["cdpAddress"]) == {
        (10102, 1): "192.0.2.46",
    }


def test_cisco_2960x_fixture_combines_ifstack_pagp_and_lacp_stack_members():
    data = fixture("snmp", "cisco-2960x.json")
    ifnames = gte.parse_ifname(data["ifName"])
    ifstack = gte.parse_if_stack_status(data["ifStack"])
    pagp = gte.parse_member_aggregate_ifindex(data["pagpGroupIfIndex"])
    lacp = gte.parse_member_aggregate_ifindex(data["lacpAttachedAggId"])
    combined = gte.merge_aggregate_member_maps(ifstack, pagp, lacp)

    assert ifnames[20102] == "Gi2/0/2"
    assert combined == {5001: [10102, 20102], 5002: [10103, 20103]}
    assert gte.incomplete_active_aggregate_ifindexes(
        ifnames, gte.parse_if_oper_status(data["ifOperStatus"]), combined,
    ) == set()


def test_c1000_and_small_business_fixture_preserves_port_and_alias_differences():
    data = fixture("snmp", "cisco-c1000-small-business.json")
    c1000 = data["c1000"]
    small_business = data["small_business"]

    assert gte.parse_ifname(c1000["ifName"])[500] == "Po11"
    assert gpt.parse_ifalias(c1000["ifAlias"])[24] == {"team": 12, "seat": 4}
    assert gte.parse_ifname(small_business["ifName"])[49] == "gi1"
    assert gpt.parse_ifalias(small_business["ifAlias"])[49] == {
        "team": 3, "seat": 2,
    }
    assert gte.normalize_port_name(c1000["lldpPortAlias"]) == "1/0/24"
    assert gte.normalize_port_name(small_business["lldpPortAlias"]) == "ethernet 1"


def test_topology_fixture_reaches_one_final_aggregate_edge():
    data = fixture("topology", "hybrid-edge.json")
    devices = {}
    for record in data["devices"]:
        device = gte._empty_device(record["ip"])
        device.update(record)
        for field in ("ifname", "ifoper"):
            device[field] = {int(key): value for key, value in record[field].items()}
        device["ifstack"] = {
            int(key): value for key, value in record["ifstack"].items()
        }
        devices[device["ip"]] = device

    edges, placeholders = gte.build_edges(devices, gte.build_name_index(devices))

    assert placeholders == []
    assert len(edges) == 1
    edge = edges[0]
    for key, expected in data["expected"].items():
        assert edge[key] == expected
