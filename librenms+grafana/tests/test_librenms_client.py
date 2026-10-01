import json
import socket
from datetime import datetime, timezone
from urllib import error as urlerror
from urllib import parse as urlparse

import pytest

from librenms_client import (
    LibreNMSAPIError,
    LibreNMSClient,
    LibreNMSInvalidResponse,
    LibreNMSUnavailable,
    age_seconds,
    is_fresh,
    parse_librenms_timestamp,
)


class FakeResponse:
    def __init__(self, payload=None, *, raw=None, status=200):
        self.status = status
        self.raw = raw if raw is not None else json.dumps(payload or {}).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def getcode(self):
        return self.status

    def read(self, limit=-1):
        return self.raw if limit < 0 else self.raw[:limit]


@pytest.fixture(autouse=True)
def clean_librenms_env(monkeypatch):
    for key in (
        "LIBRENMS_URL",
        "LIBRENMS_API_TOKEN",
        "LIBRENMS_TOKEN_FILE",
        "LIBRENMS_API_TIMEOUT",
        "LIBRENMS_API_ATTEMPTS",
        "LIBRENMS_API_RETRY_DELAY",
    ):
        monkeypatch.delenv(key, raising=False)


def make_client(*, token="test-token", max_attempts=3):
    client = LibreNMSClient(base_url="http://librenms:8000/", token=token, timeout=2, max_attempts=max_attempts)
    client.retry_delay = 0
    client._sleep = lambda _seconds: None
    return client


def attach_sequence(client, outcomes):
    calls = []
    queue = list(outcomes)

    def opener(request, timeout):
        calls.append({
            "url": request.full_url,
            "headers": {key.lower(): value for key, value in request.header_items()},
            "timeout": timeout,
        })
        outcome = queue.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    client._opener = opener
    return calls


def http_error(status):
    return urlerror.HTTPError("http://librenms.invalid", status, "failure", None, None)


def test_explicit_token_has_priority_over_file_and_environment(tmp_path, monkeypatch):
    token_file = tmp_path / "token"
    token_file.write_text("file-token\n", encoding="utf-8")
    monkeypatch.setenv("LIBRENMS_API_TOKEN", "env-token")

    client = LibreNMSClient(token="explicit-token", token_file=token_file)

    assert client.token == "explicit-token"


def test_token_file_has_priority_over_environment(tmp_path, monkeypatch):
    token_file = tmp_path / "token"
    token_file.write_text("file-token\n", encoding="utf-8")
    monkeypatch.setenv("LIBRENMS_API_TOKEN", "env-token")

    client = LibreNMSClient(token_file=token_file)

    assert client.token == "file-token"


def test_environment_token_is_fallback_for_missing_file(tmp_path, monkeypatch):
    monkeypatch.setenv("LIBRENMS_API_TOKEN", "env-token")

    client = LibreNMSClient(token_file=tmp_path / "missing")

    assert client.token == "env-token"


def test_timeout_defaults_from_environment(monkeypatch):
    monkeypatch.setenv("LIBRENMS_API_TIMEOUT", "7.5")

    assert LibreNMSClient(token="token").timeout == 7.5


def test_token_and_other_secrets_never_enter_exception_text():
    client = make_client(token="api-token-123", max_attempts=1)
    attach_sequence(
        client,
        [urlerror.URLError("password=secret community=global token=api-token-123")],
    )

    with pytest.raises(LibreNMSUnavailable) as failure:
        client.get_json("/api/v0/devices")

    message = str(failure.value)
    for secret in ("api-token-123", "password", "secret", "community", "global"):
        assert secret not in message


def test_url_join_header_and_timeout_are_consistent():
    client = make_client()
    calls = attach_sequence(client, [FakeResponse({"status": "ok", "devices": []})])

    client.get_json("/api/v0/devices")

    assert calls == [{
        "url": "http://librenms:8000/api/v0/devices",
        "headers": {"accept": "application/json", "x-auth-token": "test-token"},
        "timeout": 2.0,
    }]


