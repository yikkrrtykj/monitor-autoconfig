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
