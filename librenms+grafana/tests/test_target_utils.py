import json

import target_utils as targets


def test_expand_ipv4_targets_supports_names_ranges_and_cidr():
    assert targets.expand_ipv4_targets(
        "core:192.168.10.254,edge:192.168.10.31-32,192.168.10.8/30"
    ) == [
        "192.168.10.254",
        "192.168.10.31",
        "192.168.10.32",
        "192.168.10.9",
        "192.168.10.10",
    ]


def test_real_display_name_wins_over_ip_placeholder():
    assert targets.merge_display_names(
        {"192.168.10.254": "192.168.10.254"},
        {"192.168.10.254": "Global_SW3850-12XS_STACK"},
    ) == {"192.168.10.254": "Global_SW3850-12XS_STACK"}
    assert targets.merge_display_names(
        {"192.168.10.254": "Global_SW3850-12XS_STACK"},
        {"192.168.10.254": "192.168.10.254"},
    ) == {"192.168.10.254": "Global_SW3850-12XS_STACK"}


def test_atomic_file_sd_round_trip(tmp_path):
    path = tmp_path / "targets.json"
    payload = targets.build_file_sd({"192.168.10.254": "core"})
    targets.write_json_atomic(str(path), payload)
    assert json.loads(path.read_text(encoding="utf-8")) == payload
    assert targets.load_file_sd_targets(str(path)) == {"192.168.10.254": "core"}


def test_shared_snmp_value_normalizers():
    assert targets.normalize_mac("STRING: 0:1a:2b:3c:4d:5e") == "00:1a:2b:3c:4d:5e"
    assert targets.normalize_mac("001a2b3c4d5e") == "00:1a:2b:3c:4d:5e"
    assert targets.parse_if_oper_status(
        ".1.3.6.1.2.1.2.2.1.8.10 = INTEGER: up(1)\n"
        ".1.3.6.1.2.1.2.2.1.8.11 = INTEGER: 2"
    ) == {10: 1, 11: 2}


def test_ipv4_validator_rejects_out_of_range_octets():
    assert targets.is_ipv4("192.168.10.254") is True
    assert targets.is_ipv4("999.168.10.254") is False
