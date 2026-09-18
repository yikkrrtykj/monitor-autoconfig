import importlib.util
import io
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace


_spec = importlib.util.spec_from_file_location(
    "feishu_ws_client",
    Path(__file__).resolve().parent.parent / "feishu-ws-client.py",
)
client = importlib.util.module_from_spec(_spec)
assert _spec.loader
_spec.loader.exec_module(client)


def _load_client_module(name):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).resolve().parent.parent / "feishu-ws-client.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def _message(
    text,
    *,
    mentions=True,
    chat_type="group",
    chat_id="oc_shared",
    message_id="om_123",
    mention_open_id="ou_bot",
):
    return SimpleNamespace(
        message_id=message_id,
        message_type="text",
        chat_type=chat_type,
        chat_id=chat_id,
        content=json.dumps({"text": text}, ensure_ascii=False),
        mentions=[SimpleNamespace(
            key="@_user_1",
            id=SimpleNamespace(open_id=mention_open_id),
            name="LibreBOT",
        )] if mentions else [],
    )


def _card_action(value, *, app_id="cli_app", chat_id="oc_shared", message_id="om_card"):
    return SimpleNamespace(header=SimpleNamespace(app_id=app_id), event=SimpleNamespace(
        action=SimpleNamespace(value=value),
        operator=SimpleNamespace(open_id="ou_operator"),
        context=SimpleNamespace(open_chat_id=chat_id, open_message_id=message_id),
    ))


def test_event_handler_registers_pending_callback_only_in_company_mode(monkeypatch):
    class Builder:
        def __init__(self):
            self.calls = []

        def register_p2_im_message_receive_v1(self, handler):
            self.calls.append(("message", handler))
            return self

        def register_p2_card_action_trigger(self, handler):
            self.calls.append(("card", handler))
            return self

        def build(self):
            return self.calls

    class Dispatcher:
        @staticmethod
        def builder(_verification_token, _encrypt_key):
            return Builder()

    fake_lark = SimpleNamespace(EventDispatcherHandler=Dispatcher)

    monkeypatch.setattr(client, "DEVICE_PENDING_DELETE_ENABLED", False)
    monkeypatch.setattr(client, "INSPECTION_PAGINATION_ENABLED", False)
    assert [name for name, _handler in client.build_event_handler(fake_lark)] == ["message"]

    monkeypatch.setattr(client, "INSPECTION_PAGINATION_ENABLED", True)
    assert [name for name, _handler in client.build_event_handler(fake_lark)] == [
        "message",
        "card",
    ]

    monkeypatch.setattr(client, "INSPECTION_PAGINATION_ENABLED", False)
    monkeypatch.setattr(client, "DEVICE_PENDING_DELETE_ENABLED", True)
    assert [name for name, _handler in client.build_event_handler(fake_lark)] == [
        "message",
        "card",
    ]


def test_tournament_mode_does_not_forward_pending_card_actions(monkeypatch):
    monkeypatch.setattr(client, "DEVICE_PENDING_DELETE_ENABLED", False)
    monkeypatch.setattr(
        client.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected request")),
    )
    value = {"action": "retire_delete", "key": "switch-1", "token": "tok"}
    assert client.resolve_via_bridge(value)["enabled"] is False
    assert client.on_card_action(_card_action(value)) is None


def test_company_mode_forwards_token_guarded_card_action(monkeypatch):
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        @staticmethod
        def read():
            return b'{"ok":true,"action":"delete"}'

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return Response()

    monkeypatch.setattr(client, "DEVICE_PENDING_DELETE_ENABLED", True)
    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    value = {
        "action": "retire_delete",
        "key": "infra-dist-ping|192.168.10.27",
        "token": "tok-company",
    }

    assert client.resolve_via_bridge(value) == {"ok": True, "action": "delete"}
    request, timeout = requests[0]
    assert request.full_url == f"{client.BRIDGE_URL}/retire/resolve"
    assert request.get_method() == "POST"
    assert json.loads(request.data.decode("utf-8")) == {
        "key": "infra-dist-ping|192.168.10.27",
        "action": "delete",
        "token": "tok-company",
    }
    assert timeout == 20

    monkeypatch.setattr(client, "resolve_via_bridge", lambda received: {"ok": True, "action": "keep"})
    monkeypatch.setattr(client, "build_response", lambda received, result: (received, result))
    response = client.on_card_action(_card_action({**value, "action": "retire_keep"}))
    assert response[0]["token"] == "tok-company"
    assert response[1] == {"ok": True, "action": "keep"}


def test_inspection_bridge_transport_uses_dedicated_endpoints_and_short_timeout(monkeypatch):
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        @staticmethod
        def read():
            return b'{"ok":true}'

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return Response()

    monkeypatch.setattr(client, "APP_ID", "cli_app")
    monkeypatch.setattr(client, "INSPECTION_PAGINATION_ENABLED", True)
    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)

    assert client.bind_inspection_card(
        "session-1",
        chat_id="oc_shared",
        source_message_id="om_source",
        card_message_id="om_card",
    ) == {"ok": True}
    assert client.inspection_page_via_bridge(
        {"session_id": "session-1", "page": 2},
        app_id="cli_app",
        chat_id="oc_shared",
        card_message_id="om_card",
    ) == {"ok": True}

    bind_request, bind_timeout = requests[0]
    assert bind_request.full_url == f"{client.BRIDGE_URL}/bot/inspection/bind"
    assert bind_request.get_method() == "POST"
    assert bind_timeout == 1.0
    assert json.loads(bind_request.data.decode("utf-8")) == {
        "session_id": "session-1",
        "app_id": "cli_app",
        "chat_id": "oc_shared",
        "source_message_id": "om_source",
        "card_message_id": "om_card",
    }

    page_request, page_timeout = requests[1]
    assert page_request.full_url == f"{client.BRIDGE_URL}/bot/inspection/page"
    assert page_request.get_method() == "POST"
    assert page_timeout == 1.0
    assert json.loads(page_request.data.decode("utf-8")) == {
        "session_id": "session-1",
        "page": 2,
        "app_id": "cli_app",
        "chat_id": "oc_shared",
        "card_message_id": "om_card",
    }