def test_get_device_reads_only_requested_identity_and_handles_missing():
    client = make_client(max_attempts=1)
    calls = attach_sequence(client, [FakeResponse({"status": "ok", "devices": [
        {"device_id": 7, "hostname": "192.0.2.7", "ip": "192.0.2.7", "os": "ios"}
    ]}), http_error(404)])
    assert client.get_device("192.0.2.7")["device_id"] == 7
    assert client.get_device("192.0.2.8") is None
    assert [call["url"] for call in calls] == [
        "http://librenms:8000/api/v0/devices/192.0.2.7",
        "http://librenms:8000/api/v0/devices/192.0.2.8",
    ]


def test_invalid_base_url_fails_without_echoing_its_contents():
    client = LibreNMSClient(base_url="not-a-url-with-password=secret", token="token")

    with pytest.raises(LibreNMSUnavailable) as failure:
        client.get_json("/api/v0/devices")

    assert "secret" not in str(failure.value)


def test_query_parameters_are_urlencoded_with_doseq():
    client = make_client()
    calls = attach_sequence(client, [FakeResponse({"status": "ok"})])

    client.get_json("/api/v0/devices?existing=yes", {
        "columns": "ifName,ifAlias",
        "filter": ["wan one", "eth/2"],
    })

    parsed = urlparse.urlsplit(calls[0]["url"])
    assert parsed.path == "/api/v0/devices"
    assert urlparse.parse_qs(parsed.query) == {
        "existing": ["yes"],
        "columns": ["ifName,ifAlias"],
        "filter": ["wan one", "eth/2"],
    }


def test_http_200_json_response_is_returned():
    client = make_client()
    attach_sequence(client, [FakeResponse({"status": "ok", "count": "0"})])

    assert client.get_json("/api/v0/devices") == {"status": "ok", "count": "0"}


def test_api_status_failure_is_a_safe_uniform_exception():
    client = make_client(token="never-log-this")
    attach_sequence(client, [FakeResponse({
        "status": "error",
        "message": "token never-log-this was rejected",
    })])

    with pytest.raises(LibreNMSAPIError) as failure:
        client.get_json("/api/v0/devices")

    assert "never-log-this" not in str(failure.value)


@pytest.mark.parametrize("raw", [b"not-json", b"[1, 2, 3]"])
def test_invalid_or_non_object_json_has_a_clear_exception(raw):
    client = make_client()
    attach_sequence(client, [FakeResponse(raw=raw)])

    with pytest.raises(LibreNMSInvalidResponse):
        client.get_json("/api/v0/devices")


@pytest.mark.parametrize("failure", [socket.timeout("slow"), TimeoutError("slow")])
def test_timeout_is_bounded_and_reported_unavailable(failure):
    client = make_client(max_attempts=1)
    calls = attach_sequence(client, [failure])

    with pytest.raises(LibreNMSUnavailable):
        client.get_json("/api/v0/devices")

    assert len(calls) == 1


@pytest.mark.parametrize(
    "failure",
    [urlerror.URLError(ConnectionRefusedError("refused")), ConnectionResetError("reset")],
)
def test_connection_failures_are_bounded_and_reported_unavailable(failure):
    client = make_client(max_attempts=1)
    calls = attach_sequence(client, [failure])

    with pytest.raises(LibreNMSUnavailable):
        client.get_json("/api/v0/devices")

    assert len(calls) == 1


@pytest.mark.parametrize("status", [502, 503, 504])
def test_transient_gateway_statuses_retry_then_succeed(status):
    client = make_client(max_attempts=3)
    calls = attach_sequence(client, [
        http_error(status),
        http_error(status),
        FakeResponse({"status": "ok", "devices": []}),
    ])

    assert client.get_json("/api/v0/devices")["status"] == "ok"
    assert len(calls) == 3


@pytest.mark.parametrize("status", [401, 403, 404])
def test_auth_and_not_found_statuses_do_not_retry(status):
    client = make_client(max_attempts=3)
    calls = attach_sequence(client, [http_error(status)])

    with pytest.raises(LibreNMSAPIError) as failure:
        client.get_json("/api/v0/devices")

    assert failure.value.status_code == status
    assert len(calls) == 1


def test_retry_budget_is_finite():
    client = make_client(max_attempts=3)
    calls = attach_sequence(client, [socket.timeout("slow")] * 3)

    with pytest.raises(LibreNMSUnavailable):
        client.get_json("/api/v0/devices")

    assert len(calls) == 3
    assert client.request_count == 3


