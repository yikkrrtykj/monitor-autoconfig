"""Environment-backed settings shared by the platform API core."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Mapping


TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def env_bool(
    name: str,
    default: bool = False,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Read a boolean with the entrypoint's original exact-value semantics."""
    source = os.environ if environ is None else environ
    fallback = "true" if default else "false"
    return str(source.get(name, fallback)).lower() in TRUE_VALUES


def env_int(
    name: str,
    default: int,
    minimum: int | None = None,
    maximum: int | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Read an integer; empty or invalid values deliberately still raise."""
    source = os.environ if environ is None else environ
    value = int(source.get(name, str(default)))
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def env_float(
    name: str,
    default: float,
    environ: Mapping[str, str] | None = None,
) -> float:
    """Read a float; empty or invalid values deliberately still raise."""
    source = os.environ if environ is None else environ
    return float(source.get(name, str(default)))


@dataclass(frozen=True)
class CoreSettings:
    workdir: Path
    state_dir: Path
    config_path: Path
    example_path: Path
    env_path: Path
    incident_path: Path
    auth_path: Path
    dhcp_settings_path: Path
    iperf_history_path: Path
    history_dir: Path
    transaction_dir: Path
    apply_status_dir: Path
    write_enabled: bool
    apply_enabled: bool
    apply_command: str
    apply_timeout: int
    apply_verify_timeout: int
    max_request_body_bytes: int
    auth_enabled: bool
    auth_admin_user: str
    auth_default_password: str
    auth_cookie_name: str
    auth_cookie_secure: bool
    auth_session_seconds: int
    password_min_length: int
    auth_failure_window_seconds: int
    auth_failure_limit: int
    auth_lock_seconds: int
    transaction_retention: int
    apply_status_retention: int


def load_settings(environ: Mapping[str, str] | None = None) -> CoreSettings:
    """Build one import-time settings snapshot, matching the old entrypoint."""
    source = os.environ if environ is None else environ
    workdir = Path(source.get("PLATFORM_WORKDIR", "/workspace"))
    state_dir = Path(source.get("PLATFORM_STATE_DIR", str(workdir / "platform-state")))
    history_dir = state_dir / "history"
    return CoreSettings(
        workdir=workdir,
        state_dir=state_dir,
        config_path=Path(source.get("EVENT_CONFIG_FILE", str(workdir / "event-config.yml"))),
        example_path=Path(source.get("EVENT_CONFIG_EXAMPLE", str(workdir / "event-config.example.yml"))),
        env_path=Path(source.get("ENV_FILE", str(workdir / ".env"))),
        incident_path=state_dir / "incidents.json",
        auth_path=state_dir / "auth.json",
        dhcp_settings_path=state_dir / "dhcp-settings.json",
        iperf_history_path=state_dir / "iperf-history.json",
        history_dir=history_dir,
        transaction_dir=history_dir / "transactions",
        apply_status_dir=state_dir / "apply-status",
        write_enabled=env_bool("PLATFORM_WRITE_ENABLED", True, source),
        apply_enabled=env_bool("PLATFORM_APPLY_ENABLED", True, source),
        apply_command=source.get("PLATFORM_APPLY_COMMAND", "/bin/sh /workspace/apply-env.sh"),
        apply_timeout=env_int("PLATFORM_APPLY_TIMEOUT", 300, minimum=30, environ=source),
        apply_verify_timeout=env_int(
            "PLATFORM_APPLY_VERIFY_TIMEOUT", 90, minimum=10, environ=source,
        ),
        max_request_body_bytes=env_int(
            "PLATFORM_MAX_REQUEST_BODY_BYTES", 1024 * 1024,
            minimum=1024, environ=source,
        ),
        auth_enabled=env_bool("PLATFORM_AUTH_ENABLED", True, source),
        auth_admin_user=source.get("PLATFORM_ADMIN_USER", "admin"),
        auth_default_password=source.get("PLATFORM_ADMIN_PASSWORD", "global"),
        auth_cookie_name=source.get("PLATFORM_SESSION_COOKIE", "platform_session"),
        auth_cookie_secure=env_bool("PLATFORM_COOKIE_SECURE", False, source),
        auth_session_seconds=max(
            600,
            int(env_float("PLATFORM_SESSION_HOURS", 8, source) * 3600),
        ),
        password_min_length=env_int(
            "PLATFORM_PASSWORD_MIN_LENGTH", 10, minimum=10, environ=source,
        ),
        auth_failure_window_seconds=env_int(
            "PLATFORM_AUTH_FAILURE_WINDOW_SECONDS", 300, minimum=30,
            environ=source,
        ),
        auth_failure_limit=env_int(
            "PLATFORM_AUTH_FAILURE_LIMIT", 5, minimum=3, environ=source,
        ),
        auth_lock_seconds=env_int(
            "PLATFORM_AUTH_LOCK_SECONDS", 900, minimum=30, environ=source,
        ),
        transaction_retention=env_int(
            "PLATFORM_TRANSACTION_RETENTION", 50, minimum=5, environ=source,
        ),
        apply_status_retention=env_int(
            "PLATFORM_APPLY_STATUS_RETENTION", 200, minimum=10, environ=source,
        ),
    )
