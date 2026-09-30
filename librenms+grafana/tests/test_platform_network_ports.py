import hashlib
import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from librenms_client import LibreNMSError
from platform_api import network_ports, network_read, read_api

from .test_librenms_client import FakeResponse as LibreNMSResponse, attach_sequence, make_client
from .test_platform_network_read import FakeResponse, make_context


IP = "192.0.2.7"


class PortsClient:
    def __init__(self, ports, device=None):
        self.device = device or {"device_id": 7, "ip": IP, "os": "ios", "sysName": "core-a"}
        self.ports = ports
        self.calls = []

    def get_device(self, ip):
        self.calls.append(("device", ip))
        return self.device

    def get_device_ports(self, device, *, columns, with_vlans):
        self.calls.append(("ports", device, columns, with_vlans))
        return self.ports


def context_for(tmp_path, client, edges=None, metrics=None, fail_metrics=False):
    context, _ = make_context(tmp_path)
    context.topology_path.write_text(json.dumps(edges or []), encoding="utf-8")
    queries = []

    def urlopen(request, *, timeout):
        assert 0 < timeout <= context.http_timeout
        query = parse_qs(urlsplit(request.full_url).query)["query"][0]
        queries.append(query)
        if fail_metrics:
            raise OSError("Prometheus unavailable")
        return FakeResponse({"status": "success", "data": {"result": (metrics or {}).get(query, [])}})

    return network_read.NetworkReadContext(**{
        **context.__dict__, "librenms_client_factory": lambda: client,
        "urlopen": urlopen, "clock": lambda: context.topology_path.stat().st_mtime,
    }), queries


def sample(index, value, *, ip=IP):
    return {"metric": {"target_ip": ip, "ifIndex": str(index)}, "value": [time.time(), str(value)]}


def firewall_sample(index, value, *, ip=IP):
    return {"metric": {"job": "firewall-snmp", "instance": ip, "ifIndex": str(index)},
            "value": [time.time(), str(value)]}


def set_isp_inventory(context, entries):
    raw = json.dumps(entries).encode("utf-8")
    context.isp_inventory_path.write_bytes(raw)
    context.isp_state_path.write_text(json.dumps({
        "status": "ok", "inventory_count": len(entries),
        "inventory_sha256": hashlib.sha256(raw).hexdigest(),
    }), encoding="utf-8")


def test_full_inventory_includes_down_ports_and_observed_vlan_evidence(tmp_path):
    client = PortsClient([
        {"ifIndex": 101, "ifName": "Gi1/0/17", "ifDescr": "GigabitEthernet1/0/17",
         "ifAlias": "desk", "ifAdminStatus": 1, "ifOperStatus": 1, "ifSpeed": 1000000000,
         "vlans": [{"vlan": 42, "untagged": 1}]},
        {"ifIndex": 202, "ifName": "Te2/0/3", "ifAdminStatus": 2,
         "ifOperStatus": 2, "ifSpeed": None, "vlans": [{"vlan": "10", "untagged": 0}]},
        {"ifIndex": 303, "ifName": "Port-channel1", "ifAdminStatus": None,
         "ifOperStatus": None, "ifSpeed": 4294967295},
    ])
    context, queries = context_for(tmp_path, client)
    result = network_ports.read_ports(context, IP)

    assert result["count"] == 3
    assert result["kind"] == "cisco"
    first, second, third = result["ports"]
    assert (first["stackMember"], first["portNumber"]) == (1, 17)
    assert (second["stackMember"], second["portNumber"]) == (2, 3)
    assert (third["stackMember"], third["portNumber"]) == (None, None)
    assert network_ports._stack_parts("Gi99/0/999") == (None, None)
    assert second["adminState"] == "down" and second["operState"] == "down"
    assert third["adminState"] == "unknown" and third["speedBps"] is None
    assert first["vlanEvidence"] == {"authority": "observed", "memberships": [
        {"vlanId": 42, "untagged": True}]}
    assert second["vlanEvidence"]["memberships"] == [{"vlanId": 10, "untagged": False}]
    assert "accessVlan" not in json.dumps(result) and "nativeVlan" not in json.dumps(result)
    assert client.calls == [("device", IP), ("ports", client.device, network_ports.PORT_COLUMNS, True)]
    assert len(queries) == len(network_ports.METRICS)