def test_list_devices_normalizes_required_identity_fields():
    client = make_client()
    attach_sequence(client, [FakeResponse({
        "status": "ok",
        "devices": [{"device_id": "7", "hostname": "192.0.2.7", "sysName": "edge-7"}],
    })])

    assert client.list_devices() == [{
        "device_id": "7",
        "hostname": "192.0.2.7",
        "sysName": "edge-7",
        "ip": "192.0.2.7",
    }]


@pytest.mark.parametrize("payload", [
    {"status": "ok"},
    {"status": "ok", "devices": None},
    {"status": "ok", "devices": ["bad"]},
    {"status": "ok", "devices": [{}]},
])
def test_strict_device_inventory_rejects_missing_or_malformed_envelope(payload):
    client = make_client()
    attach_sequence(client, [FakeResponse(payload)])

    with pytest.raises(LibreNMSInvalidResponse):
        client.list_devices(strict=True)


def test_strict_device_inventory_accepts_legitimate_empty_list():
    client = make_client()
    attach_sequence(client, [FakeResponse({"status": "ok", "devices": []})])

    assert client.list_devices(strict=True) == []


@pytest.mark.parametrize("device", [
    {"device_id": 7},
    {"device_id": "7"},
    {"hostname": "switch-01"},
    {"ip": "192.0.2.10"},
    {"sysName": "Core-SW"},
    {"device_id": True, "hostname": "switch-02"},
])
def test_strict_device_inventory_accepts_typed_identity(device):
    client = make_client()
    attach_sequence(client, [FakeResponse({"status": "ok", "devices": [device]})])

    assert len(client.list_devices(strict=True)) == 1


@pytest.mark.parametrize("device", [
    {"device_id": {}},
    {"device_id": []},
    {"device_id": True},
    {"device_id": 7.5},
    {"device_id": "   "},
    {"hostname": {}},
    {"hostname": []},
    {"hostname": 123},
    {"hostname": "   "},
    {"ip": {}},
    {"ip": []},
    {"ip": 123},
    {"ip": "   "},
    {"sysName": {}},
    {"sysName": []},
    {"sysName": 123},
    {"sysName": "   "},
    {},
])
def test_strict_device_inventory_rejects_invalid_identity_types(device):
    client = make_client()
    attach_sequence(client, [FakeResponse({"status": "ok", "devices": [device]})])

    with pytest.raises(LibreNMSInvalidResponse):
        client.list_devices(strict=True)


def test_legacy_device_inventory_keeps_permissive_identity_normalization():
    client = make_client()
    attach_sequence(client, [FakeResponse({
        "status": "ok",
        "devices": [{"hostname": 123}],
    })])

    assert client.list_devices(strict=False) == [{
        "hostname": "123",
        "device_id": None,
        "ip": "",
        "sysName": "",
    }]


def test_optional_response_byte_limit_rejects_oversize_payload():
    client = make_client()
    client.max_response_bytes = 8
    attach_sequence(client, [FakeResponse(raw=b'{"status":"ok"}')])

    with pytest.raises(LibreNMSInvalidResponse):
        client.get_json("/api/v0/devices")


@pytest.mark.parametrize(
    ("identifier", "expected_id"),
    [(7, 7), ("edge.example", 7), ("192.0.2.7", 7), ("EDGE-SYSNAME", 7)],
)
def test_resolve_device_accepts_id_hostname_ip_and_sysname(identifier, expected_id):
    client = make_client()
    client._devices_cache = [{
        "device_id": 7,
        "hostname": "edge.example",
        "ip": "192.0.2.7",
        "sysName": "edge-sysname",
    }]

    assert client.resolve_device(identifier)["device_id"] == expected_id


def test_device_list_is_cached_within_one_client_cycle():
    client = make_client()
    calls = attach_sequence(client, [FakeResponse({
        "status": "ok",
        "devices": [{"device_id": 1, "hostname": "first"}],
    })])

    first = client.list_devices()
    first[0]["hostname"] = "mutated"
    second = client.list_devices()

    assert len(calls) == 1
    assert second[0]["hostname"] == "first"


