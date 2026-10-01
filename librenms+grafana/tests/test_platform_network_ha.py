from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest

from platform_api import network_ha, network_inspector
from .test_platform_network_read import FakeResponse, make_context
from .test_platform_network_inspector import InspectorClient


def row(ip, value, **labels):
    return {"metric": {"job": network_ha.HA_JOB, "target_ip": ip, **labels},
            "value": [2000, str(value)]}


def context_for(tmp_path, values, timestamps=None, config='fw-a:192.0.2.11,fw-b:192.0.2.12'):
    context, _ = make_context(tmp_path)
    context.env_path.write_text(f'FIREWALL_UNIT_SNMP_TARGETS="{config}"\n', encoding="utf-8")
    queries = []

    def urlopen(request, *, timeout):
        assert 0 < timeout <= context.http_timeout
        query = parse_qs(urlsplit(request.full_url).query)["query"][0]
        queries.append(query)
        if query.startswith("timestamp("):
            result = timestamps if timestamps is not None else [
                {**value, "value": [2000, "1990"]} for value in values]
        elif query.startswith("sysHAStatus{"):
            result = values
        else:
            result = []
        return FakeResponse({"status": "success", "data": {"result": result}})
    return replace(context, urlopen=urlopen), queries


def test_exact_fresh_pair_does_not_borrow_instance_name_or_other_job(tmp_path):
    context, queries = context_for(tmp_path, [row("192.0.2.11", 4), row("192.0.2.12", 3),
        row("192.0.2.99", 4, instance="192.0.2.11"),
        row("192.0.2.11", 3, job="firewall-snmp"),
        row("", 3, instance="192.0.2.11", display_name="fw-a")])
    result = network_ha.read_ha(context)
    assert result == {"source": "Hillstone sysHAStatus", "fresh": True, "units": [
        {"ip": "192.0.2.11", "name": "fw-a", "code": 4, "state": "master", "fresh": True},
        {"ip": "192.0.2.12", "name": "fw-b", "code": 3, "state": "backup", "fresh": True}]}
    assert queries == [
        'sysHAStatus{job="infra-fw-ha-snmp",target_ip=~"192\\\\.0\\\\.2\\\\.11|192\\\\.0\\\\.2\\\\.12"} @ 2000.0',
        'timestamp(sysHAStatus{job="infra-fw-ha-snmp",target_ip=~"192\\\\.0\\\\.2\\\\.11|192\\\\.0\\\\.2\\\\.12"} @ 2000.0)']


@pytest.mark.parametrize("code,state", [(0, "none"), (1, "init"), (2, "hello"),
    (3, "backup"), (4, "master"), (5, "vendor-unknown"), (6, "AA-mode")])
def test_vendor_enum_mapping(tmp_path, code, state):
    context, _ = context_for(tmp_path, [row("192.0.2.11", code)])
    unit = network_ha.read_ha(context)["units"][0]
    assert unit["code"] == code and unit["state"] == state and unit["fresh"]
    if code == 5:
        assert unit["vendorState"] == "slase"
        assert unit["state"] not in ("master", "backup", "AA-mode")


@pytest.mark.parametrize("values,stamps", [
    ([], []), ([row("192.0.2.11", 4)] * 2, None),
    ([row("192.0.2.11", 4)], []),
    ([row("192.0.2.11", 4)], [row("192.0.2.11", 1990)] * 2),
    ([row("192.0.2.11", 4)], [row("192.0.2.11", 1819)]),
    ([row("192.0.2.11", 4)], [row("192.0.2.11", 2001)]),
    ([row("192.0.2.11", 4)], [row("192.0.2.11", 1990, instance="other")]),
    ([row("192.0.2.11", 7)], None), ([row("192.0.2.11", 3.5)], None),
    ([row("192.0.2.11", "NaN")], None), ([row("192.0.2.11", "Inf")], None),
    ([row("192.0.2.11", 4)], [row("192.0.2.11", "NaN")]),
])
def test_missing_duplicate_stale_invalid_or_mismatched_is_unknown(tmp_path, values, stamps):
    context, _ = context_for(tmp_path, values, stamps)
    result = network_ha.read_ha(context)
    assert not result["fresh"]
    assert result["units"][0] == {"ip": "192.0.2.11", "name": "fw-a", "code": None,
                                 "state": "unknown", "fresh": False}


@pytest.mark.parametrize("stamp", [1820, 2000])
def test_freshness_inclusive_boundary_uses_scrape_timestamp_not_evaluation_time(tmp_path, stamp):
    context, _ = context_for(tmp_path, [row("192.0.2.11", 4)], [row("192.0.2.11", stamp)])
    result = network_ha.read_ha(context)
    assert result["units"][0]["fresh"]
    assert not result["units"][1]["fresh"] and not result["fresh"]


@pytest.mark.parametrize("config", ["", "invalid", "a:192.0.2.11,b:192.0.2.11",
    "192.0.2.1-192.0.2.17", ",".join(f"192.0.2.{i}" for i in range(1, 18))])
def test_unavailable_or_oversize_configuration_never_queries_prometheus(tmp_path, config):
    context, queries = context_for(tmp_path, [], config=config)
    assert network_ha.read_ha(context)["units"] == []
    assert queries == []


def test_single_bounded_query_budget_and_prometheus_failure(tmp_path, monkeypatch):
    context, queries = context_for(tmp_path, [row("192.0.2.11", 4)])
    ticks = iter([0, 6])
    monkeypatch.setattr(network_ha.time, "monotonic", lambda: next(ticks))
    result = network_ha.read_ha(context)
    assert len(queries) == 1 and not result["fresh"]
    context = replace(context, urlopen=lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError()))
    monkeypatch.setattr(network_ha.time, "monotonic", lambda: 0)
    assert not network_ha.read_ha(context)["fresh"]


@pytest.mark.parametrize("ip,expected", [("192.0.2.1", "unavailable"), ("192.0.2.11", "master")])
def test_hillstone_inspector_keeps_vip_separate_and_reuses_existing_single_device_ports(tmp_path, ip, expected):
    context, queries = context_for(tmp_path, [row("192.0.2.11", 4), row("192.0.2.12", 3)])
    device = {"device_id": 7, "ip": ip, "os": "stoneos", "sysName": "Hillstone"}
    client = InspectorClient([device], [{"ifOperStatus": "up"}])
    result = network_inspector.read_inspector(replace(context, librenms_client_factory=lambda: client), ip)
    assert result["haRole"] == expected
    assert result["ha"]["fresh"]
    assert client.requested == [("device", ip), (device, network_inspector.PORT_COLUMNS)]
    assert result["ports"]["up"] == 1
    assert not {"policy", "nat", "session", "ips", "securityPolicy"}.intersection(result)
    assert len(queries) == 4


def test_non_hillstone_does_not_read_ha_config_or_metrics(tmp_path):
    context, queries = context_for(tmp_path, [row("192.0.2.11", 4)])
    device = {"device_id": 7, "ip": "192.0.2.11", "os": "ios"}
    client = InspectorClient([device], [{"ifOperStatus": "up"}])
    result = network_inspector.read_inspector(replace(context, librenms_client_factory=lambda: client), device["ip"])
    assert "ha" not in result and result["haRole"] is None
    assert all("sysHAStatus" not in query for query in queries)