def test_inspection_bind_retries_only_transport_failures(monkeypatch):
    attempts = []
    sleeps = []

    def post(_path, _payload, _timeout):
        attempts.append(len(attempts) + 1)
        if len(attempts) < 3:
            raise TimeoutError("transient")
        return {"ok": True}

    monkeypatch.setattr(client, "INSPECTION_PAGINATION_ENABLED", True)
    monkeypatch.setattr(client, "_post_bridge_json", post)
    monkeypatch.setattr(client.time, "sleep", sleeps.append)

    assert client.bind_inspection_card(
        "session-1", chat_id="oc_shared",
        source_message_id="om_source", card_message_id="om_card",
    ) == {"ok": True}
    assert attempts == [1, 2, 3]
    assert sleeps == [0.1, 0.2]

    attempts.clear()
    sleeps.clear()
    monkeypatch.setattr(
        client,
        "_post_bridge_json",
        lambda *_args, **_kwargs: attempts.append(1) or {"ok": False, "code": "expired"},
    )
    assert client.bind_inspection_card(
        "session-1", chat_id="oc_shared",
        source_message_id="om_source", card_message_id="om_card",
    ) == {"ok": False, "code": "expired"}
    assert attempts == [1]
    assert sleeps == []


def test_reply_to_message_returns_created_card_message_id(monkeypatch):
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        @staticmethod
        def read():
            return b'{"code":0,"data":{"message_id":"om_card_reply"}}'

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return Response()

    monkeypatch.setattr(client, "tenant_access_token", lambda: "tenant-token")
    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)

    message_id = client.reply_to_message(
        "om_source",
        card={"msg_type": "interactive", "card": {"schema": "2.0"}},
    )

    assert message_id == "om_card_reply"
    request, timeout = requests[0]
    assert request.full_url.endswith("/im/v1/messages/om_source/reply")
    assert timeout == 10
    assert json.loads(request.data.decode("utf-8")) == {
        "msg_type": "interactive",
        "content": json.dumps({"schema": "2.0"}, ensure_ascii=False),
    }


def test_pending_action_transport_errors_use_neutral_unknown_result_copy(monkeypatch):
    monkeypatch.setattr(client, "DEVICE_PENDING_DELETE_ENABLED", True)
    value = {"action": "retire_keep", "key": "switch-1", "token": "tok"}

    monkeypatch.setattr(
        client.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectionError("fixture secret")),
    )
    assert client.resolve_via_bridge(value) == {
        "ok": False,
        "error": "暂时无法确认处理结果，请刷新列表查看。为避免重复操作，请先确认当前状态。",
    }

    def http_error(request, timeout):
        raise client.urllib.error.HTTPError(
            request.full_url, 502, "Bad Gateway", None, io.BytesIO(b"not-json")
        )

    monkeypatch.setattr(client.urllib.request, "urlopen", http_error)
    assert client.resolve_via_bridge(value) == {
        "ok": False,
        "error": "暂时无法确认处理结果，请刷新列表查看。为避免重复操作，请先确认当前状态。（HTTP 502）",
    }


def test_failed_pending_action_card_uses_neutral_subtitle_and_keeps_detail(monkeypatch):
    module_name = "lark_oapi.event.callback.model.p2_card_action_trigger"
    monkeypatch.setitem(
        sys.modules,
        module_name,
        SimpleNamespace(P2CardActionTriggerResponse=lambda payload: payload),
    )

    response = client.build_response(
        {"device": "switch-1"},
        {"ok": False, "error": "设备当前可达，未执行删除"},
    )

    assert response["card"]["data"]["header"]["subtitle"]["content"] == "操作未完成，请查看详情"
    assert "设备当前可达，未执行删除" in response["card"]["data"]["body"]["elements"][0]["content"]


def test_inspection_callback_is_independent_from_pending_delete(monkeypatch):
    module_name = "lark_oapi.event.callback.model.p2_card_action_trigger"
    monkeypatch.setitem(
        sys.modules,
        module_name,
        SimpleNamespace(P2CardActionTriggerResponse=lambda payload: payload),
    )
    calls = []
    monkeypatch.setattr(client, "APP_ID", "cli_app")
    monkeypatch.setattr(client, "INSPECTION_PAGINATION_ENABLED", True)
    monkeypatch.setattr(client, "DEVICE_PENDING_DELETE_ENABLED", False)
    monkeypatch.setattr(
        client,
        "inspection_page_via_bridge",
        lambda value, **context: calls.append((value, context)) or {
            "ok": True,
            "message": "已切换巡检页面",
            "card": {
                "msg_type": "interactive",
                "card": {"schema": "2.0", "body": {"elements": []}},
            },
        },
    )
    value = {"action": "inspection_page", "session_id": "session", "page": 2}
    response = client.on_card_action(_card_action(value))
    assert calls == [(value, {
        "app_id": "cli_app",
        "chat_id": "oc_shared",
        "card_message_id": "om_card",
    })]
    assert response["card"] == {
        "type": "raw",
        "data": {"schema": "2.0", "body": {"elements": []}},
    }


def test_inspection_callback_missing_or_wrong_context_fails_closed(monkeypatch):
    module_name = "lark_oapi.event.callback.model.p2_card_action_trigger"
    monkeypatch.setitem(
        sys.modules,
        module_name,
        SimpleNamespace(P2CardActionTriggerResponse=lambda payload: payload),
    )
    monkeypatch.setattr(client, "APP_ID", "cli_app")
    monkeypatch.setattr(client, "INSPECTION_PAGINATION_ENABLED", True)
    monkeypatch.setattr(
        client,
        "inspection_page_via_bridge",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must fail before bridge")),
    )
    value = {"action": "inspection_page", "session_id": "session", "page": 2}
    for kwargs in (
        {"app_id": "wrong"},
        {"chat_id": ""},
        {"message_id": ""},
    ):
        response = client.on_card_action(_card_action(value, **kwargs))
        assert response["toast"]["type"] == "error"
        assert "card" not in response


