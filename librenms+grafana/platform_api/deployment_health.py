"""Read-only deployment interpretation of the existing Bridge /health payload.

No Bridge imports, notifications, device actions or monitoring-state writes.
tokenConfigured is a Feishu webhook flag, never a LibreNMS API token flag.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

BRIDGE_HEALTH_MAX_BYTES = 65536


def notification_requirements(env: dict) -> dict[str, bool]:
    profiles = str(env.get("COMPOSE_PROFILES", "")).split(",")
    return {
        "tokenConfigured": bool(str(env.get("FEISHU_ROBOT_TOKEN", "")).strip()),
        "appConfigured": bool(str(env.get("FEISHU_APP_ID", "")).strip()
                              or str(env.get("FEISHU_APP_SECRET", "")).strip()
                              or "feishu" in profiles),
        "dryRun": str(env.get("FEISHU_BRIDGE_DRY_RUN", "")).strip().lower() in ("1", "true", "yes", "on"),
    }


def validate_bridge_health(payload: object, requirements: dict | None = None) -> bool:
    """Raise a safe error on failure; return whether notifications are enabled."""
    if not isinstance(payload, dict):
        raise ValueError("Bridge health must be an object")
    for key in ("ok", "ready", "dryRun", "tokenConfigured", "appConfigured"):
        if type(payload.get(key)) is not bool:
            raise ValueError("Bridge health has missing or invalid boolean fields")
    if payload["ok"] is not True:
        raise ValueError("Bridge liveness failed")
    dead = payload.get("deadWatchers")
    watchers = payload.get("watchers")
    if not isinstance(dead, list) or not all(isinstance(name, str) for name in dead):
        raise ValueError("Bridge deadWatchers field is invalid")
    if not isinstance(watchers, dict) or "device-online" not in watchers:
        raise ValueError("Bridge required watcher is missing")
    if dead or any(not isinstance(state, dict) or state.get("alive") is not True
                   for state in watchers.values()):
        raise ValueError("Bridge watcher is dead or invalid")
    requirements = requirements or {}
    if any(required and payload.get(key) is not True for key, required in requirements.items()):
        raise ValueError("Bridge configured notification capability is not available")
    enabled = any(payload[key] for key in ("tokenConfigured", "appConfigured", "dryRun"))
    if payload["ready"] != enabled:
        raise ValueError("Bridge readiness does not match enabled capabilities")
    return enabled


def main() -> int:
    # Imported lazily so apply_runtime only needs the pure validator.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from platform_config import read_env
    try:
        envelope = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        env = read_env(Path(sys.argv[2]))
        # Match deploy-check's existing effective profile precedence.
        if "COMPOSE_PROFILES" in os.environ:
            env["COMPOSE_PROFILES"] = os.environ["COMPOSE_PROFILES"]
        enabled = validate_bridge_health(envelope["health"], notification_requirements(env))
        if envelope.get("librenmsTokenAvailable") is not True:
            raise ValueError("LibreNMS token unavailable to Bridge consumer")
    except (ValueError, KeyError, TypeError, OSError):
        print("Bridge health, enabled capabilities or LibreNMS token availability failed validation", file=sys.stderr)
        return 1
    print("PASS\tbridge_liveness\tBridge health payload and watchers valid")
    if enabled:
        print("PASS\tbridge_notifications\tEnabled Bridge capabilities ready (delivery not tested)")
    else:
        print("SKIP\tbridge_notifications\tFeishu not configured; notification readiness intentionally skipped")
    print("PASS\tlibrenms_consumer_token\tLibreNMS token available to Bridge (no API mutation)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
