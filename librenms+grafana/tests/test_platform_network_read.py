import hashlib
import json
import os
from pathlib import Path

import pytest

from platform_api import network_read


class FakeResponse:
    def __init__(self, payload):
        self.raw = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit=-1):
        return self.raw if limit < 0 else self.raw[:limit]


class FakeLibreNMS:
    def __init__(self, devices=None, error=None):
        self.devices = [] if devices is None else devices
        self.error = error
        self.strict = None

    def list_devices(self, strict=False):
        self.strict = strict
        if self.error:
            raise self.error
        return list(self.devices)


def make_context(tmp_path: Path, *, devices=None, prom=None, now=2_000.0):
    topology = tmp_path / "edges.json"
    inventory = tmp_path / "isp_targets.json"
    state = tmp_path / "isp-discovery-state.json"
    env_path = tmp_path / ".env"
    topology.write_text("[]", encoding="utf-8")
    inventory_raw = b"[]"
    inventory.write_bytes(inventory_raw)
    state.write_text(json.dumps({
        "status": "disabled",
        "inventory_count": 0,
        "inventory_sha256": hashlib.sha256(inventory_raw).hexdigest(),
    }), encoding="utf-8")
    env_path.write_text("", encoding="utf-8")
    client = FakeLibreNMS(devices=devices)
    prom_payload = prom if prom is not None else {
        "status": "success", "data": {"result": []},
    }
    context = network_read.NetworkReadContext(
        librenms_client_factory=lambda: client,
        prometheus_url="http://prometheus:9090",
        topology_path=topology,
        isp_inventory_path=inventory,
        isp_state_path=state,
        env_path=env_path,
        urlopen=lambda *_args, **_kwargs: FakeResponse(prom_payload),
        clock=lambda: now,
        topology_stale_seconds=300,
    )
    return context, client


def test_devices_require_strict_inventory_and_map_probe_status(tmp_path):
    context, client = make_context(
        tmp_path,
        devices=[{
            "device_id": 7,
            "hostname": "192.0.2.7",
            "ip": "192.0.2.7",
            "sysName": "edge-7",
        }],
        prom={
            "status": "success",
            "data": {"result": [{
                "metric": {"target_ip": "192.0.2.7", "job": "infra-dist-ping"},
                "value": [1, "1"],
            }]},
        },
    )

    payload = network_read.read_devices(context)

    assert client.strict is True
    assert payload == {
        "ok": True,
        "degraded": False,
        "count": 1,
        "devices": [{
            "id": 7,
            "name": "edge-7",
            "hostname": "192.0.2.7",
            "ip": "192.0.2.7",
            "status": "up",
        }],
        "warnings": [],
    }


def test_devices_prometheus_failure_is_degraded_with_unknown_status(tmp_path):
    context, _ = make_context(
        tmp_path,
        devices=[{"device_id": 1, "hostname": "192.0.2.1", "ip": "192.0.2.1"}],
    )
    context = network_read.NetworkReadContext(
        **{**context.__dict__, "urlopen": lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("down"))}
    )

    payload = network_read.read_devices(context)

    assert payload["degraded"] is True
    assert payload["devices"][0]["status"] == "unknown"


def test_topology_empty_snapshot_uses_snapshot_mtime(tmp_path):
    context, _ = make_context(tmp_path, now=2_000.0)
    os.utime(context.topology_path, (1_900.0, 1_900.0))

    payload = network_read.read_topology(context)

    assert payload["generatedAt"] == "1970-01-01T00:31:40Z"
    assert payload["nodeCount"] == 0
    assert payload["edgeCount"] == 0
    assert payload["stale"] is False


@pytest.mark.parametrize("content", [b"{", b"{}", b'["bad"]'])
def test_topology_malformed_is_explicit_failure(tmp_path, content):
    context, _ = make_context(tmp_path)
    context.topology_path.write_bytes(content)

    with pytest.raises(network_read.NetworkReadError) as raised:
        network_read.read_topology(context)

    assert raised.value.status == 503
    assert raised.value.payload["code"] == "topology_malformed"


