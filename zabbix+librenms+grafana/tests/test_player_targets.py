"""Unit tests for generate-player-targets.py parsing logic.

Pure functions only. Module is loaded via importlib because the script
ships with hyphens in its filename (matching its container path).
"""
import importlib.util
import pathlib
import sys
from ipaddress import IPv4Network


# Load the script once at module import. Avoids the `from conftest import gpt`
# pattern, which doesn't resolve cleanly when conftest.py is auto-loaded.
_ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "generate_player_targets", _ROOT / "generate-player-targets.py"
)
gpt = importlib.util.module_from_spec(_spec)
sys.modules["generate_player_targets"] = gpt
_spec.loader.exec_module(gpt)


# ---- TEAM_RE regex ------------------------------------------------

class TestTeamRegex:
    def test_basic_team_seat(self):
        m = gpt.TEAM_RE.search("team01-01")
        assert m and (int(m.group(1)), int(m.group(2))) == (1, 1)

    def test_no_padding(self):
        m = gpt.TEAM_RE.search("team1-1")
        assert m and (int(m.group(1)), int(m.group(2))) == (1, 1)

    def test_underscore_separator(self):
        m = gpt.TEAM_RE.search("team02_03")
        assert m and (int(m.group(1)), int(m.group(2))) == (2, 3)

    def test_double_digit(self):
        m = gpt.TEAM_RE.search("team16-04")
        assert m and (int(m.group(1)), int(m.group(2))) == (16, 4)

    def test_case_insensitive(self):
        m = gpt.TEAM_RE.search("Team05-02")
        assert m and (int(m.group(1)), int(m.group(2))) == (5, 2)

    def test_with_surrounding_text(self):
        m = gpt.TEAM_RE.search("Stage1 team03-02 G1/0/12")
        assert m and (int(m.group(1)), int(m.group(2))) == (3, 2)

    def test_no_match(self):
        assert gpt.TEAM_RE.search("Uplink") is None
        assert gpt.TEAM_RE.search("trunk") is None


# ---- parse_ifalias() ----------------------------------------------

class TestParseIfalias:
    def test_basic(self):
        out = (
            ".1.3.6.1.2.1.31.1.1.1.18.1 = STRING: team01-01\n"
            ".1.3.6.1.2.1.31.1.1.1.18.2 = STRING: team01-02"
        )
        assert gpt.parse_ifalias(out) == {
            1: {"team": 1, "seat": 1},
            2: {"team": 1, "seat": 2},
        }

    def test_empty_input(self):
        assert gpt.parse_ifalias("") == {}

    def test_quoted_value(self):
        out = '.1.3.6.1.2.1.31.1.1.1.18.5 = STRING: "team02-03"'
        assert gpt.parse_ifalias(out) == {5: {"team": 2, "seat": 3}}

    def test_garbage_lines_skipped(self):
        out = (
            "garbage line without equals\n"
            ".1.3.6.1.2.1.31.1.1.1.18.1 = STRING: team01-01\n"
            ".1.3.6.1.2.1.31.1.1.1.18.2 = STRING: Uplink"
        )
        assert gpt.parse_ifalias(out) == {1: {"team": 1, "seat": 1}}

    def test_multiple_teams(self):
        out = "\n".join([
            f".1.3.6.1.2.1.31.1.1.1.18.{i} = STRING: team0{(i-1)//4 + 1}-0{(i-1)%4 + 1}"
            for i in range(1, 9)
        ])
        result = gpt.parse_ifalias(out)
        assert len(result) == 8
        assert result[1] == {"team": 1, "seat": 1}
        assert result[5] == {"team": 2, "seat": 1}
        assert result[8] == {"team": 2, "seat": 4}


# ---- parse_arp_ifindex() ------------------------------------------

class TestParseArpIfindex:
    def test_basic(self):
        out = ".1.3.6.1.2.1.4.22.1.1.5.192.168.11.10 = INTEGER: 5"
        result = gpt.parse_arp_ifindex(out)
        assert (5, "192.168.11.10") in result

    def test_short_oid_skipped(self):
        out = ".1.3.6.1.2.1.4.22.1.1.5 = INTEGER: 5"
        assert gpt.parse_arp_ifindex(out) == {}

    def test_invalid_ip_skipped(self):
        out = ".1.3.6.1.2.1.4.22.1.1.5.999.168.11.10 = INTEGER: 5"
        assert gpt.parse_arp_ifindex(out) == {}

    def test_multiple_entries(self):
        out = "\n".join([
            ".1.3.6.1.2.1.4.22.1.1.5.192.168.11.10 = INTEGER: 5",
            ".1.3.6.1.2.1.4.22.1.1.6.192.168.11.20 = INTEGER: 6",
            ".1.3.6.1.2.1.4.22.1.1.7.192.168.11.30 = INTEGER: 7",
        ])
        result = gpt.parse_arp_ifindex(out)
        assert len(result) == 3
        assert (5, "192.168.11.10") in result
        assert (7, "192.168.11.30") in result

    def test_empty_input(self):
        assert gpt.parse_arp_ifindex("") == {}