def test_current_metrics_rate_capacity_and_cumulative_counters_are_limited_to_matching_ifindex(tmp_path):
    client = PortsClient([
        {"ifIndex": 101, "ifName": "Gi1/0/17", "ifSpeed": 1000000000},
        {"ifIndex": 202, "ifName": "Te2/0/3", "ifSpeed": 4294967295},
    ])
    metrics = {
        template.replace("{ip}", IP): [sample(101, value)]
        for field, template, value in [
            ("rxBps", network_ports.METRICS["rxBps"], 100000000),
            ("txBps", network_ports.METRICS["txBps"], 200000000),
            ("inputErrorsTotal", network_ports.METRICS["inputErrorsTotal"], 5),
        ]
    }
    metrics[network_ports.METRICS["highSpeedMbps"].replace("{ip}", IP)] = [sample(202, 10000)]
    result = network_ports.read_ports(context_for(tmp_path, client, metrics=metrics)[0], IP)
    first, second = result["ports"]
    assert first["rxBps"] == 100000000 and first["txBps"] == 200000000
    assert first["rxUtilization"] == 0.1 and first["txUtilization"] == 0.2
    assert first["inputErrorsTotal"] == 5 and first["inputDiscardsTotal"] is None
    assert second["speedBps"] == 10000000000
    assert second["rxBps"] is None and second["rxUtilization"] is None


def test_stale_edges_omitted_and_current_aggregate_member_evidence_maps_to_ports(tmp_path):
    client = PortsClient([{"ifIndex": 101, "ifName": "Gi1/0/17"},
                          {"ifIndex": 102, "ifName": "Po1"}])
    edges = [
        {"from_ip": IP, "from_port": "Po1", "from_aggregate_port": "Po1",
         "from_member_ports": ["Gi1/0/17"], "to_ip": "192.0.2.8",
         "to_sysname": "peer-a", "to_port": "Po2", "protocols": ["lldp"]},
        {"from_ip": IP, "from_port": "Gi1/0/17", "to_ip": "192.0.2.9", "stale": True},
    ]
    result = network_ports.read_ports(context_for(tmp_path, client, edges=edges)[0], IP)
    assert result["degraded"] is True
    assert "已省略过期邻接" in result["warnings"]
    for port in result["ports"]:
        assert port["neighbors"] == [{"peerIp": "192.0.2.8", "peerName": "peer-a",
                                      "peerPort": "Po2", "protocols": ["lldp"],
                                      "aggregatePort": "Po1", "memberPorts": ["Gi1/0/17"]}]


def test_prometheus_failure_keeps_inventory_and_unknown_metrics(tmp_path):
    client = PortsClient([{"ifIndex": 101, "ifName": "Gi1/0/17", "ifSpeed": 1000000000}])
    result = network_ports.read_ports(context_for(tmp_path, client, fail_metrics=True)[0], IP)
    assert result["count"] == 1 and result["degraded"] is True
    assert "当前 IF-MIB 指标部分不可用" in result["warnings"]
    assert result["ports"][0]["rxBps"] is None
    assert result["ports"][0]["inputErrorsTotal"] is None


def test_no_ifmib_target_coverage_is_degraded_but_inventory_succeeds(tmp_path):
    client = PortsClient([{"ifIndex": 101, "ifName": "Gi1/0/17"}])
    result = network_ports.read_ports(context_for(tmp_path, client)[0], IP)
    assert result["count"] == 1
    assert result["ports"][0]["rxBps"] is None
    assert result["warnings"] == ["当前 IF-MIB 无有效覆盖"]
    assert result["degraded"] is True


