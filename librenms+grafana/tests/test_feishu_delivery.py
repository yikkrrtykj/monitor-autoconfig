import io
import json
import threading
from urllib import error

import pytest

import feishu_delivery
from feishu_delivery import FeishuDelivery


class FakeResponse:
    def __init__(self, payload=None, raw=None):
        self.raw = raw if raw is not None else json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self.raw


def card(title="test"):
    return {"card": {"header": {"title": {"content": title}}}}


def health_state():
    return {
        "lastSuccessAt": None,
        "lastFailureAt": None,
        "lastError": "",
        "lastChannel": "",
        "appChatResolved": False,
        "lastAppError": "",
    }


def delivery(**overrides):
    health = overrides.pop("health_state", health_state())
    logs = overrides.pop("logs", [])
    options = {
        "webhook_token": "webhook-token",
        "dry_run": False,
        "send_max_attempts": 3,
        "retry_base_seconds": 1,
        "app_id": "",
        "app_secret": "",
        "chat_id": "",
        "event_name": "",
        "log": logs.append,
        "health_state": health,
        "health_lock": threading.Lock(),
        "time_fn": lambda: 1000.0,
    }
    options.update(overrides)
    return FeishuDelivery(**options), health, logs


@pytest.mark.parametrize("field", ["code", "StatusCode", "status_code"])
def test_webhook_response_accepts_all_zero_business_code_fields(field):
    assert feishu_delivery._feishu_response_result(json.dumps({field: 0})) == (True, "code=0")


@pytest.mark.parametrize(
    ("response", "detail"),
    [
        ("not-json", "response is not valid JSON"),
        ("[]", "response JSON is not an object"),
        ("{}", "response has no recognizable business code"),
        ('{"code":"bad"}', "response has no recognizable business code"),
    ],
)
def test_webhook_response_rejects_invalid_or_unrecognized_payloads(response, detail):
    assert feishu_delivery._feishu_response_result(response) == (False, detail)


def test_webhook_success_uses_exact_endpoint_timeout_payload_and_health():
    calls = []

    def urlopen(req, timeout):
        calls.append((req, timeout))
        return FakeResponse({"code": 0, "msg": "success"})

    subject, health, _logs = delivery(urlopen=urlopen)
    outgoing = card()

    assert subject.send_webhook(outgoing) is True
    assert len(calls) == 1
    req, timeout = calls[0]
    assert req.full_url == "https://open.feishu.cn/open-apis/bot/v2/hook/webhook-token"
    assert timeout == 5
    assert json.loads(req.data.decode("utf-8")) == outgoing
    assert health["lastChannel"] == "webhook"
    assert health["lastSuccessAt"] == 1000
    assert health["lastError"] == ""


def test_webhook_business_error_retries_with_exact_exponential_backoff():
    responses = iter([
        FakeResponse({"code": 19002, "msg": "invalid token"}),
        FakeResponse({"code": 19003, "msg": "busy"}),
        FakeResponse({"code": 0, "msg": "success"}),
    ])
    calls = []
    sleeps = []

    def urlopen(req, timeout):
        calls.append((req, timeout))
        return next(responses)

    subject, health, _logs = delivery(urlopen=urlopen, sleep=sleeps.append, retry_base_seconds=0.5)

    assert subject.send_webhook(card()) is True
    assert len(calls) == 3
    assert sleeps == [0.5, 1.0]
    assert health["lastChannel"] == "webhook"


def test_webhook_url_error_retries_exact_count_and_records_final_failure():
    calls = []
    sleeps = []

    def urlopen(req, timeout):
        calls.append((req, timeout))
        raise error.URLError("offline")

    subject, health, _logs = delivery(urlopen=urlopen, sleep=sleeps.append)

    assert subject.send_webhook(card()) is False
    assert len(calls) == 3
    assert sleeps == [1, 2]
    assert health["lastChannel"] == "webhook"
    assert health["lastFailureAt"] == 1000
    assert "offline" in health["lastError"]


