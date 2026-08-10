import importlib.util
import json
from pathlib import Path

# alertmanager-feishu-bridge.py is hyphenated; load it by path. Importing only
# defines functions (the server starts under __main__), so this is side-effect free.
_spec = importlib.util.spec_from_file_location(
    "feishu_bridge",
    Path(__file__).resolve().parent.parent / "alertmanager-feishu-bridge.py",
)
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)


def test_recovery_waits_for_sustained_up():
    state = {"up_since": None}
    # First UP sample at t=100 starts the stable-up window.
    assert bridge.recovery_ready(state, now=100, sample_ts=100, recover_stable=10) is False
    assert state["up_since"] == 100
    # Still UP but only 5s in -> not yet recovered.
    assert bridge.recovery_ready(state, now=105, sample_ts=105, recover_stable=10) is False
    # 10s of continuous UP -> recovery is due.
    assert bridge.recovery_ready(state, now=110, sample_ts=110, recover_stable=10) is True


def test_recovery_immediate_when_stable_seconds_zero():
    # Legacy behaviour: recover on the first UP sample.
    state = {"up_since": None}
    assert bridge.recovery_ready(state, now=100, sample_ts=100, recover_stable=0) is True


def test_temporary_device_retires_at_48_hour_boundary(monkeypatch):
    monkeypatch.setattr(bridge, "DEVICE_REENROLL_AFTER_SECONDS", 48 * 60 * 60)
    monkeypatch.setattr(bridge, "DEVICE_REENROLL_JOBS", "infra-dist-ping")
    state = {"alerting": True, "down_since": 100}

    assert bridge.device_retirement_due(state, "infra-dist-ping", 100 + 48 * 60 * 60 - 1) is False
    assert bridge.device_retirement_due(state, "infra-dist-ping", 100 + 48 * 60 * 60) is True
    assert bridge.device_retirement_due(state, "infra-core-ping", 100 + 72 * 60 * 60) is False


def test_root_cause_suppressed_device_is_not_asked_for_deletion(monkeypatch):
    # 根因抑制的下游"受害者"（alerting=False）可能只是上游断了——绝不进入
    # 待删除流程，避免误删仍在使用的设备
    monkeypatch.setattr(bridge, "DEVICE_REENROLL_AFTER_SECONDS", 48 * 60 * 60)
    monkeypatch.setattr(bridge, "DEVICE_REENROLL_JOBS", "infra-dist-ping")
    state = {
        "alerting": False,
        "seen_up": True,
        "down_since": 100,
        "job": "infra-dist-ping",
    }

    assert bridge.device_retirement_due(state, "infra-dist-ping", 100 + 48 * 60 * 60) is False


def test_48_hour_outage_marks_pending_delete_instead_of_deleting(monkeypatch):
    monkeypatch.setattr(bridge, "DEVICE_REENROLL_AFTER_SECONDS", 48 * 60 * 60)
    monkeypatch.setattr(bridge, "DEVICE_REENROLL_JOBS", "infra-dist-ping")
    key = "infra-dist-ping|192.168.10.27"
    states = {key: {
        "alerting": True,
        "down_since": 100,
        "seen_up": True,
        "job": "infra-dist-ping",
    }}

    assert bridge.mark_pending_delete_states(states, 100 + 48 * 60 * 60) == [key]
    # 只标记 + 生成确认口令；告警状态原样保留，不删任何东西
    assert states[key]["pending_delete"] is True
    assert states[key]["pending_token"]
    assert states[key]["alerting"] is True
    assert states[key]["down_since"] == 100
    # 已标记的不会重复标记
    assert bridge.mark_pending_delete_states(states, 100 + 49 * 60 * 60) == []


def test_resolve_pending_delete_confirm_keep_and_bad_token(monkeypatch):
    monkeypatch.setattr(bridge, "DEVICE_REENROLL_AFTER_SECONDS", 48 * 60 * 60)
    key = "infra-dist-ping|192.168.10.27"

    def fresh_state():
        bridge.DEVICE_DOWN_STATES.clear()
        bridge.DEVICE_DOWN_STATES[key] = {
            "alerting": True,
            "down_since": 100,
            "seen_up": True,
            "pending_delete": True,
            "pending_token": "tok-1",
            "name": "access-7",
            "ip": "192.168.10.27",
            "job": "infra-dist-ping",
        }
        return bridge.DEVICE_DOWN_STATES[key]

    monkeypatch.setattr(bridge, "save_device_down_states", lambda states: None)
    monkeypatch.setattr(bridge, "_target_currently_up", lambda job, ip: False)
    deleted = []
    monkeypatch.setattr(bridge, "delete_librenms_device", lambda ip: deleted.append(ip) or "deleted")

    # 错口令拒绝，不删
    state = fresh_state()
    result = bridge.resolve_pending_delete(key, "delete", "wrong")
    assert result["ok"] is False and not deleted

    # keep：清标记、48 小时内不再询问、不删
    state = fresh_state()
    result = bridge.resolve_pending_delete(key, "keep", "tok-1")
    assert result["ok"] is True
    assert state["pending_delete"] is False
    assert state["pending_snoozed_until"] > 0
    assert not deleted

    # delete：真正删除并转入"已退役"生命周期
    state = fresh_state()
    result = bridge.resolve_pending_delete(key, "delete", "tok-1")
    assert result["ok"] is True
    assert deleted == ["192.168.10.27"]
    assert state["retired"] is True and state["librenms_deleted"] is True
    assert state["alerting"] is False and state["pending_delete"] is False

    # 设备当前在线：拒绝删除并撤销待删除
    state = fresh_state()
    monkeypatch.setattr(bridge, "_target_currently_up", lambda job, ip: True)
    result = bridge.resolve_pending_delete(key, "delete", "tok-1")
    assert result["ok"] is False
    assert state["pending_delete"] is False
    assert state.get("retired") is not True


