import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from librenms_client import LibreNMSUnavailable


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "librenms"
ISP_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "isp"
MODULE_PATH = ROOT / "discover-isp-targets.py"
spec = importlib.util.spec_from_file_location("discover_isp_targets", MODULE_PATH)
disco = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(disco)


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def complete_collection(items=None):
    return disco.CollectionResult(True, list(items or []))


def failed_collection(items=None):
    return disco.CollectionResult(False, list(items or []))


def _hillstone_walks():
    """两条 WAN(电信/联通,别名进 SNMP)+ 一个内网口,双默认路由。"""
    return {
        disco.OID_IF_ALIAS: {
            f"{disco.OID_IF_ALIAS}.1": "电信",
            f"{disco.OID_IF_ALIAS}.2": "联通",
            f"{disco.OID_IF_ALIAS}.3": "lan",
        },
        disco.OID_IF_NAME: {
            f"{disco.OID_IF_NAME}.1": "ethernet0/0",
            f"{disco.OID_IF_NAME}.2": "ethernet0/1",
            f"{disco.OID_IF_NAME}.3": "ethernet0/2",
        },
        disco.OID_IP_AD_ENT_IFINDEX: {
            f"{disco.OID_IP_AD_ENT_IFINDEX}.100.64.1.2": "1",
            f"{disco.OID_IP_AD_ENT_IFINDEX}.100.65.1.2": "2",
            f"{disco.OID_IP_AD_ENT_IFINDEX}.192.168.9.1": "3",
        },
        disco.OID_IP_AD_ENT_NETMASK: {
            f"{disco.OID_IP_AD_ENT_NETMASK}.100.64.1.2": "255.255.255.0",
            f"{disco.OID_IP_AD_ENT_NETMASK}.100.65.1.2": "255.255.255.0",
            f"{disco.OID_IP_AD_ENT_NETMASK}.192.168.9.1": "255.255.255.0",
        },
        disco.OID_CIDR_DEFAULT_NEXTHOP: {
            f"{disco.OID_CIDR_DEFAULT_NEXTHOP}.0.100.64.1.1": "100.64.1.1",
            f"{disco.OID_CIDR_DEFAULT_NEXTHOP}.0.100.65.1.1": "100.65.1.1",
        },
        disco.OID_CIDR_DEFAULT_IFINDEX: {
            f"{disco.OID_CIDR_DEFAULT_IFINDEX}.0.100.64.1.1": "1",
            f"{disco.OID_CIDR_DEFAULT_IFINDEX}.0.100.65.1.1": "2",
        },
    }


def test_parse_walk_strips_types_and_quotes():
    parsed = disco.parse_walk(
        '.1.3.6.1.2.1.31.1.1.1.18.1 = STRING: "电信"\n'
        ".1.3.6.1.2.1.4.20.1.2.100.64.1.2 = INTEGER: 1\n"
        ".1.3.6.1.2.1.4.24.4.1.4.0.0.0.0.0.0.0.0.0.100.64.1.1 = IpAddress: 100.64.1.1\n"
        ".1.3.6.1.2.1.31.1.1.1.18.9 = No Such Instance currently exists\n"
    )
    assert parsed[".1.3.6.1.2.1.31.1.1.1.18.1"] == "电信"
    assert parsed[".1.3.6.1.2.1.4.20.1.2.100.64.1.2"] == "1"
    assert ".1.3.6.1.2.1.31.1.1.1.18.9" not in parsed


def test_snmp_walk_distinguishes_successful_empty_failure_and_malformed(monkeypatch):
    monkeypatch.setattr(
        disco.subprocess, "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    assert disco.snmp_walk("192.0.2.1", "community", disco.OID_IF_ALIAS) == disco.WalkResult(
        True, {}
    )

    monkeypatch.setattr(
        disco.subprocess, "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="No more variables left in this MIB View",
            stderr="",
        ),
    )
    assert disco.snmp_walk("192.0.2.1", "community", disco.OID_IF_ALIAS).ok is True

    monkeypatch.setattr(
        disco.subprocess, "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout="", stderr="secret"),
    )
    failed = disco.snmp_walk("192.0.2.1", "community", disco.OID_IF_ALIAS)
    assert failed.ok is False and failed.values == {}

    monkeypatch.setattr(
        disco.subprocess, "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout="not valid snmp output", stderr=""
        ),
    )
    malformed = disco.snmp_walk("192.0.2.1", "community", disco.OID_IF_ALIAS)
    assert malformed.ok is False and malformed.error_type == "malformed_output"


def test_discovers_multi_wan_gateways_named_by_interface():
    results = disco.discover_from_walks(_hillstone_walks(), disco.wan_keywords("telecom,unicom,电信,联通"))
    assert [(item["name"], item["gateway"], item["wan_ip"]) for item in results] == [
        ("电信", "100.64.1.1", "100.64.1.2"),
        ("联通", "100.65.1.1", "100.65.1.2"),
    ]


def test_wan_keyword_digit_boundary_matches_like_bridge():
    keywords = disco.wan_keywords("eth0,eth1")
    assert disco.is_wan_label("eth1", keywords)
    assert not disco.is_wan_label("eth10", keywords)


def test_subnet_fallback_when_route_has_no_ifindex():
    walks = _hillstone_walks()
    walks.pop(disco.OID_CIDR_DEFAULT_IFINDEX)
    results = disco.discover_from_walks(walks, disco.wan_keywords("电信,联通"))
    assert {item["gateway"] for item in results} == {"100.64.1.1", "100.65.1.1"}


def test_rfc1213_fallback_single_default_route():
    walks = _hillstone_walks()
    walks.pop(disco.OID_CIDR_DEFAULT_NEXTHOP)
    walks.pop(disco.OID_CIDR_DEFAULT_IFINDEX)
    walks[disco.OID_ROUTE_DEFAULT_NEXTHOP] = {disco.OID_ROUTE_DEFAULT_NEXTHOP: "100.64.1.1"}
    walks[disco.OID_ROUTE_DEFAULT_IFINDEX] = {disco.OID_ROUTE_DEFAULT_IFINDEX: "1"}
    results = disco.discover_from_walks(walks, disco.wan_keywords("电信,联通"))
    assert [(item["name"], item["gateway"]) for item in results] == [
        ("电信", "100.64.1.1"),
        # The standby WAN has no active default route, but its current public
        # address must still stay visible in topology.
        ("联通", "100.65.1.1"),
    ]


def test_lan_default_route_and_duplicates_are_dropped():
    walks = _hillstone_walks()
    # 一条经内网口的默认路由(如管理旁路)不算 ISP;重复下一跳只留一条
    walks[disco.OID_CIDR_DEFAULT_NEXTHOP][f"{disco.OID_CIDR_DEFAULT_NEXTHOP}.0.192.168.9.254"] = "192.168.9.254"
    walks[disco.OID_CIDR_DEFAULT_IFINDEX][f"{disco.OID_CIDR_DEFAULT_IFINDEX}.0.192.168.9.254"] = "3"
    walks[disco.OID_CIDR_DEFAULT_NEXTHOP][f"{disco.OID_CIDR_DEFAULT_NEXTHOP}.1.100.64.1.1"] = "100.64.1.1"
    walks[disco.OID_CIDR_DEFAULT_IFINDEX][f"{disco.OID_CIDR_DEFAULT_IFINDEX}.1.100.64.1.1"] = "1"
    results = disco.discover_from_walks(walks, disco.wan_keywords("电信,联通"))
    assert {item["gateway"] for item in results} == {"100.64.1.1", "100.65.1.1"}


def test_manual_isp_ping_entries_take_precedence():
    results = disco.discover_from_walks(_hillstone_walks(), disco.wan_keywords("电信,联通"))
    payload = disco.build_file_sd(results, exclude={"100.64.1.1"})
    assert [entry["targets"][0] for entry in payload] == ["100.65.1.1"]
    assert payload[0]["labels"]["display_name"] == "联通"
    assert payload[0]["labels"]["metric_name"] == "联通"
    assert payload[0]["labels"]["wan_ip"] == "100.65.1.2"


