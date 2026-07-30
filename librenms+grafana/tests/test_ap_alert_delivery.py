import importlib.util
import json
from pathlib import Path
from urllib import error


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "alertmanager-feishu-bridge.py"
spec = importlib.util.spec_from_file_location("ap_alert_delivery_bridge", MODULE_PATH)
bridge = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(bridge)


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def _card():
    return {"card": {"header": {"title": {"content": "test"}}}}


def test_fast_ap_down_default():
    assert bridge.UNIFI_AP_DOWN_FOR_SECONDS == 10


def test_feishu_http_200_business_error_is_retried(monkeypatch):
    responses = iter([
        _FakeResponse({"code": 19002, "msg": "rate limited"}),
        _FakeResponse({"code": 0, "msg": "success"}),
    ])
    calls = []

    def fake_urlopen(req, timeout):
        calls.append((req, timeout))
        return next(responses)

    monkeypatch.setattr(bridge, "TOKEN", "token")
    monkeypatch.setattr(bridge, "DRY_RUN", False)
    monkeypatch.setattr(bridge, "FEISHU_SEND_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(bridge, "FEISHU_SEND_RETRY_BASE_SECONDS", 0)
    monkeypatch.setattr(bridge.request, "urlopen", fake_urlopen)

    assert bridge.send_feishu(_card()) is True
    assert len(calls) == 2


def test_feishu_network_failure_is_not_acknowledged(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout):
        calls.append((req, timeout))
        raise error.URLError("offline")

    monkeypatch.setattr(bridge, "TOKEN", "token")
    monkeypatch.setattr(bridge, "DRY_RUN", False)
    monkeypatch.setattr(bridge, "FEISHU_SEND_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(bridge, "FEISHU_SEND_RETRY_BASE_SECONDS", 0)
    monkeypatch.setattr(bridge.request, "urlopen", fake_urlopen)

    assert bridge.send_feishu(_card()) is False
    assert len(calls) == 3


def test_online_dedupe_is_saved_only_after_delivery(monkeypatch, tmp_path):
    state_file = tmp_path / "notified-devices.json"
    outcomes = iter([False, True])
    calls = []

    def fake_send(card):
        calls.append(card)
        return next(outcomes)

    monkeypatch.setattr(bridge, "DEVICE_ONLINE_STATE_FILE", str(state_file))
    monkeypatch.setattr(bridge, "send_feishu", fake_send)
    bridge.DEVICE_ONLINE_INFLIGHT.clear()

    assert bridge.send_device_online_once(_card(), "ap-1", "192.0.2.1") is False
    assert not state_file.exists()
    assert bridge.send_device_online_once(_card(), "ap-1", "192.0.2.1") is True
    assert set(json.loads(state_file.read_text(encoding="utf-8"))) == {"ap-1", "192.0.2.1"}
    assert bridge.send_device_online_once(_card(), "ap-1", "192.0.2.1") is True
    assert len(calls) == 2


def test_ap_down_and_recovery_titles_are_distinct():
    down = bridge.build_ap_down_card("AP-1", "192.0.2.1", "U6-LR", False, 10)
    recovered = bridge.build_ap_down_card("AP-1", "192.0.2.1", "U6-LR", True, 15)

    assert down["card"]["header"]["subtitle"]["content"] == "🔴 AP 掉线告警"
    assert recovered["card"]["header"]["subtitle"]["content"] == "🟢 AP 上线恢复"
