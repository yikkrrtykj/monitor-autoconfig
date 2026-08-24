import json
from pathlib import Path

import pytest

from platform_api import incidents

from .test_platform_auth import load_platform_api, request_json
from .test_platform_transactions import load_api
from .test_platform_write_api import request_raw, run_server


FIXED_TIME = 1_700_000_123.75


def incident_context(
    tmp_path: Path,
    require_write=lambda: None,
    clock=lambda: FIXED_TIME,
) -> incidents.IncidentContext:
    return incidents.IncidentContext(
        incident_path=tmp_path / "state" / "incidents.json",
        require_write=require_write,
        clock=clock,
    )


def seed_incidents(context: incidents.IncidentContext, items) -> None:
    context.incident_path.parent.mkdir(parents=True, exist_ok=True)
    context.incident_path.write_text(
        json.dumps(items, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def test_incidents_module_imports_without_touching_storage(tmp_path):
    context = incident_context(tmp_path)

    assert incidents.IncidentContext.__module__ == "platform_api.incidents"
    assert incidents.new_incident.__module__ == "platform_api.incidents"
    assert incidents.update_incident.__module__ == "platform_api.incidents"
    assert not context.incident_path.parent.exists()


def test_empty_corrupt_and_legacy_incident_files_keep_read_behavior(tmp_path):
    context = incident_context(tmp_path)

    assert incidents.incident_list(context) == []
    assert not context.incident_path.exists()

    seed_incidents(context, [])
    context.incident_path.write_text("{broken", encoding="utf-8")
    assert incidents.incident_list(context) == []

    legacy_payload = {
        "legacy": True,
        "note": "The existing reader does not coerce the JSON root shape.",
    }
    seed_incidents(context, legacy_payload)
    assert incidents.incident_list(context) == legacy_payload


def test_incident_read_io_errors_still_propagate(monkeypatch, tmp_path):
    context = incident_context(tmp_path)

    def fail_read(_path, _fallback):
        raise OSError("fixture read failure")

    monkeypatch.setattr(incidents, "read_json_file", fail_read)
    with pytest.raises(OSError, match="fixture read failure"):
        incidents.incident_list(context)


def test_create_incident_defaults_schema_timestamp_and_persistence(tmp_path):
    writes = []
    context = incident_context(tmp_path, require_write=lambda: writes.append("guard"))

    created = incidents.new_incident(context, {})

    assert created == {
        "id": 1,
        "title": "未命名事故",
        "severity": "warn",
        "status": "open",
        "scope": "",
        "owner": "",
        "rootCause": "",
        "startedAt": 1_700_000_123,
        "recoveredAt": None,
        "related": {},
        "events": [
            {
                "time": 1_700_000_123,
                "type": "note",
                "message": "事故创建",
            }
        ],
    }
    assert writes == ["guard"]
    assert incidents.incident_list(context) == [created]
    assert json.loads(context.incident_path.read_text(encoding="utf-8")) == [
        created
    ]
    assert not context.incident_path.with_suffix(".json.tmp").exists()


def test_create_keeps_id_order_fields_and_permissive_status(tmp_path):
    context = incident_context(tmp_path)
    existing = [
        {"id": "7", "title": "legacy", "legacyField": "preserve"},
        {"id": 2, "title": "older"},
    ]
    seed_incidents(context, existing)
    supplied_events = [{"time": 123, "type": "custom", "message": "kept"}]

    created = incidents.new_incident(
        context,
        {
            "title": "fixture",
            "severity": "critical-custom",
            "status": "arbitrary-existing-state",
            "scope": "stage",
            "owner": "operator",
            "rootCause": "fixture cause",
            "startedAt": 456,
            "recoveredAt": 789,
            "related": {"device": "switch-1"},
            "events": supplied_events,
        },
    )

    assert created == {
        "id": 8,
        "title": "fixture",
        "severity": "critical-custom",
        "status": "arbitrary-existing-state",
        "scope": "stage",
        "owner": "operator",
        "rootCause": "fixture cause",
        "startedAt": 456,
        "recoveredAt": 789,
        "related": {"device": "switch-1"},
        "events": supplied_events,
    }
    assert incidents.incident_list(context) == [created, *existing]


def test_update_keeps_order_allowed_fields_event_and_ignored_fields(tmp_path):
    context = incident_context(tmp_path)
    original = [
        {"id": 9, "title": "newer", "events": []},
        {
            "id": 3,
            "title": "old title",
            "status": "open",
            "startedAt": 111,
            "events": [{"time": 100, "type": "note", "message": "original"}],
            "legacyField": "preserve",
        },
    ]
    seed_incidents(context, original)

    updated = incidents.update_incident(
        context,
        3,
        {
            "title": "updated",
            "severity": "custom",
            "status": "another-arbitrary-state",
            "scope": "arena",
            "owner": "network-team",
            "rootCause": "known",
            "recoveredAt": 0,
            "related": None,
            "event": "recovered",
            "eventType": "status",
            "startedAt": 999,
            "events": [],
            "ignoredField": "ignored",
        },
    )

    assert updated == {
        "id": 3,
        "title": "updated",
        "severity": "custom",
        "status": "another-arbitrary-state",
        "scope": "arena",
        "owner": "network-team",
        "rootCause": "known",
        "startedAt": 111,
        "recoveredAt": 0,
        "related": None,
        "events": [
            {"time": 100, "type": "note", "message": "original"},
            {
                "time": 1_700_000_123,
                "type": "status",
                "message": "recovered",
            },
        ],
        "legacyField": "preserve",
    }
    persisted = incidents.incident_list(context)
    assert [item["id"] for item in persisted] == [9, 3]
    assert persisted[1] == updated


def test_missing_update_malformed_input_and_write_guard_errors_are_unchanged(
    tmp_path,
):
    context = incident_context(tmp_path)
    seed_incidents(context, [{"id": 1, "title": "existing"}])
    before = context.incident_path.read_bytes()

    with pytest.raises(KeyError, match="incident 99 not found"):
        incidents.update_incident(context, 99, {})
    assert context.incident_path.read_bytes() == before

    with pytest.raises(AttributeError):
        incidents.new_incident(context, None)
    assert context.incident_path.read_bytes() == before

    def deny_write():
        raise PermissionError("platform write endpoints are disabled")

    disabled_context = incident_context(tmp_path, require_write=deny_write)
    with pytest.raises(PermissionError, match="write endpoints are disabled"):
        incidents.new_incident(disabled_context, {})
    assert context.incident_path.read_bytes() == before


def test_entrypoint_incident_dependencies_keep_path_clock_guard_and_lock(tmp_path):
    api = load_api(tmp_path)
    read_context = api._read_api_context()
    write_dependencies = api._write_api_dependencies()

    read_incident_context = read_context.incident_context
    create_context = write_dependencies.new_incident.args[0]
    update_context = write_dependencies.update_incident.args[0]
    for context in (read_incident_context, create_context, update_context):
        assert context.incident_path == api.INCIDENT_PATH
        assert context.require_write is api.require_write
        assert context.clock is api.time.time
    assert write_dependencies.write_lock is api.WRITE_LOCK
    assert write_dependencies.new_incident.func is incidents.new_incident
    assert write_dependencies.update_incident.func is incidents.update_incident


def test_incident_get_post_patch_http_schema_and_persistence(monkeypatch, tmp_path):
    api = load_api(tmp_path)
    monkeypatch.setattr(api.time, "time", lambda: FIXED_TIME)
    server, thread, base_url = run_server(api)
    try:
        status, headers, payload = request_json(f"{base_url}/incidents")
        assert status == 200
        assert headers["Cache-Control"] == "no-store"
        assert payload == {"ok": True, "incidents": []}

        status, _, payload = request_json(
            f"{base_url}/incidents",
            {"title": "HTTP fixture", "note": "created"},
        )
        assert status == 200
        created = payload["incident"]
        assert payload == {"ok": True, "incident": created}
        assert created["id"] == 1
        assert created["title"] == "HTTP fixture"
        assert created["events"] == [
            {
                "time": 1_700_000_123,
                "type": "note",
                "message": "created",
            }
        ]

        status, _, patched_payload = request_raw(
            f"{base_url}/incidents/1",
            b'{"status":"closed","event":"recovered","eventType":"status"}',
            method="PATCH",
        )
        assert status == 200
        assert patched_payload["ok"] is True
        assert patched_payload["incident"]["status"] == "closed"
        assert patched_payload["incident"]["events"][-1] == {
            "time": 1_700_000_123,
            "type": "status",
            "message": "recovered",
        }

        status, _, listed = request_json(f"{base_url}/incidents")
        assert status == 200
        assert listed == {
            "ok": True,
            "incidents": [patched_payload["incident"]],
        }

        status, _, missing = request_raw(
            f"{base_url}/incidents/99",
            b"{}",
            method="PATCH",
        )
        assert status == 404
        assert missing == {
            "ok": False,
            "error": "'incident 99 not found'",
        }

        status, _, malformed = request_raw(
            f"{base_url}/incidents",
            b"[]",
        )
        assert status == 400
        assert malformed == {
            "ok": False,
            "error": "请求内容必须是 JSON 对象",
        }
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert json.loads(api.INCIDENT_PATH.read_text(encoding="utf-8")) == [
        patched_payload["incident"]
    ]


def test_incident_http_auth_contract_is_unchanged(tmp_path):
    api = load_platform_api(tmp_path)
    api.ensure_dirs()
    server, thread, base_url = run_server(api)
    expected = {
        "ok": False,
        "error": "需要登录",
        "authenticated": False,
    }
    try:
        status, _, payload = request_json(f"{base_url}/incidents")
        assert (status, payload) == (401, expected)

        status, _, payload = request_json(f"{base_url}/incidents", {})
        assert (status, payload) == (401, expected)

        status, _, payload = request_raw(
            f"{base_url}/incidents/1",
            b"{}",
            method="PATCH",
        )
        assert (status, payload) == (401, expected)
    finally:
        server.shutdown()
        thread.join(timeout=5)
