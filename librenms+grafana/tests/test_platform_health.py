from platform_api import health

from .test_platform_read_api import request_json, run_server, stop_server
from .test_platform_transactions import load_api


def test_health_payload_preserves_schema_timestamp_and_clock_dependency():
    calls = []
    context = health.HealthContext(
        clock=lambda: calls.append("clock") or 1_700_000_123.75,
    )

    assert health.health_payload(context) == {
        "ok": True,
        "time": 1_700_000_123,
    }
    assert calls == ["clock"]


def test_entrypoint_builds_health_context_without_wrapper(tmp_path):
    api = load_api(tmp_path)
    context = api._health_context()

    assert isinstance(context, health.HealthContext)
    assert context.clock is api.time.time
    assert not hasattr(api, "health_payload")


def test_health_endpoint_preserves_path_headers_and_success_response(
    monkeypatch,
    tmp_path,
):
    api = load_api(tmp_path)
    monkeypatch.setattr(
        api,
        "_health_context",
        lambda: health.HealthContext(clock=lambda: 1_700_000_123.75),
    )
    server, thread, base_url = run_server(api)
    try:
        status, headers, payload = request_json(f"{base_url}/health/?ignored=1")
        assert status == 200
        assert payload == {"ok": True, "time": 1_700_000_123}
        assert headers["Content-Type"] == "application/json; charset=utf-8"
        assert headers["Cache-Control"] == "no-store"
        assert headers["X-Content-Type-Options"] == "nosniff"
    finally:
        stop_server(server, thread)


def test_health_dependency_failure_preserves_existing_500_mapping(
    monkeypatch,
    tmp_path,
):
    api = load_api(tmp_path)

    def fail_clock():
        raise OSError("fixture health clock failed")

    monkeypatch.setattr(
        api,
        "_health_context",
        lambda: health.HealthContext(clock=fail_clock),
    )
    server, thread, base_url = run_server(api)
    try:
        status, _, payload = request_json(f"{base_url}/health")
        assert status == 500
        assert payload == {
            "ok": False,
            "error": "fixture health clock failed",
        }
    finally:
        stop_server(server, thread)
