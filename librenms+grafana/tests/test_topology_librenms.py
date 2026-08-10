"""LibreNMS-first topology adapter and fallback behavior tests."""
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys

import pytest

from librenms_client import LibreNMSUnavailable


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "librenms"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location(
    "generate_topology_edges_librenms", ROOT / "generate-topology-edges.py"
)
gte = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(gte)


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FixtureClient:
    def __init__(self):
        self.devices = fixture("devices.json")["devices"]
        self.payloads = {
            ("192.168.10.254", "ports"): fixture("core-ports.json")["ports"],
            ("192.168.10.254", "links"): fixture("core-links.json")["links"],
            ("192.168.10.254", "stack"): fixture("core-port-stack.json")["mappings"],
            ("192.168.10.45", "ports"): fixture("access-ports.json")["ports"],
            ("192.168.10.45", "links"): fixture("access-links.json")["links"],
            ("192.168.10.45", "stack"): fixture("access-port-stack.json")["mappings"],
        }
        self.failures = {}
        self.calls = []
        self.request_count = 0

    @staticmethod
    def _ip(device):
        return str(device.get("ip") or device.get("hostname"))

    def _read(self, device, component):
        ip = self._ip(device)
        self.calls.append((ip, component))
        self.request_count += 1
        failure = self.failures.get((ip, component))
        if failure:
            raise failure
        return [dict(item) for item in self.payloads[(ip, component)]]

    def list_devices(self):
        self.calls.append(("*", "devices"))
        self.request_count += 1
        return [dict(item) for item in self.devices]

    def resolve_device(self, identifier):
        raw = str(identifier)
        for device in self.devices:
            if raw in {
                str(device.get("device_id")),
                str(device.get("hostname")),
                str(device.get("ip")),
                str(device.get("sysName")),
            }:
                return dict(device)
        raise LibreNMSUnavailable("device missing")

    def get_device_ports(self, device, columns=None, with_vlans=False):
        assert columns == "port_id,device_id,ifIndex,ifName,ifDescr,ifAlias,ifOperStatus"
        assert with_vlans is False
        return self._read(device, "ports")

    def get_device_links(self, device):
        return self._read(device, "links")

    def get_device_port_stack(self, device, valid_mappings=True):
        assert valid_mappings is True
        return self._read(device, "stack")


def collect_fixture_devices(client, monkeypatch, mode="hybrid"):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("complete LibreNMS fixture must not use adjacency SNMP")

    monkeypatch.setattr(gte, "poll_snmp_neighbors", forbidden)
    monkeypatch.setattr(gte, "poll_snmp_lag", forbidden)
    return {
        ip: gte.poll_device_librenms(
            ip, "secret-community", client, collect_arp=False, mode=mode
        )
        for ip in ("192.168.10.254", "192.168.10.45")
    }


def test_hybrid_is_default_and_invalid_value_falls_back(monkeypatch, capsys):
    monkeypatch.delenv("TOPOLOGY_DATA_SOURCE", raising=False)
    assert gte.topology_data_source() == "hybrid"
    monkeypatch.setenv("TOPOLOGY_DATA_SOURCE", "future-mode")
    assert gte.topology_data_source() == "hybrid"
    assert "future-mode" in capsys.readouterr().err


def test_direct_snmp_mode_never_calls_librenms(monkeypatch):
    sentinel = gte._empty_device("192.168.10.254")
    sentinel["source"] = {key: "direct-snmp" for key in ("ports", "links", "lag")}
    monkeypatch.setattr(gte, "poll_device_snmp", lambda *_args, **_kwargs: sentinel)

    class ForbiddenClient:
        def __getattr__(self, _name):
            raise AssertionError("direct-snmp must not inspect LibreNMS")

    result = gte.collect_device_by_source(
        "192.168.10.254", "community", False, "direct-snmp", ForbiddenClient(), True
    )
    assert result is sentinel


def test_direct_snmp_baseline_counts_one_get_and_ten_walks(monkeypatch):
    class Result:
        stdout = ""

    monkeypatch.setattr(gte.subprocess, "run", lambda *_args, **_kwargs: Result())
    monkeypatch.setenv("TOPOLOGY_SNMP_DELAY_MS", "0")
    gte.reset_collection_stats()
    gte.poll_device_snmp("192.168.10.254", "community", collect_arp=False)
    stats = gte.collection_stats_snapshot()
    assert stats["direct_snmp_gets"] == 1
    assert stats["direct_snmp_walks"] == 10
    assert stats["server_snmp_gets"] == 0
    assert stats["server_snmp_walks"] == 0


