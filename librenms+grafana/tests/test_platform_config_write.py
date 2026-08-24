from dataclasses import FrozenInstanceError, replace
from functools import partial
from pathlib import Path

import pytest

from platform_api import config_write

from .test_platform_transactions import (
    CONFIG_FIXTURES,
    config_text,
    load_api,
    read_apply_status,
    seed,
)


class RecordingLock:
    def __init__(self, events):
        self.events = events

    def __enter__(self):
        self.events.append("lock.enter")
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.events.append("lock.exit")
        return False


class FakeHandler:
    def __init__(self, events):
        self.events = events
        self.sent = []

    def _send_json(self, payload, status=200, headers=None):
        self.events.append("send")
        self.sent.append((payload, status, headers))


def test_context_is_explicit_immutable_and_directly_wired(tmp_path):
    api = load_api(tmp_path)
    context = api._config_write_context()
    dependency = api._write_api_dependencies().handle_config_post

    assert context.event_config_context == api._event_config_context()
    assert context.transaction_context == api._config_transaction_context()
    assert context.apply_runtime_context == api._apply_runtime_context()
    assert context.config_path == api.CONFIG_PATH
    assert context.env_path == api.ENV_PATH
    assert context.write_enabled is api.WRITE_ENABLED
    assert context.merge_env_file is api.merge_env_file
    assert context.atomic_write_text is api.platform_storage.atomic_write_text
    assert context.clock is api.time.time
    assert isinstance(context.require_auth, partial)
    assert context.require_auth.func is api.platform_auth.require_auth
    assert context.require_auth.args == (api.AUTH_CONTEXT,)
    assert context.write_lock is api.WRITE_LOCK
    with pytest.raises(FrozenInstanceError):
        context.write_enabled = False

    assert isinstance(dependency, partial)
    assert dependency.func is config_write.handle_post
    wired_context = dependency.args[0]
    assert replace(wired_context, require_auth=context.require_auth) == context
    assert wired_context.require_auth.func is api.platform_auth.require_auth
    assert wired_context.require_auth.args == (api.AUTH_CONTEXT,)
    for name in ("save_config", "apply_config", "rollback_config"):
        assert not hasattr(api, name)


def test_save_success_preserves_snapshot_history_and_response(tmp_path):
    api = load_api(tmp_path)
    seed(api)

    result = config_write.save_config(
        api._config_write_context(), config_text("new"), "admin", "save",
    )

    assert result["ok"] is True
    assert result["config"]["event"]["name"] == "new"
    snapshot = Path(result["snapshot"])
    assert snapshot == api.TRANSACTION_DIR / result["transactionId"]
    assert (snapshot / "event-config.yml").read_text(encoding="utf-8") == config_text(
        "old"
    )
    history = api.platform_storage.read_json_file(api.STATE_DIR / "history.json", [])
    assert history[0]["action"] == "config.save"
    assert history[0]["detail"] == {
        "transactionId": result["transactionId"],
        "snapshot": result["snapshot"],
    }


def test_save_validation_failure_writes_nothing(tmp_path):
    api = load_api(tmp_path)
    seed(api)
    original_config = api.CONFIG_PATH.read_bytes()
    original_env = api.ENV_PATH.read_bytes()
    incoming = (CONFIG_FIXTURES / "event-config-future-v2.yml").read_text(
        encoding="utf-8",
    )

    result = config_write.save_config(
        api._config_write_context(), incoming, "admin", "save",
    )

    assert result["ok"] is False
    assert result["configTooNew"] is True
    assert api.CONFIG_PATH.read_bytes() == original_config
    assert api.ENV_PATH.read_bytes() == original_env
    assert not list(api.TRANSACTION_DIR.iterdir())
    assert not (api.STATE_DIR / "history.json").exists()


