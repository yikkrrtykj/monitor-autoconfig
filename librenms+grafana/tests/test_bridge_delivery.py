import json
import importlib.util
from pathlib import Path


_spec = importlib.util.spec_from_file_location(
    "feishu_bridge_delivery",
    Path(__file__).resolve().parent.parent / "alertmanager-feishu-bridge.py",
)
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)


def _card():
    return {"card": {"header": {"title": {"content": "test"}}}}


def test_every_outgoing_card_is_prefixed_with_event_name(monkeypatch):
    monkeypatch.setattr(bridge, "EVENT_NAME", "EWC 上海站")
    original = _card()
    decorated = bridge._with_event_name(original)
    assert decorated["card"]["header"]["title"]["content"] == "【EWC 上海站】 test"
    assert original["card"]["header"]["title"]["content"] == "test"
    assert bridge._with_event_name(decorated)["card"]["header"]["title"]["content"] == "【EWC 上海站】 test"


def test_event_scoped_help_teaches_shared_group_commands(monkeypatch):
    monkeypatch.setattr(bridge, "EVENT_NAME", "Singapore")
    monkeypatch.setattr(bridge, "DEVICE_PENDING_DELETE_ENABLED", True)

    result = bridge.handle_bot_query("帮助")

    assert result["ok"] is True
    assert result["text"].startswith("【Singapore】")
    assert "@机器人 Singapore 网络巡检" in result["text"]
    assert "@机器人 Singapore 待删除设备" in result["text"]
    assert "@机器人 Singapore 光功率巡检" in result["text"]
    assert "@机器人 Singapore 上联冗余巡检" in result["text"]


def test_tournament_help_omits_pending_delete_command(monkeypatch):
    monkeypatch.setattr(bridge, "EVENT_NAME", "Singapore")
    monkeypatch.setattr(bridge, "DEVICE_PENDING_DELETE_ENABLED", False)

    result = bridge.handle_bot_query("帮助")

    assert result["ok"] is True
    assert "@机器人 Singapore 网络巡检" in result["text"]
    assert "@机器人 Singapore 光功率巡检" in result["text"]
    assert "待删除设备" not in result["text"]


def test_bridge_delivery_wrappers_decorate_then_delegate(monkeypatch):
    monkeypatch.setattr(bridge, "EVENT_NAME", "EWC 上海站")
    calls = []
    monkeypatch.setattr(bridge._FEISHU_DELIVERY, "send", lambda value: calls.append(("send", value)) or True)
    monkeypatch.setattr(bridge._FEISHU_DELIVERY, "send_app", lambda value: calls.append(("app", value)) or True)
    monkeypatch.setattr(bridge._FEISHU_DELIVERY, "send_webhook", lambda value: calls.append(("webhook", value)) or True)
    original = _card()

    assert bridge.send_feishu(original) is True
    assert bridge.send_feishu_app_card(original) is True
    assert bridge._send_feishu_webhook(original) is True
    assert [name for name, _value in calls] == ["send", "app", "webhook"]
    assert all(
        value["card"]["header"]["title"]["content"] == "【EWC 上海站】 test"
        for _name, value in calls
    )
    assert original["card"]["header"]["title"]["content"] == "test"


def test_bot_network_audit_merges_status_and_offline_details(monkeypatch):
    devices = [
        {"display": "core", "hostname": "192.168.10.254", "status": 1, "disabled": 0},
        {"display": "RTS2", "hostname": "192.168.10.32", "status": 0, "disabled": 0},
        {"display": "retired", "hostname": "192.168.10.99", "status": 0, "disabled": 1},
    ]
    monkeypatch.setattr(bridge, "LIBRENMS_URL", "http://librenms")
    monkeypatch.setattr(bridge, "_librenms_token", lambda: "token")
    monkeypatch.setattr(bridge, "fetch_librenms_devices", lambda _token: devices)
    monkeypatch.setattr(bridge, "build_cisco_stackwise_audit_cards", lambda _devices: [])

    summary = bridge.handle_bot_query("网络巡检")
    assert "在线 1" in summary["text"]
    assert "离线 1" in summary["text"]
    assert "RTS2" in summary["text"]
    assert "retired" not in summary["text"]
    status_body = summary["cards"][0]["card"]["body"]["elements"][0]["content"]
    assert "core" in status_body and "RTS2" in status_body
    assert "retired" not in status_body
    assert status_body.index("RTS2") < status_body.index("core")

    # Legacy status/offline spellings remain aliases but now return the same
    # combined network audit rather than separate single-purpose commands.
    assert bridge.handle_bot_query("离线设备")["text"] == summary["text"]
    assert "设备 <设备名或 IP>" not in bridge.BOT_HELP_TEXT
    assert bridge.handle_bot_query("查设备 RTS2")["text"].startswith("未识别命令")


