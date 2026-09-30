import json
import subprocess
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path

import yaml
from urllib.parse import parse_qs, urlsplit

import pytest

from librenms_client import LibreNMSError
from platform_api import network_port_history as history, network_read, read_api
from .test_platform_network_read import make_context
from .test_librenms_client import FakeResponse, attach_sequence, make_client

IP = "192.0.2.7"
END = 1800000000


class Client:
    def __init__(self):
        self.device = {"device_id": 7, "ip": IP, "hostname": "switch.example", "os": "iosxe", "hardware": "C9300-48P"}
        self.ports = [{"device_id": 7, "ifIndex": 101, "port_id": 42, "ifName": "Gi1/0/1"}]
        self.calls = []

    def get_device(self, ip):
        self.calls.append(("device", ip))
        return self.device

    def get_device_ports(self, device, *, columns):
        self.calls.append(("ports", device["device_id"], columns))
        return self.ports


def setup(tmp_path):
    context, _ = make_context(tmp_path)
    client = Client()
    base = tmp_path / "rrd"
    folder = base / client.device["hostname"]
    folder.mkdir(parents=True)
    (folder / "port-id42.rrd").touch()
    return replace(context, librenms_client_factory=lambda: client,
                   rrd_base_path=base, clock=lambda: END), client


def raw(rx="0", tx="2"):
    return f"  OTHER OUTOCTETS INOCTETS\n\n{END - 600}: 999 {tx} {rx}\n{END - 300}: 999 NaN UNKNOWN\n{END}: 999 3 4\n"


@pytest.mark.parametrize("os_name", ["ios", "iosxe", "ciscosb", "stoneos", "hillstone"])
def test_exact_identity_and_native_resolution_zero_bits_conversion(tmp_path, monkeypatch, os_name):
    context, client = setup(tmp_path)
    client.device["os"] = os_name
    calls = []
    def run(argv, **options):
        calls.append((argv, options))
        options["stdout"].write(raw().encode())
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(history.subprocess, "run", run)
    result = history.read_port_history(context, IP, "101")
    assert client.calls == [("device", IP), ("ports", 7, "device_id,ifIndex,port_id")]
    argv, options = calls[0]
    assert argv == ["rrdtool", "fetch", str(context.rrd_base_path.resolve() / "switch.example" / "port-id42.rrd"),
                    "AVERAGE", "--daemon", "librenms-rrdcached:42217", "--start", str(END - 900), "--end", str(END)]
    assert options["timeout"] == 5 and options["shell"] is False
    assert options["stdin"] == subprocess.DEVNULL
    assert result["source"] == "LibreNMS RRD" and result["step"] == 300
    assert result["rx"] == [{"t": END - 600, "v": 0}, {"t": END, "v": 32}]
    assert result["tx"] == [{"t": END - 600, "v": 16}, {"t": END, "v": 24}]
    assert result["coverage"] == {"rx": "available", "tx": "available"}
    assert "switch.example" not in json.dumps(result) and "port_id" not in result


@pytest.mark.parametrize("mutation", [
    "no_device", "other_ip", "unsupported", "bad_device_id", "duplicate", "other_index", "other_device",
    "bad_port_id", "missing_file", "overflow", "traversal", "slash", "backslash", "absolute", "hostname_empty",
])
def test_no_identity_guess_or_path_escape(tmp_path, monkeypatch, mutation):
    context, client = setup(tmp_path)
    if mutation == "no_device": client.device = None
    elif mutation == "other_ip": client.device["ip"] = "192.0.2.99"
    elif mutation == "unsupported": client.device["os"] = "linux"
    elif mutation == "bad_device_id": client.device["device_id"] = "bad"
    elif mutation == "duplicate": client.ports.append(dict(client.ports[0]))
    elif mutation == "other_index": client.ports[0]["ifIndex"] = 102
    elif mutation == "other_device": client.ports[0]["device_id"] = 8
    elif mutation == "bad_port_id": client.ports[0]["port_id"] = "../42"
    elif mutation == "missing_file": client.ports[0]["port_id"] = 43
    elif mutation == "overflow": client.ports *= 2049
    else:
        client.device["hostname"] = {"traversal": "../switch.example", "slash": "a/b", "backslash": "a\\b", "absolute": "/tmp", "hostname_empty": ""}[mutation]
    monkeypatch.setattr(history, "_fetch", lambda *_: pytest.fail("must not open an RRD"))
    result = history.read_port_history(context, IP, "101")
    assert not result["rx"] and not result["tx"]


