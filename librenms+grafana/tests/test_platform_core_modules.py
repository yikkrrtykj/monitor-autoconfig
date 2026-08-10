import ast
import json
import os
import stat
from pathlib import Path

import pytest

from platform_api import auth, settings, storage, transactions


class DummyHandler:
    def __init__(self, cookie: str = ""):
        self.headers = {"Cookie": cookie}


def transaction_context(tmp_path: Path, retention: int = 50):
    workspace = tmp_path / "workspace"
    return transactions.TransactionContext(
        config_path=workspace / "event-config.yml",
        env_path=workspace / ".env",
        transaction_dir=tmp_path / "state" / "history" / "transactions",
        apply_status_dir=tmp_path / "state" / "apply-status",
        transaction_retention=retention,
    )


def test_settings_helpers_preserve_defaults_and_exact_boolean_values():
    assert settings.env_bool("MISSING", True, {}) is True
    assert settings.env_bool("MISSING", False, {}) is False
    for value in ("1", "TRUE", "yes", "On"):
        assert settings.env_bool("FLAG", environ={"FLAG": value}) is True
    for value in ("", "0", "false", "unexpected"):
        assert settings.env_bool("FLAG", True, {"FLAG": value}) is False


def test_integer_and_float_settings_keep_default_clamp_and_error_semantics():
    assert settings.env_int("COUNT", 8, environ={}) == 8
    assert settings.env_int("COUNT", 8, minimum=3, environ={"COUNT": "1"}) == 3
    assert settings.env_int("COUNT", 8, maximum=10, environ={"COUNT": "20"}) == 10
    assert settings.env_float("HOURS", 8, {}) == 8.0
    with pytest.raises(ValueError):
        settings.env_int("COUNT", 8, environ={"COUNT": ""})
    with pytest.raises(ValueError):
        settings.env_float("HOURS", 8, {"HOURS": "invalid"})


def test_core_settings_preserve_timeout_limit_and_path_calculation(tmp_path):
    loaded = settings.load_settings({
        "PLATFORM_WORKDIR": str(tmp_path),
        "PLATFORM_STATE_DIR": str(tmp_path / "private-state"),
        "PLATFORM_APPLY_TIMEOUT": "1",
        "PLATFORM_APPLY_VERIFY_TIMEOUT": "2",
        "PLATFORM_MAX_REQUEST_BODY_BYTES": "10",
        "PLATFORM_SESSION_HOURS": "0.1",
        "PLATFORM_TRANSACTION_RETENTION": "1",
        "PLATFORM_APPLY_STATUS_RETENTION": "2",
    })

    assert loaded.config_path == tmp_path / "event-config.yml"
    assert loaded.env_path == tmp_path / ".env"
    assert loaded.transaction_dir == tmp_path / "private-state" / "history" / "transactions"
    assert loaded.apply_timeout == 30
    assert loaded.apply_verify_timeout == 10
    assert loaded.max_request_body_bytes == 1024
    assert loaded.auth_session_seconds == 600
    assert loaded.transaction_retention == 5
    assert loaded.apply_status_retention == 10


def test_atomic_text_and_json_writes_create_parents(tmp_path):
    text_path = tmp_path / "nested" / "event-config.yml"
    json_path = tmp_path / "other" / "state.json"

    storage.atomic_write_text(text_path, "event: test\n")
    storage.write_json_file(json_path, {"ok": True, "name": "赛事"})

    assert text_path.read_text(encoding="utf-8") == "event: test\n"
    assert json.loads(json_path.read_text(encoding="utf-8")) == {
        "ok": True,
        "name": "赛事",
    }
    assert not text_path.with_suffix(".yml.tmp").exists()
    assert not json_path.with_suffix(".json.tmp").exists()


def test_atomic_write_failure_keeps_original_file(monkeypatch, tmp_path):
    target = tmp_path / "event-config.yml"
    target.write_text("original\n", encoding="utf-8")
    original_replace = Path.replace

    def fail_target_replace(path, destination):
        if path == target.with_suffix(".yml.tmp"):
            raise OSError("injected replace failure")
        return original_replace(path, destination)

    monkeypatch.setattr(Path, "replace", fail_target_replace)
    with pytest.raises(OSError, match="injected replace failure"):
        storage.atomic_write_text(target, "replacement\n")

    assert target.read_text(encoding="utf-8") == "original\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not portable to Windows")
def test_private_json_mode_is_set_before_replace(tmp_path):
    target = tmp_path / "private" / "auth.json"

    storage.write_json_file(target, {"passwordHash": "fixture"}, mode=0o600)

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_password_hash_and_auth_store_round_trip(tmp_path):
    encoded = auth.hash_password("CorrectHorse2026", salt=b"0123456789abcdef", iterations=1)
    assert auth.verify_password("CorrectHorse2026", encoded) is True
    assert auth.verify_password("wrong", encoded) is False
    assert auth.verify_password("CorrectHorse2026", "invalid") is False

    context = auth.AuthContext(
        auth_path=tmp_path / "state" / "auth.json",
        default_password="FixtureDefault2026",
    )
    auth.ensure_auth_store(context)
    persisted = json.loads(context.auth_path.read_text(encoding="utf-8"))
    assert persisted["username"] == "admin"
    assert persisted["mustChangePassword"] is True
    assert persisted["passwordChangedAt"] is None
    assert auth.verify_password("FixtureDefault2026", persisted["passwordHash"])


