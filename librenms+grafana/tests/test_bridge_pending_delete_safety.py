import copy
import importlib.util
import io
import json
import time
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest


spec = importlib.util.spec_from_file_location(
    "pending_delete_safety_bridge",
    Path(__file__).resolve().parents[1] / "alertmanager-feishu-bridge.py",
)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
JOB = "infra-dist-ping"
IP = "192.0.2.27"
KEY = f"{JOB}|{IP}"
NOW = time.time()


def sample(value, **labels):
    return {"metric": {"job": JOB, "target_ip": IP, **labels},
            "value": [NOW, value]}


@pytest.fixture
def pending(monkeypatch):
    state = {"job": JOB, "ip": IP, "name": "test-switch",
             "pending_delete": True, "pending_token": "test-confirm",
             "pending_since": 100, "down_since": 10, "alerting": True,
             "retired": False, "seen_up": True}
    calls = []
    monkeypatch.setattr(bridge.time, "time", lambda: NOW)
    monkeypatch.setattr(bridge, "DEVICE_PENDING_DELETE_ENABLED", True)
    monkeypatch.setattr(bridge, "DEVICE_DOWN_STATES", {KEY: state})
    monkeypatch.setattr(bridge, "delete_librenms_device",
                        lambda ip: calls.append(("delete", ip)) or "deleted")
    monkeypatch.setattr(bridge, "save_device_down_states",
                        lambda states: calls.append(("save", copy.deepcopy(states))))
    monkeypatch.setattr(bridge, "send_feishu", lambda *a, **k: pytest.fail("unexpected notification"))
    monkeypatch.setattr(bridge, "prometheus_query", lambda query: [sample("0")])
    return state, calls


@pytest.mark.parametrize("response", [
    [], None, {}, [None], [False], [{"metric": {"job": JOB, "target_ip": IP}}],
    [sample("bad")], [sample("NaN")], [sample("Infinity")], [sample("-Infinity")],
    [sample("2")], [sample("-1")], [sample("0.5")], [sample(None)], [sample(True)],
    [sample("0", target_ip="192.0.2.99")], [sample("0", job="other")],
    [{"metric": {}, "value": [NOW, "0"]}],
    [{"metric": {"job": JOB, "target_ip": IP}, "value": "00"}],
    [{"metric": {"job": JOB, "target_ip": IP}, "value": [0, "0"]}],
    [sample("0"), sample("bad")], [sample("bad"), sample("0")],
    [{"metric": {"job": JOB, "target_ip": IP}, "value": ["NaN", "0"]}],
    [{"metric": {"job": JOB, "target_ip": IP}, "value": [NOW + 60, "0"]}],
])
def test_unknown_preserves_pending_and_never_deletes(pending, monkeypatch, response):
    state, calls = pending
    before = copy.deepcopy(state)
    monkeypatch.setattr(bridge, "prometheus_query", lambda query: response)
    result = bridge.resolve_pending_delete(KEY, "delete", "test-confirm")
    assert result["ok"] is False
    assert "无法确认" in result["error"]
    assert state == before
    assert calls == []


@pytest.mark.parametrize("error", [TimeoutError(), ConnectionError(),
    URLError("test connection"), HTTPError("http://test", 500, "test", {}, None),
    RuntimeError("API failure")])
def test_query_failure_and_valid_retry(pending, monkeypatch, error):
    state, calls = pending
    before = copy.deepcopy(state)
    def failed(query):
        raise error
    monkeypatch.setattr(bridge, "prometheus_query", failed)
    assert bridge.resolve_pending_delete(KEY, "delete", "test-confirm")["ok"] is False
    assert state == before and calls == []
    monkeypatch.setattr(bridge, "prometheus_query", lambda query: [sample("0")])
    assert bridge.resolve_pending_delete(KEY, "delete", "test-confirm")["ok"] is True
    assert calls[0] == ("delete", IP)
    assert state["retired"] is True


@pytest.mark.parametrize("field,value", [("job", ""), ("job", "bad job"),
    ("ip", ""), ("ip", "999.999.999.999"), ("ip", "deadbeef")])
def test_invalid_identity(pending, monkeypatch, field, value):
    state, calls = pending
    state[field] = value
    before = copy.deepcopy(state)
    monkeypatch.setattr(bridge, "prometheus_query", lambda query: pytest.fail("invalid target queried"))
    assert bridge.resolve_pending_delete(KEY, "delete", "test-confirm")["ok"] is False
    assert state == before and calls == []


@pytest.mark.parametrize("values", [("1",), ("0", "1"), ("1", "0"),
                                   ("bad", "1"), ("1", "bad")])
def test_online_wins_independent_of_order(pending, monkeypatch, values):
    state, calls = pending
    monkeypatch.setattr(bridge, "prometheus_query", lambda query: [sample(v) for v in values])
    result = bridge.resolve_pending_delete(KEY, "delete", "test-confirm")
    assert result["ok"] is False and "当前在线" in result["error"]
    assert state["pending_delete"] is False
    assert not any(call[0] == "delete" for call in calls)


@pytest.mark.parametrize("result", ["deleted", "missing", "failed"])
def test_offline_keeps_existing_delete_lifecycle(pending, monkeypatch, result):
    state, calls = pending
    before = copy.deepcopy(state)
    monkeypatch.setattr(bridge, "delete_librenms_device", lambda ip: calls.append(("delete", ip)) or result)
    response = bridge.resolve_pending_delete(KEY, "delete", "test-confirm")
    assert calls[0] == ("delete", IP)
    if result == "failed":
        assert response["ok"] is False and state == before
    else:
        assert response["ok"] is True and state["retired"] is True


@pytest.mark.parametrize("action,token,enabled", [("delete", "wrong", True),
    ("invalid", "test-confirm", True), ("delete", "test-confirm", False),
    ("keep", "test-confirm", True)])
def test_existing_guards_and_keep(pending, monkeypatch, action, token, enabled):
    state, calls = pending
    monkeypatch.setattr(bridge, "DEVICE_PENDING_DELETE_ENABLED", enabled)
    monkeypatch.setattr(bridge, "prometheus_query", lambda query: pytest.fail("unexpected query"))
    result = bridge.resolve_pending_delete(KEY, action, token)
    assert result["ok"] is (action == "keep")
    assert not any(call[0] == "delete" for call in calls)


@pytest.mark.parametrize("payload", [
    {"status": "error", "error": "test API error"},
    {"status": "success", "data": {}},
    {"status": "success", "data": {"result": [None]}},
    None,
])
def test_real_query_adapter_rejects_invalid_api_response(pending, monkeypatch, payload):
    state, calls = pending
    before = copy.deepcopy(state)
    monkeypatch.setattr(bridge, "prometheus_query", real_prometheus_query)
    monkeypatch.setattr(bridge.request, "urlopen",
                        lambda *a, **k: io.BytesIO(json.dumps(payload).encode()))
    assert bridge.resolve_pending_delete(KEY, "delete", "test-confirm")["ok"] is False
    assert state == before and calls == []


real_prometheus_query = bridge.prometheus_query
