"""Parsing and target-validation helpers for platform iPerf diagnostics."""
from __future__ import annotations

import ipaddress
import json
import re
import socket
from collections.abc import Callable
from http import HTTPStatus


ErrorFactory = Callable[[int, str], Exception]


def parse_port_range(
    value,
    default: str = "5201-5210",
    max_ports: int = 10,
    *,
    error_factory: ErrorFactory,
) -> list[int]:
    text = str(value if value not in (None, "") else default).strip()
    match = re.fullmatch(r"(\d{1,5})(?:\s*-\s*(\d{1,5}))?", text)
    if not match:
        raise error_factory(HTTPStatus.BAD_REQUEST, "端口应为单个端口或范围，例如 5201-5210")
    start = int(match.group(1))
    end = int(match.group(2) or start)
    if not (1 <= start <= end <= 65535):
        raise error_factory(HTTPStatus.BAD_REQUEST, "端口范围无效")
    if end - start + 1 > max_ports:
        raise error_factory(HTTPStatus.BAD_REQUEST, f"一次最多尝试 {max_ports} 个端口")
    return list(range(start, end + 1))


def parse_iperf3_json(text: str) -> dict:
    raw = str(text or "").strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("iperf3 未返回可解析的 JSON")
        payload = json.loads(raw[start:end + 1])
    # 合法但非对象的 JSON（裸数组/数字/被代理截断的响应）必须走 ValueError，
    # 否则 AttributeError 会越过调用方的逐端口重试直接把整次测速打成 500。
    if not isinstance(payload, dict):
        raise ValueError("iperf3 返回的 JSON 不是对象")
    if payload.get("error"):
        raise ValueError(str(payload["error"]))

    def _as_dict(value) -> dict:
        return value if isinstance(value, dict) else {}

    ending = _as_dict(payload.get("end"))
    received = _as_dict(ending.get("sum_received"))
    sent = _as_dict(ending.get("sum_sent"))
    fallback = _as_dict(ending.get("sum"))
    bits_per_second = received.get("bits_per_second")
    if bits_per_second is None:
        bits_per_second = sent.get("bits_per_second", fallback.get("bits_per_second"))
    if bits_per_second is None:
        raise ValueError("iperf3 结果中没有速率数据")

    def endpoint_stats(value: dict) -> dict:
        return {
            "mbps": round(float(value.get("bits_per_second") or 0) / 1_000_000, 2),
            "bytes": int(value.get("bytes") or 0),
            "seconds": round(float(value.get("seconds") or 0), 2),
            "retransmits": int(value.get("retransmits") or 0),
        }

    intervals = []
    for item in payload.get("intervals") or []:
        if not isinstance(item, dict):
            continue
        interval = _as_dict(item.get("sum"))
        if not interval and item.get("streams"):
            streams = [stream for stream in item["streams"] if isinstance(stream, dict)]
        else:
            streams = []
        if not interval and streams:
            interval = {
                "start": min(float(stream.get("start") or 0) for stream in streams),
                "end": max(float(stream.get("end") or 0) for stream in streams),
                "seconds": max(float(stream.get("seconds") or 0) for stream in streams),
                "bytes": sum(int(stream.get("bytes") or 0) for stream in streams),
                "bits_per_second": sum(float(stream.get("bits_per_second") or 0) for stream in streams),
                "retransmits": sum(int(stream.get("retransmits") or 0) for stream in streams),
            }
        if not interval:
            continue
        intervals.append({
            "start": round(float(interval.get("start") or 0), 2),
            "end": round(float(interval.get("end") or 0), 2),
            "seconds": round(float(interval.get("seconds") or 0), 2),
            "bytes": int(interval.get("bytes") or 0),
            "mbps": round(float(interval.get("bits_per_second") or 0) / 1_000_000, 2),
            "retransmits": int(interval["retransmits"]) if interval.get("retransmits") is not None else None,
        })

    sender = endpoint_stats(sent or fallback)
    receiver = endpoint_stats(received or fallback)
    return {
        "mbps": round(float(bits_per_second) / 1_000_000, 2),
        "seconds": round(float(received.get("seconds") or sent.get("seconds") or fallback.get("seconds") or 0), 2),
        "retransmits": int(sent.get("retransmits") or 0),
        "bytes": receiver["bytes"],
        "sender": sender,
        "receiver": receiver,
        "intervals": intervals,
    }


def _iperf_error_summary(stdout: str, stderr: str, returncode: int) -> str:
    raw = (stderr or stdout or f"退出码 {returncode}").strip()
    try:
        payload = json.loads(raw)
        raw = str(payload.get("error") or raw)
    except (json.JSONDecodeError, TypeError):
        pass
    lowered = raw.lower()
    if "control socket has closed unexpectedly" in lowered:
        return "服务器中途关闭连接"
    if "server is busy" in lowered:
        return "服务器正忙"
    if "unable to connect" in lowered or "connection refused" in lowered:
        return "无法连接"
    return re.sub(r"\s+", " ", raw)[-160:]


def _iperf_target_is_internal(host: str) -> bool:
    """True when the target is (or resolves to) a non-public address.

    覆盖私网/环回/链路本地/保留/组播/未指定地址；域名会先解析再逐个地址判断，
    防止用一个解析到内网的域名绕过。解析失败按"非内网"放行——反正 iperf3
    连不上会给出明确报错，这里不用抢先拦。
    """
    def non_public(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        return (
            addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_reserved or addr.is_multicast or addr.is_unspecified
        )

    try:
        return non_public(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    for info in infos:
        try:
            if non_public(ipaddress.ip_address(info[4][0])):
                return True
        except ValueError:
            continue
    return False
