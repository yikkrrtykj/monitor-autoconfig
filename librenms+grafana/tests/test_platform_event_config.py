from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from platform_api import event_config

from .test_platform_transactions import (
    CONFIG_FIXTURES,
    apply_config,
    load_api,
    rollback_config,
    save_config,
)


def make_context(tmp_path: Path, *, write_enabled: bool = True):
    return event_config.EventConfigContext(
        config_path=tmp_path / "event-config.yml",
        example_path=tmp_path / "event-config.example.yml",
        env_path=tmp_path / ".env",
        state_dir=tmp_path / "state",
        write_enabled=write_enabled,
        get_version_info=lambda: {
            "platform_version": "2026.08.1",
            "git_commit": "fixture",
        },
    )


def fixture_text(name: str) -> str:
    return (CONFIG_FIXTURES / name).read_text(encoding="utf-8")


def test_context_is_explicit_and_immutable(tmp_path):
    context = make_context(tmp_path)

    assert context.config_path == tmp_path / "event-config.yml"
    assert context.example_path == tmp_path / "event-config.example.yml"
    assert context.env_path == tmp_path / ".env"
    assert context.state_dir == tmp_path / "state"
    assert context.write_enabled is True
    assert context.get_version_info()["git_commit"] == "fixture"
    with pytest.raises(FrozenInstanceError):
        context.write_enabled = False


def test_read_config_prefers_existing_file(tmp_path):
    context = make_context(tmp_path)
    context.config_path.write_text("event:\n  name: existing\n", encoding="utf-8")
    context.example_path.write_text("event:\n  name: example\n", encoding="utf-8")

    assert event_config.read_config_text(context) == "event:\n  name: existing\n"


def test_read_config_falls_back_to_example_and_then_default(tmp_path):
    context = make_context(tmp_path)
    context.example_path.write_text("event:\n  name: example\n", encoding="utf-8")

    assert event_config.read_config_text(context) == "event:\n  name: example\n"
    context.example_path.unlink()
    assert event_config.read_config_text(context) == "schema_version: 1\nevent:\n  name: \n"


def test_parse_config_accepts_mapping_and_rejects_non_mapping():
    assert event_config.parse_config_text("event:\n  name: fixture\n") == {
        "event": {"name": "fixture"}
    }
    with pytest.raises(ValueError, match="^event config must be a mapping$"):
        event_config.parse_config_text("- fixture")


def test_schema_payload_shapes_remain_exact():
    status = {
        "original_version": 0,
        "current_version": 1,
        "current_supported": 1,
        "migration_required": True,
        "config_too_new": False,
    }
    assert event_config._schema_response(status) == {
        "configSchemaOriginal": 0,
        "configSchemaCurrent": 1,
        "configSchemaSupported": 1,
        "migrationRequired": True,
        "configTooNew": False,
    }
    assert event_config._schema_error_payload("broken", {"schema_version": "x"}) == {
        "ok": False,
        "error": "broken",
        "config": {"schema_version": "x"},
        "issues": [{"level": "bad", "path": "schema_version", "message": "broken"}],
        "env": {},
        "normalizedText": "",
        "writeEnabled": False,
    }


def test_write_guard_allows_missing_and_supported_config(tmp_path):
    context = make_context(tmp_path)
    assert event_config.current_config_write_guard(context) is None

    context.config_path.write_text(fixture_text("event-config-v1.yml"), encoding="utf-8")
    assert event_config.current_config_write_guard(context) is None


def test_write_guard_maps_invalid_config_to_original_error_payload(tmp_path):
    context = make_context(tmp_path)
    context.config_path.write_text("schema_version: invalid\n", encoding="utf-8")

    payload = event_config.current_config_write_guard(context)

    assert payload == event_config._schema_error_payload(
        "Cannot modify event config: schema_version must be a non-negative integer"
    )


def test_write_guard_maps_read_failure_without_changing_exception_scope(tmp_path):
    context = make_context(tmp_path)
    context.config_path.mkdir()

    payload = event_config.current_config_write_guard(context)

    assert payload["ok"] is False
    assert payload["error"].startswith("Cannot modify event config: ")
    assert payload["issues"][0]["message"] == payload["error"]


def test_write_guard_refuses_future_schema_with_exact_semantics(tmp_path):
    context = make_context(tmp_path)
    context.config_path.write_text(fixture_text("event-config-future-v2.yml"), encoding="utf-8")

    payload = event_config.current_config_write_guard(context)

    assert payload["ok"] is False
    assert payload["error"] == (
        "Refusing to modify schema 2; software supports schema 1. "
        "Upgrade the monitoring platform first."
    )
    assert payload["config"]["future_runtime"]["preserve_me"] is True
    assert payload["writeEnabled"] is False
    assert payload["configSchemaOriginal"] == 2
    assert payload["configSchemaCurrent"] == 2
    assert payload["configSchemaSupported"] == 1
    assert payload["migrationRequired"] is False
    assert payload["configTooNew"] is True


