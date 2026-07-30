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


def test_isp_bandwidth_card_is_distinct_from_isp_offline_alert():
    event = {
        "label": "telecom-100M-长期",
        "direction": "in",
        "value_bps": 90_100_000,
        "duration": 6,
    }
    alert = bridge.build_isp_bandwidth_card(event, recovered=False)
    recover = bridge.build_isp_bandwidth_card(event, recovered=True)

    assert alert["card"]["header"]["subtitle"]["content"] == "🔴 外网 ISP 流量告警"
    assert recover["card"]["header"]["subtitle"]["content"] == "🟢 外网 ISP 流量告警"

    offline = bridge.build_device_down_card(
        "telecom-100M-长期", "222.72.19.237", recovered=False,
        offline_seconds=6, job="infra-isp-ping",
    )
    assert offline["card"]["header"]["subtitle"]["content"] == "🔴 外网 ISP 告警"


if __name__ == "__main__":
    test_ordered_bandwidth_fallback_for_auto_discovered_ports()
    test_named_bandwidth_still_takes_precedence_over_position()
    test_isp_bandwidth_card_is_distinct_from_isp_offline_alert()
    print("ISP bandwidth tests passed")
