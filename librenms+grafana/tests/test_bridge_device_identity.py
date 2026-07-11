import importlib.util
import json
from pathlib import Path


# alertmanager-feishu-bridge.py has a hyphen, so load it by file path just like
# test_bridge_recovery.py does. conftest.py intentionally does not export it.
_spec = importlib.util.spec_from_file_location(
    "feishu_bridge_device_identity",
    Path(__file__).resolve().parent.parent / "alertmanager-feishu-bridge.py",
)
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)


def test_inventory_chassis_model_replaces_generic_stack_platform():
    inventory = [
        {"entPhysicalClass": "module", "entPhysicalModelName": "C3KX-PWR-350WAC"},
        {"entPhysicalClass": "chassis", "entPhysicalModelName": "WS-C2960X-24TS-L"},
        {"entPhysicalClass": "port", "entPhysicalModelName": "SFP-10G-SR"},
    ]
    assert bridge._inventory_device_model(inventory) == "WS-C2960X-24TS-L"
    assert bridge._best_device_model({
        "hardware": "C29xx Stacking",
        "inventory_model": bridge._inventory_device_model(inventory),
    }) == "WS-C2960X-24TS-L"


def test_generic_stack_platform_is_not_reported_as_exact_model():
    assert bridge._clean_device_model("C29xx Stacking") == ""


def test_librenms_display_update_resolves_ip_to_device_id(monkeypatch):
    monkeypatch.setattr(bridge, "fetch_librenms_devices", lambda token: [
        {"device_id": 42, "hostname": "ap-tech-room", "ip": "192.168.200.204"},
    ])
    assert bridge._librenms_device_ref_for_ip("token", "192.168.200.204") == 42
    assert bridge._librenms_device_ref_for_ip("token", "192.168.200.207") == "192.168.200.207"


def test_already_exists_is_rejected_when_api_has_no_matching_device(monkeypatch):
    monkeypatch.setattr(bridge, "fetch_librenms_devices", lambda token: [])
    assert bridge._confirm_librenms_device_exists(
        "token", "192.168.200.204", "device may already exist", "[TEST]"
    ) is False


def test_already_exists_is_confirmed_by_matching_device(monkeypatch):
    monkeypatch.setattr(bridge, "fetch_librenms_devices", lambda token: [
        {"device_id": 42, "hostname": "192.168.200.204", "ip": "192.168.200.204"},
    ])
    assert bridge._confirm_librenms_device_exists(
        "token", "192.168.200.204", "device already exists", "[TEST]"
    ) is True


def test_new_device_card_always_contains_model_line(monkeypatch):
    monkeypatch.setattr(bridge, "next_event_title", lambda: "#1")
    card = bridge.build_device_online_card({"display": "rts1", "ip": "192.168.10.31"})
    text = json.dumps(card, ensure_ascii=False)
    assert "型号：暂未识别" in text


def test_new_device_card_prefers_inventory_model(monkeypatch):
    monkeypatch.setattr(bridge, "next_event_title", lambda: "#2")
    card = bridge.build_device_online_card({
        "display": "falak-studio5",
        "ip": "192.168.10.81",
        "hardware": "C29xx Stacking",
        "inventory_model": "WS-C2960X-24TS-L",
    })
    text = json.dumps(card, ensure_ascii=False)
    assert "型号：WS-C2960X-24TS-L" in text
    assert "C29xx Stacking" not in text