def test_duplicate_carrier_lines_use_stable_address_identity():
    """同名线路使用 WAN IP，而不是会漂移的 ifIndex 排名区分。"""
    walks = {
        disco.OID_IF_ALIAS: {
            f"{disco.OID_IF_ALIAS}.4": "电信",
            f"{disco.OID_IF_ALIAS}.2": "电信",
            f"{disco.OID_IF_ALIAS}.5": "联通",
            f"{disco.OID_IF_ALIAS}.6": "联通",
        },
        disco.OID_IP_AD_ENT_IFINDEX: {
            f"{disco.OID_IP_AD_ENT_IFINDEX}.100.64.1.2": "2",
            f"{disco.OID_IP_AD_ENT_IFINDEX}.100.64.2.2": "4",
            f"{disco.OID_IP_AD_ENT_IFINDEX}.100.65.1.2": "5",
            f"{disco.OID_IP_AD_ENT_IFINDEX}.100.65.2.2": "6",
        },
        disco.OID_IP_AD_ENT_NETMASK: {},
        disco.OID_CIDR_DEFAULT_NEXTHOP: {
            f"{disco.OID_CIDR_DEFAULT_NEXTHOP}.0.100.64.1.1": "100.64.1.1",
            f"{disco.OID_CIDR_DEFAULT_NEXTHOP}.0.100.64.2.1": "100.64.2.1",
            f"{disco.OID_CIDR_DEFAULT_NEXTHOP}.0.100.65.1.1": "100.65.1.1",
            f"{disco.OID_CIDR_DEFAULT_NEXTHOP}.0.100.65.2.1": "100.65.2.1",
        },
        disco.OID_CIDR_DEFAULT_IFINDEX: {
            f"{disco.OID_CIDR_DEFAULT_IFINDEX}.0.100.64.1.1": "2",
            f"{disco.OID_CIDR_DEFAULT_IFINDEX}.0.100.64.2.1": "4",
            f"{disco.OID_CIDR_DEFAULT_IFINDEX}.0.100.65.1.1": "5",
            f"{disco.OID_CIDR_DEFAULT_IFINDEX}.0.100.65.2.1": "6",
        },
    }
    results = disco.discover_from_walks(walks, disco.wan_keywords("电信,联通"))
    assert [(item["name"], item["gateway"]) for item in results] == [
        ("电信@100.64.1.2", "100.64.1.1"),
        ("电信@100.64.2.2", "100.64.2.1"),
        ("联通@100.65.1.2", "100.65.1.1"),
        ("联通@100.65.2.2", "100.65.2.1"),
    ]


def test_foreign_carrier_names_match_by_keyword():
    """国外运营商:关键词/口名没有任何语言假设,配进 WAN 过滤即可。"""
    walks = {
        disco.OID_IF_ALIAS: {
            f"{disco.OID_IF_ALIAS}.1": "Vodafone-Line",
            f"{disco.OID_IF_ALIAS}.2": "Singtel",
        },
        disco.OID_IP_AD_ENT_IFINDEX: {
            f"{disco.OID_IP_AD_ENT_IFINDEX}.203.0.113.2": "1",
            f"{disco.OID_IP_AD_ENT_IFINDEX}.198.51.100.2": "2",
        },
        disco.OID_IP_AD_ENT_NETMASK: {},
        disco.OID_CIDR_DEFAULT_NEXTHOP: {
            f"{disco.OID_CIDR_DEFAULT_NEXTHOP}.0.203.0.113.1": "203.0.113.1",
            f"{disco.OID_CIDR_DEFAULT_NEXTHOP}.0.198.51.100.1": "198.51.100.1",
        },
        disco.OID_CIDR_DEFAULT_IFINDEX: {
            f"{disco.OID_CIDR_DEFAULT_IFINDEX}.0.203.0.113.1": "1",
            f"{disco.OID_CIDR_DEFAULT_IFINDEX}.0.198.51.100.1": "2",
        },
    }
    results = disco.discover_from_walks(walks, disco.wan_keywords("vodafone,singtel"))
    assert {item["name"] for item in results} == {"Vodafone-Line", "Singtel"}


def test_generic_interface_names_bind_console_metadata_by_public_ip():
    """Generic ports may use manual labels only through explicit public IPs."""
    walks = {
        disco.OID_IF_ALIAS: {},
        disco.OID_IF_NAME: {
            f"{disco.OID_IF_NAME}.1": "ethernet0/0",
            f"{disco.OID_IF_NAME}.3": "ethernet0/2",
            f"{disco.OID_IF_NAME}.5": "ethernet0/4",
            f"{disco.OID_IF_NAME}.7": "ethernet0/6",
            f"{disco.OID_IF_NAME}.9": "ethernet0/8",
        },
        disco.OID_IP_AD_ENT_IFINDEX: {
            f"{disco.OID_IP_AD_ENT_IFINDEX}.101.95.176.198": "1",
            f"{disco.OID_IP_AD_ENT_IFINDEX}.116.238.242.155": "3",
            f"{disco.OID_IP_AD_ENT_IFINDEX}.116.128.201.226": "5",
            f"{disco.OID_IP_AD_ENT_IFINDEX}.61.169.238.58": "7",
            f"{disco.OID_IP_AD_ENT_IFINDEX}.192.168.9.1": "9",
        },
        disco.OID_IP_AD_ENT_NETMASK: {
            f"{disco.OID_IP_AD_ENT_NETMASK}.101.95.176.198": "255.255.255.252",
            f"{disco.OID_IP_AD_ENT_NETMASK}.116.238.242.155": "255.255.255.248",
            f"{disco.OID_IP_AD_ENT_NETMASK}.116.128.201.226": "255.255.255.240",
            f"{disco.OID_IP_AD_ENT_NETMASK}.61.169.238.58": "255.255.255.248",
            f"{disco.OID_IP_AD_ENT_NETMASK}.192.168.9.1": "255.255.255.0",
        },
        disco.OID_CIDR_DEFAULT_NEXTHOP: {
            f"{disco.OID_CIDR_DEFAULT_NEXTHOP}.0.101.95.176.197": "101.95.176.197",
            f"{disco.OID_CIDR_DEFAULT_NEXTHOP}.0.116.238.242.153": "116.238.242.153",
            f"{disco.OID_CIDR_DEFAULT_NEXTHOP}.0.116.128.201.225": "116.128.201.225",
            f"{disco.OID_CIDR_DEFAULT_NEXTHOP}.0.61.169.238.57": "61.169.238.57",
            f"{disco.OID_CIDR_DEFAULT_NEXTHOP}.0.192.168.9.254": "192.168.9.254",
        },
        disco.OID_CIDR_DEFAULT_IFINDEX: {
            f"{disco.OID_CIDR_DEFAULT_IFINDEX}.0.101.95.176.197": "1",
            f"{disco.OID_CIDR_DEFAULT_IFINDEX}.0.116.238.242.153": "3",
            f"{disco.OID_CIDR_DEFAULT_IFINDEX}.0.116.128.201.225": "5",
            f"{disco.OID_CIDR_DEFAULT_IFINDEX}.0.61.169.238.57": "7",
            f"{disco.OID_CIDR_DEFAULT_IFINDEX}.0.192.168.9.254": "9",
        },
    }
    names = ["telcom-100M-长期", "telcom-1000M", "unicom-1000M", "telcom-100M"]
    configured_ips = {
        "telcom-100M-长期": "101.95.176.198",
        "telcom-1000M": "116.238.242.155",
        "unicom-1000M": "116.128.201.226",
        "telcom-100M": "61.169.238.58",
    }
    results = disco.discover_from_walks(
        walks, disco.wan_keywords("telecom,unicom,WAN"), names, configured_ips
    )
    assert [(item["name"], item["wan_ip"], item["gateway"]) for item in results] == [
        ("telcom-1000M", "116.238.242.155", "116.238.242.153"),
        ("telcom-100M", "61.169.238.58", "61.169.238.57"),
        ("telcom-100M-长期", "101.95.176.198", "101.95.176.197"),
        ("unicom-1000M", "116.128.201.226", "116.128.201.225"),
    ]
    assert {item["metric_name"] for item in results} == {
        "ethernet0/0", "ethernet0/2", "ethernet0/4", "ethernet0/6",
    }


