import importlib.util
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "platform_config.py"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("platform_config_schema_test", MODULE_PATH)
platform_config = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(platform_config)


VALID_V0 = {
    "event": {"name": "Schema Test"},
    "devices": {"core": {"ip": "192.168.10.254"}},
    "custom_test": {
        "foo": "bar",
        "nested": {"keep": True},
        "ordered": ["third", "first", "second"],
    },
}


def test_missing_schema_is_zero_and_requires_migration():
    status = platform_config.inspect_config_schema(VALID_V0)

    assert platform_config.get_schema_version(VALID_V0) == 0
    assert status == {
        "original_version": 0,
        "current_supported": 1,
        "current_version": 1,
        "migration_required": True,
        "config_too_new": False,
    }


@pytest.mark.parametrize("value", ["1", True, 1.0, -1])
def test_invalid_schema_versions_are_rejected(value):
    with pytest.raises(platform_config.ConfigSchemaError):
        platform_config.get_schema_version({"schema_version": value})


def test_v0_to_v1_only_adds_schema_and_preserves_unknown_data():
    original = platform_config.copy.deepcopy(VALID_V0)

    migrated = platform_config.migrate_config(original)

    assert original == VALID_V0
    assert migrated == {"schema_version": 1, **VALID_V0}
    assert migrated["custom_test"]["nested"] == {"keep": True}
    assert migrated["custom_test"]["ordered"] == ["third", "first", "second"]


def test_v1_to_v1_is_content_idempotent_and_returns_an_independent_copy():
    original = {"schema_version": 1, **VALID_V0}

    migrated = platform_config.migrate_config(original)

    assert migrated == original
    assert migrated is not original
    assert migrated["custom_test"] is not original["custom_test"]


def test_newer_schema_is_never_downgraded():
    original = {"schema_version": 2, "unknown": {"keep": "safe"}}

    with pytest.raises(platform_config.ConfigTooNewError, match="Refusing to downgrade schema 2"):
        platform_config.migrate_config(original)
    assert original == {"schema_version": 2, "unknown": {"keep": "safe"}}


def test_render_env_receives_the_migrated_schema(monkeypatch):
    observed = []
    original_normalize = platform_config.normalize_config

    def observe(config):
        observed.append(platform_config.get_schema_version(config))
        return original_normalize(config)

    monkeypatch.setattr(platform_config, "normalize_config", observe)
    rendered = platform_config.render_env(VALID_V0)

    assert rendered["EVENT_NAME"] == "Schema Test"
    assert observed and observed[0] == 1


def test_migrate_cli_dry_run_does_not_write(tmp_path):
    path = tmp_path / "event-config.yml"
    original = platform_config.dump_simple_yaml(VALID_V0) + "\n"
    path.write_text(original, encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(MODULE_PATH), "migrate", str(path)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0
    assert "Current schema: 0" in completed.stdout
    assert "Migration required: yes" in completed.stdout
    assert "No files changed." in completed.stdout
    assert path.read_text(encoding="utf-8") == original


def test_migrate_cli_write_is_atomic_idempotent_and_preserves_permissions(tmp_path):
    path = tmp_path / "event-config.yml"
    path.write_text(platform_config.dump_simple_yaml(VALID_V0) + "\n", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o640)
    before_mode = stat.S_IMODE(path.stat().st_mode)

    first = subprocess.run(
        [sys.executable, str(MODULE_PATH), "migrate", str(path), "--write"],
        text=True,
        capture_output=True,
        check=False,
    )
    first_bytes = path.read_bytes()
    second = subprocess.run(
        [sys.executable, str(MODULE_PATH), "migrate", str(path), "--write"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert first.returncode == 0
    assert "Migrated schema 0 -> 1" in first.stdout
    assert platform_config.get_schema_version(platform_config.parse_simple_yaml(path.read_text(encoding="utf-8"))) == 1
    assert second.returncode == 0
    assert "Already at schema 1" in second.stdout
    assert path.read_bytes() == first_bytes
    assert stat.S_IMODE(path.stat().st_mode) == before_mode


def test_migrate_cli_refuses_newer_schema_without_modifying_file(tmp_path):
    path = tmp_path / "event-config.yml"
    original = "schema_version: 2\ncustom_test:\n  foo: bar\n"
    path.write_text(original, encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(MODULE_PATH), "migrate", str(path), "--write"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "Refusing to downgrade schema 2; software supports schema 1." in completed.stderr
    assert path.read_text(encoding="utf-8") == original


def test_migrate_file_validates_after_migration(monkeypatch, tmp_path):
    path = tmp_path / "event-config.yml"
    path.write_text(platform_config.dump_simple_yaml(VALID_V0) + "\n", encoding="utf-8")
    observed = []

    def validate(config):
        observed.append(platform_config.get_schema_version(config))
        return []

    monkeypatch.setattr(platform_config, "validate_config", validate)

    assert platform_config.migrate_file(path, write=False) == 0
    assert observed == [1]
