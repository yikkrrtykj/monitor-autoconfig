"""Config fixtures exercise migration through rendered Compose input."""
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "config"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location(
    "fixture_platform_config", ROOT / "platform_config.py"
)
platform_config = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(platform_config)


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_v0_fixture_migrates_validates_renders_and_compose_accepts_it(tmp_path):
    original = platform_config.parse_simple_yaml(fixture("event-config-v0.yml"))
    migrated = platform_config.migrate_config(original)

    assert original.get("schema_version") is None
    assert migrated["schema_version"] == 1
    assert migrated["fixture_extension"] == {
        "owner": "test-suite", "enabled": True,
    }
    assert not [
        issue for issue in platform_config.validate_config(migrated)
        if issue["level"] == "bad"
    ]

    project = tmp_path / "rendered"
    project.mkdir()
    config_path = project / "event-config.yml"
    env_path = project / ".env"
    platform_config.atomic_write_config(
        config_path, platform_config.dump_simple_yaml(migrated) + "\n",
    )
    platform_config.atomic_write_config(
        env_path, "CUSTOM_KEEP=fixture\nFEISHU_APP_SECRET=old-fixture-secret\n",
    )
    existing = platform_config.read_env(env_path)
    rendered = platform_config.render_env(migrated, existing)
    platform_config.atomic_write_config(
        env_path, platform_config.merge_env_file(env_path, rendered),
    )

    saved = platform_config.parse_simple_yaml(config_path.read_text(encoding="utf-8"))
    saved_env = platform_config.read_env(env_path)
    assert saved["fixture_extension"]["owner"] == "test-suite"
    assert saved_env["FEISHU_APP_SECRET"] == "fixture-preserved-secret"
    assert saved_env["CUSTOM_KEEP"] == "fixture"
    assert saved_env["TOURNAMENT_SWITCHES"] == "stage-switch-a:192.0.2.45"

    if sys.platform == "win32":
        installed = Path(
            r"C:\Program Files\Docker\Docker\resources\cli-plugins\docker-compose.exe"
        )
        compose = [str(installed)] if installed.exists() else []
    else:
        docker = shutil.which("docker")
        compose = [docker, "compose"] if docker else []
    assert compose, "Docker Compose is required for the config-chain integration test"
    completed = subprocess.run(
        compose + [
            "--project-directory", str(project),
            "--env-file", str(env_path), "-f", str(ROOT / "docker-compose.yml"),
            "config", "--quiet",
        ],
        cwd=project,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_v1_fixture_is_idempotent_and_renders_current_stage_authority():
    parsed = platform_config.parse_simple_yaml(fixture("event-config-v1.yml"))
    migrated = platform_config.migrate_config(parsed)
    env = platform_config.render_env(migrated)

    assert migrated == parsed
    assert env["TOURNAMENT_SWITCHES"] == (
        "stage-switch-a:192.0.2.45,stage-switch-b:192.0.2.46"
    )
    assert env["FEISHU_APP_SECRET"] == "fixture-preserved-secret"


def test_extra_fields_fixture_survives_parse_migrate_validate_and_atomic_save(tmp_path):
    parsed = platform_config.parse_simple_yaml(fixture("event-config-extra-fields.yml"))
    migrated = platform_config.migrate_config(parsed)
    assert not [
        issue for issue in platform_config.validate_config(migrated)
        if issue["level"] == "bad"
    ]

    path = tmp_path / "event-config.yml"
    platform_config.atomic_write_config(
        path, platform_config.dump_simple_yaml(migrated) + "\n",
    )
    saved = platform_config.parse_simple_yaml(path.read_text(encoding="utf-8"))

    assert saved["event"]["fixture_banner"] == "preserve-event-extra"
    assert saved["devices"]["core"]["fixture_asset_tag"] == "preserve-core-extra"
    assert saved["fixture_plugin"]["nested"]["keep"] == "untouched"
    assert not list(tmp_path.glob(".event-config.yml.*.tmp"))


def test_future_v2_fixture_is_rejected_without_downgrade():
    parsed = platform_config.parse_simple_yaml(fixture("event-config-future-v2.yml"))

    with pytest.raises(platform_config.ConfigTooNewError):
        platform_config.migrate_config(parsed)

    assert parsed["future_runtime"]["preserve_me"] is True
