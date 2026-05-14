"""Unit tests for generate-player-targets.py parsing logic.

Pure functions only. Module is loaded via importlib because the script
ships with hyphens in its filename (matching its container path).
"""
import importlib.util
import os
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


class TestWirelessScanExclusions:
    def test_exclude_single_ip(self):
        assert gpt.parse_excluded_ip_item("172.16.40.220") == {"172.16.40.220"}

    def test_exclude_short_last_octet_range(self):
        result = gpt.parse_excluded_ip_item("172.16.40.220-224")
        assert result == {
            "172.16.40.220",
            "172.16.40.221",
            "172.16.40.222",
            "172.16.40.223",
            "172.16.40.224",
        }

    def test_exclude_full_ip_range(self):
        result = gpt.parse_excluded_ip_item("172.16.40.252-172.16.40.254")
        assert result == {"172.16.40.252", "172.16.40.253", "172.16.40.254"}

    def test_load_excluded_ips_accepts_mixed_items(self):
        os.environ["TEST_WIRELESS_EXCLUDE"] = "172.16.40.220-222,172.16.40.10"
        try:
            result = gpt.load_excluded_ips("TEST_WIRELESS_EXCLUDE")
        finally:
            os.environ.pop("TEST_WIRELESS_EXCLUDE", None)
        assert result == {
            "172.16.40.10",
            "172.16.40.220",
            "172.16.40.221",
            "172.16.40.222",
        }

    def test_exclude_reversed_range_is_invalid(self):
        try:
            gpt.parse_excluded_ip_item("172.16.40.254-220")
        except ValueError:
            return
        assert False, "expected ValueError"

    def test_gateway_like_ips_excludes_first_and_last_host(self):
        result = gpt.gateway_like_ips([IPv4Network("172.16.40.0/24")])
        assert "172.16.40.1" in result
        assert "172.16.40.254" in result
        assert "172.16.40.2" not in result

    def test_gateway_like_ips_keeps_tiny_subnets(self):
        assert gpt.gateway_like_ips([IPv4Network("172.16.40.0/30")]) == set()


# ---- normalize_mac() ----------------------------------------------

class TestNormalizeMac:
    def test_hex_string_with_spaces(self):
        assert gpt.normalize_mac("Hex-STRING: 00 1a 2b 3c 4d 5e") == "00:1a:2b:3c:4d:5e"

    def test_string_colon_form(self):
        assert gpt.normalize_mac("STRING: 0:1a:2b:3c:4d:5e") == "00:1a:2b:3c:4d:5e"

    def test_bare_colon_form(self):
        assert gpt.normalize_mac("aa:bb:cc:dd:ee:ff") == "aa:bb:cc:dd:ee:ff"

    def test_upper_case_normalised(self):
        assert gpt.normalize_mac("AA:BB:CC:DD:EE:FF") == "aa:bb:cc:dd:ee:ff"

    def test_dash_separator(self):
        assert gpt.normalize_mac("00-1a-2b-3c-4d-5e") == "00:1a:2b:3c:4d:5e"

    def test_quoted_value(self):
        assert gpt.normalize_mac('"00 1a 2b 3c 4d 5e"') == "00:1a:2b:3c:4d:5e"

    def test_too_few_bytes(self):
        assert gpt.normalize_mac("00 1a 2b 3c 4d") is None

    def test_too_many_bytes(self):
        assert gpt.normalize_mac("00 1a 2b 3c 4d 5e 6f") is None

    def test_none_input(self):
        assert gpt.normalize_mac(None) is None

    def test_garbage_input(self):
        assert gpt.normalize_mac("hello world") is None


# ---- mac_from_decimal_suffix() ------------------------------------

