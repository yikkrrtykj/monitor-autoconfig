import importlib.util
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "platform-api.py"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_platform_api(state_dir: Path):
    os.environ["PLATFORM_STATE_DIR"] = str(state_dir)
    os.environ["PLATFORM_ADMIN_USER"] = "admin"
    os.environ["PLATFORM_ADMIN_PASSWORD"] = "global"
    os.environ["PLATFORM_AUTH_ENABLED"] = "true"
    spec = importlib.util.spec_from_file_location("platform_api_auth_test", MODULE_PATH)
    platform_api = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(platform_api)
    return platform_api


def test_auth_store_defaults_and_password_change_rules():
    with tempfile.TemporaryDirectory() as tmp:
        api = load_platform_api(Path(tmp))
        api.ensure_dirs()
        store = api.read_auth_store()
        assert store["username"] == "admin"
        assert store["mustChangePassword"] is True
        assert api.verify_password("global", store["passwordHash"])
        assert api.password_strength_error("short")
        assert api.password_strength_error("global")
        assert api.password_strength_error("NoDigitsHere")
        assert api.password_strength_error("StrongPass2026") is None


def test_session_lifecycle():
    with tempfile.TemporaryDirectory() as tmp:
        api = load_platform_api(Path(tmp))
        api.ensure_dirs()
        token = api.create_session("admin")
        assert token in api.SESSIONS
        assert "HttpOnly" in api.session_cookie(token)
        assert "SameSite=Lax" in api.session_cookie(token)
        api.SESSIONS[token]["expires"] = 1
        api.prune_sessions()
        assert token not in api.SESSIONS


def request_json(url: str, payload=None, cookie: str = ""):
    body = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    if cookie:
        request.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.headers, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, json.loads(exc.read().decode("utf-8"))


def test_http_auth_flow():
    with tempfile.TemporaryDirectory() as tmp:
        api = load_platform_api(Path(tmp))
        api.ensure_dirs()
        server = HTTPServer(("127.0.0.1", 0), api.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            status, _, payload = request_json(f"{base_url}/auth/status")
            assert status == 200
            assert payload["authenticated"] is False

            status, _, payload = request_json(f"{base_url}/config")
            assert status == 401
            assert payload["error"] == "需要登录"

            status, _, payload = request_json(f"{base_url}/network/dhcp/settings")
            assert status == 401
            assert payload["error"] == "需要登录"

            status, _, payload = request_json(f"{base_url}/network/dhcp/test", {})
            assert status == 401
            assert payload["error"] == "需要登录"

            status, _, payload = request_json(f"{base_url}/network/iperf3/status")
            assert status == 401
            assert payload["error"] == "需要登录"

            status, headers, payload = request_json(f"{base_url}/auth/login", {
                "username": "admin",
                "password": "global",
            })
            assert status == 200
            assert payload["mustChangePassword"] is True
            cookie = headers["Set-Cookie"].split(";", 1)[0]

            status, _, payload = request_json(f"{base_url}/config", cookie=cookie)
            assert status == 403
            assert payload["mustChangePassword"] is True

            status, headers, payload = request_json(f"{base_url}/auth/change-password", {
                "currentPassword": "global",
                "newPassword": "StrongPass2026",
                "confirmPassword": "StrongPass2026",
            }, cookie=cookie)
            assert status == 200
            assert payload["mustChangePassword"] is False
            assert "HttpOnly" in headers["Set-Cookie"]
            cookie = headers["Set-Cookie"].split(";", 1)[0]

            api.CONFIG_PATH = Path(tmp) / "event-config.yml"
            api.CONFIG_PATH.write_text("devices:\n  core:\n    ip: 192.168.10.254\n", encoding="utf-8")
            status, _, payload = request_json(f"{base_url}/network/dhcp/settings", {
                "username": "cisco-admin",
                "password": "private-login-password",
                "enablePassword": "private-enable-password",
                "port": 23,
            }, cookie=cookie)
            assert status == 200
            assert payload["passwordConfigured"] is True
            assert payload["enablePasswordConfigured"] is True
            assert "password" not in payload
            assert "enablePassword" not in payload

            observed = {}

            def fake_save(text, actor, note):
                observed.update(text=text, actor=actor, note=note)
                return {"ok": True}

            api.save_config = fake_save
            status, _, payload = request_json(f"{base_url}/config/save", {
                "text": "event: {}",
                "actor": "forged-admin",
                "note": "audit",
            }, cookie=cookie)
            assert status == 200
            assert payload["ok"] is True
            assert observed["actor"] == "admin"

            api.MAX_REQUEST_BODY_BYTES = 8
            status, _, payload = request_json(f"{base_url}/auth/login", {
                "username": "admin",
                "password": "anything",
            })
            assert status == 413
            assert payload["error"] == "请求内容过大"
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_dhcp_get_preserves_diagnostic_http_status():
    with tempfile.TemporaryDirectory() as tmp:
        api = load_platform_api(Path(tmp))
        api.ensure_dirs()

        def fail_dashboard(_force=False):
            raise api.DiagnosticError(503, "尚未配置交换机密码")

        api.get_dhcp_dashboard = fail_dashboard
        server = HTTPServer(("127.0.0.1", 0), api.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status, _, payload = request_json(
                f"http://127.0.0.1:{server.server_port}/network/dhcp"
            )
            assert status == 503
            assert payload == {"ok": False, "error": "尚未配置交换机密码"}
        finally:
            server.shutdown()
            thread.join(timeout=5)


if __name__ == "__main__":
    test_auth_store_defaults_and_password_change_rules()
    test_session_lifecycle()
    test_http_auth_flow()
    test_dhcp_get_preserves_diagnostic_http_status()
    print("platform auth tests passed")
