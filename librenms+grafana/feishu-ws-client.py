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
  FEISHU_GATEWAY_MODE local (default) or hub.  Site-only deployments must not
                      start this client.
  FEISHU_SITE_ID      stable id of the local monitoring site
  FEISHU_SITE_ROUTES  JSON list used by hub mode; each item contains project
                      name, group name (or chat id), and bridge URL. Tokens are
                      derived automatically from the shared app secret.
  FEISHU_DEFAULT_SITE_ID optional route for direct (p2p) bot messages
  FEISHU_BRIDGE_API_TOKEN token used for the local bridge in local mode

Feishu console prerequisites (one-time, see .env.example):
  自建应用 -> 开启机器人能力 -> 事件与回调选择"使用长连接接收" ->
  订阅卡片回传交互(card.action.trigger) 和接收消息(im.message.receive_v1)
  -> 把应用机器人加进告警群。
"""
from __future__ import annotations

import hashlib
import hmac
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
GATEWAY_MODE = os.environ.get("FEISHU_GATEWAY_MODE", "local").strip().lower() or "local"
SITE_ID = os.environ.get("FEISHU_SITE_ID", "").strip() or "local"
DEFAULT_SITE_ID = os.environ.get("FEISHU_DEFAULT_SITE_ID", "").strip()
BRIDGE_API_TOKEN = os.environ.get("FEISHU_BRIDGE_API_TOKEN", "").strip()
SITE_ROUTES_RAW = os.environ.get("FEISHU_SITE_ROUTES", "").strip()
_TENANT_TOKEN = {"value": "", "expires_at": 0.0}
_TOKEN_LOCK = threading.Lock()
_SEEN_MESSAGES: dict[str, float] = {}
_SEEN_LOCK = threading.Lock()
_ROUTE_LOCK = threading.Lock()


def log(message: str) -> None:
    print(f"[feishu-ws] {message}", file=sys.stderr, flush=True)


def derived_bridge_token(site_id: str) -> str:
    site = str(site_id or "").strip()
    if not APP_SECRET or not site:
        return ""
    message = f"monitor-autoconfig/feishu-bridge/{site}".encode("utf-8")
    return hmac.new(APP_SECRET.encode("utf-8"), message, hashlib.sha256).hexdigest()


def parse_site_routes(raw: str) -> list[dict]:
    """Parse the hub's explicit chat/site routing table.

    Never infer a route by list position: sending a Shanghai command to an
    overseas bridge is worse than returning a visible configuration error.
    """
    if not str(raw or "").strip():
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        log(f"invalid FEISHU_SITE_ROUTES JSON: {exc}")
        return []
    if isinstance(payload, dict):
        payload = payload.get("sites", [])
    if not isinstance(payload, list):
        log("FEISHU_SITE_ROUTES must be a JSON list")
        return []
    routes = []
    seen_sites = set()
    seen_chats = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        site_id = str(item.get("site_id") or item.get("site") or "").strip()
        chat_id = str(item.get("chat_id") or "").strip()
        bridge_url = str(item.get("bridge_url") or "").strip().rstrip("/")
        bridge_token = str(item.get("bridge_token") or item.get("token") or "").strip() or derived_bridge_token(site_id)
        if not site_id or not chat_id or not bridge_url:
            log(f"ignored incomplete Feishu site route: site={site_id or '-'} chat={chat_id or '-'}")
            continue
        site_key = site_id.casefold()
        chat_key = chat_id.casefold()
        if site_key in seen_sites or chat_key in seen_chats:
            log(f"ignored duplicate Feishu site route: site={site_id} chat={chat_id}")
            continue
        if not bridge_url.startswith(("http://", "https://")):
            log(f"ignored invalid bridge URL for {site_id}: {bridge_url}")
            continue
        routes.append({
            "site_id": site_id,
            "chat_id": chat_id,
            "bridge_url": bridge_url,
            "bridge_token": bridge_token,
        })
        seen_sites.add(site_key)
        seen_chats.add(chat_key)
    return routes


SITE_ROUTES = parse_site_routes(SITE_ROUTES_RAW)
LOCAL_ROUTE = {
    "site_id": SITE_ID,
    "chat_id": "",
    "bridge_url": BRIDGE_URL,
    "bridge_token": BRIDGE_API_TOKEN,
}


def _route_by_site(site_id: str) -> dict | None:
    if GATEWAY_MODE != "hub":
        return LOCAL_ROUTE
    wanted = str(site_id or "").strip()
    if not wanted:
        wanted = DEFAULT_SITE_ID
    matches = [route for route in SITE_ROUTES if route["site_id"] == wanted]
    return matches[0] if len(matches) == 1 else None


def _route_for_message(message) -> dict | None:
    if GATEWAY_MODE != "hub":
        return LOCAL_ROUTE
    chat_type = str(_field(message, "chat_type", "") or "").lower()
    if chat_type == "p2p":
        return _route_by_site(DEFAULT_SITE_ID)
    chat_id = str(_field(message, "chat_id", "") or "").strip()
    with _ROUTE_LOCK:
        matches = [
            route for route in SITE_ROUTES
            if str(route.get("resolved_chat_id") or route["chat_id"]) == chat_id
        ]
    if len(matches) == 1:
        return matches[0]
    try:
        chat_name = _chat_name_for_id(chat_id)
    except Exception as exc:  # noqa: BLE001 - leave the message visibly unrouted
        log(f"cannot resolve Feishu group name for {chat_id}: {exc}")
        return None
    wanted = chat_name.casefold()
    with _ROUTE_LOCK:
        matches = [
            route for route in SITE_ROUTES
            if not route["chat_id"].startswith("oc_") and route["chat_id"].casefold() == wanted
        ]
        if len(matches) == 1:
            matches[0]["resolved_chat_id"] = chat_id
            log(f"resolved group '{chat_name}' to {chat_id} for project {matches[0]['site_id']}")
    return matches[0] if len(matches) == 1 else None


def _bridge_headers(route: dict) -> dict:
    headers = {"Content-Type": "application/json"}
    token = str(route.get("bridge_token") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def resolve_via_bridge(value: dict, route: dict | None = None) -> dict:
    """Forward the button's value to the bridge; it validates token + state."""
    route = route or LOCAL_ROUTE
    payload = json.dumps({
        "key": str(value.get("key") or ""),
        "action": "delete" if value.get("action") == "retire_delete" else "keep",
        "token": str(value.get("token") or ""),
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{route['bridge_url']}/retire/resolve", data=payload,
        headers=_bridge_headers(route), method="POST",
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


def query_via_bridge(text: str, route: dict | None = None) -> dict:
    route = route or LOCAL_ROUTE
    payload = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{route['bridge_url']}/bot/query", data=payload,
        headers=_bridge_headers(route), method="POST",
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


def _chat_name_for_id(chat_id: str) -> str:
    token = tenant_access_token()
    encoded = urllib.parse.quote(str(chat_id or ""), safe="")
    req = urllib.request.Request(
        f"https://open.feishu.cn/open-apis/im/v1/chats/{encoded}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8") or "{}")
    name = str((data.get("data") or {}).get("name") or "").strip()
    if data.get("code") != 0 or not name:
        raise RuntimeError(f"chat lookup rejected: {data.get('code')} {data.get('msg')}")
    return name


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


def _process_message(message_id: str, command: str, route: dict | None = None) -> None:
    result = query_via_bridge(command, route)
    reply = str(result.get("text") or result.get("error") or "查询失败，请稍后再试。")
    try:
        cards = [item for item in (result.get("cards") or []) if isinstance(item, dict)]
        if cards:
            for position, card in enumerate(cards):
                reply_to_message(message_id, card=card)
                if position + 1 < len(cards):
                    time.sleep(0.15)
        else:
            reply_to_message(message_id, reply)
        log(f"replied to message {message_id} site={(route or LOCAL_ROUTE).get('site_id')}: {command[:80]}")
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
    route = _route_for_message(message)
    if route is None:
        chat_id = str(_field(message, "chat_id", "") or "")
        log(f"no site route for chat={chat_id or '-'} command={command[:120]!r}")
        threading.Thread(
            target=reply_to_message,
            args=(message_id, "该群尚未绑定监控项目，请在中心填写比赛名称、群名称和监控地址。"),
            daemon=True,
            name=f"feishu-unrouted-{message_id[-8:]}",
        ).start()
        return None
    log(f"message command {command[:120]!r} id={message_id} site={route['site_id']}")
    # A LibreNMS query may take several seconds. Acknowledge the event handler
    # immediately and send the reply asynchronously so Feishu does not retry it.
    threading.Thread(
        target=_process_message, args=(message_id, command, route), daemon=True,
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
    route = _route_by_site(str(value.get("site_id") or ""))
    log(f"card action {value.get('action')} key={value.get('key')} site={value.get('site_id') or '-'} by={who}")
    if route is None:
        result = {"ok": False, "error": "卡片所属监控站点未配置或已移除，请联系管理员。"}
    else:
        result = resolve_via_bridge(value, route)
    log(f"bridge result: {json.dumps(result, ensure_ascii=False)[:200]}")
    return build_response(value, result)


def main() -> None:
    if not APP_ID or not APP_SECRET:
        log("FEISHU_APP_ID/FEISHU_APP_SECRET not configured; sleeping. "
            "Configure the self-built app in .env to enable in-card confirmation.")
        while True:
            time.sleep(3600)

    if GATEWAY_MODE == "site":
        log("FEISHU_GATEWAY_MODE=site: this deployment must not own a Feishu long connection; sleeping")
        while True:
            time.sleep(3600)
    if GATEWAY_MODE == "hub":
        if not SITE_ROUTES:
            log("FEISHU_GATEWAY_MODE=hub but no valid FEISHU_SITE_ROUTES were configured")
        else:
            log(f"hub routes loaded: {', '.join(route['site_id'] for route in SITE_ROUTES)}")

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