def test_single_metric_failure_keeps_other_current_values(tmp_path):
    client = PortsClient([{"ifIndex": 101, "ifName": "Gi1/0/17"}])
    context, _ = context_for(tmp_path, client)
    original = context.urlopen

    def partial(request, *, timeout):
        query = parse_qs(urlsplit(request.full_url).query)["query"][0]
        if "ifHCInOctets" in query:
            raise OSError("RX metric unavailable")
        if "ifInErrors" in query:
            return FakeResponse({"status": "success", "data": {"result": [sample(101, 7)]}})
        return original(request, timeout=timeout)

    context = network_read.NetworkReadContext(**{**context.__dict__, "urlopen": partial})
    result = network_ports.read_ports(context, IP)
    assert result["ports"][0]["rxBps"] is None
    assert result["ports"][0]["inputErrorsTotal"] == 7
    assert result["degraded"] is True
    assert result["warnings"] == ["当前 IF-MIB 指标部分不可用"]


def test_stopped_scrape_does_not_become_current_rate_despite_fresh_query_timestamp(tmp_path):
    client = PortsClient([{"ifIndex": 101, "ifName": "Gi1/0/17", "ifSpeed": 1000000000}])
    context, queries = context_for(tmp_path, client)
    stopped_scrape_age = network_ports.METRIC_STALE_SECONDS + 1

    def prometheus(request, *, timeout):
        query = parse_qs(urlsplit(request.full_url).query)["query"][0]
        queries.append(query)
        # Prometheus evaluates the expression now, while the last raw scrape is old.
        raw_age = stopped_scrape_age
        freshness_filter = ("timestamp(" in query
                            and f'target_ip="{IP}"' in query
                            and f"time() - {network_ports.METRIC_STALE_SECONDS}" in query)
        rows = [] if freshness_filter and raw_age > network_ports.METRIC_STALE_SECONDS else [
            sample(101, 100000000)]
        return FakeResponse({"status": "success", "data": {"result": rows}})

    context = network_read.NetworkReadContext(**{**context.__dict__, "urlopen": prometheus})
    result = network_ports.read_ports(context, IP)
    assert result["ports"][0]["rxBps"] is None
    assert result["ports"][0]["rxUtilization"] is None
    assert result["warnings"] == ["当前 IF-MIB 无有效覆盖"]
    assert len(queries) == len(network_ports.METRICS)
    assert "timestamp(ifHCInOctets" in queries[0]


def test_prometheus_metric_enrichment_uses_one_deadline(monkeypatch, tmp_path):
    client = PortsClient([{"ifIndex": 101, "ifName": "Gi1/0/17"}])
    context, _ = context_for(tmp_path, client)
    elapsed = [0.0]
    timeouts = []
    monkeypatch.setattr(network_ports.time, "monotonic", lambda: elapsed[0])

    def slow_prometheus(_request, *, timeout):
        timeouts.append(timeout)
        elapsed[0] += min(2.0, timeout)
        raise TimeoutError("slow Prometheus")

    context = network_read.NetworkReadContext(**{
        **context.__dict__, "urlopen": slow_prometheus, "http_timeout": 5.0,
    })
    result = network_ports.read_ports(context, IP)
    assert timeouts == [5.0, 3.0, 1.0]
    assert elapsed[0] == 5.0
    assert result["count"] == 1 and result["ports"][0]["rxBps"] is None
    assert result["degraded"] is True


@pytest.mark.parametrize("value,status,code", [
    ("not-an-ip", 400, "invalid_node"),
    ("127.0.0.1/../../x", 400, "invalid_node"),
])
def test_invalid_ip_rejected_before_client_use(tmp_path, value, status, code):
    client = PortsClient([])
    with pytest.raises(network_read.NetworkReadError) as caught:
        network_ports.read_ports(context_for(tmp_path, client)[0], value)
    assert (caught.value.status, caught.value.payload["code"]) == (status, code)
    assert client.calls == []