class TestMacFromDecimalSuffix:
    def test_basic(self):
        # 0,26,43,60,77,94 = 00:1a:2b:3c:4d:5e
        assert gpt.mac_from_decimal_suffix(["0", "26", "43", "60", "77", "94"]) == "00:1a:2b:3c:4d:5e"

    def test_takes_last_six_octets(self):
        # OID prefix before the MAC should be ignored
        assert gpt.mac_from_decimal_suffix(["1", "3", "6", "1", "0", "26", "43", "60", "77", "94"]) == "00:1a:2b:3c:4d:5e"

    def test_short_input_returns_none(self):
        assert gpt.mac_from_decimal_suffix(["0", "26", "43", "60", "77"]) is None

    def test_out_of_range_octet(self):
        assert gpt.mac_from_decimal_suffix(["0", "26", "43", "60", "77", "300"]) is None

    def test_non_numeric(self):
        assert gpt.mac_from_decimal_suffix(["0", "26", "43", "60", "77", "abc"]) is None


# ---- parse_dot1d_fdb() / parse_dot1d_baseport() -------------------

class TestParseDot1dFdb:
    def test_basic(self):
        # MAC 00:1a:2b:3c:4d:5e on bridgePort 5
        out = ".1.3.6.1.2.1.17.4.3.1.2.0.26.43.60.77.94 = INTEGER: 5"
        assert gpt.parse_dot1d_fdb(out) == {"00:1a:2b:3c:4d:5e": 5}

    def test_zero_port_dropped(self):
        # port 0 means "learning" / "no port" - drop it
        out = ".1.3.6.1.2.1.17.4.3.1.2.0.26.43.60.77.94 = INTEGER: 0"
        assert gpt.parse_dot1d_fdb(out) == {}

    def test_multiple_entries(self):
        out = "\n".join([
            ".1.3.6.1.2.1.17.4.3.1.2.0.26.43.60.77.94 = INTEGER: 5",
            ".1.3.6.1.2.1.17.4.3.1.2.170.187.204.221.238.255 = INTEGER: 7",
        ])
        result = gpt.parse_dot1d_fdb(out)
        assert result == {
            "00:1a:2b:3c:4d:5e": 5,
            "aa:bb:cc:dd:ee:ff": 7,
        }

    def test_empty_input(self):
        assert gpt.parse_dot1d_fdb("") == {}


class TestParseDot1dBaseport:
    def test_basic(self):
        # bridgePort 5 -> ifIndex 105
        out = ".1.3.6.1.2.1.17.1.4.1.2.5 = INTEGER: 105"
        assert gpt.parse_dot1d_baseport(out) == {5: 105}

    def test_multiple_entries(self):
        out = "\n".join([
            ".1.3.6.1.2.1.17.1.4.1.2.1 = INTEGER: 101",
            ".1.3.6.1.2.1.17.1.4.1.2.2 = INTEGER: 102",
            ".1.3.6.1.2.1.17.1.4.1.2.3 = INTEGER: 103",
        ])
        assert gpt.parse_dot1d_baseport(out) == {1: 101, 2: 102, 3: 103}

    def test_empty_input(self):
        assert gpt.parse_dot1d_baseport("") == {}


class TestParseDot1qFdb:
    def test_basic_with_vlan(self):
        # VLAN 10, MAC 00:1a:2b:3c:4d:5e, bridgePort 5
        out = ".1.3.6.1.2.1.17.7.1.2.2.1.2.10.0.26.43.60.77.94 = INTEGER: 5"
        assert gpt.parse_dot1q_fdb(out) == {"00:1a:2b:3c:4d:5e": 5}

    def test_vlan_dimension_dropped(self):
        # Same MAC seen on two VLANs - only last entry kept (dict semantics)
        out = "\n".join([
            ".1.3.6.1.2.1.17.7.1.2.2.1.2.10.0.26.43.60.77.94 = INTEGER: 5",
            ".1.3.6.1.2.1.17.7.1.2.2.1.2.20.0.26.43.60.77.94 = INTEGER: 7",
        ])
        result = gpt.parse_dot1q_fdb(out)
        assert len(result) == 1
        assert result["00:1a:2b:3c:4d:5e"] in (5, 7)

    def test_port_zero_dropped(self):
        out = ".1.3.6.1.2.1.17.7.1.2.2.1.2.10.0.26.43.60.77.94 = INTEGER: 0"
        assert gpt.parse_dot1q_fdb(out) == {}

    def test_empty_input(self):
        assert gpt.parse_dot1q_fdb("") == {}