def test_inspection_callback_latest_started_request_wins(monkeypatch):
    module_name = "lark_oapi.event.callback.model.p2_card_action_trigger"
    monkeypatch.setitem(
        sys.modules,
        module_name,
        SimpleNamespace(P2CardActionTriggerResponse=lambda payload: payload),
    )
    started = threading.Event()
    release = threading.Event()
    responses = {}

    def page(value, **_context):
        if value["page"] == 2:
            started.set()
            assert release.wait(2)
            marker = "older"
        else:
            marker = "newer"
        return {
            "ok": True,
            "message": marker,
            "card": {"msg_type": "interactive", "card": {"schema": "2.0", "marker": marker}},
        }

    monkeypatch.setattr(client, "APP_ID", "cli_app")
    monkeypatch.setattr(client, "INSPECTION_PAGINATION_ENABLED", True)
    monkeypatch.setattr(client, "inspection_page_via_bridge", page)
    with client._INSPECTION_CALLBACK_LOCK:
        client._INSPECTION_CALLBACK_SEQUENCE.clear()

    older = threading.Thread(
        target=lambda: responses.setdefault("older", client.on_card_action(_card_action({
            "action": "inspection_page", "session_id": "session", "page": 2,
        }))),
    )
    older.start()
    assert started.wait(2)
    responses["newer"] = client.on_card_action(_card_action({
        "action": "inspection_page", "session_id": "session", "page": 1,
    }))
    release.set()
    older.join(2)
    assert not older.is_alive()

    assert responses["newer"]["card"]["data"]["marker"] == "newer"
    assert "card" not in responses["older"]
    assert responses["older"]["toast"] == {
        "type": "info", "content": "已忽略较早的翻页操作。",
    }


def test_unknown_action_never_falls_back_to_retire(monkeypatch):
    monkeypatch.setattr(client, "INSPECTION_PAGINATION_ENABLED", True)
    monkeypatch.setattr(client, "DEVICE_PENDING_DELETE_ENABLED", True)
    monkeypatch.setattr(
        client,
        "resolve_via_bridge",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unknown action mutated state")),
    )
    monkeypatch.setattr(
        client,
        "inspection_page_via_bridge",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unknown action paginated")),
    )
    assert client.on_card_action(_card_action({"action": "something_else"})) is None


def test_initial_inspection_sends_only_first_card_and_binds_returned_message_id(monkeypatch):
    card = {"msg_type": "interactive", "card": {"schema": "2.0", "header": {}}}
    queries = []
    replies = []
    bindings = []
    monkeypatch.setattr(client, "INSPECTION_PAGINATION_ENABLED", True)
    monkeypatch.setattr(client, "APP_ID", "cli_app")
    monkeypatch.setattr(
        client,
        "query_via_bridge",
        lambda command, context: queries.append((command, context)) or {
            "ok": True,
            "text": "完成",
            "cards": [card],
            "inspection_session": {"session_id": "session-1"},
        },
    )
    monkeypatch.setattr(
        client,
        "reply_to_message",
        lambda message_id, text="", card=None: replies.append((message_id, text, card)) or "om_card_reply",
    )
    monkeypatch.setattr(
        client,
        "bind_inspection_card",
        lambda session_id, **context: bindings.append((session_id, context)) or {"ok": True},
    )

    client._process_message(
        "om_source", "网络巡检", False, "oc_shared", "cli_app",
    )
    assert queries == [("网络巡检", {
        "app_id": "cli_app",
        "chat_id": "oc_shared",
        "source_message_id": "om_source",
    })]
    assert len(replies) == 1
    assert bindings == [("session-1", {
        "chat_id": "oc_shared",
        "source_message_id": "om_source",
        "card_message_id": "om_card_reply",
    })]
def test_extracts_command_after_robot_mention():
    message = _message("@_user_1  查光功率 192.168.10.31 Gi1/0/1")
    assert client.should_handle_message(message) is True
    assert client.extract_command(message) == "查光功率 192.168.10.31 Gi1/0/1"


def test_event_command_routing_handles_scope_case_boundary_and_multi_word_names():
    # Empty or whitespace EVENT_NAME defaults to silently rejecting commands;
    # only the explicitly allowed legacy p2p path keeps unscoped behavior.
    assert client.route_event_command("网络巡检", "") is None
    assert client.route_event_command("网络巡检", "   ") is None
    assert client.route_event_command("网络巡检", "\t") is None
    assert client.route_event_command("网络巡检", "", allow_unscoped=True) == "网络巡检"
    assert client.route_event_command("网络巡检", "  ", allow_unscoped=True) == "网络巡检"
    # allow_unscoped must never bypass the prefix requirement of a named event.
    assert client.route_event_command("网络巡检", "Singapore", allow_unscoped=True) is None
    assert client.route_event_command("Shanghai 网络巡检", "Singapore", allow_unscoped=True) is None
    assert client.route_event_command("Singapore 网络巡检", "Singapore") == "网络巡检"
    assert client.route_event_command("singapore 帮助", "Singapore") == "帮助"
    assert client.route_event_command("Singapore 光功率巡检", "Singapore") == "光功率巡检"
    assert client.route_event_command("Singapore: 上联冗余巡检", "Singapore") == "上联冗余巡检"
    assert client.route_event_command("Singapore：待删除设备", "Singapore") == "待删除设备"
    assert client.route_event_command("Singapore - 网络巡检", "Singapore") == "网络巡检"
    assert client.route_event_command("Shanghai 网络巡检", "Singapore") is None
    assert client.route_event_command("网络巡检", "Singapore") is None
    assert client.route_event_command("SG2 网络巡检", "SG") is None
    assert client.route_event_command("IEM Chengdu 网络巡检", "IEM Chengdu") == "网络巡检"
    assert client.route_event_command("IEM Cologne 网络巡检", "IEM Chengdu") is None


