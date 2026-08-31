import json
import threading
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import HTTPServer
from unittest.mock import patch

from platform_api import write_api

from .test_platform_auth import load_platform_api
from .test_platform_transactions import load_api


class RecordingLock:
    def __init__(self, calls):
        self.calls = calls

    def __enter__(self):
        self.calls.append(("lock.enter", ()))
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.calls.append(("lock.exit", ()))
        return False


class FakeHandler:
    def __init__(self, calls, body=None):
        self.calls = calls
        self.client_address = ("198.51.100.17", 12345)
        self.body = body or {}
        self.body_reads = 0
        self.sent = []

    def _body(self):
        self.body_reads += 1
        self.calls.append(("body", ()))
        return self.body

    def _send_json(self, payload, status=200, headers=None):
        self.calls.append(("send", (payload, status, headers)))
        self.sent.append((payload, status, headers))


def build_dependencies(calls):
    def result(name, *args):
        calls.append((name, args))
        return {"ok": True, "route": name}

    def login(username, password, source):
        calls.append(("login_auth", (username, password, source)))
        return {"ok": True, "route": "login_auth"}, "login-cookie"

    def change_password(handler, data):
        calls.append(("change_password_auth", (handler, data)))
        return {"ok": True, "route": "change_password_auth"}, "changed-cookie"

    def logout(handler):
        calls.append(("logout_auth", (handler,)))

    def clear_cookie():
        calls.append(("clear_session_cookie", ()))
        return "cleared-cookie"

    def require(handler):
        calls.append(("require_auth", (handler,)))
        return {"username": "fixture-admin"}

    def handle_config_post(handler, path, data):
        if path not in {
            "/config/save",
            "/config/apply",
            "/config/rollback",
            "/config/import",
        }:
            return False
        calls.append(("handle_config_post", (handler, path, data)))
        handler._send_json({"ok": True, "route": "handle_config_post"})
        return True

    return write_api.WriteApiDependencies(
        login_auth=login,
        change_password_auth=change_password,
        logout_auth=logout,
        clear_session_cookie=clear_cookie,
        require_auth=require,
        config_payload=lambda text: result("config_payload", text),
        write_lock=RecordingLock(calls),
        handle_config_post=handle_config_post,
        new_incident=lambda data: result("new_incident", data),
        send_test_alert=lambda: result("send_test_alert"),
        run_precheck=lambda: result("run_precheck"),
        start_iperf_task=lambda data: result("start_iperf_task", data),
        stop_iperf_task=lambda data: result("stop_iperf_task", data),
        bridge_retire_resolve=lambda data: result("bridge_retire_resolve", data),
        test_dhcp_connection=lambda: result("test_dhcp_connection"),
        save_dhcp_settings=lambda data: result("save_dhcp_settings", data),
        update_incident=lambda incident_id, data: result(
            "update_incident", incident_id, data
        ),
    )


def call_names(calls):
    return [name for name, _args in calls]


def business_args(calls, name):
    return next(args for called, args in calls if called == name)


def test_write_api_module_imports_independently():
    assert write_api.WriteApiDependencies.__module__ == "platform_api.write_api"
    assert callable(write_api.handle_post)
    assert callable(write_api.handle_patch)


def test_public_auth_post_routes_keep_payload_cookie_and_client_contract():
    calls = []
    deps = build_dependencies(calls)

    handler = FakeHandler(calls)
    write_api.handle_post(
        handler,
        "/auth/login/?ignored=1",
        {"username": "admin", "password": "secret"},
        deps,
    )
    assert business_args(calls, "login_auth") == (
        "admin",
        "secret",
        "198.51.100.17",
    )
    assert "require_auth" not in call_names(calls)
    assert handler.sent == [
        (
            {"ok": True, "route": "login_auth"},
            200,
            {"Set-Cookie": "login-cookie"},
        )
    ]

    calls.clear()
    handler = FakeHandler(calls)
    data = {"currentPassword": "old", "newPassword": "new"}
    write_api.handle_post(handler, "/auth/change-password", data, deps)
    assert business_args(calls, "change_password_auth") == (handler, data)
    assert "require_auth" not in call_names(calls)
    assert handler.sent[0][2] == {"Set-Cookie": "changed-cookie"}

    calls.clear()
    handler = FakeHandler(calls)
    write_api.handle_post(handler, "/auth/logout", {}, deps)
    assert call_names(calls) == ["logout_auth", "clear_session_cookie", "send"]
    assert handler.sent == [
        (
            {"ok": True, "authenticated": False},
            200,
            {"Set-Cookie": "cleared-cookie"},
        )
    ]


def test_every_protected_post_route_dispatches_to_existing_business_function():
    cases = [
        ("/config/validate", {"text": "cfg"}, "config_payload", False),
        ("/incidents", {"title": "fixture"}, "new_incident", True),
        ("/test-alert", {}, "send_test_alert", False),
        ("/pre-check", {}, "run_precheck", False),
        ("/network/iperf3", {"server": "example.test"}, "start_iperf_task", False),
        ("/network/iperf3/stop", {"taskId": "task-1"}, "stop_iperf_task", False),
        (
            "/network/retire/resolve",
            {"key": "switch-1"},
            "bridge_retire_resolve",
            False,
        ),
        ("/network/dhcp/test", {}, "test_dhcp_connection", False),
        (
            "/network/dhcp/settings",
            {"username": "operator"},
            "save_dhcp_settings",
            True,
        ),
    ]

    for path, data, business, locked in cases:
        calls = []
        deps = build_dependencies(calls)
        handler = FakeHandler(calls)
        write_api.handle_post(handler, path, data, deps)
        names = call_names(calls)
        assert names[0] == "require_auth", path
        assert business in names, path
        assert names.index("require_auth") < names.index(business), path
        assert ("lock.enter" in names) is locked, path
        assert ("lock.exit" in names) is locked, path
        if locked:
            assert names.index("lock.enter") < names.index(business), path
            assert names.index(business) < names.index("lock.exit"), path
        assert handler.sent[0][1:] == (200, None), path