def test_bot_pending_delete_command_returns_interactive_cards(monkeypatch):
    key = "infra-dist-ping|192.168.10.81"
    bridge.DEVICE_DOWN_STATES.clear()
    bridge.DEVICE_DOWN_STATES[key] = {
        "pending_delete": True,
        "pending_since": 200,
        "pending_token": "token-81",
        "down_since": 100,
        "name": "falak-studio5",
        "ip": "192.168.10.81",
        "job": "infra-dist-ping",
    }
    result = bridge.handle_bot_query("待删除设备")
    assert result["ok"] is True
    assert len(result["cards"]) == 1
    elements = result["cards"][0]["card"]["body"]["elements"]
    buttons = [item for item in elements if item.get("tag") == "button"]
    assert [item["text"]["content"] for item in buttons] == ["确认删除", "保留设备"]
    assert buttons[0]["behaviors"][0]["value"]["token"] == "token-81"
    bridge.DEVICE_DOWN_STATES.clear()


def test_reenrolled_device_sends_new_online_card_and_clears_old_outage(monkeypatch):
    state = {
        "alerting": False,
        "retired": True,
        "retired_at": 100,
        "down_since": None,
        "up_since": None,
        "seen_up": True,
        "online_sent": False,
        "online_pending": False,
    }
    cards = []
    monkeypatch.setattr(
        bridge,
        "send_device_online_new_lifecycle",
        lambda card, *identity: cards.append((card, identity)) or True,
    )
    assert bridge.notify_device_reenrolled(state, "access-7", "192.168.10.27") is True
    assert len(cards) == 1
    assert cards[0][1] == ("access-7", "192.168.10.27")
    assert state["alerting"] is False
    assert state["retired"] is False
    assert state["down_since"] is None
    assert state["online_sent"] is True


def test_reenroll_waits_for_online_card_delivery(monkeypatch):
    state = {"alerting": False, "retired": True, "retired_at": 100, "down_since": None, "seen_up": True}
    monkeypatch.setattr(bridge, "send_device_online_new_lifecycle", lambda card, *identity: False)
    assert bridge.notify_device_reenrolled(state, "access-7", "192.168.10.27") is False
    assert state["retired"] is True
    assert state["retired_at"] == 100


def test_returned_device_reenrolls_even_without_prior_librenms_delete(monkeypatch):
    # 确认制下删除只发生在人工确认里；设备自己回来时 LibreNMS 记录还在，
    # re-add 返回 exists 直接复用——不再卡在"必须先删除成功"的悬空状态
    monkeypatch.setattr(bridge, "DEVICE_LIBRENMS_SYNC_RETRY_SECONDS", 60)
    monkeypatch.setattr(
        bridge,
        "add_librenms_snmp_device",
        lambda ip, name, log_prefix: "exists",
    )
    state = {
        "retired": True,
        "librenms_deleted": False,
        "librenms_readded": False,
        "librenms_sync_last_attempt": 0,
    }
    assert bridge.prepare_reenrolled_librenms_device(state, "access-7", "192.168.10.27", 100) is True
    assert state["librenms_readded"] is True


def test_reenrolled_device_is_readded_with_bounded_retry(monkeypatch):
    monkeypatch.setattr(bridge, "DEVICE_LIBRENMS_SYNC_RETRY_SECONDS", 60)
    outcomes = iter(["", "added"])
    calls = []
    monkeypatch.setattr(
        bridge,
        "add_librenms_snmp_device",
        lambda ip, name, log_prefix: calls.append((ip, name, log_prefix)) or next(outcomes),
    )
    state = {
        "retired": True,
        "librenms_deleted": True,
        "librenms_readded": False,
        "librenms_sync_last_attempt": 0,
    }

    assert bridge.prepare_reenrolled_librenms_device(state, "access-7", "192.168.10.27", 100) is False
    assert bridge.prepare_reenrolled_librenms_device(state, "access-7", "192.168.10.27", 159) is False
    assert bridge.prepare_reenrolled_librenms_device(state, "access-7", "192.168.10.27", 160) is True
    assert state["librenms_readded"] is True
    assert calls == [
        ("192.168.10.27", "access-7", "[DOWN]"),
        ("192.168.10.27", "access-7", "[DOWN]"),
    ]


