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
    return {"metric": {"job": JOB, "target_ip": IP, **labels}, "value": [NOW, value]}


@pytest.fixture
def pending(monkeypatch, tmp_path):
    state = {
        "job": JOB, "ip": IP, "name": "test-switch",
        "pending_delete": True, "pending_token": "test-confirm",
        "pending_since": 100, "down_since": 10, "alerting": True,
        "retired": False, "seen_up": True,
    }
    calls = []
    monkeypatch.setattr(bridge.time, "time", lambda: NOW)
    monkeypatch.setattr(bridge, "DEVICE_PENDING_DELETE_ENABLED", True)
    monkeypatch.setattr(bridge, "MANUAL_DELETE_GUARDS_READY", True)
    monkeypatch.setattr(bridge, "MANUAL_DELETE_GUARD_DIR", str(tmp_path / "guards"))
    monkeypatch.setattr(bridge, "DEVICE_DOWN_STATE_FILE", str(tmp_path / "states.json"))
    monkeypatch.setattr(bridge, "LIBRENMS_URL", "http://librenms.test")
    monkeypatch.setattr(bridge, "DEVICE_DOWN_STATES", {KEY: state})
    bridge.MANUAL_DELETE_OPERATIONS.clear()
    bridge.MANUAL_DELETE_BY_KEY.clear()
    bridge.MANUAL_DELETE_BY_IP.clear()
    bridge.MANUAL_DELETE_GUARDS.clear()
    monkeypatch.setattr(
        bridge, "_manual_delete_inventory",
        lambda operation: calls.append(("inventory", operation["ip"]))
        or ("secret", "42", {"device_id": 42, "ip": IP}),
    )
    monkeypatch.setattr(
        bridge, "_blackbox_icmp_probe",
        lambda ip, timeout=None, deadline=None: calls.append(("blackbox", ip)) or False,
    )
    monkeypatch.setattr(
        bridge, "_manual_delete_exact_id",
        lambda token, device_id, timeout, deadline=None: calls.append(("delete", device_id)) or "deleted",
    )
    monkeypatch.setattr(
        bridge, "save_device_down_states_durable",
        lambda states: calls.append(("durable-state", copy.deepcopy(states))),
    )
    monkeypatch.setattr(
        bridge, "save_device_down_states",
        lambda states: calls.append(("state", copy.deepcopy(states))),
    )
    monkeypatch.setattr(bridge, "send_feishu", lambda *a, **k: pytest.fail("unexpected notification"))
    monkeypatch.setattr(bridge, "prometheus_query", lambda query, timeout=10, deadline=None: [sample("0")])
    return state, calls


UNKNOWN_RESPONSES = [
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
]


@pytest.mark.parametrize("response", UNKNOWN_RESPONSES)
def test_unknown_prometheus_requires_blackbox_then_can_delete(pending, monkeypatch, response):
    state, calls = pending
    monkeypatch.setattr(bridge, "prometheus_query", lambda query, timeout=10, deadline=None: response)
    result = bridge.resolve_pending_delete(KEY, "delete", "test-confirm")
    assert result["ok"] is True
    stages = [(kind, value) for kind, value, *_ in calls if kind in {"inventory", "blackbox", "delete"}]
    assert stages == [("inventory", IP), ("blackbox", IP), ("delete", "42")]
    assert state["retired"] is True


@pytest.mark.parametrize("failure", [
    TimeoutError(), ConnectionError(), URLError("connection"),
    HTTPError("http://test", 500, "test", {}, None), RuntimeError("API failure"),
])
def test_prometheus_failure_still_requires_final_blackbox(pending, monkeypatch, failure):
    state, calls = pending

    def failed(query, timeout=10, deadline=None):
        raise failure

    monkeypatch.setattr(bridge, "prometheus_query", failed)
    assert bridge.resolve_pending_delete(KEY, "delete", "test-confirm")["ok"] is True
    assert ("blackbox", IP) in calls and ("delete", "42") in calls
    assert state["retired"] is True


