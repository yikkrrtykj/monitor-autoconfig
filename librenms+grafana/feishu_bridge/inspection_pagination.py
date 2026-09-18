"""Bounded in-memory pagination for immutable Feishu inspection snapshots."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import copy
import json
import secrets
import threading
import time

from feishu_bridge.card_presentation import make_card


class InspectionPaginationError(Exception):
    """Base error carrying a stable result code and operator-safe message."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class InspectionCapacityError(InspectionPaginationError):
    pass


@dataclass(frozen=True)
class _Session:
    session_id: str
    created_at: float
    expires_at: float
    snapshot_json: bytes
    page_size: int
    page_count: int
    app_id: str
    chat_id: str
    source_message_id: str
    card_message_id: str = ""


def _strict_page(value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise InspectionPaginationError("invalid_page", "页码无效，请重新操作。")
    if value < 1:
        raise InspectionPaginationError("invalid_page", "页码无效，请重新操作。")
    return value


def _snapshot_bytes(snapshot):
    if not isinstance(snapshot, dict):
        raise ValueError("inspection snapshot must be an object")
    normalized = copy.deepcopy(snapshot)
    items = normalized.get("items")
    if not isinstance(items, list):
        raise ValueError("inspection snapshot items must be a list")
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("markdown"), str):
            raise ValueError("inspection snapshot item must contain markdown")
    return json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _button(label, session_id, page):
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": "default",
        "width": "default",
        "margin": "8px 8px 0px 0px",
        "behaviors": [{
            "type": "callback",
            "value": {
                "action": "inspection_page",
                "session_id": session_id,
                "page": page,
            },
        }],
    }


def _button_row(buttons):
    return {
        "tag": "column_set",
        "horizontal_spacing": "8px",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "elements": [button],
            }
            for button in buttons
        ],
    }


