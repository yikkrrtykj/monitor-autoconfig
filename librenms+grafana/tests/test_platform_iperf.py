import json
import socket
from http import HTTPStatus

import pytest

from platform_api import iperf

from .test_platform_transactions import load_api


class PortRangeError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def parse_ports(value, default="5201-5210", max_ports=10):
    return iperf.parse_port_range(
        value,
        default,
        max_ports,
        error_factory=PortRangeError,
    )


def test_iperf_helpers_are_extracted_without_entrypoint_wrappers(tmp_path):
    api = load_api(tmp_path)

    assert iperf.parse_port_range.__module__ == "platform_api.iperf"
    assert iperf.parse_iperf3_json.__module__ == "platform_api.iperf"
    assert iperf._iperf_error_summary.__module__ == "platform_api.iperf"
    assert iperf._iperf_target_is_internal.__module__ == "platform_api.iperf"
    assert api.platform_iperf is iperf
    assert not hasattr(api, "parse_port_range")
    assert not hasattr(api, "parse_iperf3_json")
    assert not hasattr(api, "_iperf_error_summary")
    assert not hasattr(api, "_iperf_target_is_internal")


@pytest.mark.parametrize("value", [None, ""])
def test_parse_port_range_keeps_default_for_missing_values(value):
    assert parse_ports(value) == list(range(5201, 5211))


def test_parse_port_range_keeps_single_range_and_whitespace_forms():
    assert parse_ports(" 5201 ") == [5201]
    assert parse_ports(" 5201 - 5203 ") == [5201, 5202, 5203]


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (" ", "端口应为单个端口或范围，例如 5201-5210"),
        ("5201,5202", "端口应为单个端口或范围，例如 5201-5210"),
        ("5202-5201", "端口范围无效"),
        ("0", "端口范围无效"),
        ("65536", "端口范围无效"),
    ],
)
def test_parse_port_range_keeps_validation_status_and_messages(value, message):
    with pytest.raises(PortRangeError) as exc:
        parse_ports(value)

    assert exc.value.status == HTTPStatus.BAD_REQUEST
    assert str(exc.value) == message


def test_parse_port_range_keeps_configurable_limit_message():
    with pytest.raises(PortRangeError) as exc:
        parse_ports("5201-5203", max_ports=2)

    assert exc.value.status == HTTPStatus.BAD_REQUEST
    assert str(exc.value) == "一次最多尝试 2 个端口"


def test_parse_iperf3_json_keeps_received_rate_endpoints_and_interval_sum():
    result = iperf.parse_iperf3_json(json.dumps({
        "intervals": [{
            "sum": {
                "start": 0.126,
                "end": 1.239,
                "seconds": 1.113,
                "bytes": 123,
                "bits_per_second": 1_234_567,
                "retransmits": 2,
            },
        }],
        "end": {
            "sum_sent": {
                "bits_per_second": 3_456_789,
                "seconds": 1.236,
                "bytes": 789,
                "retransmits": 7,
            },
            "sum_received": {
                "bits_per_second": 2_345_678,
                "seconds": 1.236,
                "bytes": 456,
            },
        },
    }))

    assert result == {
        "mbps": 2.35,
        "seconds": 1.24,
        "retransmits": 7,
        "bytes": 456,
        "sender": {
            "mbps": 3.46,
            "bytes": 789,
            "seconds": 1.24,
            "retransmits": 7,
        },
        "receiver": {
            "mbps": 2.35,
            "bytes": 456,
            "seconds": 1.24,
            "retransmits": 0,
        },
        "intervals": [{
            "start": 0.13,
            "end": 1.24,
            "seconds": 1.11,
            "bytes": 123,
            "mbps": 1.23,
            "retransmits": 2,
        }],
    }


def test_parse_iperf3_json_keeps_sent_rate_fallback():
    result = iperf.parse_iperf3_json(json.dumps({
        "end": {
            "sum_received": {"bytes": 12, "seconds": 2},
            "sum_sent": {
                "bits_per_second": 4_000_000,
                "bytes": 34,
                "seconds": 3,
                "retransmits": 5,
            },
        },
    }))

    assert result["mbps"] == 4.0
    assert result["seconds"] == 2.0
    assert result["bytes"] == 12
    assert result["sender"]["mbps"] == 4.0
    assert result["receiver"]["mbps"] == 0.0
    assert result["retransmits"] == 5


def test_parse_iperf3_json_keeps_sum_fallback():
    result = iperf.parse_iperf3_json(json.dumps({
        "end": {
            "sum": {
                "bits_per_second": 5_678_901,
                "bytes": 90,
                "seconds": 4.126,
                "retransmits": 6,
            },
        },
    }))

    assert result["mbps"] == 5.68
    assert result["seconds"] == 4.13
    assert result["bytes"] == 90
    assert result["sender"] == result["receiver"]
    assert result["sender"]["retransmits"] == 6
    assert result["retransmits"] == 0