def test_missing_wrong_identity_and_non_cisco_rejected(tmp_path):
    for device, expected in [
        (None, "node_not_found"),
        ({"device_id": 9, "ip": "192.0.2.99", "os": "ios"}, "identity_mismatch"),
        ({"device_id": 7, "ip": IP, "os": "linux"}, "unsupported_os"),
    ]:
        client = PortsClient([{"ifIndex": 1}], device={"ip": IP, "os": "ios"})
        client.device = device
        with pytest.raises(network_read.NetworkReadError) as caught:
            network_ports.read_ports(context_for(tmp_path, client)[0], IP)
        assert caught.value.payload["code"] == expected
        assert not any(call[0] == "ports" for call in client.calls)


@pytest.mark.parametrize("os_name", ["stoneos", "hillstone"])
def test_hillstone_inventory_without_current_coverage_keeps_all_interfaces(tmp_path, os_name):
    client = PortsClient([
        {"ifIndex": 7, "ifName": "eth1", "ifAdminStatus": 1, "ifOperStatus": 1},
        {"ifIndex": 8, "ifName": "eth2", "ifAdminStatus": 2, "ifOperStatus": 2},
        {"ifIndex": 9, "ifName": "vlan10", "ifAdminStatus": 1, "ifOperStatus": 2},
    ], device={"device_id": 7, "ip": IP, "os": os_name})
    context, queries = context_for(tmp_path, client)
    result = network_ports.read_ports(context, IP)
    assert result["kind"] == "hillstone" and result["count"] == 3
    assert [port["ifName"] for port in result["ports"]] == ["eth1", "eth2", "vlan10"]
    assert all(port["stackMember"] is None and port["portNumber"] is None for port in result["ports"])
    assert all(port["rxBps"] is None and port["wanEvidence"] is None for port in result["ports"])
    assert result["ports"][1]["adminState"] == "down"
    assert result["degraded"] and "当前防火墙 IF-MIB 无有效覆盖" in result["warnings"]
    assert len(queries) == len(network_ports.FIREWALL_METRICS)
    assert all(f'job="firewall-snmp",instance="{IP}"' in query for query in queries)
    assert "timestamp(ifHCInOctets" in queries[0] and "time() - 120" in queries[0]


def test_hillstone_current_metrics_match_exact_instance_and_ifindex(tmp_path):
    client = PortsClient([{"ifIndex": 7, "ifName": "eth1", "ifSpeed": 1000000000},
                          {"ifIndex": 8, "ifName": "eth2"}],
                         device={"device_id": 7, "ip": IP, "os": "stoneos"})
    queries = network_ports.FIREWALL_METRICS
    metrics = {
        queries["rxBps"].replace("{ip}", IP): [firewall_sample(7, 100000000),
                                                 firewall_sample(8, 50000000, ip="192.0.2.8")],
        queries["txBps"].replace("{ip}", IP): [firewall_sample(7, 200000000)],
        queries["inputErrorsTotal"].replace("{ip}", IP): [firewall_sample(7, 5)],
    }
    result = network_ports.read_ports(context_for(tmp_path, client, metrics=metrics)[0], IP)
    first, second = result["ports"]
    assert first["rxBps"] == 100000000 and first["txBps"] == 200000000
    assert first["rxUtilization"] == 0.1 and first["txUtilization"] == 0.2
    assert first["inputErrorsTotal"] == 5
    assert second["rxBps"] is None and second["rxUtilization"] is None


def test_hillstone_stopped_scrape_and_peer_metrics_never_look_current(tmp_path):
    client = PortsClient([{"ifIndex": 7, "ifName": "eth1"}],
                         device={"device_id": 7, "ip": IP, "os": "stoneos"})
    context, queries = context_for(tmp_path, client)
    original = context.urlopen

    def prometheus(request, *, timeout):
        query = parse_qs(urlsplit(request.full_url).query)["query"][0]
        if "ifHCInOctets" in query:
            assert 'instance="192.0.2.7"' in query and "timestamp(" in query
            return FakeResponse({"status": "success", "data": {"result": [
                firewall_sample(7, 42, ip="192.0.2.8")]}})
        return original(request, timeout=timeout)

    context = network_read.NetworkReadContext(**{**context.__dict__, "urlopen": prometheus})
    result = network_ports.read_ports(context, IP)
    assert result["ports"][0]["rxBps"] is None
    assert "当前防火墙 IF-MIB 无有效覆盖" in result["warnings"]
    assert len(queries) == len(network_ports.FIREWALL_METRICS) - 1


