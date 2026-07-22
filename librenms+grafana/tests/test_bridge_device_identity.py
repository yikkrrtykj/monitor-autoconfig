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


def test_device_display_skips_ip_placeholder_and_uses_sysname():
    device = {
        "display": "192.168.10.18",
        "sysName": "Broadcast_WS-C2960X-24TS-L",
        "hostname": "192.168.10.18",
        "ip": "192.168.10.18",
    }
    assert bridge._device_display(device) == "Broadcast_WS-C2960X-24TS-L"


def test_device_display_prefers_current_discovered_name():
    device = {
        "display": "192.168.10.254",
        "sysName": "192.168.10.254",
        "hostname": "192.168.10.254",
        "ip": "192.168.10.254",
    }
    assert bridge._device_display(
        device, {"192.168.10.254": "Global_SW3850-12XS_STACK"}
    ) == "Global_SW3850-12XS_STACK"


def test_network_status_uses_ping_for_names_and_reachability():
    devices = [
        {"display": "192.168.10.18", "hostname": "192.168.10.18", "status": 0},
        {"display": "192.168.200.88", "hostname": "192.168.200.88", "status": 0},
        {"display": "192.168.10.11", "hostname": "192.168.10.11", "status": 1},
    ]
    observations = bridge.parse_network_reachability_samples([
        {
            "metric": {
                "target_ip": "192.168.10.18",
                "display_name": "Broadcast_WS-C2960X-24TS-L",
            },
            "value": [1, "1"],
        },
        {
            "metric": {"target_ip": "192.168.200.88", "display_name": "old-server"},
            "value": [1, "0"],
        },
        {
            "metric": {"target_ip": "192.168.10.11", "display_name": "Global-new-stack"},
            "value": [1, "1"],
        },
    ])

    cards = bridge.build_network_device_status_cards(devices, observations=observations)
    text = json.dumps(cards, ensure_ascii=False)
    assert "网络可达：**2 台**" in text
    assert "网络离线：**1 台**" in text
    assert "Broadcast_WS-C2960X-24TS-L" in text
    assert "old-server" in text
    assert '🟢 **Broadcast_WS-C2960X-24TS-L**' in text


def test_librenms_display_update_resolves_ip_to_device_id(monkeypatch):
    monkeypatch.setattr(bridge, "fetch_librenms_devices", lambda token: [
        {"device_id": 42, "hostname": "ap-tech-room", "ip": "192.168.200.204"},
    ])
    assert bridge._librenms_device_ref_for_ip("token", "192.168.200.204") == 42
    assert bridge._librenms_device_ref_for_ip("token", "192.168.200.207") == "192.168.200.207"


def test_librenms_device_delete_uses_resolved_device_id(monkeypatch):
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        @staticmethod
        def read():
            return json.dumps([{"status": "ok"}]).encode("utf-8")

    def fake_urlopen(req, timeout):
        requests.append((req.get_method(), req.full_url, req.data, timeout))
        return Response()

    monkeypatch.setattr(bridge, "LIBRENMS_URL", "http://librenms")
    monkeypatch.setattr(bridge, "_librenms_token", lambda: "token")
    monkeypatch.setattr(bridge, "_find_librenms_device_by_ip", lambda token, ip: {"device_id": 42})
    monkeypatch.setattr(bridge.request, "urlopen", fake_urlopen)

    assert bridge.delete_librenms_device("192.168.10.27") == "deleted"
    assert requests == [("DELETE", "http://librenms/api/v0/devices/42", None, 10)]


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