# ---- parse_arp_macaddr() ------------------------------------------

class TestParseArpMacaddr:
    def test_basic_hex_string(self):
        # ifIndex 5, IP 192.168.11.100, MAC aa:bb:cc:dd:ee:ff
        out = ".1.3.6.1.2.1.4.22.1.2.5.192.168.11.100 = Hex-STRING: AA BB CC DD EE FF"
        assert gpt.parse_arp_macaddr(out) == {"192.168.11.100": "aa:bb:cc:dd:ee:ff"}

    def test_string_form(self):
        out = ".1.3.6.1.2.1.4.22.1.2.5.192.168.11.100 = STRING: aa:bb:cc:dd:ee:ff"
        assert gpt.parse_arp_macaddr(out) == {"192.168.11.100": "aa:bb:cc:dd:ee:ff"}

    def test_invalid_mac_skipped(self):
        out = ".1.3.6.1.2.1.4.22.1.2.5.192.168.11.100 = Hex-STRING: AA BB CC"
        assert gpt.parse_arp_macaddr(out) == {}

    def test_invalid_ip_skipped(self):
        out = ".1.3.6.1.2.1.4.22.1.2.5.999.168.11.100 = Hex-STRING: AA BB CC DD EE FF"
        assert gpt.parse_arp_macaddr(out) == {}

    def test_multiple_entries(self):
        out = "\n".join([
            ".1.3.6.1.2.1.4.22.1.2.5.172.25.11.10 = Hex-STRING: 00 1a 2b 3c 4d 5e",
            ".1.3.6.1.2.1.4.22.1.2.5.172.25.11.11 = Hex-STRING: AA BB CC DD EE FF",
        ])
        assert gpt.parse_arp_macaddr(out) == {
            "172.25.11.10": "00:1a:2b:3c:4d:5e",
            "172.25.11.11": "aa:bb:cc:dd:ee:ff",
        }

    def test_empty_input(self):
        assert gpt.parse_arp_macaddr("") == {}


# ---- join_gateway_arp_to_teams() ----------------------------------

class TestJoinGatewayArpToTeams:
    def _stage_index(self):
        return {
            "172.25.10.3": {
                "ifalias": {
                    101: {"team": 1, "seat": 1},
                    102: {"team": 1, "seat": 2},
                },
                "mac_to_ifindex": {
                    "00:1a:2b:3c:4d:5e": 101,
                    "00:1a:2b:3c:4d:5f": 102,
                },
            },
            "172.25.10.4": {
                "ifalias": {
                    201: {"team": 2, "seat": 1},
                },
                "mac_to_ifindex": {
                    "aa:bb:cc:dd:ee:ff": 201,
                },
            },
        }

    def test_emits_targets_for_matched_macs(self):
        arp = {
            "172.25.11.10": "00:1a:2b:3c:4d:5e",
            "172.25.11.11": "00:1a:2b:3c:4d:5f",
            "172.25.11.20": "aa:bb:cc:dd:ee:ff",
        }
        targets, stats = gpt.join_gateway_arp_to_teams(arp, self._stage_index(), [])
        assert stats["matched"] == 3
        assert stats["unmatched_macs"] == 0
        by_ip = {t["targets"][0]: t["labels"] for t in targets}
        assert by_ip["172.25.11.10"]["team"] == "1"
        assert by_ip["172.25.11.10"]["seat"] == "1"
        assert by_ip["172.25.11.10"]["switch"] == "172.25.10.3"
        assert by_ip["172.25.11.20"]["switch"] == "172.25.10.4"
        assert all(label["network"] == "wired" for label in by_ip.values())

    def test_unmatched_macs_counted(self):
        arp = {"172.25.11.99": "de:ad:be:ef:00:01"}
        targets, stats = gpt.join_gateway_arp_to_teams(arp, self._stage_index(), [])
        assert targets == []
        assert stats["matched"] == 0
        assert stats["unmatched_macs"] == 1

    def test_wireless_classification_when_ip_in_wireless_subnet(self):
        wireless = [IPv4Network("172.25.12.0/24")]
        arp = {"172.25.12.50": "00:1a:2b:3c:4d:5e"}
        targets, _ = gpt.join_gateway_arp_to_teams(arp, self._stage_index(), wireless)
        assert targets[0]["labels"]["network"] == "wireless"

    def test_trust_team_label_even_when_ip_outside_player_subnets(self):
        # Regression: previously the loop would drop IPs not in PLAYER_SUBNETS
        # even when the port had a team label. Now the team label is
        # authoritative.
        arp = {"10.99.99.99": "00:1a:2b:3c:4d:5e"}
        targets, stats = gpt.join_gateway_arp_to_teams(arp, self._stage_index(), [])
        assert stats["matched"] == 1
        assert stats["unmatched_macs"] == 0
        assert targets[0]["labels"]["network"] == "wired"
        assert targets[0]["labels"]["team"] == "1"


