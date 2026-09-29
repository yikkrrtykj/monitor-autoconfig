import json
from urllib.parse import parse_qs, urlsplit

import pytest

from librenms_client import LibreNMSError
from platform_api import network_inspector, network_read, read_api

from .test_platform_network_read import FakeResponse, make_context


class InspectorClient:
    def __init__(self, devices, ports=None, fail_ports=False):
        self.devices = devices
        self.port_rows = ports or []
        self.fail_ports = fail_ports
        self.requested = []

    def get_device(self, ip):
        self.requested.append(("device", ip))
        return next((device for device in self.devices if device.get("ip") == ip), None)

    def get_device_ports(self, device, columns=None):
        self.requested.append((device, columns))
        if self.fail_ports:
            raise LibreNMSError("offline")
        return self.port_rows


def context_for(tmp_path, client, samples=None, edges=None):
    context, _ = make_context(tmp_path)
    context.topology_path.write_text(json.dumps(edges or []), encoding="utf-8")

    def urlopen(request, *, timeout):
        assert timeout == context.http_timeout
        query = parse_qs(urlsplit(request.full_url).query)["query"][0]
        results = (samples or {}).get(query, [])
        return FakeResponse({"status": "success", "data": {"result": results}})

    return network_read.NetworkReadContext(
        **{**context.__dict__, "librenms_client_factory": lambda: client,
           "urlopen": urlopen, "clock": lambda: context.topology_path.stat().st_mtime}
    )


def sample(target, value, **labels):
    return {"metric": {"target_ip": target, **labels}, "value": [1, str(value)]}


def test_cisco_summary_is_single_node_bounded_and_does_not_fetch_counters(tmp_path):
    ip = "192.0.2.7"
    client = InspectorClient([{"device_id": 7, "ip": ip, "os": "ios",
                               "sysName": "core-a", "hardware": "C9300"}], [
        {"ifOperStatus": "up"}, {"ifOperStatus": 2}, {"ifOperStatus": None},
    ])
    edges = [{"from_ip": ip, "from_port": "Gi1/0/1", "to_ip": "192.0.2.8",
              "to_port": "Gi1/0/2", "from_aggregate_port": "Po1",
              "from_member_ports": ["Gi1/0/1"], "protocols": ["lldp"]}]
    context = context_for(tmp_path, client, edges=edges)
    payload = network_inspector.read_inspector(context, ip)
    assert payload["kind"] == "cisco"
    assert payload["name"] == "core-a"
    assert payload["model"] == "C9300"
    assert payload["online"] == "unknown"
    assert payload["ports"] == {"up": 1, "down": 1, "unknown": 1, "total": 3}
    assert payload["neighbors"][0]["aggregatePort"] == "Po1"
    assert client.requested == [("device", ip), (7, network_inspector.PORT_COLUMNS)]
    assert "ifHighSpeed" not in network_inspector.PORT_COLUMNS
    assert "ifHCInOctets" not in json.dumps(payload)


def test_port_failure_is_partial_and_does_not_claim_zero(tmp_path):
    ip = "192.0.2.7"
    client = InspectorClient([{"device_id": 7, "ip": ip, "os": "ios"}], fail_ports=True)
    payload = network_inspector.read_inspector(context_for(tmp_path, client), ip)
    assert payload["ports"] is None
    assert payload["online"] == "unknown"
    assert payload["degraded"] is True
    assert "端口汇总暂不可用" in payload["warnings"]


def test_prometheus_failure_preserves_identity_and_never_marks_down(tmp_path):
    ip = "192.0.2.7"
    client = InspectorClient([{"device_id": 7, "ip": ip, "os": "ios"}])
    context = context_for(tmp_path, client)
    context = network_read.NetworkReadContext(**{
        **context.__dict__, "urlopen": lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline"))
    })
    payload = network_inspector.read_inspector(context, ip)
    assert payload["online"] == "unknown"
    assert payload["latencySeconds"] is None
    assert payload["ports"] is None
    assert payload["degraded"] is True
    assert "当前探测状态暂不可用" in payload["warnings"]


def test_missing_topology_is_partial_without_zero_link_claim(tmp_path):
    ip = "192.0.2.7"
    context = context_for(tmp_path, InspectorClient([{"device_id": 7, "ip": ip}]))
    context.topology_path.unlink()
    payload = network_inspector.read_inspector(context, ip)
    assert payload["neighbors"] == []
    assert "邻接资料暂不可用" in payload["warnings"]
    assert payload["degraded"] is True


def test_librenms_failure_keeps_known_topology_node_partial(tmp_path):
    ip = "192.0.2.7"
    client = InspectorClient([])
    client.get_device = lambda _ip: (_ for _ in ()).throw(LibreNMSError("offline"))
    context = context_for(tmp_path, client, edges=[
        {"from_ip": ip, "to_ip": "192.0.2.8", "from_port": "Gi1"}
    ])
    payload = network_inspector.read_inspector(context, ip)
    assert payload["kind"] == "unknown"
    assert payload["ports"] is None
    assert payload["online"] == "unknown"
    assert payload["neighbors"][0]["peerIp"] == "192.0.2.8"
    assert payload["degraded"] is True