def test_direct_webhook_dry_run_skips_http_and_marks_success():
    subject, health, logs = delivery(
        webhook_token="",
        dry_run=True,
        urlopen=lambda *_args, **_kwargs: pytest.fail("HTTP must not run"),
    )

    assert subject.send_webhook(card()) is True
    assert health["lastChannel"] == "dry-run"
    assert health["lastSuccessAt"] == 1000
    assert logs == ["[DRY] would POST card: test"]


def test_empty_webhook_token_fails_without_http():
    subject, health, _logs = delivery(
        webhook_token="",
        urlopen=lambda *_args, **_kwargs: pytest.fail("HTTP must not run"),
    )

    assert subject.send_webhook(card()) is False
    assert health["lastChannel"] == "webhook"
    assert health["lastError"] == "FEISHU_ROBOT_TOKEN is empty"


@pytest.mark.parametrize(
    ("app_id", "app_secret", "expected"),
    [("id", "secret", True), ("", "secret", False), ("id", "", False)],
)
def test_app_configured_requires_both_credentials(app_id, app_secret, expected):
    subject, _health, _logs = delivery(app_id=app_id, app_secret=app_secret)
    assert subject.app_configured() is expected


def test_direct_app_dry_run_precedes_configuration_check():
    subject, health, logs = delivery(
        app_id="",
        app_secret="",
        dry_run=True,
        urlopen=lambda *_args, **_kwargs: pytest.fail("HTTP must not run"),
    )

    assert subject.send_app(card()) is True
    assert health == health_state()
    assert logs == ["[DRY][APP] would send interactive card: test"]


def test_tenant_token_cache_hit_does_not_refresh():
    subject, _health, _logs = delivery(app_id="id", app_secret="secret", time_fn=lambda: 100.0)
    subject._app_token.update({"token": "cached", "expires_at": 221.0})
    subject._api_post = lambda *_args, **_kwargs: pytest.fail("cache should be reused")

    assert subject._tenant_token() == "cached"


def test_tenant_token_refreshes_inside_120_second_margin():
    calls = []
    subject, _health, _logs = delivery(app_id="id", app_secret="secret", time_fn=lambda: 100.0)
    subject._app_token.update({"token": "old", "expires_at": 220.0})

    def api_post(path, payload, token=""):
        calls.append((path, payload, token))
        return {"code": 0, "tenant_access_token": "new", "expire": 3600}

    subject._api_post = api_post

    assert subject._tenant_token() == "new"
    assert calls == [(
        "/open-apis/auth/v3/tenant_access_token/internal",
        {"app_id": "id", "app_secret": "secret"},
        "",
    )]
    assert subject._app_token == {"token": "new", "expires_at": 3700.0}


@pytest.mark.parametrize(
    "result",
    [
        {"code": 1, "msg": "rejected"},
        {"code": 0, "msg": "missing token"},
    ],
)
def test_tenant_token_business_failure_records_app_error(result):
    subject, health, _logs = delivery(app_id="id", app_secret="secret")
    subject._api_post = lambda *_args, **_kwargs: result

    assert subject._tenant_token() == ""
    assert "code=" in health["lastAppError"]


def test_tenant_token_request_failure_records_app_error():
    subject, health, _logs = delivery(app_id="id", app_secret="secret")
    subject._api_post = lambda *_args, **_kwargs: (_ for _ in ()).throw(error.URLError("offline"))

    assert subject._tenant_token() == ""
    assert "offline" in health["lastAppError"]


def test_http_error_detail_includes_business_code_and_permission_hint():
    body = io.BytesIO(json.dumps({
        "code": 99991672,
        "msg": "no permission to read chat",
    }).encode("utf-8"))
    exc = error.HTTPError("https://open.feishu.cn", 400, "Bad Request", {}, body)

    detail = feishu_delivery._feishu_http_error_detail(exc)

    assert "HTTP 400" in detail
    assert "code=99991672" in detail
    assert "im:chat:read" in detail


def test_direct_chat_id_skips_lookup_and_marks_resolution():
    subject, health, _logs = delivery(chat_id="oc_direct")

    assert subject._app_chat_id("token") == "oc_direct"
    assert health["appChatResolved"] is True
    assert health["lastAppError"] == ""