def test_clear_cache_starts_a_new_device_list_cycle():
    client = make_client()
    calls = attach_sequence(client, [
        FakeResponse({"status": "ok", "devices": [{"device_id": 1, "hostname": "first"}]}),
        FakeResponse({"status": "ok", "devices": [{"device_id": 2, "hostname": "second"}]}),
    ])

    assert client.list_devices()[0]["device_id"] == 1
    client.clear_cache()
    assert client.list_devices()[0]["device_id"] == 2
    assert len(calls) == 2


def test_ports_normalization_preserves_fields_and_encodes_options():
    client = make_client()
    calls = attach_sequence(client, [FakeResponse({
        "status": "ok",
        "ports": {"one": {"port_id": "9", "ifIndex": 3, "ifName": "Gi0/1"}},
    })])

    ports = client.get_device_ports(
        {"device_id": 7},
        columns=["port_id", "ifIndex", "ifName"],
        with_vlans=True,
    )

    assert ports == [{"port_id": "9", "ifIndex": 3, "ifName": "Gi0/1"}]
    assert urlparse.parse_qs(urlparse.urlsplit(calls[0]["url"]).query) == {
        "columns": ["port_id,ifIndex,ifName"],
        "with": ["vlans"],
    }


def test_device_ip_address_normalization():
    client = make_client()
    attach_sequence(client, [FakeResponse({
        "status": "ok",
        "addresses": [{"ipv4_address": "192.0.2.7", "port_id": 9}],
    })])

    assert client.get_device_ip_addresses({"device_id": "7"}) == [
        {"ipv4_address": "192.0.2.7", "port_id": 9},
    ]


def test_fdb_single_object_is_normalized_to_a_list():
    client = make_client()
    attach_sequence(client, [FakeResponse({
        "status": "ok",
        "ports_fdb": {"port_id": 9, "mac_address": "aabbccddeeff", "vlan_id": "40"},
    })])

    assert client.get_device_fdb({"device_id": 7}) == [
        {"port_id": 9, "mac_address": "aabbccddeeff", "vlan_id": "40"},
    ]


def test_arp_normalization_uses_official_device_query():
    client = make_client()
    calls = attach_sequence(client, [FakeResponse({
        "status": "ok",
        "arp": [{"port_id": "9", "ipv4_address": "192.0.2.8"}],
    })])

    assert client.get_device_arp({"device_id": 7}) == [
        {"port_id": "9", "ipv4_address": "192.0.2.8"},
    ]
    assert calls[0]["url"].endswith("/api/v0/resources/ip/arp/all?device=7")


def test_links_normalization_preserves_lldp_fields():
    client = make_client()
    attach_sequence(client, [FakeResponse({
        "status": "ok",
        "links": [{
            "local_port_id": "9",
            "remote_port_id": 10,
            "protocol": "lldp",
        }],
    })])

    assert client.get_device_links({"device_id": "7"}) == [{
        "local_port_id": "9",
        "remote_port_id": 10,
        "protocol": "lldp",
    }]


def test_port_stack_normalization_preserves_string_and_integer_ids():
    client = make_client()
    calls = attach_sequence(client, [FakeResponse({
        "status": "ok",
        "mappings": [{
            "device_id": "7",
            "port_id_high": 100,
            "port_id_low": "9",
            "ifStackStatus": "active",
        }],
    })])

    assert client.get_device_port_stack({"device_id": 7}) == [{
        "device_id": "7",
        "port_id_high": 100,
        "port_id_low": "9",
        "ifStackStatus": "active",
    }]
    assert urlparse.parse_qs(urlparse.urlsplit(calls[0]["url"]).query) == {
        "valid_mappings": ["1"],
    }


def test_null_and_empty_results_normalize_to_empty_lists():
    client = make_client()
    attach_sequence(client, [
        FakeResponse({"status": "ok", "ports": None}),
        FakeResponse({"status": "ok", "ports_fdb": {}}),
    ])

    assert client.get_device_ports({"device_id": 7}) == []
    assert client.get_device_fdb({"device_id": 7}) == []