def test_hillstone_role_is_unavailable_even_when_unit_probe_up(tmp_path):
    ip = "192.0.2.11"
    client = InspectorClient([{"device_id": 11, "ip": ip, "os": "hillstone"}])
    query = (f'probe_success{{job=~"infra-core-ping|infra-dist-ping|infra-fw-ping|'
             f'infra-fw-unit-ping",target_ip="{ip}"}}')
    payload = network_inspector.read_inspector(
        context_for(tmp_path, client, {query: [sample(ip, 1)]}), ip)
    assert payload["online"] == "up"
    assert payload["haRole"] == "unavailable"


def test_unknown_probe_value_never_becomes_down(tmp_path):
    ip = "192.0.2.7"
    client = InspectorClient([{"device_id": 7, "ip": ip, "os": "ios"}])
    query = (f'probe_success{{job=~"infra-core-ping|infra-dist-ping|infra-fw-ping|'
             f'infra-fw-unit-ping",target_ip="{ip}"}}')
    payload = network_inspector.read_inspector(
        context_for(tmp_path, client, {query: [sample(ip, "NaN")]}), ip)
    assert payload["online"] == "unknown"


def test_unifi_uses_existing_uptime_and_clients_without_controller_poll(tmp_path):
    ip = "192.0.2.30"
    client = InspectorClient([])
    samples = {
        f'unpoller_device_info{{type="uap",ip="{ip}"}}': [
            sample(ip, 1, ip=ip, name="AP-A", model="U6")],
        'unpoller_device_uptime_seconds{type="uap",name="AP-A"}': [
            sample(ip, 100, name="AP-A")],
        'sum by (name) (unpoller_device_stations{type="uap",name="AP-A"})': [
            sample(ip, 5, name="AP-A")],
    }
    payload = network_inspector.read_inspector(context_for(tmp_path, client, samples), ip)
    assert payload["kind"] == "unifi-ap"
    assert payload["online"] == "up"
    assert payload["clients"] == 5
    assert payload["uplink"] is None and payload["radio"] is None
    assert client.requested == [("device", ip)]


def test_unifi_also_uses_ap_metrics_when_present_in_librenms_inventory(tmp_path):
    ip = "192.0.2.30"
    client = InspectorClient([{"device_id": 30, "ip": ip, "os": "unifi",
                               "sysName": "inventory-name"}])
    info = f'unpoller_device_info{{type="uap",ip="{ip}"}}'
    payload = network_inspector.read_inspector(context_for(tmp_path, client, {
        info: [sample(ip, 1, ip=ip, name="AP-A", model="U6")],
    }), ip)
    assert payload["kind"] == "unifi-ap"
    assert payload["name"] == "AP-A"
    assert payload["ports"] is None
    assert client.requested == [("device", ip)]


def test_unifi_unknown_label_without_uptime_is_not_down(tmp_path):
    ip = "192.0.2.30"
    client = InspectorClient([])
    info = f'unpoller_device_info{{type="uap",ip="{ip}"}}'
    payload = network_inspector.read_inspector(context_for(tmp_path, client, {
        info: [sample(ip, 1, ip=ip, name="AP-A", status="unknown")],
    }), ip)
    assert payload["online"] == "unknown"
    assert payload["degraded"] is True


def test_invalid_and_unknown_node_rejected(tmp_path):
    context = context_for(tmp_path, InspectorClient([]))
    with pytest.raises(network_read.NetworkReadError) as invalid:
        network_inspector.read_inspector(context, "127.0.0.1/../../x")
    assert invalid.value.status == 400
    with pytest.raises(network_read.NetworkReadError) as missing:
        network_inspector.read_inspector(context, "192.0.2.99")
    assert missing.value.status == 404


def test_unifi_inventory_failure_does_not_look_like_missing_node(tmp_path):
    context = context_for(tmp_path, InspectorClient([]))
    context = network_read.NetworkReadContext(**{
        **context.__dict__, "urlopen": lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline"))
    })
    with pytest.raises(network_read.NetworkReadError) as unavailable:
        network_inspector.read_inspector(context, "192.0.2.30")
    assert unavailable.value.status == 503


def test_port_count_limit_fails_partial(tmp_path):
    ip = "192.0.2.7"
    client = InspectorClient([{"device_id": 7, "ip": ip}],
                             [{"ifOperStatus": 1}] * (network_inspector.PORT_LIMIT + 1))
    payload = network_inspector.read_inspector(context_for(tmp_path, client), ip)
    assert payload["ports"] is None
    assert payload["degraded"] is True


def test_single_node_get_route_authenticates_before_read(monkeypatch, tmp_path):
    context = context_for(tmp_path, InspectorClient([]))
    events = []

    class Handler:
        def _send_json(self, payload, status=200):
            events.append(("response", status, payload))

    monkeypatch.setattr(network_inspector, "read_inspector", lambda _ctx, ip: (
        events.append(("read", ip)) or {"ok": True, "ip": ip}
    ))
    router = read_api.ReadApiContext(
        event_config_context=None, transaction_context=None, incident_context=None,
        iperf_runtime_context=None, dhcp_settings_context=None, dhcp_runtime_context=None,
        bridge_url="", require_auth=lambda _handler: {}, read_json_file=lambda *_: None,
        stamp=lambda: "", network_context=context,
        network_require_auth=lambda _handler: events.append(("auth",)),
    )
    read_api.handle_get(Handler(), "/network/nodes/192.0.2.7/inspector", router)
    assert events == [("auth",), ("read", "192.0.2.7"),
                      ("response", 200, {"ok": True, "ip": "192.0.2.7"})]