def test_public_wan_addresses_survive_when_firewall_hides_route_table():
    """Current WAN IPs remain visible even when standard route OIDs are empty."""
    walks = _hillstone_walks()
    walks[disco.OID_CIDR_DEFAULT_NEXTHOP] = {}
    walks[disco.OID_CIDR_DEFAULT_IFINDEX] = {}
    names = ["电信-100M", "联通-1000M"]

    results = disco.discover_from_walks(
        walks,
        disco.wan_keywords("电信,联通"),
        names,
        {"电信-100M": "100.64.1.2", "联通-1000M": "100.65.1.2"},
    )

    assert [(item["name"], item["wan_ip"], item["gateway"], item["source"]) for item in results] == [
        ("电信-100M", "100.64.1.2", "100.64.1.1", "subnet_gateway"),
        ("联通-1000M", "100.65.1.2", "100.65.1.1", "subnet_gateway"),
    ]
    payload = disco.build_file_sd(results, exclude=set())
    assert payload[0]["labels"]["discovery_source"] == "subnet_gateway"


def test_librenms_inventory_fallback_maps_current_public_addresses_and_prefixes():
    addresses = [
        {"ipv4_address": "101.95.176.198", "ipv4_prefixlen": "30", "port_id": "10"},
        {"ipv4_address": "192.168.9.11", "ipv4_prefixlen": "24", "port_id": "11"},
        {"ipv4_address": "116.238.242.155", "ipv4_prefixlen": "29", "port_id": "12"},
        {"ipv4_address": "116.128.201.226", "ipv4_prefixlen": "28", "port_id": "14"},
        {"ipv4_address": "61.169.238.58", "ipv4_prefixlen": "29", "port_id": "16"},
    ]
    ports = [
        {"port_id": "10", "ifIndex": "1", "ifName": "ethernet0/0"},
        {"port_id": "11", "ifIndex": "2", "ifName": "ethernet0/1"},
        {"port_id": "12", "ifIndex": "3", "ifName": "ethernet0/2"},
        {"port_id": "14", "ifIndex": "5", "ifName": "ethernet0/4"},
        {"port_id": "16", "ifIndex": "7", "ifName": "ethernet0/6"},
    ]
    results = disco.discover_from_librenms(
        addresses,
        ports,
        ["telecom-100M-long", "telecom-1000M", "unicom-1000M", "telecom-100M"],
        {
            "telecom-100M-long": "101.95.176.198",
            "telecom-1000M": "116.238.242.155",
            "unicom-1000M": "116.128.201.226",
            "telecom-100M": "61.169.238.58",
        },
    )
    assert {
        item["name"]: (item["wan_ip"], item["gateway"]) for item in results
    } == {
        "telecom-100M-long": ("101.95.176.198", "101.95.176.197"),
        "telecom-1000M": ("116.238.242.155", "116.238.242.153"),
        "unicom-1000M": ("116.128.201.226", "116.128.201.225"),
        "telecom-100M": ("61.169.238.58", "61.169.238.57"),
    }
    assert {item["source"] for item in results} == {"librenms_subnet_gateway"}
    assert {item["metric_name"] for item in results} == {
        "ethernet0/0", "ethernet0/2", "ethernet0/4", "ethernet0/6",
    }


def test_librenms_inventory_binds_by_wan_alias_before_interface_order():
    results = disco.discover_from_librenms(
        [
            {"ipv4_address": "61.169.238.58", "ipv4_prefixlen": "29", "port_id": "1"},
            {"ipv4_address": "101.95.176.198", "ipv4_prefixlen": "30", "port_id": "2"},
        ],
        [
            {"port_id": "1", "ifIndex": "1", "ifAlias": "telecom-100M"},
            {"port_id": "2", "ifIndex": "2", "ifAlias": "telecom-100M-long"},
        ],
        ["telecom-100M-long", "telecom-100M"],
    )
    assert [(item["name"], item["wan_ip"]) for item in results] == [
        ("telecom-100M", "61.169.238.58"),
        ("telecom-100M-long", "101.95.176.198"),
    ]


def test_manual_name_never_falls_back_to_ifindex_position(capsys):
    results = disco.discover_from_librenms(
        [
            {"ipv4_address": "8.8.8.10", "ipv4_prefixlen": 29, "port_id": "1"},
            {"ipv4_address": "1.1.1.2", "ipv4_prefixlen": 30, "port_id": "2"},
        ],
        [
            {"port_id": "1", "ifIndex": 10, "ifName": "ethernet0/0"},
            {"port_id": "2", "ifIndex": 20, "ifName": "ethernet0/1"},
        ],
        ["ISP-A", "ISP-B"],
    )

    assert {item["name"] for item in results} == {"ethernet0/0", "ethernet0/1"}
    warning = capsys.readouterr().err
    assert 'manual ISP metadata "ISP-A" could not be safely matched' in warning
    assert 'manual ISP metadata "ISP-B" could not be safely matched' in warning


def test_identity_survives_ifindex_and_input_reorder():
    addresses = [
        {"ipv4_address": "8.8.8.10", "ipv4_prefixlen": 29, "port_id": "a"},
        {"ipv4_address": "1.1.1.2", "ipv4_prefixlen": 30, "port_id": "b"},
        {"ipv4_address": "9.9.9.10", "ipv4_prefixlen": 29, "port_id": "c"},
    ]
    first_ports = [
        {"port_id": "a", "ifIndex": 10, "ifAlias": "ISP-A"},
        {"port_id": "b", "ifIndex": 20, "ifAlias": "ISP-B"},
        {"port_id": "c", "ifIndex": 30, "ifAlias": "ISP-C"},
    ]
    second_ports = [
        {"port_id": "c", "ifIndex": 7, "ifAlias": "ISP-C"},
        {"port_id": "a", "ifIndex": 31, "ifAlias": "ISP-A"},
        {"port_id": "b", "ifIndex": 44, "ifAlias": "ISP-B"},
    ]

    first = disco.discover_from_librenms(addresses, first_ports, ["ISP-A", "ISP-B", "ISP-C"])
    second = disco.discover_from_librenms(
        list(reversed(addresses)), second_ports, ["ISP-A", "ISP-B", "ISP-C"]
    )

    identity = lambda rows: {item["wan_ip"]: item["name"] for item in rows}
    assert identity(first) == identity(second) == {
        "8.8.8.10": "ISP-A",
        "1.1.1.2": "ISP-B",
        "9.9.9.10": "ISP-C",
    }


def test_production_four_manual_metadata_rows_keep_five_discovered_isps():
    payload = json.loads(
        (ISP_FIXTURES / "production-ha-inventory.json").read_text(encoding="utf-8")
    )
    manual = [
        "telcom-100M-长期", "telcom-1000M", "unicom-1000M", "MLBB-telcom-300M"
    ]
    results = [
        {
            "gateway": entry["targets"][0],
            "name": entry["labels"]["display_name"],
            "wan_ip": entry["labels"]["wan_ip"],
            "source": entry["labels"]["discovery_source"],
            "_labels": [entry["labels"]["display_name"]],
        }
        for entry in reversed(payload)
    ]

    finalized = disco.finalize_discovered_results(results, manual)

    assert len(finalized) == 5
    assert {item["name"] for item in finalized} == {
        entry["labels"]["display_name"] for entry in payload
    }
    assert "MLBB-unicom-300M" in {item["name"] for item in finalized}


def test_duplicate_stable_evidence_fails_safe_without_overwrite(capsys):
    results = [
        {"gateway": "8.8.8.9", "name": "WAN", "wan_ip": "8.8.8.10", "_labels": ["WAN"]},
        {"gateway": "8.8.4.3", "name": "WAN", "wan_ip": "8.8.8.10", "_labels": ["WAN"]},
    ]

    matched = disco.bind_manual_metadata(
        results, ["ISP-A"], {"ISP-A": "8.8.8.10"}
    )

    assert matched == set()
    assert [item["name"] for item in results] == ["WAN", "WAN"]
    assert "has ambiguous identity evidence; override skipped" in capsys.readouterr().err