def test_apply_success_preserves_status_files_and_payload(monkeypatch, tmp_path):
    api = load_api(tmp_path)
    seed(api)
    contexts = []

    def succeed(context):
        contexts.append(context)
        return {
            "applied": True,
            "needsRedeploy": False,
            "applyOutput": "ok",
        }

    monkeypatch.setattr(config_write.apply_runtime, "run_apply_command", succeed)

    result = config_write.apply_config(
        api._config_write_context(),
        config_text("new"),
        "admin",
        "apply",
        "apply-write-success",
    )

    assert result["ok"] is True
    assert result["applied"] is True
    assert result["state"] == "succeeded"
    assert result["operationId"] == "apply-write-success"
    assert contexts == [api._apply_runtime_context()]
    status = read_apply_status(api, "apply-write-success")
    assert status["state"] == "succeeded"
    assert status["applyOutput"] == "ok"


def test_apply_failure_restores_pair_and_runtime(monkeypatch, tmp_path):
    api = load_api(tmp_path)
    original_config = (CONFIG_FIXTURES / "event-config-v0.yml").read_text(
        encoding="utf-8",
    )
    original_env = "CUSTOM=old\nFEISHU_APP_SECRET=fixture-secret\n"
    api.CONFIG_PATH.write_text(original_config, encoding="utf-8")
    api.ENV_PATH.write_text(original_env, encoding="utf-8")
    outcomes = iter((
        {"ok": False, "error": "compose failed", "applyOutput": "bad"},
        {"applied": True, "needsRedeploy": False, "applyOutput": "restored"},
    ))
    contexts = []

    def fail_then_restore(context):
        contexts.append(context)
        return next(outcomes)

    monkeypatch.setattr(
        config_write.apply_runtime, "run_apply_command", fail_then_restore,
    )

    result = config_write.apply_config(
        api._config_write_context(),
        (CONFIG_FIXTURES / "event-config-v1.yml").read_text(encoding="utf-8"),
        "admin",
        "apply",
        "apply-write-failure",
    )

    assert result["ok"] is False
    assert result["rolledBack"] is True
    assert result["rollbackApply"]["applied"] is True
    assert api.CONFIG_PATH.read_text(encoding="utf-8") == original_config
    assert api.ENV_PATH.read_text(encoding="utf-8") == original_env
    assert len(contexts) == 2
    assert contexts[0] is contexts[1]
    assert read_apply_status(api, "apply-write-failure")["state"] == "failed"


def test_rollback_success_keeps_guard_before_restore_and_consumes_target(
    monkeypatch, tmp_path,
):
    api = load_api(tmp_path)
    seed(api, env="CUSTOM=old\n")
    saved = config_write.save_config(
        api._config_write_context(), config_text("new"), "admin", "save",
    )
    api.ENV_PATH.write_text("CUSTOM=new\n", encoding="utf-8")
    events = []
    real_create = config_write.config_transaction.create_config_snapshot
    real_restore = config_write.config_transaction.restore_config_snapshot

    def observe_create(context, action, actor="", note=""):
        events.append(("create", action))
        return real_create(context, action, actor, note)

    def observe_restore(context, directory):
        events.append(("restore", Path(directory).name))
        return real_restore(context, directory)

    monkeypatch.setattr(
        config_write.config_transaction, "create_config_snapshot", observe_create,
    )
    monkeypatch.setattr(
        config_write.config_transaction, "restore_config_snapshot", observe_restore,
    )
    monkeypatch.setattr(
        config_write.apply_runtime,
        "run_apply_command",
        lambda _context: {
            "applied": True,
            "needsRedeploy": False,
            "applyOutput": "ok",
        },
    )

    result = config_write.rollback_config(
        api._config_write_context(), "admin", "rollback", "rollback-write-success",
    )

    assert result["applied"] is True
    assert result["restored"]["transactionId"] == saved["transactionId"]
    assert events[:2] == [
        ("create", "config.rollback.guard"),
        ("restore", saved["transactionId"]),
    ]
    metadata = api.platform_storage.read_json_file(
        api.TRANSACTION_DIR / saved["transactionId"] / "metadata.json", {},
    )
    assert metadata["consumedAt"]
    assert read_apply_status(api, "rollback-write-success")["state"] == "succeeded"


