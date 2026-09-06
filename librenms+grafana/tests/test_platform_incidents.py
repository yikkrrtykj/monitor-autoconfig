import json
from concurrent.futures import ThreadPoolExecutor
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


def test_missing_file_is_empty_but_corrupt_or_non_list_storage_fails(tmp_path):
    context = incident_context(tmp_path)

    assert incidents.incident_list(context) == []
    assert not context.incident_path.exists()

    seed_incidents(context, [])
    context.incident_path.write_text("{broken", encoding="utf-8")
    with pytest.raises(incidents.IncidentStorageError, match="不是有效 JSON"):
        incidents.incident_list(context)

    legacy_payload = {
        "legacy": True,
        "note": "non-list roots are unsafe for incident operations",
    }
    seed_incidents(context, legacy_payload)
    with pytest.raises(incidents.IncidentStorageError, match="根节点必须是列表"):
        incidents.incident_list(context)


def test_incident_read_io_errors_still_propagate(monkeypatch, tmp_path):
    context = incident_context(tmp_path)

    def fail_open(*_args, **_kwargs):
        raise OSError("fixture read failure with private path")

    monkeypatch.setattr(Path, "open", fail_open)
    with pytest.raises(incidents.IncidentStorageError, match="事故存储不可读取") as caught:
        incidents.incident_list(context)
    assert "private path" not in str(caught.value)


def test_incident_reader_is_byte_bounded_before_parse(monkeypatch, tmp_path):
    context = incident_context(tmp_path)
    context.incident_path.parent.mkdir(parents=True)
    context.incident_path.write_bytes(b"[" + b" " * 8 + b"]")
    monkeypatch.setattr(incidents, "MAX_INCIDENT_FILE_BYTES", 8)

    with pytest.raises(incidents.IncidentStorageError, match="超过 8 字节读取上限"):
        incidents.incident_list(context)


def test_incident_reader_requests_only_limit_plus_one_bytes(monkeypatch, tmp_path):
    reads = []

    class Handle:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, size):
            reads.append(size)
            return b"[]"

    class IncidentPath:
        def open(self, mode):
            assert mode == "rb"
            return Handle()

    monkeypatch.setattr(incidents, "MAX_INCIDENT_FILE_BYTES", 8)
    context = incidents.IncidentContext(
        incident_path=IncidentPath(), require_write=lambda: None, clock=lambda: FIXED_TIME,
    )
    assert incidents.incident_list(context) == []
    assert reads == [9]


@pytest.mark.parametrize("payload, message", [
    (b'\xff', "不是有效 UTF-8"),
    (b'[1]', "第 1 条记录不是对象"),
    (b'[{"id":"bad"}]', "第 1 条记录 ID 无效"),
    (b'[{"id":1,"events":{}}]', "第 1 条记录 events 不是列表"),
])
def test_incident_reader_rejects_unsafe_existing_records(tmp_path, payload, message):
    context = incident_context(tmp_path)
    context.incident_path.parent.mkdir(parents=True)
    context.incident_path.write_bytes(payload)
    with pytest.raises(incidents.IncidentStorageError, match=message):
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


def test_create_enforces_count_and_event_limits_without_overwriting(monkeypatch, tmp_path):
    context = incident_context(tmp_path)
    seed_incidents(context, [{"id": 1, "events": []}])
    before = context.incident_path.read_bytes()
    monkeypatch.setattr(incidents, "MAX_INCIDENTS", 1)
    with pytest.raises(incidents.IncidentCapacityError, match="事故数量达到上限 1"):
        incidents.new_incident(context, {})
    assert context.incident_path.read_bytes() == before

    empty_context = incident_context(tmp_path / "events")
    monkeypatch.setattr(incidents, "MAX_INCIDENTS", 1000)
    monkeypatch.setattr(incidents, "MAX_EVENTS_PER_INCIDENT", 2)
    incidents.new_incident(empty_context, {"events": [{}, {}]})
    before = empty_context.incident_path.read_bytes()
    with pytest.raises(incidents.IncidentCapacityError, match="events 数量超过上限 2"):
        incidents.new_incident(empty_context, {"events": [{}, {}, {}]})
    assert empty_context.incident_path.read_bytes() == before


