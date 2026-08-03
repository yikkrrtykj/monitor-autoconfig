"""Unit tests for generate-topology-edges.py parsing logic."""
import importlib.util
import pathlib
import sys


_ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "generate_topology_edges", _ROOT / "generate-topology-edges.py"
)
gte = importlib.util.module_from_spec(_spec)
sys.modules["generate_topology_edges"] = gte
_spec.loader.exec_module(gte)


def test_topology_snmp_calls_are_bounded_and_do_not_retry(monkeypatch):
    calls = []

    class Result:
        stdout = '"core"\n'

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return Result()

    monkeypatch.setenv("TOPOLOGY_SNMP_TIMEOUT", "2")
    monkeypatch.setenv("TOPOLOGY_SNMP_RETRIES", "0")
    monkeypatch.setattr(gte.subprocess, "run", run)
    assert gte.snmpget("192.168.10.254", "global", gte.SYS_NAME_OID) == "core"
    command, kwargs = calls[0]
    assert command[command.index("-t") + 1] == "2.0"
    assert command[command.index("-r") + 1] == "0"
    assert kwargs["timeout"] == 4.0


def test_poll_device_skips_arp_walk_outside_configured_l3_scope(monkeypatch):
    walked = []
    monkeypatch.setattr(gte, "snmpget", lambda *_args, **_kwargs: "access-1")
    monkeypatch.setattr(gte, "snmpwalk", lambda _ip, _community, oid: walked.append(oid) or "")

    device = gte.poll_device("192.168.10.11", "global", collect_arp=False)

    assert device["arp"] == {}
    assert gte.IP_NET_TO_MEDIA_PHYS_ADDRESS_OID not in walked


def test_poll_device_uses_pagp_mapping_for_static_etherchannel(monkeypatch):
    responses = {
        gte.IF_NAME_OID: (
            ".1.3.6.1.2.1.31.1.1.1.1.102 = STRING: Te1/0/2\n"
            ".1.3.6.1.2.1.31.1.1.1.1.202 = STRING: Te2/0/2\n"
            ".1.3.6.1.2.1.31.1.1.1.1.400 = STRING: Po11"
        ),
        gte.IF_OPER_STATUS_OID: (
            ".1.3.6.1.2.1.2.2.1.8.102 = INTEGER: up(1)\n"
            ".1.3.6.1.2.1.2.2.1.8.202 = INTEGER: up(1)\n"
            ".1.3.6.1.2.1.2.2.1.8.400 = INTEGER: up(1)"
        ),
        gte.PAGP_GROUP_IFINDEX_OID: (
            ".1.3.6.1.4.1.9.9.98.1.1.1.1.8.102 = INTEGER: 400\n"
            ".1.3.6.1.4.1.9.9.98.1.1.1.1.8.202 = INTEGER: 400"
        ),
    }
    monkeypatch.setattr(gte, "snmpget", lambda *_args, **_kwargs: "Global-new-stack")
    monkeypatch.setattr(
        gte, "snmpwalk",
        lambda _ip, _community, oid: responses.get(oid, ""),
    )

    device = gte.poll_device("192.168.10.11", "global", collect_arp=False)

    assert device["ifstack"] == {400: [102, 202]}


# ---- strip_string_value() ----

class TestStripStringValue:
    def test_strips_string_prefix(self):
        assert gte.strip_string_value("STRING: GigabitEthernet1/0/1") == "GigabitEthernet1/0/1"

    def test_strips_quotes(self):
        assert gte.strip_string_value('"hello"') == "hello"

    def test_raw_value(self):
        assert gte.strip_string_value("plain") == "plain"

    def test_hex_string_label(self):
        assert gte.strip_string_value("Hex-STRING: AA BB CC") == "AA BB CC"


# ---- parse_ifname() ----

class TestParseIfname:
    def test_basic(self):
        out = (
            ".1.3.6.1.2.1.31.1.1.1.1.1 = STRING: Vlan1\n"
            ".1.3.6.1.2.1.31.1.1.1.1.10101 = STRING: Gi1/0/1"
        )
        assert gte.parse_ifname(out) == {1: "Vlan1", 10101: "Gi1/0/1"}

    def test_empty(self):
        assert gte.parse_ifname("") == {}

    def test_skip_garbage_lines(self):
        out = (
            "junk line without equals\n"
            ".1.3.6.1.2.1.31.1.1.1.1.5 = STRING: Gi1/0/5"
        )
        assert gte.parse_ifname(out) == {5: "Gi1/0/5"}


# ---- parse_if_oper_status() ----

class TestParseIfOperStatus:
    def test_named_and_numeric_values(self):
        out = (
            ".1.3.6.1.2.1.2.2.1.8.1 = INTEGER: up(1)\n"
            ".1.3.6.1.2.1.2.2.1.8.2 = INTEGER: down(2)\n"
            ".1.3.6.1.2.1.2.2.1.8.3 = INTEGER: 1"
        )
        assert gte.parse_if_oper_status(out) == {1: 1, 2: 2, 3: 1}


def test_parse_if_stack_status_keeps_only_active_real_relationships():
    out = (
        ".1.3.6.1.2.1.31.1.2.1.3.400.101 = INTEGER: active(1)\n"
        ".1.3.6.1.2.1.31.1.2.1.3.400.201 = INTEGER: 1\n"
        ".1.3.6.1.2.1.31.1.2.1.3.400.301 = INTEGER: notInService(2)\n"
        ".1.3.6.1.2.1.31.1.2.1.3.0.400 = INTEGER: active(1)"
    )
    assert gte.parse_if_stack_status(out) == {400: [101, 201]}


