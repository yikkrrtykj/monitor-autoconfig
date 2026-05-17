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


# ---- resolve_ifindex() ----

class TestResolveIfindex:
    def test_identity_when_loc_port_in_ifname(self):
        assert gte.resolve_ifindex(10101, {10101: "Gi1/0/1"}, {}) == 10101

    def test_match_via_port_desc(self):
        ifname = {10101: "Gi1/0/1", 10102: "Gi1/0/2"}
        loc_desc = {2: "Gi1/0/2"}
        assert gte.resolve_ifindex(2, ifname, loc_desc) == 10102

    def test_returns_none_when_no_match(self):
        assert gte.resolve_ifindex(99, {1: "Gi1/0/1"}, {99: "alien"}) is None


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


# ---- build_uplink_targets() ----

class TestBuildUplinkTargets:
    def test_each_endpoint_becomes_target(self):
        edges = [
            {
                "from_ip": "10.0.0.1", "from_ifindex": 24, "from_port": "Gi1/0/24",
                "to_ip": "10.0.0.3", "to_ifindex": 49, "to_port": "Gi1/0/49",
            },
        ]
        targets = gte.build_uplink_targets(edges)
        # one entry per (device, ifindex) -> 2 targets for one edge
        assert len(targets) == 2
        labels_pairs = sorted([(t["targets"][0], t["labels"]["ifIndex"]) for t in targets])
        assert labels_pairs == [("10.0.0.1", "24"), ("10.0.0.3", "49")]

    def test_dedupes_when_same_endpoint_in_multiple_edges(self):
        edges = [
            {
                "from_ip": "10.0.0.1", "from_ifindex": 24, "from_port": "Gi1/0/24",
                "to_ip": "10.0.0.3", "to_ifindex": 49, "to_port": "Gi1/0/49",
            },
            {
                "from_ip": "10.0.0.1", "from_ifindex": 24, "from_port": "Gi1/0/24",
                "to_ip": "10.0.0.4", "to_ifindex": 50, "to_port": "Gi1/0/50",
            },
        ]
        targets = gte.build_uplink_targets(edges)
        # 10.0.0.1:24 should appear only once
        ones = [t for t in targets if t["targets"][0] == "10.0.0.1" and t["labels"]["ifIndex"] == "24"]
        assert len(ones) == 1