def test_reenroll_age_survives_bridge_restart(monkeypatch, tmp_path):
    state_file = tmp_path / "device-down.json"
    monkeypatch.setattr(bridge, "DEVICE_DOWN_STATE_FILE", str(state_file))
    monkeypatch.setattr(bridge, "DEVICE_REENROLL_AFTER_SECONDS", 48 * 60 * 60)
    monkeypatch.setattr(bridge, "DEVICE_REENROLL_JOBS", "infra-dist-ping")
    key = "infra-dist-ping|192.168.10.27"

    bridge.save_device_down_states({key: {
        "alerting": True,
        "down_since": 100,
        "seen_up": True,
        "name": "access-7",
        "ip": "192.168.10.27",
        "job": "infra-dist-ping",
    }})
    restored = bridge.load_device_down_states()[key]

    assert restored["down_since"] == 100
    # 48h 到点只标记待删除；标记连同口令要在桥接重启后幸存
    assert bridge.mark_pending_delete_states({key: restored}, 100 + 48 * 60 * 60) == [key]
    bridge.save_device_down_states({key: restored})
    pending_after_restart = bridge.load_device_down_states()[key]
    assert pending_after_restart["pending_delete"] is True
    assert pending_after_restart["pending_token"] == restored["pending_token"]
    assert pending_after_restart["alerting"] is True
    assert pending_after_restart["down_since"] == 100


def test_flap_restarts_the_stable_window():
    state = {"up_since": None}
    # UP at t=100, not yet stable.
    assert bridge.recovery_ready(state, now=100, sample_ts=100, recover_stable=10) is False
    # A dip clears up_since (this is what the watcher's down-branch does).
    state["up_since"] = None
    # UP again at t=108 -> window restarts from 108, so at t=115 (7s) still not stable.
    assert bridge.recovery_ready(state, now=115, sample_ts=108, recover_stable=10) is False
    assert state["up_since"] == 108
    # Only once it has been continuously UP for the full 10s (t>=118) does it recover.
    assert bridge.recovery_ready(state, now=118, sample_ts=118, recover_stable=10) is True


def test_classify_interconnect_distinguishes_degraded_from_down():
    # All members up -> nothing to report.
    assert bridge.classify_interconnect(True, [True, True]) == "healthy"
    # Aggregate protocol/oper state down is a real failure even if every
    # physical member still has carrier (for example LACP negotiation failed).
    assert bridge.classify_interconnect(False, [True, True]) == "down"
    # One member down while the bundle is still up -> the alertable case.
    assert bridge.classify_interconnect(True, [True, False]) == "degraded"
    # Every member down -> bundle down and must be alerted directly.
    assert bridge.classify_interconnect(False, [False, False]) == "down"
    # A single-member/down bundle is commonly an intentional dormant Cisco
    # channel-group. It has no redundancy to lose and must not alert on restart.
    assert bridge.classify_interconnect(False, [False]) == "unknown"
    # No member visibility (no ifStackTable) -> nothing to say.
    assert bridge.classify_interconnect(True, []) == "unknown"
    assert bridge.classify_interconnect(False, []) == "unknown"


def test_interconnect_fetch_skips_admin_down_bundle(monkeypatch):
    oper = [{
        "metric": {
            "job": "infra-switch-ifmib", "target_ip": "10.0.0.1",
            "ifIndex": "10", "ifName": "Po1",
        },
        "value": [0, "2"],
    }]
    admin = [{
        "metric": {
            "job": "infra-switch-ifmib", "target_ip": "10.0.0.1",
            "ifIndex": "10", "ifName": "Po1",
        },
        "value": [0, "2"],
    }]

    def query(expr):
        if expr.startswith("ifOperStatus"):
            return oper
        if expr.startswith("ifAdminStatus"):
            return admin
        if expr.startswith("ifStackStatus"):
            return []
        raise AssertionError(expr)

    monkeypatch.setattr(bridge, "prometheus_query", query)

    assert bridge.fetch_interconnect_ports("infra-switch-ifmib") == []


