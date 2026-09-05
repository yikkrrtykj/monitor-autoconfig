import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "isp-bandwidth-resolver.py"
spec = importlib.util.spec_from_file_location("isp_bandwidth_resolver", MODULE_PATH)
resolver = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(resolver)


def port(port_id, *labels, ips=()):
    return {"port_id": port_id, "labels": list(labels), "ips": list(ips)}


def decisions(payload):
    result = resolver.resolve_port_bandwidth(payload)
    return {item["port_id"]: item for item in result["decisions"]}, result["warnings"]


def test_exact_label_match_never_uses_substring_or_interface_position():
    mapped, warnings = decisions({
        "bandwidth": "*:1000,ISP-A:200",
        "ports": [port(20, "ISP-A-backup"), port(10, "ISP-A")],
    })

    assert mapped[10] == {
        "port_id": 10, "mbps": 200.0, "source": "named", "identity": "ISP-A",
    }
    assert mapped[20]["source"] == "global"
    assert warnings == []


def test_ifindex_and_input_reorder_do_not_move_named_bandwidth():
    payload = {
        "bandwidth": "*:1000,ISP-A:200",
        "ports": [
            {**port(10, "ISP-A"), "ifindex": 2},
            {**port(20, "ISP-B"), "ifindex": 1},
        ],
    }
    first, _warnings = decisions(payload)
    second, _warnings = decisions({
        **payload,
        "ports": [
            {**port(20, "ISP-B"), "ifindex": 99},
            {**port(10, "ISP-A"), "ifindex": 77},
        ],
    })

    assert first[10]["source"] == second[10]["source"] == "named"
    assert first[10]["mbps"] == second[10]["mbps"] == 200.0
    assert first[20]["source"] == second[20]["source"] == "global"


def test_exact_public_ip_can_bind_a_generic_interface_name():
    mapped, warnings = decisions({
        "bandwidth": "*:1000,Carrier A:300/100",
        "manual_ips": "Carrier A:203.0.113.2",
        "ports": [port(7, "ethernet0/4", ips=("203.0.113.2",))],
    })

    assert mapped[7]["mbps"] == 300.0
    assert mapped[7]["source"] == "named"
    assert warnings == []


def test_conflicting_ip_and_label_evidence_falls_back_to_global():
    mapped, warnings = decisions({
        "bandwidth": "*:1000,Carrier A:200",
        "manual_ips": "Carrier A:203.0.113.2",
        "ports": [
            port(1, "Carrier A", ips=("198.51.100.2",)),
            port(2, "ethernet0/4", ips=("203.0.113.2",)),
        ],
    })

    assert all(item["source"] == "global" for item in mapped.values())
    assert any("conflicting IP and label evidence" in warning for warning in warnings)


def test_duplicate_labels_are_ambiguous_and_use_global_default():
    mapped, warnings = decisions({
        "bandwidth": "*:1000,Carrier A:200",
        "ports": [port(1, "Carrier A"), port(2, "Carrier A")],
    })

    assert all(item["source"] == "global" for item in mapped.values())
    assert any("ambiguous identity evidence" in warning for warning in warnings)


def test_duplicate_manual_ip_is_fail_safe_and_row_order_independent():
    base = {
        "bandwidth": "*:1000,Carrier A:200,Carrier B:300",
        "ports": [
            port(1, "Carrier A", ips=("203.0.113.2",)),
            port(2, "Carrier B", ips=("198.51.100.2",)),
        ],
    }
    for manual_ips in (
        "Carrier A:203.0.113.2,Carrier B:203.0.113.2",
        "Carrier B:203.0.113.2,Carrier A:203.0.113.2",
    ):
        mapped, warnings = decisions({**base, "manual_ips": manual_ips})
        assert all(item["source"] == "global" for item in mapped.values())
        assert sum("duplicate manual WAN IP" in warning for warning in warnings) == 2


def test_duplicate_manual_name_is_fail_safe_and_row_order_independent():
    base = {
        "bandwidth": "*:1000,Carrier A:200",
        "ports": [port(1, "Carrier A", ips=("203.0.113.2",))],
    }
    for manual_ips in (
        "Carrier A:203.0.113.2,Carrier A:198.51.100.2",
        "Carrier A:198.51.100.2,Carrier A:203.0.113.2",
    ):
        mapped, warnings = decisions({**base, "manual_ips": manual_ips})
        assert mapped[1]["source"] == "global"
        assert any("duplicate manual identity" in warning for warning in warnings)


def test_two_named_rows_cannot_claim_the_same_interface():
    mapped, warnings = decisions({
        "bandwidth": "*:1000,Carrier A:200,Carrier B:300",
        "ports": [port(1, "Carrier A", "Carrier B")],
    })

    assert mapped[1]["source"] == "global"
    assert sum("ambiguous identity evidence" in warning for warning in warnings) == 2


def test_no_global_fallback_keeps_unmatched_existing_speed():
    mapped, warnings = decisions({
        "bandwidth": "missing:200",
        "ports": [port(1, "Carrier A")],
    })

    assert mapped == {}
    assert any("named override skipped" in warning for warning in warnings)
    assert any("existing speed kept" in warning for warning in warnings)