def render_page(snapshot, session_id, page, page_size=10):
    """Render one absolute page without mutating the stored snapshot."""
    page = _strict_page(page)
    items = snapshot.get("items") or []
    page_count = max(1, (len(items) + page_size - 1) // page_size)
    if page > page_count:
        raise InspectionPaginationError("invalid_page", "页码无效，请重新操作。")
    start = (page - 1) * page_size
    visible = items[start:start + page_size]
    lines = list(snapshot.get("summary_lines") or [])
    if lines and visible:
        lines.append("")
    lines.extend(str(item["markdown"]) for item in visible)
    if not visible:
        lines.append("暂无已启用监控的设备。")
    extra = []
    if page_count > 1:
        extra.append({
            "tag": "markdown",
            "content": f"第 **{page} / {page_count}** 页",
            "text_align": "center",
        })
        buttons = []
        if page > 1:
            buttons.append(_button("上一页", session_id, page - 1))
        if page < page_count:
            buttons.append(_button("下一页", session_id, page + 1))
        extra.append(_button_row(buttons))
    return make_card(
        str(snapshot.get("title") or "网络巡检"),
        str(snapshot.get("subtitle") or "Network Inspection"),
        str(snapshot.get("template") or "green"),
        "\n".join(lines),
        extra_elements=extra,
    )


class InspectionSessionStore:
    """Thread-safe, non-persistent store that never evicts live sessions."""

    def __init__(
        self, *, ttl_seconds=1200, max_sessions=128,
        max_snapshot_bytes=1024 * 1024, max_total_bytes=16 * 1024 * 1024,
        page_size=10, clock=time.monotonic,
    ):
        self.ttl_seconds = float(ttl_seconds)
        self.max_sessions = int(max_sessions)
        self.max_snapshot_bytes = int(max_snapshot_bytes)
        self.max_total_bytes = int(max_total_bytes)
        self.page_size = int(page_size)
        self._clock = clock
        self._lock = threading.RLock()
        self._sessions = OrderedDict()
        self._expired = OrderedDict()
        self._total_bytes = 0

    def _purge_expired_locked(self, now):
        for session_id, session in list(self._sessions.items()):
            if now < session.expires_at:
                continue
            self._sessions.pop(session_id)
            self._total_bytes -= len(session.snapshot_json)
            self._expired[session_id] = now + self.ttl_seconds
        for session_id, keep_until in list(self._expired.items()):
            if now >= keep_until:
                self._expired.pop(session_id)

    def create(self, snapshot, *, app_id, chat_id, source_message_id):
        app_id = str(app_id or "").strip()
        chat_id = str(chat_id or "").strip()
        source_message_id = str(source_message_id or "").strip()
        if not app_id or not chat_id or not source_message_id:
            raise InspectionPaginationError("missing_context", "巡检来源信息不完整，无法启用分页。")
        payload = _snapshot_bytes(snapshot)
        if len(payload) > self.max_snapshot_bytes:
            raise InspectionCapacityError("snapshot_too_large", "巡检结果过大，无法创建分页卡。")
        items = snapshot.get("items") or []
        page_count = max(1, (len(items) + self.page_size - 1) // self.page_size)
        now = self._clock()
        with self._lock:
            self._purge_expired_locked(now)
            if len(self._sessions) >= self.max_sessions:
                raise InspectionCapacityError("session_capacity", "当前分页巡检过多，请稍后重试。")
            if self._total_bytes + len(payload) > self.max_total_bytes:
                raise InspectionCapacityError("total_capacity", "巡检分页存储已满，请稍后重试。")
            session_id = secrets.token_urlsafe(24)
            while session_id in self._sessions or session_id in self._expired:
                session_id = secrets.token_urlsafe(24)
            session = _Session(
                session_id=session_id,
                created_at=now,
                expires_at=now + self.ttl_seconds,
                snapshot_json=payload,
                page_size=self.page_size,
                page_count=page_count,
                app_id=app_id,
                chat_id=chat_id,
                source_message_id=source_message_id,
            )
            self._sessions[session_id] = session
            self._total_bytes += len(payload)
        return session_id, render_page(json.loads(payload), session_id, 1, self.page_size)

    def bind_card(self, session_id, *, app_id, chat_id, source_message_id, card_message_id):
        values = [str(value or "").strip() for value in (
            session_id, app_id, chat_id, source_message_id, card_message_id,
        )]
        session_id, app_id, chat_id, source_message_id, card_message_id = values
        if not all(values):
            raise InspectionPaginationError("missing_context", "巡检卡片绑定信息不完整。")
        now = self._clock()
        with self._lock:
            self._purge_expired_locked(now)
            session = self._sessions.get(session_id)
            if session is None:
                code = "expired" if session_id in self._expired else "unknown_session"
                message = (
                    "本次巡检结果已过期，请重新执行巡检。"
                    if code == "expired" else "本次巡检结果不可用，请重新执行巡检。"
                )
                raise InspectionPaginationError(code, message)
            if (app_id, chat_id, source_message_id) != (
                session.app_id, session.chat_id, session.source_message_id,
            ):
                raise InspectionPaginationError("context_mismatch", "巡检卡片来源不匹配。")
            if session.card_message_id and session.card_message_id != card_message_id:
                raise InspectionPaginationError("card_mismatch", "巡检卡片不匹配。")
            if not session.card_message_id:
                self._sessions[session_id] = _Session(
                    **{**session.__dict__, "card_message_id": card_message_id}
                )
        return True

    def page(self, session_id, page, *, app_id, chat_id, card_message_id):
        page = _strict_page(page)
        session_id = str(session_id or "").strip()
        app_id = str(app_id or "").strip()
        chat_id = str(chat_id or "").strip()
        card_message_id = str(card_message_id or "").strip()
        if not session_id:
            raise InspectionPaginationError("unknown_session", "本次巡检结果不可用，请重新执行巡检。")
        if not app_id or not chat_id or not card_message_id:
            raise InspectionPaginationError("missing_context", "无法确认巡检卡片来源。")
        now = self._clock()
        with self._lock:
            self._purge_expired_locked(now)
            session = self._sessions.get(session_id)
            if session is None:
                code = "expired" if session_id in self._expired else "unknown_session"
                message = (
                    "本次巡检结果已过期，请重新执行巡检。"
                    if code == "expired" else "本次巡检结果不可用，请重新执行巡检。"
                )
                raise InspectionPaginationError(code, message)
            if not session.card_message_id:
                raise InspectionPaginationError("unbound", "本次巡检结果不可用，请重新执行巡检。")
            if (app_id, chat_id, card_message_id) != (
                session.app_id, session.chat_id, session.card_message_id,
            ):
                raise InspectionPaginationError("context_mismatch", "无法确认巡检卡片来源。")
            if page > session.page_count:
                raise InspectionPaginationError("invalid_page", "页码无效，请重新操作。")
            snapshot = json.loads(session.snapshot_json.decode("utf-8"))
            return render_page(snapshot, session.session_id, page, session.page_size)

    def stats(self):
        with self._lock:
            self._purge_expired_locked(self._clock())
            return {"sessions": len(self._sessions), "bytes": self._total_bytes}