def test_conflicting_ip_and_label_evidence_preserves_both_native_identities(capsys):
    results = [
        {
            "gateway": "8.8.8.9", "name": "ISP-A", "metric_name": "ISP-A",
            "wan_ip": "8.8.8.10", "_labels": ["ISP-A"],
        },
        {
            "gateway": "1.1.1.1", "name": "ethernet0/4",
            "metric_name": "ethernet0/4", "wan_ip": "1.1.1.2",
            "_labels": ["ethernet0/4"],
        },
    ]

    matched = disco.bind_manual_metadata(
        results, ["ISP-A"], {"ISP-A": "1.1.1.2"}
    )

    assert matched == set()
    assert [item["name"] for item in results] == ["ISP-A", "ethernet0/4"]
    assert all(item["_metadata_conflict"] is True for item in results)
    payload = disco.build_file_sd(results, exclude=set())
    assert all(item["labels"]["metadata_conflict"] == "true" for item in payload)
    assert "has conflicting identity evidence; override skipped" in capsys.readouterr().err


def test_identity_evidence_agreement_or_single_unique_source_enriches():
    template = [
        {
            "gateway": "8.8.8.9", "name": "native-a", "metric_name": "native-a",
            "wan_ip": "8.8.8.10", "_labels": ["ISP-A", "native-a"],
        }
    ]
    for configured_ip in ("8.8.8.10", "9.9.9.9", ""):
        results = [dict(template[0], _labels=list(template[0]["_labels"]))]
        matched = disco.bind_manual_metadata(
            results, ["ISP-A"], {"ISP-A": configured_ip} if configured_ip else {}
        )
        assert matched == {"ISP-A"}
        assert results[0]["name"] == "ISP-A"
        assert results[0]["metric_name"] == "native-a"

    results = [{
        "gateway": "8.8.8.9", "name": "native-a", "metric_name": "native-a",
        "wan_ip": "8.8.8.10", "_labels": ["native-a"],
    }]
    assert disco.bind_manual_metadata(
        results, ["ISP-A"], {"ISP-A": "8.8.8.10"}
    ) == {"ISP-A"}


def test_conflict_does_not_consume_candidate_needed_by_later_metadata(capsys):
    results = [
        {"gateway": "8.8.8.9", "name": "ISP-A", "wan_ip": "8.8.8.10", "_labels": ["ISP-A"]},
        {"gateway": "1.1.1.1", "name": "ISP-B", "wan_ip": "1.1.1.2", "_labels": ["ISP-B"]},
    ]
    matched = disco.bind_manual_metadata(
        results,
        ["ISP-A", "ISP-B"],
        {"ISP-A": "1.1.1.2", "ISP-B": "1.1.1.2"},
    )
    assert matched == {"ISP-B"}
    assert [item["name"] for item in results] == ["ISP-A", "ISP-B"]
    assert "conflicting identity evidence" in capsys.readouterr().err


def test_pppoe_slash31_and_slash32_are_inventory_only_not_fake_self_pings():
    for prefix in (31, 32):
        results = disco.discover_from_librenms(
            [{"ipv4_address": "8.8.8.8", "ipv4_prefixlen": str(prefix), "port_id": "8"}],
            [{"port_id": "8", "ifIndex": "8", "ifAlias": "pppoe-telecom"}],
            ["pppoe-telecom"],
        )
        assert results[0]["gateway"] == ""
        assert results[0]["source"] == "librenms_interface_only"
        assert disco.build_file_sd(results, set()) == []


class FakeLibreNMSClient:
    base_url = "http://librenms:8000"
    token = "configured"

    def __init__(self, *, failure=None):
        self.failure = failure
        self.list_calls = 0
        self.resolved = []
        self.port_columns = None

    def list_devices(self):
        self.list_calls += 1
        if self.failure:
            raise self.failure
        return [{"device_id": 7, "hostname": "192.0.2.1"}]

    def resolve_device(self, identifier):
        self.resolved.append(identifier)
        if self.failure:
            raise self.failure
        return {"device_id": 7, "hostname": identifier, "ip": identifier, "sysName": "fw"}

    @staticmethod
    def get_device_ip_addresses(_device):
        return [{"ipv4_address": "8.8.8.10", "ipv4_prefixlen": "29", "port_id": "9"}]

    def get_device_ports(self, _device, columns=None, with_vlans=False):
        self.port_columns = columns
        assert with_vlans is False
        return [{"port_id": 9, "ifIndex": "3", "ifAlias": "telecom"}]


class PolicyClient:
    base_url = "http://librenms:8000"
    token = "configured"

    def __init__(self, inventory, failures=None):
        self.inventory = inventory
        self.failures = failures or {}
        self.calls = []
        self.request_count = 0

    def _failure(self, target, component):
        failure = self.failures.get((target, component))
        if failure:
            raise failure

    def list_devices(self):
        self.calls.append(("*", "devices"))
        self.request_count += 1
        self._failure("*", "devices")
        return [dict(value[0]) for value in self.inventory.values()]

    def resolve_device(self, identifier):
        target = str(identifier)
        self._failure(target, "device")
        if target not in self.inventory:
            raise LibreNMSUnavailable("device not found token=hidden")
        return dict(self.inventory[target][0])

    def get_device_ports(self, metadata, columns=None, with_vlans=False):
        target = str(metadata["ip"])
        self.calls.append((target, "ports"))
        self.request_count += 1
        self._failure(target, "ports")
        assert columns == "port_id,ifIndex,ifName,ifDescr,ifAlias,ifOperStatus"
        assert with_vlans is False
        return [dict(row) for row in self.inventory[target][1]]

    def get_device_ip_addresses(self, metadata):
        target = str(metadata["ip"])
        self.calls.append((target, "addresses"))
        self.request_count += 1
        self._failure(target, "addresses")
        return [dict(row) for row in self.inventory[target][2]]


def fixture_inventory(target="192.0.2.10", last_polled=None):
    metadata = dict(fixture("firewall-device.json")["devices"][0])
    metadata.update({"hostname": target, "ip": target})
    if last_polled is not None:
        metadata["last_polled"] = last_polled
    return (
        metadata,
        fixture("firewall-ports.json")["ports"],
        fixture("firewall-ip.json")["addresses"],
    )


def route_walk(gateway="8.8.8.14", ifindex="11", legacy=False, calls=None):
    def walk(_ip, _community, oid, _timeout):
        if calls is not None:
            calls.append(oid)
        suffix = ".0.fixture"
        if oid == disco.OID_CIDR_DEFAULT_NEXTHOP:
            return {} if legacy else {f"{oid}{suffix}": gateway}
        if oid == disco.OID_CIDR_DEFAULT_IFINDEX:
            return {} if legacy else {f"{oid}{suffix}": ifindex}
        if oid == disco.OID_ROUTE_DEFAULT_NEXTHOP:
            return {oid: gateway}
        if oid == disco.OID_ROUTE_DEFAULT_IFINDEX:
            return {oid: ifindex}
        raise AssertionError(f"inventory SNMP walk was not expected: {oid}")
    return walk


def test_librenms_fallback_uses_shared_client_without_changing_wan_mapping():
    client = FakeLibreNMSClient()

    results = disco.collect_from_librenms(["192.0.2.1"], ["telecom"], client=client)

    assert client.resolved == ["192.0.2.1"]
    assert client.list_calls == 1
    assert client.port_columns == "port_id,ifIndex,ifName,ifDescr,ifAlias,ifOperStatus"
    assert results == [{
        "gateway": "8.8.8.9",
        "name": "telecom",
        "metric_name": "telecom",
        "wan_ip": "8.8.8.10",
        "source": "librenms_subnet_gateway",
    }]


def test_librenms_api_failure_returns_safely_without_logging_secret(capsys):
    client = FakeLibreNMSClient(
        failure=LibreNMSUnavailable("token=do-not-log password=do-not-log"),
    )

    assert disco.collect_from_librenms(["192.0.2.1"], client=client) == []
    error = capsys.readouterr().err
    assert "LibreNMSUnavailable" in error
    assert "do-not-log" not in error