def test_symlink_escape(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    outside = tmp_path / "outside.rrd"
    outside.touch()
    folder = base / "switch"
    folder.mkdir()
    try:
        (folder / "port-id1.rrd").symlink_to(outside)
    except OSError:
        pytest.skip("symlink permission unavailable on this host")
    assert history._rrd_path(base, "switch", 1) is None


@pytest.mark.parametrize("ip,index", [("bad", "101"), (IP, "0"), (IP, "-1"), (IP, "1.5"), (IP, "2147483648")])
def test_invalid_route_identity_precedes_librenms(tmp_path, ip, index):
    context, client = setup(tmp_path)
    with pytest.raises(network_read.NetworkReadError) as error:
        history.read_port_history(context, ip, index)
    assert error.value.status == 400 and client.calls == []


@pytest.mark.parametrize("output", [
    "INOCTETS OUTOCTETS\n", raw("NaN", "UNKNOWN").replace(" 3 4", " NaN NaN"),
    raw().replace(str(END), str(END - 3600)),
    "INOCTETS OUTOCTETS\n1: 0 0\n1: 2 2\n", "INOCTETS INOCTETS\n1: 0 0\n2: 1 1\n",
    "malformed",
])
def test_empty_unknown_stale_or_malformed_never_fills_zero(tmp_path, monkeypatch, output):
    context, _ = setup(tmp_path)
    monkeypatch.setattr(history, "_fetch", lambda *_: output)
    result = history.read_port_history(context, IP, "101")
    assert result["rx"] == result["tx"] == []


@pytest.mark.parametrize("failure", ["timeout", "exit", "missing_binary", "too_large"])
def test_bounded_command_failures_are_safe_no_coverage(tmp_path, monkeypatch, failure):
    context, _ = setup(tmp_path)
    def run(argv, **options):
        if failure == "timeout": raise subprocess.TimeoutExpired(argv, 5)
        if failure == "missing_binary": raise FileNotFoundError("private filesystem detail")
        if failure == "too_large": options["stdout"].write(b"x" * (history.OUTPUT_LIMIT + 1))
        return SimpleNamespace(returncode=1 if failure == "exit" else 0)
    monkeypatch.setattr(history.subprocess, "run", run)
    assert history.read_port_history(context, IP, "101")["rx"] == []


@pytest.mark.parametrize("devices", [[], [{"device_id": 7, "ip": IP, "hostname": IP, "os": "ios"}] * 2,
                                    [{"device_id": 8, "ip": "192.0.2.99", "hostname": "other", "os": "ios"}]])
def test_real_librenms_device_response_is_not_ambiguous(tmp_path, monkeypatch, devices):
    context, _ = setup(tmp_path)
    client = make_client()
    calls = attach_sequence(client, [FakeResponse({"status": "ok", "devices": devices})])
    monkeypatch.setattr(history, "_fetch", lambda *_: pytest.fail("identity rejected"))
    result = history.read_port_history(replace(context, librenms_client_factory=lambda: client), IP, "101")
    assert not result["rx"] and len(calls) == 1


def test_real_librenms_ports_url_and_column_contract(tmp_path, monkeypatch):
    context, fixture = setup(tmp_path)
    client = make_client()
    calls = attach_sequence(client, [FakeResponse({"status": "ok", "devices": [fixture.device]}),
                                     FakeResponse({"status": "ok", "ports": fixture.ports})])
    monkeypatch.setattr(history, "_fetch", lambda *_: raw())
    assert history.read_port_history(replace(context, librenms_client_factory=lambda: client), IP, "101")["rx"]
    urls = [urlsplit(call["url"]) for call in calls]
    assert [url.path for url in urls] == [f"/api/v0/devices/{IP}", "/api/v0/devices/7/ports"]
    assert parse_qs(urls[1].query) == {"columns": ["device_id,ifIndex,port_id"]}


@pytest.mark.parametrize("authorized", [True, False])
def test_route_requires_auth_before_identity_and_read(tmp_path, monkeypatch, authorized):
    context, _ = setup(tmp_path)
    events = []
    def auth(_handler):
        events.append("auth")
        if not authorized: raise PermissionError("unauthenticated")
    monkeypatch.setattr(history, "read_port_history", lambda ctx, ip, index: events.append((ip, index)) or {"ok": True})
    class Handler:
        def _send_json(self, payload, status=200): events.append((status, payload))
    router = read_api.ReadApiContext(event_config_context=None, transaction_context=None, incident_context=None,
        iperf_runtime_context=None, dhcp_settings_context=None, dhcp_runtime_context=None,
        bridge_url="", require_auth=lambda _: {}, read_json_file=lambda *_: None, stamp=lambda: "",
        network_context=context, network_require_auth=auth)
    if authorized:
        read_api.handle_get(Handler(), f"/network/nodes/{IP}/ports/101/history", router)
        assert events == ["auth", (IP, "101"), (200, {"ok": True})]
    else:
        with pytest.raises(PermissionError): read_api.handle_get(Handler(), f"/network/nodes/{IP}/ports/101/history", router)
        assert events == ["auth"]


def test_storage_wiring_matches_rrdcached_read_only():
    root = Path(__file__).resolve().parents[1]
    compose = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))
    api = compose["services"]["platform-api"]
    cached = compose["services"]["librenms-rrdcached"]
    assert "./librenms-data/rrd:/data/db:ro" in api["volumes"]
    assert "./librenms-data/rrd:/data/db" in cached["volumes"]
    assert api["environment"]["PLATFORM_NETWORK_RRD_BASE_PATH"] == "/data/db"
    assert "iperf3 rrdtool" in (root / "docker/platform-api/Dockerfile").read_text()


def test_native_points_are_sorted_and_unknown_direction_not_filled(tmp_path, monkeypatch):
    context, _ = setup(tmp_path)
    output = f"INOCTETS OUTOCTETS\n{END}: 0 NaN\n{END - 300}: 2 -1\n{END - 600}: inf UNKNOWN\n"
    monkeypatch.setattr(history, "_fetch", lambda *_: output)
    result = history.read_port_history(context, IP, "101")
    assert result["rx"] == [{"t": END - 300, "v": 16}, {"t": END, "v": 0}]
    assert result["tx"] == [] and result["coverage"]["tx"] == "empty"


def test_librenms_failure_does_not_read_rrd(tmp_path, monkeypatch):
    context, client = setup(tmp_path)
    def fail(_ip): raise LibreNMSError("private details")
    client.get_device = fail
    monkeypatch.setattr(history, "_fetch", lambda *_: pytest.fail("no resolved identity"))
    assert history.read_port_history(context, IP, "101")["rx"] == []
