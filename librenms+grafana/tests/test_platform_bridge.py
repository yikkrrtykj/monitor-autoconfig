import io
import json
import urllib.error

from platform_api import bridge

from .test_platform_transactions import load_api


BRIDGE_URL = "http://alertmanager-feishu-bridge:5005"


class _Response:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self.data


def test_bridge_module_and_entrypoint_dependency_assembly(tmp_path):
    api = load_api(tmp_path)

    read_context = api._read_api_context()
    write_dependencies = api._write_api_dependencies()
    resolve = write_dependencies.bridge_retire_resolve
    test_alert = write_dependencies.send_test_alert

    assert bridge.bridge_retire_pending.__module__ == "platform_api.bridge"
    assert bridge.bridge_retire_resolve.__module__ == "platform_api.bridge"
    assert bridge.send_test_alert.__module__ == "platform_api.bridge"
    assert read_context.bridge_url == api.BRIDGE_URL
    assert resolve.func is bridge.bridge_retire_resolve
    assert resolve.args == (api.BRIDGE_URL,)
    assert test_alert.func is bridge.send_test_alert
    assert test_alert.args == (api.BRIDGE_URL,)
    assert not hasattr(api, "bridge_retire_pending")
    assert not hasattr(api, "bridge_retire_resolve")
    assert not hasattr(api, "send_test_alert")


def test_retire_pending_keeps_url_timeout_and_json_response(monkeypatch):
    calls = []
    expected = {"ok": True, "pending": [{"key": "switch-1"}]}

    def urlopen(url, timeout):
        calls.append((url, timeout))
        return _Response(json.dumps(expected).encode("utf-8"))

    monkeypatch.setattr(bridge.urllib.request, "urlopen", urlopen)

    assert bridge.bridge_retire_pending(BRIDGE_URL) == expected
    assert calls == [(f"{BRIDGE_URL}/retire/pending", 8)]


def test_retire_pending_keeps_network_error_payload(monkeypatch):
    monkeypatch.setattr(
        bridge.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("fixture offline")
        ),
    )

    assert bridge.bridge_retire_pending(BRIDGE_URL) == {
        "ok": False,
        "error": "无法连接告警服务：fixture offline",
        "pending": [],
    }


def test_retire_resolve_keeps_request_body_content_type_and_timeout(monkeypatch):
    calls = []
    expected = {"ok": True, "action": "confirm"}

    def urlopen(request, timeout):
        calls.append((request, timeout))
        return _Response(json.dumps(expected).encode("utf-8"))

    monkeypatch.setattr(bridge.urllib.request, "urlopen", urlopen)

    result = bridge.bridge_retire_resolve(
        BRIDGE_URL,
        {"key": 17, "action": "confirm", "token": None, "ignored": "value"},
    )

    assert result == expected
    assert len(calls) == 1
    request, timeout = calls[0]
    assert request.full_url == f"{BRIDGE_URL}/retire/resolve"
    assert request.get_method() == "POST"
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data.decode("utf-8")) == {
        "key": "17",
        "action": "confirm",
        "token": "",
    }
    assert timeout == 15


def test_retire_resolve_keeps_http_error_json_body(monkeypatch):
    payload = {"ok": False, "error": "invalid token", "code": "fixture"}

    def urlopen(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            409,
            "Conflict",
            None,
            io.BytesIO(json.dumps(payload).encode("utf-8")),
        )

    monkeypatch.setattr(bridge.urllib.request, "urlopen", urlopen)

    assert bridge.bridge_retire_resolve(BRIDGE_URL, {}) == payload


def test_retire_resolve_keeps_invalid_http_error_body_fallback(monkeypatch):
    def urlopen(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            502,
            "Bad Gateway",
            None,
            io.BytesIO(b"not-json"),
        )

    monkeypatch.setattr(bridge.urllib.request, "urlopen", urlopen)

    assert bridge.bridge_retire_resolve(BRIDGE_URL, {}) == {
        "ok": False,
        "error": "告警服务返回 HTTP 502",
    }


def test_retire_resolve_keeps_ordinary_connection_error(monkeypatch):
    monkeypatch.setattr(
        bridge.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ConnectionError("fixture refused")
        ),
    )

    assert bridge.bridge_retire_resolve(BRIDGE_URL, {}) == {
        "ok": False,
        "error": "无法连接告警服务：fixture refused",
    }


def test_send_test_alert_keeps_request_and_success_response(monkeypatch):
    calls = []
    expected = {"ok": True, "message": "sent"}

    def urlopen(request, timeout):
        calls.append((request, timeout))
        return _Response(json.dumps(expected).encode("utf-8"))

    monkeypatch.setattr(bridge.urllib.request, "urlopen", urlopen)

    assert bridge.send_test_alert(BRIDGE_URL) == expected
    assert len(calls) == 1
    request, timeout = calls[0]
    assert request.full_url == f"{BRIDGE_URL}/test-alert"
    assert request.get_method() == "POST"
    assert request.get_header("Content-type") == "application/json"
    assert request.data == b"{}"
    assert timeout == 10


def test_send_test_alert_keeps_exception_payload(monkeypatch):
    monkeypatch.setattr(
        bridge.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            TimeoutError("fixture timeout")
        ),
    )

    assert bridge.send_test_alert(BRIDGE_URL) == {
        "ok": False,
        "error": "无法连接告警服务：fixture timeout",
    }
