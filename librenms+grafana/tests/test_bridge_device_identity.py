import importlib.util
import json
import pytest
import threading
from concurrent.futures import ThreadPoolExecutor
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
    assert card["card"]["header"]["title"]["content"] == "#1 🔵 新设备部署"
    assert "subtitle" not in card["card"]["header"]


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


@pytest.fixture
def ap_identity_env(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "UNIFI_AP_INVENTORY_FILE", str(tmp_path / "aps.json"))
    monkeypatch.setattr(bridge._ONLINE_IDENTITY, "state_file", str(tmp_path / "online.json"))
    monkeypatch.setattr(bridge, "fetch_unifi_controller_aps_cached", lambda: {})
    monkeypatch.setattr(bridge, "next_event_title", lambda: "#test")
    sent = []
    monkeypatch.setattr(bridge, "send_feishu", lambda card: sent.append(card) or True)

    def forbidden(*args, **kwargs):
        pytest.fail("Identity hotfix must not call network/delete/pending mutation")

    for name in ("delete_librenms_device", "delete_librenms_device_record",
                 "_manual_delete_exact_id", "save_device_down_states",
                 "mark_pending_delete_states", "run_device_auto_delete_cycle"):
        monkeypatch.setattr(bridge, name, forbidden)
    monkeypatch.setattr(bridge.request, "urlopen", forbidden)
    return sent


def _ap(mac="aabbccddeeff", ip="192.0.2.10", name="AP-1"):
    return {"mac": mac, "ip": ip, "librenms_ip": ip, "name": name, "model": "test AP"}


def _write_inventory(*aps):
    Path(bridge.UNIFI_AP_INVENTORY_FILE).write_text(json.dumps({
        "unifi-ap:" + ap["mac"]: ap for ap in aps
    }), encoding="utf-8")


def test_inventory_fallback_and_legacy_migration(ap_identity_env):
    _write_inventory(_ap())
    bridge.mark_device_online_notified("192.0.2.10")
    dev = bridge._enrich_device_with_unifi({"hostname": "192.0.2.10"})
    assert bridge._device_online_identity_values(dev) == ("unifi-ap:aabbccddeeff",)
    assert bridge._migrate_unifi_device_online_identity(dev)
    assert bridge.send_device_online_once({}, *bridge._device_online_identity_values(dev))
    assert ap_identity_env == []
    assert "unifi-ap:aabbccddeeff" in bridge._ONLINE_IDENTITY.known_identities()


def test_ambiguous_ap_name_defers_without_ip_fallback(ap_identity_env):
    _write_inventory(_ap(name="same"), _ap("112233445566", "192.0.2.11", "same"))
    dev = bridge._enrich_device_with_unifi({"display": "same", "ip": "192.0.2.99", "unifi_ap": True})
    assert bridge._device_online_identity_values(dev) == ()
    assert not bridge.send_device_online_once({}, *bridge._device_online_identity_values(dev))
    assert ap_identity_env == []


def test_ap_without_mac_never_sends_deployment(ap_identity_env):
    assert not bridge._send_pending_ap_deployment(
        "AP-1", "192.0.2.10", "test", {"192.0.2.10"}, set(),
    )
    assert ap_identity_env == []


def test_reused_ip_does_not_inherit_legacy_owner(ap_identity_env, monkeypatch):
    _write_inventory(_ap())
    bridge.mark_device_online_notified("192.0.2.10", "unifi-ap:aabbccddeeff")
    monkeypatch.setattr(bridge, "fetch_unifi_controller_aps_cached", lambda: {
        "unifi-ap:112233445566": _ap("112233445566"),
    })
    dev = bridge._enrich_device_with_unifi({"hostname": "192.0.2.10"})
    assert bridge._device_online_identity_values(dev) == ("unifi-ap:112233445566",)
    assert not bridge._migrate_unifi_device_online_identity(dev)
    assert bridge.send_device_online_once({}, *bridge._device_online_identity_values(dev))
    assert len(ap_identity_env) == 1