def test_config_write_guard_blocks_all_mutations_before_state_changes(
    monkeypatch, tmp_path,
):
    api = load_api(tmp_path)
    seed(api)
    blocked = {"ok": False, "error": "fixture guard"}
    monkeypatch.setattr(
        config_write.event_config,
        "current_config_write_guard",
        lambda _context: blocked,
    )
    monkeypatch.setattr(
        config_write.apply_runtime,
        "run_apply_command",
        lambda _context: (_ for _ in ()).throw(AssertionError("must not apply")),
    )
    context = api._config_write_context()

    assert config_write.save_config(context, "{}") == blocked
    assert config_write.apply_config(
        context, "{}", operation_id="apply-write-guard",
    ) == {**blocked, "operationId": "apply-write-guard"}
    assert config_write.rollback_config(
        context, operation_id="rollback-write-guard",
    ) == {**blocked, "operationId": "rollback-write-guard"}
    assert not list(api.TRANSACTION_DIR.iterdir())
    assert not list(api.APPLY_STATUS_DIR.iterdir())


def test_post_request_parsing_locking_and_payloads_remain_exact(
    monkeypatch, tmp_path,
):
    api = load_api(tmp_path)
    events = []
    handler = FakeHandler(events)
    context = replace(
        api._config_write_context(),
        require_auth=lambda passed_handler: (
            events.append("auth") or {"username": "fixture-admin"}
        ) if passed_handler is handler else (_ for _ in ()).throw(
            AssertionError("wrong handler")
        ),
        write_lock=RecordingLock(events),
    )
    calls = []

    monkeypatch.setattr(
        config_write,
        "save_config",
        lambda passed_context, text, actor, note: (
            calls.append(("save", passed_context, text, actor, note))
            or {"ok": True, "route": "save"}
        ),
    )
    monkeypatch.setattr(
        config_write,
        "apply_config",
        lambda passed_context, text, actor, note, operation_id: (
            calls.append((
                "apply", passed_context, text, actor, note, operation_id,
            )) or {"ok": True, "route": "apply"}
        ),
    )
    monkeypatch.setattr(
        config_write,
        "rollback_config",
        lambda passed_context, actor, note, operation_id: (
            calls.append((
                "rollback", passed_context, actor, note, operation_id,
            )) or {"ok": True, "route": "rollback"}
        ),
    )

    assert config_write.handle_post(
        context,
        handler,
        "/config/save",
        {"text": "cfg", "note": "audit", "actor": "forged"},
    ) is True
    assert calls[-1] == ("save", context, "cfg", "fixture-admin", "audit")
    assert events == ["auth", "lock.enter", "send", "lock.exit"]

    events.clear()
    assert config_write.handle_post(
        context, handler, "/config/apply", {},
    ) is True
    assert calls[-1] == ("apply", context, None, "fixture-admin", "", None)
    assert events == ["auth", "lock.enter", "send", "lock.exit"]

    events.clear()
    assert config_write.handle_post(
        context,
        handler,
        "/config/rollback",
        {"note": "back", "operationId": "rollback-id"},
    ) is True
    assert calls[-1] == (
        "rollback", context, "fixture-admin", "back", "rollback-id",
    )

    events.clear()
    assert config_write.handle_post(
        context, handler, "/config/import", {"text": "imported"},
    ) is True
    assert calls[-1] == ("save", context, "imported", "fixture-admin", "import")
    assert handler.sent[-1] == ({"ok": True, "route": "save"}, 200, None)


def test_unknown_post_path_has_no_auth_lock_or_response(tmp_path):
    api = load_api(tmp_path)
    events = []
    context = replace(
        api._config_write_context(),
        require_auth=lambda _handler: events.append("auth"),
        write_lock=RecordingLock(events),
    )
    handler = FakeHandler(events)

    assert config_write.handle_post(
        context, handler, "/incidents", {"title": "fixture"},
    ) is False
    assert events == []
    assert handler.sent == []