def test_multi_instance_routing_is_mutually_exclusive():
    singapore_command = "Singapore 网络巡检"
    shanghai_command = "Shanghai 网络巡检"
    assert client.route_event_command(singapore_command, "Singapore") == "网络巡检"
    assert client.route_event_command(shanghai_command, "Singapore") is None
    assert client.route_event_command(singapore_command, "Shanghai") is None
    assert client.route_event_command(shanghai_command, "Shanghai") == "网络巡检"
    assert client.route_event_command(singapore_command, "") is None
    assert client.route_event_command(shanghai_command, "") is None


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
    monkeypatch.setattr(client, "EVENT_NAME", "Singapore")
    monkeypatch.setattr(client, "CHAT_TARGET", "")
    monkeypatch.setattr(client, "_POLL_READY", False)
    monkeypatch.setattr(client.threading, "Thread", ImmediateThread)
    client.on_message(SimpleNamespace(event=SimpleNamespace(message=_message("@_user_1 Singapore 帮助"))))
    assert calls == [("om_123", "帮助")]

    calls.clear()
    client._SEEN_MESSAGES.clear()
    monkeypatch.setattr(client, "_POLL_READY", True)
    client.on_message(SimpleNamespace(event=SimpleNamespace(message=_message("@_user_1 帮助"))))
    assert calls == []


def test_long_connection_fallback_routes_scope_and_warns_only_once(monkeypatch):
    calls = []
    logs = []

    class ImmediateThread:
        def __init__(self, target, args, **_kwargs):
            self.target, self.args = target, args

        def start(self):
            calls.append(self.args)

    client._SEEN_MESSAGES.clear()
    monkeypatch.setattr(client, "EVENT_NAME", "Singapore")
    monkeypatch.setattr(client, "CHAT_TARGET", "oc_shared")
    monkeypatch.setattr(client, "_POLL_READY", False)
    monkeypatch.setattr(client, "_DEGRADED_WARNING_EMITTED", False)
    monkeypatch.setattr(client, "log", logs.append)
    monkeypatch.setattr(client.threading, "Thread", ImmediateThread)

    client.on_message(SimpleNamespace(event=SimpleNamespace(message=_message(
        "@_user_1 Shanghai 网络巡检", message_id="om_wrong",
    ))))
    client.on_message(SimpleNamespace(event=SimpleNamespace(message=_message(
        "@_user_1 网络巡检", message_id="om_unscoped",
    ))))
    client.on_message(SimpleNamespace(event=SimpleNamespace(message=_message(
        "@_user_1 Singapore 网络巡检", message_id="om_right",
    ))))

    assert calls == [("om_right", "网络巡检")]
    assert {"om_wrong", "om_unscoped", "om_right"}.issubset(client._SEEN_MESSAGES)
    assert sum("shared-group event routing is degraded" in item for item in logs) == 1


def test_long_connection_fallback_rejects_blank_name_for_group_like_chats(monkeypatch):
    class UnexpectedThread:
        def __init__(self, *args, **kwargs):
            raise AssertionError("no command thread may start")

    monkeypatch.setattr(client, "EVENT_NAME", "  ")
    monkeypatch.setattr(client, "_POLL_READY", False)
    monkeypatch.setattr(client, "_DEGRADED_WARNING_EMITTED", True)
    monkeypatch.setattr(client.threading, "Thread", UnexpectedThread)

    for chat_target in ("oc_shared", ""):
        monkeypatch.setattr(client, "CHAT_TARGET", chat_target)
        for chat_type in ("group", "unknown", None):
            client._SEEN_MESSAGES.clear()
            message = _message("@_user_1 帮助", chat_type=chat_type, message_id="om_reject")
            assert client.on_message(SimpleNamespace(event=SimpleNamespace(message=message))) is None
    assert "om_reject" in client._SEEN_MESSAGES


def test_long_connection_p2p_keeps_legacy_unscoped_fallback(monkeypatch):
    calls = []

    class ImmediateThread:
        def __init__(self, target, args, **_kwargs):
            self.args = args

        def start(self):
            calls.append(self.args)

    client._SEEN_MESSAGES.clear()
    monkeypatch.setattr(client, "EVENT_NAME", "")
    monkeypatch.setattr(client, "CHAT_TARGET", "oc_shared")
    monkeypatch.setattr(client, "_POLL_READY", False)
    monkeypatch.setattr(client.threading, "Thread", ImmediateThread)

    client.on_message(SimpleNamespace(event=SimpleNamespace(message=_message(
        "@_user_1 帮助", chat_type="p2p", message_id="om_p2p",
    ))))
    assert calls == [("om_p2p", "帮助")]

    # A non-empty EVENT_NAME still requires its prefix even in p2p chats.
    calls.clear()
    monkeypatch.setattr(client, "EVENT_NAME", "Singapore")
    client.on_message(SimpleNamespace(event=SimpleNamespace(message=_message(
        "@_user_1 网络巡检", chat_type="p2p", message_id="om_p2p_plain",
    ))))
    client.on_message(SimpleNamespace(event=SimpleNamespace(message=_message(
        "@_user_1 Singapore 网络巡检", chat_type="p2p", message_id="om_p2p_scoped",
    ))))
    assert calls == [("om_p2p_scoped", "网络巡检")]


def test_site_polling_baselines_old_messages_then_handles_new_once(monkeypatch):
    old = {
        "message_id": "om_old", "message_type": "text", "chat_type": "group",
        "create_time": "100", "body": {"content": '{"text":"@_user_1 Singapore 帮助"}'},
        "mentions": [{"key": "@_user_1"}], "sender": {"sender_type": "user"},
    }
    new = {
        "message_id": "om_new", "message_type": "text", "chat_type": "group",
        "create_time": "200", "body": {"content": '{"text":"@_user_1 Singapore 光功率巡检"}'},
        "mentions": [{"key": "@_user_1"}], "sender": {"sender_type": "user"},
    }
    calls = []

    class ImmediateThread:
        def __init__(self, target, args, **_kwargs):
            self.target, self.args = target, args

        def start(self):
            calls.append(self.args)

    client._SEEN_MESSAGES.clear()
    monkeypatch.setattr(client, "EVENT_NAME", "Singapore")
    monkeypatch.setattr(client.threading, "Thread", ImmediateThread)
    assert client.process_polled_messages([old], baseline=True) == 0
    assert client.process_polled_messages([old, new]) == 1
    assert client.process_polled_messages([new]) == 0
    assert calls == [("om_new", "光功率巡检")]


