import copy
import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from feishu_bridge.inspection_pagination import (
    InspectionCapacityError,
    InspectionPaginationError,
    InspectionSessionStore,
)


_BRIDGE_SPEC = importlib.util.spec_from_file_location(
    "feishu_bridge_inspection_test_bridge",
    Path(__file__).resolve().parent.parent / "alertmanager-feishu-bridge.py",
)
bridge = importlib.util.module_from_spec(_BRIDGE_SPEC)
assert _BRIDGE_SPEC.loader
_BRIDGE_SPEC.loader.exec_module(bridge)


class Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value


def snapshot(count):
    return {
        "title": "【PGS】 网络巡检",
        "subtitle": "Network Inspection",
        "template": "green",
        "summary_lines": ["巡检时间：2026-09-18 12:00:00", "设备合计：12 台"],
        "items": [
            {"kind": "device", "markdown": f"检查项 {index}"}
            for index in range(1, count + 1)
        ],
    }


def body(card):
    return card["card"]["body"]["elements"]


def callback_values(card):
    values = []
    for element in body(card):
        for behavior in element.get("behaviors") or []:
            values.append(behavior["value"])
    return values


def create_bound(store, count=13, *, app="cli_app", chat="oc_group", source="om_source", card="om_card"):
    session_id, first = store.create(
        snapshot(count), app_id=app, chat_id=chat, source_message_id=source,
    )
    store.bind_card(
        session_id, app_id=app, chat_id=chat,
        source_message_id=source, card_message_id=card,
    )
    return session_id, first


def test_single_page_has_all_items_and_no_pagination_controls():
    store = InspectionSessionStore()
    session_id, card = store.create(
        snapshot(6), app_id="cli_app", chat_id="oc_group", source_message_id="om_source",
    )

    rendered = json.dumps(card, ensure_ascii=False)
    assert session_id
    for index in range(1, 7):
        assert f"检查项 {index}" in rendered
    assert "第 **" not in rendered
    assert callback_values(card) == []


def test_two_pages_use_absolute_next_and_previous_actions():
    store = InspectionSessionStore()
    session_id, first = create_bound(store, count=7)
    assert callback_values(first) == [{
        "action": "inspection_page", "session_id": session_id, "page": 2,
    }]

    second = store.page(
        session_id, 2, app_id="cli_app", chat_id="oc_group", card_message_id="om_card",
    )
    rendered = json.dumps(second, ensure_ascii=False)
    assert "检查项 7" in rendered
    assert "检查项 6" not in rendered
    assert "第 **2 / 2** 页" in rendered
    assert callback_values(second) == [{
        "action": "inspection_page", "session_id": session_id, "page": 1,
    }]


def test_three_pages_middle_page_has_both_controls():
    store = InspectionSessionStore()
    session_id, _first = create_bound(store, count=14)
    middle = store.page(
        session_id, 2, app_id="cli_app", chat_id="oc_group", card_message_id="om_card",
    )
    assert callback_values(middle) == [
        {"action": "inspection_page", "session_id": session_id, "page": 1},
        {"action": "inspection_page", "session_id": session_id, "page": 3},
    ]


@pytest.mark.parametrize("page", [None, True, False, 0, -1, 1.0, 2.5, "", "2", "bad"])
def test_page_rejects_non_integer_and_out_of_range_values(page):
    store = InspectionSessionStore()
    session_id, _first = create_bound(store)
    with pytest.raises(InspectionPaginationError) as caught:
        store.page(
            session_id, page,
            app_id="cli_app", chat_id="oc_group", card_message_id="om_card",
        )
    assert caught.value.code == "invalid_page"


def test_page_rejects_page_past_server_side_page_count():
    store = InspectionSessionStore()
    session_id, _first = create_bound(store, count=7)
    with pytest.raises(InspectionPaginationError) as caught:
        store.page(
            session_id, 3,
            app_id="cli_app", chat_id="oc_group", card_message_id="om_card",
        )
    assert caught.value.code == "invalid_page"