def test_config_payload_returns_schema_error_without_rendering(tmp_path):
    context = make_context(tmp_path)

    payload = event_config.config_payload(context, "schema_version: invalid\n")

    assert payload["ok"] is False
    assert payload["error"] == "schema_version must be a non-negative integer"
    assert payload["config"] == {"schema_version": "invalid"}
    assert payload["issues"] == [{
        "level": "bad",
        "path": "schema_version",
        "message": "schema_version must be a non-negative integer",
    }]
    assert payload["normalizedText"] == ""
    assert payload["env"] == {}
    assert payload["writeEnabled"] is False
    assert payload["text"] == "schema_version: invalid\n"
    assert payload["paths"] == {
        "config": str(context.config_path),
        "env": str(context.env_path),
        "state": str(context.state_dir),
    }


@pytest.mark.parametrize(
    ("editing_existing", "expected_ok"),
    [(True, True), (False, False)],
)
def test_future_schema_is_read_only_but_preserves_existing_read_contract(
    tmp_path, editing_existing, expected_ok,
):
    context = make_context(tmp_path)
    text = fixture_text("event-config-future-v2.yml")
    context.config_path.write_text(text, encoding="utf-8")

    payload = event_config.config_payload(
        context,
        None if editing_existing else text,
    )

    assert payload["ok"] is expected_ok
    assert payload["readOnly"] is True
    assert payload["configTooNew"] is True
    assert payload["config"]["future_runtime"]["preserve_me"] is True
    assert payload["normalizedText"] == ""
    assert payload["env"] == {}
    assert payload["writeEnabled"] is False
    assert payload["error"] == (
        "event-config schema 2 is newer than supported schema 1; "
        "upgrade the monitoring platform first"
    )


def test_legacy_schema_migrates_and_preserves_unknown_fields_and_secret(tmp_path):
    context = make_context(tmp_path, write_enabled=False)
    text = fixture_text("event-config-v0.yml")

    payload = event_config.config_payload(context, text)

    assert payload["ok"] is True
    assert payload["configSchemaOriginal"] == 0
    assert payload["configSchemaCurrent"] == 1
    assert payload["configSchemaSupported"] == 1
    assert payload["migrationRequired"] is True
    assert payload["configTooNew"] is False
    assert payload["config"]["schema_version"] == 1
    assert payload["config"]["fixture_extension"] == {
        "owner": "test-suite",
        "enabled": True,
    }
    assert payload["config"]["alerts"]["feishu_app_secret"] == (
        "fixture-preserved-secret"
    )
    assert payload["normalizedText"].endswith("\n")
    assert event_config.parse_config_text(payload["normalizedText"])[
        "fixture_extension"
    ] == payload["config"]["fixture_extension"]
    assert payload["writeEnabled"] is False


def test_config_payload_keeps_migrate_validate_render_order_and_results(
    monkeypatch, tmp_path,
):
    context = make_context(tmp_path)
    calls = []
    migrated = {"schema_version": 1, "event": {"name": "migrated"}}
    issues = [{"level": "warn", "path": "event.name", "message": "fixture"}]
    rendered = {"EVENT_NAME": "migrated"}

    def migrate(config):
        calls.append(("migrate", config))
        return migrated

    def read_environment(path):
        calls.append(("read_env", path))
        return {"EXISTING": "1"}

    def validate(config):
        calls.append(("validate", config))
        return issues

    def render(config, existing):
        calls.append(("render", config, existing))
        return rendered

    monkeypatch.setattr(event_config, "migrate_config", migrate)
    monkeypatch.setattr(event_config, "read_env", read_environment)
    monkeypatch.setattr(event_config, "validate_config", validate)
    monkeypatch.setattr(event_config, "render_env", render)

    payload = event_config.config_payload(
        context,
        "schema_version: 1\nevent:\n  name: submitted\n",
    )

    assert [call[0] for call in calls] == ["migrate", "read_env", "validate", "render"]
    assert calls[2][1] is migrated
    assert calls[3][1:] == (migrated, {"EXISTING": "1"})
    assert payload["issues"] is issues
    assert payload["env"] is rendered
    assert payload["normalizedText"] == (
        "schema_version: 1\nevent:\n  name: migrated\n"
    )


@pytest.mark.parametrize("write_enabled", [True, False])
def test_config_payload_reports_context_paths_and_write_enabled(tmp_path, write_enabled):
    context = make_context(tmp_path, write_enabled=write_enabled)

    payload = event_config.config_payload(context, "schema_version: 1\n")

    assert payload["writeEnabled"] is write_enabled
    assert payload["paths"] == {
        "config": str(context.config_path),
        "env": str(context.env_path),
        "state": str(context.state_dir),
    }


def test_existing_config_imports_only_missing_legacy_feishu_credentials(tmp_path):
    context = make_context(tmp_path)
    context.config_path.write_text(
        "schema_version: 1\nalerts:\n  feishu_app_id: configured\n",
        encoding="utf-8",
    )
    context.env_path.write_text(
        "FEISHU_APP_ID=legacy-id\n"
        "FEISHU_APP_SECRET=legacy-secret\n"
        "FEISHU_CHAT_ID=legacy-chat\n",
        encoding="utf-8",
    )

    payload = event_config.config_payload(context)

    assert payload["config"]["alerts"]["feishu_app_id"] == "configured"
    assert payload["config"]["alerts"]["feishu_app_secret"] == "legacy-secret"
    assert payload["config"]["alerts"]["feishu_chat_id"] == "legacy-chat"
    assert payload["env"]["FEISHU_APP_ID"] == "configured"
    assert payload["env"]["FEISHU_APP_SECRET"] == "legacy-secret"
    assert payload["env"]["FEISHU_CHAT_ID"] == "legacy-chat"