def test_site_polling_rejects_blank_event_name_silently(monkeypatch):
    def history(message_id, text, created, chat_type="group"):
        return {
            "message_id": message_id,
            "message_type": "text",
            "chat_type": chat_type,
            "create_time": str(created),
            "body": {"content": json.dumps({"text": f"@_user_1 {text}"}, ensure_ascii=False)},
            "mentions": [{"key": "@_user_1"}],
            "sender": {"sender_type": "user"},
        }

    class UnexpectedThread:
        def __init__(self, *args, **kwargs):
            raise AssertionError("no command thread may start")

    client._SEEN_MESSAGES.clear()
    monkeypatch.setattr(client, "EVENT_NAME", "   ")
    monkeypatch.setattr(client, "CHAT_TARGET", "oc_shared")
    monkeypatch.setattr(client.threading, "Thread", UnexpectedThread)
    monkeypatch.setattr(
        client, "query_via_bridge",
        lambda _command: (_ for _ in ()).throw(AssertionError("unexpected bridge query")),
    )
    monkeypatch.setattr(
        client, "reply_to_message",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected reply")),
    )

    # Unscoped, other-event, help and mention-only commands are all dropped;
    # missing chat_type or a mislabeled p2p entry cannot bypass the rule.
    messages = [
        history("om_blank_plain", "网络巡检", 100),
        history("om_blank_help", "帮助", 101),
        history("om_blank_other", "Singapore 网络巡检", 102),
        history("om_blank_no_type", "网络巡检", 103, chat_type=None),
        history("om_blank_p2p_label", "网络巡检", 104, chat_type="p2p"),
    ]
    assert client.process_polled_messages(messages) == 0
    assert {
        "om_blank_plain", "om_blank_help", "om_blank_other",
        "om_blank_no_type", "om_blank_p2p_label",
    }.issubset(client._SEEN_MESSAGES)


def test_multi_instance_polling_isolates_same_messages_per_process(monkeypatch):
    def history(message_id, text, created):
        return {
            "message_id": message_id,
            "message_type": "text",
            "chat_type": "group",
            "create_time": str(created),
            "body": {"content": json.dumps({"text": f"@_user_1 {text}"}, ensure_ascii=False)},
            "mentions": [{"key": "@_user_1"}],
            "sender": {"sender_type": "user"},
        }

    class ImmediateThread:
        def __init__(self, target, args, **_kwargs):
            self.args = args

        def start(self):
            calls.append(self.args)

    messages = [
        history("om_multi_sg", "Singapore 网络巡检", 200),
        history("om_multi_sh", "Shanghai 网络巡检", 201),
        history("om_multi_plain", "网络巡检", 202),
    ]
    # Simulate independent processes with separate dedup caches per instance.
    for event_name, expected in (("Singapore", ["om_multi_sg"]), ("Shanghai", ["om_multi_sh"]), ("", [])):
        calls = []
        client._SEEN_MESSAGES = {}
        monkeypatch.setattr(client, "EVENT_NAME", event_name)
        monkeypatch.setattr(client.threading, "Thread", ImmediateThread)
        handled = client.process_polled_messages(messages)
        assert handled == len(expected)
        assert [message_id for message_id, _command in calls] == expected


def test_site_polling_reserves_and_silently_ignores_other_events(monkeypatch):
    def history(message_id, text, created):
        return {
            "message_id": message_id,
            "message_type": "text",
            "chat_type": "group",
            "create_time": str(created),
            "body": {"content": json.dumps({"text": f"@_user_1 {text}"}, ensure_ascii=False)},
            "mentions": [{"key": "@_user_1"}],
            "sender": {"sender_type": "user"},
        }

    calls = []

    class ImmediateThread:
        def __init__(self, target, args, **_kwargs):
            self.args = args

        def start(self):
            calls.append(self.args)

    singapore = history("om_sg", "Singapore 网络巡检", 100)
    shanghai = history("om_sh", "Shanghai 网络巡检", 101)
    unscoped = history("om_plain", "帮助", 102)
    client._SEEN_MESSAGES.clear()
    monkeypatch.setattr(client, "EVENT_NAME", "Singapore")
    monkeypatch.setattr(client.threading, "Thread", ImmediateThread)

    assert client.process_polled_messages([singapore, shanghai, unscoped]) == 1
    assert calls == [("om_sg", "网络巡检")]
    assert {"om_sg", "om_sh", "om_plain"}.issubset(client._SEEN_MESSAGES)
    assert client.process_polled_messages([singapore, shanghai, unscoped]) == 0


def test_global_help_requires_exact_command_bot_identity_and_target_group(monkeypatch):
    monkeypatch.setattr(client, "GLOBAL_HELP_RESPONDER", True)
    monkeypatch.setattr(client, "BOT_OPEN_ID", "ou_bot")
    monkeypatch.setattr(client, "EVENT_NAME", "PGS")
    monkeypatch.setattr(client, "CHAT_TARGET", "oc_shared")

    message = _message("@_user_1   帮助", chat_id="oc_shared")
    assert client._route_message_command(
        message, client.extract_command(message),
        source_chat_id="oc_shared", source_is_group=True,
    ) == ("帮助", True)

    for text in ("@_user_1", "@_user_1 help", "@_user_1 ?", "@_user_1 命令", "@_user_1 帮助 网络巡检"):
        rejected = _message(text, chat_id="oc_shared")
        assert client._route_message_command(
            rejected, client.extract_command(rejected),
            source_chat_id="oc_shared", source_is_group=True,
        ) is None

    wrong_bot = _message("@_user_1 帮助", mention_open_id="ou_other")
    assert client._route_message_command(
        wrong_bot, client.extract_command(wrong_bot),
        source_chat_id="oc_shared", source_is_group=True,
    ) is None

    wrong_group = _message("@_user_1 帮助", chat_id="oc_other")
    assert client._route_message_command(
        wrong_group, client.extract_command(wrong_group),
        source_chat_id="oc_other", source_is_group=True,
    ) is None

    missing_key = _message("帮助")
    assert client._route_message_command(
        missing_key, client.extract_command(missing_key),
        source_chat_id="oc_shared", source_is_group=True,
    ) is None