def test_expired_and_unknown_sessions_have_distinct_safe_messages():
    clock = Clock()
    store = InspectionSessionStore(ttl_seconds=20, clock=clock)
    session_id, _first = create_bound(store)
    clock.value += 21

    with pytest.raises(InspectionPaginationError) as expired:
        store.page(
            session_id, 1,
            app_id="cli_app", chat_id="oc_group", card_message_id="om_card",
        )
    assert expired.value.code == "expired"
    assert expired.value.message == "本次巡检结果已过期，请重新执行巡检。"

    with pytest.raises(InspectionPaginationError) as unknown:
        store.page(
            "missing", 1,
            app_id="cli_app", chat_id="oc_group", card_message_id="om_card",
        )
    assert unknown.value.code == "unknown_session"
    assert "结果不可用" in unknown.value.message
    assert "已过期" not in unknown.value.message


@pytest.mark.parametrize("app,chat,card", [
    ("wrong", "oc_group", "om_card"),
    ("cli_app", "oc_other", "om_card"),
    ("cli_app", "oc_group", "om_other"),
    ("", "oc_group", "om_card"),
    ("cli_app", "", "om_card"),
    ("cli_app", "oc_group", ""),
])
def test_page_fails_closed_for_wrong_or_missing_trusted_context(app, chat, card):
    store = InspectionSessionStore()
    session_id, _first = create_bound(store)
    with pytest.raises(InspectionPaginationError):
        store.page(session_id, 1, app_id=app, chat_id=chat, card_message_id=card)


def test_sessions_are_independent_and_snapshot_is_immutable():
    store = InspectionSessionStore()
    original_a = snapshot(7)
    session_a, _ = store.create(
        original_a, app_id="cli_app", chat_id="oc_a", source_message_id="om_a",
    )
    original_a["items"][0]["markdown"] = "MUTATED"
    store.bind_card(
        session_a, app_id="cli_app", chat_id="oc_a",
        source_message_id="om_a", card_message_id="card_a",
    )
    session_b, _ = create_bound(
        store, count=13, chat="oc_b", source="om_b", card="card_b",
    )

    page_a = store.page(
        session_a, 1, app_id="cli_app", chat_id="oc_a", card_message_id="card_a",
    )
    page_b = store.page(
        session_b, 3, app_id="cli_app", chat_id="oc_b", card_message_id="card_b",
    )
    assert "MUTATED" not in json.dumps(page_a, ensure_ascii=False)
    assert "检查项 1" in json.dumps(page_a, ensure_ascii=False)
    assert "检查项 13" in json.dumps(page_b, ensure_ascii=False)


def test_repeated_and_multi_user_clicks_are_idempotent_without_current_page_state():
    store = InspectionSessionStore()
    session_id, _first = create_bound(store)
    kwargs = {"app_id": "cli_app", "chat_id": "oc_group", "card_message_id": "om_card"}
    first = store.page(session_id, 2, **kwargs)
    second = store.page(session_id, 2, **kwargs)
    assert first == second
    assert store.page(session_id, 1, **kwargs) == store.page(session_id, 1, **kwargs)


def test_capacity_never_evicts_a_live_session():
    store = InspectionSessionStore(max_sessions=1)
    session_id, _first = create_bound(store, count=1)
    with pytest.raises(InspectionCapacityError) as caught:
        store.create(
            snapshot(1), app_id="cli_app", chat_id="oc_other", source_message_id="om_other",
        )
    assert caught.value.code == "session_capacity"
    assert store.stats()["sessions"] == 1
    assert store.page(
        session_id, 1,
        app_id="cli_app", chat_id="oc_group", card_message_id="om_card",
    )


def test_snapshot_and_total_byte_limits_reject_without_truncation():
    large = snapshot(1)
    large["items"][0]["markdown"] = "x" * 2000
    with pytest.raises(InspectionCapacityError) as single:
        InspectionSessionStore(max_snapshot_bytes=1000).create(
            large, app_id="cli_app", chat_id="oc_group", source_message_id="om_source",
        )
    assert single.value.code == "snapshot_too_large"

    store = InspectionSessionStore(max_total_bytes=2500)
    store.create(snapshot(6), app_id="cli_app", chat_id="oc_a", source_message_id="om_a")
    with pytest.raises(InspectionCapacityError) as total:
        store.create(large, app_id="cli_app", chat_id="oc_b", source_message_id="om_b")
    assert total.value.code == "total_capacity"
    assert store.stats()["sessions"] == 1