@pytest.mark.parametrize("field", ["ip", "librenms_ip"])
def test_inventory_exact_address_beats_duplicate_names(ap_identity_env, field):
    a, b = _ap(name="same"), _ap("112233445566", "192.0.2.11", "same")
    b[field] = "192.0.2.99"
    _write_inventory(a, b)
    dev = bridge._enrich_device_with_unifi({"ip": "192.0.2.99", "display": "same"})
    assert bridge._device_online_identity_values(dev) == ("unifi-ap:112233445566",)


def test_direct_mac_has_priority_and_invalid_mac_defers(ap_identity_env):
    _write_inventory(_ap())
    dev = bridge._enrich_device_with_unifi({"ip": "192.0.2.10", "unifi_mac": "11:22:33:44:55:66"})
    assert bridge._device_online_identity_values(dev) == ("unifi-ap:112233445566",)
    dev = bridge._enrich_device_with_unifi({"ip": "192.0.2.99", "unifi_mac": "invalid"})
    assert bridge._device_online_identity_values(dev) == ()


def test_controller_and_inventory_name_conflict_defers(ap_identity_env, monkeypatch):
    _write_inventory(_ap(name="same"))
    monkeypatch.setattr(bridge, "fetch_unifi_controller_aps_cached", lambda: {
        "unifi-ap:112233445566": _ap("112233445566", "192.0.2.11", "same"),
    })
    dev = bridge._enrich_device_with_unifi({"ip": "192.0.2.99", "display": "same", "unifi_ap": True})
    assert bridge._device_online_identity_values(dev) == ()


@pytest.mark.parametrize("source", ["inventory", "controller"])
def test_generic_switch_with_ap_name_keeps_its_own_identity(ap_identity_env, monkeypatch, source):
    ap = _ap(name="shared-name")
    if source == "inventory":
        _write_inventory(ap)
    else:
        monkeypatch.setattr(bridge, "fetch_unifi_controller_aps_cached", lambda: {
            "unifi-ap:aabbccddeeff": ap,
        })
    dev = bridge._enrich_device_with_unifi({
        "ip": "192.0.2.99", "hostname": "192.0.2.99",
        "display": "shared-name", "os": "ios", "hardware": "test switch",
    })
    assert dev["unifi_identity_status"] == "not-an-AP"
    assert bridge._device_online_identity_values(dev) == ("192.0.2.99",)
    assert bridge.send_device_online_once({}, *bridge._device_online_identity_values(dev))
    assert len(ap_identity_env) == 1
    assert bridge._ONLINE_IDENTITY.known_identities() == {"192.0.2.99"}


def test_live_controller_invalidates_stale_inventory_old_ip(ap_identity_env, monkeypatch):
    _write_inventory(_ap(ip="192.0.2.10"))
    monkeypatch.setattr(bridge, "fetch_unifi_controller_aps_cached", lambda: {
        "unifi-ap:aabbccddeeff": _ap(ip="192.0.2.99"),
    })
    dev = bridge._enrich_device_with_unifi({
        "ip": "192.0.2.10", "hostname": "192.0.2.10",
        "display": "replacement", "os": "ios", "hardware": "test switch",
    })
    assert dev["unifi_identity_status"] == "not-an-AP"
    assert bridge._device_online_identity_values(dev) == ("192.0.2.10",)
    assert bridge.send_device_online_once({}, *bridge._device_online_identity_values(dev))
    assert len(ap_identity_env) == 1
    assert bridge._ONLINE_IDENTITY.known_identities() == {"192.0.2.10"}