def test_global_help_accepts_polling_and_event_mention_identity_shapes(monkeypatch):
    monkeypatch.setattr(client, "GLOBAL_HELP_RESPONDER", True)
    monkeypatch.setattr(client, "BOT_OPEN_ID", "ou_bot")
    monkeypatch.setattr(client, "EVENT_NAME", "PGS")
    monkeypatch.setattr(client, "CHAT_TARGET", "oc_shared")

    event_message = _message("@_user_1 帮助")
    polling_message = {
        "message_id": "om_poll_help",
        "message_type": "text",
        "body": {"content": json.dumps({"text": "@_user_1 帮助"}, ensure_ascii=False)},
        "mentions": [{"key": "@_user_1", "id": "ou_bot", "id_type": "open_id"}],
        "sender": {"sender_type": "user"},
    }
    assert client._mentions_configured_bot(event_message) is True
    assert client._mentions_configured_bot(polling_message) is True

    polling_message["mentions"][0]["id_type"] = "user_id"
    assert client._mentions_configured_bot(polling_message) is False
    polling_message["mentions"][0].pop("id_type")
    polling_message["mentions"][0]["id"] = {"open_id": "ou_bot"}
    assert client._mentions_configured_bot(polling_message) is True


def test_global_help_switch_and_empty_event_name_fail_closed(monkeypatch):
    message = _message("@_user_1 帮助")
    monkeypatch.setattr(client, "BOT_OPEN_ID", "ou_bot")
    monkeypatch.setattr(client, "CHAT_TARGET", "oc_shared")

    for enabled, event_name in ((False, "PGS"), (True, ""), (True, "   ")):
        monkeypatch.setattr(client, "GLOBAL_HELP_RESPONDER", enabled)
        monkeypatch.setattr(client, "EVENT_NAME", event_name)
        assert client._route_message_command(
            message, client.extract_command(message),
            source_chat_id="oc_shared", source_is_group=True,
        ) is None


def test_global_help_configuration_enables_only_exact_true(monkeypatch):
    monkeypatch.setenv("FEISHU_BOT_OPEN_ID", " ou_bot ")
    monkeypatch.setenv("EVENT_NAME", " PGS ")
    monkeypatch.setenv("FEISHU_CHAT_ID", " oc_shared ")
    for index, value in enumerate(("", "false", "1", "yes", "on", "invalid")):
        monkeypatch.setenv("FEISHU_GLOBAL_HELP_RESPONDER", value)
        loaded = _load_client_module(f"feishu_ws_global_off_{index}")
        assert loaded.GLOBAL_HELP_RESPONDER is False
    monkeypatch.setenv("FEISHU_GLOBAL_HELP_RESPONDER", " TrUe ")
    loaded = _load_client_module("feishu_ws_global_on")
    assert loaded.GLOBAL_HELP_RESPONDER is True
    assert loaded.BOT_OPEN_ID == "ou_bot"


def test_global_help_uses_direct_or_last_resolved_target_group(monkeypatch):
    monkeypatch.setattr(client, "CHAT_TARGET", "oc_direct")
    monkeypatch.setattr(client, "_RESOLVED_COMMAND_CHAT_ID", "oc_old")
    assert client._configured_command_chat_id() == "oc_direct"

    monkeypatch.setattr(client, "CHAT_TARGET", "统一监控群")
    monkeypatch.setattr(client, "_RESOLVED_COMMAND_CHAT_ID", "")
    assert client._configured_command_chat_id() == ""
    client._publish_resolved_command_chat_id("oc_first")
    assert client._configured_command_chat_id() == "oc_first"
    client._publish_resolved_command_chat_id("invalid")
    assert client._configured_command_chat_id() == "oc_first"
    client._publish_resolved_command_chat_id("oc_new")
    assert client._configured_command_chat_id() == "oc_new"
    monkeypatch.setattr(client, "CHAT_TARGET", "")
    assert client._configured_command_chat_id() == ""


def test_long_connection_global_help_is_target_group_only(monkeypatch):
    calls = []

    class ImmediateThread:
        def __init__(self, target, args, **_kwargs):
            self.args = args

        def start(self):
            calls.append(self.args)

    client._SEEN_MESSAGES.clear()
    monkeypatch.setattr(client, "GLOBAL_HELP_RESPONDER", True)
    monkeypatch.setattr(client, "BOT_OPEN_ID", "ou_bot")
    monkeypatch.setattr(client, "EVENT_NAME", "PGS")
    monkeypatch.setattr(client, "CHAT_TARGET", "oc_shared")
    monkeypatch.setattr(client, "_POLL_READY", False)
    monkeypatch.setattr(client, "_DEGRADED_WARNING_EMITTED", True)
    monkeypatch.setattr(client.threading, "Thread", ImmediateThread)

    client.on_message(SimpleNamespace(event=SimpleNamespace(message=_message(
        "@_user_1 帮助", message_id="om_global", chat_id="oc_shared",
    ))))
    client.on_message(SimpleNamespace(event=SimpleNamespace(message=_message(
        "@_user_1 帮助", message_id="om_other", chat_id="oc_other",
    ))))
    client.on_message(SimpleNamespace(event=SimpleNamespace(message=_message(
        "@_user_1 帮助", message_id="om_missing", chat_id="",
    ))))
    assert calls == [("om_global", "帮助", True)]

    # Existing scoped routing stays independent of the newly configured group.
    client.on_message(SimpleNamespace(event=SimpleNamespace(message=_message(
        "@_user_1 PGS 网络巡检", message_id="om_scoped", chat_id="oc_other",
    ))))
    assert calls[-1] == ("om_scoped", "网络巡检")

    calls.clear()
    monkeypatch.setattr(client, "CHAT_TARGET", "统一监控群")
    monkeypatch.setattr(client, "_RESOLVED_COMMAND_CHAT_ID", "")
    client.on_message(SimpleNamespace(event=SimpleNamespace(message=_message(
        "@_user_1 帮助", message_id="om_unresolved", chat_id="oc_shared",
    ))))
    assert calls == []
    client._publish_resolved_command_chat_id("oc_shared")
    client.on_message(SimpleNamespace(event=SimpleNamespace(message=_message(
        "@_user_1 帮助", message_id="om_resolved", chat_id="oc_shared",
    ))))
    assert calls == [("om_resolved", "帮助", True)]