def test_interconnect_card_names_the_down_physical_port_and_peer(monkeypatch):
    monkeypatch.setattr(bridge, "next_event_title", lambda: "#1")
    event = {
        "device": "douyucarnival-core", "ip": "192.168.10.254",
        "alias": "to-stage4", "port": "Po4", "peer_switch": "douyucarnival-stage4",
        "down_members": ["Gi1/0/4"], "up_members": ["Gi1/0/5"], "duration": 6,
    }
    card = bridge.build_interconnect_card(event, recovered=False)
    text = json.dumps(card, ensure_ascii=False)
    assert "Gi1/0/4" in text                   # the actual down physical port
    assert "douyucarnival-stage4" in text      # the real peer switch (from LLDP)
    assert "对端交换机" in text
    assert "链路聚合告警" in text              # framed as a LAG event, not a full outage
    assert "剩 Gi1/0/5 在线" in text           # status shows the surviving leg
    assert card["card"]["header"]["template"] == "orange"
    assert card["card"]["header"]["subtitle"]["content"] == "链路聚合告警"
    assert "状态：冗余降低" in card["card"]["body"]["elements"][0]["content"]


def test_peer_switch_resolves_from_lldp_by_down_member_port():
    edges = [
        {"from_ip": "192.168.10.254", "from_port": "Gi1/0/4", "to_sysname": "douyucarnival-stage4"},
        {"from_ip": "192.168.10.254", "from_port": "Gi1/0/9", "to_sysname": "other"},
    ]
    peer_map = bridge.build_peer_map(edges)
    # The down member's LLDP neighbor is the peer switch.
    assert bridge.resolve_peer_switch(peer_map, "192.168.10.254", ["Gi1/0/4", "Po4"]) == "douyucarnival-stage4"
    # Unknown ports -> "" (card falls back to the alias).
    assert bridge.resolve_peer_switch(peer_map, "192.168.10.254", ["Po9"]) == ""


def test_peer_switch_map_is_bidirectional_when_remote_port_is_known():
    edges = [{
        "from_ip": "10.0.0.1", "from_sysname": "core", "from_port": "Gi1/0/1",
        "to_ip": "10.0.0.2", "to_sysname": "stage", "to_port": "Gi1/0/48",
    }]
    peer_map = bridge.build_peer_map(edges)
    assert bridge.resolve_peer_switch(peer_map, "10.0.0.1", ["Gi1/0/1"]) == "stage"
    assert bridge.resolve_peer_switch(peer_map, "10.0.0.2", ["Gi1/0/48"]) == "core"


def test_peer_switch_uses_c1000_member_array_and_rejects_tied_conflicts():
    edges = [{
        "from_ip": "192.168.10.11", "from_sysname": "new-stack",
        "from_port": "Te1/0/2", "from_member_ports": ["Te1/0/2", "Te2/0/2"],
        "from_aggregate_port": "Po11",
        "to_ip": "192.168.10.254", "to_sysname": "core", "to_port": "Te1/0/1",
    }]
    peer_map = bridge.build_peer_map(edges)
    assert bridge.resolve_peer_switch(
        peer_map, "192.168.10.11", ["TenGigabitEthernet2/0/2", "Po11"],
    ) == "core"

    conflicting = bridge.build_peer_map([
        {"from_ip": "10.0.0.1", "from_port": "Gi1/0/1", "to_sysname": "peer-a"},
        {"from_ip": "10.0.0.1", "from_port": "Gi1/0/2", "to_sysname": "peer-b"},
    ])
    assert bridge.resolve_peer_switch(
        conflicting, "10.0.0.1", ["Gi1/0/1", "Gi1/0/2"],
    ) == ""


def test_interconnect_card_describes_protocol_down_without_fake_member(monkeypatch):
    monkeypatch.setattr(bridge, "next_event_title", lambda: "#1")
    card = bridge.build_interconnect_card({
        "device": "core", "ip": "10.0.0.1", "port": "Po1", "peer_switch": "stage",
        "down_members": [], "up_members": ["Gi1/0/1", "Gi1/0/2"], "status": "down", "duration": 8,
    })
    text = json.dumps(card, ensure_ascii=False)
    assert "聚合链路 DOWN" in text
    assert "Po1" in text
    assert card["card"]["header"]["template"] == "red"
    assert card["card"]["header"]["subtitle"]["content"] == "链路聚合告警"


def test_interconnect_recovery_uses_green_header_without_severity_emoji(monkeypatch):
    monkeypatch.setattr(bridge, "next_event_title", lambda: "#1")
    card = bridge.build_interconnect_card({
        "device": "core", "ip": "10.0.0.1", "port": "Po1",
        "peer_switch": "stage", "down_members": ["Gi1/0/1"],
        "up_members": ["Gi1/0/1", "Gi1/0/2"], "duration": 18,
    }, recovered=True)

    assert card["card"]["header"]["template"] == "green"
    assert card["card"]["header"]["subtitle"]["content"] == "链路聚合恢复"
    assert "状态：链路冗余已恢复" in card["card"]["body"]["elements"][0]["content"]


