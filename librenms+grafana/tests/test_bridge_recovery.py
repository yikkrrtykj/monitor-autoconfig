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
    # One member down while the bundle is still up -> the alertable case.
    assert bridge.classify_interconnect(True, [True, False]) == "degraded"
    # Every member down -> bundle down; device-down covers the peer, stay quiet.
    assert bridge.classify_interconnect(False, [False, False]) == "down"
    # A single-member bundle that drops is "down", never "degraded".
    assert bridge.classify_interconnect(False, [False]) == "down"
    # No member visibility (no ifStackTable) -> nothing to say.
    assert bridge.classify_interconnect(True, []) == "unknown"


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
