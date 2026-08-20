"""Config snapshots, history, and apply-status persistence."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import secrets
import shutil
import time

from .storage import atomic_write_text, read_json_file, write_json_file


@dataclass(frozen=True)
class ConfigTransactionContext:
    config_path: Path
    env_path: Path
    history_path: Path
    transaction_dir: Path
    apply_status_dir: Path
    transaction_retention: int = 50
    apply_status_retention: int = 200


def new_operation_id(prefix: str = "op") -> str:
    return (
        f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}-"
        f"{time.time_ns() % 1_000_000_000:09d}-{secrets.token_hex(3)}"
    )


def normalize_operation_id(value: str | None, prefix: str = "op") -> str:
    clean = str(value or "").strip()
    if clean and re.fullmatch(r"[A-Za-z0-9_-]{8,96}", clean):
        return clean
    return new_operation_id(prefix)


def apply_status_path(
    context: ConfigTransactionContext,
    operation_id: str,
) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,96}", str(operation_id or "")):
        raise ValueError("invalid operation id")
    return context.apply_status_dir / f"{operation_id}.json"


def prune_retained_paths(paths, keep: int) -> None:
    ordered = sorted(
        (path for path in paths if path.exists()),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    for path in ordered[max(1, keep):]:
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as exc:
            print(
                f"[platform-api] state retention cleanup failed for {path}: {exc}",
                flush=True,
            )


def prune_generated_state(context: ConfigTransactionContext) -> None:
    if context.transaction_dir.exists():
        prune_retained_paths(
            context.transaction_dir.iterdir(), context.transaction_retention,
        )
    if context.apply_status_dir.exists():
        prune_retained_paths(
            context.apply_status_dir.glob("*.json"),
            context.apply_status_retention,
        )


def write_apply_status(
    context: ConfigTransactionContext,
    operation_id: str,
    state: str,
    **detail,
) -> dict:
    payload = {
        "ok": state in ("succeeded", "pending"),
        "operationId": operation_id,
        "state": state,
        "updatedAt": int(time.time()),
        **detail,
    }
    write_json_file(apply_status_path(context, operation_id), payload)
    prune_generated_state(context)
    return payload


def read_apply_status(
    context: ConfigTransactionContext,
    operation_id: str,
) -> dict:
    clean = str(operation_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,96}", clean):
        return {
            "ok": False, "operationId": clean, "state": "unknown",
            "error": "无效的应用任务编号",
        }
    path = context.apply_status_dir / f"{clean}.json"
    if not path.exists():
        return {
            "ok": False, "operationId": clean, "state": "unknown",
            "error": "找不到该应用任务",
        }
    return read_json_file(
        path, {"ok": False, "operationId": clean, "state": "unknown"},
    )


def create_config_snapshot(
    context: ConfigTransactionContext,
    action: str,
    actor: str = "",
    note: str = "",
) -> dict:
    transaction_id = new_operation_id("txn")
    directory = context.transaction_dir / transaction_id
    directory.mkdir(parents=True, exist_ok=False)
    metadata = {
        "id": transaction_id,
        "action": action,
        "actor": actor,
        "note": note,
        "createdAt": int(time.time()),
        "configExisted": context.config_path.exists(),
        "envExisted": context.env_path.exists(),
    }
    if context.config_path.exists():
        shutil.copy2(context.config_path, directory / "event-config.yml")
    if context.env_path.exists():
        shutil.copy2(context.env_path, directory / ".env")
    write_json_file(directory / "metadata.json", metadata)
    prune_generated_state(context)
    return {**metadata, "path": str(directory)}


def list_config_snapshots(context: ConfigTransactionContext) -> list[Path]:
    if not context.transaction_dir.exists():
        return []
    eligible = []
    for path in context.transaction_dir.iterdir():
        if not path.is_dir():
            continue
        metadata = read_json_file(path / "metadata.json", {})
        if (
            metadata.get("action") == "config.rollback.guard"
            or metadata.get("consumedAt")
        ):
            continue
        eligible.append(path)
    return sorted(eligible, reverse=True)


def mark_config_snapshot_consumed(directory: Path) -> None:
    metadata_path = directory / "metadata.json"
    metadata = read_json_file(metadata_path, {})
    if not metadata:
        return
    metadata["consumedAt"] = int(time.time())
    write_json_file(metadata_path, metadata)


def restore_config_snapshot(
    context: ConfigTransactionContext,
    directory: Path,
) -> dict:
    metadata = read_json_file(directory / "metadata.json", {})
    if not metadata:
        raise ValueError(f"invalid config snapshot: {directory}")
    restored = {"transactionId": metadata.get("id") or directory.name}
    config_backup = directory / "event-config.yml"
    env_backup = directory / ".env"
    if metadata.get("configExisted"):
        atomic_write_text(
            context.config_path, config_backup.read_text(encoding="utf-8"),
        )
        restored["config"] = str(config_backup)
    elif context.config_path.exists():
        context.config_path.unlink()
        restored["config"] = "removed"
    if metadata.get("envExisted"):
        atomic_write_text(
            context.env_path, env_backup.read_text(encoding="utf-8"),
        )
        restored["env"] = str(env_backup)
    elif context.env_path.exists():
        context.env_path.unlink()
        restored["env"] = "removed"
    return restored


def append_history(
    context: ConfigTransactionContext,
    action: str,
    actor: str,
    note: str,
    detail: dict,
) -> None:
    history = read_json_file(context.history_path, [])
    history.insert(0, {
        "time": int(time.time()),
        "action": action,
        "actor": actor,
        "note": note,
        "detail": detail,
    })
    write_json_file(context.history_path, history[:200])