def test_interconnect_card_does_not_claim_alias_is_a_peer_switch(monkeypatch):
    monkeypatch.setattr(bridge, "next_event_title", lambda: "#1")
    card = bridge.build_interconnect_card({
        "device": "new-stack", "ip": "192.168.10.11", "port": "Po16",
        "alias": "old-stage-name", "peer_switch": "", "down_members": ["Te1/0/1"],
        "up_members": [], "status": "down", "duration": 8,
    })
    text = json.dumps(card, ensure_ascii=False)
    assert "对端交换机：未确认（接口描述：old-stage-name）" in text
    assert "对端交换机：old-stage-name" not in text


def _chain_edges():
    # 监控 -> 核心 -> 汇聚A -> 接入1 / 接入2
    return [
        {"from_ip": "10.0.0.1", "to_ip": "10.0.0.2"},   # core <-> distA
        {"from_ip": "10.0.0.2", "to_ip": "10.0.0.11"},  # distA <-> access1
        {"from_ip": "10.0.0.2", "to_ip": "10.0.0.12"},  # distA <-> access2
    ]


def test_build_topology_parents_roots_at_core():
    parents = bridge.build_topology_parents(_chain_edges(), root_ip="10.0.0.1")
    assert parents == {"10.0.0.2": "10.0.0.1", "10.0.0.11": "10.0.0.2", "10.0.0.12": "10.0.0.2"}
    # Unknown core -> empty map (fail open: everything is treated as a root cause).
    assert bridge.build_topology_parents(_chain_edges(), root_ip="") == {}


def test_root_cause_vs_symptom_when_middle_switch_fails():
    parents = bridge.build_topology_parents(_chain_edges(), root_ip="10.0.0.1")
    # distA down takes its two access switches with it.
    unreachable = {"10.0.0.2", "10.0.0.11", "10.0.0.12"}
    # distA's uplink (core) is fine -> distA is the root cause, alert it.
    assert bridge.is_down_symptom("10.0.0.2", parents, unreachable) is False
    # The access switches sit below a down device -> symptoms, suppress.
    assert bridge.is_down_symptom("10.0.0.11", parents, unreachable) is True
    assert bridge.is_down_symptom("10.0.0.12", parents, unreachable) is True
    # The root card can report how many downstream devices are also down.
    assert bridge.count_down_descendants("10.0.0.2", parents, unreachable) == 2


def test_unknown_parent_is_never_suppressed():
    parents = bridge.build_topology_parents(_chain_edges(), root_ip="10.0.0.1")
    # A device with no mapped parent (not in the LLDP tree) always alerts.
    assert bridge.is_down_symptom("10.9.9.9", parents, {"10.0.0.2"}) is False


def test_root_cause_card_folds_in_downstream_count(monkeypatch):
    monkeypatch.setattr(bridge, "next_event_title", lambda: "#1")
    card = bridge.build_device_down_card("汇聚A", "10.0.0.2", recovered=False, offline_seconds=12, downstream=2)
    text = json.dumps(card, ensure_ascii=False)
    assert "下游 2 台" in text
    # A lone outage (no downstream) shows no fold-in line.
    plain = json.dumps(bridge.build_device_down_card("接入1", "10.0.0.11", recovered=False, downstream=0), ensure_ascii=False)
    assert "下游" not in plain


def test_sysname_change_is_a_notification_not_an_alert(monkeypatch):
    monkeypatch.setattr(bridge, "next_event_title", lambda: "#1")
    card = bridge.build_sysname_change_card("ac", "pgs-ac", ip="192.168.10.56")
    header = card["card"]["header"]

    assert header["subtitle"]["content"] == "✏️ sysName 变更"
    assert "告警" not in header["title"]["content"]


def test_sysname_change_rejects_numeric_ip_and_case_only_artifacts():
    assert bridge._meaningful_sysname("2") == ""
    assert bridge._meaningful_sysname("192.168.71.8") == ""
    assert bridge._meaningful_sysname("AVL") == "AVL"
    assert bridge._sysname_changed("2", "avl") is False
    assert bridge._sysname_changed("AVL", "avl") is False
    assert bridge._sysname_changed("old-avl", "avl") is True


def test_device_down_and_recovery_titles_are_distinct(monkeypatch):
    monkeypatch.setattr(bridge, "next_event_title", lambda: "#1")
    down = bridge.build_device_down_card(
        "ac", "192.168.10.56", recovered=False, offline_seconds=11,
    )
    recovered = bridge.build_device_down_card(
        "ac", "192.168.10.56", recovered=True, offline_seconds=425,
    )

    assert down["card"]["header"]["subtitle"]["content"] == "设备离线告警"
    assert recovered["card"]["header"]["subtitle"]["content"] == "设备上线恢复"
    assert "状态：DOWN" in down["card"]["body"]["elements"][0]["content"]
    assert "状态：UP" in recovered["card"]["body"]["elements"][0]["content"]


