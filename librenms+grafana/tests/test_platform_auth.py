import importlib.util
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from functools import partial
from http.server import HTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "platform-api.py"
CONFIG_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "config"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from platform_api import auth


def load_platform_api(state_dir: Path):
    workdir = state_dir / "workspace"
    workdir.mkdir(exist_ok=True)
    os.environ.update({
        "PLATFORM_STATE_DIR": str(state_dir / "state"),
        "PLATFORM_WORKDIR": str(workdir),
        "EVENT_CONFIG_FILE": str(workdir / "event-config.yml"),
        "EVENT_CONFIG_EXAMPLE": str(workdir / "event-config.example.yml"),
        "ENV_FILE": str(workdir / ".env"),
        "PLATFORM_ADMIN_USER": "admin",
        "PLATFORM_ADMIN_PASSWORD": "global",
        "PLATFORM_AUTH_ENABLED": "true",
    })
    spec = importlib.util.spec_from_file_location("platform_api_auth_test", MODULE_PATH)
    platform_api = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(platform_api)
    return platform_api


def test_auth_store_defaults_and_password_change_rules():
    with tempfile.TemporaryDirectory() as tmp:
        api = load_platform_api(Path(tmp))
        api.ensure_dirs()
        context = api.AUTH_CONTEXT
        store = auth.read_auth_store(context)
        assert store["username"] == "admin"
        assert store["mustChangePassword"] is True
        assert auth.verify_password("global", store["passwordHash"])
        assert auth.password_strength_error(context, "short")
        assert auth.password_strength_error(context, "global")
        assert auth.password_strength_error(context, "NoDigitsHere")
        assert auth.password_strength_error(context, "StrongPass2026") is None


def test_session_lifecycle():
    with tempfile.TemporaryDirectory() as tmp:
        api = load_platform_api(Path(tmp))
        api.ensure_dirs()
        context = api.AUTH_CONTEXT
        token = auth.create_session(context, "admin")
        assert token in context.sessions
        assert "HttpOnly" in auth.session_cookie(context, token)
        assert "SameSite=Lax" in auth.session_cookie(context, token)
        context.sessions[token]["expires"] = 1
        auth.prune_sessions(context)
        assert token not in context.sessions


def test_login_failures_lock_both_ip_and_account():
    with tempfile.TemporaryDirectory() as tmp:
        api = load_platform_api(Path(tmp))
        api.ensure_dirs()
        context = api.AUTH_CONTEXT
        context.failure_limit = 3
        context.lock_seconds = 60
        context.failures.clear()

        for _attempt in range(2):
            try:
                auth.login_auth(context, "admin", "wrong", "192.0.2.10")
            except auth.AuthError as exc:
                assert exc.status == 401
        try:
            auth.login_auth(context, "admin", "wrong", "192.0.2.10")
        except auth.AuthError as exc:
            assert exc.status == 429
            assert exc.payload["retryAfter"] > 0
        else:
            raise AssertionError("third failed login must lock the source")

        try:
            auth.login_auth(context, "admin", "global", "192.0.2.11")
        except auth.AuthError as exc:
            assert exc.status == 429  # account-wide lock also applies
        else:
            raise AssertionError("locked account must reject a correct password")