def test_hybrid_is_default_and_invalid_source_is_safe(monkeypatch, capsys):
    monkeypatch.delenv("ISP_DISCOVERY_SOURCE", raising=False)
    assert disco.isp_discovery_source() == "hybrid"
    monkeypatch.setenv("ISP_DISCOVERY_SOURCE", "future")
    assert disco.isp_discovery_source() == "hybrid"
    assert "future" in capsys.readouterr().err


def test_fixture_inventory_joins_port_id_to_real_ifindex_and_filters_down():
    _metadata, ports, addresses = fixture_inventory()
    walks = disco.librenms_inventory_walks(addresses, ports)

    assert walks[disco.OID_IP_AD_ENT_IFINDEX][
        f"{disco.OID_IP_AD_ENT_IFINDEX}.8.8.8.10"
    ] == "11"
    assert f"{disco.OID_IF_ALIAS}.11" in walks[disco.OID_IF_ALIAS]
    assert f"{disco.OID_IF_ALIAS}.1001" not in walks[disco.OID_IF_ALIAS]
    assert f"{disco.OID_IP_AD_ENT_IFINDEX}.9.9.9.10" not in walks[
        disco.OID_IP_AD_ENT_IFINDEX
    ]


def test_fixture_librenms_mapping_keeps_alias_name_descr_and_safe_prefixes():
    _metadata, ports, addresses = fixture_inventory()
    results = disco.discover_from_librenms(addresses, ports)
    by_ip = {item["wan_ip"]: item for item in results}

    assert by_ip["8.8.8.10"]["name"] == "WAN1"
    assert by_ip["1.1.1.2"]["name"] == "WAN2"
    assert "9.9.9.10" not in by_ip
    assert "10.20.30.1" not in by_ip
    assert by_ip["4.4.4.4"]["gateway"] == ""
    assert by_ip["208.67.222.222"]["gateway"] == ""

    descr_only = disco.discover_from_librenms(
        [{"port_id": 9, "ipv4_address": "8.8.4.4", "ipv4_prefixlen": 30}],
        [{"port_id": 9, "ifIndex": 90, "ifDescr": "WAN descr", "ifOperStatus": 1}],
    )
    assert descr_only[0]["name"] == "WAN descr"


def test_hybrid_uses_only_route_snmp_and_real_next_hop_wins():
    _metadata, ports, addresses = fixture_inventory()
    calls = []
    results = disco.collect_hybrid(
        "192.0.2.10", "community", disco.wan_keywords("wan"), 2,
        addresses, ports, walk=route_walk(calls=calls),
    )
    by_ip = {item["wan_ip"]: item for item in results}

    assert calls == [disco.OID_CIDR_DEFAULT_NEXTHOP, disco.OID_CIDR_DEFAULT_IFINDEX]
    assert by_ip["8.8.8.10"]["gateway"] == "8.8.8.14"
    assert by_ip["8.8.8.10"]["source"] == "gateway"
    assert by_ip["1.1.1.2"]["gateway"] == "1.1.1.1"
    assert by_ip["1.1.1.2"]["source"] == "librenms_subnet_gateway"


def test_hybrid_retains_rfc1213_route_fallback_without_inventory_walks():
    _metadata, ports, addresses = fixture_inventory()
    calls = []
    results = disco.collect_hybrid(
        "192.0.2.10", "community", disco.wan_keywords("wan"), 2,
        addresses, ports, walk=route_walk(legacy=True, calls=calls),
    )
    assert calls == [
        disco.OID_CIDR_DEFAULT_NEXTHOP,
        disco.OID_CIDR_DEFAULT_IFINDEX,
        disco.OID_ROUTE_DEFAULT_NEXTHOP,
        disco.OID_ROUTE_DEFAULT_IFINDEX,
    ]
    assert next(item for item in results if item["wan_ip"] == "8.8.8.10")[
        "gateway"
    ] == "8.8.8.14"


def test_direct_walk_mapping_never_invents_slash31_or_slash32_gateway():
    for mask in ("255.255.255.254", "255.255.255.255"):
        walks = {
            disco.OID_IF_ALIAS: {f"{disco.OID_IF_ALIAS}.7": "WAN"},
            disco.OID_IP_AD_ENT_IFINDEX: {
                f"{disco.OID_IP_AD_ENT_IFINDEX}.8.8.8.8": "7"
            },
            disco.OID_IP_AD_ENT_NETMASK: {
                f"{disco.OID_IP_AD_ENT_NETMASK}.8.8.8.8": mask
            },
            disco.OID_CIDR_DEFAULT_NEXTHOP: {},
            disco.OID_CIDR_DEFAULT_IFINDEX: {},
        }
        results = disco.discover_from_walks(walks, disco.wan_keywords("wan"))
        assert results[0]["gateway"] == ""
        assert disco.build_file_sd(results, set()) == []


def test_missing_public_port_mapping_is_incomplete_not_port_id_guessing():
    with pytest.raises(disco.ISPDataIncomplete):
        disco.librenms_inventory_walks(
            [{"port_id": 9999, "ipv4_address": "8.8.8.10", "ipv4_prefixlen": 29}],
            [{"port_id": 1, "ifIndex": 11, "ifName": "WAN"}],
        )


def test_stale_inventory_is_rejected_but_unknown_timestamp_is_accepted(monkeypatch):
    monkeypatch.setenv("ISP_LIBRENMS_POLL_MAX_AGE_SECONDS", "600")
    stale = (datetime.now(timezone.utc) - timedelta(seconds=601)).isoformat()
    stale_client = PolicyClient({"192.0.2.10": fixture_inventory(last_polled=stale)})
    with pytest.raises(disco.ISPDataIncomplete):
        disco.fetch_librenms_inventory(stale_client, "192.0.2.10")

    unknown_client = PolicyClient({"192.0.2.10": fixture_inventory()})
    _metadata, addresses, ports = disco.fetch_librenms_inventory(
        unknown_client, "192.0.2.10"
    )
    assert addresses and ports


def test_main_keeps_direct_snmp_before_librenms_fallback(monkeypatch, tmp_path):
    written = []
    monkeypatch.setenv("ISP_TARGETS_FILE", str(tmp_path / "isp.json"))
    monkeypatch.setenv("ISP_GATEWAY_AUTO_DISCOVER", "true")
    monkeypatch.setenv("FIREWALL_SNMP_TARGETS", "192.0.2.1")
    monkeypatch.setenv("BIGSCREEN_ISP_NAMES", "")
    monkeypatch.setenv("ISP_PING", "")
    monkeypatch.setenv("ISP_DISCOVERY_SOURCE", "direct-snmp")
    monkeypatch.setattr(disco, "write_file_sd", lambda _path, payload: written.append(payload))
    monkeypatch.setattr(disco, "collect", lambda *_args, **_kwargs: complete_collection([{
        "gateway": "8.8.8.9", "name": "direct", "wan_ip": "8.8.8.10", "source": "gateway",
    }]))
    monkeypatch.setattr(
        disco, "LibreNMSClient",
        lambda: (_ for _ in ()).throw(AssertionError("direct mode must not use API")),
    )

    disco.main()

    assert written[0][0]["labels"]["display_name"] == "direct"


def test_main_manual_isp_ping_skips_all_automatic_collection(monkeypatch, tmp_path):
    written = []
    calls = []
    monkeypatch.setenv("ISP_TARGETS_FILE", str(tmp_path / "isp.json"))
    monkeypatch.setenv("ISP_GATEWAY_AUTO_DISCOVER", "true")
    monkeypatch.setenv("FIREWALL_SNMP_TARGETS", "192.0.2.1")
    monkeypatch.setenv("BIGSCREEN_ISP_NAMES", "")
    monkeypatch.setenv("ISP_PING", "manual:8.8.8.8")
    monkeypatch.setattr(disco, "write_file_sd", lambda _path, payload: written.append(payload))
    monkeypatch.setattr(disco, "collect", lambda *_args, **_kwargs: calls.append("snmp"))
    monkeypatch.setattr(disco, "LibreNMSClient", lambda: calls.append("api"))

    disco.main()

    assert calls == []
    assert written == [[]]