def test_parse_pagp_manual_etherchannel_member_mapping():
    out = (
        ".1.3.6.1.4.1.9.9.98.1.1.1.1.8.102 = INTEGER: 400\n"
        ".1.3.6.1.4.1.9.9.98.1.1.1.1.8.202 = INTEGER: 400\n"
        ".1.3.6.1.4.1.9.9.98.1.1.1.1.8.301 = INTEGER: 0\n"
        ".1.3.6.1.4.1.9.9.98.1.1.1.1.8.302 = INTEGER: 302"
    )
    assert gte.parse_member_aggregate_ifindex(out) == {400: [102, 202]}


def test_parse_ieee_lacp_member_mapping_and_merge_with_ifstack():
    out = (
        ".1.2.840.10006.300.43.1.2.1.1.13.103 = INTEGER: 401\n"
        ".1.2.840.10006.300.43.1.2.1.1.13.203 = INTEGER: 401"
    )
    lacp = gte.parse_member_aggregate_ifindex(out)
    assert lacp == {401: [103, 203]}
    assert gte.merge_aggregate_member_maps(
        {400: [102]}, {400: [202]}, lacp
    ) == {400: [102, 202], 401: [103, 203]}


# ---- parse_lldp_loc_port_desc() ----

class TestParseLldpLocPortDesc:
    def test_basic(self):
        out = (
            ".1.0.8802.1.1.2.1.3.7.1.3.1 = STRING: Gi1/0/1\n"
            ".1.0.8802.1.1.2.1.3.7.1.3.24 = STRING: Gi1/0/24"
        )
        assert gte.parse_lldp_loc_port_desc(out) == {1: "Gi1/0/1", 24: "Gi1/0/24"}


# ---- parse_lldp_rem_field() ----

class TestParseLldpRemField:
    def test_basic_three_part_index(self):
        out = (
            ".1.0.8802.1.1.2.1.4.1.1.9.0.1.1 = STRING: core-sw\n"
            ".1.0.8802.1.1.2.1.4.1.1.9.0.24.1 = STRING: stage3"
        )
        assert gte.parse_lldp_rem_field(out) == {
            (0, 1, 1): "core-sw",
            (0, 24, 1): "stage3",
        }

    def test_too_short_oid_skipped(self):
        assert gte.parse_lldp_rem_field(".1.2 = STRING: too-short") == {}


# ---- normalize_hostname() ----

class TestNormalizeHostname:
    def test_strips_domain(self):
        assert gte.normalize_hostname("switch1.example.com") == "switch1"

    def test_lowercases(self):
        assert gte.normalize_hostname("SW3-POE") == "sw3-poe"

    def test_empty(self):
        assert gte.normalize_hostname("") == ""


# ---- normalize_port_name() ----

class TestNormalizePortName:
    def test_long_form(self):
        assert gte.normalize_port_name("GigabitEthernet1/0/19") == "1/0/19"

    def test_short_form(self):
        assert gte.normalize_port_name("Gi1/0/19") == "1/0/19"

    def test_two_segments(self):
        assert gte.normalize_port_name("Te0/1") == "0/1"

    def test_no_path_returns_lowercase(self):
        assert gte.normalize_port_name("Ethernet 1") == "ethernet 1"

    def test_port_channel_matches_short_form(self):
        assert gte.normalize_port_name("Port-channel1") == "agg1"
        assert gte.normalize_port_name("Po1") == "agg1"
        assert gte.normalize_port_name("LAG1") == "agg1"

    def test_empty(self):
        assert gte.normalize_port_name("") == ""


# ---- resolve_ifindex() ----

class TestResolveIfindex:
    def test_identity_when_loc_port_in_ifname(self):
        assert gte.resolve_ifindex(10101, {10101: "Gi1/0/1"}, {}) == 10101

    def test_match_via_port_desc_long_vs_short(self):
        ifname = {10119: "Gi1/0/19", 10120: "Gi1/0/20"}
        loc_desc = {19: "GigabitEthernet1/0/19"}
        assert gte.resolve_ifindex(19, ifname, loc_desc) == 10119

    def test_returns_none_when_no_match(self):
        assert gte.resolve_ifindex(99, {1: "Gi1/0/1"}, {99: "alien"}) is None

    def test_returns_none_on_ambiguous_match(self):
        ifname = {1: "Gi1/0/1", 2: "Gi1/0/1"}
        loc_desc = {99: "GigabitEthernet1/0/1"}
        assert gte.resolve_ifindex(99, ifname, loc_desc) is None

    def test_match_port_channel_active_uplink(self):
        ifname = {5001: "Po1", 10101: "Gi1/0/1", 10102: "Gi1/0/2"}
        loc_desc = {1: "Port-channel1"}
        assert gte.resolve_ifindex(1, ifname, loc_desc) == 5001


# ---- canonical_edge_key() ----

class TestCanonicalEdgeKey:
    def test_symmetric(self):
        edge_a = {"from_ip": "1.1.1.1", "from_ifindex": 5, "to_ip": "2.2.2.2", "to_ifindex": 10}
        edge_b = {"from_ip": "2.2.2.2", "from_ifindex": 10, "to_ip": "1.1.1.1", "to_ifindex": 5}
        assert gte.canonical_edge_key(edge_a) == gte.canonical_edge_key(edge_b)


# ---- build_edges() ----