def test_timestamp_parsing_supports_librenms_sql_iso_and_epoch_formats(monkeypatch):
    monkeypatch.setenv("LIBRENMS_TIMEZONE", "UTC")
    expected = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)

    assert parse_librenms_timestamp("2026-08-10 12:00:00") == expected
    assert parse_librenms_timestamp("2026-08-10T12:00:00Z") == expected
    assert parse_librenms_timestamp(expected.timestamp()) == expected
    assert parse_librenms_timestamp("not-a-time") is None


def test_age_and_freshness_use_only_the_callers_threshold(monkeypatch):
    monkeypatch.setenv("LIBRENMS_TIMEZONE", "UTC")
    now = datetime(2026, 8, 10, 12, 2, tzinfo=timezone.utc)
    timestamp = "2026-08-10 12:00:00"

    assert age_seconds(timestamp, now=now) == 120
    assert is_fresh(timestamp, 120, now=now)
    assert not is_fresh(timestamp, 119, now=now)
    assert not is_fresh("bad", 120, now=now)


def test_explicit_retry_budget_overrides_environment(monkeypatch):
    monkeypatch.setenv("LIBRENMS_API_ATTEMPTS", "9")
    client = LibreNMSClient(token="test-token", max_attempts=1)
    assert client.max_attempts == 1
    calls = attach_sequence(client, [urlerror.URLError("offline")])
    client._sleep = lambda _: pytest.fail("one attempt must not sleep/retry")
    with pytest.raises(LibreNMSUnavailable): client.get_json("/api/v0/devices")
    assert len(calls) == client.request_count == 1


@pytest.mark.parametrize("value,expected", [(None, 3), ("5", 5), ("0", 3), ("invalid", 3)])
def test_omitted_retry_budget_preserves_env_default(monkeypatch, value, expected):
    if value is not None: monkeypatch.setenv("LIBRENMS_API_ATTEMPTS", value)
    assert LibreNMSClient(token="test-token").max_attempts == expected
    assert LibreNMSClient(token="test-token", max_attempts=None).max_attempts == expected


@pytest.mark.parametrize("value", [0, -1, "0", "invalid", 0.5])
def test_invalid_explicit_retry_budget_uses_existing_positive_int_default(monkeypatch, value):
    monkeypatch.setenv("LIBRENMS_API_ATTEMPTS", "9")
    assert LibreNMSClient(token="test-token", max_attempts=value).max_attempts == 3


