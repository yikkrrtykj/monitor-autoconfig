import importlib.util
from pathlib import Path

from feishu_bridge import device_model


_spec = importlib.util.spec_from_file_location(
    "feishu_bridge_device_model_wrappers",
    Path(__file__).resolve().parent.parent / "alertmanager-feishu-bridge.py",
)
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)


def test_clean_device_model_uses_fixed_generic_and_concrete_expectations():
    for value in (
        None, "", "   ", "C29xx Stacking", "cisco ios", "GENERIC",
        "unknown", "N/A", "n/a", "none", "not available", "0x1a", "zeroDotZero", "123",
    ):
        assert device_model.clean_device_model(value) == ""

    assert device_model.clean_device_model("  WS-C2960X-24TS-L\n") == "WS-C2960X-24TS-L"
    assert device_model.clean_device_model("UniFi   U6-LR") == "UniFi U6-LR"


def test_inventory_device_model_prefers_chassis_deduplicates_and_preserves_order():
    inventory = [
        {"entPhysicalClass": "module", "entPhysicalContainedIn": "0", "entPhysicalModelName": "C3KX-PWR-350WAC"},
        {"entPhysicalClass": "chassis", "entPhysicalModelName": "WS-C2960X-24TS-L"},
        {"entPhysicalClass": "3", "entPhysicalModelName": "WS-C3850-12XS"},
        {"entPhysicalClass": "chassis", "entPhysicalModelName": "WS-C2960X-24TS-L"},
        {"entPhysicalClass": "port", "entPhysicalModelName": "SFP-10G-SR"},
    ]
    assert device_model.inventory_device_model(inventory) == "WS-C2960X-24TS-L / WS-C3850-12XS"


def test_inventory_device_model_falls_back_to_root_rows_and_ignores_invalid_values():
    inventory = [
        None,
        "invalid",
        {"entPhysicalClass": "chassis", "entPhysicalModelName": "Generic"},
        {"entPhysicalClass": "module", "entPhysicalContainedIn": "8", "entPhysicalModelName": "nested"},
        {"entPhysicalClass": "module", "entPhysicalContainedIn": "0", "entPhysicalModelName": "root-a"},
        {"entPhysicalClass": "module", "entPhysicalContainedIn": "", "entPhysicalModelName": "root-b"},
    ]
    assert device_model.inventory_device_model(inventory) == "root-a / root-b"
    assert device_model.inventory_device_model(None) == ""


def test_best_device_model_preserves_field_precedence_and_generic_fallback():
    assert device_model.best_device_model({
        "inventory_model": "WS-C2960X-24TS-L",
        "hardware": "hardware-value",
        "model": "model-value",
    }) == "WS-C2960X-24TS-L"
    assert device_model.best_device_model({
        "inventory_model": "unknown",
        "hardware": "C29xx Stacking",
        "model": "U6-LR",
    }) == "U6-LR"
    assert device_model.best_device_model({}) == ""


def test_bridge_wrappers_match_module_and_share_the_generic_regex_object():
    assert bridge._GENERIC_DEVICE_MODEL_RE is device_model.GENERIC_DEVICE_MODEL_RE

    clean_values = [None, "C29xx Stacking", "  WS-C2960X-24TS-L  ", "0x2a"]
    for value in clean_values:
        assert bridge._clean_device_model(value) == device_model.clean_device_model(value)

    inventory = [
        {"entPhysicalClass": "module", "entPhysicalModelName": "C3KX-PWR-350WAC"},
        {"entPhysicalClass": "chassis", "entPhysicalModelName": "WS-C2960X-24TS-L"},
    ]
    assert bridge._inventory_device_model(inventory) == device_model.inventory_device_model(inventory)

    devices = [
        {"inventory_model": "WS-C2960X-24TS-L", "hardware": "generic"},
        {"inventory_model": "unknown", "hardware": "U6-LR"},
        {},
    ]
    for device in devices:
        assert bridge._best_device_model(device) == device_model.best_device_model(device)
