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