class TestBuildEdges:
    def _devices(self):
        return {
            "10.0.0.1": {
                "ip": "10.0.0.1",
                "sysname": "core-sw",
                "ifname": {1: "Gi1/0/1", 24: "Gi1/0/24"},
                "loc_port_desc": {1: "Gi1/0/1", 24: "Gi1/0/24"},
                "rem_sys": {(0, 24, 1): "stage3"},
                "rem_port_desc": {(0, 24, 1): "Gi1/0/49"},
                "rem_port_id": {},
            },
            "10.0.0.3": {
                "ip": "10.0.0.3",
                "sysname": "stage3",
                "ifname": {49: "Gi1/0/49"},
                "loc_port_desc": {49: "Gi1/0/49"},
                "rem_sys": {(0, 49, 1): "core-sw"},
                "rem_port_desc": {(0, 49, 1): "Gi1/0/24"},
                "rem_port_id": {},
            },
        }

    def test_dedupes_bidirectional_edges(self):
        devices = self._devices()
        name_index = gte.build_name_index(devices)
        edges, placeholders = gte.build_edges(devices, name_index)
        assert len(edges) == 1
        assert placeholders == []
        edge = edges[0]
        assert sorted([edge["from_ip"], edge["to_ip"]]) == ["10.0.0.1", "10.0.0.3"]
        assert edge["from_ifindex"] is not None
        assert edge["to_ifindex"] is not None

    def test_placeholder_for_unmatched_neighbor(self):
        devices = self._devices()
        # core advertises a neighbor we never polled
        devices["10.0.0.1"]["rem_sys"][(0, 12, 1)] = "outsider"
        devices["10.0.0.1"]["rem_port_desc"][(0, 12, 1)] = "Te0/1"
        name_index = gte.build_name_index(devices)
        edges, placeholders = gte.build_edges(devices, name_index)
        assert len(placeholders) == 1
        assert placeholders[0]["neighbor_name"] == "outsider"


class TestPortChannelEdges:
    def test_c1000_single_neighbor_row_is_enriched_with_both_lag_members(self):
        devices = {
            "192.168.10.254": {
                "ip": "192.168.10.254", "sysname": "core",
                "ifname": {11: "Te1/0/1"}, "ifoper": {11: 1}, "ifstack": {},
                "loc_port_desc": {11: "Te1/0/1"},
                "rem_sys": {(0, 11, 1): "new-stack"},
                "rem_port_desc": {(0, 11, 1): "Te1/0/2"}, "rem_port_id": {},
                "cdp_device_id": {}, "cdp_device_port": {}, "cdp_address": {},
            },
            "192.168.10.11": {
                "ip": "192.168.10.11", "sysname": "new-stack",
                "ifname": {102: "Te1/0/2", 202: "Te2/0/2", 400: "Po11"},
                "ifoper": {102: 1, 202: 1, 400: 1},
                "ifstack": {400: [102, 202]},
                "loc_port_desc": {}, "rem_sys": {}, "rem_port_desc": {}, "rem_port_id": {},
                "cdp_device_id": {}, "cdp_device_port": {}, "cdp_address": {},
            },
        }

        edges, _ = gte.build_edges(devices, gte.build_name_index(devices))

        assert len(edges) == 1
        assert edges[0]["to_aggregate_port"] == "Po11"
        assert edges[0]["to_member_ports"] == ["Te1/0/2", "Te2/0/2"]

    def test_c1000_port_name_recovers_lag_when_lldp_ifindex_is_missing(self):
        devices = {
            "192.168.10.11": {
                "ip": "192.168.10.11", "sysname": "Global-new-stack",
                "ifname": {102: "Te1/0/2", 202: "Te2/0/2", 400: "Po11"},
                "ifstack": {400: [102, 202]},
            },
            "192.168.10.254": {
                "ip": "192.168.10.254", "sysname": "core",
                "ifname": {}, "ifstack": {},
            },
        }
        edges = [{
            "from_ip": "192.168.10.11", "from_port": "Te1/0/2", "from_ifindex": None,
            "to_ip": "192.168.10.254", "to_port": "To-Global_2960X-4", "to_ifindex": None,
        }]

        enriched = gte.enrich_aggregate_members(edges, devices)

        assert enriched[0]["from_aggregate_port"] == "Po11"
        assert enriched[0]["from_member_ports"] == ["Te1/0/2", "Te2/0/2"]

    def test_remote_port_display_uses_resolved_ifname(self):
        devices = {
            "10.0.0.1": {
                "ip": "10.0.0.1",
                "sysname": "core-sw",
                "ifname": {5001: "Po1"},
                "loc_port_desc": {1: "Port-channel1"},
                "rem_sys": {(0, 1, 1): "stage3"},
                "rem_port_desc": {(0, 1, 1): "Port-channel10 active"},
                "rem_port_id": {},
            },
            "10.0.0.3": {
                "ip": "10.0.0.3",
                "sysname": "stage3",
                "ifname": {5010: "Po10"},
                "loc_port_desc": {},
                "rem_sys": {},
                "rem_port_desc": {},
                "rem_port_id": {},
            },
        }
        edges, _ = gte.build_edges(devices, gte.build_name_index(devices))
        assert edges[0]["from_ifindex"] == 5001
        assert edges[0]["to_ifindex"] == 5010
        assert edges[0]["to_port"] == "Po10"


# ---- hexstr_to_ipv4() ----

class TestHexstrToIpv4:
    def test_spaced_hex(self):
        assert gte.hexstr_to_ipv4("C0 A8 0A 17") == "192.168.10.23"

    def test_wrong_length(self):
        assert gte.hexstr_to_ipv4("C0 A8 0A") is None

    def test_non_hex(self):
        assert gte.hexstr_to_ipv4("nope") is None