def test_librenms_recovery_callback_does_not_reuse_offline_title(monkeypatch):
    monkeypatch.setattr(bridge, "next_event_title", lambda: "#1")
    card = bridge.build_librenms_card({
        "state": "0",
        "name": "设备离线告警",
        "sysName": "ac",
        "ip": "192.168.10.56",
    })

    assert card["card"]["header"]["subtitle"]["content"] == "设备上线恢复"
    assert "状态：UP" in card["card"]["body"]["elements"][0]["content"]


def test_resource_cards_use_header_color_without_severity_emoji(monkeypatch):
    monkeypatch.setattr(bridge, "next_event_title", lambda: "#1")
    sample = {"kind": "cpu", "name": "core", "ip": "10.0.0.1", "value": 95}

    alert = bridge.build_device_resource_card(sample, recovered=False, duration=60)
    recovered = bridge.build_device_resource_card(sample, recovered=True, duration=120)

    assert alert["card"]["header"]["template"] == "orange"
    assert alert["card"]["header"]["subtitle"]["content"] == "交换机 CPU 持续高占用"
    assert recovered["card"]["header"]["template"] == "green"
    assert recovered["card"]["header"]["subtitle"]["content"] == "交换机 CPU 已恢复"


def test_fetch_interconnect_members_maps_aggregate_to_member_ifindexes(monkeypatch):
    # ifStackTable rows: higher=aggregate ifIndex, lower=member ifIndex; 0 is a
    # top/bottom sentinel and must be ignored.
    stack_rows = [
        {"metric": {"target_ip": "10.0.0.1", "ifStackHigherLayer": "400", "ifStackLowerLayer": "4"}, "value": [0, "1"]},
        {"metric": {"target_ip": "10.0.0.1", "ifStackHigherLayer": "400", "ifStackLowerLayer": "5"}, "value": [0, "1"]},
        {"metric": {"target_ip": "10.0.0.1", "ifStackHigherLayer": "0", "ifStackLowerLayer": "400"}, "value": [0, "1"]},
        {"metric": {"target_ip": "10.0.0.1", "ifStackHigherLayer": "4", "ifStackLowerLayer": "0"}, "value": [0, "1"]},
    ]
    monkeypatch.setattr(bridge, "prometheus_query", lambda q: stack_rows)
    members = bridge.fetch_interconnect_members("infra-switch-ifmib")
    assert members == {("10.0.0.1", "400"): ["4", "5"]}


def test_fetch_interconnect_members_ignores_inactive_relationships(monkeypatch):
    stack_rows = [
        {"metric": {"target_ip": "10.0.0.1", "ifStackHigherLayer": "400", "ifStackLowerLayer": "4"}, "value": [0, "1"]},
        {"metric": {"target_ip": "10.0.0.1", "ifStackHigherLayer": "400", "ifStackLowerLayer": "5"}, "value": [0, "2"]},
    ]
    monkeypatch.setattr(bridge, "prometheus_query", lambda q: stack_rows)

    assert bridge.fetch_interconnect_members("infra-switch-ifmib") == {
        ("10.0.0.1", "400"): ["4"],
    }


def test_stale_down_bundle_loses_members_owned_by_current_up_bundle():
    ports = [
        {
            "ip": "192.168.10.254", "port": "Po2", "lag_up": True,
            "members": [
                {"ifindex": "10", "name": "Te1/0/3", "up": True},
                {"ifindex": "29", "name": "Te2/0/3", "up": True},
                # IOS-XE also left one unrelated stale relationship on Po2.
                {"ifindex": "11", "name": "Te1/0/4", "up": True},
            ],
        },
        {
            "ip": "192.168.10.254", "port": "Po10", "lag_up": False,
            "members": [
                {"ifindex": "10", "name": "Te1/0/3", "up": True},
                {"ifindex": "29", "name": "Te2/0/3", "up": True},
            ],
        },
    ]

    resolved = bridge.suppress_shadowed_down_aggregates(ports)

    assert len(resolved[0]["members"]) == 3
    assert resolved[1]["members"] == []
    assert resolved[1]["shadowed_by"] == "Po2"
    assert bridge.classify_interconnect(False, []) == "unknown"


def test_real_down_bundle_is_kept_when_only_some_members_overlap():
    ports = [
        {
            "ip": "192.168.10.254", "port": "Po2", "lag_up": True,
            "members": [
                {"ifindex": "11", "name": "Te1/0/4", "up": True},
                {"ifindex": "29", "name": "Te2/0/3", "up": True},
            ],
        },
        {
            "ip": "192.168.10.254", "port": "Po3", "lag_up": False,
            "members": [
                {"ifindex": "11", "name": "Te1/0/4", "up": False},
                {"ifindex": "30", "name": "Te2/0/4", "up": False},
            ],
        },
    ]

    resolved = bridge.suppress_shadowed_down_aggregates(ports)

    assert len(resolved[1]["members"]) == 2
    assert bridge.classify_interconnect(False, [False, False]) == "down"