def test_hillstone_old_raw_sample_and_partial_prometheus_failure_keep_inventory(tmp_path):
    client = PortsClient([{"ifIndex": 7, "ifName": "eth1"}],
                         device={"device_id": 7, "ip": IP, "os": "stoneos"})
    context, _ = context_for(tmp_path, client)

    def prometheus(request, *, timeout):
        query = parse_qs(urlsplit(request.full_url).query)["query"][0]
        if "ifHCInOctets" in query:
            assert "timestamp(ifHCInOctets" in query and "time() - 120" in query
            # A previous raw scrape is outside the expression's freshness gate.
            return FakeResponse({"status": "success", "data": {"result": []}})
        if "ifInErrors" in query:
            raise OSError("one metric unavailable")
        if "ifOutErrors" in query:
            return FakeResponse({"status": "success", "data": {"result": [firewall_sample(7, 3)]}})
        return FakeResponse({"status": "success", "data": {"result": []}})

    context = network_read.NetworkReadContext(**{**context.__dict__, "urlopen": prometheus})
    result = network_ports.read_ports(context, IP)
    assert result["count"] == 1 and result["ports"][0]["rxBps"] is None
    assert result["ports"][0]["outputErrorsTotal"] == 3
    assert "当前防火墙 IF-MIB 指标部分不可用" in result["warnings"]


def test_hillstone_exact_isp_join_and_conflict_do_not_guess(tmp_path):
    client = PortsClient([{"ifIndex": 7, "ifName": "eth1"}, {"ifIndex": 8, "ifName": "eth2"}],
                         device={"device_id": 7, "ip": IP, "os": "stoneos"})
    context, _ = context_for(tmp_path, client)
    record = lambda target, index, name: {"targets": ["198.51.100.1"], "labels": {
        "metric_target": target, "metric_ifindex": str(index), "display_name": name,
        "wan_ip": "198.51.100.2"}}
    set_isp_inventory(context, [record(IP, 7, "ISP A"), record("192.0.2.8", 8, "peer ISP")])
    result = network_ports.read_ports(context, IP)
    assert result["ports"][0]["wanEvidence"] == {"authority": "isp-inventory", "name": "ISP A",
                                                    "wanIp": "198.51.100.2", "gateway": "198.51.100.1"}
    assert result["ports"][1]["wanEvidence"] is None
    set_isp_inventory(context, [record(IP, 7, "ISP A"), record(IP, 7, "ISP B")])
    result = network_ports.read_ports(context, IP)
    assert result["ports"][0]["wanEvidence"] is None
    assert "WAN / ISP 映射存在冲突，已省略" in result["warnings"]
    context.isp_state_path.write_text('{"status":"stale"}', encoding="utf-8")
    result = network_ports.read_ports(context, IP)
    assert result["ports"][0]["wanEvidence"] is None
    assert "WAN / ISP 资料已过期或不一致" in result["warnings"]


def test_hillstone_wan_evidence_retries_publication_pair_once(monkeypatch, tmp_path):
    client = PortsClient([{"ifIndex": 7, "ifName": "eth1"}],
                         device={"device_id": 7, "ip": IP, "os": "stoneos"})
    context, _ = context_for(tmp_path, client)
    set_isp_inventory(context, [{"targets": ["198.51.100.1"], "labels": {
        "metric_target": IP, "metric_ifindex": "7", "display_name": "ISP A",
        "wan_ip": "198.51.100.2"}}])
    coherent = network_read._read_isp_pair(context)
    inventory, state, raw = coherent
    calls = []

    def publication_pair(_context):
        calls.append(True)
        if len(calls) == 1:
            return inventory, {**state, "inventory_sha256": "old-published-digest"}, raw
        return coherent

    monkeypatch.setattr(network_ports, "_read_isp_pair", publication_pair)
    result = network_ports.read_ports(context, IP)
    assert len(calls) == 2, "a transient pair mismatch gets one bounded re-read"
    assert result["ports"][0]["wanEvidence"] == {
        "authority": "isp-inventory", "name": "ISP A",
        "wanIp": "198.51.100.2", "gateway": "198.51.100.1",
    }
    assert "WAN / ISP 资料已过期或不一致" not in result["warnings"]


