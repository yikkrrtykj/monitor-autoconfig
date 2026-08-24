"""Config write orchestration and POST request handling."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import apply_runtime, config_transaction, event_config


@dataclass(frozen=True)
class ConfigWriteContext:
    event_config_context: event_config.EventConfigContext
    transaction_context: config_transaction.ConfigTransactionContext
    apply_runtime_context: apply_runtime.ApplyRuntimeContext
    config_path: Path
    env_path: Path
    write_enabled: bool
    merge_env_file: Callable[[Path, dict], str]
    atomic_write_text: Callable[[Path, str], None]
    clock: Callable[[], float]
    require_auth: Callable[[Any], dict]
    write_lock: Any


def _require_write(context: ConfigWriteContext) -> None:
    if not context.write_enabled:
        raise PermissionError("platform write endpoints are disabled")


def save_config(
    context: ConfigWriteContext,
    text: str,
    actor: str = "",
    note: str = "",
) -> dict:
    _require_write(context)
    blocked = event_config.current_config_write_guard(
        context.event_config_context,
    )
    if blocked:
        return blocked
    payload = event_config.config_payload(context.event_config_context, text)
    if not payload.get("ok") or payload.get("configTooNew"):
        return payload
    bad = [item for item in payload["issues"] if item.get("level") == "bad"]
    if bad:
        return {
            **payload,
            "ok": False,
            "error": "config has blocking validation errors",
        }
    snapshot = config_transaction.create_config_snapshot(
        context.transaction_context, "config.save", actor, note,
    )
    context.atomic_write_text(context.config_path, payload["normalizedText"])
    config_transaction.append_history(
        context.transaction_context,
        "config.save",
        actor,
        note,
        {"transactionId": snapshot["id"], "snapshot": snapshot["path"]},
    )
    return {
        **event_config.config_payload(context.event_config_context),
        "transactionId": snapshot["id"],
        "snapshot": snapshot["path"],
    }


def apply_config(
    context: ConfigWriteContext,
    text: str | None,
    actor: str = "",
    note: str = "",
    operation_id: str | None = None,
) -> dict:
    _require_write(context)
    operation_id = config_transaction.normalize_operation_id(
        operation_id, "apply",
    )
    blocked = event_config.current_config_write_guard(
        context.event_config_context,
    )
    if blocked:
        return {**blocked, "operationId": operation_id}
    try:
        payload = event_config.config_payload(context.event_config_context, text)
    except Exception as exc:
        return {
            "ok": False,
            "operationId": operation_id,
            "error": f"应用配置失败：{exc}",
        }
    if not payload.get("ok") or payload.get("configTooNew"):
        return {**payload, "operationId": operation_id}
    started_at = int(context.clock())
    operation_timeout = apply_runtime.apply_operation_timeout_seconds(
        context.apply_runtime_context,
    )
    config_transaction.write_apply_status(
        context.transaction_context,
        operation_id,
        "running",
        action="apply",
        startedAt=started_at,
        timeoutSeconds=operation_timeout,
        deadlineAt=started_at + operation_timeout,
    )
    snapshot = None
    try:
        bad = [item for item in payload["issues"] if item.get("level") == "bad"]
        if bad:
            result = {
                **payload,
                "ok": False,
                "error": "config has blocking validation errors",
                "operationId": operation_id,
            }
            config_transaction.write_apply_status(
                context.transaction_context,
                operation_id,
                "failed",
                action="apply",
                error=result["error"],
            )
            return result

        snapshot = config_transaction.create_config_snapshot(
            context.transaction_context, "config.apply", actor, note,
        )
        if text is not None or payload.get("migrationRequired"):
            context.atomic_write_text(
                context.config_path, payload["normalizedText"],
            )
        rendered = context.merge_env_file(context.env_path, payload["env"])
        context.atomic_write_text(context.env_path, rendered)
        config_transaction.append_history(
            context.transaction_context,
            "config.apply",
            actor,
            note,
            {
                "operationId": operation_id,
                "transactionId": snapshot["id"],
                "snapshot": snapshot["path"],
                "envKeys": sorted(payload["env"]),
            },
        )
        apply_result = apply_runtime.run_apply_command(
            context.apply_runtime_context,
        )
        failed = apply_result.get("ok") is False
        rollback_result = None
        restored = None
        if failed:
            restored = config_transaction.restore_config_snapshot(
                context.transaction_context, Path(snapshot["path"]),
            )
            rollback_result = apply_runtime.run_apply_command(
                context.apply_runtime_context,
            )
        config_transaction.append_history(
            context.transaction_context,
            "config.apply_command",
            actor,
            note,
            {
                "operationId": operation_id,
                "transactionId": snapshot["id"],
                "applied": bool(apply_result.get("applied")),
                "needsRedeploy": bool(apply_result.get("needsRedeploy")),
                "error": apply_result.get("error", ""),
                "rolledBack": bool(restored),
                "runtimeRestored": bool(
                    rollback_result and rollback_result.get("applied")
                ),
            },
        )
        if failed:
            result = {
                **event_config.config_payload(context.event_config_context),
                **apply_result,
                "ok": False,
                "operationId": operation_id,
                "transactionId": snapshot["id"],
                "rolledBack": True,
                "restored": restored,
                "rollbackApply": rollback_result,
            }
            config_transaction.write_apply_status(
                context.transaction_context,
                operation_id,
                "failed",
                action="apply",
                error=apply_result.get("error", "应用失败"),
                rolledBack=True,
                runtimeRestored=bool(
                    rollback_result and rollback_result.get("applied")
                ),
                applyOutput=apply_result.get("applyOutput", ""),
            )
            return result

        state = "succeeded" if apply_result.get("applied") else "pending"
        status = config_transaction.write_apply_status(
            context.transaction_context,
            operation_id,
            state,
            action="apply",
            applied=bool(apply_result.get("applied")),
            needsRedeploy=bool(apply_result.get("needsRedeploy")),
            applyOutput=apply_result.get("applyOutput", ""),
        )
        return {
            **event_config.config_payload(context.event_config_context),
            **apply_result,
            "operationId": operation_id,
            "transactionId": snapshot["id"],
            "state": status["state"],
        }
    except Exception as exc:
        restored = None
        rollback_result = None
        if snapshot:
            try:
                restored = config_transaction.restore_config_snapshot(
                    context.transaction_context, Path(snapshot["path"]),
                )
                rollback_result = apply_runtime.run_apply_command(
                    context.apply_runtime_context,
                )
            except Exception as rollback_exc:
                rollback_result = {"ok": False, "error": str(rollback_exc)}
        config_transaction.write_apply_status(
            context.transaction_context,
            operation_id,
            "failed",
            action="apply",
            error=str(exc),
            rolledBack=bool(restored),
            runtimeRestored=bool(
                rollback_result and rollback_result.get("applied")
            ),
        )
        return {
            "ok": False,
            "operationId": operation_id,
            "error": f"应用配置失败：{exc}",
            "rolledBack": bool(restored),
            "rollbackApply": rollback_result,
        }


def rollback_config(
    context: ConfigWriteContext,
    actor: str = "",
    note: str = "",
    operation_id: str | None = None,
) -> dict:
    _require_write(context)
    operation_id = config_transaction.normalize_operation_id(
        operation_id, "rollback",
    )
    blocked = event_config.current_config_write_guard(
        context.event_config_context,
    )
    if blocked:
        return {**blocked, "operationId": operation_id}
    started_at = int(context.clock())
    operation_timeout = apply_runtime.apply_operation_timeout_seconds(
        context.apply_runtime_context,
    )
    config_transaction.write_apply_status(
        context.transaction_context,
        operation_id,
        "running",
        action="rollback",
        startedAt=started_at,
        timeoutSeconds=operation_timeout,
        deadlineAt=started_at + operation_timeout,
    )
    snapshots = config_transaction.list_config_snapshots(
        context.transaction_context,
    )
    if not snapshots:
        error_message = "没有可用的一致性配置快照；旧版分散备份不会自动混合回滚"
        config_transaction.write_apply_status(
            context.transaction_context,
            operation_id,
            "failed",
            action="rollback",
            error=error_message,
        )
        return {"ok": False, "operationId": operation_id, "error": error_message}

    target = snapshots[0]
    guard = config_transaction.create_config_snapshot(
        context.transaction_context, "config.rollback.guard", actor, note,
    )
    try:
        restored = config_transaction.restore_config_snapshot(
            context.transaction_context, target,
        )
        apply_result = apply_runtime.run_apply_command(
            context.apply_runtime_context,
        )
        if apply_result.get("ok") is False:
            config_transaction.restore_config_snapshot(
                context.transaction_context, Path(guard["path"]),
            )
            recovery_result = apply_runtime.run_apply_command(
                context.apply_runtime_context,
            )
            error_message = apply_result.get("error", "回滚后的服务应用失败")
            config_transaction.append_history(
                context.transaction_context,
                "config.rollback_failed",
                actor,
                note,
                {
                    "operationId": operation_id,
                    "targetTransactionId": restored.get("transactionId"),
                    "guardTransactionId": guard["id"],
                    "error": error_message,
                    "runtimeRestored": bool(recovery_result.get("applied")),
                },
            )
            config_transaction.write_apply_status(
                context.transaction_context,
                operation_id,
                "failed",
                action="rollback",
                error=error_message,
                rolledBack=True,
                runtimeRestored=bool(recovery_result.get("applied")),
            )
            return {
                **event_config.config_payload(context.event_config_context),
                "ok": False,
                "operationId": operation_id,
                "error": error_message,
                "rolledBack": True,
                "rollbackApply": recovery_result,
            }

        state = "succeeded" if apply_result.get("applied") else "pending"
        config_transaction.mark_config_snapshot_consumed(target)
        config_transaction.append_history(
            context.transaction_context,
            "config.rollback",
            actor,
            note,
            {
                "operationId": operation_id,
                "targetTransactionId": restored.get("transactionId"),
                "guardTransactionId": guard["id"],
                "restored": restored,
                "applied": bool(apply_result.get("applied")),
            },
        )
        config_transaction.write_apply_status(
            context.transaction_context,
            operation_id,
            state,
            action="rollback",
            applied=bool(apply_result.get("applied")),
            needsRedeploy=bool(apply_result.get("needsRedeploy")),
            restored=restored,
            applyOutput=apply_result.get("applyOutput", ""),
        )
        return {
            **event_config.config_payload(context.event_config_context),
            **apply_result,
            "operationId": operation_id,
            "restored": restored,
            "state": state,
        }
    except Exception as exc:
        try:
            config_transaction.restore_config_snapshot(
                context.transaction_context, Path(guard["path"]),
            )
        except Exception:
            pass
        config_transaction.write_apply_status(
            context.transaction_context,
            operation_id,
            "failed",
            action="rollback",
            error=str(exc),
        )
        return {
            "ok": False,
            "operationId": operation_id,
            "error": f"回滚失败：{exc}",
        }


def handle_post(
    context: ConfigWriteContext,
    handler: Any,
    path: str,
    data: dict,
) -> bool:
    """Handle one normalized config write path, returning whether it matched."""
    if path not in {
        "/config/save",
        "/config/apply",
        "/config/rollback",
        "/config/import",
    }:
        return False

    auth = context.require_auth(handler)
    with context.write_lock:
        if path == "/config/save":
            payload = save_config(
                context,
                data.get("text", ""),
                auth["username"],
                data.get("note", ""),
            )
        elif path == "/config/apply":
            text = data.get("text") if "text" in data else None
            payload = apply_config(
                context,
                text,
                auth["username"],
                data.get("note", ""),
                data.get("operationId"),
            )
        elif path == "/config/rollback":
            payload = rollback_config(
                context,
                auth["username"],
                data.get("note", ""),
                data.get("operationId"),
            )
        else:
            payload = save_config(
                context, data.get("text", ""), auth["username"], "import",
            )
        handler._send_json(payload)
    return True