# ---- parse_cdp_field() / parse_cdp_address() ----

class TestParseCdp:
    def test_field_two_part_index(self):
        out = (
            ".1.3.6.1.4.1.9.9.23.1.2.1.1.6.10101.1 = STRING: PMGO-JIESHOU-RIGHT\n"
            ".1.3.6.1.4.1.9.9.23.1.2.1.1.6.10102.1 = STRING: PMGO-core"
        )
        assert gte.parse_cdp_field(out) == {
            (10101, 1): "PMGO-JIESHOU-RIGHT",
            (10102, 1): "PMGO-core",
        }

    def test_address_hex_to_ip(self):
        out = ".1.3.6.1.4.1.9.9.23.1.2.1.1.4.10101.1 = Hex-STRING: C0 A8 0A 17"
        assert gte.parse_cdp_address(out) == {(10101, 1): "192.168.10.23"}


class TestServerAttachmentDiscovery:
    def test_parse_arp_table_includes_vlan_and_mac(self):
        output = (
            ".1.3.6.1.2.1.4.22.1.2.42.192.168.42.203 "
            "= Hex-STRING: 00 11 22 AA BB CC"
        )
        assert gte.parse_arp_table(output, {42: "Vlan42"}) == {
            "192.168.42.203": {
                "mac": "00:11:22:aa:bb:cc",
                "ifindex": 42,
                "vlan": 42,
            }
        }

    def test_exact_qbridge_lookup_maps_bridge_port_to_ifindex(self, monkeypatch):
        responses = {
            f"{gte.DOT1Q_TP_FDB_PORT_OID}.42.0.17.34.170.187.204": "7",
            f"{gte.DOT1D_BASE_PORT_IFINDEX_OID}.7": "10110",
        }
        monkeypatch.setattr(
            gte,
            "snmpget",
            lambda _ip, _community, oid, **_kwargs: responses.get(oid, ""),
        )
        assert gte.lookup_fdb_ifindex(
            "192.168.10.11",
            "global",
            42,
            "00:11:22:aa:bb:cc",
            {10110: "Gi1/0/10"},
        ) == 10110

    def test_deepest_non_uplink_switch_wins(self, monkeypatch):
        devices = {
            "192.168.10.254": {
                "ip": "192.168.10.254",
                "sysname": "core",
                "ifname": {5001: "Po1", 42: "Vlan42"},
                "arp": {
                    "192.168.42.203": {
                        "mac": "00:11:22:aa:bb:cc",
                        "ifindex": 42,
                        "vlan": 42,
                    }
                },
            },
            "192.168.10.11": {
                "ip": "192.168.10.11",
                "sysname": "Global-new-stack",
                "ifname": {5001: "Po1", 10110: "Gi1/0/10"},
                "arp": {},
            },
        }
        edges = [{
            "from_ip": "192.168.10.254",
            "from_ifindex": 5001,
            "to_ip": "192.168.10.11",
            "to_ifindex": 5001,
        }]
        monkeypatch.setenv("CORE_SWITCH_PING", "core:192.168.10.254")
        monkeypatch.setenv("FIREWALL_PING", "")
        monkeypatch.setattr(
            gte,
            "lookup_fdb_ifindex",
            lambda ip, *_args, **_kwargs: {
                "192.168.10.254": 5001,
                "192.168.10.11": 10110,
            }.get(ip),
        )
        found = gte.discover_server_edges(
            devices,
            edges,
            {"192.168.42.203": "sdwan"},
            "global",
        )
        assert len(found) == 1
        assert found[0]["from_ip"] == "192.168.10.11"
        assert found[0]["from_port"] == "Gi1/0/10"
        assert found[0]["to_ip"] == "192.168.42.203"
        assert found[0]["source"] == "fdb"

    def test_cached_physical_owner_avoids_full_switch_fanout(self, monkeypatch):
        devices = {
            "192.168.10.254": {
                "ip": "192.168.10.254", "sysname": "core",
                "ifname": {42: "Vlan42", 5001: "Po1"},
                "arp": {"192.168.42.203": {
                    "mac": "00:11:22:aa:bb:cc", "ifindex": 42, "vlan": 42,
                }},
            },
            "192.168.10.11": {
                "ip": "192.168.10.11", "sysname": "access-1",
                "ifname": {10110: "Gi1/0/10"}, "arp": {},
            },
            "192.168.10.12": {
                "ip": "192.168.10.12", "sysname": "access-2",
                "ifname": {10110: "Gi1/0/10"}, "arp": {},
            },
        }
        calls = []
        monkeypatch.setenv("CORE_SWITCH_PING", "core:192.168.10.254")
        monkeypatch.setenv("FIREWALL_PING", "")
        monkeypatch.setattr(
            gte,
            "lookup_fdb_ifindex",
            lambda ip, *_args, **_kwargs: calls.append(ip) or (10110 if ip == "192.168.10.11" else None),
        )
        cached = [{
            "from_ip": "192.168.10.11", "from_ifindex": 10110,
            "from_port": "Gi1/0/10", "to_ip": "192.168.42.203", "source": "fdb",
        }]

        found = gte.discover_server_edges(
            devices, [], {"192.168.42.203": "sdwan"}, "global", cached,
        )

        assert found[0]["from_ip"] == "192.168.10.11"
        assert calls == ["192.168.10.11"]

    def test_physical_access_port_beats_unconfirmed_transit_port_channel(self, monkeypatch):
        devices = {
            "192.168.10.254": {
                "ip": "192.168.10.254",
                "sysname": "core",
                "ifname": {5001: "Po1", 42: "Vlan42"},
                "arp": {
                    "192.168.42.203": {
                        "mac": "00:11:22:aa:bb:cc",
                        "ifindex": 42,
                        "vlan": 42,
                    }
                },
            },
            "192.168.10.11": {
                "ip": "192.168.10.11",
                "sysname": "Global-new-stack",
                "ifname": {10110: "Gi6/0/43"},
                "arp": {},
            },
            "192.168.10.47": {
                "ip": "192.168.10.47",
                "sysname": "Lan-Server",
                "ifname": {5001: "Po1", 10101: "Gi1/1/1"},
                "arp": {},
            },
        }
        # Lan-Server's topology edge names the physical LAG member. Its FDB,
        # however, reports the logical Po1, so exact endpoint matching alone
        # cannot identify that observation as transit.
        edges = [
            {
                "from_ip": "192.168.10.254",
                "from_ifindex": 10001,
                "to_ip": "192.168.10.11",
                "to_ifindex": 10001,
            },
            {
                "from_ip": "192.168.10.254",
                "from_ifindex": 10003,
                "to_ip": "192.168.10.47",
                "to_ifindex": 10101,
            },
        ]
        monkeypatch.setenv("CORE_SWITCH_PING", "core:192.168.10.254")
        monkeypatch.setenv("FIREWALL_PING", "")
        monkeypatch.setattr(
            gte,
            "lookup_fdb_ifindex",
            lambda ip, *_args, **_kwargs: {
                "192.168.10.11": 10110,
                "192.168.10.47": 5001,
            }.get(ip),
        )

        found = gte.discover_server_edges(
            devices,
            edges,
            {"192.168.42.203": "sdwan"},
            "global",
        )

        assert len(found) == 1
        assert found[0]["from_ip"] == "192.168.10.11"
        assert found[0]["from_port"] == "Gi6/0/43"

    def test_deeper_transit_port_channel_never_steals_server(self, monkeypatch):
        devices = {
            "192.168.10.254": {
                "ip": "192.168.10.254", "sysname": "core",
                "ifname": {42: "Vlan42", 10001: "Te1/0/1"},
                "arp": {"192.168.42.203": {
                    "mac": "00:11:22:aa:bb:cc", "ifindex": 42, "vlan": 42,
                }},
            },
            "192.168.10.11": {
                "ip": "192.168.10.11", "sysname": "Global-new-stack",
                "ifname": {10110: "Gi6/0/43", 10147: "Gi6/0/47"}, "arp": {},
            },
            "192.168.10.47": {
                "ip": "192.168.10.47", "sysname": "Lan-Server",
                "ifname": {5001: "Po1", 10101: "Gi1/1/1"}, "arp": {},
            },
        }
        # Lan-Server is one level deeper, so the old depth-first ranking chose
        # its Po1 even though .11 had the real physical access-port hit.
        edges = [
            {"from_ip": "192.168.10.254", "from_ifindex": 10001,
             "to_ip": "192.168.10.11", "to_ifindex": 10147},
            {"from_ip": "192.168.10.11", "from_ifindex": 10147,
             "to_ip": "192.168.10.47", "to_ifindex": 10101},
        ]
        monkeypatch.setenv("CORE_SWITCH_PING", "core:192.168.10.254")
        monkeypatch.setenv("FIREWALL_PING", "")
        monkeypatch.setattr(
            gte, "lookup_fdb_ifindex",
            lambda ip, *_args, **_kwargs: {
                "192.168.10.11": 10110,
                "192.168.10.47": 5001,
            }.get(ip),
        )

        found = gte.discover_server_edges(
            devices, edges, {"192.168.42.203": "sdwan"}, "global",
        )

        assert len(found) == 1
        assert found[0]["from_ip"] == "192.168.10.11"
        assert found[0]["from_port"] == "Gi6/0/43"

    def test_aggregate_only_server_location_is_left_unresolved(self, monkeypatch):
        devices = {
            "192.168.10.254": {
                "ip": "192.168.10.254", "sysname": "core",
                "ifname": {42: "Vlan42"},
                "arp": {"192.168.42.203": {
                    "mac": "00:11:22:aa:bb:cc", "ifindex": 42, "vlan": 42,
                }},
            },
            "192.168.10.47": {
                "ip": "192.168.10.47", "sysname": "Lan-Server",
                "ifname": {5001: "Po1"}, "arp": {},
            },
        }
        monkeypatch.setenv("CORE_SWITCH_PING", "core:192.168.10.254")
        monkeypatch.setenv("FIREWALL_PING", "")
        monkeypatch.setattr(
            gte, "lookup_fdb_ifindex",
            lambda ip, *_args, **_kwargs: 5001 if ip == "192.168.10.47" else None,
        )

        assert gte.discover_server_edges(
            devices, [], {"192.168.42.203": "sdwan"}, "global",
        ) == []

    def test_cached_server_attachment_survives_transient_fdb_miss(self):
        cached = [{
            "from_ip": "192.168.10.11",
            "from_sysname": "Global-new-stack",
            "from_port": "Gi6/0/43",
            "from_ifindex": 10643,
            "to_ip": "192.168.42.203",
            "to_sysname": "sdwan",
            "to_port": None,
            "to_ifindex": None,
            "source": "fdb",
        }]

        found = gte.preserve_cached_server_edges(
            [], cached, {"192.168.42.203": "sdwan"},
            ["192.168.10.254", "192.168.10.11"],
        )

        assert found == cached

    def test_partial_attachment_ledger_is_completed_from_live_snapshot(self):
        primary = [{
            "from_ip": "192.168.10.11", "to_ip": "192.168.42.203",
            "source": "fdb",
        }]
        fallback = [
            {
                "from_ip": "192.168.10.47", "to_ip": "192.168.42.201",
                "source": "fdb",
            },
            {
                "from_ip": "192.168.10.49", "to_ip": "192.168.42.203",
                "source": "fdb",
            },
        ]
        servers = {
            "192.168.42.201": "server1",
            "192.168.42.203": "sdwan",
        }

        merged = gte.merge_cached_server_ledgers(primary, fallback, servers)

        assert merged == [primary[0], fallback[0]]

    def test_fresh_server_attachment_replaces_cached_location(self):
        cached = [{
            "from_ip": "192.168.10.11", "to_ip": "192.168.42.203",
            "source": "fdb",
        }]
        fresh = [{
            "from_ip": "192.168.10.49", "to_ip": "192.168.42.203",
            "source": "fdb",
        }]

        assert gte.preserve_cached_server_edges(
            fresh, cached, {"192.168.42.203": "sdwan"},
            ["192.168.10.11", "192.168.10.49"],
        ) == []

    def test_cached_attachment_survives_transient_parent_discovery_miss(self):
        cached = [{
            "from_ip": "192.168.10.11", "to_ip": "192.168.42.203",
            "source": "fdb",
        }]

        assert gte.preserve_cached_server_edges(
            [], cached, {"192.168.42.203": "sdwan"},
            ["192.168.10.254"],
        ) == cached

    def test_confirmed_fdb_edge_replaces_weaker_server_edge(self):
        weak = [{
            "from_ip": "192.168.10.254", "to_ip": "192.168.42.203",
            "from_port": "Te1/0/10",
        }]
        confirmed = [{
            "from_ip": "192.168.10.11", "to_ip": "192.168.42.203",
            "from_port": "Gi6/0/43", "source": "fdb",
        }]

        assert gte.replace_server_edges(
            weak, confirmed, {"192.168.42.203": "sdwan"},
        ) == confirmed

    def test_removing_server_from_config_drops_its_cached_attachment(self):
        cached = [{
            "from_ip": "192.168.10.11", "to_ip": "192.168.42.203",
            "source": "fdb",
        }]

        assert gte.preserve_cached_server_edges(
            [], cached, {}, ["192.168.10.11"],
        ) == []