def test_submitted_text_does_not_import_missing_legacy_feishu_credentials(tmp_path):
    context = make_context(tmp_path)
    context.env_path.write_text(
        "FEISHU_APP_ID=legacy-id\n"
        "FEISHU_APP_SECRET=legacy-secret\n"
        "FEISHU_CHAT_ID=legacy-chat\n",
        encoding="utf-8",
    )

    payload = event_config.config_payload(
        context,
        "schema_version: 1\nalerts:\n  feishu_robot_token:\n",
    )

    assert "feishu_app_id" not in payload["config"]["alerts"]
    assert "feishu_app_secret" not in payload["config"]["alerts"]
    assert "feishu_chat_id" not in payload["config"]["alerts"]


def test_submitted_text_can_clear_legacy_feishu_credentials(tmp_path):
    context = make_context(tmp_path)
    context.env_path.write_text(
        "FEISHU_APP_ID=legacy-id\n"
        "FEISHU_APP_SECRET=legacy-secret\n"
        "FEISHU_CHAT_ID=legacy-chat\n",
        encoding="utf-8",
    )

    payload = event_config.config_payload(
        context,
        "schema_version: 1\n"
        "alerts:\n"
        "  feishu_robot_token:\n"
        "  feishu_app_id:\n"
        "  feishu_app_secret:\n"
        "  feishu_chat_id:\n",
    )

    assert payload["config"]["alerts"]["feishu_app_id"] == ""
    assert payload["config"]["alerts"]["feishu_app_secret"] == ""
    assert payload["config"]["alerts"]["feishu_chat_id"] == ""
    assert payload["env"]["FEISHU_APP_ID"] == ""
    assert payload["env"]["FEISHU_APP_SECRET"] == ""
    assert payload["env"]["FEISHU_CHAT_ID"] == ""


def test_version_payload_combines_version_and_schema_metadata(tmp_path):
    context = make_context(tmp_path)
    context.config_path.write_text(fixture_text("event-config-v0.yml"), encoding="utf-8")

    assert event_config.version_payload(context) == {
        "ok": True,
        "platform_version": "2026.08.1",
        "git_commit": "fixture",
        "config_schema_original": 0,
        "config_schema_current": 1,
        "migration_required": True,
        "config_too_new": False,
    }


def test_version_payload_preserves_version_data_on_config_error(tmp_path):
    context = make_context(tmp_path)
    context.config_path.write_text("schema_version: invalid\n", encoding="utf-8")

    assert event_config.version_payload(context) == {
        "ok": True,
        "platform_version": "2026.08.1",
        "git_commit": "fixture",
        "config_schema_original": None,
        "config_schema_current": None,
        "migration_required": False,
        "config_too_new": False,
        "config_schema_error": "schema_version must be a non-negative integer",
    }


def test_entrypoint_builds_direct_read_and_write_dependencies(tmp_path):
    api = load_api(tmp_path)

    read_context = api._read_api_context()
    write_dependencies = api._write_api_dependencies()

    assert write_dependencies.config_payload.func is event_config.config_payload
    contexts = (
        read_context.event_config_context,
        write_dependencies.config_payload.args[0],
    )
    for context in contexts:
        assert context.config_path == api.CONFIG_PATH
        assert context.example_path == api.EXAMPLE_PATH
        assert context.env_path == api.ENV_PATH
        assert context.state_dir == api.STATE_DIR
        assert context.write_enabled is api.WRITE_ENABLED
        assert context.get_version_info is api.get_version_info


def test_entrypoint_mutations_call_new_write_guard_directly(monkeypatch, tmp_path):
    api = load_api(tmp_path)
    blocked = {"ok": False, "error": "fixture guard"}
    contexts = []

    def guard(context):
        contexts.append(context)
        return blocked

    monkeypatch.setattr(event_config, "current_config_write_guard", guard)

    assert save_config(api, "{}") == blocked
    assert apply_config(api, "{}", operation_id="event-config-apply") == {
        **blocked,
        "operationId": "event-config-apply",
    }
    assert rollback_config(api, operation_id="event-config-rollback") == {
        **blocked,
        "operationId": "event-config-rollback",
    }
    assert len(contexts) == 3
    assert all(context.config_path == api.CONFIG_PATH for context in contexts)


def test_entrypoint_keeps_no_event_config_compatibility_helpers(tmp_path):
    api = load_api(tmp_path)

    for symbol in (
        "read_config_text",
        "parse_config_text",
        "_schema_response",
        "_schema_error_payload",
        "current_config_write_guard",
        "config_payload",
        "version_payload",
    ):
        assert not hasattr(api, symbol)
