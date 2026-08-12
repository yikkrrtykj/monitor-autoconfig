import json
import runpy
import threading
import urllib.error
import urllib.request
from http import server as http_server
from http.server import HTTPServer
from pathlib import Path

from platform_api import read_api

from .test_platform_auth import load_platform_api
from .test_platform_transactions import load_api


ROOT = Path(__file__).resolve().parents[1]


def request(url: str):
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def request_json(url: str):
    status, headers, body = request(url)
    return status, headers, json.loads(body.decode("utf-8"))


def run_server(api):
    server = HTTPServer(("127.0.0.1", 0), api.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_port}"


def stop_server(server, thread):
    server.shutdown()
    thread.join(timeout=5)


def test_read_api_module_imports_independently():
    assert read_api.ReadApiDependencies.__module__ == "platform_api.read_api"
    assert callable(read_api.handle_get)


def test_platform_api_remains_a_direct_docker_entrypoint(monkeypatch, tmp_path):
    observed = {}

    class FakeServer:
        def __init__(self, address, handler):
            observed.update(address=address, handler=handler)

        def serve_forever(self):
            observed["served"] = True

    workdir = tmp_path / "workspace"
    monkeypatch.setenv("PLATFORM_WORKDIR", str(workdir))
    monkeypatch.setenv("PLATFORM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("EVENT_CONFIG_FILE", str(workdir / "event-config.yml"))
    monkeypatch.setenv("EVENT_CONFIG_EXAMPLE", str(workdir / "event-config.example.yml"))
    monkeypatch.setenv("ENV_FILE", str(workdir / ".env"))
    monkeypatch.setenv("PLATFORM_API_PORT", "9200")
    monkeypatch.setattr(http_server, "ThreadingHTTPServer", FakeServer)

    runpy.run_path(str(ROOT / "platform-api.py"), run_name="__main__")

    assert observed["address"] == ("0.0.0.0", 9200)
    assert observed["handler"].__name__ == "Handler"
    assert observed["served"] is True


def test_public_read_routes_and_unknown_path_keep_http_contract(monkeypatch, tmp_path):
    api = load_api(tmp_path)
    monkeypatch.setattr(api.time, "time", lambda: 1_700_000_123.75)
    api.version_payload = lambda: {
        "ok": True,
        "platform_version": "2026.08.1",
        "git_commit": "fixture",
    }
    server, thread, base_url = run_server(api)
    try:
        status, headers, payload = request_json(f"{base_url}/health/")
        assert status == 200
        assert payload == {"ok": True, "time": 1_700_000_123}
        assert headers["Content-Type"] == "application/json; charset=utf-8"
        assert headers["Cache-Control"] == "no-store"
        assert headers["X-Content-Type-Options"] == "nosniff"

        status, _, payload = request_json(f"{base_url}/version?ignored=1")
        assert status == 200
        assert payload == {
            "ok": True,
            "platform_version": "2026.08.1",
            "git_commit": "fixture",
        }

        status, _, payload = request_json(f"{base_url}/unknown-read-route")
        assert status == 404
        assert payload == {"ok": False, "error": "not found"}
    finally:
        stop_server(server, thread)


def test_all_protected_read_routes_still_require_auth(tmp_path):
    api = load_platform_api(tmp_path)
    api.ensure_dirs()
    server, thread, base_url = run_server(api)
    try:
        protected_paths = (
            "/config",
            "/config/apply-status?operationId=fixture",
            "/incidents",
            "/network/iperf3/status?taskId=fixture",
            "/network/iperf3/history",
            "/network/dhcp/settings",
            "/network/dhcp/bindings",
            "/network/retire/pending",
            "/network/dhcp?force=1",
            "/config/download",
        )
        for path in protected_paths:
            status, _, payload = request_json(f"{base_url}{path}")
            assert status == 401, path
            assert payload == {
                "ok": False,
                "error": "需要登录",
                "authenticated": False,
            }, path
    finally:
        stop_server(server, thread)


def test_protected_read_routes_keep_paths_queries_and_payloads(monkeypatch, tmp_path):
    api = load_api(tmp_path)
    api.CONFIG_PATH = tmp_path / "event-config.yml"
    api.CONFIG_PATH.write_text("event:\n  name: fixture\n", encoding="utf-8")
    api.stamp = lambda: "fixture-stamp"
    history = [{"id": index} for index in range(25)]
    api.config_payload = lambda: {"ok": True, "config": {"name": "fixture"}}
    api.read_json_file = lambda path, default: history if path.name == "history.json" else default
    api.read_apply_status = lambda operation_id: {
        "ok": True,
        "operationId": operation_id,
    }
    api.write_json_file(
        api.INCIDENT_PATH,
        [{"id": 7, "title": "fixture"}],
    )
    api.iperf_status_payload = lambda task_id: {
        "ok": True,
        "taskId": task_id,
        "state": "complete",
    }
    api.iperf_history_payload = lambda: {"ok": True, "history": [{"taskId": "old"}]}
    api.get_dhcp_settings = lambda: {"ok": True, "host": "192.0.2.1"}
    api.get_dhcp_bindings = lambda: {"ok": True, "bindings": [{"ip": "192.0.2.10"}]}
    monkeypatch.setattr(
        api.platform_bridge,
        "bridge_retire_pending",
        lambda _bridge_url: {
            "ok": True,
            "pending": [{"key": "switch-1"}],
        },
    )
    observed_force = []

    def dhcp_dashboard(force=False):
        observed_force.append(force)
        return {"ok": True, "force": force}

    api.get_dhcp_dashboard = dhcp_dashboard
    server, thread, base_url = run_server(api)
    try:
        expected = {
            "/config": {
                "ok": True,
                "config": {"name": "fixture"},
                "history": history[:20],
            },
            "/config/apply-status?operationId=apply%2D123": {
                "ok": True,
                "operationId": "apply-123",
            },
            "/incidents": {
                "ok": True,
                "incidents": [{"id": 7, "title": "fixture"}],
            },
            "/network/iperf3/status?taskId=task%2D9": {
                "ok": True,
                "taskId": "task-9",
                "state": "complete",
            },
            "/network/iperf3/history": {
                "ok": True,
                "history": [{"taskId": "old"}],
            },
            "/network/dhcp/settings": {"ok": True, "host": "192.0.2.1"},
            "/network/dhcp/bindings": {
                "ok": True,
                "bindings": [{"ip": "192.0.2.10"}],
            },
            "/network/retire/pending": {
                "ok": True,
                "pending": [{"key": "switch-1"}],
            },
            "/network/dhcp?force=yes": {"ok": True, "force": True},
        }
        for path, expected_payload in expected.items():
            status, _, payload = request_json(f"{base_url}{path}")
            assert status == 200, path
            assert payload == expected_payload, path

        status, headers, body = request(f"{base_url}/config/download")
        assert status == 200
        assert headers["Content-Type"] == "application/x-yaml; charset=utf-8"
        assert headers["Content-Disposition"] == (
            'attachment; filename="event-config-fixture-stamp.yml"'
        )
        assert body == b"event:\n  name: fixture\n"
        assert observed_force == [True]
    finally:
        stop_server(server, thread)


def test_read_failure_keeps_existing_500_json_mapping(tmp_path):
    api = load_api(tmp_path)

    def fail_version():
        raise OSError("fixture version read failed")

    api.version_payload = fail_version
    server, thread, base_url = run_server(api)
    try:
        status, _, payload = request_json(f"{base_url}/version")
        assert status == 500
        assert payload == {"ok": False, "error": "fixture version read failed"}
    finally:
        stop_server(server, thread)