# ---- build_edges() via CDP (Cisco gear without LLDP) ----

class TestBuildEdgesCdp:
    def _devices(self):
        # FOH <-> JIESHOU-RIGHT, discovered only through CDP.
        return {
            "192.168.10.24": {
                "ip": "192.168.10.24", "sysname": "PMGO-FOH",
                "ifname": {10101: "Gi1/0/1"}, "loc_port_desc": {},
                "rem_sys": {}, "rem_port_desc": {}, "rem_port_id": {},
                "cdp_device_id": {(10101, 1): "PMGO-JIESHOU-RIGHT"},
                "cdp_device_port": {(10101, 1): "GigabitEthernet1/0/49"},
                "cdp_address": {(10101, 1): "192.168.10.23"},
            },
            "192.168.10.23": {
                "ip": "192.168.10.23", "sysname": "PMGO-JIESHOU-RIGHT",
                "ifname": {10149: "Gi1/0/49"}, "loc_port_desc": {},
                "rem_sys": {}, "rem_port_desc": {}, "rem_port_id": {},
                "cdp_device_id": {(10149, 1): "PMGO-FOH"},
                "cdp_device_port": {(10149, 1): "GigabitEthernet1/0/1"},
                "cdp_address": {(10149, 1): "192.168.10.24"},
            },
        }

    def test_cdp_builds_and_dedupes(self):
        devices = self._devices()
        edges, placeholders = gte.build_edges(devices, gte.build_name_index(devices))
        assert placeholders == []
        assert len(edges) == 1
        edge = edges[0]
        assert sorted([edge["from_ip"], edge["to_ip"]]) == ["192.168.10.23", "192.168.10.24"]
        assert edge["from_ifindex"] is not None
        assert edge["to_ifindex"] is not None

    def test_cdp_neighbor_ip_via_address_when_name_unknown(self):
        # deviceId is a hostname not in name_index, but cdpCacheAddress resolves it.
        devices = self._devices()
        devices["192.168.10.24"]["cdp_device_id"][(10101, 1)] = "weird-fqdn-not-in-index.local"
        edges, placeholders = gte.build_edges(devices, gte.build_name_index(devices))
        assert any(
            sorted([e["from_ip"], e["to_ip"]]) == ["192.168.10.23", "192.168.10.24"]
            for e in edges
        )

    def test_cdp_edge_on_down_remote_management_port_is_discarded(self):
        devices = self._devices()
        devices["192.168.10.24"]["ifoper"] = {10101: 1}
        devices["192.168.10.23"]["ifoper"] = {10149: 2}
        edges, _ = gte.build_edges(devices, gte.build_name_index(devices))
        assert edges == []

    def test_cdp_address_prevents_same_name_ap_becoming_switch_edge(self):
        devices = self._devices()
        source = devices["192.168.10.24"]
        peer = devices["192.168.10.23"]
        source["cdp_device_id"] = {(10101, 1): peer["sysname"]}
        source["cdp_device_port"] = {(10101, 1): "Fa0"}
        source["cdp_address"] = {(10101, 1): "192.168.200.49"}
        peer["cdp_device_id"] = {}
        peer["cdp_device_port"] = {}
        peer["cdp_address"] = {}

        edges, placeholders = gte.build_edges(devices, gte.build_name_index(devices))

        assert edges == []
        assert len(placeholders) == 1
        assert placeholders[0]["neighbor_port"] == "Fa0"