def _stack_sample(name, target, value, entity="", instance="core"):
    labels = {
        "__name__": name,
        "job": "infra-switch-stackwise",
        "target_ip": target,
        "instance": instance,
    }
    if entity:
        labels["entPhysicalIndex"] = entity
    return {"metric": labels, "value": [1, str(value)]}


def _two_member_stack(state_two=4, ring=1):
    samples = [_stack_sample("cswRingRedundant", "10.0.0.1", ring)]
    for entity, number, role, state in (("1001", 1, 1, 4), ("2001", 2, 4, state_two)):
        samples.extend([
            _stack_sample("cswSwitchNumCurrent", "10.0.0.1", number, entity),
            _stack_sample("cswSwitchRole", "10.0.0.1", role, entity),
            _stack_sample("cswSwitchState", "10.0.0.1", state, entity),
        ])
    return samples


def test_stackwise_audit_skips_standalone_cisco_edge_switch():
    samples = [
        _stack_sample("cswSwitchNumCurrent", "10.0.0.2", 1, "1001", instance="edge-1"),
        _stack_sample("cswSwitchRole", "10.0.0.2", 1, "1001", instance="edge-1"),
        _stack_sample("cswSwitchState", "10.0.0.2", 4, "1001", instance="edge-1"),
    ]
    stacks, baseline = bridge.evaluate_cisco_stackwise_samples(samples)
    assert stacks == []
    assert baseline == {}


def test_stackwise_audit_drops_old_count_when_ip_is_reused_by_new_device():
    samples = [
        _stack_sample("cswSwitchNumCurrent", "10.0.0.2", 1, "1001", instance="new-edge"),
        _stack_sample("cswSwitchRole", "10.0.0.2", 1, "1001", instance="new-edge"),
        _stack_sample("cswSwitchState", "10.0.0.2", 4, "1001", instance="new-edge"),
    ]
    stacks, baseline = bridge.evaluate_cisco_stackwise_samples(
        samples, {"10.0.0.2": {"members": 2, "name": "old-stack"}},
    )
    assert stacks == []
    assert baseline == {}


def test_stackwise_audit_learns_healthy_stack_and_reports_roles():
    stacks, baseline = bridge.evaluate_cisco_stackwise_samples(_two_member_stack())
    assert len(stacks) == 1
    assert stacks[0]["healthy"] is True
    assert baseline["10.0.0.1"]["members"] == 2
    assert [item["role"] for item in stacks[0]["members"]] == [1, 4]


def test_stackwise_audit_reports_version_mismatch_and_broken_ring():
    stacks, _baseline = bridge.evaluate_cisco_stackwise_samples(
        _two_member_stack(state_two=6, ring=2),
    )
    assert stacks[0]["healthy"] is False
    assert any("版本不一致" in issue for issue in stacks[0]["issues"])
    assert any("环不冗余" in issue for issue in stacks[0]["issues"])


def test_stackwise_audit_uses_learned_count_when_member_row_disappears():
    samples = [
        _stack_sample("cswRingRedundant", "10.0.0.1", 2),
        _stack_sample("cswSwitchNumCurrent", "10.0.0.1", 1, "1001"),
        _stack_sample("cswSwitchRole", "10.0.0.1", 1, "1001"),
        _stack_sample("cswSwitchState", "10.0.0.1", 4, "1001"),
    ]
    stacks, baseline = bridge.evaluate_cisco_stackwise_samples(
        samples, {"10.0.0.1": {"members": 2, "name": "core"}},
    )
    assert len(stacks) == 1
    assert any("成员数量由 2 变为 1" in issue for issue in stacks[0]["issues"])
    assert baseline["10.0.0.1"]["members"] == 2


def test_network_audit_attaches_stackwise_card(monkeypatch):
    monkeypatch.setattr(bridge, "LIBRENMS_URL", "http://librenms")
    monkeypatch.setattr(bridge, "_librenms_token", lambda: "token")
    monkeypatch.setattr(bridge, "fetch_librenms_devices", lambda _token: [])
    card = {"card": {"header": {"title": {"content": "网络巡检 · 思科堆叠"}}}}
    monkeypatch.setattr(bridge, "build_cisco_stackwise_audit_cards", lambda _devices: [card])
    result = bridge.handle_bot_query("网络巡检")
    assert len(result["cards"]) == 2
    assert result["cards"][0]["card"]["header"]["title"]["content"].startswith("网络巡检 · 设备状态")
    assert result["cards"][1] == card
    assert "网络巡检" in bridge.BOT_HELP_TEXT and "思科堆叠" in bridge.BOT_HELP_TEXT