# ---- ip_in_subnets() ----------------------------------------------

class TestIpInSubnets:
    def test_empty_subnet_list_returns_false(self):
        # Regression: this used to return True, causing every IP to match the
        # wireless filter when WIRELESS_SUBNETS was unset (commit 6292387).
        assert gpt.ip_in_subnets("192.168.1.1", []) is False

    def test_match_single_subnet(self):
        nets = [IPv4Network("192.168.1.0/24")]
        assert gpt.ip_in_subnets("192.168.1.50", nets) is True

    def test_no_match_single_subnet(self):
        nets = [IPv4Network("192.168.1.0/24")]
        assert gpt.ip_in_subnets("10.0.0.1", nets) is False

    def test_match_one_of_many(self):
        nets = [IPv4Network("10.0.0.0/8"), IPv4Network("192.168.0.0/16")]
        assert gpt.ip_in_subnets("192.168.5.5", nets) is True

    def test_invalid_ip(self):
        nets = [IPv4Network("192.168.1.0/24")]
        assert gpt.ip_in_subnets("not.an.ip", nets) is False

    def test_slash_32_edge(self):
        nets = [IPv4Network("192.168.1.5/32")]
        assert gpt.ip_in_subnets("192.168.1.5", nets) is True
        assert gpt.ip_in_subnets("192.168.1.6", nets) is False


# ---- parse_static_player_targets() --------------------------------

class TestParseStaticPlayerTargets:
    def test_basic_wireless_target(self):
        wireless = [IPv4Network("192.168.12.0/24")]
        result = gpt.parse_static_player_targets(
            "1-1=192.168.12.101", [], wireless, "wireless"
        )
        assert result == [{
            "targets": ["192.168.12.101"],
            "labels": {
                "team": "1",
                "seat": "1",
                "switch": "static",
                "network": "wireless",
                "role": "player",
            },
        }]

    def test_team_prefix_and_explicit_network(self):
        result = gpt.parse_static_player_targets(
            "team02-05=192.168.11.205:wired", [], [], "wireless"
        )
        assert result[0]["labels"]["team"] == "2"
        assert result[0]["labels"]["seat"] == "5"
        assert result[0]["labels"]["network"] == "wired"

    def test_at_separator_and_subnet_inference(self):
        wired = [IPv4Network("192.168.11.0/24")]
        result = gpt.parse_static_player_targets(
            "2-3@192.168.11.203", wired, [], "wireless"
        )
        assert result[0]["labels"]["network"] == "wired"

    def test_invalid_entries_are_skipped(self):
        result = gpt.parse_static_player_targets(
            "bad,1-1=not.an.ip,2-2=192.168.12.22:cellular", [], [], "wireless"
        )
        assert result == []


# ---- build_wireless_scan_targets() --------------------------------

class TestBuildWirelessScanTargets:
    def test_sorts_and_assigns_synthetic_teams(self):
        result = gpt.build_wireless_scan_targets(
            ["172.16.40.80", "172.16.40.73", "172.16.40.68"],
            team_size=2,
        )
        assert [item["targets"][0] for item in result] == [
            "172.16.40.68",
            "172.16.40.73",
            "172.16.40.80",
        ]
        assert [item["labels"]["team"] for item in result] == ["1", "1", "2"]
        assert [item["labels"]["seat"] for item in result] == ["1", "2", "1"]
        assert all(item["labels"]["network"] == "wireless" for item in result)

    def test_zero_limit_keeps_all_targets(self):
        result = gpt.build_wireless_scan_targets(
            ["172.16.40.10", "172.16.40.11", "172.16.40.12"],
            limit=0,
            team_size=5,
        )
        assert [item["targets"][0] for item in result] == [
            "172.16.40.10",
            "172.16.40.11",
            "172.16.40.12",
        ]

    def test_positive_limit_caps_scan_targets(self):
        result = gpt.build_wireless_scan_targets(
            ["172.16.40.10", "172.16.40.11", "172.16.40.12"],
            limit=2,
            team_size=5,
        )
        assert [item["targets"][0] for item in result] == ["172.16.40.10", "172.16.40.11"]