def test_restart_loses_sessions_and_reports_unknown_not_expired():
    first_store = InspectionSessionStore()
    session_id, _first = create_bound(first_store)
    restarted = InspectionSessionStore()
    with pytest.raises(InspectionPaginationError) as caught:
        restarted.page(
            session_id, 1,
            app_id="cli_app", chat_id="oc_group", card_message_id="om_card",
        )
    assert caught.value.code == "unknown_session"


def test_concurrent_absolute_page_reads_are_safe_and_independent():
    store = InspectionSessionStore()
    session_id, _first = create_bound(store, count=19)
    kwargs = {"app_id": "cli_app", "chat_id": "oc_group", "card_message_id": "om_card"}
    pages = [1, 2, 3, 4] * 8
    with ThreadPoolExecutor(max_workers=8) as pool:
        cards = list(pool.map(lambda page: store.page(session_id, page, **kwargs), pages))
    for requested, card in zip(pages, cards):
        assert f"第 **{requested} / 4** 页" in json.dumps(card, ensure_ascii=False)


def test_bridge_collects_once_then_pagination_path_is_strictly_read_only(monkeypatch):
    calls = {"librenms": 0, "prometheus": 0, "stackwise": 0}
    devices = [
        {"hostname": f"switch-{index}", "ip": f"192.0.2.{index}", "status": 1, "disabled": 0}
        for index in range(1, 8)
    ]

    def fetch_devices(_token):
        calls["librenms"] += 1
        return copy.deepcopy(devices)

    def fetch_reachability():
        calls["prometheus"] += 1
        return {}

    def collect_stackwise(_devices):
        calls["stackwise"] += 1
        return []

    monkeypatch.setattr(bridge, "INSPECTION_PAGINATION_ENABLED", True)
    monkeypatch.setattr(bridge, "FEISHU_APP_ID", "cli_app")
    monkeypatch.setattr(bridge, "LIBRENMS_URL", "http://librenms:8000")
    monkeypatch.setattr(bridge, "_librenms_token", lambda: "token")
    monkeypatch.setattr(bridge, "fetch_librenms_devices", fetch_devices)
    monkeypatch.setattr(bridge, "fetch_network_reachability", fetch_reachability)
    monkeypatch.setattr(bridge, "collect_cisco_stackwise_audit", collect_stackwise)
    monkeypatch.setattr(bridge, "INSPECTION_SESSIONS", InspectionSessionStore())

    result = bridge.handle_bot_query("网络巡检", {
        "app_id": "cli_app",
        "chat_id": "oc_group",
        "source_message_id": "om_source",
    })
    assert result["ok"] is True
    assert len(result["cards"]) == 1
    assert calls == {"librenms": 1, "prometheus": 1, "stackwise": 1}

    session_id = result["inspection_session"]["session_id"]
    assert bridge.bind_inspection_session({
        "session_id": session_id,
        "app_id": "cli_app",
        "chat_id": "oc_group",
        "source_message_id": "om_source",
        "card_message_id": "om_card",
    }) == {"ok": True}

    def forbidden(*_args, **_kwargs):
        raise AssertionError("pagination performed forbidden collection or mutation")

    for name in (
        "fetch_librenms_devices",
        "fetch_network_reachability",
        "prometheus_query",
        "collect_cisco_stackwise_audit",
        "_save_json_dict",
        "resolve_pending_delete",
        "delete_librenms_device",
        "delete_librenms_device_record",
    ):
        monkeypatch.setattr(bridge, name, forbidden)

    page = bridge.resolve_inspection_page({
        "session_id": session_id,
        "page": 2,
        "app_id": "cli_app",
        "chat_id": "oc_group",
        "card_message_id": "om_card",
    })
    assert page["ok"] is True
    assert "switch-7" in json.dumps(page["card"], ensure_ascii=False)
    assert calls == {"librenms": 1, "prometheus": 1, "stackwise": 1}