@pytest.mark.parametrize("edge", [
    {"from_ip": [], "to_ip": "192.0.2.2"},
    {"from_ip": {}, "to_ip": "192.0.2.2"},
    {"from_ip": 123, "to_ip": "192.0.2.2"},
    {"from_ip": True, "to_ip": "192.0.2.2"},
    {"from_ip": "192.0.2.1", "to_ip": []},
    {"from_ip": "192.0.2.1", "to_ip": {}},
    {"from_ip": "192.0.2.1", "to_ip": 123},
    {"from_ip": "", "to_ip": "192.0.2.2"},
    {"from_ip": "   ", "to_ip": "192.0.2.2"},
    {"from_ip": "192.0.2.1", "to_ip": ""},
    {"from_ip": "192.0.2.1", "to_ip": "   "},
])
def test_topology_rejects_non_string_or_blank_endpoints(tmp_path, edge):
    context, _ = make_context(tmp_path)
    context.topology_path.write_text(json.dumps([edge]), encoding="utf-8")

    with pytest.raises(network_read.NetworkReadError) as raised:
        network_read.read_topology(context)

    assert raised.value.status == 503
    assert raised.value.payload["code"] == "topology_malformed"


def test_topology_accepts_nonempty_string_endpoints(tmp_path):
    context, _ = make_context(tmp_path)
    edge = {"from_ip": " 192.0.2.1 ", "to_ip": "192.0.2.2"}
    context.topology_path.write_text(json.dumps([edge]), encoding="utf-8")

    payload = network_read.read_topology(context)

    assert payload["edgeCount"] == 1
    assert payload["nodes"] == ["192.0.2.1", "192.0.2.2"]
    assert payload["edges"] == [edge]


def test_isp_keeps_targetless_auto_and_reads_manual_applied_env(tmp_path):
    context, _ = make_context(tmp_path)
    inventory = [{
        "targets": [],
        "labels": {
            "display_name": "PPPoE-A",
            "metric_name": "pppoe0",
            "metric_target": "192.0.2.1",
            "metric_ifindex": "7",
            "wan_ip": "203.0.113.20",
            "discovery_source": "direct-snmp",
        },
    }]
    raw = json.dumps(inventory, separators=(",", ":")).encode("utf-8")
    context.isp_inventory_path.write_bytes(raw)
    context.isp_state_path.write_text(json.dumps({
        "status": "disabled",
        "inventory_count": 1,
        "inventory_sha256": hashlib.sha256(raw).hexdigest(),
    }), encoding="utf-8")
    context.env_path.write_text("ISP_PING=Backup:198.51.100.1\n", encoding="utf-8")

    payload = network_read.read_isp(context)

    assert payload["count"] == 2
    assert payload["isps"][0]["target"] is None
    assert payload["isps"][0]["status"] == "unknown"
    assert payload["isps"][1] == {
        "name": "Backup",
        "target": "198.51.100.1",
        "status": "unknown",
        "source": "manual",
        "wanIp": None,
        "metricTarget": None,
        "metricIfindex": None,
    }


def test_isp_mismatch_rereads_once_then_marks_degraded(monkeypatch, tmp_path):
    context, _ = make_context(tmp_path)
    calls = []

    def inconsistent(_context):
        calls.append(True)
        return [], {"status": "ok", "inventory_count": 1, "inventory_sha256": "bad"}, b"[]"

    monkeypatch.setattr(network_read, "_read_isp_pair", inconsistent)

    payload = network_read.read_isp(context)

    assert len(calls) == 2
    assert payload["degraded"] is True
    assert payload["stale"] is True


def test_overview_keeps_failed_domain_null_and_fails_when_all_unavailable(monkeypatch, tmp_path):
    context, _ = make_context(tmp_path)
    monkeypatch.setattr(network_read, "read_devices", lambda _context: {"ok": True, "degraded": False, "warnings": [], "count": 0})
    monkeypatch.setattr(network_read, "read_topology", lambda _context: (_ for _ in ()).throw(network_read.NetworkReadError(503, "topology_unavailable", "missing")))
    monkeypatch.setattr(network_read, "read_isp", lambda _context: {"ok": True, "degraded": False, "warnings": [], "count": 0})

    payload = network_read.read_overview(context)

    assert payload["degraded"] is True
    assert payload["topology"] is None
    assert payload["devices"]["count"] == 0

    failure = lambda _context: (_ for _ in ()).throw(network_read.NetworkReadError(503, "unavailable", "down"))
    monkeypatch.setattr(network_read, "read_devices", failure)
    monkeypatch.setattr(network_read, "read_topology", failure)
    monkeypatch.setattr(network_read, "read_isp", failure)
    with pytest.raises(network_read.NetworkReadError) as raised:
        network_read.read_overview(context)
    assert raised.value.payload["code"] == "network_unavailable"