def test_first_mac_rename_ip_restart_and_replacement(ap_identity_env, monkeypatch):
    for mac, ip, name in [
        ("aabbccddeeff", "192.0.2.10", "old"),
        ("aabbccddeeff", "192.0.2.11", "new"),
        ("112233445566", "192.0.2.11", "new"),
    ]:
        # Recreate the service as a fresh process would, retaining only its file.
        old = bridge._ONLINE_IDENTITY
        monkeypatch.setattr(bridge, "_ONLINE_IDENTITY", bridge.OnlineIdentityService(
            state_file=old.state_file, load_set=old.load_set, save_set=old.save_set,
            send=lambda card: bridge.send_feishu(card),
        ))
        assert bridge._send_pending_ap_deployment(name, ip, "test", {ip}, set(), mac)
    assert len(ap_identity_env) == 2
    assert bridge._ONLINE_IDENTITY.known_identities() == {
        "unifi-ap:aabbccddeeff", "unifi-ap:112233445566",
    }


def test_frozen_legacy_alias_survives_inventory_update_and_restart(ap_identity_env):
    _write_inventory(_ap())
    bridge.mark_device_online_notified("192.0.2.10")
    inventory = bridge.load_unifi_ap_inventory()
    inventory["unifi-ap:aabbccddeeff"].update(ip="192.0.2.99", librenms_ip="192.0.2.99", name="renamed")
    assert bridge.save_unifi_ap_inventory(inventory)
    dev = bridge._enrich_device_with_unifi({"ip": "192.0.2.99"})
    assert bridge._migrate_unifi_device_online_identity(dev)
    assert bridge.send_device_online_once({}, *bridge._device_online_identity_values(dev))
    assert ap_identity_env == []


def test_migration_write_failure_defers_delivery(ap_identity_env, monkeypatch):
    _write_inventory(_ap())
    bridge.mark_device_online_notified("192.0.2.10")
    monkeypatch.setattr(bridge._ONLINE_IDENTITY, "save_set", lambda *args: False)
    dev = bridge._enrich_device_with_unifi({"ip": "192.0.2.10"})
    assert not bridge._migrate_unifi_device_online_identity(dev)
    assert bridge._device_online_identity_values(dev) == ()
    assert not bridge._send_pending_ap_deployment("AP-1", "192.0.2.10", "test", {"192.0.2.10"}, set(), "aabbccddeeff")
    assert ap_identity_env == []
    assert bridge._ONLINE_IDENTITY.known_identities() == {"192.0.2.10"}