def test_bot_full_fiber_audit_returns_summary_and_grouped_details(monkeypatch):
    devices = [
        {"device_id": 1, "display": "RTS1", "hostname": "192.168.10.31", "disabled": 0},
        {"device_id": 2, "display": "RTS2", "hostname": "192.168.10.32", "disabled": 0},
    ]
    readings = {
        1: [
            {"sensor_descr": "Gi1/0/1 Rx Power", "sensor_current": -26.0},
            {"sensor_descr": "Gi1/0/2 Rx Power", "sensor_current": -3.0},
            {"sensor_descr": "Te2/0/3 Receive Power", "sensor_current": -39.9},
        ],
        2: [{"sensor_descr": "Gi1/0/8 Rx Power", "sensor_current": -23.5}],
    }
    monkeypatch.setattr(bridge, "LIBRENMS_URL", "http://librenms")
    monkeypatch.setattr(bridge, "_librenms_token", lambda: "token")
    monkeypatch.setattr(bridge, "fetch_librenms_devices", lambda _token: devices)
    monkeypatch.setattr(
        bridge,
        "fetch_librenms_dbm_sensors",
        lambda _token, device_id: readings[device_id],
    )
    monkeypatch.setattr(
        bridge,
        "fetch_librenms_port_states",
        lambda _token, device_id: (
            [{"ifName": "Te2/0/3", "ifOperStatus": "down"}] if device_id == 1 else []
        ),
    )

    result = bridge.handle_bot_query("光功率巡检")
    assert result["ok"] is True
    assert len(result["cards"]) == 2
    summary = result["cards"][0]["card"]["body"]["elements"][0]["content"]
    details = result["cards"][1]["card"]["body"]["elements"][0]["content"]
    assert "已检查光功率：** 3" in summary
    assert "未接线/无入光：** 1" in summary
    assert "发现异常：** 2" in summary
    assert "严重" in details and "警告" in details
    assert "RTS1" in details and "RTS2" in details
    assert "Te2/0/3" not in details


def test_fiber_audit_ignores_unplugged_dom_floor_and_down_ports():
    assert bridge._fiber_audit_level({"sensor_current": -39.9}) == "inactive"
    assert bridge._fiber_sensor_port_status(
        {"sensor_descr": "Te2/0/3 Receive Power"},
        [{"ifDescr": "TenGigabitEthernet2/0/3", "ifOperStatus": "down"}],
    ) == "down"


def test_uplink_audit_skips_plain_physical_uplinks_without_port_channel():
    edges = [
        {"from_ip": "10.0.0.1", "to_ip": "10.0.0.2", "to_port": "Te1/0/1, Te2/0/1"},
        {"from_ip": "10.0.0.2", "to_ip": "10.0.0.3", "to_port": "Gi1/0/1"},
    ]
    rows = bridge.audit_uplink_redundancy(
        edges,
        {"10.0.0.2": "access-a", "10.0.0.3": "access-b"},
        "10.0.0.1",
    )
    assert rows == []


def test_uplink_audit_reports_configured_port_channel_with_one_active_member():
    edges = [{
        "from_ip": "10.0.0.1", "to_ip": "10.0.0.2",
        "to_port": "Te1/0/1", "to_ifindex": "101",
    }]
    aggregates = [{
        "ip": "10.0.0.2", "port": "Po3", "ifindex": "400", "lag_up": True,
        "members": [
            {"name": "Te1/0/1", "ifindex": "101", "up": True},
            {"name": "Te2/0/1", "ifindex": "201", "up": False},
        ],
    }]
    row = bridge.audit_uplink_redundancy(
        edges, {"10.0.0.2": "access-a"}, "10.0.0.1", aggregates,
    )[0]
    assert row["aggregate"] == "Po3"
    assert row["redundant"] is False
    assert row["active_members"] == ["Te1/0/1"]


def test_uplink_audit_matches_lldp_physical_port_to_port_channel_members():
    edges = [{
        "from_ip": "10.0.0.1", "to_ip": "10.0.0.2",
        "to_port": "GigabitEthernet27", "to_ifindex": "27",
    }]
    aggregates = [{
        "ip": "10.0.0.2", "port": "Po1", "ifindex": "400", "lag_up": True,
        "members": [
            {"name": "Gi27", "ifindex": "27", "up": True},
            {"name": "Gi28", "ifindex": "28", "up": True},
        ],
    }]
    row = bridge.audit_uplink_redundancy(
        edges, {"10.0.0.2": "access-a"}, "10.0.0.1", aggregates,
    )[0]
    assert row["aggregate"] == "Po1"
    assert row["members"] == ["Gi27", "Gi28"]
    assert row["redundant"] is True
    metadata_edges = [{
        **edges[0], "edge_type": "physical", "protocols": ["lldp"],
    }]
    assert bridge.audit_uplink_redundancy(
        metadata_edges, {"10.0.0.2": "access-a"}, "10.0.0.1", aggregates,
    ) == [row]