def test_auth_status_valid_invalid_and_permission_flows():
    with tempfile.TemporaryDirectory() as tmp:
        api = load_platform_api(Path(tmp))
        api.ensure_dirs()
        context = api.AUTH_CONTEXT

        class Handler:
            def __init__(self, cookie=""):
                self.headers = {"Cookie": cookie}

        anonymous = Handler()
        assert auth.auth_status(context, anonymous) == {
            "ok": True,
            "enabled": True,
            "authenticated": False,
            "username": "",
            "defaultUser": "admin",
            "mustChangePassword": False,
            "sessionExpiresAt": 0,
        }
        try:
            auth.require_auth(context, anonymous)
        except auth.AuthError as exc:
            assert exc.status == 401
            assert exc.payload == {
                "ok": False,
                "error": "需要登录",
                "authenticated": False,
            }
        else:
            raise AssertionError("anonymous request must be rejected")

        try:
            auth.login_auth(context, "admin", "wrong", "192.0.2.20")
        except auth.AuthError as exc:
            assert exc.status == 401
            assert exc.payload["error"] == "账号或密码错误"
        else:
            raise AssertionError("invalid credentials must be rejected")

        payload, cookie = auth.login_auth(
            context,
            "admin",
            "global",
            "192.0.2.20",
        )
        assert payload["authenticated"] is True
        authenticated = Handler(cookie.split(";", 1)[0])
        assert auth.auth_status(context, authenticated)["authenticated"] is True
        try:
            auth.require_auth(context, authenticated)
        except auth.AuthError as exc:
            assert exc.status == 403
            assert exc.payload["mustChangePassword"] is True
        else:
            raise AssertionError("default password must keep writes blocked")

        assert auth.require_auth(
            context,
            authenticated,
            allow_must_change=True,
        ) == {"username": "admin"}


