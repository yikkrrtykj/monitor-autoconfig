#!/usr/bin/env python3
"""Feishu long-connection client for card callbacks and @ query commands.

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
  订阅卡片回传交互(card.action.trigger) 和接收消息(im.message.receive_v1)
  -> 把应用机器人加进告警群。
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

APP_ID = os.environ.get("FEISHU_APP_ID", "").strip()
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "").strip()
BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://alertmanager-feishu-bridge:5005").rstrip("/")
_TENANT_TOKEN = {"value": "", "expires_at": 0.0}
_TOKEN_LOCK = threading.Lock()
_SEEN_MESSAGES: dict[str, float] = {}
_SEEN_LOCK = threading.Lock()


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


def query_via_bridge(text: str) -> dict:
    payload = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{BRIDGE_URL}/bot/query", data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except Exception as exc:  # noqa: BLE001 - converted to a user-visible reply
        log(f"bot query bridge failed: {exc}")
        return {"ok": False, "text": "查询监控数据失败，请稍后再试。"}


def tenant_access_token() -> str:
    now = time.time()
    with _TOKEN_LOCK:
        if _TENANT_TOKEN["value"] and now < _TENANT_TOKEN["expires_at"] - 120:
            return _TENANT_TOKEN["value"]
    payload = json.dumps({"app_id": APP_ID, "app_secret": APP_SECRET}).encode("utf-8")
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=payload, headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8") or "{}")
    if data.get("code") != 0 or not data.get("tenant_access_token"):
        raise RuntimeError(f"tenant token rejected: {data.get('code')} {data.get('msg')}")
    with _TOKEN_LOCK:
        _TENANT_TOKEN["value"] = data["tenant_access_token"]
        _TENANT_TOKEN["expires_at"] = now + float(data.get("expire") or 3600)
        return _TENANT_TOKEN["value"]


def reply_to_message(message_id: str, text: str) -> None:
    token = tenant_access_token()
    payload = json.dumps({
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }, ensure_ascii=False).encode("utf-8")
    encoded_id = urllib.parse.quote(str(message_id), safe="")
    req = urllib.request.Request(
        f"https://open.feishu.cn/open-apis/im/v1/messages/{encoded_id}/reply",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8") or "{}")
    if data.get("code") != 0:
        raise RuntimeError(f"reply rejected: {data.get('code')} {data.get('msg')}")


def _field(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def extract_command(message) -> str:
    try:
        content = json.loads(str(_field(message, "content", "") or "{}"))
    except json.JSONDecodeError:
        content = {}
    text = str(content.get("text") or "")
    for mention in (_field(message, "mentions", []) or []):
        key = str(_field(mention, "key", "") or "")
        if key:
            text = text.replace(key, " ")
    # Defensive fallback for SDK/model variants where mentions were omitted but
    # the placeholder remains in text.
    text = re.sub(r"@_user_\d+", " ", text)
    return " ".join(text.split())


def should_handle_message(message) -> bool:
    chat_type = str(_field(message, "chat_type", "") or "").lower()
    # The app may have the sensitive "all group messages" permission.  Ignore
    # ordinary group chatter and react only when the robot was explicitly @'d.
    if chat_type != "p2p" and not (_field(message, "mentions", []) or []):
        return False
    return str(_field(message, "message_type", "") or "").lower() == "text"


def _reserve_message(message_id: str) -> bool:
    now = time.time()
    with _SEEN_LOCK:
        for key, seen_at in list(_SEEN_MESSAGES.items()):
            if now - seen_at > 600:
                _SEEN_MESSAGES.pop(key, None)
        if message_id in _SEEN_MESSAGES:
            return False
        _SEEN_MESSAGES[message_id] = now
        return True


def _process_message(message_id: str, command: str) -> None:
    result = query_via_bridge(command)
    reply = str(result.get("text") or result.get("error") or "查询失败，请稍后再试。")
    try:
        reply_to_message(message_id, reply)
        log(f"replied to message {message_id}: {command[:80]}")
    except Exception as exc:  # noqa: BLE001 - event loop must stay alive
        log(f"reply to {message_id} failed: {exc}")


def on_message(data):
    message = _field(_field(data, "event"), "message")
    if not message or not should_handle_message(message):
        return None
    message_id = str(_field(message, "message_id", "") or "")
    if not message_id or not _reserve_message(message_id):
        return None
    command = extract_command(message) or "帮助"
    log(f"message command {command[:120]!r} id={message_id}")
    # A LibreNMS query may take several seconds. Acknowledge the event handler
    # immediately and send the reply asynchronously so Feishu does not retry it.
    threading.Thread(
        target=_process_message, args=(message_id, command), daemon=True,
        name=f"feishu-query-{message_id[-8:]}",
    ).start()
    return None


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
        .register_p2_im_message_receive_v1(on_message)
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
