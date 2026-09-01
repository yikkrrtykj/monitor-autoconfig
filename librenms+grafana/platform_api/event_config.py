"""Event configuration parsing, schema, migration, and payload helpers."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from platform_config import (
    ConfigSchemaError,
    default_config_text,
    dump_simple_yaml,
    inspect_config_schema,
    migrate_config,
    parse_simple_yaml,
    read_env,
    render_env,
    validate_config,
)


_HIDDEN_ALERT_ENV_FIELDS = (
    ("gateway_macs", "SYSLOG_GATEWAY_MACS", False),
    ("gateway_uplink_ports", "SYSLOG_GATEWAY_UPLINK_PORTS", False),
    ("mac_flap_window_seconds", "SYSLOG_MAC_FLAP_WINDOW_SECONDS", True),
    ("mac_flap_threshold", "SYSLOG_MAC_FLAP_THRESHOLD", True),
    ("cpu_alert_percent", "DEVICE_CPU_ALERT_PERCENT", True),
    ("memory_alert_percent", "DEVICE_MEMORY_ALERT_PERCENT", True),
)


@dataclass(frozen=True)
class EventConfigContext:
    config_path: Path
    example_path: Path
    env_path: Path
    state_dir: Path
    write_enabled: bool
    get_version_info: Callable[[], dict]


def read_config_text(context: EventConfigContext) -> str:
    if context.config_path.exists():
        return context.config_path.read_text(encoding="utf-8")
    return default_config_text(context.example_path)


def parse_config_text(text: str) -> dict:
    config = parse_simple_yaml(text)
    if not isinstance(config, dict):
        raise ValueError("event config must be a mapping")
    return config


def _schema_response(status: dict) -> dict:
    return {
        "configSchemaOriginal": status["original_version"],
        "configSchemaCurrent": status["current_version"],
        "configSchemaSupported": status["current_supported"],
        "migrationRequired": status["migration_required"],
        "configTooNew": status["config_too_new"],
    }


def _schema_error_payload(message: str, config: dict | None = None) -> dict:
    return {
        "ok": False,
        "error": message,
        "config": config or {},
        "issues": [{"level": "bad", "path": "schema_version", "message": message}],
        "env": {},
        "normalizedText": "",
        "writeEnabled": False,
    }


def current_config_write_guard(context: EventConfigContext) -> dict | None:
    """Refuse every config mutation when the on-disk file is not writable by
    this schema version. The check happens before snapshots or target parsing so
    an older platform cannot replace a newer config with submitted schema 1."""
    if not context.config_path.exists():
        return None
    try:
        config = parse_config_text(context.config_path.read_text(encoding="utf-8"))
        status = inspect_config_schema(config)
    except (OSError, ValueError) as exc:
        return _schema_error_payload(f"Cannot modify event config: {exc}")
    if status["config_too_new"]:
        message = (
            f"Refusing to modify schema {status['original_version']}; "
            f"software supports schema {status['current_supported']}. "
            "Upgrade the monitoring platform first."
        )
        return {**_schema_error_payload(message, config), **_schema_response(status)}
    return None


def _current_alert_values(context: EventConfigContext) -> dict:
    if not context.config_path.exists():
        return {}
    try:
        current = migrate_config(parse_config_text(
            context.config_path.read_text(encoding="utf-8")
        ))
    except (OSError, ValueError, ConfigSchemaError):
        return {}
    alerts = current.get("alerts")
    return alerts if isinstance(alerts, dict) else {}


def _coerce_hidden_alert_value(value: str, numeric: bool):
    if not numeric:
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    return int(number) if number.is_integer() else number


def _preserve_hidden_alert_values(
    context: EventConfigContext,
    config: dict,
    existing_env: dict[str, str],
    *,
    editing_existing: bool,
) -> None:
    """Keep advanced alert values that the normal UI intentionally omits.

    Existing YAML remains authoritative. Older installations may have these
    settings only in .env, so missing keys inherit that value. Submitted
    configurations also inherit missing keys from the current on-disk config;
    an explicitly supplied blank/value is never overwritten.
    """
    alerts = config.get("alerts")
    if alerts is not None and not isinstance(alerts, dict):
        return
    current_alerts = (alerts or {}) if editing_existing else _current_alert_values(context)
    preserved = {}
    for config_key, env_key, numeric in _HIDDEN_ALERT_ENV_FIELDS:
        if isinstance(alerts, dict) and config_key in alerts:
            continue
        if config_key in current_alerts:
            preserved[config_key] = current_alerts[config_key]
        elif env_key in existing_env:
            preserved[config_key] = _coerce_hidden_alert_value(
                existing_env[env_key], numeric,
            )
    if preserved:
        config.setdefault("alerts", {}).update(preserved)


def config_payload(context: EventConfigContext, text: str | None = None) -> dict:
    editing_existing = text is None
    text = read_config_text(context) if editing_existing else text
    config = parse_config_text(text)
    try:
        schema = inspect_config_schema(config)
    except ConfigSchemaError as exc:
        return {
            **_schema_error_payload(str(exc), config),
            "text": text,
            "paths": {
                "config": str(context.config_path),
                "env": str(context.env_path),
                "state": str(context.state_dir),
            },
        }
    if schema["config_too_new"]:
        message = (
            f"event-config schema {schema['original_version']} is newer than supported "
            f"schema {schema['current_supported']}; upgrade the monitoring platform first"
        )
        return {
            **_schema_error_payload(message, config),
            **_schema_response(schema),
            "ok": editing_existing,
            "readOnly": True,
            "text": text,
            "paths": {
                "config": str(context.config_path),
                "env": str(context.env_path),
                "state": str(context.state_dir),
            },
        }

    config = migrate_config(config)
    existing_env = read_env(context.env_path)
    _preserve_hidden_alert_values(
        context, config, existing_env, editing_existing=editing_existing,
    )
    # Migrate legacy .env-only application credentials into the authenticated
    # editor model.  They are then visible beside the old webhook token and are
    # persisted to event-config.yml on the next save/apply.  Do not do this for
    # submitted text: an operator must still be able to clear a credential.
    if editing_existing:
        alerts = config.setdefault("alerts", {})
        if isinstance(alerts, dict):
            for config_key, env_key in (
                ("feishu_app_id", "FEISHU_APP_ID"),
                ("feishu_app_secret", "FEISHU_APP_SECRET"),
                ("feishu_chat_id", "FEISHU_CHAT_ID"),
            ):
                if config_key not in alerts and existing_env.get(env_key):
                    alerts[config_key] = existing_env[env_key]
    issues = validate_config(config)
    env = render_env(config, existing_env)
    return {
        "ok": True,
        "text": text,
        "config": config,
        "normalizedText": dump_simple_yaml(config) + "\n",
        "issues": issues,
        "env": env,
        "writeEnabled": context.write_enabled,
        **_schema_response(schema),
        "paths": {
            "config": str(context.config_path),
            "env": str(context.env_path),
            "state": str(context.state_dir),
        },
    }


def version_payload(context: EventConfigContext) -> dict:
    payload = {"ok": True, **context.get_version_info()}
    try:
        config = parse_config_text(read_config_text(context))
        status = inspect_config_schema(config)
    except (OSError, ValueError) as exc:
        return {
            **payload,
            "config_schema_original": None,
            "config_schema_current": None,
            "migration_required": False,
            "config_too_new": False,
            "config_schema_error": str(exc),
        }
    return {
        **payload,
        "config_schema_original": status["original_version"],
        "config_schema_current": status["current_version"],
        "migration_required": status["migration_required"],
        "config_too_new": status["config_too_new"],
    }
