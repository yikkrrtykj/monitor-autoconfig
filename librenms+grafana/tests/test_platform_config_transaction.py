import json
import os
from dataclasses import FrozenInstanceError
from functools import partial
from pathlib import Path

import pytest

from platform_api import config_transaction

from .test_platform_transactions import load_api


def make_context(tmp_path: Path, **overrides):
    values = {
        "config_path": tmp_path / "workspace" / "event-config.yml",
        "env_path": tmp_path / "workspace" / ".env",
        "history_path": tmp_path / "state" / "history.json",
        "transaction_dir": tmp_path / "state" / "history" / "transactions",
        "apply_status_dir": tmp_path / "state" / "apply-status",
        "transaction_retention": 50,
        "apply_status_retention": 200,
    }
    values.update(overrides)
    return config_transaction.ConfigTransactionContext(**values)


def seed_pair(context, config="event: old\n", env="EVENT_NAME=old\n"):
    context.config_path.parent.mkdir(parents=True, exist_ok=True)
    context.config_path.write_text(config, encoding="utf-8")
    context.env_path.write_text(env, encoding="utf-8")


def test_context_contains_only_transaction_dependencies_and_is_immutable(tmp_path):
    context = make_context(tmp_path)

    assert context.config_path == tmp_path / "workspace" / "event-config.yml"
    assert context.env_path == tmp_path / "workspace" / ".env"
    assert context.history_path == tmp_path / "state" / "history.json"
    assert context.transaction_dir == tmp_path / "state" / "history" / "transactions"
    assert context.apply_status_dir == tmp_path / "state" / "apply-status"
    assert context.transaction_retention == 50
    assert context.apply_status_retention == 200
    with pytest.raises(FrozenInstanceError):
        context.config_path = tmp_path / "other.yml"


def test_snapshot_success_preserves_name_metadata_paths_and_contents(
    monkeypatch, tmp_path,
):
    context = make_context(tmp_path)
    seed_pair(context)
    monkeypatch.setattr(
        config_transaction, "new_operation_id", lambda prefix="op": "txn-fixed-0001",
    )
    monkeypatch.setattr(config_transaction.time, "time", lambda: 1_700_000_000.9)

    snapshot = config_transaction.create_config_snapshot(
        context, "config.apply", "admin", "fixture",
    )

    directory = context.transaction_dir / "txn-fixed-0001"
    assert snapshot == {
        "id": "txn-fixed-0001",
        "action": "config.apply",
        "actor": "admin",
        "note": "fixture",
        "createdAt": 1_700_000_000,
        "configExisted": True,
        "envExisted": True,
        "path": str(directory),
    }
    assert (directory / "event-config.yml").read_text(encoding="utf-8") == "event: old\n"
    assert (directory / ".env").read_text(encoding="utf-8") == "EVENT_NAME=old\n"
    metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    assert metadata == {key: value for key, value in snapshot.items() if key != "path"}


def test_snapshot_copy_failure_propagates_without_writing_metadata(monkeypatch, tmp_path):
    context = make_context(tmp_path)
    seed_pair(context)
    monkeypatch.setattr(
        config_transaction, "new_operation_id", lambda prefix="op": "txn-failed-0001",
    )
    monkeypatch.setattr(
        config_transaction.shutil,
        "copy2",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("copy failed")),
    )

    with pytest.raises(OSError, match="copy failed"):
        config_transaction.create_config_snapshot(context, "config.save")

    directory = context.transaction_dir / "txn-failed-0001"
    assert directory.is_dir()
    assert not (directory / "metadata.json").exists()