def test_dbm_query_falls_back_to_device_health_when_global_sensor_page_is_incomplete(monkeypatch):
    def fake_get(_token, path, timeout=15):
        if path.startswith("/api/v0/resources/sensors"):
            return {"sensors": []}
        if path.endswith("/health/device_dbm"):
            return {"graphs": [{"sensor_id": 91, "desc": "Gi1/0/1 Rx Power"}]}
        if path.endswith("/health/device_dbm/91"):
            return {"graphs": [{
                "sensor_id": 91, "device_id": 7, "sensor_class": "dbm",
                "sensor_descr": "Gi1/0/1 Rx Power", "sensor_current": -3.2,
            }]}
        raise AssertionError(path)

    monkeypatch.setattr(bridge, "_librenms_get_json", fake_get)
    sensors = bridge.fetch_librenms_dbm_sensors("token", 7)
    assert len(sensors) == 1
    assert sensors[0]["sensor_current"] == -3.2


def test_port_state_read_uses_shared_librenms_client(monkeypatch):
    calls = []

    class Client:
        @staticmethod
        def get_device_ports(device, columns=None, with_vlans=False):
            calls.append((device, columns, with_vlans))
            return [{"ifName": "Gi1/0/1", "ifOperStatus": "up"}]

    monkeypatch.setattr(bridge, "_librenms_client", lambda _token, timeout=10: Client())

    assert bridge.fetch_librenms_port_states("token", 7) == [
        {"ifName": "Gi1/0/1", "ifOperStatus": "up"},
    ]
    assert calls == [(
        {"device_id": 7},
        "ifName,ifDescr,ifOperStatus,ifAdminStatus",
        False,
    )]


def test_device_list_read_uses_shared_librenms_client(monkeypatch):
    class Client:
        @staticmethod
        def list_devices():
            return [{"device_id": 7, "hostname": "edge"}]

    calls = []

    def client(token, timeout=10):
        calls.append((token, timeout))
        return Client()

    monkeypatch.setattr(bridge, "_librenms_client", client)

    assert bridge.fetch_librenms_devices("token") == [{"device_id": 7, "hostname": "edge"}]
    assert calls == [("token", 10)]


def test_online_dedupe_is_committed_only_after_delivery(monkeypatch, tmp_path):
    state_file = tmp_path / "online.json"
    outcomes = iter([False, True])
    calls = []

    def fake_send(card):
        calls.append(card)
        return next(outcomes)

    monkeypatch.setattr(bridge._ONLINE_IDENTITY, "state_file", str(state_file))
    monkeypatch.setattr(bridge, "send_feishu", fake_send)

    assert bridge.send_device_online_once(_card(), "switch-1", "10.0.0.1") is False
    assert not state_file.exists()
    assert bridge.send_device_online_once(_card(), "switch-1", "10.0.0.1") is True
    assert set(json.loads(state_file.read_text(encoding="utf-8"))) == {"switch-1", "10.0.0.1"}
    # Already delivered: considered satisfied without another HTTP request.
    assert bridge.send_device_online_once(_card(), "switch-1", "10.0.0.1") is True
    assert len(calls) == 2


def test_online_delivery_does_not_hold_state_lock_during_network_io(monkeypatch, tmp_path):
    state_file = tmp_path / "online.json"
    lock_was_free = []

    def fake_send(_card):
        lock_was_free.append(bridge._ONLINE_IDENTITY.known_identities() == set())
        return True

    monkeypatch.setattr(bridge._ONLINE_IDENTITY, "state_file", str(state_file))
    monkeypatch.setattr(bridge, "send_feishu", fake_send)

    assert bridge.send_device_online_once(_card(), "switch-1", "10.0.0.1") is True
    assert lock_was_free == [True]


def test_ap_deployment_retries_until_delivery_is_confirmed(monkeypatch):
    outcomes = iter([False, True])
    calls = []

    def fake_send(card, *identities):
        calls.append((card, identities))
        return next(outcomes)

    monkeypatch.setattr(bridge, "send_device_online_once", fake_send)
    confirmed = {"192.0.2.10"}
    delivered = set()

    assert bridge._send_pending_ap_deployment(
        "AP-1", "192.0.2.10", "U6-LR", confirmed, delivered,
        "aa:bb:cc:dd:ee:ff",
    ) is False
    assert delivered == set()
    assert bridge._send_pending_ap_deployment(
        "AP-1", "192.0.2.10", "U6-LR", confirmed, delivered,
        "AA-BB-CC-DD-EE-FF",
    ) is True
    assert delivered == {"unifi-ap:aabbccddeeff"}
    assert calls[0][1] == ("unifi-ap:aabbccddeeff",)
    assert len(calls) == 2


