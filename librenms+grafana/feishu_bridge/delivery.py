"""Feishu HTTP delivery transport for the alert bridge.

Business cards, event names, event ids, and watcher retry policies deliberately
stay in ``alertmanager-feishu-bridge.py``.  This module only owns Feishu HTTP
transport, app token/chat caches, fallback, and the shared delivery-health
mutations.
"""

import json
import threading
import time
from urllib import error, parse, request


def _feishu_response_result(response_text):
    """Return ``(ok, detail)`` for a Feishu webhook JSON response."""
    try:
        payload = json.loads(str(response_text or "").strip())
    except (TypeError, ValueError, json.JSONDecodeError):
        return False, "response is not valid JSON"
    if not isinstance(payload, dict):
        return False, "response JSON is not an object"
    code = payload.get("code")
    if code is None:
        code = payload.get("StatusCode", payload.get("status_code"))
    try:
        ok = int(code) == 0
    except (TypeError, ValueError):
        return False, "response has no recognizable business code"
    detail = str(payload.get("msg") or payload.get("StatusMessage") or payload.get("message") or "")
    return ok, detail or f"code={code}"


def _feishu_http_error_detail(exc):
    """Return Feishu's business code/message instead of only ``HTTP 400``."""
    try:
        raw = exc.read().decode("utf-8", errors="replace")
    except Exception:
        raw = ""
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    code = payload.get("code") if isinstance(payload, dict) else None
    message = payload.get("msg") if isinstance(payload, dict) else None
    detail = f"HTTP {getattr(exc, 'code', '?')}"
    if code is not None:
        detail += f" code={code}"
    if message:
        detail += f" msg={str(message)[:240]}"
    elif raw:
        detail += f" body={raw[:240]}"
    if code in (99991672, 99991679):
        detail += "; 请给应用开通“查看群信息 (im:chat:read)”权限并发布新版本"
    elif code in (230006, 232025):
        detail += "; 请开启机器人能力并发布应用"
    elif code == 232034:
        detail += "; 请在当前租户安装并启用应用"
    elif code in (230002, 232011):
        detail += "; 请把应用机器人加入目标群并确认机器人仍在群内"
    elif code in (230034, 232006):
        detail += "; 请检查 oc_ 开头的 Chat ID 是否有效且与接收类型一致"
    elif code == 230035:
        detail += "; 请检查群禁言、机器人发言权限和租户沟通权限"
    elif code in (230001, 232001):
        detail += "; 请求参数无效，请结合 msg 检查 Chat ID、接收类型和消息内容"
    return detail


