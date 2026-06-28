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
    os.environ["PLATFORM_ADMIN_PASSWORD"] = "Event@2026!"
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
        assert api.verify_password("Event@2026!", store["passwordHash"])
        assert api.password_strength_error("short")
        assert api.password_strength_error("Event@2026!")
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

            status, headers, payload = request_json(f"{base_url}/auth/login", {
                "username": "admin",
                "password": "Event@2026!",
            })
            assert status == 200
            assert payload["mustChangePassword"] is True
            cookie = headers["Set-Cookie"].split(";", 1)[0]

            status, _, payload = request_json(f"{base_url}/config", cookie=cookie)
            assert status == 403
            assert payload["mustChangePassword"] is True

            status, headers, payload = request_json(f"{base_url}/auth/change-password", {
                "currentPassword": "Event@2026!",
                "newPassword": "StrongPass2026",
                "confirmPassword": "StrongPass2026",
            }, cookie=cookie)
            assert status == 200
            assert payload["mustChangePassword"] is False
            assert "HttpOnly" in headers["Set-Cookie"]
        finally:
            server.shutdown()
            thread.join(timeout=5)


if __name__ == "__main__":
    test_auth_store_defaults_and_password_change_rules()
    test_session_lifecycle()
    test_http_auth_flow()
    print("platform auth tests passed")