def test_ap_ip_change_does_not_send_a_second_deployment(monkeypatch, tmp_path):
    state_file = tmp_path / "online.json"
    sent = []

    monkeypatch.setattr(bridge._ONLINE_IDENTITY, "state_file", str(state_file))
    monkeypatch.setattr(bridge, "send_feishu", lambda card: sent.append(card) or True)

    mac = "aa:bb:cc:dd:ee:ff"
    assert bridge._send_pending_ap_deployment(
        "AP-1", "192.0.2.10", "U6-LR", {"192.0.2.10"}, set(), mac,
    ) is True
    # A fresh in-memory set simulates a bridge restart after the AP received a
    # different DHCP address. The persisted MAC must still suppress the card.
    assert bridge._send_pending_ap_deployment(
        "AP-1", "192.0.2.99", "U6-LR", {"192.0.2.99"}, set(), mac,
    ) is True

    assert len(sent) == 1
    assert json.loads(state_file.read_text(encoding="utf-8")) == [
        "unifi-ap:aabbccddeeff"
    ]


def test_librenms_ap_identity_uses_controller_mac(monkeypatch):
    monkeypatch.setattr(
        bridge,
        "_find_unifi_ap_by_ip",
        lambda _ip: {
            "name": "AP-1",
            "ip": "192.0.2.99",
            "model": "U6-LR",
            "mac": "AA-BB-CC-DD-EE-FF",
        },
    )

    enriched = bridge._enrich_device_with_unifi({"hostname": "192.0.2.99"})

    assert enriched["unifi_mac"] == "AA-BB-CC-DD-EE-FF"
    assert bridge._device_online_identity_values(enriched) == (
        "unifi-ap:aabbccddeeff",
    )


def test_librenms_ap_identity_survives_stale_controller_ip(monkeypatch):
    monkeypatch.setattr(bridge, "UNIFI_CONTROLLER_URL", "https://controller")
    monkeypatch.setattr(bridge, "UNIFI_CONTROLLER_USER", "user")
    monkeypatch.setattr(bridge, "UNIFI_CONTROLLER_PASS", "password")
    monkeypatch.setattr(bridge, "_find_unifi_ap_by_ip", lambda _ip: None)
    monkeypatch.setattr(
        bridge,
        "fetch_unifi_controller_aps_cached",
        lambda: {
            "unifi-ap:aabbccddeeff": {
                "name": "OB5",
                "ip": "192.168.39.201",
                "mac": "aa:bb:cc:dd:ee:ff",
            }
        },
    )

    enriched = bridge._enrich_device_with_unifi({
        "hostname": "192.168.39.1",
        "sysName": "OB5",
    })

    assert enriched["unifi_mac"] == "aa:bb:cc:dd:ee:ff"
    assert bridge._device_online_identity_values(enriched) == (
        "unifi-ap:aabbccddeeff",
    )


def test_ap_down_and_recovery_titles_are_distinct():
    down = bridge.build_ap_down_card("AP-1", "192.0.2.10", "U6-LR", False, 10)
    recovered = bridge.build_ap_down_card("AP-1", "192.0.2.10", "U6-LR", True, 15)

    assert down["card"]["header"]["title"]["content"].endswith("🔴 AP 掉线告警")
    assert recovered["card"]["header"]["title"]["content"].endswith("🟢 AP 上线恢复")
    assert "subtitle" not in down["card"]["header"]
    assert "subtitle" not in recovered["card"]["header"]
    assert "🔴 状态：DOWN" in down["card"]["body"]["elements"][0]["content"]
    assert "🟢 状态：UP" in recovered["card"]["body"]["elements"][0]["content"]


def test_fast_ap_down_default():
    assert bridge.UNIFI_AP_DOWN_FOR_SECONDS == 10
    assert bridge.UNIFI_AP_RECOVER_FOR_SECONDS == 10


