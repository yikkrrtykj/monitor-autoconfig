import importlib.util
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


def _message(text, *, mentions=True, chat_type="group", chat_id="oc_shanghai"):
    return SimpleNamespace(
        message_id="om_123",
        message_type="text",
        chat_type=chat_type,
        chat_id=chat_id,
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
    monkeypatch.setattr(client, "query_via_bridge", lambda _command, _route=None: {"ok": True, "cards": cards})
    monkeypatch.setattr(client, "reply_to_message", lambda message_id, text="", card=None: calls.append((message_id, text, card)))
    monkeypatch.setattr(client.time, "sleep", lambda _seconds: None)
    client._process_message("om_cards", "待删除设备")
    assert [item[2] for item in calls] == cards


def test_hub_routes_group_messages_by_exact_chat_id(monkeypatch):
    routes = client.parse_site_routes(json.dumps([
        {
            "site_id": "shanghai",
            "chat_id": "oc_shanghai",
            "bridge_url": "http://shanghai:5005",
            "bridge_token": "sh-token",
        },
        {
            "site_id": "overseas-1",
            "chat_id": "oc_overseas",
            "bridge_url": "https://overseas.example/bridge",
            "bridge_token": "os-token",
        },
    ]))
    monkeypatch.setattr(client, "GATEWAY_MODE", "hub")
    monkeypatch.setattr(client, "SITE_ROUTES", routes)
    monkeypatch.setattr(client, "DEFAULT_SITE_ID", "shanghai")

    assert client._route_for_message(_message("帮助", chat_id="oc_overseas"))["site_id"] == "overseas-1"
    assert client._route_for_message(_message("帮助", chat_id="oc_unknown")) is None
    assert client._route_for_message(_message("帮助", chat_type="p2p", mentions=False))["site_id"] == "shanghai"


def test_hub_resolves_exact_group_name_without_operator_chat_id(monkeypatch):
    routes = client.parse_site_routes(json.dumps([{
        "site_id": "英雄电竞上海站",
        "chat_id": "英雄电竞上海站告警群",
        "bridge_url": "http://event-monitor:5005",
        "bridge_token": "site-secret",
    }], ensure_ascii=False))
    lookups = []
    monkeypatch.setattr(client, "GATEWAY_MODE", "hub")
    monkeypatch.setattr(client, "SITE_ROUTES", routes)
    monkeypatch.setattr(
        client,
        "_chat_name_for_id",
        lambda chat_id: lookups.append(chat_id) or "英雄电竞上海站告警群",
    )

    assert client._route_for_message(_message("帮助", chat_id="oc_runtime"))["site_id"] == "英雄电竞上海站"
    assert client._route_for_message(_message("帮助", chat_id="oc_runtime"))["site_id"] == "英雄电竞上海站"
    assert lookups == ["oc_runtime"]


def test_bridge_requests_carry_the_per_site_bearer_token(monkeypatch):
    route = {
        "site_id": "overseas-1",
        "chat_id": "oc_overseas",
        "bridge_url": "https://overseas.example/bridge",
        "bridge_token": "site-secret",
    }
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self):
            return b'{"ok":true,"text":"ok"}'

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["auth"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    assert client.query_via_bridge("帮助", route)["ok"] is True
    assert captured["url"] == "https://overseas.example/bridge/bot/query"
    assert captured["auth"] == "Bearer site-secret"