def test_utf8_incident_and_file_byte_limits_are_exact(monkeypatch, tmp_path):
    template_context = incident_context(tmp_path / "template")
    template = incidents.new_incident(template_context, {"title": "中文事故"})
    incident_bytes = len(json.dumps(template, ensure_ascii=False, indent=2).encode("utf-8"))
    file_bytes = len(json.dumps([template], ensure_ascii=False, indent=2).encode("utf-8"))
    assert len(json.dumps(template, ensure_ascii=False, indent=2)) < incident_bytes

    exact_context = incident_context(tmp_path / "exact")
    monkeypatch.setattr(incidents, "MAX_INCIDENT_BYTES", incident_bytes)
    monkeypatch.setattr(incidents, "MAX_INCIDENT_FILE_BYTES", file_bytes)
    assert incidents.new_incident(exact_context, {"title": "中文事故"}) == template
    assert incidents.incident_list(exact_context) == [template]

    incident_over = incident_context(tmp_path / "incident-over")
    monkeypatch.setattr(incidents, "MAX_INCIDENT_BYTES", incident_bytes - 1)
    with pytest.raises(incidents.IncidentCapacityError, match="单条事故序列化字节数"):
        incidents.new_incident(incident_over, {"title": "中文事故"})
    assert not incident_over.incident_path.exists()

    file_over = incident_context(tmp_path / "file-over")
    monkeypatch.setattr(incidents, "MAX_INCIDENT_BYTES", incident_bytes)
    monkeypatch.setattr(incidents, "MAX_INCIDENT_FILE_BYTES", file_bytes - 1)
    with pytest.raises(incidents.IncidentCapacityError, match="候选文件序列化字节数"):
        incidents.new_incident(file_over, {"title": "中文事故"})
    assert not file_over.incident_path.exists()


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


def test_update_is_atomic_when_event_or_record_would_overflow(monkeypatch, tmp_path):
    context = incident_context(tmp_path)
    original = [{"id": 1, "status": "open", "title": "x", "events": [{}, {}]}]
    seed_incidents(context, original)
    before = context.incident_path.read_bytes()
    monkeypatch.setattr(incidents, "MAX_EVENTS_PER_INCIDENT", 2)

    with pytest.raises(incidents.IncidentCapacityError, match="events 数量超过上限 2"):
        incidents.update_incident(context, 1, {"status": "closed", "event": "附带事件"})
    assert context.incident_path.read_bytes() == before

    monkeypatch.setattr(incidents, "MAX_EVENTS_PER_INCIDENT", 200)
    current_size = len(json.dumps(original[0], ensure_ascii=False, indent=2).encode("utf-8"))
    monkeypatch.setattr(incidents, "MAX_INCIDENT_BYTES", current_size)
    with pytest.raises(incidents.IncidentCapacityError, match="单条事故序列化字节数"):
        incidents.update_incident(context, 1, {"title": "中文" * 100})
    assert context.incident_path.read_bytes() == before


def test_update_candidate_file_limit_has_no_partial_write(monkeypatch, tmp_path):
    context = incident_context(tmp_path)
    original = [{"id": 1, "title": "x", "status": "open", "events": []}]
    seed_incidents(context, original)
    before = context.incident_path.read_bytes()
    monkeypatch.setattr(incidents, "MAX_INCIDENT_FILE_BYTES", len(before) + 4)
    monkeypatch.setattr(incidents, "MAX_INCIDENT_BYTES", 1024 * 1024)

    with pytest.raises(incidents.IncidentCapacityError, match="候选文件序列化字节数"):
        incidents.update_incident(context, 1, {"status": "closed", "owner": "中文负责人"})
    assert context.incident_path.read_bytes() == before


def test_incident_capacity_constants_are_centralized():
    assert incidents.MAX_INCIDENTS == 1000
    assert incidents.MAX_EVENTS_PER_INCIDENT == 200
    assert incidents.MAX_INCIDENT_BYTES == 64 * 1024
    assert incidents.MAX_INCIDENT_FILE_BYTES == 16 * 1024 * 1024


def test_legacy_over_limit_data_is_preserved_and_only_non_growth_updates_allowed(
    monkeypatch, tmp_path,
):
    context = incident_context(tmp_path)
    original = [
        {"id": 3, "title": "legacy-large-title", "events": [{}, {}, {}], "unknown": "kept"},
        {"id": 2, "title": "older", "events": []},
        {"id": 1, "title": "oldest", "events": []},
    ]
    seed_incidents(context, original)
    monkeypatch.setattr(incidents, "MAX_INCIDENTS", 2)
    monkeypatch.setattr(incidents, "MAX_EVENTS_PER_INCIDENT", 2)
    monkeypatch.setattr(incidents, "MAX_INCIDENT_BYTES", 80)

    assert incidents.incident_list(context) == original
    updated = incidents.update_incident(context, 3, {"title": "short"})
    assert updated["events"] == [{}, {}, {}]
    assert updated["unknown"] == "kept"
    assert [item["id"] for item in incidents.incident_list(context)] == [3, 2, 1]
    before = context.incident_path.read_bytes()
    with pytest.raises(incidents.IncidentCapacityError, match="events 数量超过上限 2"):
        incidents.update_incident(context, 3, {"event": "cannot grow"})
    assert context.incident_path.read_bytes() == before
    with pytest.raises(incidents.IncidentCapacityError, match="事故数量达到上限 2"):
        incidents.new_incident(context, {})
    assert context.incident_path.read_bytes() == before


