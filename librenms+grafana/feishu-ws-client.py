#!/usr/bin/env python3
"""Feishu client for card callbacks and per-site-group @ query commands.

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
  FEISHU_CHAT_ID this physical monitor's group name or oc_ chat id
  EVENT_NAME     local company/event name shown before every result
  BRIDGE_URL   bridge base URL (default http://alertmanager-feishu-bridge:5005)

Feishu console prerequisites (one-time, see .env.example):
  自建应用 -> 开启机器人能力 -> 事件与回调选择"使用长连接接收" ->
  订阅卡片回传交互(card.action.trigger) 和接收消息(im.message.receive_v1)
  -> 把应用机器人加进告警群。
"""
from __future__ import annotations

import copy
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
CHAT_TARGET = os.environ.get("FEISHU_CHAT_ID", "").strip()
EVENT_NAME = os.environ.get("EVENT_NAME", "").strip()
POLL_SECONDS = max(2.0, float(os.environ.get("FEISHU_COMMAND_POLL_SECONDS", "5") or 5))
BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://alertmanager-feishu-bridge:5005").rstrip("/")
_TENANT_TOKEN = {"value": "", "expires_at": 0.0}
_TOKEN_LOCK = threading.Lock()
_SEEN_MESSAGES: dict[str, float] = {}
_SEEN_LOCK = threading.Lock()
_POLL_READY = False
_POLL_STATE_LOCK = threading.Lock()


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
        # Full-network fiber audits may fan out across many LibreNMS devices.
        # Message handling is already asynchronous, so allow the internal
        # request to finish instead of returning a false timeout at 30 seconds.
        timeout = 90 if re.search(r"巡检|check[_ ]?(?:fiber|uplinks)", text, re.I) else 30
        with urllib.request.urlopen(req, timeout=timeout) as resp:
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