def _main_env(monkeypatch, tmp_path, source, targets="192.0.2.10"):
    monkeypatch.setenv("ISP_TARGETS_FILE", str(tmp_path / "isp.json"))
    monkeypatch.setenv("ISP_DISCOVERY_STATE_FILE", str(tmp_path / "isp-state.json"))
    monkeypatch.setenv("ISP_GATEWAY_AUTO_DISCOVER", "true")
    monkeypatch.setenv("ISP_DISCOVERY_SOURCE", source)
    monkeypatch.setenv("FIREWALL_SNMP_TARGETS", targets)
    monkeypatch.setenv("FIREWALL_UNIT_SNMP_TARGETS", "")
    monkeypatch.setenv("FIREWALL_SNMP_COMMUNITY", "private-do-not-log")
    monkeypatch.setenv("FIREWALL_WAN_IF_FILTER", "wan")
    monkeypatch.setenv("BIGSCREEN_ISP_NAMES", "")
    monkeypatch.setenv("BIGSCREEN_ISP_IPS", "")
    monkeypatch.setenv("ISP_PING", "")


def test_main_ha_hybrid_uses_only_logical_vip_direct_snmp(
    monkeypatch, tmp_path, capsys
):
    _main_env(monkeypatch, tmp_path, "hybrid", targets="192.168.9.1")
    monkeypatch.setenv(
        "FIREWALL_UNIT_SNMP_TARGETS", "192.168.9.11,192.168.9.12"
    )
    direct_calls = []
    written = []
    monkeypatch.setattr(
        disco,
        "LibreNMSClient",
        lambda: (_ for _ in ()).throw(
            AssertionError("HA VIP hybrid mode must not call LibreNMS")
        ),
    )
    monkeypatch.setattr(
        disco,
        "collect",
        lambda ip, *_args, **_kwargs: direct_calls.append(ip) or complete_collection([{
            "gateway": "8.8.8.9",
            "name": "WAN",
            "wan_ip": "8.8.8.10",
            "source": "gateway",
        }]),
    )
    monkeypatch.setattr(
        disco, "write_file_sd", lambda _path, payload: written.append(payload)
    )

    disco.main()

    assert direct_calls == ["192.168.9.1"]
    assert written[0][0]["targets"] == ["8.8.8.9"]
    log = capsys.readouterr().err
    assert (
        "source=hybrid device=192.168.9.1 mode=ha-vip "
        "inventory=direct-snmp gateway=direct-snmp"
    ) in log
    assert "collection stats: api_requests=0" in log
    assert "LibreNMS WAN inventory failed" not in log


def test_main_librenms_only_never_calls_snmp_and_keeps_output_schema(
    monkeypatch, tmp_path, capsys
):
    _main_env(monkeypatch, tmp_path, "librenms")
    client = PolicyClient({"192.0.2.10": fixture_inventory()})
    written = []
    monkeypatch.setattr(disco, "LibreNMSClient", lambda: client)
    monkeypatch.setattr(
        disco, "collect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no SNMP")),
    )
    monkeypatch.setattr(
        disco, "collect_hybrid",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no route SNMP")),
    )
    monkeypatch.setattr(disco, "write_file_sd", lambda _path, payload: written.append(payload))

    disco.main()

    assert {entry["targets"][0] for entry in written[0]} == {"8.8.8.9", "1.1.1.1"}
    assert all(set(entry) == {"targets", "labels"} for entry in written[0])
    assert all("display_name" in entry["labels"] for entry in written[0])
    log = capsys.readouterr().err
    assert "source=librenms" in log
    assert "collection stats: api_requests=3 snmp_walks=0 snmp_gets=0" in log


def test_main_hybrid_unknown_timestamp_uses_api_inventory_and_route_only(
    monkeypatch, tmp_path, capsys
):
    _main_env(monkeypatch, tmp_path, "hybrid")
    client = PolicyClient({"192.0.2.10": fixture_inventory()})
    written = []
    original = disco.collect_hybrid
    monkeypatch.setattr(disco, "LibreNMSClient", lambda: client)
    monkeypatch.setattr(
        disco, "collect_hybrid",
        lambda ip, community, keywords, timeout, addresses, ports,
               configured_names=None, configured_ips=None: original(
                   ip, community, keywords, timeout, addresses, ports,
                   configured_names, configured_ips, walk=route_walk(),
               ),
    )
    monkeypatch.setattr(
        disco, "collect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("usable unknown-time API data must not fully fall back")
        ),
    )
    monkeypatch.setattr(disco, "write_file_sd", lambda _path, payload: written.append(payload))

    disco.main()

    assert "8.8.8.14" in {entry["targets"][0] for entry in written[0]}
    log = capsys.readouterr().err
    assert "inventory=librenms gateway=direct-snmp" in log
    assert "private-do-not-log" not in log


def test_main_stale_librenms_inventory_falls_back_only_that_device(
    monkeypatch, tmp_path
):
    _main_env(monkeypatch, tmp_path, "hybrid")
    stale = (datetime.now(timezone.utc) - timedelta(seconds=601)).isoformat()
    client = PolicyClient({"192.0.2.10": fixture_inventory(last_polled=stale)})
    direct_calls = []
    written = []
    monkeypatch.setattr(disco, "LibreNMSClient", lambda: client)
    monkeypatch.setattr(
        disco, "collect",
        lambda ip, *_args, **_kwargs: direct_calls.append(ip) or complete_collection([{
            "gateway": "8.8.8.9", "name": "direct", "wan_ip": "8.8.8.10",
            "source": "gateway",
        }]),
    )
    monkeypatch.setattr(disco, "write_file_sd", lambda _path, payload: written.append(payload))

    disco.main()

    assert direct_calls == ["192.0.2.10"]
    assert written[0][0]["targets"] == ["8.8.8.9"]


def test_main_multiple_firewalls_fall_back_per_device_without_secret_leak(
    monkeypatch, tmp_path, capsys
):
    targets = "192.0.2.10,192.0.2.20"
    _main_env(monkeypatch, tmp_path, "hybrid", targets=targets)
    client = PolicyClient(
        {
            "192.0.2.10": fixture_inventory("192.0.2.10"),
            "192.0.2.20": fixture_inventory("192.0.2.20"),
        },
        failures={
            ("192.0.2.20", "device"): LibreNMSUnavailable(
                "token=hidden community=hidden"
            )
        },
    )
    direct_calls = []
    written = []
    original = disco.collect_hybrid
    monkeypatch.setattr(disco, "LibreNMSClient", lambda: client)
    monkeypatch.setattr(
        disco, "collect_hybrid",
        lambda ip, community, keywords, timeout, addresses, ports,
               configured_names=None, configured_ips=None: original(
                   ip, community, keywords, timeout, addresses, ports,
                   configured_names, configured_ips, walk=route_walk(),
               ),
    )
    monkeypatch.setattr(
        disco, "collect",
        lambda ip, *_args, **_kwargs: direct_calls.append(ip) or complete_collection([{
            "gateway": "9.9.9.9", "name": "fallback", "wan_ip": "9.9.9.10",
            "source": "gateway",
        }]),
    )
    monkeypatch.setattr(disco, "write_file_sd", lambda _path, payload: written.append(payload))

    disco.main()

    assert direct_calls == ["192.0.2.20"]
    targets_written = {entry["targets"][0] for entry in written[0]}
    assert {"8.8.8.14", "9.9.9.9"}.issubset(targets_written)
    log = capsys.readouterr().err
    assert "LibreNMSUnavailable" in log
    assert "hidden" not in log and "private-do-not-log" not in log