def test_polling_global_help_uses_actual_history_source_and_deduplicates(monkeypatch):
    calls = []

    class ImmediateThread:
        def __init__(self, target, args, **_kwargs):
            self.args = args

        def start(self):
            calls.append(self.args)

    message = {
        "message_id": "om_poll_global",
        "message_type": "text",
        "chat_type": "p2p",
        "create_time": "100",
        "body": {"content": json.dumps({"text": "@_user_1 帮助"}, ensure_ascii=False)},
        "mentions": [{"key": "@_user_1", "id": "ou_bot", "id_type": "open_id"}],
        "sender": {"sender_type": "user"},
    }
    client._SEEN_MESSAGES.clear()
    monkeypatch.setattr(client, "GLOBAL_HELP_RESPONDER", True)
    monkeypatch.setattr(client, "BOT_OPEN_ID", "ou_bot")
    monkeypatch.setattr(client, "EVENT_NAME", "PGS")
    monkeypatch.setattr(client, "CHAT_TARGET", "oc_shared")
    monkeypatch.setattr(client.threading, "Thread", ImmediateThread)

    assert client.process_polled_messages([message], source_chat_id="oc_shared") == 1
    assert client.process_polled_messages([message], source_chat_id="oc_shared") == 0
    assert calls == [("om_poll_global", "帮助", True)]

    client._SEEN_MESSAGES.clear()
    assert client.process_polled_messages(
        [message], baseline=True, source_chat_id="oc_shared",
    ) == 0
    monkeypatch.setattr(client, "_POLL_READY", False)
    client.on_message(SimpleNamespace(event=SimpleNamespace(message=_message(
        "@_user_1 帮助", message_id="om_poll_global", chat_id="oc_shared",
    ))))
    assert calls == [("om_poll_global", "帮助", True)]


def test_only_one_independent_instance_answers_global_help(monkeypatch):
    real_thread = threading.Thread
    modules = []
    for index, (event_name, enabled) in enumerate((("PGS", "true"), ("Shanghai", "false"))):
        monkeypatch.setenv("EVENT_NAME", event_name)
        monkeypatch.setenv("FEISHU_CHAT_ID", "oc_shared")
        monkeypatch.setenv("FEISHU_BOT_OPEN_ID", "ou_bot")
        monkeypatch.setenv("FEISHU_GLOBAL_HELP_RESPONDER", enabled)
        modules.append(_load_client_module(f"feishu_ws_instance_{index}"))

    bridge_calls = []
    for loaded in modules:
        loaded._SEEN_MESSAGES = {}
        loaded.query_via_bridge = lambda command, event=loaded.EVENT_NAME: (
            bridge_calls.append((event, command)) or {"ok": True, "text": "help"}
        )
        loaded.reply_to_message = lambda *_args, **_kwargs: None

        class ImmediateThread:
            def __init__(self, target, args, **_kwargs):
                self.target, self.args = target, args

            def start(self):
                self.target(*self.args)

        loaded.threading = SimpleNamespace(Thread=ImmediateThread)
        loaded.process_polled_messages([{
            "message_id": "om_shared_global",
            "message_type": "text",
            "create_time": "100",
            "body": {"content": json.dumps({"text": "@_user_1 帮助"}, ensure_ascii=False)},
            "mentions": [{"key": "@_user_1", "id": "ou_bot", "id_type": "open_id"}],
            "sender": {"sender_type": "user"},
        }], source_chat_id="oc_shared")

    assert bridge_calls == [("PGS", "帮助")]
    assert threading.Thread is real_thread