# ---- merge_dedup_targets() ----------------------------------------

class TestMergeDedupTargets:
    def _target(self, team, seat, ip, switch):
        return {
            "targets": [ip],
            "labels": {
                "team": str(team),
                "seat": str(seat),
                "switch": switch,
                "network": "wired",
                "role": "player",
            },
        }

    def test_path_b_wins_on_conflict(self):
        path_a = [self._target(1, 1, "172.25.11.10", "stage-via-arp")]
        path_b = [self._target(1, 1, "172.25.11.10", "stage-via-mac-table")]
        merged = gpt.merge_dedup_targets(path_b, path_a)
        assert len(merged) == 1
        assert merged[0]["labels"]["switch"] == "stage-via-mac-table"

    def test_distinct_entries_kept(self):
        path_a = [self._target(1, 1, "172.25.11.10", "sw-A")]
        path_b = [self._target(2, 1, "172.25.11.20", "sw-B")]
        merged = gpt.merge_dedup_targets(path_b, path_a)
        assert len(merged) == 2

    def test_dedup_key_is_team_seat_ip(self):
        # Same team+seat but different IP -> two entries
        path_a = [self._target(1, 1, "172.25.11.10", "sw")]
        path_b = [self._target(1, 1, "172.25.11.11", "sw")]
        merged = gpt.merge_dedup_targets(path_b, path_a)
        assert len(merged) == 2


# ---- build_stage_mac_index() community-indexing -------------------