def test_hillstone_metric_budget_and_bounds(monkeypatch, tmp_path):
    client = PortsClient([{"ifIndex": 1, "ifName": "X" * 300}],
                         device={"device_id": 7, "ip": IP, "os": "stoneos"})
    context, _ = context_for(tmp_path, client)
    elapsed = [0.0]
    timeouts = []
    monkeypatch.setattr(network_ports.time, "monotonic", lambda: elapsed[0])

    def slow(_request, *, timeout):
        timeouts.append(timeout)
        elapsed[0] += min(2.0, timeout)
        raise TimeoutError("slow")

    context = network_read.NetworkReadContext(**{**context.__dict__, "urlopen": slow, "http_timeout": 5.0})
    result = network_ports.read_ports(context, IP)
    assert timeouts == [5.0, 3.0, 1.0]
    assert len(result["ports"][0]["ifName"]) == network_ports.TEXT_LIMIT
    assert result["ports"][0]["rxBps"] is None
    client.ports = [{"ifIndex": 1}] * (network_ports.PORT_LIMIT + 1)
    with pytest.raises(network_read.NetworkReadError) as oversized:
        network_ports.read_ports(context, IP)
    assert oversized.value.payload["code"] == "ports_invalid"
    client.ports = [{"ifIndex": 1, "ifName": "eth1"}]
    monkeypatch.setattr(network_ports, "RESPONSE_BYTE_LIMIT", 100)
    with pytest.raises(network_read.NetworkReadError) as oversized_response:
        network_ports.read_ports(context, IP)
    assert oversized_response.value.payload["code"] == "ports_oversize"


def test_inventory_failure_and_hard_port_count_bound(tmp_path):
    client = PortsClient([{"ifIndex": 1}])
    client.get_device_ports = lambda *_args, **_kwargs: (_ for _ in ()).throw(LibreNMSError("offline"))
    with pytest.raises(network_read.NetworkReadError) as unavailable:
        network_ports.read_ports(context_for(tmp_path, client)[0], IP)
    assert unavailable.value.payload["code"] == "ports_unavailable"
    client = PortsClient([{"ifIndex": 1}] * (network_ports.PORT_LIMIT + 1))
    with pytest.raises(network_read.NetworkReadError) as oversized:
        network_ports.read_ports(context_for(tmp_path, client)[0], IP)
    assert oversized.value.payload["code"] == "ports_invalid"


def test_device_read_failure_is_unavailable_and_does_not_request_ports(tmp_path):
    client = PortsClient([{"ifIndex": 1}])

    def fail_device(_ip):
        client.calls.append(("device", IP))
        raise LibreNMSError("offline")

    client.get_device = fail_device
    with pytest.raises(network_read.NetworkReadError) as unavailable:
        network_ports.read_ports(context_for(tmp_path, client)[0], IP)
    assert unavailable.value.payload["code"] == "librenms_unavailable"
    assert client.calls == [("device", IP)]


def test_text_and_vlan_bounds_are_applied(tmp_path):
    client = PortsClient([{"ifIndex": 1, "ifName": "X" * 300,
                           "ifAlias": "Y" * 300,
                           "vlans": [{"vlan": index + 1} for index in range(network_ports.VLAN_LIMIT + 1)]}])
    result = network_ports.read_ports(context_for(tmp_path, client)[0], IP)
    port = result["ports"][0]
    assert len(port["ifName"]) == network_ports.TEXT_LIMIT
    assert len(port["ifAlias"]) == network_ports.TEXT_LIMIT
    assert port["vlanEvidence"] is None
    assert "VLAN 观测资料超出范围" in result["warnings"]


