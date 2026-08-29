import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "alertmanager-feishu-bridge.py"
spec = importlib.util.spec_from_file_location("alertmanager_feishu_bridge", MODULE_PATH)
bridge = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(bridge)


def test_isp_data_missing_card_states():
    alert = bridge.build_isp_data_missing_card(130, recovered=False)
    body = alert["card"]["body"]["elements"][0]["content"]
    assert "数据中断" in body
    assert "FIREWALL_WAN_IF_FILTER" in body
    assert alert["card"]["header"]["title"]["content"].endswith("外网流量采集中断")
    assert "subtitle" not in alert["card"]["header"]
    assert "状态：数据中断" in body

    recover = bridge.build_isp_data_missing_card(130, recovered=True)
    body = recover["card"]["body"]["elements"][0]["content"]
    assert "已恢复" in body
    assert recover["card"]["header"]["title"]["content"].endswith("外网流量采集恢复")
    assert "subtitle" not in recover["card"]["header"]
    assert "状态：已恢复" in body


def test_isp_bandwidth_card_titles_distinguish_alert_and_recovery():
    event = {
        "label": "telecom-100M",
        "direction": "in",
        "value_bps": 95_000_000,
        "duration": 120,
    }
    alert = bridge.build_isp_bandwidth_card(event, recovered=False)
    recover = bridge.build_isp_bandwidth_card(event, recovered=True)
    assert alert["card"]["header"]["title"]["content"].endswith("外网 ISP 带宽超限")
    assert recover["card"]["header"]["title"]["content"].endswith("外网 ISP 带宽恢复")
    assert "subtitle" not in alert["card"]["header"]
    assert "subtitle" not in recover["card"]["header"]
    assert "状态：带宽超限" in alert["card"]["body"]["elements"][0]["content"]
    assert "状态：已恢复" in recover["card"]["body"]["elements"][0]["content"]
    assert alert["card"]["header"]["template"] == "orange"
    assert recover["card"]["header"]["template"] == "green"


def test_bridge_isp_watcher_wrapper_delegates(monkeypatch):
    calls = []
    monkeypatch.setattr(bridge._ISP_BANDWIDTH_WATCHER, "run", lambda: calls.append("run"))

    assert bridge.isp_bandwidth_watcher() is None
    assert calls == ["run"]