class FeishuDelivery:
    """Stateful Feishu app/webhook delivery transport."""

    def __init__(
        self,
        *,
        webhook_token,
        dry_run,
        send_max_attempts,
        retry_base_seconds,
        app_id,
        app_secret,
        chat_id,
        event_name,
        log,
        health_state,
        health_lock,
        urlopen=None,
        sleep=None,
        time_fn=None,
    ):
        self.webhook_token = webhook_token
        self.dry_run = dry_run
        self.send_max_attempts = send_max_attempts
        self.retry_base_seconds = retry_base_seconds
        self.app_id = app_id
        self.app_secret = app_secret
        self.chat_id = chat_id
        self.event_name = event_name
        self.log = log
        self.health_state = health_state
        self.health_lock = health_lock
        self._urlopen = urlopen or request.urlopen
        self._sleep = sleep or time.sleep
        self._time = time_fn or time.time
        self._app_token = {"token": "", "expires_at": 0.0}
        self._app_token_lock = threading.Lock()
        self._app_chat = {"chat_id": ""}

    def app_configured(self):
        return bool(self.app_id and self.app_secret)

    def health_snapshot(self):
        with self.health_lock:
            return dict(self.health_state)

    def _mark_delivery_health(self, ok, error_message="", channel=""):
        with self.health_lock:
            if channel:
                self.health_state["lastChannel"] = channel
            if ok:
                self.health_state["lastSuccessAt"] = int(self._time())
                self.health_state["lastError"] = ""
            else:
                self.health_state["lastFailureAt"] = int(self._time())
                self.health_state["lastError"] = str(error_message or "delivery failed")[:300]

    def _mark_app_resolution(self, ok, error_message=""):
        with self.health_lock:
            self.health_state["appChatResolved"] = bool(ok)
            self.health_state["lastAppError"] = "" if ok else str(error_message or "群解析失败")[:500]

    def _mark_app_error(self, error_message):
        """Record an application delivery error without rewriting chat resolution."""
        with self.health_lock:
            self.health_state["lastAppError"] = str(error_message or "应用投递失败")[:500]

    def send_webhook(self, card):
        if self.dry_run:
            self.log(f"[DRY] would POST card: {card['card']['header']['title']['content']}")
            self._mark_delivery_health(True, channel="dry-run")
            return True
        if not self.webhook_token:
            self.log("[WARN] FEISHU_ROBOT_TOKEN empty, dropping alert (set token or enable DRY_RUN)")
            self._mark_delivery_health(False, "FEISHU_ROBOT_TOKEN is empty", "webhook")
            return False
        url = f"https://open.feishu.cn/open-apis/bot/v2/hook/{self.webhook_token}"
        data = json.dumps(card).encode("utf-8")
        req = request.Request(url, data=data, headers={"Content-Type": "application/json"})
        last_error = "delivery failed"
        for attempt in range(1, self.send_max_attempts + 1):
            try:
                with self._urlopen(req, timeout=5) as resp:
                    response_text = resp.read().decode("utf-8", errors="replace")
                ok, detail = _feishu_response_result(response_text)
                if ok:
                    self.log(f"feishu response: {response_text[:200]}")
                    self._mark_delivery_health(True, channel="webhook")
                    return True
                last_error = detail
                self.log(
                    f"[ERR] feishu rejected alert attempt "
                    f"{attempt}/{self.send_max_attempts}: {detail}; response={response_text[:200]}"
                )
            except error.URLError as exc:
                last_error = str(exc)
                self.log(f"[ERR] feishu request attempt {attempt}/{self.send_max_attempts} failed: {exc}")
            except Exception as exc:
                last_error = str(exc)
                self.log(f"[ERR] unexpected Feishu error attempt {attempt}/{self.send_max_attempts}: {exc}")
            if attempt < self.send_max_attempts:
                delay = self.retry_base_seconds * (2 ** (attempt - 1))
                if delay > 0:
                    self._sleep(delay)
        self._mark_delivery_health(False, last_error, "webhook")
        return False

    def _api_post(self, path, payload, token=""):
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = request.Request(
            f"https://open.feishu.cn{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
        )
        with self._urlopen(req, timeout=8) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace") or "{}")

    def _tenant_token(self):
        """Cached tenant_access_token；过期前 120 秒刷新。失败返回空串。"""
        now = self._time()
        with self._app_token_lock:
            if self._app_token["token"] and now < self._app_token["expires_at"] - 120:
                return self._app_token["token"]
        try:
            data = self._api_post(
                "/open-apis/auth/v3/tenant_access_token/internal",
                {"app_id": self.app_id, "app_secret": self.app_secret},
            )
        except error.HTTPError as exc:
            detail = _feishu_http_error_detail(exc)
            self._mark_app_error(detail)
            self.log(f"[APP] tenant token request failed: {detail}")
            return ""
        except Exception as exc:
            self._mark_app_error(exc)
            self.log(f"[APP] tenant token request failed: {exc}")
            return ""
        if data.get("code") != 0 or not data.get("tenant_access_token"):
            detail = f"code={data.get('code')} msg={str(data.get('msg'))[:160]}"
            self._mark_app_error(detail)
            self.log(f"[APP] tenant token rejected: {detail}")
            return ""
        with self._app_token_lock:
            self._app_token["token"] = data["tenant_access_token"]
            self._app_token["expires_at"] = now + float(data.get("expire") or 3600)
            return self._app_token["token"]

    def _app_chat_id(self, token):
        """Resolve the proactive alert group without guessing between groups."""
        if self.chat_id.startswith("oc_"):
            self._mark_app_resolution(True)
            return self.chat_id
        if self._app_chat["chat_id"]:
            return self._app_chat["chat_id"]
        items = []
        page_token = ""
        try:
            while True:
                query = {"page_size": "100"}
                if page_token:
                    query["page_token"] = page_token
                req = request.Request(
                    "https://open.feishu.cn/open-apis/im/v1/chats?" + parse.urlencode(query),
                    headers={"Authorization": f"Bearer {token}"},
                )
                with self._urlopen(req, timeout=8) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
                if data.get("code") not in (None, 0):
                    raise RuntimeError(f"code={data.get('code')} msg={data.get('msg')}")
                page = data.get("data") or {}
                items.extend(
                    item for item in (page.get("items") or [])
                    if isinstance(item, dict) and item.get("chat_id")
                )
                if not page.get("has_more") or not page.get("page_token"):
                    break
                page_token = str(page["page_token"])
        except error.HTTPError as exc:
            detail = _feishu_http_error_detail(exc)
            self._mark_app_resolution(False, detail)
            self.log(f"[APP] chat list failed: {detail}")
            return ""
        except Exception as exc:
            detail = str(exc)
            self._mark_app_resolution(False, detail)
            self.log(f"[APP] chat list failed: {detail}")
            return ""

        def match_name(value):
            wanted = str(value or "").strip().casefold()
            if not wanted:
                return []
            return [item for item in items if str(item.get("name") or "").strip().casefold() == wanted]

        candidates = match_name(self.chat_id) if self.chat_id else match_name(self.event_name)
        reason = "configured group name" if self.chat_id else "event name"
        if len(candidates) == 1:
            item = candidates[0]
        elif not self.chat_id and not candidates and len(items) == 1:
            item = items[0]
            reason = "only bot group"
        else:
            if not items:
                detail = "机器人不在任何群；请把自建应用机器人加入告警群"
                self._mark_app_resolution(False, detail)
                self.log(f"[APP] {detail}")
            elif self.chat_id:
                detail = f"告警群名称 '{self.chat_id}' 不存在或不唯一；建议直接填写 oc_ 开头的 chat_id"
                self._mark_app_resolution(False, detail)
                self.log(f"[APP] {detail}")
            else:
                names = ", ".join(str(entry.get("name") or entry.get("chat_id")) for entry in items[:10])
                detail = f"机器人属于多个群 ({names})；请把 FEISHU_CHAT_ID 设置为群名或 oc_ 开头的 chat_id"
                self._mark_app_resolution(False, detail)
                self.log(f"[APP] {detail}")
            return ""
        chat_id = str(item.get("chat_id") or "")
        if chat_id:
            self._app_chat["chat_id"] = chat_id
            self._mark_app_resolution(True)
            self.log(f"[APP] selected chat {chat_id} ({str(item.get('name'))[:40]}) by {reason}")
            return chat_id
        return ""

    def send_app(self, card):
        """Send one interactive card through the app bot. Return False on failure."""
        if self.dry_run:
            self.log(f"[DRY][APP] would send interactive card: {card['card']['header']['title']['content']}")
            return True
        if not self.app_configured():
            return False
        token = self._tenant_token()
        if not token:
            return False
        chat_id = self._app_chat_id(token)
        if not chat_id:
            return False
        try:
            data = self._api_post(
                "/open-apis/im/v1/messages?receive_id_type=chat_id",
                {
                    "receive_id": chat_id,
                    "msg_type": "interactive",
                    "content": json.dumps(card["card"], ensure_ascii=False),
                },
                token=token,
            )
        except error.HTTPError as exc:
            detail = _feishu_http_error_detail(exc)
            self._mark_app_error(detail)
            self.log(f"[APP] interactive card send failed: {detail}")
            return False
        except Exception as exc:
            self._mark_app_error(exc)
            self.log(f"[APP] interactive card send failed: {exc}")
            return False
        if data.get("code") != 0:
            detail = f"code={data.get('code')} msg={str(data.get('msg'))[:160]}"
            self._mark_app_error(detail)
            self.log(f"[APP] interactive card rejected: {detail}")
            return False
        self._mark_app_resolution(True)
        return True

    def send(self, card):
        """Prefer the approved app bot; retain the webhook as a safe fallback."""
        if self.app_configured():
            if self.send_app(card):
                self._mark_delivery_health(True, channel="app")
                return True
            with self.health_lock:
                app_error = self.health_state.get("lastAppError") or "应用卡片发送失败"
                self.health_state["lastAppError"] = app_error
            self.log("[APP] app delivery failed; falling back to FEISHU_ROBOT_TOKEN")
        return self.send_webhook(card)
