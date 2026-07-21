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