def test_entrypoint_uses_one_auth_context_without_compatibility_wrappers():
    with tempfile.TemporaryDirectory() as tmp:
        api = load_platform_api(Path(tmp))
        context = api.AUTH_CONTEXT
        read_context = api._read_api_context()
        config_context = api._config_write_context()
        write_dependencies = api._write_api_dependencies()

        assert isinstance(context, auth.AuthContext)
        for dependency, function in (
            (read_context.require_auth, auth.require_auth),
            (config_context.require_auth, auth.require_auth),
            (write_dependencies.login_auth, auth.login_auth),
            (write_dependencies.change_password_auth, auth.change_password_auth),
            (write_dependencies.logout_auth, auth.logout_auth),
            (write_dependencies.clear_session_cookie, auth.clear_session_cookie),
            (write_dependencies.require_auth, auth.require_auth),
        ):
            assert isinstance(dependency, partial)
            assert dependency.func is function
            assert dependency.args == (context,)

        for name in (
            "SESSIONS",
            "AUTH_FAILURES",
            "AUTH_FAILURES_LOCK",
            "AuthError",
            "_sync_auth_context",
            "ensure_auth_store",
            "read_auth_store",
            "write_auth_store",
            "password_strength_error",
            "prune_sessions",
            "create_session",
            "current_session",
            "session_cookie",
            "clear_session_cookie",
            "auth_status",
            "require_auth",
            "login_auth",
            "change_password_auth",
            "logout_auth",
        ):
            assert not hasattr(api, name)


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

            api.CONFIG_PATH = Path(tmp) / "event-config.yml"
            api.CONFIG_PATH.write_text(
                "devices:\n  core:\n    ip: 192.168.10.254\nalerts:\n  feishu_app_secret: never-return-this\n",
                encoding="utf-8",
            )
            api.get_version_info = lambda: {
                "platform_version": (ROOT.parent / "VERSION").read_text(encoding="utf-8").strip(),
                "git_commit": "abcdef1",
                "config_schema_supported": 1,
            }
            status, _, payload = request_json(f"{base_url}/version")
            assert status == 200
            assert payload == {
                "ok": True,
                "platform_version": (ROOT.parent / "VERSION").read_text(encoding="utf-8").strip(),
                "git_commit": "abcdef1",
                "config_schema_supported": 1,
                "config_schema_original": 0,
                "config_schema_current": 1,
                "migration_required": True,
                "config_too_new": False,
            }
            assert "never-return-this" not in json.dumps(payload)

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

            status, _, payload = request_json(f"{base_url}/delivery/manifest", cookie=cookie)
            assert status == 404
            assert payload == {"ok": False, "error": "not found"}

            # Exercise the actual HTTP handler, transaction and rollback chain
            # with the same config fixtures used by schema tests.
            original_config = (CONFIG_FIXTURES / "event-config-v0.yml").read_text(
                encoding="utf-8"
            )
            original_env = "CUSTOM=http-old\nFEISHU_APP_SECRET=http-fixture-secret\n"
            api.CONFIG_PATH.write_text(original_config, encoding="utf-8")
            api.ENV_PATH.write_text(original_env, encoding="utf-8")
            outcomes = iter([
                {"ok": False, "error": "compose failed", "applyOutput": "bad"},
                {"applied": True, "needsRedeploy": False, "applyOutput": "restored"},
            ])
            with patch.object(
                api.platform_apply_runtime,
                "run_apply_command",
                side_effect=lambda _context: next(outcomes),
            ):
                status, _, payload = request_json(f"{base_url}/config/apply", {
                    "text": (CONFIG_FIXTURES / "event-config-v1.yml").read_text(
                        encoding="utf-8"
                    ),
                    "operationId": "http-apply-failure",
                }, cookie=cookie)
            assert status == 200
            assert payload["ok"] is False
            assert payload["rolledBack"] is True
            assert api.CONFIG_PATH.read_text(encoding="utf-8") == original_config
            assert api.ENV_PATH.read_text(encoding="utf-8") == original_env

            before_config = api.CONFIG_PATH.read_bytes()
            before_env = api.ENV_PATH.read_bytes()
            status, _, payload = request_json(f"{base_url}/config/save", {
                "text": (CONFIG_FIXTURES / "event-config-future-v2.yml").read_text(
                    encoding="utf-8"
                ),
            }, cookie=cookie)
            assert status == 200
            assert payload["ok"] is False
            assert payload["configTooNew"] is True
            assert api.CONFIG_PATH.read_bytes() == before_config
            assert api.ENV_PATH.read_bytes() == before_env

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

            def fake_save(_context, text, actor, note):
                observed.update(text=text, actor=actor, note=note)
                return {"ok": True}

            with patch.object(api.platform_config_write, "save_config", fake_save):
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

        def fail_dashboard(_context, _force=False):
            raise api.DiagnosticError(503, "尚未配置交换机密码")

        with patch.object(
            api.platform_dhcp_runtime,
            "get_dhcp_dashboard",
            fail_dashboard,
        ):
            server = HTTPServer(("127.0.0.1", 0), api.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_port}"
                # 未登录必须 401：这个接口会驱动对核心交换机的特权 Telnet 会话
                status, _, payload = request_json(f"{base_url}/network/dhcp")
                assert status == 401
                assert payload["error"] == "需要登录"

                status, headers, _ = request_json(f"{base_url}/auth/login", {
                    "username": "admin",
                    "password": "global",
                })
                assert status == 200
                cookie = headers["Set-Cookie"].split(";", 1)[0]
                status, headers, _ = request_json(f"{base_url}/auth/change-password", {
                    "currentPassword": "global",
                    "newPassword": "StrongPass2026",
                    "confirmPassword": "StrongPass2026",
                }, cookie=cookie)
                assert status == 200
                cookie = headers["Set-Cookie"].split(";", 1)[0]

                # 登录后 DiagnosticError 的 HTTP 状态原样透传
                status, _, payload = request_json(f"{base_url}/network/dhcp", cookie=cookie)
                assert status == 503
                assert payload == {"ok": False, "error": "尚未配置交换机密码"}
            finally:
                server.shutdown()
                thread.join(timeout=5)


if __name__ == "__main__":
    test_auth_store_defaults_and_password_change_rules()
    test_session_lifecycle()
    test_login_failures_lock_both_ip_and_account()
    test_auth_status_valid_invalid_and_permission_flows()
    test_entrypoint_uses_one_auth_context_without_compatibility_wrappers()
    test_http_auth_flow()
    test_dhcp_get_preserves_diagnostic_http_status()
    print("platform auth tests passed")