def test_corrupt_storage_cannot_be_overwritten_by_create_or_update(tmp_path):
    context = incident_context(tmp_path)
    context.incident_path.parent.mkdir(parents=True)
    context.incident_path.write_bytes(b"{broken")
    before = context.incident_path.read_bytes()
    with pytest.raises(incidents.IncidentStorageError):
        incidents.new_incident(context, {})
    with pytest.raises(incidents.IncidentStorageError):
        incidents.update_incident(context, 1, {"status": "closed"})
    assert context.incident_path.read_bytes() == before


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize("failure", ["mkdir", "write", "replace"])
def test_persistence_os_errors_are_sanitized_and_preserve_formal_file(
    monkeypatch, tmp_path, operation, failure,
):
    context = incident_context(tmp_path)
    seed_incidents(context, [{"id": 1, "title": "original", "events": []}])
    before = context.incident_path.read_bytes()
    private_detail = f"private path and content from {failure}"

    def fail(*_args, **_kwargs):
        raise OSError(private_detail)

    if failure == "mkdir":
        monkeypatch.setattr(Path, "mkdir", fail)
    elif failure == "write":
        monkeypatch.setattr(Path, "write_bytes", fail)
    else:
        monkeypatch.setattr(Path, "replace", fail)

    if operation == "create":
        action = lambda: incidents.new_incident(context, {"title": "new secret"})
    else:
        action = lambda: incidents.update_incident(context, 1, {"title": "updated secret"})
    with pytest.raises(incidents.IncidentStorageError) as caught:
        action()
    assert str(caught.value) == "事故存储写入失败，原记录未更新，请检查存储状态"
    assert private_detail not in str(caught.value)
    assert context.incident_path.read_bytes() == before


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
    with pytest.raises(PermissionError, match="write endpoints are disabled"):
        incidents.update_incident(disabled_context, 1, {"status": "closed"})
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


def test_incident_http_distinguishes_storage_and_capacity_errors(monkeypatch, tmp_path):
    api = load_api(tmp_path)
    api.INCIDENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    api.INCIDENT_PATH.write_bytes(b"{broken")
    server, thread, base_url = run_server(api)
    try:
        for method, path, body in (
            ("GET", "/incidents", None),
            ("POST", "/incidents", b"{}"),
            ("PATCH", "/incidents/1", b"{}"),
        ):
            status, _, payload = request_raw(
                f"{base_url}{path}", body, method=method,
            )
            assert status == 500
            assert payload["ok"] is False
            assert "事故存储损坏" in payload["error"]
            assert str(api.INCIDENT_PATH) not in payload["error"]
        assert api.INCIDENT_PATH.read_bytes() == b"{broken"
    finally:
        server.shutdown()
        thread.join(timeout=5)

    api.INCIDENT_PATH.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(incidents, "MAX_INCIDENTS", 0)
    server, thread, base_url = run_server(api)
    try:
        status, _, payload = request_raw(f"{base_url}/incidents", b"{}", method="POST")
        assert status == 409
        assert payload == {"ok": False, "error": "事故数量达到上限 0，无法新建事故"}
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_concurrent_http_creates_cannot_exceed_incident_limit(monkeypatch, tmp_path):
    api = load_api(tmp_path)
    monkeypatch.setattr(incidents, "MAX_INCIDENTS", 1)
    server, thread, base_url = run_server(api)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(
                lambda title: request_json(f"{base_url}/incidents", {"title": title}),
                ("first", "second"),
            ))
        assert sorted(status for status, _, _ in results) == [200, 409]
        assert len(incidents.incident_list(api._incident_context())) == 1
        failed = next(payload for status, _, payload in results if status == 409)
        assert failed["ok"] is False
        assert "事故数量达到上限 1" in failed["error"]
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_incident_post_and_patch_sanitize_persistence_failures(monkeypatch, tmp_path):
    api = load_api(tmp_path)
    seed_incidents(api._incident_context(), [{"id": 1, "title": "original", "events": []}])
    before = api.INCIDENT_PATH.read_bytes()
    server, thread, base_url = run_server(api)

    def fail_write(*_args, **_kwargs):
        raise OSError("private/server/incidents.json secret incident content")

    monkeypatch.setattr(Path, "write_bytes", fail_write)
    try:
        for method, path, body in (
            ("POST", "/incidents", b'{"title":"new secret"}'),
            ("PATCH", "/incidents/1", b'{"title":"updated secret"}'),
        ):
            status, _, payload = request_raw(f"{base_url}{path}", body, method=method)
            assert status == 500
            assert payload == {
                "ok": False,
                "error": "事故存储写入失败，原记录未更新，请检查存储状态",
            }
            assert "incidents.json" not in payload["error"]
            assert "secret" not in payload["error"]
        assert api.INCIDENT_PATH.read_bytes() == before
    finally:
        server.shutdown()
        thread.join(timeout=5)
