"""HTTP proxies from the platform API to the Feishu bridge service."""
from __future__ import annotations

import json
import urllib.error
import urllib.request


def bridge_retire_pending(bridge_url: str) -> dict:
    """Fetch the bridge's pending-delete device list (48h+ offline, unconfirmed)."""
    try:
        with urllib.request.urlopen(
            f"{bridge_url}/retire/pending", timeout=8,
        ) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except Exception as exc:
        return {
            "ok": False,
            "enabled": False,
            "error": f"无法连接告警服务：{exc}",
            "pending": [],
        }


def bridge_retire_resolve(bridge_url: str, data: dict) -> dict:
    """Forward a confirm/keep decision to the bridge (which owns the state)."""
    payload = json.dumps({
        "key": str(data.get("key") or ""),
        "action": str(data.get("action") or ""),
        "token": str(data.get("token") or ""),
    }).encode("utf-8")
    request_obj = urllib.request.Request(
        f"{bridge_url}/retire/resolve", data=payload, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request_obj, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(
                exc.read().decode("utf-8", errors="replace") or "{}"
            )
        except json.JSONDecodeError:
            return {"ok": False, "error": f"告警服务返回 HTTP {exc.code}"}
    except Exception as exc:
        return {"ok": False, "error": f"无法连接告警服务：{exc}"}


def send_test_alert(bridge_url: str) -> dict:
    """Ask the Feishu bridge to push a test card for an operator check."""
    request = urllib.request.Request(
        f"{bridge_url}/test-alert", data=b"{}", method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except Exception as exc:
        return {"ok": False, "error": f"无法连接告警服务：{exc}"}