def test_polling_publishes_group_and_failure_keeps_it_for_long_connection(monkeypatch):
    class StopPolling(Exception):
        pass

    fetch_count = 0
    sleep_count = 0
    calls = []

    def fetch_messages(_token, chat_id):
        nonlocal fetch_count
        fetch_count += 1
        assert chat_id == "oc_resolved"
        if fetch_count == 1:
            return []
        raise ConnectionError("temporary polling failure")

    def controlled_sleep(_seconds):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count >= 2:
            raise StopPolling

    class ImmediateThread:
        def __init__(self, target, args, **_kwargs):
            self.args = args

        def start(self):
            calls.append(self.args)

    client._SEEN_MESSAGES.clear()
    monkeypatch.setattr(client, "GLOBAL_HELP_RESPONDER", True)
    monkeypatch.setattr(client, "BOT_OPEN_ID", "ou_bot")
    monkeypatch.setattr(client, "EVENT_NAME", "PGS")
    monkeypatch.setattr(client, "CHAT_TARGET", "统一监控群")
    monkeypatch.setattr(client, "_RESOLVED_COMMAND_CHAT_ID", "")
    monkeypatch.setattr(client, "_POLL_READY", False)
    monkeypatch.setattr(client, "_DEGRADED_WARNING_EMITTED", True)
    monkeypatch.setattr(client, "tenant_access_token", lambda: "token")
    monkeypatch.setattr(client, "resolve_command_chat", lambda _token: "oc_resolved")
    monkeypatch.setattr(client, "fetch_chat_messages", fetch_messages)
    monkeypatch.setattr(client.time, "sleep", controlled_sleep)
    monkeypatch.setattr(client.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(client, "log", lambda _message: None)

    try:
        client.poll_site_group_commands()
    except StopPolling:
        pass
    else:
        raise AssertionError("controlled polling loop did not stop")

    assert fetch_count == 2
    assert client._POLL_READY is False
    assert client._configured_command_chat_id() == "oc_resolved"

    client.on_message(SimpleNamespace(event=SimpleNamespace(message=_message(
        "@_user_1 帮助", message_id="om_after_failure", chat_id="oc_resolved",
    ))))
    assert calls == [("om_after_failure", "帮助", True)]


def test_responder_rejects_other_unscoped_commands_in_both_group_entries(monkeypatch):
    class UnexpectedThread:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("rejected command must not start a worker")

    monkeypatch.setattr(client, "GLOBAL_HELP_RESPONDER", True)
    monkeypatch.setattr(client, "BOT_OPEN_ID", "ou_bot")
    monkeypatch.setattr(client, "EVENT_NAME", "PGS")
    monkeypatch.setattr(client, "CHAT_TARGET", "oc_shared")
    monkeypatch.setattr(client, "_POLL_READY", False)
    monkeypatch.setattr(client, "_DEGRADED_WARNING_EMITTED", True)
    monkeypatch.setattr(client.threading, "Thread", UnexpectedThread)
    monkeypatch.setattr(
        client, "query_via_bridge",
        lambda _command: (_ for _ in ()).throw(AssertionError("unexpected Bridge query")),
    )
    monkeypatch.setattr(
        client, "reply_to_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected reply")),
    )

    commands = ("网络巡检", "光功率巡检", "上联冗余巡检", "待删除设备")
    for index, command in enumerate(commands):
        polling_message = {
            "message_id": f"om_poll_reject_{index}",
            "message_type": "text",
            "body": {"content": json.dumps({"text": f"@_user_1 {command}"}, ensure_ascii=False)},
            "mentions": [{"key": "@_user_1", "id": "ou_bot", "id_type": "open_id"}],
            "sender": {"sender_type": "user"},
        }
        client._SEEN_MESSAGES.clear()
        assert client.process_polled_messages(
            [polling_message], source_chat_id="oc_shared",
        ) == 0

        client._SEEN_MESSAGES.clear()
        message = _message(
            f"@_user_1 {command}",
            message_id=f"om_event_reject_{index}",
            chat_id="oc_shared",
        )
        assert client.on_message(SimpleNamespace(event=SimpleNamespace(message=message))) is None

    for index, chat_type in enumerate(("unknown", None)):
        client._SEEN_MESSAGES.clear()
        message = _message(
            "@_user_1 帮助",
            message_id=f"om_type_reject_{index}",
            chat_type=chat_type,
            chat_id="oc_shared",
        )
        assert client.on_message(SimpleNamespace(event=SimpleNamespace(message=message))) is None


def test_responder_does_not_change_p2p_compatibility(monkeypatch):
    calls = []

    class ImmediateThread:
        def __init__(self, target, args, **_kwargs):
            self.args = args

        def start(self):
            calls.append(self.args)

    client._SEEN_MESSAGES.clear()
    monkeypatch.setattr(client, "GLOBAL_HELP_RESPONDER", True)
    monkeypatch.setattr(client, "BOT_OPEN_ID", "ou_bot")
    monkeypatch.setattr(client, "CHAT_TARGET", "oc_shared")
    monkeypatch.setattr(client, "_POLL_READY", False)
    monkeypatch.setattr(client, "_DEGRADED_WARNING_EMITTED", True)
    monkeypatch.setattr(client.threading, "Thread", ImmediateThread)

    monkeypatch.setattr(client, "EVENT_NAME", "PGS")
    client.on_message(SimpleNamespace(event=SimpleNamespace(message=_message(
        "@_user_1 帮助", chat_type="p2p", message_id="om_p2p_unscoped",
    ))))
    client.on_message(SimpleNamespace(event=SimpleNamespace(message=_message(
        "@_user_1 PGS 帮助", chat_type="p2p", message_id="om_p2p_scoped_help",
    ))))
    assert calls == [("om_p2p_scoped_help", "帮助")]

    monkeypatch.setattr(client, "EVENT_NAME", "")
    client.on_message(SimpleNamespace(event=SimpleNamespace(message=_message(
        "@_user_1 网络巡检", chat_type="p2p", message_id="om_p2p_legacy",
    ))))
    assert calls[-1] == ("om_p2p_legacy", "网络巡检")


def test_global_help_reply_skips_event_decoration_without_mutating_event(monkeypatch):
    replies = []
    monkeypatch.setattr(client, "EVENT_NAME", "PGS")
    monkeypatch.setattr(client, "query_via_bridge", lambda command: {
        "ok": True, "text": f"reply:{command}",
    })
    monkeypatch.setattr(
        client, "reply_to_message",
        lambda message_id, text=None, card=None: replies.append((message_id, text, card)),
    )

    client._process_message("om_global", "帮助", True)
    client._process_message("om_scoped", "帮助")
    assert replies == [
        ("om_global", "reply:帮助", None),
        ("om_scoped", "【PGS】\nreply:帮助", None),
    ]
    assert client.EVENT_NAME == "PGS"

    replies.clear()
    card = {"header": {"title": {"tag": "plain_text", "content": "LibreBOT 帮助"}}}
    monkeypatch.setattr(client, "query_via_bridge", lambda _command: {
        "ok": True, "cards": [card],
    })
    client._process_message("om_global_card", "帮助", True)
    client._process_message("om_scoped_card", "帮助")
    assert replies[0] == ("om_global_card", None, card)
    assert replies[1][2]["header"]["title"]["content"] == "【PGS】 LibreBOT 帮助"
    assert card["header"]["title"]["content"] == "LibreBOT 帮助"
