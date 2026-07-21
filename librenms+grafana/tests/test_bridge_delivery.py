import json
import importlib.util
from pathlib import Path
from urllib import error


_spec = importlib.util.spec_from_file_location(
    "feishu_bridge_delivery",
    Path(__file__).resolve().parent.parent / "alertmanager-feishu-bridge.py",
)
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)


class _FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def _card():
    return {"card": {"header": {"title": {"content": "test"}}}}


def test_feishu_http_200_business_error_is_retried(monkeypatch):
    responses = iter([
        _FakeResponse({"code": 19002, "msg": "invalid token"}),
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


def test_approved_app_bot_is_preferred_for_normal_alerts(monkeypatch):
    monkeypatch.setattr(bridge, "FEISHU_APP_ID", "cli_x")
    monkeypatch.setattr(bridge, "FEISHU_APP_SECRET", "secret")
    monkeypatch.setattr(bridge, "send_feishu_app_card", lambda _card: True)
    monkeypatch.setattr(bridge, "_send_feishu_webhook", lambda _card: (_ for _ in ()).throw(AssertionError("webhook should not run")))

    assert bridge.send_feishu(_card()) is True


def test_normal_alert_falls_back_to_webhook_when_app_delivery_fails(monkeypatch):
    monkeypatch.setattr(bridge, "FEISHU_APP_ID", "cli_x")
    monkeypatch.setattr(bridge, "FEISHU_APP_SECRET", "secret")
    monkeypatch.setattr(bridge, "send_feishu_app_card", lambda _card: False)
    monkeypatch.setattr(bridge, "_send_feishu_webhook", lambda _card: True)

    assert bridge.send_feishu(_card()) is True


def test_online_dedupe_is_committed_only_after_delivery(monkeypatch, tmp_path):
    state_file = tmp_path / "online.json"
    outcomes = iter([False, True])
    calls = []

    def fake_send(card):
        calls.append(card)
        return next(outcomes)

    monkeypatch.setattr(bridge, "DEVICE_ONLINE_STATE_FILE", str(state_file))
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
        acquired = bridge.DEVICE_ONLINE_STATE_LOCK.acquire(blocking=False)
        lock_was_free.append(acquired)
        if acquired:
            bridge.DEVICE_ONLINE_STATE_LOCK.release()
        return True

    monkeypatch.setattr(bridge, "DEVICE_ONLINE_STATE_FILE", str(state_file))
    monkeypatch.setattr(bridge, "send_feishu", fake_send)
    bridge.DEVICE_ONLINE_INFLIGHT.clear()

    assert bridge.send_device_online_once(_card(), "switch-1", "10.0.0.1") is True
    assert lock_was_free == [True]


def test_new_lifecycle_online_card_bypasses_lifetime_dedupe(monkeypatch, tmp_path):
    state_file = tmp_path / "online.json"
    state_file.write_text(json.dumps(["switch-1", "10.0.0.1"]), encoding="utf-8")
    calls = []

    monkeypatch.setattr(bridge, "DEVICE_ONLINE_STATE_FILE", str(state_file))
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


def test_retire_confirm_card_has_buttons_only_when_app_configured():
    state = {
        "name": "access-7", "ip": "192.168.10.27", "job": "infra-dist-ping",
        "down_since": 100.0, "pending_token": "tok-9",
    }
    # 配了自建应用：带两个回传按钮，value 携带 key/token/action
    interactive = bridge.build_retire_confirm_card(state, "infra-dist-ping|192.168.10.27", True)
    elements = interactive["card"]["body"]["elements"]
    buttons = [e for e in elements if e.get("tag") == "button"]
    assert len(buttons) == 2
    actions = {b["behaviors"][0]["value"]["action"] for b in buttons}
    assert actions == {"retire_delete", "retire_keep"}
    assert all(b["behaviors"][0]["value"]["token"] == "tok-9" for b in buttons)
    assert all(b["behaviors"][0]["type"] == "callback" for b in buttons)

    # 没配应用：退化为纯通知卡（无按钮），提示到控制台确认
    plain = bridge.build_retire_confirm_card(state, "k", False)
    assert not [e for e in plain["card"]["body"]["elements"] if e.get("tag") == "button"]
    assert "控制台" in plain["card"]["body"]["elements"][0]["content"]


def test_pending_delete_notify_downgrades_to_webhook_when_app_send_fails(monkeypatch):
    key = "infra-dist-ping|192.168.10.27"
    states = {key: {
        "name": "access-7", "ip": "192.168.10.27", "job": "infra-dist-ping",
        "down_since": 100.0, "pending_delete": True, "pending_token": "t",
        "pending_notified": False, "pending_last_notified": None,
    }}
    monkeypatch.setattr(bridge, "FEISHU_APP_ID", "cli_x")
    monkeypatch.setattr(bridge, "FEISHU_APP_SECRET", "secret")
    # 应用发卡失败 -> 必须回退到 webhook 通知卡
    monkeypatch.setattr(bridge, "send_feishu_app_card", lambda card: False)
    webhook_calls = []
    monkeypatch.setattr(bridge, "_send_feishu_webhook", lambda card: webhook_calls.append(card) or True)

    changed = bridge.notify_pending_delete_states(states, 1000.0)
    assert changed is True
    assert states[key]["pending_notified"] is True
    assert len(webhook_calls) == 1

    # 已通知后不重复发（DEVICE_PENDING_DELETE_REALERT_SECONDS=0）
    monkeypatch.setattr(bridge, "DEVICE_PENDING_DELETE_REALERT_SECONDS", 0)
    webhook_calls.clear()
    assert bridge.notify_pending_delete_states(states, 5000.0) is False
    assert not webhook_calls


def test_pending_delete_notify_not_committed_when_all_sends_fail(monkeypatch):
    key = "infra-dist-ping|192.168.10.27"
    states = {key: {
        "name": "access-7", "ip": "192.168.10.27", "job": "infra-dist-ping",
        "down_since": 100.0, "pending_delete": True, "pending_token": "t",
        "pending_notified": False, "pending_last_notified": None,
    }}
    monkeypatch.setattr(bridge, "FEISHU_APP_ID", "")
    monkeypatch.setattr(bridge, "FEISHU_APP_SECRET", "")
    monkeypatch.setattr(bridge, "send_feishu", lambda card: False)
    # 发送失败时不置 notified，下轮还会重试
    assert bridge.notify_pending_delete_states(states, 1000.0) is False
    assert states[key]["pending_notified"] is False