class TestBuildStageMacIndexCommunityIndexing:
    def setup_method(self):
        self._snmpwalk = gpt.snmpwalk
        self.calls = []

    def teardown_method(self):
        gpt.snmpwalk = self._snmpwalk

    def _install_fake_snmpwalk(self, responses):
        """responses: dict keyed by (host, community, oid) -> stdout."""
        def fake(host, community, oid, timeout=15):
            self.calls.append((host, community, oid))
            return responses.get((host, community, oid), "")
        gpt.snmpwalk = fake

    def test_default_context_only_when_no_vlan_ids(self):
        self._install_fake_snmpwalk({
            ("10.0.0.1", "global", gpt.IF_ALIAS_OID):
                ".1.3.6.1.2.1.31.1.1.1.18.10 = STRING: team01-01",
            ("10.0.0.1", "global", gpt.BRIDGE_MIB_BASEPORT_OID):
                ".1.3.6.1.2.1.17.1.4.1.2.5 = INTEGER: 10",
            ("10.0.0.1", "global", gpt.BRIDGE_MIB_FDB_PORT_OID):
                ".1.3.6.1.2.1.17.4.3.1.2.0.26.43.60.77.94 = INTEGER: 5",
        })
        idx = gpt.build_stage_mac_index(["10.0.0.1"], "global")
        assert idx["10.0.0.1"]["mac_to_ifindex"] == {"00:1a:2b:3c:4d:5e": 10}
        communities = {c for _, c, _ in self.calls}
        assert communities == {"global"}, "should not use community-indexing when vlan_ids empty"

    def test_per_vlan_community_indexing(self):
        # Default context returns nothing (Cisco VLAN 1 stripped); VLAN 11 has data
        self._install_fake_snmpwalk({
            ("10.0.0.1", "global", gpt.IF_ALIAS_OID):
                ".1.3.6.1.2.1.31.1.1.1.18.10 = STRING: team01-01",
            ("10.0.0.1", "global@11", gpt.BRIDGE_MIB_BASEPORT_OID):
                ".1.3.6.1.2.1.17.1.4.1.2.5 = INTEGER: 10",
            ("10.0.0.1", "global@11", gpt.BRIDGE_MIB_FDB_PORT_OID):
                ".1.3.6.1.2.1.17.4.3.1.2.0.26.43.60.77.94 = INTEGER: 5",
        })
        idx = gpt.build_stage_mac_index(["10.0.0.1"], "global", vlan_ids=[11])
        assert idx["10.0.0.1"]["mac_to_ifindex"] == {"00:1a:2b:3c:4d:5e": 10}
        # Both default and VLAN 11 contexts should have been queried
        communities = {c for _, c, _ in self.calls}
        assert "global" in communities
        assert "global@11" in communities

    def test_default_context_wins_over_vlan_context(self):
        # If same MAC appears in both contexts, default context value is kept
        self._install_fake_snmpwalk({
            ("10.0.0.1", "global", gpt.IF_ALIAS_OID):
                ".1.3.6.1.2.1.31.1.1.1.18.10 = STRING: team01-01",
            ("10.0.0.1", "global", gpt.BRIDGE_MIB_BASEPORT_OID):
                ".1.3.6.1.2.1.17.1.4.1.2.5 = INTEGER: 100",
            ("10.0.0.1", "global", gpt.BRIDGE_MIB_FDB_PORT_OID):
                ".1.3.6.1.2.1.17.4.3.1.2.0.26.43.60.77.94 = INTEGER: 5",
            ("10.0.0.1", "global@11", gpt.BRIDGE_MIB_BASEPORT_OID):
                ".1.3.6.1.2.1.17.1.4.1.2.7 = INTEGER: 200",
            ("10.0.0.1", "global@11", gpt.BRIDGE_MIB_FDB_PORT_OID):
                ".1.3.6.1.2.1.17.4.3.1.2.0.26.43.60.77.94 = INTEGER: 7",
        })
        idx = gpt.build_stage_mac_index(["10.0.0.1"], "global", vlan_ids=[11])
        assert idx["10.0.0.1"]["mac_to_ifindex"] == {"00:1a:2b:3c:4d:5e": 100}

    def test_multiple_vlans_merge(self):
        self._install_fake_snmpwalk({
            ("10.0.0.1", "global", gpt.IF_ALIAS_OID):
                ".1.3.6.1.2.1.31.1.1.1.18.10 = STRING: team01-01",
            ("10.0.0.1", "global@11", gpt.BRIDGE_MIB_BASEPORT_OID):
                ".1.3.6.1.2.1.17.1.4.1.2.5 = INTEGER: 10",
            ("10.0.0.1", "global@11", gpt.BRIDGE_MIB_FDB_PORT_OID):
                ".1.3.6.1.2.1.17.4.3.1.2.0.26.43.60.77.94 = INTEGER: 5",
            ("10.0.0.1", "global@12", gpt.BRIDGE_MIB_BASEPORT_OID):
                ".1.3.6.1.2.1.17.1.4.1.2.6 = INTEGER: 11",
            ("10.0.0.1", "global@12", gpt.BRIDGE_MIB_FDB_PORT_OID):
                ".1.3.6.1.2.1.17.4.3.1.2.170.187.204.221.238.255 = INTEGER: 6",
        })
        idx = gpt.build_stage_mac_index(["10.0.0.1"], "global", vlan_ids=[11, 12])
        assert idx["10.0.0.1"]["mac_to_ifindex"] == {
            "00:1a:2b:3c:4d:5e": 10,
            "aa:bb:cc:dd:ee:ff": 11,
        }


