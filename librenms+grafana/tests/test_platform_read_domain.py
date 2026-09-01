from dataclasses import fields, replace
from types import SimpleNamespace

from platform_api import read_api


class Handler:
    def __init__(self):
        self.json_responses = []
        self.byte_responses = []

    def _send_json(self, payload, status=200):
        self.json_responses.append((status, payload))

    def _send_bytes(self, body, filename, content_type="application/zip"):
        self.byte_responses.append((body, filename, content_type))


def make_context(tmp_path, auth_calls=None):
    config_path = tmp_path / "event-config.yml"
    history_path = tmp_path / "history.json"
    auth_calls = [] if auth_calls is None else auth_calls
    return read_api.ReadApiContext(
        event_config_context=SimpleNamespace(config_path=config_path),
        transaction_context=SimpleNamespace(history_path=history_path),
        incident_context=object(),
        iperf_runtime_context=object(),
        dhcp_settings_context=object(),
        dhcp_runtime_context=object(),
        bridge_url="http://bridge.test",
        require_auth=lambda handler: auth_calls.append(handler) or {
            "username": "fixture",
        },
        read_json_file=lambda path, default: default,
        stamp=lambda: "fixture-stamp",
    )


def test_read_context_is_explicit_and_does_not_accept_entrypoint_globals():
    assert {field.name for field in fields(read_api.ReadApiContext)} == {
        "event_config_context",
        "transaction_context",
        "incident_context",
        "iperf_runtime_context",
        "dhcp_settings_context",
        "dhcp_runtime_context",
        "bridge_url",
        "require_auth",
        "read_json_file",
        "stamp",
    }


def test_config_and_version_reads_call_event_config_directly(
    monkeypatch,
    tmp_path,
):
    auth_calls = []
    context = make_context(tmp_path, auth_calls)
    history = [{"id": index} for index in range(25)]
    calls = []
    monkeypatch.setattr(
        read_api.event_config,
        "version_payload",
        lambda domain_context: (
            calls.append(("version", domain_context))
            or {"ok": True, "version": "fixture"}
        ),
    )
    monkeypatch.setattr(
        read_api.event_config,
        "config_payload",
        lambda domain_context: (
            calls.append(("config", domain_context))
            or {"ok": True, "config": {"name": "fixture"}}
        ),
    )
    context = replace(
        context,
        read_json_file=lambda path, default: (
            history
            if path is context.transaction_context.history_path
            else default
        ),
    )
    handler = Handler()

    read_api.handle_get(handler, "/version?ignored=1", context)
    read_api.handle_get(handler, "/config", context)

    assert handler.json_responses == [
        (200, {"ok": True, "version": "fixture"}),
        (
            200,
            {
                "ok": True,
                "config": {"name": "fixture"},
                "history": history[:20],
            },
        ),
    ]
    assert calls == [
        ("version", context.event_config_context),
        ("config", context.event_config_context),
    ]
    assert auth_calls == [handler]


def test_incident_transaction_and_retire_reads_delegate_to_domains(
    monkeypatch,
    tmp_path,
):
    context = make_context(tmp_path)
    calls = []
    monkeypatch.setattr(
        read_api.incidents,
        "incident_list",
        lambda domain_context: (
            calls.append(("incidents", domain_context))
            or [{"id": 7}]
        ),
    )
    monkeypatch.setattr(
        read_api.config_transaction,
        "read_apply_status",
        lambda domain_context, operation_id: (
            calls.append(("status", domain_context, operation_id))
            or {"ok": True, "operationId": operation_id}
        ),
    )
    monkeypatch.setattr(
        read_api.bridge,
        "bridge_retire_pending",
        lambda bridge_url: (
            calls.append(("retire", bridge_url))
            or {"ok": True, "enabled": True, "pending": []}
        ),
    )
    handler = Handler()

    read_api.handle_get(handler, "/incidents", context)
    read_api.handle_get(
        handler,
        "/config/apply-status?operationId=apply%2D123",
        context,
    )
    read_api.handle_get(handler, "/network/retire/pending", context)

    assert handler.json_responses == [
        (200, {"ok": True, "incidents": [{"id": 7}]}),
        (200, {"ok": True, "operationId": "apply-123"}),
        (200, {"ok": True, "enabled": True, "pending": []}),
    ]
    assert calls == [
        ("incidents", context.incident_context),
        ("status", context.transaction_context, "apply-123"),
        ("retire", context.bridge_url),
    ]


def test_dhcp_reads_preserve_context_force_and_auth_order(monkeypatch, tmp_path):
    auth_calls = []
    context = make_context(tmp_path, auth_calls)
    calls = []
    monkeypatch.setattr(
        read_api.dhcp_settings,
        "get_dhcp_settings",
        lambda domain_context: (
            calls.append(("settings", domain_context)) or {"settings": True}
        ),
    )
    monkeypatch.setattr(
        read_api.dhcp_runtime,
        "get_dhcp_bindings",
        lambda domain_context: (
            calls.append(("bindings", domain_context)) or {"bindings": []}
        ),
    )
    monkeypatch.setattr(
        read_api.dhcp_runtime,
        "get_dhcp_dashboard",
        lambda domain_context, force=False: (
            calls.append(("dashboard", domain_context, force))
            or {"force": force}
        ),
    )
    handler = Handler()

    read_api.handle_get(handler, "/network/dhcp/settings", context)
    read_api.handle_get(handler, "/network/dhcp/bindings", context)
    read_api.handle_get(handler, "/network/dhcp?force=yes", context)

    assert handler.json_responses == [
        (200, {"settings": True}),
        (200, {"bindings": []}),
        (200, {"force": True}),
    ]
    assert calls == [
        ("settings", context.dhcp_settings_context),
        ("bindings", context.dhcp_runtime_context),
        ("dashboard", context.dhcp_runtime_context, True),
    ]
    assert auth_calls == [handler, handler, handler]


def test_iperf_reads_preserve_task_query_and_runtime_context(
    monkeypatch,
    tmp_path,
):
    context = make_context(tmp_path)
    calls = []
    monkeypatch.setattr(
        read_api.iperf_runtime,
        "iperf_status_payload",
        lambda domain_context, task_id: (
            calls.append(("status", domain_context, task_id))
            or {"taskId": task_id}
        ),
    )
    monkeypatch.setattr(
        read_api.iperf_runtime,
        "iperf_history_payload",
        lambda domain_context: (
            calls.append(("history", domain_context)) or {"history": []}
        ),
    )
    handler = Handler()

    read_api.handle_get(
        handler,
        "/network/iperf3/status?taskId=task%2D9",
        context,
    )
    read_api.handle_get(handler, "/network/iperf3/history", context)

    assert handler.json_responses == [
        (200, {"taskId": "task-9"}),
        (200, {"history": []}),
    ]
    assert calls == [
        ("status", context.iperf_runtime_context, "task-9"),
        ("history", context.iperf_runtime_context),
    ]


def test_health_and_auth_status_are_not_owned_by_read_domain(tmp_path):
    context = make_context(tmp_path)
    handler = Handler()

    read_api.handle_get(handler, "/health", context)
    read_api.handle_get(handler, "/auth/status", context)

    assert handler.json_responses == [
        (404, {"ok": False, "error": "not found"}),
        (404, {"ok": False, "error": "not found"}),
    ]