def test_librenms_fixture_reaches_final_edge_without_adjacency_snmp(monkeypatch):
    client = FixtureClient()
    gte.reset_collection_stats()
    devices = collect_fixture_devices(client, monkeypatch, mode="librenms")

    edges, placeholders = gte.build_edges(devices, gte.build_name_index(devices))

    assert placeholders == []
    assert len(edges) == 1
    edge = edges[0]
    assert edge == {
        "from_ip": "192.168.10.254",
        "from_sysname": "Core",
        "from_port": "Po11",
        "from_ifindex": 400,
        "to_ip": "192.168.10.45",
        "to_sysname": "Access-Stack",
        "to_port": "Po11",
        "to_ifindex": 500,
        "from_aggregate_port": "Po11",
        "from_member_ports": ["Te1/0/2", "Te2/0/2"],
        "to_aggregate_port": "Po11",
        "to_member_ports": ["Gi1/0/24", "Gi2/0/24"],
    }
    # Fixture IDs deliberately differ: LibreNMS port_id 1001/2001 maps to
    # IF-MIB ifIndex 400/500 and is never copied into the edge as ifIndex.
    assert edge["from_ifindex"] != 1001
    assert edge["to_ifindex"] != 2001
    assert gte.collection_stats_snapshot()["direct_snmp_walks"] == 0
    assert client.calls.count(("192.168.10.254", "ports")) == 1
    assert client.calls.count(("192.168.10.254", "links")) == 1
    assert client.calls.count(("192.168.10.254", "stack")) == 1


def test_lldp_and_cdp_fixture_observations_dedupe_to_one_edge(monkeypatch):
    client = FixtureClient()
    devices = collect_fixture_devices(client, monkeypatch)
    assert devices["192.168.10.254"]["neighbors"][0]["protocol"] == "lldp"
    assert devices["192.168.10.45"]["neighbors"][0]["protocol"] == "cdp"
    edges, _ = gte.build_edges(devices, gte.build_name_index(devices))
    assert len(edges) == 1


def test_remote_port_name_fallback_uses_existing_endpoint_resolver(monkeypatch):
    client = FixtureClient()
    for key in (("192.168.10.254", "links"), ("192.168.10.45", "links")):
        for link in client.payloads[key]:
            link.pop("remote_port_id", None)
    devices = collect_fixture_devices(client, monkeypatch)
    edges, _ = gte.build_edges(devices, gte.build_name_index(devices))
    assert [(edge["from_ifindex"], edge["to_ifindex"]) for edge in edges] == [(400, 500)]


def test_inactive_librenms_links_are_not_current_edges(monkeypatch):
    client = FixtureClient()
    for key in (("192.168.10.254", "links"), ("192.168.10.45", "links")):
        client.payloads[key][0]["active"] = 0
    devices = collect_fixture_devices(client, monkeypatch)
    edges, _ = gte.build_edges(devices, gte.build_name_index(devices))
    assert edges == []


def test_missing_active_field_uses_port_oper_status(monkeypatch):
    client = FixtureClient()
    for key in (("192.168.10.254", "links"), ("192.168.10.45", "links")):
        client.payloads[key][0].pop("active", None)
    devices = collect_fixture_devices(client, monkeypatch)
    edges, _ = gte.build_edges(devices, gte.build_name_index(devices))
    assert len(edges) == 1


def test_down_librenms_port_is_not_a_current_edge(monkeypatch):
    client = FixtureClient()
    client.payloads[("192.168.10.45", "ports")][0]["ifOperStatus"] = "down"
    devices = collect_fixture_devices(client, monkeypatch)
    edges, _ = gte.build_edges(devices, gte.build_name_index(devices))
    assert edges == []


def test_complete_two_member_port_stack_skips_snmp_supplement(monkeypatch):
    client = FixtureClient()
    monkeypatch.setattr(
        gte,
        "poll_snmp_lag",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("complete port_stack must not run LAG SNMP")
        ),
    )
    device = gte.poll_device_librenms(
        "192.168.10.254", "community", client, collect_arp=False, mode="hybrid"
    )
    assert device["ifstack"] == {400: [102, 202]}
    assert device["source"]["lag"] == "librenms"


def test_incomplete_port_stack_uses_only_lag_supplement(monkeypatch):
    client = FixtureClient()
    client.payloads[("192.168.10.254", "stack")] = [
        client.payloads[("192.168.10.254", "stack")][0]
    ]
    calls = []

    def supplement(ip, community, ifname, ifoper, initial=None):
        calls.append((ip, community, initial))
        return gte.merge_aggregate_member_maps(initial, {400: [102, 202]})

    monkeypatch.setattr(gte, "poll_snmp_lag", supplement)
    monkeypatch.setattr(
        gte,
        "poll_snmp_neighbors",
        lambda *_args: (_ for _ in ()).throw(AssertionError("links must stay LibreNMS")),
    )
    device = gte.poll_device_librenms(
        "192.168.10.254", "community", client, collect_arp=False, mode="hybrid"
    )
    assert calls == [("192.168.10.254", "community", {400: [102]})]
    assert device["ifstack"] == {400: [102, 202]}
    assert device["source"] == {"ports": "librenms", "links": "librenms", "lag": "hybrid"}


