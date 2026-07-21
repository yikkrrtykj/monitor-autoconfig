import importlib.util
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "platform-api.py"
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
    seed(api)
    outcomes = iter([
        {"ok": False, "error": "compose failed", "applyOutput": "bad"},
        {"applied": True, "needsRedeploy": False, "applyOutput": "restored"},
    ])
    monkeypatch.setattr(api, "run_apply_command", lambda: next(outcomes))

    result = api.apply_config(config_text("new"), "admin", "apply", "apply-test-0001")

    assert result["ok"] is False
    assert result["rolledBack"] is True
    assert json.loads(api.CONFIG_PATH.read_text(encoding="utf-8"))["event"]["name"] == "old"
    assert api.ENV_PATH.read_text(encoding="utf-8") == "CUSTOM=old\n"
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