def test_config_routes_delegate_normalized_path_and_body_to_injected_domain():
    calls = []
    deps = build_dependencies(calls)
    handler = FakeHandler(calls)
    write_api.handle_post(
        handler,
        "/config/save/?ignored=1",
        {"text": "cfg", "note": "audit", "actor": "forged"},
        deps,
    )
    assert business_args(calls, "handle_config_post") == (
        handler,
        "/config/save",
        {"text": "cfg", "note": "audit", "actor": "forged"},
    )
    assert handler.sent == [
        ({"ok": True, "route": "handle_config_post"}, 200, None),
    ]


def test_incident_response_schema_remains_exact():
    calls = []
    deps = build_dependencies(calls)

    handler = FakeHandler(calls)
    data = {"title": "fixture"}
    write_api.handle_post(handler, "/incidents", data, deps)
    assert business_args(calls, "new_incident") == (data,)
    assert handler.sent[0][0] == {
        "ok": True,
        "incident": {"ok": True, "route": "new_incident"},
    }


def test_unknown_post_and_patch_keep_auth_body_and_status_ordering():
    calls = []
    deps = build_dependencies(calls)
    handler = FakeHandler(calls, {"status": "resolved"})

    write_api.handle_post(handler, "/unknown-write-route", {}, deps)
    assert call_names(calls) == ["send"]
    assert handler.sent == [
        ({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND, None)
    ]

    calls.clear()
    handler = FakeHandler(calls, {"status": "resolved"})
    write_api.handle_patch(handler, "/unknown-patch-route", deps)
    assert call_names(calls) == ["require_auth", "send"]
    assert handler.body_reads == 0
    assert handler.sent[0][1] == HTTPStatus.NOT_FOUND

    calls.clear()
    handler = FakeHandler(calls, {"status": "resolved"})
    write_api.handle_patch(handler, "/incidents/%31%37", deps)
    assert call_names(calls) == [
        "require_auth",
        "lock.enter",
        "body",
        "update_incident",
        "lock.exit",
        "send",
    ]
    assert business_args(calls, "update_incident") == (
        17,
        {"status": "resolved"},
    )
    assert handler.sent[0][0] == {
        "ok": True,
        "incident": {"ok": True, "route": "update_incident"},
    }


def run_server(api):
    server = HTTPServer(("127.0.0.1", 0), api.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_port}"


def request_raw(url, body, method="POST"):
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.headers, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, json.loads(exc.read())


def test_entrypoint_wiring_preserves_monkeypatch_and_body_error_contract(tmp_path):
    api = load_api(tmp_path)
    observed = {}

    def fake_save(_context, text, actor, note):
        observed.update(text=text, actor=actor, note=note)
        return {"ok": True, "saved": "fixture"}

    server, thread, base_url = run_server(api)
    try:
        with patch.object(api.platform_config_write, "save_config", fake_save):
            dependency = api._write_api_dependencies().handle_config_post
            assert dependency.func is api.platform_config_write.handle_post
            status, _, payload = request_raw(
                f"{base_url}/config/save",
                json.dumps({"text": "cfg", "note": "audit"}).encode(),
            )
        assert status == 200
        assert payload == {"ok": True, "saved": "fixture"}
        assert observed == {
            "text": "cfg",
            "actor": "local",
            "note": "audit",
        }

        status, _, payload = request_raw(
            f"{base_url}/unknown-post",
            b"{",
        )
        assert status == 400
        assert payload == {"ok": False, "error": "请求内容不是有效 JSON"}

        status, _, payload = request_raw(
            f"{base_url}/unknown-post",
            b"[]",
        )
        assert status == 400
        assert payload == {"ok": False, "error": "请求内容必须是 JSON 对象"}

        status, _, payload = request_raw(
            f"{base_url}/unknown-post",
            b"{}",
        )
        assert status == 404
        assert payload == {"ok": False, "error": "not found"}

        status, _, payload = request_raw(
            f"{base_url}/incidents/1",
            b"{",
            method="PATCH",
        )
        assert status == 400
        assert payload == {"ok": False, "error": "请求内容不是有效 JSON"}

        status, _, payload = request_raw(
            f"{base_url}/unknown-patch",
            b"{",
            method="PATCH",
        )
        assert status == 404
        assert payload == {"ok": False, "error": "not found"}
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_unauthenticated_write_routes_and_login_http_contract(tmp_path):
    api = load_platform_api(tmp_path)
    api.ensure_dirs()
    server, thread, base_url = run_server(api)
    try:
        status, _, payload = request_raw(
            f"{base_url}/config/validate",
            b'{"text":"event: {}"}',
        )
        assert status == 401
        assert payload == {
            "ok": False,
            "error": "需要登录",
            "authenticated": False,
        }

        # PATCH authentication still runs before path matching or body parsing.
        status, _, payload = request_raw(
            f"{base_url}/incidents/1",
            b"{",
            method="PATCH",
        )
        assert status == 401
        assert payload["error"] == "需要登录"

        status, headers, payload = request_raw(
            f"{base_url}/auth/login",
            b'{"username":"admin","password":"global123!@#"}',
        )
        assert status == 200
        assert payload["ok"] is True
        assert payload["authenticated"] is True
        assert payload["mustChangePassword"] is False
        assert "HttpOnly" in headers["Set-Cookie"]
    finally:
        server.shutdown()
        thread.join(timeout=5)