def test_inactive_port_stack_mapping_is_not_used_as_a_member(monkeypatch):
    client = FixtureClient()
    client.payloads[("192.168.10.254", "stack")][1]["ifStackStatus"] = "inactive"
    monkeypatch.setattr(
        gte,
        "poll_snmp_lag",
        lambda _ip, _community, _ifname, _ifoper, initial=None: initial,
    )
    device = gte.poll_device_librenms(
        "192.168.10.254", "community", client, collect_arp=False, mode="hybrid"
    )
    assert device["ifstack"] == {400: [102]}
    assert device["source"]["lag"] == "hybrid"


def test_one_device_links_failure_falls_back_only_that_device(monkeypatch):
    client = FixtureClient()
    client.failures[("192.168.10.45", "links")] = LibreNMSUnavailable(
        "token=never-log-this"
    )
    fallbacks = []

    def neighbors(ip, _community):
        fallbacks.append(ip)
        return {
            "loc_port_desc": {}, "rem_sys": {}, "rem_port_desc": {},
            "rem_port_id": {}, "cdp_device_id": {},
            "cdp_device_port": {}, "cdp_address": {},
        }

    monkeypatch.setattr(gte, "poll_snmp_neighbors", neighbors)
    monkeypatch.setattr(
        gte,
        "poll_snmp_lag",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("LAG is complete")),
    )
    core = gte.poll_device_librenms(
        "192.168.10.254", "community", client, collect_arp=False, mode="hybrid"
    )
    access = gte.poll_device_librenms(
        "192.168.10.45", "community", client, collect_arp=False, mode="hybrid"
    )
    assert fallbacks == ["192.168.10.45"]
    assert core["source"]["links"] == "librenms"
    assert access["source"]["links"] == "direct-snmp"


def test_freshness_states_are_independent_and_missing_is_unknown():
    now = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
    assert gte.librenms_freshness(now - timedelta(seconds=600), 600, now=now) == "fresh"
    assert gte.librenms_freshness(now - timedelta(seconds=601), 600, now=now) == "stale"
    assert gte.librenms_freshness(None, 600, now=now) == "unknown"


def test_explicit_fresh_poll_and_discovery_metadata_stays_librenms(monkeypatch):
    client = FixtureClient()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    client.devices[0]["last_polled"] = now
    client.devices[0]["last_discovered"] = now
    monkeypatch.setattr(
        gte,
        "poll_device_snmp",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("fresh API data")),
    )
    monkeypatch.setattr(
        gte,
        "poll_snmp_lag",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("complete API LAG")),
    )
    device = gte.poll_device_librenms(
        "192.168.10.254", "community", client, collect_arp=False, mode="hybrid"
    )
    assert device["freshness"] == {"poll": "fresh", "discovery": "fresh"}
    assert device["source"] == {"ports": "librenms", "links": "librenms", "lag": "librenms"}


def test_explicit_stale_poll_data_falls_back_full_device(monkeypatch):
    client = FixtureClient()
    client.devices[0]["last_polled"] = "2000-01-01 00:00:00"
    sentinel = gte._empty_device("192.168.10.254")
    sentinel["source"] = {key: "direct-snmp" for key in ("ports", "links", "lag")}
    calls = []
    monkeypatch.setattr(
        gte,
        "poll_device_snmp",
        lambda ip, _community, collect_arp=True: calls.append((ip, collect_arp)) or sentinel,
    )
    result = gte.poll_device_librenms(
        "192.168.10.254", "community", client, collect_arp=False, mode="hybrid"
    )
    assert result is sentinel
    assert calls == [("192.168.10.254", False)]
    assert not any(component == "ports" for _ip, component in client.calls)


def test_explicit_stale_discovery_falls_back_links_and_lag_only(monkeypatch):
    client = FixtureClient()
    client.devices[0]["last_discovered"] = "2000-01-01 00:00:00"
    calls = []
    monkeypatch.setattr(
        gte, "poll_snmp_neighbors",
        lambda ip, _community: calls.append((ip, "links")) or {
            "loc_port_desc": {}, "rem_sys": {}, "rem_port_desc": {},
            "rem_port_id": {}, "cdp_device_id": {},
            "cdp_device_port": {}, "cdp_address": {},
        },
    )
    monkeypatch.setattr(
        gte, "poll_snmp_lag",
        lambda ip, _community, *_args, **_kwargs: calls.append((ip, "lag")) or {},
    )
    device = gte.poll_device_librenms(
        "192.168.10.254", "community", client, collect_arp=False, mode="hybrid"
    )
    assert calls == [("192.168.10.254", "links"), ("192.168.10.254", "lag")]
    assert device["source"] == {
        "ports": "librenms", "links": "direct-snmp", "lag": "direct-snmp"
    }
    assert not any(component in ("links", "stack") for _ip, component in client.calls)