@pytest.mark.parametrize("ap_first", [True, False])
def test_two_watcher_delivery_race_uses_one_reservation(ap_identity_env, monkeypatch, ap_first):
    _write_inventory(_ap())
    entered, release = threading.Event(), threading.Event()

    def send(card):
        ap_identity_env.append(card)
        entered.set()
        assert release.wait(5)
        return True

    monkeypatch.setattr(bridge, "send_feishu", send)

    def ap_path():
        return bridge._send_pending_ap_deployment("AP-1", "192.0.2.10", "test", {"192.0.2.10"}, set(), "aabbccddeeff")

    def generic_path():
        dev = bridge._enrich_device_with_unifi({"ip": "192.0.2.10"})
        bridge._migrate_unifi_device_online_identity(dev)
        return bridge.send_device_online_once({}, *bridge._device_online_identity_values(dev))

    first, second = (ap_path, generic_path) if ap_first else (generic_path, ap_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        task = pool.submit(first)
        try:
            assert entered.wait(5)
            assert pool.submit(second).result(timeout=5) is False
        finally:
            release.set()
        assert task.result(timeout=5)
    assert second()
    assert len(ap_identity_env) == 1


def _run_device_polls(monkeypatch, polls):
    class Finished(BaseException):
        pass

    samples = iter(polls)

    def fetch(token):
        try:
            return next(samples)
        except StopIteration:
            raise Finished()

    monkeypatch.setattr(bridge, "_librenms_token", lambda: "test")
    monkeypatch.setattr(bridge, "fetch_librenms_devices", fetch)
    monkeypatch.setattr(bridge, "_enrich_device_with_inventory", lambda device, token: device)
    monkeypatch.setattr(bridge.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(bridge, "DEVICE_MODEL_WAIT_SECONDS", 0)
    with pytest.raises(Finished):
        bridge.device_watcher()


def test_generic_watcher_inventory_retry_and_switch_regression(ap_identity_env, monkeypatch):
    _write_inventory(_ap(name="same"), _ap("112233445566", "192.0.2.11", "same"))
    bridge.mark_device_online_notified("unrelated-existing")
    _run_device_polls(monkeypatch, [
        [{"ip": "192.0.2.99", "display": "same"}],
        [{"ip": "192.0.2.10", "display": "same"}, {"ip": "192.0.2.11", "display": "same"}],
        [{"ip": "192.0.2.12", "display": "Cisco switch", "hardware": "test switch"}],
        [{"ip": "192.0.2.10", "display": "same"}, {"ip": "192.0.2.12", "display": "Cisco switch"}],
    ])
    assert len(ap_identity_env) == 4
    assert bridge._ONLINE_IDENTITY.known_identities() == {
        "unrelated-existing", "unifi-ap:aabbccddeeff", "unifi-ap:112233445566",
        "192.0.2.99", "192.0.2.12",
    }


def test_ping_and_reenrollment_share_ap_mac_but_switch_keeps_new_lifecycle(ap_identity_env):
    _write_inventory(_ap())
    assert bridge.send_device_online_once({}, "AP-1", "192.0.2.10")
    assert bridge.send_device_online_new_lifecycle({}, "AP-1", "192.0.2.10")
    assert len(ap_identity_env) == 1
    assert bridge.send_device_online_once({}, "Cisco", "192.0.2.20")
    assert bridge.send_device_online_new_lifecycle({}, "Cisco", "192.0.2.20")
    assert len(ap_identity_env) == 3


def test_legacy_name_migrates_only_for_unique_owner(ap_identity_env):
    _write_inventory(_ap())
    bridge.mark_device_online_notified("AP-1")
    assert bridge.send_device_online_new_lifecycle({}, "AP-1", "192.0.2.10")
    assert ap_identity_env == []
    _write_inventory(_ap(name="same"), _ap("112233445566", "192.0.2.11", "same"))
    bridge.mark_device_online_notified("same")
    assert bridge.send_device_online_new_lifecycle({}, "same", "192.0.2.99")
    assert len(ap_identity_env) == 1
    assert "unifi-ap:112233445566" not in bridge._ONLINE_IDENTITY.known_identities()


def test_inventory_key_without_mac_field_is_canonical(ap_identity_env):
    ap = _ap()
    del ap["mac"]
    Path(bridge.UNIFI_AP_INVENTORY_FILE).write_text(json.dumps({"unifi-ap:aabbccddeeff": ap}), encoding="utf-8")
    dev = bridge._enrich_device_with_unifi({"ip": "192.0.2.10"})
    assert bridge._device_online_identity_values(dev) == ("unifi-ap:aabbccddeeff",)


def test_controller_failure_uses_persisted_inventory(ap_identity_env, monkeypatch):
    _write_inventory(_ap())
    bridge.mark_device_online_notified("unifi-ap:aabbccddeeff")
    monkeypatch.setattr(bridge, "_unifi_controller_enabled", lambda: True)
    monkeypatch.setattr(bridge, "UNIFI_CONTROLLER_AP_CACHE", {"ts": 0, "items": {}})

    def fail():
        raise TimeoutError("synthetic controller timeout")

    # The fixture replaces the public cache call; restore its real function to
    # exercise the controller exception and empty-cache fallback.
    real_cache = ap_identity_env_cache_function
    monkeypatch.setattr(bridge, "fetch_unifi_controller_aps_cached", real_cache)
    monkeypatch.setattr(bridge, "_fetch_unifi_controller_aps", fail)
    dev = bridge._enrich_device_with_unifi({"ip": "192.0.2.10"})
    assert bridge._device_online_identity_values(dev) == ("unifi-ap:aabbccddeeff",)
    assert bridge.send_device_online_once({}, *bridge._device_online_identity_values(dev))
    assert ap_identity_env == []


ap_identity_env_cache_function = bridge.fetch_unifi_controller_aps_cached