def test_member_errdisable_is_merged_into_peer_aggregate_alert(monkeypatch):
    bridge.reset_link_event_correlation()
    monkeypatch.setattr(bridge, "INTERCONNECT_SYSLOG_MERGE_SECONDS", 20)
    monkeypatch.setattr(
        bridge,
        "_host_display_name",
        lambda host: "Lan-Server" if host == "192.168.10.47" else host,
    )
    monkeypatch.setattr(bridge, "next_event_title", lambda: "#1")
    syslog_event = {"kind": "errdisable", "port": "Gi1/1/2", "reason": "link-flap"}
    bridge.register_errdisable_merge_candidate("192.168.10.47", syslog_event, now=100)
    aggregate_event = {
        "device": "Global_SW3850-12XS_STACK",
        "ip": "192.168.10.254",
        "port": "Po2",
        "peer_switch": "Lan-Server",
        "down_members": ["Te2/0/3"],
        "up_members": ["Te1/0/3"],
        "status": "degraded",
        "duration": 5,
    }

    cause = bridge.find_errdisable_merge_candidate(aggregate_event, now=105)
    assert cause["port"] == "Gi1/1/2"
    aggregate_event["syslog_cause"] = cause
    card = bridge.build_interconnect_card(aggregate_event, recovered=False)
    assert "Lan-Server Gi1/1/2 被保护关闭（link-flap（链路频繁抖动））" in json.dumps(
        card, ensure_ascii=False,
    )

    bridge.complete_interconnect_merge(aggregate_event, cause, now=105)
    # The syslog card is flushed after the 20-second wait. Its consumed state
    # must still exist then, otherwise the same incident would be sent twice.
    assert bridge.errdisable_was_merged(syslog_event, now=121) is True
    bridge.reset_link_event_correlation()


def test_errdisable_card_keeps_title_and_explains_raw_reason(monkeypatch):
    monkeypatch.setattr(bridge, "next_event_title", lambda: "#35659")
    monkeypatch.setattr(bridge, "_host_display_name", lambda _host: "lan-server")
    event = {
        "kind": "errdisable",
        "title": "🛑 接口被保护关闭",
        "color": "orange",
        "port": "Gi1/1/2",
        "reason": "link-flap",
    }

    card = bridge.build_network_syslog_card("192.168.10.47", "message", event)
    header = card["card"]["header"]
    body = card["card"]["body"]["elements"][0]["content"]

    assert header["title"]["content"] == "#35659 接口被保护关闭"
    assert header["subtitle"]["content"] == "接口被保护关闭"
    assert header["template"] == "orange"
    assert "设备：lan-server (192.168.10.47)" in body
    assert "接口：Gi1/1/2" in body
    assert "状态：Err-disable" in body
    assert "原因：link-flap（链路频繁抖动）" in body


def test_errdisable_reason_explanations_preserve_raw_tokens():
    assert bridge.format_errdisable_reason("link-flap") == "link-flap（链路频繁抖动）"
    assert bridge.format_errdisable_reason("bpduguard") == "bpduguard（BPDU保护触发）"
    assert bridge.format_errdisable_reason("loopback") == "loopback（检测到二层环路）"
    assert bridge.format_errdisable_reason("storm-control") == "storm-control"
    assert bridge.format_mac_flap_reason() == "MAC flap（MAC地址漂移）"


def test_network_risk_cards_are_orange_and_recovery_is_green(monkeypatch):
    monkeypatch.setattr(bridge, "next_event_title", lambda: "#1")
    monkeypatch.setattr(bridge, "_host_display_name", lambda host: host)

    mac_flap = bridge.build_network_syslog_card(
        "core", "message", {
            "kind": "mac_flap", "title": "🔴 网关 MAC 异常移动", "color": "red",
            "mac": "00:11:22:33:44:55", "vlan": "10", "port_a": "Gi1/0/1",
            "port_b": "Gi1/0/2",
        },
    )
    bpdu = bridge.build_network_syslog_card(
        "core", "message", {
            "kind": "bpduguard", "title": "⛔ BPDU blocked: Has worsened",
            "color": "red", "port": "Gi1/0/3",
        },
    )
    recovered = bridge.build_network_syslog_card(
        "core", "message", {
            "kind": "errdisable", "title": "🛑 接口被保护关闭",
            "color": "orange", "port": "Gi1/0/4", "reason": "loopback",
        }, recovered=True, duration=20,
    )

    assert mac_flap["card"]["header"]["template"] == "orange"
    assert mac_flap["card"]["header"]["subtitle"]["content"] == "网关 MAC 异常移动"
    assert "原因：MAC flap（MAC地址漂移）" in mac_flap["card"]["body"]["elements"][0]["content"]
    assert bpdu["card"]["header"]["template"] == "orange"
    assert bpdu["card"]["header"]["subtitle"]["content"] == "BPDU 保护触发"
    assert recovered["card"]["header"]["template"] == "green"
    assert recovered["card"]["header"]["subtitle"]["content"] == "接口保护恢复"
    assert "状态：已恢复" in recovered["card"]["body"]["elements"][0]["content"]