def test_blackbox_ping_is_ap_reachability_source(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read():
            return b"# TYPE probe_success gauge\nprobe_success 1\n"

    requested = []

    def fake_urlopen(req, timeout):
        requested.append((req.full_url, timeout))
        return Response()

    monkeypatch.setattr(bridge.request, "urlopen", fake_urlopen)

    assert bridge._blackbox_icmp_probe("192.0.2.10") is True
    assert "target=192.0.2.10" in requested[0][0]
    assert "module=icmp" in requested[0][0]


def test_ap_ping_overrides_stale_controller_state():
    known = {
        "ap-up": {"ip": "192.0.2.10"},
        "ap-down": {"ip": "192.0.2.11"},
        "ap-fallback": {"ip": "192.0.2.12"},
    }
    controller_current = {
        "ap-down": known["ap-down"],
        "ap-fallback": known["ap-fallback"],
    }

    current, authoritative = bridge.apply_unifi_ap_ping_reachability(
        known,
        controller_current,
        {
            "192.0.2.10": True,
            "192.0.2.11": False,
            "192.0.2.12": None,
        },
    )

    assert set(current) == {"ap-up", "ap-fallback"}
    assert authoritative == 2


def test_fresh_controller_heartbeat_protects_ap_from_stale_ip_ping():
    identity = "unifi-ap:aabbccddeeff"
    known = {
        identity: {
            "ip": "192.0.2.10",
            "source": "controller",
            "last_seen": 1_000,
        },
    }

    current, observed = bridge.apply_unifi_ap_ping_reachability(
        known,
        dict(known),
        {"192.0.2.10": False},
        now=1_025,
    )

    assert set(current) == {identity}
    assert observed == 1


def test_stale_controller_heartbeat_returns_authority_to_ping():
    identity = "unifi-ap:aabbccddeeff"
    known = {
        identity: {
            "ip": "192.0.2.10",
            "source": "controller",
            "last_seen": 1_000,
        },
    }

    current, observed = bridge.apply_unifi_ap_ping_reachability(
        known,
        dict(known),
        {"192.0.2.10": False},
        now=1_031,
    )

    assert current == {}
    assert observed == 1


def test_controller_declared_offline_is_not_protected_by_old_heartbeat():
    identity = "unifi-ap:aabbccddeeff"
    known = {
        identity: {
            "ip": "192.0.2.10",
            "source": "controller",
            "last_seen": 1_000,
        },
    }

    current, observed = bridge.apply_unifi_ap_ping_reachability(
        known,
        {},
        {"192.0.2.10": False},
        previously_seen={identity},
        now=1_005,
    )

    assert current == {}
    assert observed == 1


def test_unifi_controller_heartbeat_accepts_millisecond_epoch():
    assert bridge._unifi_epoch_seconds(1_700_000_000_000) == 1_700_000_000
    assert bridge._unifi_controller_heartbeat_fresh(
        {
            "source": "controller",
            "last_seen": 1_700_000_000_000,
        },
        now=1_700_000_030,
        max_age=30,
    ) is True


def test_ap_ip_change_keeps_mac_identity_and_migrates_librenms(monkeypatch):
    identity = "unifi-ap:aabbccddeeff"
    inventory = {
        identity: {
            "mac": "aabbccddeeff",
            "name": "AP-1",
            "ip": "192.0.2.10",
            "model": "U6-LR",
            "librenms_ip": "192.0.2.10",
        }
    }
    known = {
        identity: {
            "mac": "aa:bb:cc:dd:ee:ff",
            "name": "AP-1",
            "ip": "192.0.2.99",
            "model": "U6-LR",
        }
    }
    migrations = []
    monkeypatch.setattr(
        bridge,
        "rename_librenms_device",
        lambda old, new, name, log_prefix: migrations.append((old, new, name)) or True,
    )

    changed, migrated = bridge.reconcile_unifi_ap_inventory(
        known, inventory, {}, now=1000,
    )

    assert changed is True
    assert migrated == {"192.0.2.99"}
    assert migrations == [("192.0.2.10", "192.0.2.99", "AP-1")]
    assert inventory[identity]["ip"] == "192.0.2.99"
    assert inventory[identity]["librenms_ip"] == "192.0.2.99"


def test_unifi_controller_state_overrides_stale_prometheus_uptime():
    metric = {"state": "1"}
    metric_online = {"aa:bb:cc:dd:ee:ff": True}
    controller = {"online": False}

    assert bridge._resolve_ap_online(
        "aa:bb:cc:dd:ee:ff", metric, metric_online, controller,
    ) is False


def test_unifi_metrics_are_used_without_controller_state():
    metric = {"state": "0"}

    assert bridge._resolve_ap_online("ap-1", metric, {}, None) is False
    assert bridge._resolve_ap_online("ap-1", metric, {"ap-1": True}, None) is True


def test_unifi_alert_state_survives_bridge_restart(monkeypatch, tmp_path):
    state_file = tmp_path / "unifi-ap-alerts.json"
    monkeypatch.setattr(bridge, "UNIFI_AP_STATE_FILE", str(state_file))
    states = {
        "aa:bb:cc:dd:ee:ff": {
            "alerting": True,
            "down_since": 1234.0,
            "seen_up": True,
            "last_seen": 1234.0,
            "name": "AP-1",
            "ip": "192.0.2.10",
            "model": "U6-LR",
        },
        "online-ap": {
            "alerting": False,
            "name": "AP-2",
            "ip": "192.0.2.11",
        },
    }

    bridge.save_unifi_ap_states(states)
    loaded = bridge.load_unifi_ap_states()

    state_key = "unifi-ap:aabbccddeeff"
    assert set(loaded) == {state_key}
    assert loaded[state_key]["alerting"] is True
    assert loaded[state_key]["down_since"] == 1234.0
    assert loaded[state_key]["name"] == "AP-1"
    assert loaded[state_key]["ip"] == "192.0.2.10"
    assert loaded[state_key]["mac"] == "aabbccddeeff"


def test_ap_watcher_restores_active_outages_instead_of_isp_watcher():
    import inspect

    ap_source = inspect.getsource(bridge.unifi_ap_watcher)
    isp_source = inspect.getsource(bridge.isp_bandwidth_watcher)

    assert "states = load_unifi_ap_states()" in ap_source
    assert "load_unifi_ap_states()" not in isp_source


def test_new_lifecycle_online_card_bypasses_lifetime_dedupe(monkeypatch, tmp_path):
    state_file = tmp_path / "online.json"
    state_file.write_text(json.dumps(["switch-1", "10.0.0.1"]), encoding="utf-8")
    calls = []

    monkeypatch.setattr(bridge._ONLINE_IDENTITY, "state_file", str(state_file))
    monkeypatch.setattr(bridge, "send_feishu", lambda card: calls.append(card) or True)

    assert bridge.send_device_online_new_lifecycle(_card(), "switch-1", "10.0.0.1") is True
    assert len(calls) == 1
    assert set(json.loads(state_file.read_text(encoding="utf-8"))) == {"switch-1", "10.0.0.1"}


def test_librenms_webhook_returns_502_when_feishu_fails(monkeypatch):
    handler = object.__new__(bridge.Handler)
    handler._read_json = lambda: {"name": "test rule", "state": 1}
    handler._send = lambda status, body=b"OK", content_type="text/plain": (status, body)
    monkeypatch.setattr(bridge, "send_feishu", lambda card: False)

    status, body = handler._handle_librenms()
    assert status == 502
    assert b"failed" in body


def test_bridge_health_reports_missing_token_and_dead_watcher(monkeypatch):
    class DeadThread:
        @staticmethod
        def is_alive():
            return False

    monkeypatch.setattr(bridge, "TOKEN", "")
    monkeypatch.setattr(bridge, "DRY_RUN", False)
    monkeypatch.setattr(bridge, "WATCHER_THREADS", {"device-down": DeadThread()})
    monkeypatch.setattr(bridge, "WATCHER_HEALTH", {"device-down": {"lastError": "boom"}})

    health = bridge.bridge_health_payload()

    assert health["ready"] is False
    assert health["tokenConfigured"] is False
    assert health["deadWatchers"] == ["device-down"]


def test_company_retire_card_has_buttons_and_plain_console_fallback(monkeypatch):
    monkeypatch.setattr(bridge, "DEVICE_PENDING_DELETE_ENABLED", True)
    monkeypatch.setattr(bridge.time, "time", lambda: 100.0 + 48 * 60 * 60 + 90)
    monkeypatch.setattr(bridge, "SERVER_IP", "192.168.16.20")
    monkeypatch.setattr(bridge, "BIGSCREEN_PORT", "8088")
    state = {
        "name": "access-7", "ip": "192.168.10.27", "job": "infra-dist-ping",
        "down_since": 100.0, "pending_token": "tok-9",
    }
    notification = bridge.build_retire_confirm_card(
        state,
        "infra-dist-ping|192.168.10.27",
        True,
    )
    elements = notification["card"]["body"]["elements"]
    buttons = [e for e in elements if e.get("tag") == "button"]
    assert [button["text"]["content"] for button in buttons] == ["确认删除", "保留"]
    assert {
        button["behaviors"][0]["value"]["action"] for button in buttons
    } == {"retire_delete", "retire_keep"}
    assert all(
        button["behaviors"][0]["value"]["token"] == "tok-9"
        for button in buttons
    )
    assert notification["card"]["header"]["title"]["content"].endswith("⚠️ 设备持续离线｜需要确认")
    assert notification["card"]["header"]["template"] == "orange"
    assert "subtitle" not in notification["card"]["header"]
    body = notification["card"]["body"]["elements"][0]["content"]
    assert "⚠️ 设备已连续离线 48 小时，已进入待退役确认。" in body
    assert "💻 设备：access-7" in body
    assert "🌐 IP：192.168.10.27" in body
    assert "🔴 状态：连续离线 48 小时 1 分" in body
    assert "🕒 时间：" in body
    assert "设备已离线满 48 小时，等待人工处理。" in body
    assert "请进入对应监控控制台确认删除或保留：" in body
    assert "http://192.168.16.20:8088/control" in body
    plain = bridge.build_retire_confirm_card(
        state,
        "infra-dist-ping|192.168.10.27",
        False,
    )
    assert not [
        element for element in plain["card"]["body"]["elements"]
        if element.get("tag") == "button"
    ]
    assert "tok-9" not in json.dumps(plain, ensure_ascii=False)
    assert "callback" not in json.dumps(plain, ensure_ascii=False)


def test_company_pending_notification_prefers_interactive_app_card(monkeypatch):
    monkeypatch.setattr(bridge, "DEVICE_PENDING_DELETE_ENABLED", True)
    monkeypatch.setattr(bridge, "feishu_app_configured", lambda: True)
    monkeypatch.setattr(bridge, "next_event_title", lambda: "#company")
    key = "infra-dist-ping|192.168.10.27"
    states = {key: {
        "name": "access-7",
        "ip": "192.168.10.27",
        "job": "infra-dist-ping",
        "down_since": 100.0,
        "pending_delete": True,
        "pending_token": "tok-company",
        "pending_notified": False,
        "pending_last_notified": None,
    }}
    app_cards = []
    monkeypatch.setattr(
        bridge,
        "send_feishu_app_card",
        lambda card: app_cards.append(card) or True,
    )
    monkeypatch.setattr(
        bridge,
        "send_feishu",
        lambda _card: (_ for _ in ()).throw(AssertionError("unexpected fallback")),
    )

    assert bridge.notify_pending_delete_states(states, 1000.0) is True
    assert states[key]["pending_notified"] is True
    assert len(app_cards) == 1
    buttons = [
        element for element in app_cards[0]["card"]["body"]["elements"]
        if element.get("tag") == "button"
    ]
    assert [button["text"]["content"] for button in buttons] == ["确认删除", "保留"]
    assert all(
        button["behaviors"][0]["value"]["token"] == "tok-company"
        for button in buttons
    )


def test_company_pending_notification_falls_back_to_plain_webhook(monkeypatch):
    monkeypatch.setattr(bridge, "DEVICE_PENDING_DELETE_ENABLED", True)
    monkeypatch.setattr(bridge, "feishu_app_configured", lambda: True)
    monkeypatch.setattr(bridge, "next_event_title", lambda: "#fallback")
    key = "infra-dist-ping|192.168.10.27"
    states = {key: {
        "name": "access-7",
        "ip": "192.168.10.27",
        "job": "infra-dist-ping",
        "down_since": 100.0,
        "pending_delete": True,
        "pending_token": "tok-fallback",
        "pending_notified": False,
    }}
    monkeypatch.setattr(bridge, "send_feishu_app_card", lambda _card: False)
    webhook_cards = []
    monkeypatch.setattr(
        bridge,
        "_send_feishu_webhook",
        lambda card: webhook_cards.append(card) or True,
    )

    assert bridge.notify_pending_delete_states(states, 1000.0) is True
    assert len(webhook_cards) == 1
    serialized = json.dumps(webhook_cards[0], ensure_ascii=False)
    assert "tok-fallback" not in serialized
    assert "callback" not in serialized
    assert not [
        element for element in webhook_cards[0]["card"]["body"]["elements"]
        if element.get("tag") == "button"
    ]


def test_pending_delete_notify_uses_normal_delivery_without_interaction(monkeypatch):
    monkeypatch.setattr(bridge, "DEVICE_PENDING_DELETE_ENABLED", True)
    monkeypatch.setattr(bridge, "feishu_app_configured", lambda: False)
    key = "infra-dist-ping|192.168.10.27"
    states = {key: {
        "name": "access-7", "ip": "192.168.10.27", "job": "infra-dist-ping",
        "down_since": 100.0, "pending_delete": True, "pending_token": "t",
        "pending_notified": False, "pending_last_notified": None,
    }}
    monkeypatch.setattr(bridge, "EVENT_NAME", "Singapore")
    delivery_calls = []
    monkeypatch.setattr(bridge, "send_feishu", lambda card: delivery_calls.append(card) or True)
    event_titles = []
    monkeypatch.setattr(bridge, "next_event_title", lambda: event_titles.append("#77") or "#77")

    changed = bridge.notify_pending_delete_states(states, 1000.0)
    assert changed is True
    assert states[key]["pending_notified"] is True
    assert states[key]["pending_last_notified"] == 1000.0
    assert len(delivery_calls) == 1
    assert event_titles == ["#77"]
    assert delivery_calls[0]["card"]["header"]["title"]["content"].startswith("#77 ")
    assert not [
        element
        for element in delivery_calls[0]["card"]["body"]["elements"]
        if element.get("tag") == "button"
    ]

    # 已告知后不会重复刷屏，也没有第二条交互卡重试路径。
    monkeypatch.setattr(bridge, "DEVICE_PENDING_DELETE_REALERT_SECONDS", 0)
    delivery_calls.clear()
    assert bridge.notify_pending_delete_states(states, 1030.0) is False
    assert not delivery_calls


def test_pending_delete_notify_not_committed_when_all_sends_fail(monkeypatch):
    monkeypatch.setattr(bridge, "DEVICE_PENDING_DELETE_ENABLED", True)
    monkeypatch.setattr(bridge, "feishu_app_configured", lambda: False)
    key = "infra-dist-ping|192.168.10.27"
    states = {key: {
        "name": "access-7", "ip": "192.168.10.27", "job": "infra-dist-ping",
        "down_since": 100.0, "pending_delete": True, "pending_token": "t",
        "pending_notified": False, "pending_last_notified": None,
    }}
    monkeypatch.setattr(bridge, "send_feishu", lambda card: False)
    # 发送失败时不置 notified，下轮还会重试
    assert bridge.notify_pending_delete_states(states, 1000.0) is False
    assert states[key]["pending_notified"] is False