def test_restore_success_preserves_config_then_env_order_and_result(monkeypatch, tmp_path):
    context = make_context(tmp_path)
    seed_pair(context)
    monkeypatch.setattr(
        config_transaction, "new_operation_id", lambda prefix="op": "txn-restore-0001",
    )
    snapshot = config_transaction.create_config_snapshot(context, "config.apply")
    context.config_path.write_text("event: new\n", encoding="utf-8")
    context.env_path.write_text("EVENT_NAME=new\n", encoding="utf-8")
    real_atomic_write = config_transaction.atomic_write_text
    writes = []

    def observe_write(path, text):
        writes.append((path, text))
        real_atomic_write(path, text)

    monkeypatch.setattr(config_transaction, "atomic_write_text", observe_write)

    restored = config_transaction.restore_config_snapshot(
        context, Path(snapshot["path"]),
    )

    assert writes == [
        (context.config_path, "event: old\n"),
        (context.env_path, "EVENT_NAME=old\n"),
    ]
    assert restored == {
        "transactionId": "txn-restore-0001",
        "config": str(Path(snapshot["path"]) / "event-config.yml"),
        "env": str(Path(snapshot["path"]) / ".env"),
    }


def test_restore_missing_metadata_preserves_value_error(tmp_path):
    context = make_context(tmp_path)
    directory = context.transaction_dir / "txn-missing-0001"
    directory.mkdir(parents=True)

    with pytest.raises(ValueError) as exc_info:
        config_transaction.restore_config_snapshot(context, directory)
    assert str(exc_info.value) == f"invalid config snapshot: {directory}"


def test_restore_removes_files_that_did_not_exist_at_snapshot_time(
    monkeypatch, tmp_path,
):
    context = make_context(tmp_path)
    monkeypatch.setattr(
        config_transaction, "new_operation_id", lambda prefix="op": "txn-empty-0001",
    )
    snapshot = config_transaction.create_config_snapshot(context, "config.apply")
    seed_pair(context, "event: created\n", "EVENT_NAME=created\n")

    restored = config_transaction.restore_config_snapshot(
        context, Path(snapshot["path"]),
    )

    assert restored == {
        "transactionId": "txn-empty-0001",
        "config": "removed",
        "env": "removed",
    }
    assert not context.config_path.exists()
    assert not context.env_path.exists()


def test_history_append_preserves_newest_first_json_shape_and_limit(
    monkeypatch, tmp_path,
):
    context = make_context(tmp_path)
    original = [{"time": index, "action": f"old.{index}"} for index in range(200)]
    config_transaction.write_json_file(context.history_path, original)
    monkeypatch.setattr(config_transaction.time, "time", lambda: 1_700_000_123.8)

    config_transaction.append_history(
        context, "config.save", "admin", "fixture", {"transactionId": "txn-1"},
    )

    history = json.loads(context.history_path.read_text(encoding="utf-8"))
    assert len(history) == 200
    assert history[0] == {
        "time": 1_700_000_123,
        "action": "config.save",
        "actor": "admin",
        "note": "fixture",
        "detail": {"transactionId": "txn-1"},
    }
    assert history[1:] == original[:199]


def test_apply_status_create_preserves_path_payload_and_json(monkeypatch, tmp_path):
    context = make_context(tmp_path)
    monkeypatch.setattr(config_transaction.time, "time", lambda: 1_700_000_200.4)

    payload = config_transaction.write_apply_status(
        context,
        "apply-test-0001",
        "pending",
        action="apply",
        applied=False,
    )

    assert config_transaction.apply_status_path(
        context, "apply-test-0001",
    ) == context.apply_status_dir / "apply-test-0001.json"
    assert payload == {
        "ok": True,
        "operationId": "apply-test-0001",
        "state": "pending",
        "updatedAt": 1_700_000_200,
        "action": "apply",
        "applied": False,
    }
    assert config_transaction.read_apply_status(context, "apply-test-0001") == payload


def test_apply_status_update_overwrites_existing_state(monkeypatch, tmp_path):
    context = make_context(tmp_path)
    times = iter((1_700_000_300, 1_700_000_301))
    monkeypatch.setattr(config_transaction.time, "time", lambda: next(times))
    config_transaction.write_apply_status(
        context, "apply-test-0002", "running", action="apply", startedAt=1,
    )

    updated = config_transaction.write_apply_status(
        context, "apply-test-0002", "failed", action="apply", error="failed",
    )

    assert updated == {
        "ok": False,
        "operationId": "apply-test-0002",
        "state": "failed",
        "updatedAt": 1_700_000_301,
        "action": "apply",
        "error": "failed",
    }
    assert config_transaction.read_apply_status(context, "apply-test-0002") == updated