def test_configured_group_name_exact_match_and_cached_reuse():
    calls = []

    def urlopen(req, timeout):
        calls.append((req, timeout))
        return FakeResponse({
            "code": 0,
            "data": {"items": [
                {"chat_id": "oc_other", "name": "公司告警群"},
                {"chat_id": "oc_event", "name": "比赛告警群"},
            ]},
        })

    subject, health, _logs = delivery(chat_id="比赛告警群", urlopen=urlopen)

    assert subject._app_chat_id("token") == "oc_event"
    assert subject._app_chat_id("different-token") == "oc_event"
    assert len(calls) == 1
    assert health["appChatResolved"] is True


def test_event_name_is_only_a_chat_name_fallback():
    subject, _health, _logs = delivery(
        event_name="EWC 上海站",
        urlopen=lambda _req, timeout: FakeResponse({
            "code": 0,
            "data": {"items": [{"chat_id": "oc_event", "name": "EWC 上海站"}]},
        }),
    )

    assert subject._app_chat_id("token") == "oc_event"


def test_only_bot_group_is_selected_without_configured_name():
    subject, _health, _logs = delivery(
        urlopen=lambda _req, timeout: FakeResponse({
            "code": 0,
            "data": {"items": [{"chat_id": "oc_only", "name": "任意群"}]},
        }),
    )

    assert subject._app_chat_id("token") == "oc_only"


@pytest.mark.parametrize(
    ("items", "expected_error"),
    [
        ([], "机器人不在任何群"),
        (
            [
                {"chat_id": "oc_a", "name": "A"},
                {"chat_id": "oc_b", "name": "B"},
            ],
            "机器人属于多个群",
        ),
    ],
)
def test_no_or_ambiguous_groups_fail_without_guessing(items, expected_error):
    subject, health, _logs = delivery(
        urlopen=lambda _req, timeout: FakeResponse({"code": 0, "data": {"items": items}}),
    )

    assert subject._app_chat_id("token") == ""
    assert health["appChatResolved"] is False
    assert expected_error in health["lastAppError"]


def test_missing_configured_group_fails_even_when_one_other_group_exists():
    subject, health, _logs = delivery(
        chat_id="目标群",
        urlopen=lambda _req, timeout: FakeResponse({
            "code": 0,
            "data": {"items": [{"chat_id": "oc_other", "name": "其他群"}]},
        }),
    )

    assert subject._app_chat_id("token") == ""
    assert "目标群" in health["lastAppError"]


def test_chat_lookup_reads_all_pages():
    responses = iter([
        FakeResponse({
            "code": 0,
            "data": {
                "items": [{"chat_id": "oc_other", "name": "公司告警群"}],
                "has_more": True,
                "page_token": "next-page",
            },
        }),
        FakeResponse({
            "code": 0,
            "data": {"items": [{"chat_id": "oc_event", "name": "比赛告警群"}]},
        }),
    ])
    urls = []

    def urlopen(req, timeout):
        urls.append((req.full_url, timeout))
        return next(responses)

    subject, _health, _logs = delivery(chat_id="比赛告警群", urlopen=urlopen)

    assert subject._app_chat_id("token") == "oc_event"
    assert len(urls) == 2
    assert "page_token=next-page" in urls[1][0]
    assert all(timeout == 8 for _url, timeout in urls)


def test_chat_lookup_http_failure_updates_resolution_health():
    body = io.BytesIO(json.dumps({
        "code": 99991672,
        "msg": "no permission to read chat",
    }).encode("utf-8"))

    def urlopen(_req, timeout):
        del timeout
        raise error.HTTPError("https://open.feishu.cn", 400, "Bad Request", {}, body)

    subject, health, _logs = delivery(chat_id="比赛告警群", urlopen=urlopen)

    assert subject._app_chat_id("token") == ""
    assert health["appChatResolved"] is False
    assert "code=99991672" in health["lastAppError"]