# ---- load_device_list() merges auto-discovered switches ----

class TestLoadDeviceList:
    def _write_targets(self, tmp_path, payload):
        import json as _json
        path = tmp_path / "switch_targets.json"
        path.write_text(_json.dumps(payload), encoding="utf-8")
        return str(path)

    def test_union_includes_discovered_switches(self, tmp_path, monkeypatch):
        # 运维只填网段时 DIST/TOURNAMENT 为空，发现文件里的交换机必须进轮询清单，
        # 否则接入层之间的 LLDP/CDP 边采不到、拓扑退化成平铺。
        targets = self._write_targets(tmp_path, [
            {"targets": ["192.168.10.11"], "labels": {"display_name": "Global-new-stack"}},
            {"targets": ["192.168.10.81"], "labels": {"display_name": "falak-studio5"}},
        ])
        monkeypatch.setenv("SWITCH_TARGETS_FILE", targets)
        monkeypatch.setenv("TOPOLOGY_DEVICES", "")
        monkeypatch.setenv("CORE_SWITCH_PING", "core:192.168.10.254")
        monkeypatch.setenv("DIST_SWITCH_PING", "")
        monkeypatch.setenv("FIREWALL_PING", "192.168.9.1")
        monkeypatch.setenv("TOURNAMENT_SWITCHES", "")
        devices = gte.load_device_list()
        assert devices == ["192.168.10.254", "192.168.9.1", "192.168.10.11", "192.168.10.81"]

    def test_discovered_duplicates_are_not_repeated(self, tmp_path, monkeypatch):
        targets = self._write_targets(tmp_path, [
            {"targets": ["192.168.10.254"], "labels": {"display_name": "core"}},
        ])
        monkeypatch.setenv("SWITCH_TARGETS_FILE", targets)
        monkeypatch.setenv("TOPOLOGY_DEVICES", "")
        monkeypatch.setenv("CORE_SWITCH_PING", "192.168.10.254")
        monkeypatch.setenv("DIST_SWITCH_PING", "")
        monkeypatch.setenv("FIREWALL_PING", "")
        monkeypatch.setenv("TOURNAMENT_SWITCHES", "")
        assert gte.load_device_list() == ["192.168.10.254"]

    def test_explicit_topology_devices_still_override(self, tmp_path, monkeypatch):
        targets = self._write_targets(tmp_path, [
            {"targets": ["192.168.10.81"], "labels": {}},
        ])
        monkeypatch.setenv("SWITCH_TARGETS_FILE", targets)
        monkeypatch.setenv("TOPOLOGY_DEVICES", "10.0.0.1,10.0.0.2")
        assert gte.load_device_list() == ["10.0.0.1", "10.0.0.2"]

    def test_missing_or_bad_file_is_ignored(self, monkeypatch):
        monkeypatch.setenv("SWITCH_TARGETS_FILE", "/nonexistent/switch_targets.json")
        monkeypatch.setenv("TOPOLOGY_DEVICES", "")
        monkeypatch.setenv("CORE_SWITCH_PING", "192.168.10.254")
        monkeypatch.setenv("DIST_SWITCH_PING", "")
        monkeypatch.setenv("FIREWALL_PING", "")
        monkeypatch.setenv("TOURNAMENT_SWITCHES", "")
        assert gte.load_device_list() == ["192.168.10.254"]