def test_apply_status_invalid_and_missing_ids_preserve_unknown_payloads(tmp_path):
    context = make_context(tmp_path)

    with pytest.raises(ValueError, match="invalid operation id"):
        config_transaction.apply_status_path(context, "bad/id")
    assert config_transaction.read_apply_status(context, "bad/id") == {
        "ok": False,
        "operationId": "bad/id",
        "state": "unknown",
        "error": "无效的应用任务编号",
    }
    assert config_transaction.read_apply_status(context, "apply-missing-0001") == {
        "ok": False,
        "operationId": "apply-missing-0001",
        "state": "unknown",
        "error": "找不到该应用任务",
    }


def test_cleanup_prunes_transaction_and_status_paths_by_mtime(tmp_path):
    context = make_context(
        tmp_path, transaction_retention=2, apply_status_retention=2,
    )
    transaction_paths = []
    status_paths = []
    for index in range(4):
        transaction_path = context.transaction_dir / f"txn-{index}"
        transaction_path.mkdir(parents=True)
        status_path = context.apply_status_dir / f"status-{index}.json"
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text("{}", encoding="utf-8")
        os.utime(transaction_path, (index + 1, index + 1))
        os.utime(status_path, (index + 1, index + 1))
        transaction_paths.append(transaction_path)
        status_paths.append(status_path)

    config_transaction.prune_generated_state(context)

    assert sorted(path.name for path in context.transaction_dir.iterdir()) == [
        "txn-2", "txn-3",
    ]
    assert sorted(path.name for path in context.apply_status_dir.iterdir()) == [
        "status-2.json", "status-3.json",
    ]


def test_snapshot_listing_excludes_guards_and_consumed_entries(monkeypatch, tmp_path):
    context = make_context(tmp_path)
    seed_pair(context)
    ids = iter(("txn-normal-0001", "txn-guard-0001", "txn-normal-0002"))
    monkeypatch.setattr(
        config_transaction, "new_operation_id", lambda prefix="op": next(ids),
    )
    first = config_transaction.create_config_snapshot(context, "config.save")
    config_transaction.create_config_snapshot(context, "config.rollback.guard")
    latest = config_transaction.create_config_snapshot(context, "config.apply")

    assert config_transaction.list_config_snapshots(context) == [
        Path(latest["path"]), Path(first["path"]),
    ]
    config_transaction.mark_config_snapshot_consumed(Path(latest["path"]))
    assert config_transaction.list_config_snapshots(context) == [Path(first["path"])]


def test_platform_api_owns_only_context_and_direct_module_wiring(tmp_path):
    api = load_api(tmp_path)
    context = api._config_transaction_context()
    read_apply_status = api._read_api_dependencies().read_apply_status

    assert context == config_transaction.ConfigTransactionContext(
        config_path=api.CONFIG_PATH,
        env_path=api.ENV_PATH,
        history_path=api.STATE_DIR / "history.json",
        transaction_dir=api.TRANSACTION_DIR,
        apply_status_dir=api.APPLY_STATUS_DIR,
        transaction_retention=api.TRANSACTION_RETENTION,
        apply_status_retention=api.APPLY_STATUS_RETENTION,
    )
    assert isinstance(read_apply_status, partial)
    assert read_apply_status.func is config_transaction.read_apply_status
    assert read_apply_status.args == (context,)
    for name in (
        "new_operation_id",
        "normalize_operation_id",
        "prune_retained_paths",
        "mark_config_snapshot_consumed",
        "apply_status_path",
        "prune_generated_state",
        "write_apply_status",
        "read_apply_status",
        "create_config_snapshot",
        "list_config_snapshots",
        "restore_config_snapshot",
        "append_history",
    ):
        assert not hasattr(api, name)
    assert not hasattr(api, "platform_transactions")
    assert not (Path(api.__file__).parent / "platform_api" / "transactions.py").exists()