def test_response_byte_limit_fails_explicitly_without_truncating_ports(monkeypatch, tmp_path):
    monkeypatch.setattr(network_ports, "RESPONSE_BYTE_LIMIT", 100)
    client = PortsClient([{"ifIndex": 1, "ifName": "Gi1/0/1"}])
    with pytest.raises(network_read.NetworkReadError) as oversized:
        network_ports.read_ports(context_for(tmp_path, client)[0], IP)
    assert oversized.value.payload["code"] == "ports_oversize"


def test_stale_topology_snapshot_does_not_present_old_neighbor_as_current(tmp_path):
    client = PortsClient([{"ifIndex": 1, "ifName": "Gi1/0/1"}])
    context, _ = context_for(tmp_path, client, edges=[
        {"from_ip": IP, "from_port": "Gi1/0/1", "to_ip": "192.0.2.8"}])
    context = network_read.NetworkReadContext(**{
        **context.__dict__, "clock": lambda: context.topology_path.stat().st_mtime
        + context.topology_stale_seconds + 1,
    })
    result = network_ports.read_ports(context, IP)
    assert result["ports"][0]["neighbors"] is None
    assert "拓扑快照已过期" in result["warnings"]


def test_missing_topology_is_unknown_neighbor_evidence_not_empty_success(tmp_path):
    client = PortsClient([{"ifIndex": 1, "ifName": "Gi1/0/1"}])
    context, _ = context_for(tmp_path, client)
    context.topology_path.unlink()
    result = network_ports.read_ports(context, IP)
    assert result["ports"][0]["neighbors"] is None
    assert "邻接资料暂不可用" in result["warnings"]


@pytest.mark.parametrize("os_name", ["ios", "stoneos"])
def test_real_librenms_client_uses_single_device_and_one_ports_get(tmp_path, os_name):
    client = make_client(max_attempts=1)
    calls = attach_sequence(client, [
        LibreNMSResponse({"status": "ok", "devices": [{"device_id": 7, "ip": IP, "os": os_name}]}),
        LibreNMSResponse({"status": "ok", "ports": [{"ifIndex": 1, "ifName": "Gi1/0/1"}]}),
    ])
    result = network_ports.read_ports(context_for(tmp_path, client)[0], IP)
    assert result["count"] == 1
    assert result["kind"] == ("cisco" if os_name == "ios" else "hillstone")
    paths = [urlsplit(call["url"]).path for call in calls]
    assert paths == [f"/api/v0/devices/{IP}", "/api/v0/devices/7/ports"]
    assert "/api/v0/devices" not in paths
    assert parse_qs(urlsplit(calls[1]["url"]).query) == {
        "columns": [network_ports.PORT_COLUMNS], "with": ["vlans"]}


def test_ports_route_authenticates_before_read(monkeypatch, tmp_path):
    events = []
    context = context_for(tmp_path, PortsClient([]))[0]
    monkeypatch.setattr(network_ports, "read_ports", lambda _context, ip: events.append(("read", ip)) or {"ok": True})

    class Handler:
        def _send_json(self, payload, status=200):
            events.append(("response", status, payload))

    router = read_api.ReadApiContext(
        event_config_context=None, transaction_context=None, incident_context=None,
        iperf_runtime_context=None, dhcp_settings_context=None, dhcp_runtime_context=None,
        bridge_url="", require_auth=lambda _handler: {}, read_json_file=lambda *_: None,
        stamp=lambda: "", network_context=context,
        network_require_auth=lambda _handler: events.append(("auth",)),
    )
    read_api.handle_get(Handler(), f"/network/nodes/{IP}/ports", router)
    assert events == [("auth",), ("read", IP), ("response", 200, {"ok": True})]