@pytest.mark.parametrize("probe", [True, None, 0, 1, "0", {}, []])
def test_blackbox_online_or_malformed_preserves_pending(pending, monkeypatch, probe):
    state, calls = pending
    before = copy.deepcopy(state)
    monkeypatch.setattr(
        bridge, "_blackbox_icmp_probe",
        lambda ip, timeout=None, deadline=None: calls.append(("blackbox", ip)) or probe,
    )
    result = bridge.resolve_pending_delete(KEY, "delete", "test-confirm")
    assert result["ok"] is False
    assert state == before
    assert not any(call[0] == "delete" for call in calls)


@pytest.mark.parametrize("field,value", [
    ("job", ""), ("job", "bad job"), ("ip", ""),
    ("ip", "999.999.999.999"), ("ip", "deadbeef"),
])
def test_invalid_identity_performs_no_evidence_or_delete_io(pending, monkeypatch, field, value):
    state, calls = pending
    state[field] = value
    before = copy.deepcopy(state)
    monkeypatch.setattr(bridge, "prometheus_query", lambda *a, **k: pytest.fail("queried"))
    result = bridge.resolve_pending_delete(KEY, "delete", "test-confirm")
    assert result["ok"] is False
    assert state == before and calls == []


@pytest.mark.parametrize("values", [("1",), ("0", "1"), ("1", "0"), ("bad", "1"), ("1", "bad")])
def test_prometheus_online_wins_and_skips_later_stages(pending, monkeypatch, values):
    state, calls = pending
    monkeypatch.setattr(
        bridge, "prometheus_query",
        lambda query, timeout=10: [sample(value) for value in values],
    )
    result = bridge.resolve_pending_delete(KEY, "delete", "test-confirm")
    assert result["ok"] is False and "当前在线" in result["error"]
    assert state["pending_delete"] is False
    assert not any(call[0] in {"inventory", "blackbox", "delete"} for call in calls)


def test_missing_inventory_record_still_probes_then_succeeds_without_delete(pending, monkeypatch):
    state, calls = pending
    monkeypatch.setattr(
        bridge, "_manual_delete_inventory",
        lambda operation: calls.append(("inventory", operation["ip"])) or ("secret", "", None),
    )
    result = bridge.resolve_pending_delete(KEY, "delete", "test-confirm")
    assert result["ok"] is True
    assert ("blackbox", IP) in calls
    assert not any(call[0] == "delete" for call in calls)
    assert state["retired"] is True


@pytest.mark.parametrize("action,token,enabled", [
    ("delete", "wrong", True), ("invalid", "test-confirm", True),
    ("delete", "test-confirm", False), ("keep", "test-confirm", True),
])
def test_existing_guards_and_keep(pending, monkeypatch, action, token, enabled):
    state, calls = pending
    monkeypatch.setattr(bridge, "DEVICE_PENDING_DELETE_ENABLED", enabled)
    result = bridge.resolve_pending_delete(KEY, action, token)
    assert result["ok"] is (action == "keep")
    assert not any(call[0] in {"inventory", "blackbox", "delete"} for call in calls)


@pytest.mark.parametrize("payload", [
    {"status": "error", "error": "test API error"},
    {"status": "success", "data": {}},
    {"status": "success", "data": {"result": [None]}},
    None,
])
def test_real_query_adapter_unknown_falls_through_to_blackbox(pending, monkeypatch, payload):
    state, calls = pending
    monkeypatch.setattr(bridge, "prometheus_query", real_prometheus_query)
    monkeypatch.setattr(
        bridge.request, "urlopen",
        lambda *a, **k: io.BytesIO(json.dumps(payload).encode()),
    )
    result = bridge.resolve_pending_delete(KEY, "delete", "test-confirm")
    assert result["ok"] is True
    assert ("blackbox", IP) in calls and ("delete", "42") in calls
    assert state["retired"] is True


real_prometheus_query = bridge.prometheus_query