def test_app_message_success_uses_exact_endpoint_payload_and_token():
    calls = []
    subject, health, _logs = delivery(app_id="id", app_secret="secret")
    subject._tenant_token = lambda: "tenant-token"
    subject._app_chat_id = lambda _token: "oc_chat"

    def api_post(path, payload, token=""):
        calls.append((path, payload, token))
        return {"code": 0}

    subject._api_post = api_post
    outgoing = card()

    assert subject.send_app(outgoing) is True
    assert calls == [(
        "/open-apis/im/v1/messages?receive_id_type=chat_id",
        {
            "receive_id": "oc_chat",
            "msg_type": "interactive",
            "content": json.dumps(outgoing["card"], ensure_ascii=False),
        },
        "tenant-token",
    )]
    assert health["appChatResolved"] is True


def test_app_message_business_failure_preserves_resolved_chat_state():
    subject, health, _logs = delivery(app_id="id", app_secret="secret", chat_id="oc_direct")
    subject._tenant_token = lambda: "tenant-token"
    subject._api_post = lambda *_args, **_kwargs: {"code": 230002, "msg": "message rejected"}

    assert subject.send_app(card()) is False
    assert health["appChatResolved"] is True
    assert "code=230002" in health["lastAppError"]


def test_app_message_http_failure_returns_false_and_records_detail():
    body = io.BytesIO(json.dumps({"code": 230002, "msg": "not in chat"}).encode("utf-8"))
    subject, health, _logs = delivery(app_id="id", app_secret="secret")
    subject._tenant_token = lambda: "tenant-token"
    subject._app_chat_id = lambda _token: "oc_chat"
    subject._api_post = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        error.HTTPError("https://open.feishu.cn", 400, "Bad Request", {}, body)
    )

    assert subject.send_app(card()) is False
    assert "code=230002" in health["lastAppError"]


def test_app_success_never_calls_webhook_and_marks_app_channel():
    subject, health, _logs = delivery(app_id="id", app_secret="secret")
    subject.send_app = lambda _card: True
    subject.send_webhook = lambda _card: pytest.fail("webhook should not run")

    assert subject.send(card()) is True
    assert health["lastChannel"] == "app"


def test_app_failure_falls_back_once_and_retains_app_error_on_webhook_success():
    calls = []
    subject, health, _logs = delivery(app_id="id", app_secret="secret")

    def app_send(_card):
        subject._mark_app_error("app failed")
        return False

    subject.send_app = app_send

    def webhook_send(_card):
        calls.append("webhook")
        subject._mark_delivery_health(True, channel="webhook")
        return True

    subject.send_webhook = webhook_send

    assert subject.send(card()) is True
    assert calls == ["webhook"]
    assert health["lastChannel"] == "webhook"
    assert health["lastAppError"] == "app failed"


def test_unconfigured_app_goes_directly_to_webhook():
    calls = []
    subject, _health, _logs = delivery(app_id="", app_secret="")
    subject.send_app = lambda _card: pytest.fail("app should not run")
    subject.send_webhook = lambda _card: calls.append("webhook") or True

    assert subject.send(card()) is True
    assert calls == ["webhook"]


def test_app_and_webhook_failure_returns_false():
    subject, health, _logs = delivery(app_id="id", app_secret="secret")
    subject.send_app = lambda _card: False
    subject.send_webhook = lambda _card: False

    assert subject.send(card()) is False
    assert health["lastAppError"] == "应用卡片发送失败"


def test_delivery_does_not_create_event_id_or_decorate_card_title():
    outgoing = card("原始标题")
    captured = []
    subject, _health, _logs = delivery(
        event_name="EWC 上海站",
        urlopen=lambda req, timeout: captured.append((req, timeout)) or FakeResponse({"code": 0}),
    )

    assert subject.send_webhook(outgoing) is True
    assert outgoing["card"]["header"]["title"]["content"] == "原始标题"
    assert json.loads(captured[0][0].data.decode("utf-8"))["card"]["header"]["title"]["content"] == "原始标题"
    assert not hasattr(feishu_delivery, "EVENT_ID")