def test_empty_discovery_cycle_does_not_erase_confirmed_topology(tmp_path, monkeypatch):
    import json as _json

    topology_dir = tmp_path / "topology"
    topology_dir.mkdir()
    previous_edges = [{
        "from_ip": "192.168.10.11",
        "from_port": "Gi6/0/43",
        "to_ip": "192.168.42.203",
        "source": "fdb",
    }]
    edges_path = topology_dir / "edges.json"
    attachments_path = topology_dir / "server-attachments.json"
    edges_path.write_text(_json.dumps(previous_edges), encoding="utf-8")
    attachments_path.write_text(_json.dumps(previous_edges), encoding="utf-8")

    monkeypatch.setenv("TOPOLOGY_OUTPUT_DIR", str(topology_dir))
    monkeypatch.setenv("SWITCH_TARGETS_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setenv("TOPOLOGY_DEVICES", "")
    monkeypatch.setenv("CORE_SWITCH_PING", "")
    monkeypatch.setenv("DIST_SWITCH_PING", "")
    monkeypatch.setenv("FIREWALL_PING", "")
    monkeypatch.setenv("TOURNAMENT_SWITCHES", "")

    assert gte.main() == 0
    assert _json.loads(edges_path.read_text(encoding="utf-8")) == previous_edges
    assert _json.loads(attachments_path.read_text(encoding="utf-8")) == previous_edges


def test_missing_network_edge_is_marked_stale_then_expires():
    cached = [{
        "from_ip": "192.168.10.254", "from_port": "Te1/0/1", "from_ifindex": 101,
        "to_ip": "192.168.10.57", "to_port": "Gi1/0/1", "to_ifindex": 1,
        "last_seen": 100.0,
    }]

    retained = gte.retain_cached_network_edges(
        [], cached, ["192.168.10.254", "192.168.10.57"],
        now=100 + 86400, retention_seconds=86400,
    )
    assert len(retained) == 1
    assert retained[0]["stale"] is True
    assert retained[0]["last_seen"] == 100.0

    assert gte.retain_cached_network_edges(
        [], cached, ["192.168.10.254", "192.168.10.57"],
        now=101 + 86400, retention_seconds=86400,
    ) == []


def test_incomplete_cached_reverse_row_cannot_make_live_pair_stale():
    live = [{
        "from_ip": "192.168.10.11", "from_port": "Te1/0/2", "from_ifindex": None,
        "to_ip": "192.168.10.254", "to_port": "To-Global_2960X-4", "to_ifindex": None,
    }]
    cached = [{
        "from_ip": "192.168.10.11", "from_port": None, "from_ifindex": None,
        "to_ip": "192.168.10.254", "to_port": "To-Global_2960X-4", "to_ifindex": None,
        "last_seen": 100.0,
    }]

    merged = gte.retain_cached_network_edges(
        live, cached, ["192.168.10.11", "192.168.10.254"],
        now=200, retention_seconds=86400,
    )

    assert len(merged) == 1
    assert merged[0]["from_port"] == "Te1/0/2"
    assert merged[0]["stale"] is False


def test_fully_identified_missing_parallel_member_remains_stale():
    live = [{
        "from_ip": "192.168.10.254", "from_port": "Te1/0/1", "from_ifindex": None,
        "to_ip": "192.168.10.11", "to_port": "Te1/0/2", "to_ifindex": None,
    }]
    cached = [{
        "from_ip": "192.168.10.254", "from_port": "Te2/0/1", "from_ifindex": None,
        # Catalyst 1000 can repeat the same advertised remote port for both
        # members; the distinct known local member must still be retained.
        "to_ip": "192.168.10.11", "to_port": "Te1/0/2", "to_ifindex": None,
        "last_seen": 100.0,
    }]

    merged = gte.retain_cached_network_edges(
        live, cached, ["192.168.10.11", "192.168.10.254"],
        now=200, retention_seconds=86400,
    )

    assert len(merged) == 2
    assert sum(edge["stale"] is True for edge in merged) == 1


def test_active_aggregate_member_cache_shadow_is_not_retained():
    live = [{
        "from_ip": "192.168.10.254", "from_port": "Te1/0/1", "from_ifindex": 101,
        "from_aggregate_port": "Po11",
        "from_member_ports": ["Te1/0/1", "Te2/0/1"],
        "to_ip": "192.168.10.11", "to_port": "Te1/0/2", "to_ifindex": 102,
        "to_aggregate_port": "Po11",
        "to_member_ports": ["Te1/0/2", "Te2/0/2"],
    }]
    cached = [{
        "from_ip": "192.168.10.254", "from_port": "Te2/0/1", "from_ifindex": 201,
        # Older C1000 observations can carry a non-interface remote alias.
        "to_ip": "192.168.10.11", "to_port": "To-WS-3850-12XS", "to_ifindex": None,
        "last_seen": 100.0,
    }]
    devices = {
        "192.168.10.254": {
            "ifname": {101: "Te1/0/1", 201: "Te2/0/1", 400: "Po11"},
            "ifoper": {101: 1, 201: 1, 400: 1},
        },
        "192.168.10.11": {
            "ifname": {102: "Te1/0/2", 202: "Te2/0/2", 401: "Po11"},
            "ifoper": {102: 1, 202: 1, 401: 1},
        },
    }

    merged = gte.retain_cached_network_edges(
        live, cached, ["192.168.10.254", "192.168.10.11"],
        now=200, retention_seconds=86400, devices=devices,
    )

    assert len(merged) == 1
    assert merged[0]["stale"] is False


def test_down_aggregate_member_cache_row_remains_stale():
    live = [{
        "from_ip": "192.168.10.254", "from_port": "Te1/0/1", "from_ifindex": 101,
        "from_aggregate_port": "Po11",
        "from_member_ports": ["Te1/0/1", "Te2/0/1"],
        "to_ip": "192.168.10.11", "to_port": "Te1/0/2", "to_ifindex": 102,
    }]
    cached = [{
        "from_ip": "192.168.10.254", "from_port": "Te2/0/1", "from_ifindex": 201,
        "to_ip": "192.168.10.11", "to_port": "Te2/0/2", "to_ifindex": None,
        "last_seen": 100.0,
    }]
    devices = {
        "192.168.10.254": {
            "ifname": {101: "Te1/0/1", 201: "Te2/0/1", 400: "Po11"},
            "ifoper": {101: 1, 201: 2, 400: 1},
        },
    }

    merged = gte.retain_cached_network_edges(
        live, cached, ["192.168.10.254", "192.168.10.11"],
        now=200, retention_seconds=86400, devices=devices,
    )

    assert len(merged) == 2
    assert sum(edge["stale"] is True for edge in merged) == 1


def test_cached_edge_cannot_overwrite_a_live_port_move():
    live = [{
        "from_ip": "192.168.10.254", "from_port": "Te1/0/1", "from_ifindex": 101,
        "to_ip": "192.168.10.58", "to_port": "Gi1/0/1", "to_ifindex": 1,
    }]
    cached = [{
        "from_ip": "192.168.10.254", "from_port": "Te1/0/1", "from_ifindex": 101,
        "to_ip": "192.168.10.57", "to_port": "Gi1/0/1", "to_ifindex": 1,
        "last_seen": 100.0,
    }]

    merged = gte.retain_cached_network_edges(
        live, cached, ["192.168.10.254", "192.168.10.57", "192.168.10.58"],
        now=200, retention_seconds=86400,
    )

    assert len(merged) == 1
    assert merged[0]["to_ip"] == "192.168.10.58"
    assert merged[0]["stale"] is False