def test_main_hybrid_global_api_failure_falls_back_each_firewall(
    monkeypatch, tmp_path, capsys
):
    _main_env(
        monkeypatch, tmp_path, "hybrid", targets="192.0.2.10,192.0.2.20"
    )
    client = PolicyClient(
        {
            "192.0.2.10": fixture_inventory("192.0.2.10"),
            "192.0.2.20": fixture_inventory("192.0.2.20"),
        },
        failures={
            ("*", "devices"): LibreNMSUnavailable("token=hidden password=hidden")
        },
    )
    direct_calls = []
    monkeypatch.setattr(disco, "LibreNMSClient", lambda: client)
    monkeypatch.setattr(
        disco, "collect",
        lambda ip, *_args, **_kwargs: direct_calls.append(ip) or complete_collection([{
            "gateway": "8.8.8.9" if ip.endswith(".10") else "1.1.1.1",
            "name": ip, "wan_ip": "8.8.8.10", "source": "gateway",
        }]),
    )
    monkeypatch.setattr(disco, "write_file_sd", lambda *_args: None)

    disco.main()

    assert direct_calls == ["192.0.2.10", "192.0.2.20"]
    log = capsys.readouterr().err
    assert "LibreNMSUnavailable" in log
    assert "hidden" not in log and "private-do-not-log" not in log


def test_main_librenms_only_insufficient_inventory_skips_safely(
    monkeypatch, tmp_path, capsys
):
    _main_env(monkeypatch, tmp_path, "librenms")
    metadata, _ports, addresses = fixture_inventory()
    client = PolicyClient({"192.0.2.10": (metadata, [], addresses)})
    written = []
    monkeypatch.setattr(disco, "LibreNMSClient", lambda: client)
    monkeypatch.setattr(
        disco, "collect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no SNMP")),
    )
    monkeypatch.setattr(disco, "write_file_sd", lambda _path, payload: written.append(payload))

    with pytest.raises(SystemExit) as error:
        disco.main()

    assert error.value.code == 1
    assert written == []
    log = capsys.readouterr().err
    assert "insufficient; skipping" in log
    assert "no valid last-known-good inventory" in log


def test_transient_failure_preserves_last_known_good_inventory(
    monkeypatch, tmp_path, capsys
):
    _main_env(monkeypatch, tmp_path, "direct-snmp")
    target_file = tmp_path / "isp.json"
    prior = json.loads(
        (ISP_FIXTURES / "production-ha-inventory.json").read_text(encoding="utf-8")
    )
    original = json.dumps(prior, ensure_ascii=False, indent=2) + "\n"
    target_file.write_text(original, encoding="utf-8")
    monkeypatch.setattr(disco, "collect", lambda *_args, **_kwargs: failed_collection())

    with pytest.raises(SystemExit) as error:
        disco.main()

    assert error.value.code == 1
    assert target_file.read_text(encoding="utf-8") == original
    state = json.loads((tmp_path / "isp-state.json").read_text(encoding="utf-8"))
    assert state["status"] == "stale"
    assert state["count"] == 5
    assert state["last_success_at"] == int(target_file.stat().st_mtime)
    assert state["last_error_at"] is not None
    assert "preserving last-known-good inventory (5 target(s))" in capsys.readouterr().err


def test_malformed_sources_preserve_last_known_good_inventory(monkeypatch, tmp_path):
    _main_env(monkeypatch, tmp_path, "hybrid")
    target_file = tmp_path / "isp.json"
    prior = json.loads(
        (ISP_FIXTURES / "production-ha-inventory.json").read_text(encoding="utf-8")
    )
    target_file.write_text(json.dumps(prior), encoding="utf-8")
    client = PolicyClient(
        {"192.0.2.10": fixture_inventory()},
        failures={("192.0.2.10", "ports"): LibreNMSUnavailable("malformed response")},
    )
    monkeypatch.setattr(disco, "LibreNMSClient", lambda: client)
    monkeypatch.setattr(disco, "collect", lambda *_args, **_kwargs: failed_collection())

    with pytest.raises(SystemExit):
        disco.main()

    assert json.loads(target_file.read_text(encoding="utf-8")) == prior
    assert json.loads((tmp_path / "isp-state.json").read_text(encoding="utf-8"))["status"] == "stale"


def test_successful_discovery_atomically_replaces_inventory_and_marks_ok(
    monkeypatch, tmp_path
):
    _main_env(monkeypatch, tmp_path, "direct-snmp")
    target_file = tmp_path / "isp.json"
    target_file.write_text("[]\n", encoding="utf-8")
    discovered = [
        {
            "gateway": f"8.8.{index}.1",
            "name": f"ISP-{index}",
            "wan_ip": f"8.8.{index}.2",
            "source": "gateway",
        }
        for index in range(1, 7)
    ]
    monkeypatch.setattr(
        disco, "collect", lambda *_args, **_kwargs: complete_collection(discovered)
    )

    disco.main()

    payload = json.loads(target_file.read_text(encoding="utf-8"))
    state = json.loads((tmp_path / "isp-state.json").read_text(encoding="utf-8"))
    assert len(payload) == 6
    assert state["status"] == "ok"
    assert state["count"] == 6
    assert state["last_success_at"] is not None
    assert state["last_error_at"] is None


