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