@pytest.mark.parametrize("timeout", [2, 15])
def test_real_platform_history_factory_constructs_one_attempt_client(tmp_path, monkeypatch, timeout):
    import importlib.util
    from pathlib import Path
    monkeypatch.setenv("PLATFORM_WORKDIR", str(tmp_path))
    monkeypatch.setenv("PLATFORM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PLATFORM_NETWORK_HTTP_TIMEOUT", str(timeout))
    token_file = tmp_path / "token"
    token_file.write_text("fixture-token", encoding="utf-8")
    monkeypatch.setenv("PLATFORM_NETWORK_LIBRENMS_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("LIBRENMS_API_ATTEMPTS", "9")
    spec = importlib.util.spec_from_file_location("history_factory_wiring_test", Path(__file__).resolve().parents[1] / "platform_api/main.py")
    main = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(main)
    context = main._network_read_context()
    client = context.librenms_history_client_factory()
    assert isinstance(client, LibreNMSClient)
    assert client.max_attempts == 1 and client.timeout == min(3, timeout)
    assert context.librenms_client_factory().max_attempts == 9
    calls = attach_sequence(client, [urlerror.URLError("offline")])
    client._sleep = lambda _: pytest.fail("history factory must not retry")
    with pytest.raises(LibreNMSUnavailable): client.get_device("192.0.2.7")
    assert len(calls) == client.request_count == 1
    assert calls[0]["timeout"] <= 3
    assert not (tmp_path / "state").exists(), "factory must not initialize runtime or write state"


@pytest.mark.parametrize("value", ["2026-09-29 16:16:49", datetime(2026, 9, 29, 16, 16, 49)])
def test_naive_local_timestamp_uses_named_override(monkeypatch, value):
    monkeypatch.setenv("LIBRENMS_TIMEZONE", "Asia/Shanghai")
    monkeypatch.setenv("TZ", "UTC")
    assert parse_librenms_timestamp(value) == datetime(2026, 9, 29, 8, 16, 49, tzinfo=timezone.utc)


def test_timestamp_source_resolution_and_absolute_semantics(monkeypatch):
    monkeypatch.delenv("LIBRENMS_TIMEZONE", raising=False)
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    expected = datetime(2026, 9, 29, 8, 16, 49, 123456, tzinfo=timezone.utc)
    assert parse_librenms_timestamp("2026-09-29 16:16:49.123456") == expected
    for value in ["2026-09-29T16:16:49.123456+08:00", "2026-09-29T08:16:49.123456Z", expected, expected.timestamp()]:
        assert parse_librenms_timestamp(value) == expected
    monkeypatch.setenv("LIBRENMS_TIMEZONE", "invalid/timezone")
    assert parse_librenms_timestamp("2026-09-29 16:16:49.123456") == expected
    assert parse_librenms_timestamp("bad") is None


def test_local_timestamp_freshness_boundaries(monkeypatch):
    monkeypatch.setenv("LIBRENMS_TIMEZONE", "Asia/Shanghai")
    now = datetime(2026, 9, 29, 8, 26, 49, tzinfo=timezone.utc)
    assert age_seconds("2026-09-29 16:16:49", now=now) == 600
    assert is_fresh("2026-09-29 16:16:49", 600, now=now)
    assert not is_fresh("2026-09-29 16:16:48", 600, now=now)
    assert not is_fresh("2026-09-29 16:26:50", 600, now=now)
    assert not is_fresh(None, 600, now=now)


def test_unusable_named_timezone_delegates_to_runtime_local(monkeypatch):
    import librenms_client as client_module
    class LocalDatetime(datetime):
        def astimezone(self, tz=None):
            if self.tzinfo is None:
                from datetime import timedelta
                return self.replace(tzinfo=timezone(timedelta(hours=-5))).astimezone(tz)
            return super().astimezone(tz)
    monkeypatch.setattr(client_module, "datetime", LocalDatetime)
    for key in ("TZ", "LIBRENMS_TIMEZONE"):
        monkeypatch.setenv(key, "invalid/timezone")
    assert parse_librenms_timestamp("2026-09-29 16:16:49") == datetime(2026, 9, 29, 21, 16, 49, tzinfo=timezone.utc)


@pytest.mark.parametrize("local,utc_hour", [("2026-01-15 12:00:00", 17), ("2026-07-15 12:00:00", 16)])
def test_named_timezone_uses_historical_dst_not_fixed_offset(monkeypatch, local, utc_hour):
    monkeypatch.setenv("LIBRENMS_TIMEZONE", "America/New_York")
    assert parse_librenms_timestamp(local).hour == utc_hour


def test_posix_runtime_local_fallback(monkeypatch):
    import time
    if not hasattr(time, "tzset"):
        pytest.skip("POSIX tzset is unavailable; deterministic local fallback covered separately")
    try:
        with monkeypatch.context() as context:
            context.delenv("LIBRENMS_TIMEZONE", raising=False)
            context.setenv("TZ", "EST5")
            time.tzset()
            assert parse_librenms_timestamp("2026-09-29 16:16:49") == datetime(2026, 9, 29, 21, 16, 49, tzinfo=timezone.utc)
    finally:
        time.tzset()


@pytest.mark.parametrize("value", ["2026-11-01 01:30:00", datetime(2026, 11, 1, 1, 30), datetime(2026, 11, 1, 1, 30, fold=1)])
def test_duplicate_wall_clock_chooses_later_instant(monkeypatch, value):
    monkeypatch.setenv("LIBRENMS_TIMEZONE", "America/New_York")
    expected = datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc)
    assert parse_librenms_timestamp(value) == expected
    assert age_seconds(value, now=datetime(2026, 11, 1, 7, 30, tzinfo=timezone.utc)) == 3600


@pytest.mark.parametrize("value", ["2026-03-08 02:30:00", datetime(2026, 3, 8, 2, 30)])
def test_nonexistent_wall_clock_is_invalid(monkeypatch, value):
    monkeypatch.setenv("LIBRENMS_TIMEZONE", "America/New_York")
    assert parse_librenms_timestamp(value) is None
    assert age_seconds(value) is None
    assert not is_fresh(value, 600)
