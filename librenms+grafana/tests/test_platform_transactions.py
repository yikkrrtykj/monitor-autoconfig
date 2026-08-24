import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "platform-api.py"
CONFIG_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "config"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_api(tmp_path: Path):
    workdir = tmp_path / "workspace"
    state_dir = tmp_path / "state"
    workdir.mkdir()
    os.environ.update({
        "PLATFORM_WORKDIR": str(workdir),
        "PLATFORM_STATE_DIR": str(state_dir),
        "EVENT_CONFIG_FILE": str(workdir / "event-config.yml"),
        "EVENT_CONFIG_EXAMPLE": str(workdir / "event-config.example.yml"),
        "ENV_FILE": str(workdir / ".env"),
        "PLATFORM_AUTH_ENABLED": "false",
    })
    spec = importlib.util.spec_from_file_location(f"platform_api_transaction_{tmp_path.name}", MODULE_PATH)
    api = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(api)
    api.ensure_dirs()
    return api


def config_text(name: str) -> str:
    return json.dumps({
        "event": {"name": name, "mode": "match", "default_layout": "tournament-64-2layer"},
        "networks": {"player_subnets": "192.168.40.0/24"},
        "devices": {"core": {"ip": "192.168.10.254"}, "stage_switches": [], "access_switches": []},
        "isp": {"links": []},
        "alerts": {"mode": "match"},
        "security": {"grafana_anonymous": False},
    }, ensure_ascii=False)


def seed(api, name="old", env="CUSTOM=old\n"):
    api.CONFIG_PATH.write_text(config_text(name), encoding="utf-8")
    api.ENV_PATH.write_text(env, encoding="utf-8")


def read_apply_status(api, operation_id: str) -> dict:
    return api.platform_config_transaction.read_apply_status(
        api._config_transaction_context(), operation_id,
    )


def save_config(api, text: str, actor: str = "", note: str = "") -> dict:
    return api.platform_config_write.save_config(
        api._config_write_context(), text, actor, note,
    )


def apply_config(
    api,
    text: str | None,
    actor: str = "",
    note: str = "",
    operation_id: str | None = None,
) -> dict:
    return api.platform_config_write.apply_config(
        api._config_write_context(), text, actor, note, operation_id,
    )


def rollback_config(
    api,
    actor: str = "",
    note: str = "",
    operation_id: str | None = None,
) -> dict:
    return api.platform_config_write.rollback_config(
        api._config_write_context(), actor, note, operation_id,
    )


def test_save_snapshots_config_and_env_as_one_generation(tmp_path):
    api = load_api(tmp_path)
    seed(api)

    result = save_config(api, config_text("new"), "admin", "save")

    assert result["ok"] is True
    snapshot = api.TRANSACTION_DIR / result["transactionId"]
    assert json.loads((snapshot / "event-config.yml").read_text(encoding="utf-8"))["event"]["name"] == "old"
    assert (snapshot / ".env").read_text(encoding="utf-8") == "CUSTOM=old\n"


def test_failed_apply_restores_both_files_and_records_failure(monkeypatch, tmp_path):
    api = load_api(tmp_path)
    original_config = (CONFIG_FIXTURES / "event-config-v0.yml").read_text(
        encoding="utf-8"
    )
    original_env = "CUSTOM=paired-old\nFEISHU_APP_SECRET=fixture-env-secret\n"
    api.CONFIG_PATH.write_text(original_config, encoding="utf-8")
    api.ENV_PATH.write_text(original_env, encoding="utf-8")
    outcomes = iter([
        {"ok": False, "error": "compose failed", "applyOutput": "bad"},
        {"applied": True, "needsRedeploy": False, "applyOutput": "restored"},
    ])
    monkeypatch.setattr(
        api.platform_apply_runtime,
        "run_apply_command",
        lambda _context: next(outcomes),
    )

    result = apply_config(api,
        (CONFIG_FIXTURES / "event-config-v1.yml").read_text(encoding="utf-8"),
        "admin", "apply", "apply-test-0001",
    )

    assert result["ok"] is False
    assert result["rolledBack"] is True
    restored = api.platform_event_config.parse_config_text(
        api.CONFIG_PATH.read_text(encoding="utf-8")
    )
    assert restored["event"]["name"] == "Fixture Legacy Event"
    assert "schema_version" not in restored
    assert restored["alerts"]["feishu_app_secret"] == "fixture-preserved-secret"
    assert api.CONFIG_PATH.read_text(encoding="utf-8") == original_config
    assert api.ENV_PATH.read_text(encoding="utf-8") == original_env
    assert not list(api.CONFIG_PATH.parent.glob(".*.tmp"))
    status = read_apply_status(api, "apply-test-0001")
    assert status["state"] == "failed"
    assert status["runtimeRestored"] is True


