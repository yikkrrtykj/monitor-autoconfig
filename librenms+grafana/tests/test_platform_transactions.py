import importlib.util
import json
import os
import sys
from pathlib import Path


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


def test_save_snapshots_config_and_env_as_one_generation(tmp_path):
    api = load_api(tmp_path)
    seed(api)

    result = api.save_config(config_text("new"), "admin", "save")

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
    monkeypatch.setattr(api, "run_apply_command", lambda: next(outcomes))

    result = api.apply_config(
        (CONFIG_FIXTURES / "event-config-v1.yml").read_text(encoding="utf-8"),
        "admin", "apply", "apply-test-0001",
    )

    assert result["ok"] is False
    assert result["rolledBack"] is True
    restored = api.parse_config_text(api.CONFIG_PATH.read_text(encoding="utf-8"))
    assert restored["event"]["name"] == "Fixture Legacy Event"
    assert "schema_version" not in restored
    assert restored["alerts"]["feishu_app_secret"] == "fixture-preserved-secret"
    assert api.CONFIG_PATH.read_text(encoding="utf-8") == original_config
    assert api.ENV_PATH.read_text(encoding="utf-8") == original_env
    assert not list(api.CONFIG_PATH.parent.glob(".*.tmp"))
    status = api.read_apply_status("apply-test-0001")
    assert status["state"] == "failed"
    assert status["runtimeRestored"] is True


def test_successful_apply_has_durable_success_record(monkeypatch, tmp_path):
    api = load_api(tmp_path)
    seed(api)
    monkeypatch.setattr(api, "run_apply_command", lambda: {
        "applied": True,
        "needsRedeploy": False,
        "applyOutput": "ok",
    })

    result = api.apply_config(config_text("new"), "admin", "apply", "apply-test-0002")

    assert result["applied"] is True
    assert result["state"] == "succeeded"
    assert api.parse_config_text(api.CONFIG_PATH.read_text(encoding="utf-8"))["event"]["name"] == "new"
    assert "EVENT_NAME=new" in api.ENV_PATH.read_text(encoding="utf-8")
    assert api.read_apply_status("apply-test-0002")["state"] == "succeeded"


def test_rollback_restores_a_paired_snapshot_and_applies_it(monkeypatch, tmp_path):
    api = load_api(tmp_path)
    seed(api, env="CUSTOM=paired-old\n")
    saved = api.save_config(config_text("new"), "admin", "save")
    api.ENV_PATH.write_text("CUSTOM=mutated\n", encoding="utf-8")
    monkeypatch.setattr(api, "run_apply_command", lambda: {
        "applied": True,
        "needsRedeploy": False,
        "applyOutput": "ok",
    })

    result = api.rollback_config("admin", "rollback", "rollback-test-01")

    assert result["applied"] is True
    assert result["restored"]["transactionId"] == saved["transactionId"]
    assert json.loads(api.CONFIG_PATH.read_text(encoding="utf-8"))["event"]["name"] == "old"
    assert api.ENV_PATH.read_text(encoding="utf-8") == "CUSTOM=paired-old\n"
    assert api.read_apply_status("rollback-test-01")["state"] == "succeeded"


def test_failed_rollback_restore_does_not_leave_a_half_restored_pair(monkeypatch, tmp_path):
    api = load_api(tmp_path)
    seed(api, env="CUSTOM=old\n")
    api.save_config(config_text("new"), "admin", "save")
    api.ENV_PATH.write_text("CUSTOM=new\n", encoding="utf-8")
    before_config = api.CONFIG_PATH.read_bytes()
    before_env = api.ENV_PATH.read_bytes()
    real_atomic_write = api.platform_transactions.atomic_write_text
    write_count = 0

    def fail_second_restore_write(path, text):
        nonlocal write_count
        write_count += 1
        if write_count == 2:
            raise OSError("injected paired restore failure")
        real_atomic_write(path, text)

    monkeypatch.setattr(
        api.platform_transactions, "atomic_write_text", fail_second_restore_write,
    )
    monkeypatch.setattr(
        api,
        "run_apply_command",
        lambda: (_ for _ in ()).throw(AssertionError("must not apply")),
    )

    result = api.rollback_config("admin", "rollback", "rollback-half-state")

    assert result["ok"] is False
    assert "injected paired restore failure" in result["error"]
    assert api.CONFIG_PATH.read_bytes() == before_config
    assert api.ENV_PATH.read_bytes() == before_env
    assert api.read_apply_status("rollback-half-state")["state"] == "failed"