def test_late_errdisable_is_suppressed_after_aggregate_alert(monkeypatch):
    bridge.reset_link_event_correlation()
    monkeypatch.setattr(bridge, "INTERCONNECT_SYSLOG_MERGE_SECONDS", 20)
    monkeypatch.setattr(bridge, "_host_display_name", lambda host: "Lan-Server")
    aggregate_event = {
        "device": "Global_SW3850-12XS_STACK",
        "ip": "192.168.10.254",
        "port": "Po2",
        "peer_switch": "Lan-Server",
        "down_members": ["Te2/0/3"],
    }
    bridge.complete_interconnect_merge(aggregate_event, now=100)
    syslog_event = {"kind": "errdisable", "port": "Gi1/1/2", "reason": "link-flap"}

    record = bridge.register_errdisable_merge_candidate(
        "192.168.10.47", syslog_event, now=105,
    )

    assert record["consumed"] is True
    assert bridge.errdisable_was_merged(syslog_event, now=121) is True
    bridge.reset_link_event_correlation()


def test_wan_keyword_digit_suffix_requires_boundary():
    # WatchGuard 这类防火墙 SNMP 只报 eth0/eth1 物理名：填 eth1 只能命中 eth1，
    # 不能顺带把 eth10~eth15 当成 WAN 口；非数字结尾的关键词维持包含匹配。
    original = bridge.FIREWALL_WAN_IF_FILTER
    try:
        bridge.FIREWALL_WAN_IF_FILTER = "telecom,WAN,eth0,eth1"
        assert bridge._is_wan_port("eth0") is True
        assert bridge._is_wan_port("eth1") is True
        assert bridge._is_wan_port("eth10") is False
        assert bridge._is_wan_port("eth15") is False
        assert bridge._is_wan_port("WAN1") is True
        assert bridge._is_wan_port("telecom-200M") is True
        assert bridge._is_wan_port("lan-port") is False
    finally:
        bridge.FIREWALL_WAN_IF_FILTER = original


def _mac_flap_event(mac="0011.2233.4455", vlan="41", port_a="Gi1/0/20", port_b="Po1"):
    parsed = bridge.parse_network_syslog_event(
        f"%SW_MATM-4-MACFLAP_NOTIF: Host {mac} in vlan {vlan} "
        f"is flapping between port {port_a} and port {port_b}"
    )
    assert parsed and parsed["kind"] == "mac_flap"
    return parsed


def test_ordinary_mac_flap_requires_frequency_threshold():
    tracker = bridge.MacFlapTracker(window_seconds=60, threshold=3)

    assert tracker.observe("core", _mac_flap_event(), now=100) is None
    assert tracker.observe("core", _mac_flap_event(), now=120) is None
    event = tracker.observe("core", _mac_flap_event(), now=140)

    assert event["title"] == "🟠 普通 MAC 频繁漂移"
    assert event["move_count"] == 3
    assert event["window_seconds"] == 60


def test_gateway_mac_between_expected_uplink_and_access_port_alerts_immediately(monkeypatch):
    tracker = bridge.MacFlapTracker(
        gateway_macs=["0011.2233.4455"],
        gateway_uplink_ports=["Port-channel1"],
        window_seconds=60,
        threshold=3,
    )

    event = tracker.observe("core", _mac_flap_event(), now=100)

    assert event["title"] == "🔴 网关 MAC 异常移动"
    assert event["normal_port"] == "Po1"
    assert event["abnormal_port"] == "Gi1/0/20"
    assert event["move_count"] == 1
    monkeypatch.setattr(bridge, "next_event_title", lambda: "#1")
    card_text = json.dumps(
        bridge.build_network_syslog_card("core", "message", event),
        ensure_ascii=False,
    )
    assert "✅ 正常上联：Po1" in card_text
    assert "⚠️ 异常接口：Gi1/0/20" in card_text
    assert "🔁 60 秒移动次数：1 次" in card_text
    assert "🧭 判断：" in card_text


def test_gateway_mac_single_move_between_two_expected_ha_uplinks_is_not_alerted():
    tracker = bridge.MacFlapTracker(
        gateway_macs=["0011.2233.4455"],
        gateway_uplink_ports=["Po1", "Po2"],
        window_seconds=60,
        threshold=3,
    )
    event = _mac_flap_event(port_a="Po1", port_b="Po2")

    assert tracker.observe("core", event, now=100) is None
    assert tracker.observe("core", event, now=110) is None
    frequent = tracker.observe("core", event, now=120)
    assert frequent["gateway_mac"] is True
    assert frequent["move_count"] == 3
    assert "normal_port" not in frequent