def test_ports_route_rejects_unauthenticated_before_any_read(monkeypatch, tmp_path):
    events = []
    context = context_for(tmp_path, PortsClient([]))[0]
    monkeypatch.setattr(network_ports, "read_ports", lambda *_args: events.append("read"))

    def deny(_handler):
        events.append("auth")
        raise PermissionError("unauthenticated")

    router = read_api.ReadApiContext(
        event_config_context=None, transaction_context=None, incident_context=None,
        iperf_runtime_context=None, dhcp_settings_context=None, dhcp_runtime_context=None,
        bridge_url="", require_auth=lambda _handler: {}, read_json_file=lambda *_: None,
        stamp=lambda: "", network_context=context, network_require_auth=deny,
    )
    with pytest.raises(PermissionError):
        read_api.handle_get(object(), f"/network/nodes/{IP}/ports", router)
    assert events == ["auth"]


@pytest.mark.parametrize("os_name", ["ciscosb", "cisco-access", "hillstone"])
def test_fresh_poller_fallback_is_single_read_and_device_local(tmp_path, os_name):
    row = {"device_id": 7, "ifIndex": 1, "ifName": "Gi1/0/1", "ifSpeed": 1000,
           "poll_time": time.time() - 30, "ifInOctets_rate": 10,
           "ifOutOctets_rate": 20, "ifInErrors": 3}
    client = PortsClient([row], {"device_id": 7, "ip": IP, "os": os_name})
    context, _ = context_for(tmp_path, client)
    result = network_ports.read_ports(context, IP)
    port = result["ports"][0]
    assert port["rxBps"] == 80 and port["txBps"] == 160
    assert port["rxUtilization"] == .08 and port["inputErrorsTotal"] == 3
    assert port["outputErrorsTotal"] is None
    assert port["metricSource"] == "LibreNMS poller"
    assert 29 <= port["metricAgeSeconds"] <= 31
    assert port["stackMember"] is None and port["portNumber"] is None
    assert len([call for call in client.calls if call[0] == "ports"]) == 1
    row["device_id"] = 99
    port = network_ports.read_ports(context, IP)["ports"][0]
    assert port["rxBps"] is None and port["metricSource"] is None


@pytest.mark.parametrize("poll", [None, "invalid", "2026-09-30T00:00:00Z", True,
                                 float("nan"), float("inf"), 0, -1,
                                 time.time() - 700, time.time() + 60])
def test_invalid_poller_time_never_populates_current_values(tmp_path, poll):
    client = PortsClient([{"ifIndex": 1, "poll_time": poll, "ifInOctets_rate": 10,
                           "ifOutOctets_rate": 20, "ifInErrors": 3}],
                         {"device_id": 7, "ip": IP, "os": "ciscosb"})
    result = network_ports.read_ports(context_for(tmp_path, client)[0], IP)
    port = result["ports"][0]
    assert all(port[field] is None for field in network_ports.POLLER_FIELDS)
    assert port["metricSource"] is None
    assert any("poller" in warning for warning in result["warnings"])


@pytest.mark.parametrize("os_name", ["ciscosb", "hillstone"])
def test_exact_prometheus_wins_and_peer_cannot_supply_fallback(tmp_path, os_name):
    client = PortsClient([{"ifIndex": 1, "poll_time": time.time() - 30,
                           "ifInOctets_rate": 999}],
                         {"device_id": 7, "ip": IP, "os": os_name})
    templates = network_ports.FIREWALL_METRICS if os_name == "hillstone" else network_ports.METRICS
    make_sample = firewall_sample if os_name == "hillstone" else sample
    query = templates["rxBps"].replace("{ip}", IP)
    context, _ = context_for(tmp_path, client, metrics={query: [make_sample(1, 80)]})
    port = network_ports.read_ports(context, IP)["ports"][0]
    assert port["rxBps"] == 80 and port["metricSource"] == "Prometheus"
    assert port["metricAgeSeconds"] is None
    context, _ = context_for(tmp_path, client, metrics={query: [make_sample(1, 80, ip="192.0.2.99")]})
    port = network_ports.read_ports(context, IP)["ports"][0]
    assert port["rxBps"] == 999 * 8 and port["metricSource"] == "LibreNMS poller"
