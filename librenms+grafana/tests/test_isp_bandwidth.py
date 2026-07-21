import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "alertmanager-feishu-bridge.py"
spec = importlib.util.spec_from_file_location("alertmanager_feishu_bridge", MODULE_PATH)
bridge = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(bridge)


def test_ordered_bandwidth_fallback_for_auto_discovered_ports():
    cfg = bridge._parse_bandwidth_config("*:1000,__link_1:200,__link_2:500")
    rates = [
        {"label": "eth1", "if_index": "3"},
        {"label": "eth0", "if_index": "2"},
    ]
    indexes = bridge._bandwidth_indexes(rates)
    assert bridge._bandwidth_for_label("eth0", "in", cfg, indexes["eth0"]) == 200
    assert bridge._bandwidth_for_label("eth1", "out", cfg, indexes["eth1"]) == 500
    assert bridge._bandwidth_for_label("unknown", "in", cfg) == 1000


def test_named_bandwidth_still_takes_precedence_over_position():
    cfg = bridge._parse_bandwidth_config("*:800,telecom:200,__link_2:500")
    assert bridge._bandwidth_for_label("telecom-wan", "in", cfg, 1) == 200


def test_counter_glitch_limit_drops_impossible_rates():
    # 200 Mbps 口默认 5 倍防护线 = 1 Gbps
    limit = bridge._counter_glitch_limit_bps(200, factor=5)
    assert limit == 1000000000
    # 换防火墙时 rate() 算出的 43.7 Gbps 假速率必须被判为毛刺
    assert 43.7e9 >= limit
    # 真实打满甚至小幅超卖(200 Mbps 口跑 250 Mbps)仍然正常参与告警
    assert 250e6 < limit


def test_counter_glitch_limit_disabled_or_invalid():
    assert bridge._counter_glitch_limit_bps(200, factor=0) is None
    assert bridge._counter_glitch_limit_bps(0, factor=5) is None
    assert bridge._counter_glitch_limit_bps("bad", factor=5) is None


def test_most_specific_bandwidth_entry_wins_over_generic():
    # "电信" 兜底在前也不能抢走 "电信2" 的精确条目
    cfg = bridge._parse_bandwidth_config("电信:500,电信2:200")
    assert bridge._bandwidth_for_label("电信2", "in", cfg) == 200
    assert bridge._bandwidth_for_label("电信1", "in", cfg) == 500
    # 带后缀的去重名（电信-2）按去符号匹配到 电信2 条目
    assert bridge._bandwidth_for_label("电信-2", "in", cfg) == 200


def test_duplicate_wan_labels_get_ifindex_suffixes():
    def sample(label, if_index, direction="in"):
        return {"key": f"{label}|{direction}", "label": label,
                "direction": direction, "value_bps": 0.0, "if_index": if_index}

    rates = bridge._dedupe_wan_labels([
        sample("电信", "7"), sample("电信", "3"),
        sample("电信", "7", "out"), sample("电信", "3", "out"),
        sample("联通", "5"),
    ])
    labels = {(item["label"], item["direction"]) for item in rates}
    # 双电信按 ifIndex 升序编号，两个方向后缀一致；单线联通名字不动
    assert ("电信-1", "in") in labels and ("电信-1", "out") in labels
    assert ("电信-2", "in") in labels and ("电信-2", "out") in labels
    assert ("联通", "in") in labels
    by_label = {item["label"]: item for item in rates if item["direction"] == "in"}
    assert by_label["电信-1"]["if_index"] == "3"
    assert by_label["电信-2"]["if_index"] == "7"
    # 状态键跟着新名字走，两条线不再共用一个告警状态
    assert {item["key"] for item in rates} == {
        "电信-1|in", "电信-2|in", "电信-1|out", "电信-2|out", "联通|in",
    }


def test_duplicate_wan_labels_without_ifindex_still_split():
    # ifIndex 全缺（SNMP 未给 ifIndex 标签）时，两条同名线仍要分成 -1/-2，
    # 不能塌缩到同一个状态键，且 in/out 后缀一致。
    def sample(label, direction):
        return {"key": f"{label}|{direction}", "label": label,
                "direction": direction, "value_bps": 0.0, "if_index": None}

    rates = bridge._dedupe_wan_labels([
        sample("电信", "in"), sample("电信", "in"),
        sample("电信", "out"), sample("电信", "out"),
    ])
    keys = [item["key"] for item in rates]
    assert sorted(keys) == ["电信-1|in", "电信-1|out", "电信-2|in", "电信-2|out"]
    # 每条线在 in 和 out 各出现一次（后缀跨方向一致）
    assert keys.count("电信-1|in") == 1 and keys.count("电信-1|out") == 1


def test_isp_data_missing_card_states():
    alert = bridge.build_isp_data_missing_card(130, recovered=False)
    body = alert["card"]["body"]["elements"][0]["content"]
    assert "数据中断" in body
    assert "FIREWALL_WAN_IF_FILTER" in body

    recover = bridge.build_isp_data_missing_card(130, recovered=True)
    body = recover["card"]["body"]["elements"][0]["content"]
    assert "已恢复" in body


if __name__ == "__main__":
    test_ordered_bandwidth_fallback_for_auto_discovered_ports()
    test_named_bandwidth_still_takes_precedence_over_position()
    test_counter_glitch_limit_drops_impossible_rates()
    test_counter_glitch_limit_disabled_or_invalid()
    test_most_specific_bandwidth_entry_wins_over_generic()
    test_duplicate_wan_labels_get_ifindex_suffixes()
    test_duplicate_wan_labels_without_ifindex_still_split()
    test_isp_data_missing_card_states()
    print("ISP bandwidth tests passed")