def test_disabled_discovery_clears_prior_inventory_without_inheriting_lkg(
    monkeypatch, tmp_path
):
    _main_env(monkeypatch, tmp_path, "direct-snmp")
    monkeypatch.setenv("ISP_GATEWAY_AUTO_DISCOVER", "false")
    target_file = tmp_path / "isp.json"
    target_file.write_text(
        (ISP_FIXTURES / "production-ha-inventory.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    disco.main()

    assert json.loads(target_file.read_text(encoding="utf-8")) == []
    state = json.loads((tmp_path / "isp-state.json").read_text(encoding="utf-8"))
    assert state == {
        "status": "disabled", "last_success_at": None,
        "last_error_at": None, "count": 0,
    }


def test_first_failure_is_error_and_does_not_create_empty_inventory(monkeypatch, tmp_path):
    _main_env(monkeypatch, tmp_path, "direct-snmp")
    monkeypatch.setattr(disco, "collect", lambda *_args, **_kwargs: failed_collection())

    with pytest.raises(SystemExit):
        disco.main()

    assert not (tmp_path / "isp.json").exists()
    state = json.loads((tmp_path / "isp-state.json").read_text(encoding="utf-8"))
    assert state["status"] == "error"
    assert state["last_success_at"] is None


def _structured_walk(responses):
    def walk(_ip, _community, oid, _timeout):
        return responses.get(oid, disco.WalkResult(False, {}, "timeout"))
    return walk


def test_partial_snmp_failure_preserves_lkg_and_true_last_success(
    monkeypatch, tmp_path
):
    _main_env(monkeypatch, tmp_path, "direct-snmp")
    target_file = tmp_path / "isp.json"
    state_file = tmp_path / "isp-state.json"
    prior = json.loads(
        (ISP_FIXTURES / "production-ha-inventory.json").read_text(encoding="utf-8")
    )
    original = json.dumps(prior, ensure_ascii=False, indent=2) + "\n"
    target_file.write_text(original, encoding="utf-8")
    state_file.write_text(json.dumps({
        "status": "ok", "last_success_at": 123456789,
        "last_error_at": None, "count": 5,
    }), encoding="utf-8")
    responses = {
        disco.OID_IF_ALIAS: disco.WalkResult(True, {}),
        disco.OID_IP_AD_ENT_IFINDEX: disco.WalkResult(True, {}),
    }
    outcome = disco.collect(
        "192.0.2.10", "community", disco.wan_keywords("wan"),
        walk=_structured_walk(responses),
    )
    assert outcome == failed_collection()
    monkeypatch.setattr(disco, "collect", lambda *_args, **_kwargs: outcome)

    with pytest.raises(SystemExit) as error:
        disco.main()

    assert error.value.code == 1
    assert target_file.read_text(encoding="utf-8") == original
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["status"] == "stale"
    assert state["count"] == 5
    assert state["last_success_at"] == 123456789
    assert state["last_error_at"] is not None


def test_complete_empty_walks_are_valid_zero_and_route_fallback_can_succeed():
    all_empty = {
        oid: disco.WalkResult(True, {})
        for oid in (
            disco.OID_IF_ALIAS, disco.OID_IF_NAME, disco.OID_IF_DESCR,
            disco.OID_IP_AD_ENT_IFINDEX, disco.OID_IP_AD_ENT_NETMASK,
            disco.OID_CIDR_DEFAULT_NEXTHOP, disco.OID_CIDR_DEFAULT_IFINDEX,
            disco.OID_ROUTE_DEFAULT_NEXTHOP, disco.OID_ROUTE_DEFAULT_IFINDEX,
        )
    }
    outcome = disco.collect(
        "192.0.2.10", "community", disco.wan_keywords("wan"),
        walk=_structured_walk(all_empty),
    )
    assert outcome == complete_collection()

    legacy = dict(all_empty)
    legacy[disco.OID_ROUTE_DEFAULT_NEXTHOP] = disco.WalkResult(
        True, {disco.OID_ROUTE_DEFAULT_NEXTHOP: "8.8.8.9"}
    )
    legacy[disco.OID_ROUTE_DEFAULT_IFINDEX] = disco.WalkResult(
        True, {disco.OID_ROUTE_DEFAULT_IFINDEX: "1"}
    )
    assert disco.collect(
        "192.0.2.10", "community", disco.wan_keywords("wan"),
        walk=_structured_walk(legacy),
    ).complete is True


def test_primary_and_legacy_route_command_failures_are_incomplete():
    responses = {
        oid: disco.WalkResult(True, {})
        for oid in (
            disco.OID_IF_ALIAS, disco.OID_IF_NAME, disco.OID_IF_DESCR,
            disco.OID_IP_AD_ENT_IFINDEX, disco.OID_IP_AD_ENT_NETMASK,
        )
    }
    outcome = disco.collect(
        "192.0.2.10", "community", disco.wan_keywords("wan"),
        walk=_structured_walk(responses),
    )
    assert outcome.complete is False
    assert outcome.items == []


def test_disabled_then_enabled_failure_never_invents_last_success(monkeypatch, tmp_path):
    _main_env(monkeypatch, tmp_path, "direct-snmp")
    monkeypatch.setenv("ISP_GATEWAY_AUTO_DISCOVER", "false")
    disco.main()

    monkeypatch.setenv("ISP_GATEWAY_AUTO_DISCOVER", "true")
    monkeypatch.setattr(disco, "collect", lambda *_args, **_kwargs: failed_collection())
    with pytest.raises(SystemExit):
        disco.main()

    state = json.loads((tmp_path / "isp-state.json").read_text(encoding="utf-8"))
    assert state["status"] == "error"
    assert state["last_success_at"] is None
    assert state["count"] == 0


def test_prior_success_then_disabled_then_enabled_failure_does_not_revive_history(
    monkeypatch, tmp_path
):
    _main_env(monkeypatch, tmp_path, "direct-snmp")
    prior = [
        {
            "gateway": f"8.8.{index}.1",
            "name": f"ISP-{index}",
            "metric_name": f"ethernet0/{index}",
            "wan_ip": f"8.8.{index}.2",
            "source": "gateway",
        }
        for index in range(1, 6)
    ]
    monkeypatch.setattr(
        disco, "collect", lambda *_args, **_kwargs: complete_collection(prior)
    )
    disco.main()
    assert json.loads((tmp_path / "isp-state.json").read_text(encoding="utf-8"))["count"] == 5

    monkeypatch.setenv("ISP_GATEWAY_AUTO_DISCOVER", "false")
    disco.main()
    monkeypatch.setenv("ISP_GATEWAY_AUTO_DISCOVER", "true")
    monkeypatch.setattr(disco, "collect", lambda *_args, **_kwargs: failed_collection())
    with pytest.raises(SystemExit):
        disco.main()

    assert json.loads((tmp_path / "isp.json").read_text(encoding="utf-8")) == []
    state = json.loads((tmp_path / "isp-state.json").read_text(encoding="utf-8"))
    assert state["status"] == "error"
    assert state["last_success_at"] is None
    assert state["count"] == 0
    assert isinstance(state["last_error_at"], int)


def test_legacy_empty_inventory_is_not_inferred_as_lkg(monkeypatch, tmp_path):
    _main_env(monkeypatch, tmp_path, "direct-snmp")
    (tmp_path / "isp.json").write_text("[]\n", encoding="utf-8")
    monkeypatch.setattr(disco, "collect", lambda *_args, **_kwargs: failed_collection())

    with pytest.raises(SystemExit):
        disco.main()

    state = json.loads((tmp_path / "isp-state.json").read_text(encoding="utf-8"))
    assert state["status"] == "error"
    assert state["last_success_at"] is None


def test_inventory_write_failure_cannot_mark_discovery_ok(monkeypatch, tmp_path):
    _main_env(monkeypatch, tmp_path, "direct-snmp")
    target_file = tmp_path / "isp.json"
    state_file = tmp_path / "isp-state.json"
    target_file.write_text(json.dumps([{
        "targets": ["8.8.8.9"], "labels": {"display_name": "old"},
    }]), encoding="utf-8")
    state_file.write_text(json.dumps({
        "status": "ok", "last_success_at": 456,
        "last_error_at": None, "count": 1,
    }), encoding="utf-8")
    monkeypatch.setattr(disco, "collect", lambda *_args, **_kwargs: complete_collection([{
        "gateway": "1.1.1.1", "name": "new", "metric_name": "native-new",
        "wan_ip": "1.1.1.2", "source": "gateway",
    }]))
    monkeypatch.setattr(
        disco, "write_file_sd",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full secret")),
    )

    with pytest.raises(SystemExit) as error:
        disco.main()

    assert error.value.code == 1
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["status"] == "stale"
    assert state["last_success_at"] == 456


def test_successful_zero_is_distinct_from_collection_failure(monkeypatch, tmp_path, capsys):
    _main_env(monkeypatch, tmp_path, "direct-snmp")

    monkeypatch.setattr(
        disco, "collect", lambda *_args, **_kwargs: complete_collection()
    )
    disco.main()

    assert json.loads((tmp_path / "isp.json").read_text(encoding="utf-8")) == []
    state = json.loads((tmp_path / "isp-state.json").read_text(encoding="utf-8"))
    assert state["status"] == "ok"
    assert state["count"] == 0
    assert "discovery succeeded, 0 matching ISP interfaces" in capsys.readouterr().err


def test_hybrid_route_walk_count_is_significantly_below_old_inventory_path():
    _metadata, ports, addresses = fixture_inventory()
    hybrid_calls = []
    disco.collect_hybrid(
        "192.0.2.10", "community", disco.wan_keywords("wan"), 2,
        addresses, ports, walk=route_walk(calls=hybrid_calls),
    )
    inventory = disco.librenms_inventory_walks(addresses, ports)
    old_calls = []
    route = route_walk()

    def full_walk(ip, community, oid, timeout):
        old_calls.append(oid)
        if oid in inventory:
            return inventory[oid]
        return route(ip, community, oid, timeout)

    disco.collect(
        "192.0.2.10", "community", disco.wan_keywords("wan"), 2,
        walk=full_walk,
    )
    assert len(old_calls) == 7
    assert hybrid_calls == [
        disco.OID_CIDR_DEFAULT_NEXTHOP, disco.OID_CIDR_DEFAULT_IFINDEX
    ]


def test_target_ips_parses_named_lists():
    assert disco.target_ips("FW:192.168.9.1, 192.168.9.2\ntelecom:1.2.3.4") == [
        "192.168.9.1", "192.168.9.2", "1.2.3.4",
    ]


if __name__ == "__main__":
    test_parse_walk_strips_types_and_quotes()
    test_discovers_multi_wan_gateways_named_by_interface()
    test_wan_keyword_digit_boundary_matches_like_bridge()
    test_subnet_fallback_when_route_has_no_ifindex()
    test_rfc1213_fallback_single_default_route()
    test_lan_default_route_and_duplicates_are_dropped()
    test_manual_isp_ping_entries_take_precedence()
    test_duplicate_carrier_lines_use_stable_address_identity()
    test_foreign_carrier_names_match_by_keyword()
    test_target_ips_parses_named_lists()
    print("ISP discovery tests passed")
