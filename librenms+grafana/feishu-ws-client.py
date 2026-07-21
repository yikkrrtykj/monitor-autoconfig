#!/usr/bin/env python3
"""Feishu long-connection callback client for in-card confirm buttons.

The bridge sends "device pending delete" cards with 确认删除/保留 buttons via
the Feishu app bot. This sidecar keeps an OUTBOUND WebSocket to Feishu's cloud
(长连接模式) and receives the button clicks — no public IP, no port mapping,
no inbound HTTPS needed, which matches venue networks that can reach the
internet but cannot be reached from it.

Each click is forwarded to the alert bridge's /retire/resolve, which owns the
device state and performs the actual (token-guarded, reachability-checked)
LibreNMS deletion. The response updates the card in place and shows a toast.

Env:
  FEISHU_APP_ID / FEISHU_APP_SECRET  self-built app credentials (required)
  BRIDGE_URL   bridge base URL (default http://alertmanager-feishu-bridge:5005)

Feishu console prerequisites (one-time, see .env.example):
  自建应用 -> 开启机器人能力 -> 事件与回调选择"使用长连接接收" ->
  订阅卡片回传交互(card.action.trigger) -> 把应用机器人加进告警群。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

APP_ID = os.environ.get("FEISHU_APP_ID", "").strip()
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "").strip()
BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://alertmanager-feishu-bridge:5005").rstrip("/")


def log(message: str) -> None:
    print(f"[feishu-ws] {message}", file=sys.stderr, flush=True)


def resolve_via_bridge(value: dict) -> dict:
    """Forward the button's value to the bridge; it validates token + state."""
    payload = json.dumps({
        "key": str(value.get("key") or ""),
        "action": "delete" if value.get("action") == "retire_delete" else "keep",
        "token": str(value.get("token") or ""),
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{BRIDGE_URL}/retire/resolve", data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8", errors="replace") or "{}")
        except json.JSONDecodeError:
            return {"ok": False, "error": f"告警服务返回 HTTP {exc.code}"}
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator as a toast
        return {"ok": False, "error": f"无法连接告警服务：{exc}"}


def build_response(value: dict, result: dict):
    """Toast + in-place card update so the group sees the outcome."""
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        P2CardActionTriggerResponse,
    )
    ok = bool(result.get("ok"))
    message = str(result.get("message") or result.get("error") or ("已处理" if ok else "处理失败"))
    device = str(value.get("device") or "")
    if ok and result.get("action") == "delete":
        status_line = f"✅ 已确认删除：{device}"
        template = "green"
    elif ok:
        status_line = f"🟢 已保留：{device}，继续监控"
        template = "green"
    else:
        status_line = f"⚠️ {message}"
        template = "orange"
    card = {
        "type": "raw",
        "data": {
            "schema": "2.0",
            "header": {
                "title": {"tag": "plain_text", "content": "设备待删除确认"},
                "subtitle": {"tag": "plain_text", "content": "已处理" if ok else "处理失败"},
                "template": template,
            },
            "body": {
                "direction": "vertical",
                "elements": [{
                    "tag": "markdown",
                    "content": f"{status_line}\n处理时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
                }],
            },
        },
    }
    return P2CardActionTriggerResponse({
        "toast": {"type": "success" if ok else "error", "content": message},
        "card": card,
    })


def on_card_action(data):
    action = getattr(getattr(data, "event", None), "action", None)
    value = dict(getattr(action, "value", None) or {})
    if value.get("action") not in ("retire_delete", "retire_keep"):
        return None  # 其它卡片的回传交给未来的处理器，别误吞
    operator = getattr(getattr(data, "event", None), "operator", None)
    who = getattr(operator, "open_id", "") or getattr(operator, "user_id", "") or "?"
    log(f"card action {value.get('action')} key={value.get('key')} by={who}")
    result = resolve_via_bridge(value)
    log(f"bridge result: {json.dumps(result, ensure_ascii=False)[:200]}")
    return build_response(value, result)


def main() -> None:
    if not APP_ID or not APP_SECRET:
        log("FEISHU_APP_ID/FEISHU_APP_SECRET not configured; sleeping. "
            "Configure the self-built app in .env to enable in-card confirmation.")
        while True:
            time.sleep(3600)

    import lark_oapi as lark

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_card_action_trigger(on_card_action)
        .build()
    )
    while True:
        try:
            log("starting long-connection client")
            client = lark.ws.Client(
                APP_ID, APP_SECRET,
                event_handler=handler,
                log_level=lark.LogLevel.INFO,
            )
            client.start()  # blocks; SDK reconnects internally
        except Exception as exc:  # noqa: BLE001 - keep the sidecar alive
            log(f"ws client stopped: {exc}; retrying in 15s")
            time.sleep(15)


if __name__ == "__main__":
    main()