# ---- parse_if_oper_status() ---------------------------------------

class TestParseIfOperStatus:
    def test_integer_status(self):
        out = ".1.3.6.1.2.1.2.2.1.8.10 = INTEGER: 1"
        assert gpt.parse_if_oper_status(out) == {10: 1}

    def test_named_status(self):
        out = ".1.3.6.1.2.1.2.2.1.8.10 = INTEGER: up(1)"
        assert gpt.parse_if_oper_status(out) == {10: 1}

    def test_down_status(self):
        out = ".1.3.6.1.2.1.2.2.1.8.11 = INTEGER: 2"
        assert gpt.parse_if_oper_status(out) == {11: 2}

    def test_multiple_entries(self):
        out = "\n".join([
            ".1.3.6.1.2.1.2.2.1.8.10 = INTEGER: 1",
            ".1.3.6.1.2.1.2.2.1.8.11 = INTEGER: 2",
            ".1.3.6.1.2.1.2.2.1.8.12 = INTEGER: 1",
        ])
        assert gpt.parse_if_oper_status(out) == {10: 1, 11: 2, 12: 1}

    def test_empty_input(self):
        assert gpt.parse_if_oper_status("") == {}


# ---- link-up filter on join paths ---------------------------------

class TestRequireLinkUpFilter:
    def _stage_index_with_oper(self, oper_status_for_101):
        return {
            "172.25.10.3": {
                "ifalias": {101: {"team": 14, "seat": 1}},
                "mac_to_ifindex": {"00:1a:2b:3c:4d:5e": 101},
                "oper_status": {101: oper_status_for_101},
            },
        }

    def test_link_up_emits_target(self):
        idx = self._stage_index_with_oper(1)  # up
        arp = {"172.25.11.10": "00:1a:2b:3c:4d:5e"}
        targets, stats = gpt.join_gateway_arp_to_teams(arp, idx, [], require_link_up=True)
        assert len(targets) == 1
        assert stats["skipped_link_down"] == 0
        assert stats["matched"] == 1

    def test_link_down_skipped_when_required(self):
        # The canonical phantom case: port unplugged but MAC and ARP are still
        # cached on the switch / gateway.
        idx = self._stage_index_with_oper(2)  # down
        arp = {"172.25.11.10": "00:1a:2b:3c:4d:5e"}
        targets, stats = gpt.join_gateway_arp_to_teams(arp, idx, [], require_link_up=True)
        assert targets == []
        assert stats["skipped_link_down"] == 1
        assert stats["matched"] == 0
        assert stats["unmatched_macs"] == 0

    def test_link_down_emitted_when_disabled(self):
        # If user opts out, behaviour reverts to pre-filter (emit phantoms too).
        idx = self._stage_index_with_oper(2)
        arp = {"172.25.11.10": "00:1a:2b:3c:4d:5e"}
        targets, stats = gpt.join_gateway_arp_to_teams(arp, idx, [], require_link_up=False)
        assert len(targets) == 1
        assert stats["matched"] == 1
        assert stats["skipped_link_down"] == 0

    def test_missing_oper_status_does_not_skip(self):
        # If ifOperStatus didn't come back at all (old switch, ACL, etc.),
        # don't punish the port — better to over-emit than to silently hide
        # real players.
        idx = {
            "172.25.10.3": {
                "ifalias": {101: {"team": 14, "seat": 1}},
                "mac_to_ifindex": {"00:1a:2b:3c:4d:5e": 101},
                "oper_status": {},
            },
        }
        arp = {"172.25.11.10": "00:1a:2b:3c:4d:5e"}
        targets, stats = gpt.join_gateway_arp_to_teams(arp, idx, [], require_link_up=True)
        assert len(targets) == 1
        assert stats["matched"] == 1