def test_parse_iperf3_json_keeps_streams_interval_aggregation():
    result = iperf.parse_iperf3_json(json.dumps({
        "intervals": [{
            "streams": [
                {
                    "start": 0.123,
                    "end": 1.234,
                    "seconds": 1.111,
                    "bytes": 100,
                    "bits_per_second": 1_200_000,
                    "retransmits": 1,
                },
                "ignored",
                {
                    "start": 0.234,
                    "end": 1.456,
                    "seconds": 1.222,
                    "bytes": 200,
                    "bits_per_second": 2_300_000,
                    "retransmits": 2,
                },
            ],
        }],
        "end": {"sum": {"bits_per_second": 1_000_000}},
    }))

    assert result["intervals"] == [{
        "start": 0.12,
        "end": 1.46,
        "seconds": 1.22,
        "bytes": 300,
        "mbps": 3.5,
        "retransmits": 3,
    }]


def test_parse_iperf3_json_keeps_prefix_suffix_recovery():
    raw = 'warning before\n{"end":{"sum":{"bits_per_second":1234567}}}\nwarning after'

    assert iperf.parse_iperf3_json(raw)["mbps"] == 1.23


@pytest.mark.parametrize("raw", ["[]", "42", '"text"'])
def test_parse_iperf3_json_keeps_non_object_error(raw):
    with pytest.raises(ValueError, match="^iperf3 返回的 JSON 不是对象$"):
        iperf.parse_iperf3_json(raw)


def test_parse_iperf3_json_keeps_invalid_json_error():
    with pytest.raises(ValueError, match="^iperf3 未返回可解析的 JSON$"):
        iperf.parse_iperf3_json("not json")


def test_parse_iperf3_json_keeps_payload_error():
    with pytest.raises(ValueError, match="^fixture server error$"):
        iperf.parse_iperf3_json(json.dumps({"error": "fixture server error"}))


def test_parse_iperf3_json_keeps_missing_rate_error():
    with pytest.raises(ValueError, match="^iperf3 结果中没有速率数据$"):
        iperf.parse_iperf3_json(json.dumps({"end": {"sum_received": {}}}))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("control socket has closed unexpectedly", "服务器中途关闭连接"),
        ("server is busy", "服务器正忙"),
        ("unable to connect to server", "无法连接"),
        ("connection refused", "无法连接"),
    ],
)
def test_iperf_error_summary_keeps_known_mappings(raw, expected):
    assert iperf._iperf_error_summary("", raw, 1) == expected


def test_iperf_error_summary_keeps_json_error_mapping():
    raw = json.dumps({"error": "control socket has closed unexpectedly"})

    assert iperf._iperf_error_summary(raw, "", 1) == "服务器中途关闭连接"


def test_iperf_error_summary_keeps_source_fallback_and_whitespace_compression():
    assert iperf._iperf_error_summary("stdout\n  fallback", "", 2) == "stdout fallback"
    assert iperf._iperf_error_summary("stdout", "stderr\n preferred", 2) == "stderr preferred"
    assert iperf._iperf_error_summary("", "", 17) == "退出码 17"


def test_iperf_error_summary_keeps_last_160_characters():
    raw = "prefix " + ("x" * 170)

    assert iperf._iperf_error_summary("", raw, 1) == "x" * 160


@pytest.mark.parametrize(
    "host",
    [
        "10.0.0.1",
        "127.0.0.1",
        "169.254.1.1",
        "240.0.0.1",
        "224.0.0.1",
        "0.0.0.0",
        "::1",
        "fe80::1",
        "ff02::1",
        "::",
    ],
)
def test_iperf_target_is_internal_keeps_non_public_literal_categories(host):
    assert iperf._iperf_target_is_internal(host) is True


@pytest.mark.parametrize("host", ["8.8.8.8", "2606:4700:4700::1111"])
def test_iperf_target_is_internal_keeps_public_literals(host):
    assert iperf._iperf_target_is_internal(host) is False


def _address_info(address):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 0))


@pytest.mark.parametrize(
    ("addresses", "expected"),
    [
        (["8.8.8.8"], False),
        (["192.168.10.5"], True),
        (["8.8.8.8", "10.0.0.1"], True),
    ],
)
def test_iperf_target_is_internal_keeps_dns_resolution_semantics(
    monkeypatch, addresses, expected,
):
    calls = []

    def getaddrinfo(host, port, **kwargs):
        calls.append((host, port, kwargs))
        return [_address_info(address) for address in addresses]

    monkeypatch.setattr(iperf.socket, "getaddrinfo", getaddrinfo)

    assert iperf._iperf_target_is_internal("speed.example") is expected
    assert calls == [("speed.example", None, {"proto": socket.IPPROTO_TCP})]


def test_iperf_target_is_internal_keeps_dns_failure_as_public(monkeypatch):
    def getaddrinfo(*_args, **_kwargs):
        raise OSError("fixture lookup failed")

    monkeypatch.setattr(iperf.socket, "getaddrinfo", getaddrinfo)

    assert iperf._iperf_target_is_internal("missing.example") is False
