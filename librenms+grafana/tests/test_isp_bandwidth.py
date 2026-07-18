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
    test_isp_data_missing_card_states()
    print("ISP bandwidth tests passed")
