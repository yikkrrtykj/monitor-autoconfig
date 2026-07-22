import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace


_spec = importlib.util.spec_from_file_location(
    "feishu_ws_client",
    Path(__file__).resolve().parent.parent / "feishu-ws-client.py",
)
client = importlib.util.module_from_spec(_spec)
assert _spec.loader
_spec.loader.exec_module(client)


def _message(text, *, mentions=True, chat_type="group"):
    return SimpleNamespace(
        message_id="om_123",
        message_type="text",
        chat_type=chat_type,
        content=json.dumps({"text": text}, ensure_ascii=False),
        mentions=[SimpleNamespace(key="@_user_1", name="LibreBOT")] if mentions else [],
    )


def test_extracts_command_after_robot_mention():
    message = _message("@_user_1  查光功率 192.168.10.31 Gi1/0/1")
    assert client.should_handle_message(message) is True
    assert client.extract_command(message) == "查光功率 192.168.10.31 Gi1/0/1"


def test_ignores_ordinary_group_chatter_even_with_sensitive_permission():
    assert client.should_handle_message(_message("查设备 RTS1", mentions=False)) is False
    assert client.should_handle_message(_message("查设备 RTS1", mentions=False, chat_type="p2p")) is True


def test_duplicate_message_is_reserved_once(monkeypatch):
    client._SEEN_MESSAGES.clear()
    monkeypatch.setattr(client.time, "time", lambda: 1000)
    assert client._reserve_message("om_same") is True
    assert client._reserve_message("om_same") is False


def test_process_message_replies_with_each_interactive_card(monkeypatch):
    cards = [
        {"msg_type": "interactive", "card": {"schema": "2.0", "header": {"title": {"content": "a"}}}},
        {"msg_type": "interactive", "card": {"schema": "2.0", "header": {"title": {"content": "b"}}}},
    ]
    calls = []
    monkeypatch.setattr(client, "query_via_bridge", lambda _command: {"ok": True, "cards": cards})
    monkeypatch.setattr(client, "reply_to_message", lambda message_id, text="", card=None: calls.append((message_id, text, card)))
    monkeypatch.setattr(client.time, "sleep", lambda _seconds: None)
    client._process_message("om_cards", "待删除设备")
    assert [item[2] for item in calls] == cards


def test_history_message_uses_body_content_and_event_prefix(monkeypatch):
    message = {
        "message_id": "om_history",
        "message_type": "text",
        "chat_type": "group",
        "body": {"content": json.dumps({"text": "@_user_1 光功率巡检"}, ensure_ascii=False)},
        "mentions": [{"key": "@_user_1", "name": "LibreBOT"}],
        "sender": {"sender_type": "user"},
    }
    assert client.extract_command(message) == "光功率巡检"
    monkeypatch.setattr(client, "EVENT_NAME", "EWC 上海站")
    assert client._decorate_text("检查完成") == "【EWC 上海站】\n检查完成"
    card = {"msg_type": "interactive", "card": {"header": {"title": {"content": "光功率巡检"}}}}
    decorated = client._decorate_card(card)
    assert decorated["card"]["header"]["title"]["content"] == "【EWC 上海站】 光功率巡检"
    assert card["card"]["header"]["title"]["content"] == "光功率巡检"


def test_resolve_site_group_by_exact_name(monkeypatch):
    monkeypatch.setattr(client, "CHAT_TARGET", "统一监控群")
    monkeypatch.setattr(client, "_api_get", lambda _path, _token: {
        "code": 0,
        "data": {"items": [
            {"chat_id": "oc_wrong", "name": "统一监控群-旧"},
            {"chat_id": "oc_right", "name": "统一监控群"},
        ]},
    })
    assert client.resolve_command_chat("token") == "oc_right"


def test_api_http_error_reports_required_permission(monkeypatch):
    http_error = client.urllib.error.HTTPError(
        "https://open.feishu.cn/open-apis/im/v1/chats", 400, "Bad Request", {},
        io.BytesIO(b'{"code":99991672,"msg":"Access denied"}'),
    )
    monkeypatch.setattr(
        client.urllib.request,
        "urlopen",
        lambda _req, timeout: (_ for _ in ()).throw(http_error),
    )
    try:
        client._api_get("/open-apis/im/v1/chats?page_size=100", "token")
    except RuntimeError as exc:
        assert "99991672" in str(exc)
        assert "im:chat" in str(exc)
    else:
        raise AssertionError("missing permission error was not surfaced")


def test_long_connection_message_remains_fallback_until_polling_is_ready(monkeypatch):
    calls = []

    class ImmediateThread:
        def __init__(self, target, args, **_kwargs):
            self.target, self.args = target, args

        def start(self):
            calls.append(self.args)

    client._SEEN_MESSAGES.clear()
    monkeypatch.setattr(client, "_POLL_READY", False)
    monkeypatch.setattr(client.threading, "Thread", ImmediateThread)
    client.on_message(SimpleNamespace(event=SimpleNamespace(message=_message("@_user_1 帮助"))))
    assert calls == [("om_123", "帮助")]

    calls.clear()
    client._SEEN_MESSAGES.clear()
    monkeypatch.setattr(client, "_POLL_READY", True)
    client.on_message(SimpleNamespace(event=SimpleNamespace(message=_message("@_user_1 帮助"))))
    assert calls == []


def test_site_polling_baselines_old_messages_then_handles_new_once(monkeypatch):
    old = {
        "message_id": "om_old", "message_type": "text", "chat_type": "group",
        "create_time": "100", "body": {"content": '{"text":"@_user_1 帮助"}'},
        "mentions": [{"key": "@_user_1"}], "sender": {"sender_type": "user"},
    }
    new = {
        "message_id": "om_new", "message_type": "text", "chat_type": "group",
        "create_time": "200", "body": {"content": '{"text":"@_user_1 光功率巡检"}'},
        "mentions": [{"key": "@_user_1"}], "sender": {"sender_type": "user"},
    }
    calls = []

    class ImmediateThread:
        def __init__(self, target, args, **_kwargs):
            self.target, self.args = target, args

        def start(self):
            calls.append(self.args)

    client._SEEN_MESSAGES.clear()
    monkeypatch.setattr(client.threading, "Thread", ImmediateThread)
    assert client.process_polled_messages([old], baseline=True) == 0
    assert client.process_polled_messages([old, new]) == 1
    assert client.process_polled_messages([new]) == 0
    assert calls == [("om_new", "光功率巡检")]