def test_session_create_validate_expire_and_logout(monkeypatch, tmp_path):
    context = auth.AuthContext(
        auth_path=tmp_path / "auth.json",
        cookie_name="platform_session",
        session_seconds=600,
    )
    monkeypatch.setattr(auth.time, "time", lambda: 1000.0)
    token = auth.create_session(context, "admin")
    handler = DummyHandler(f"other=x; platform_session={token}")

    assert auth.current_session(context, handler)["username"] == "admin"
    assert "HttpOnly" in auth.session_cookie(context, token)
    auth.logout_auth(context, handler)
    assert auth.current_session(context, handler) is None

    expired = auth.create_session(context, "admin")
    auth.prune_sessions(context, now=1601.0)
    assert expired not in context.sessions


def test_auth_failure_counter_lock_and_expiry(tmp_path):
    context = auth.AuthContext(
        auth_path=tmp_path / "auth.json",
        failure_limit=3,
        failure_window_seconds=60,
        lock_seconds=30,
    )

    assert auth.record_auth_failure(context, "Admin", "192.0.2.10", now=100) == 0
    assert auth.record_auth_failure(context, "Admin", "192.0.2.10", now=101) == 0
    assert auth.record_auth_failure(context, "Admin", "192.0.2.10", now=102) == 30
    assert auth.auth_lock_remaining(context, "admin", "192.0.2.11", now=103) == 29
    assert auth.auth_lock_remaining(context, "admin", "192.0.2.10", now=133) == 0

    auth.clear_auth_failures(context, "admin", "192.0.2.10")
    assert not context.failures


def test_transaction_snapshot_metadata_and_paired_restore(tmp_path):
    context = transaction_context(tmp_path)
    context.config_path.parent.mkdir(parents=True)
    context.config_path.write_text("event: old\n", encoding="utf-8")
    context.env_path.write_text("EVENT_NAME=old\n", encoding="utf-8")

    snapshot = transactions.create_config_snapshot(
        context, "config.apply", "admin", "fixture",
    )
    directory = Path(snapshot["path"])
    metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    assert metadata == {
        "id": snapshot["id"],
        "action": "config.apply",
        "actor": "admin",
        "note": "fixture",
        "createdAt": snapshot["createdAt"],
        "configExisted": True,
        "envExisted": True,
    }
    assert (directory / "event-config.yml").read_text(encoding="utf-8") == "event: old\n"
    assert (directory / ".env").read_text(encoding="utf-8") == "EVENT_NAME=old\n"

    context.config_path.write_text("event: new\n", encoding="utf-8")
    context.env_path.write_text("EVENT_NAME=new\n", encoding="utf-8")
    restored = transactions.restore_config_snapshot(context, directory)

    assert restored["transactionId"] == snapshot["id"]
    assert context.config_path.read_text(encoding="utf-8") == "event: old\n"
    assert context.env_path.read_text(encoding="utf-8") == "EVENT_NAME=old\n"


def test_transaction_retention_listing_order_and_consumed_filter(tmp_path):
    context = transaction_context(tmp_path, retention=2)
    context.config_path.parent.mkdir(parents=True)
    context.config_path.write_text("event: fixture\n", encoding="utf-8")
    context.env_path.write_text("EVENT_NAME=fixture\n", encoding="utf-8")

    created = [
        transactions.create_config_snapshot(context, f"config.{index}")
        for index in range(3)
    ]
    assert len(list(context.transaction_dir.iterdir())) == 2
    listed = transactions.list_config_snapshots(context)
    assert listed == sorted(listed, reverse=True)

    transactions.mark_config_snapshot_consumed(Path(created[-1]["path"]))
    assert Path(created[-1]["path"]) not in transactions.list_config_snapshots(context)


def test_platform_api_package_dependency_direction_and_compose_mount():
    root = Path(__file__).resolve().parents[1]

    def package_dependencies(module_name):
        source = (root / "platform_api" / f"{module_name}.py").read_text(
            encoding="utf-8",
        )
        return {
            node.module
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ImportFrom) and node.level == 1
        }

    assert package_dependencies("settings") == set()
    assert package_dependencies("storage") == set()
    assert package_dependencies("auth") == {"storage"}
    assert package_dependencies("transactions") == {"storage"}

    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    assert "- ./:/workspace" in compose
    assert 'command: ["python", "/workspace/platform-api.py"]' in compose
    assert (root / "platform_api" / "__init__.py").is_file()