def reply_to_message(message_id: str, text: str = "", card: dict | None = None) -> None:
    token = tenant_access_token()
    if card:
        card_content = card.get("card") if card.get("msg_type") == "interactive" else card
        message = {
            "msg_type": "interactive",
            "content": json.dumps(card_content, ensure_ascii=False),
        }
    else:
        message = {
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
    payload = json.dumps(message, ensure_ascii=False).encode("utf-8")
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


def _api_get(path: str, token: str) -> dict:
    req = urllib.request.Request(
        f"https://open.feishu.cn{path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw or "{}")
            detail = f"code={payload.get('code')} msg={payload.get('msg')}"
        except json.JSONDecodeError:
            detail = raw[:500] or str(exc)
        if path.startswith("/open-apis/im/v1/chats"):
            hint = "需要应用身份权限 im:chat（获取与更新群组信息）"
        elif path.startswith("/open-apis/im/v1/messages"):
            hint = "需要应用身份权限 im:message:readonly，并保留 im:message.group_msg"
        else:
            hint = "请检查飞书应用权限"
        raise RuntimeError(f"Feishu HTTP {exc.code}: {detail}; {hint}") from exc
    if data.get("code") != 0:
        raise RuntimeError(f"Feishu API rejected: {data.get('code')} {data.get('msg')}")
    return data


def resolve_command_chat(token: str) -> str:
    """Resolve this physical monitor's command/alert group without guessing."""
    if CHAT_TARGET.startswith("oc_"):
        return CHAT_TARGET
    wanted = CHAT_TARGET.casefold()
    items = []
    page_token = ""
    while True:
        query = {"page_size": "100"}
        if page_token:
            query["page_token"] = page_token
        data = _api_get(f"/open-apis/im/v1/chats?{urllib.parse.urlencode(query)}", token)
        page = data.get("data") or {}
        items.extend(item for item in (page.get("items") or []) if item.get("chat_id"))
        if not page.get("has_more") or not page.get("page_token"):
            break
        page_token = str(page["page_token"])
    if wanted:
        matches = [
            item for item in items
            if str(item.get("name") or "").strip().casefold() == wanted
        ]
    else:
        matches = items if len(items) == 1 else []
    if len(matches) != 1:
        names = "、".join(str(item.get("name") or item.get("chat_id")) for item in items[:10])
        if wanted:
            raise RuntimeError(f"群名称不存在或不唯一：{CHAT_TARGET}；机器人当前群：{names or '无'}")
        raise RuntimeError(f"机器人加入了多个群，请填写告警及巡检群名称：{names or '无'}")
    return str(matches[0]["chat_id"])


def fetch_chat_messages(token: str, chat_id: str) -> list[dict]:
    query = urllib.parse.urlencode({
        "container_id_type": "chat",
        "container_id": chat_id,
        "sort_type": "ByCreateTimeDesc",
        "page_size": "50",
    })
    data = _api_get(f"/open-apis/im/v1/messages?{query}", token)
    return [item for item in ((data.get("data") or {}).get("items") or []) if isinstance(item, dict)]


def _field(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _message_content(message) -> str:
    body = _field(message, "body")
    return str(_field(body, "content", "") or _field(message, "content", "") or "")


def extract_command(message) -> str:
    try:
        content = json.loads(_message_content(message) or "{}")
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
    message_type = _field(message, "message_type", "") or _field(message, "msg_type", "")
    return str(message_type or "").lower() == "text"


def _is_app_message(message) -> bool:
    sender = _field(message, "sender") or {}
    return str(_field(sender, "sender_type", "") or "").lower() in {"app", "bot"}


def _reserve_message(message_id: str) -> bool:
    now = time.time()
    with _SEEN_LOCK:
        if message_id in _SEEN_MESSAGES:
            return False
        _SEEN_MESSAGES[message_id] = now
        # Polling repeatedly returns the latest history page. Keep command IDs
        # for the process lifetime so an old command is never replayed after a
        # time-based cache expiry; cap only as a long-term memory guard.
        while len(_SEEN_MESSAGES) > 10000:
            _SEEN_MESSAGES.pop(next(iter(_SEEN_MESSAGES)))
        return True


def _process_message(message_id: str, command: str) -> None:
    result = query_via_bridge(command)
    reply = str(result.get("text") or result.get("error") or "查询失败，请稍后再试。")
    try:
        cards = [_decorate_card(item) for item in (result.get("cards") or []) if isinstance(item, dict)]
        if cards:
            for position, card in enumerate(cards):
                reply_to_message(message_id, card=card)
                if position + 1 < len(cards):
                    time.sleep(0.15)
        else:
            reply_to_message(message_id, _decorate_text(reply))
        log(f"replied to message {message_id}: {command[:80]}")
    except Exception as exc:  # noqa: BLE001 - event loop must stay alive
        log(f"reply to {message_id} failed: {exc}")


def _event_prefix() -> str:
    return f"【{EVENT_NAME}】" if EVENT_NAME else ""


def _decorate_text(text: str) -> str:
    prefix = _event_prefix()
    return f"{prefix}\n{text}" if prefix and not str(text).startswith(prefix) else str(text)


def _decorate_card(card: dict) -> dict:
    prefix = _event_prefix()
    if not prefix:
        return card
    decorated = copy.deepcopy(card)
    payload = decorated.get("card") if decorated.get("msg_type") == "interactive" else decorated
    header = payload.get("header") if isinstance(payload, dict) else None
    title = header.get("title") if isinstance(header, dict) else None
    if isinstance(title, dict):
        content = str(title.get("content") or "")
        if not content.startswith(prefix):
            title["content"] = f"{prefix} {content}".strip()
    return decorated


def process_polled_messages(items: list[dict], *, baseline: bool = False) -> int:
    """Reserve the initial history, then execute every newly observed @ command."""
    handled = 0

    def created_at(item):
        try:
            return int(str(item.get("create_time") or "0") or 0)
        except (TypeError, ValueError):
            return 0

    ordered = sorted(items, key=created_at)
    for message in ordered:
        message_id = str(_field(message, "message_id", "") or "")
        if not message_id:
            continue
        if baseline:
            _reserve_message(message_id)
            continue
        if _is_app_message(message) or not should_handle_message(message):
            continue
        if not _reserve_message(message_id):
            continue
        command = extract_command(message) or "帮助"
        log(f"site-group command {command[:120]!r} id={message_id}")
        threading.Thread(
            target=_process_message, args=(message_id, command), daemon=True,
            name=f"feishu-query-{message_id[-8:]}",
        ).start()
        handled += 1
    return handled


def poll_site_group_commands() -> None:
    """Consume only the group configured for this physical monitor/site."""
    global _POLL_READY
    chat_id = ""
    initialized = False
    while True:
        try:
            token = tenant_access_token()
            if not chat_id:
                chat_id = resolve_command_chat(token)
                log(f"site command group resolved: {chat_id}; event={EVENT_NAME or '未命名'}")
            items = fetch_chat_messages(token, chat_id)
            process_polled_messages(items, baseline=not initialized)
            if not initialized:
                initialized = True
                log(f"site command polling ready; baseline={len(items)} messages")
            with _POLL_STATE_LOCK:
                _POLL_READY = True
        except Exception as exc:  # noqa: BLE001 - keep polling after cloud/network failures
            log(f"site command polling failed: {exc}")
            with _POLL_STATE_LOCK:
                _POLL_READY = False
            chat_id = ""
        time.sleep(POLL_SECONDS)


def on_message(data):
    # Prefer per-site history polling when available. If the newly required
    # read permissions have not been granted yet, retain the original
    # long-connection @ event path so existing single-site installations keep
    # working exactly as before.
    with _POLL_STATE_LOCK:
        if _POLL_READY:
            return None
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
                "title": {"tag": "plain_text", "content": f"{_event_prefix()} 设备待删除确认".strip()},
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

    if CHAT_TARGET:
        threading.Thread(
            target=poll_site_group_commands,
            daemon=True,
            name="feishu-site-command-poller",
        ).start()

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