class MissingTokenClient:
    request_count = 0

    @staticmethod
    def resolve_device(_identifier):
        raise LibreNMSUnavailable("token=secret community=secret")


def test_missing_token_hybrid_falls_back_without_logging_secret(monkeypatch, capsys):
    sentinel = gte._empty_device("192.168.10.254")
    monkeypatch.setattr(gte, "poll_device_snmp", lambda *_args, **_kwargs: sentinel)
    result = gte.poll_device_librenms(
        "192.168.10.254", "private-community", MissingTokenClient(),
        collect_arp=False, mode="hybrid",
    )
    assert result is sentinel
    log = capsys.readouterr().err
    assert "LibreNMSUnavailable" in log
    assert "secret" not in log
    assert "private-community" not in log


def test_snmp_failure_log_never_contains_community(monkeypatch, capsys):
    def timeout(command, **_kwargs):
        raise subprocess.TimeoutExpired(command, 1)

    monkeypatch.setattr(gte.subprocess, "run", timeout)
    monkeypatch.setenv("TOPOLOGY_SNMP_DELAY_MS", "0")
    assert gte.snmpwalk("192.168.10.254", "private-community", gte.IF_NAME_OID) == ""
    log = capsys.readouterr().err
    assert "TimeoutExpired" in log
    assert "private-community" not in log


def test_librenms_only_failure_never_calls_direct_snmp_and_retains_history(monkeypatch):
    monkeypatch.setattr(
        gte,
        "poll_device_snmp",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("librenms-only must not use adjacency SNMP")
        ),
    )
    device = gte.poll_device_librenms(
        "192.168.10.254", "community", MissingTokenClient(),
        collect_arp=False, mode="librenms",
    )
    assert device["ifname"] == {}
    cached = [{
        "from_ip": "192.168.10.254", "from_port": "Po11", "from_ifindex": 400,
        "to_ip": "192.168.10.45", "to_port": "Po11", "to_ifindex": 500,
        "last_seen": 100.0,
    }]
    retained = gte.retain_cached_network_edges(
        [], cached, ["192.168.10.254", "192.168.10.45"],
        now=101, retention_seconds=86400,
    )
    assert retained[0]["stale"] is True


def test_full_librenms_cycle_reports_zero_adjacency_snmp_and_compatible_schema(
    monkeypatch, tmp_path, capsys
):
    client = FixtureClient()
    commands = []

    class Result:
        stdout = ""

    def run(command, **_kwargs):
        commands.append(command)
        return Result()

    monkeypatch.setattr(gte, "LibreNMSClient", lambda: client)
    monkeypatch.setattr(gte.subprocess, "run", run)
    monkeypatch.setenv("TOPOLOGY_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("TOPOLOGY_DEVICES", "192.168.10.254,192.168.10.45")
    monkeypatch.setenv("TOPOLOGY_DATA_SOURCE", "hybrid")
    monkeypatch.setenv("TOPOLOGY_SNMP_DELAY_MS", "0")
    monkeypatch.setenv("TOPOLOGY_POLL_WORKERS", "1")
    monkeypatch.setenv("CORE_SWITCH_PING", "")
    monkeypatch.setenv("FIREWALL_PING", "")
    monkeypatch.setenv("SERVER_PING", "")

    assert gte._run_collection() == 0

    log = capsys.readouterr().err
    assert "collection stats: api_requests=7 snmp_walks=0 snmp_gets=0" in log
    assert "source summary: librenms=2 hybrid=0 direct-snmp=0" in log
    # No server is configured, so the unchanged server-ARP stage also has no
    # work. The complete fixture cycle performs no direct SNMP operation.
    assert commands == []
    edges = json.loads((tmp_path / "edges.json").read_text(encoding="utf-8"))
    assert len(edges) == 1
    assert "source" not in edges[0]
    assert set(edges[0]) == {
        "from_ip", "from_sysname", "from_port", "from_ifindex",
        "to_ip", "to_sysname", "to_port", "to_ifindex",
        "from_aggregate_port", "from_member_ports",
        "to_aggregate_port", "to_member_ports", "last_seen", "stale",
    }