def test_successful_apply_has_durable_success_record(monkeypatch, tmp_path):
    api = load_api(tmp_path)
    seed(api)
    monkeypatch.setattr(
        api.platform_apply_runtime,
        "run_apply_command",
        lambda _context: {
            "applied": True,
            "needsRedeploy": False,
            "applyOutput": "ok",
        },
    )

    result = apply_config(
        api, config_text("new"), "admin", "apply", "apply-test-0002",
    )

    assert result["applied"] is True
    assert result["state"] == "succeeded"
    assert api.platform_event_config.parse_config_text(
        api.CONFIG_PATH.read_text(encoding="utf-8")
    )["event"]["name"] == "new"
    assert "EVENT_NAME=new" in api.ENV_PATH.read_text(encoding="utf-8")
    assert read_apply_status(api, "apply-test-0002")["state"] == "succeeded"


@pytest.mark.parametrize(
    ("stdout", "stderr", "expected"),
    [
        (b"stdout-\xff", b"stderr-bytes", "stdout-\ufffd\nstderr-bytes"),
        ("stdout-text", None, "stdout-text"),
        (None, "stderr-text", "stderr-text"),
    ],
)
def test_apply_timeout_normalizes_captured_output(
    monkeypatch, tmp_path, stdout, stderr, expected,
):
    api = load_api(tmp_path)
    api.APPLY_TIMEOUT = 300
    monkeypatch.delenv("DEPLOY_CHECK_TIMEOUT", raising=False)

    def time_out(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(
            ["/bin/sh", "apply-env.sh"], 300, output=stdout, stderr=stderr,
        )

    monkeypatch.setattr(api.platform_apply_runtime.subprocess, "run", time_out)

    result = api.platform_apply_runtime.run_apply_command(api._apply_runtime_context())

    assert result["ok"] is False
    assert result["error"] == "配置已写入，但自动应用超时（300s）"
    assert result["applyOutput"] == expected


def test_self_apply_bounds_deploy_check_below_parent_timeout(monkeypatch, tmp_path):
    api = load_api(tmp_path)
    api.APPLY_TIMEOUT = 300
    monkeypatch.setenv("DEPLOY_CHECK_TIMEOUT", "999")

    env = api.platform_apply_runtime.host_exec_env(api._apply_runtime_context())

    assert env["PLATFORM_API_SELF_APPLY"] == "true"
    assert env["DEPLOY_CHECK_TIMEOUT"] == "270"
    assert int(env["DEPLOY_CHECK_TIMEOUT"]) < api.APPLY_TIMEOUT


def test_timeout_finishes_durable_status_and_restores_runtime(monkeypatch, tmp_path):
    api = load_api(tmp_path)
    api.APPLY_TIMEOUT = 300
    seed(api)
    calls = 0

    def timeout_then_recover(args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise subprocess.TimeoutExpired(
                args, 300, output=b"apply partial", stderr=b"health stalled",
            )
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="runtime restored", stderr=None,
        )

    monkeypatch.setattr(
        api.platform_apply_runtime.subprocess,
        "run",
        timeout_then_recover,
    )
    monkeypatch.setattr(
        api.platform_apply_runtime,
        "verify_runtime_after_apply",
        lambda _context: {"ok": True, "services": []},
    )

    result = apply_config(api,
        config_text("new"), "admin", "timeout", "apply-timeout-bytes",
    )
    status = read_apply_status(api, "apply-timeout-bytes")

    assert result["ok"] is False
    assert result["rolledBack"] is True
    assert status["state"] == "failed"
    assert status["error"] == "配置已写入，但自动应用超时（300s）"
    assert status["runtimeRestored"] is True
    assert status["applyOutput"] == "apply partial\nhealth stalled"


def test_running_status_has_recovery_deadline_and_exceptions_finish_failed(
    monkeypatch, tmp_path,
):
    api = load_api(tmp_path)
    api.APPLY_TIMEOUT = 300
    api.APPLY_VERIFY_TIMEOUT = 90
    seed(api)
    captured = {}

    def observe_running_then_fail(_context):
        captured.update(read_apply_status(api, "apply-deadline-test"))
        raise RuntimeError("injected apply exception")

    monkeypatch.setattr(
        api.platform_apply_runtime,
        "run_apply_command",
        observe_running_then_fail,
    )

    result = apply_config(api,
        config_text("new"), "admin", "exception", "apply-deadline-test",
    )
    final = read_apply_status(api, "apply-deadline-test")

    assert captured["state"] == "running"
    assert captured["timeoutSeconds"] == 810
    assert captured["deadlineAt"] == captured["startedAt"] + 810
    assert result["ok"] is False
    assert final["state"] == "failed"
    assert "injected apply exception" in final["error"]


def test_rollback_restores_a_paired_snapshot_and_applies_it(monkeypatch, tmp_path):
    api = load_api(tmp_path)
    seed(api, env="CUSTOM=paired-old\n")
    saved = save_config(api, config_text("new"), "admin", "save")
    api.ENV_PATH.write_text("CUSTOM=mutated\n", encoding="utf-8")
    monkeypatch.setattr(
        api.platform_apply_runtime,
        "run_apply_command",
        lambda _context: {
            "applied": True,
            "needsRedeploy": False,
            "applyOutput": "ok",
        },
    )

    result = rollback_config(api, "admin", "rollback", "rollback-test-01")

    assert result["applied"] is True
    assert result["restored"]["transactionId"] == saved["transactionId"]
    assert json.loads(api.CONFIG_PATH.read_text(encoding="utf-8"))["event"]["name"] == "old"
    assert api.ENV_PATH.read_text(encoding="utf-8") == "CUSTOM=paired-old\n"
    assert read_apply_status(api, "rollback-test-01")["state"] == "succeeded"


def test_failed_rollback_restore_does_not_leave_a_half_restored_pair(monkeypatch, tmp_path):
    api = load_api(tmp_path)
    seed(api, env="CUSTOM=old\n")
    save_config(api, config_text("new"), "admin", "save")
    api.ENV_PATH.write_text("CUSTOM=new\n", encoding="utf-8")
    before_config = api.CONFIG_PATH.read_bytes()
    before_env = api.ENV_PATH.read_bytes()
    real_atomic_write = api.platform_config_transaction.atomic_write_text
    write_count = 0

    def fail_second_restore_write(path, text):
        nonlocal write_count
        write_count += 1
        if write_count == 2:
            raise OSError("injected paired restore failure")
        real_atomic_write(path, text)

    monkeypatch.setattr(
        api.platform_config_transaction, "atomic_write_text", fail_second_restore_write,
    )
    monkeypatch.setattr(
        api.platform_apply_runtime,
        "run_apply_command",
        lambda _context: (_ for _ in ()).throw(AssertionError("must not apply")),
    )

    result = rollback_config(api, "admin", "rollback", "rollback-half-state")

    assert result["ok"] is False
    assert "injected paired restore failure" in result["error"]
    assert api.CONFIG_PATH.read_bytes() == before_config
    assert api.ENV_PATH.read_bytes() == before_env
    assert read_apply_status(api, "rollback-half-state")["state"] == "failed"


def test_repeated_rollback_walks_back_without_restoring_guard(monkeypatch, tmp_path):
    api = load_api(tmp_path)
    seed(api, "old")
    save_config(api, config_text("new"), "admin", "first")
    save_config(api, config_text("newer"), "admin", "second")
    monkeypatch.setattr(
        api.platform_apply_runtime,
        "run_apply_command",
        lambda _context: {
            "applied": True, "needsRedeploy": False, "applyOutput": "ok",
        },
    )

    first = rollback_config(api, "admin", "rollback", "rollback-test-02")
    second = rollback_config(api, "admin", "rollback", "rollback-test-03")

    assert first["restored"]["transactionId"] != second["restored"]["transactionId"]
    assert json.loads(api.CONFIG_PATH.read_text(encoding="utf-8"))["event"]["name"] == "old"
    assert all(
        api.platform_storage.read_json_file(
            path / "metadata.json", {},
        ).get("action") != "config.rollback.guard"
        for path in api.platform_config_transaction.list_config_snapshots(
            api._config_transaction_context(),
        )
    )


def test_generated_state_retention_is_bounded(tmp_path):
    api = load_api(tmp_path)
    seed(api)
    api.TRANSACTION_RETENTION = 2
    api.APPLY_STATUS_RETENTION = 3

    for index in range(5):
        api.platform_config_transaction.create_config_snapshot(
            api._config_transaction_context(), f"test.{index}",
        )
        api.platform_config_transaction.write_apply_status(
            api._config_transaction_context(),
            f"retention-{index:04d}",
            "succeeded",
        )

    assert len(list(api.TRANSACTION_DIR.iterdir())) == 2
    assert len(list(api.APPLY_STATUS_DIR.glob("*.json"))) == 3


def test_get_old_config_migrates_in_memory_without_rewriting_file(tmp_path):
    api = load_api(tmp_path)
    seed(api)
    original = api.CONFIG_PATH.read_bytes()

    payload = api.platform_event_config.config_payload(api._event_config_context())

    assert payload["ok"] is True
    assert payload["configSchemaOriginal"] == 0
    assert payload["configSchemaCurrent"] == 1
    assert payload["migrationRequired"] is True
    assert payload["config"]["schema_version"] == 1
    assert api.CONFIG_PATH.read_bytes() == original


def test_save_and_import_upgrade_schema_zero_to_one(tmp_path):
    api = load_api(tmp_path)
    seed(api)

    saved = save_config(api, config_text("saved"), "admin", "save")
    assert saved["ok"] is True
    assert api.platform_event_config.parse_config_text(
        api.CONFIG_PATH.read_text(encoding="utf-8")
    )["schema_version"] == 1

    imported = json.loads(config_text("imported"))
    imported.pop("schema_version", None)
    result = save_config(api, json.dumps(imported), "admin", "import")
    assert result["ok"] is True
    persisted = api.platform_event_config.parse_config_text(
        api.CONFIG_PATH.read_text(encoding="utf-8")
    )
    assert persisted["schema_version"] == 1
    assert persisted["event"]["name"] == "imported"


def test_apply_existing_schema_zero_writes_migrated_config(monkeypatch, tmp_path):
    api = load_api(tmp_path)
    seed(api)
    monkeypatch.setattr(
        api.platform_apply_runtime,
        "run_apply_command",
        lambda _context: {
            "applied": True,
            "needsRedeploy": False,
            "applyOutput": "ok",
        },
    )

    result = apply_config(api, None, "admin", "apply", "apply-schema-zero")

    assert result["ok"] is True
    persisted = api.platform_event_config.parse_config_text(
        api.CONFIG_PATH.read_text(encoding="utf-8")
    )
    assert persisted["schema_version"] == 1
    assert "EVENT_NAME=old" in api.ENV_PATH.read_text(encoding="utf-8")


def test_newer_current_config_blocks_save_apply_import_and_rollback(monkeypatch, tmp_path):
    api = load_api(tmp_path)
    newer = json.dumps({
        "schema_version": 2,
        "event": {"name": "future"},
        "custom_future": {"keep": "untouched"},
    }, ensure_ascii=False)
    api.CONFIG_PATH.write_text(newer, encoding="utf-8")
    api.ENV_PATH.write_text("CUSTOM=future\n", encoding="utf-8")
    original_config = api.CONFIG_PATH.read_bytes()
    original_env = api.ENV_PATH.read_bytes()
    monkeypatch.setattr(
        api.platform_apply_runtime,
        "run_apply_command",
        lambda _context: (_ for _ in ()).throw(AssertionError("must not apply")),
    )

    readable = api.platform_event_config.config_payload(api._event_config_context())
    assert readable["ok"] is True
    assert readable["readOnly"] is True
    assert readable["configTooNew"] is True
    assert readable["config"]["custom_future"] == {"keep": "untouched"}
    assert readable["env"] == {}

    save_result = save_config(api, config_text("older"), "admin", "save")
    import_result = save_config(api, config_text("imported"), "admin", "import")
    apply_result = apply_config(
        api, config_text("older"), "admin", "apply", "apply-too-new",
    )
    rollback_result = rollback_config(
        api, "admin", "rollback", "rollback-too-new",
    )

    for result in (save_result, import_result, apply_result, rollback_result):
        assert result["ok"] is False
        assert result["configTooNew"] is True
        assert "software supports schema 1" in result["error"]
    assert api.CONFIG_PATH.read_bytes() == original_config
    assert api.ENV_PATH.read_bytes() == original_env
    assert not list(api.TRANSACTION_DIR.iterdir())


def test_import_of_newer_schema_is_rejected_before_overwrite(tmp_path):
    api = load_api(tmp_path)
    current = {"schema_version": 1, **json.loads(config_text("current"))}
    api.CONFIG_PATH.write_text(json.dumps(current), encoding="utf-8")
    api.ENV_PATH.write_text("CUSTOM=current\n", encoding="utf-8")
    before_config = api.CONFIG_PATH.read_bytes()
    before_env = api.ENV_PATH.read_bytes()
    incoming = json.dumps({"schema_version": 2, "custom_future": {"secret": "keep"}})

    result = save_config(api, incoming, "admin", "import")

    assert result["ok"] is False
    assert result["configTooNew"] is True
    assert api.CONFIG_PATH.read_bytes() == before_config
    assert api.ENV_PATH.read_bytes() == before_env


def test_apply_of_newer_submitted_schema_writes_no_files(monkeypatch, tmp_path):
    api = load_api(tmp_path)
    current = {"schema_version": 1, **json.loads(config_text("current"))}
    api.CONFIG_PATH.write_text(json.dumps(current), encoding="utf-8")
    api.ENV_PATH.write_text("CUSTOM=current\n", encoding="utf-8")
    before_config = api.CONFIG_PATH.read_bytes()
    before_env = api.ENV_PATH.read_bytes()
    monkeypatch.setattr(
        api.platform_apply_runtime,
        "run_apply_command",
        lambda _context: (_ for _ in ()).throw(AssertionError("must not apply")),
    )

    result = apply_config(api,
        json.dumps({"schema_version": 2, "custom_future": {"keep": True}}),
        "admin",
        "apply",
        "apply-submitted-newer",
    )

    assert result["ok"] is False
    assert result["configTooNew"] is True
    assert api.CONFIG_PATH.read_bytes() == before_config
    assert api.ENV_PATH.read_bytes() == before_env
    assert not (api.APPLY_STATUS_DIR / "apply-submitted-newer.json").exists()