def test_repeated_rollback_walks_back_without_restoring_guard(monkeypatch, tmp_path):
    api = load_api(tmp_path)
    seed(api, "old")
    api.save_config(config_text("new"), "admin", "first")
    api.save_config(config_text("newer"), "admin", "second")
    monkeypatch.setattr(api, "run_apply_command", lambda: {
        "applied": True, "needsRedeploy": False, "applyOutput": "ok",
    })

    first = api.rollback_config("admin", "rollback", "rollback-test-02")
    second = api.rollback_config("admin", "rollback", "rollback-test-03")

    assert first["restored"]["transactionId"] != second["restored"]["transactionId"]
    assert json.loads(api.CONFIG_PATH.read_text(encoding="utf-8"))["event"]["name"] == "old"
    assert all(
        api.read_json_file(path / "metadata.json", {}).get("action") != "config.rollback.guard"
        for path in api.list_config_snapshots()
    )


def test_generated_state_retention_is_bounded(tmp_path):
    api = load_api(tmp_path)
    seed(api)
    api.TRANSACTION_RETENTION = 2
    api.APPLY_STATUS_RETENTION = 3

    for index in range(5):
        api.create_config_snapshot(f"test.{index}")
        api.write_apply_status(f"retention-{index:04d}", "succeeded")

    assert len(list(api.TRANSACTION_DIR.iterdir())) == 2
    assert len(list(api.APPLY_STATUS_DIR.glob("*.json"))) == 3


def test_get_old_config_migrates_in_memory_without_rewriting_file(tmp_path):
    api = load_api(tmp_path)
    seed(api)
    original = api.CONFIG_PATH.read_bytes()

    payload = api.config_payload()

    assert payload["ok"] is True
    assert payload["configSchemaOriginal"] == 0
    assert payload["configSchemaCurrent"] == 1
    assert payload["migrationRequired"] is True
    assert payload["config"]["schema_version"] == 1
    assert api.CONFIG_PATH.read_bytes() == original


def test_save_and_import_upgrade_schema_zero_to_one(tmp_path):
    api = load_api(tmp_path)
    seed(api)

    saved = api.save_config(config_text("saved"), "admin", "save")
    assert saved["ok"] is True
    assert api.parse_config_text(api.CONFIG_PATH.read_text(encoding="utf-8"))["schema_version"] == 1

    imported = json.loads(config_text("imported"))
    imported.pop("schema_version", None)
    result = api.save_config(json.dumps(imported), "admin", "import")
    assert result["ok"] is True
    persisted = api.parse_config_text(api.CONFIG_PATH.read_text(encoding="utf-8"))
    assert persisted["schema_version"] == 1
    assert persisted["event"]["name"] == "imported"


def test_apply_existing_schema_zero_writes_migrated_config(monkeypatch, tmp_path):
    api = load_api(tmp_path)
    seed(api)
    monkeypatch.setattr(api, "run_apply_command", lambda: {
        "applied": True,
        "needsRedeploy": False,
        "applyOutput": "ok",
    })

    result = api.apply_config(None, "admin", "apply", "apply-schema-zero")

    assert result["ok"] is True
    persisted = api.parse_config_text(api.CONFIG_PATH.read_text(encoding="utf-8"))
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
    monkeypatch.setattr(api, "run_apply_command", lambda: (_ for _ in ()).throw(AssertionError("must not apply")))

    readable = api.config_payload()
    assert readable["ok"] is True
    assert readable["readOnly"] is True
    assert readable["configTooNew"] is True
    assert readable["config"]["custom_future"] == {"keep": "untouched"}
    assert readable["env"] == {}

    save_result = api.save_config(config_text("older"), "admin", "save")
    import_result = api.save_config(config_text("imported"), "admin", "import")
    apply_result = api.apply_config(config_text("older"), "admin", "apply", "apply-too-new")
    rollback_result = api.rollback_config("admin", "rollback", "rollback-too-new")

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

    result = api.save_config(incoming, "admin", "import")

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
    monkeypatch.setattr(api, "run_apply_command", lambda: (_ for _ in ()).throw(AssertionError("must not apply")))

    result = api.apply_config(
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
