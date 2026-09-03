import importlib.util
import json
from pathlib import Path

import pytest


_spec = importlib.util.spec_from_file_location(
    "feishu_bridge_auto_delete",
    Path(__file__).resolve().parent.parent / "alertmanager-feishu-bridge.py",
)
bridge = importlib.util.module_from_spec(_spec)
assert _spec.loader
_spec.loader.exec_module(bridge)

NOW = 2_000_000_000
WEEK = 604800


def device(ip="192.168.10.18", **overrides):
    value = {
        "device_id": 18,
        "hostname": ip,
        "ip": ip,
        "status": 0,
        "disabled": 0,
        "last_polled": NOW - WEEK - 60,
    }
    value.update(overrides)
    return value


@pytest.fixture(autouse=True)
def safe_auto_delete_defaults(monkeypatch):
    monkeypatch.setattr(bridge, "DEVICE_PENDING_DELETE_ENABLED", True)
    monkeypatch.setattr(bridge, "DEVICE_AUTO_DELETE_ENABLED", True)
    monkeypatch.setattr(bridge, "DEVICE_AUTO_DELETE_AFTER_SECONDS", WEEK)
    monkeypatch.setattr(bridge, "DEVICE_AUTO_DELETE_DRY_RUN", True)
    for key in bridge.DEVICE_AUTO_DELETE_PROTECTION_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key in (
        "LIBRENMS_DISCOVERY_TARGETS",
        "FIREWALL_DISCOVERY_RANGE",
        "SWITCH_DISCOVERY_RANGE",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize(
    "pending_enabled,auto_enabled",
    [(False, False), (False, True), (True, False)],
)
def test_both_feature_gates_are_required(
    monkeypatch, pending_enabled, auto_enabled
):
    deletes = []
    monkeypatch.setattr(
        bridge, "DEVICE_PENDING_DELETE_ENABLED", pending_enabled
    )
    monkeypatch.setattr(bridge, "DEVICE_AUTO_DELETE_ENABLED", auto_enabled)

    stats = bridge.run_device_auto_delete_cycle(
        now=NOW,
        devices=[device()],
        token="token",
        probe=lambda _ip: False,
        delete=lambda *_args: deletes.append(True) or True,
    )

    assert stats["candidates"] == 0
    assert deletes == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": 1},
        {"status": None},
        {"last_polled": NOW - WEEK + 1},
        {"last_polled": None},
        {"last_polled": "not-a-time"},
        {"disabled": 1},
        {"disabled": None},
    ],
)
def test_non_candidates_fail_closed_before_probe(overrides):
    probes = []
    deletes = []

    stats = bridge.run_device_auto_delete_cycle(
        now=NOW,
        devices=[device(**overrides)],
        token="token",
        probe=lambda ip: probes.append(ip) or False,
        delete=lambda *_args: deletes.append(True) or True,
    )

    assert stats["candidates"] == 0
    assert probes == []
    assert deletes == []


@pytest.mark.parametrize(
    "key,value,ip",
    [
        ("LIBRENMS_CORE_IP", "192.168.10.254", "192.168.10.254"),
        ("CORE_SWITCH_PING", "core:192.168.10.254", "192.168.10.254"),
        ("DIST_SWITCH_PING", "dist:192.168.10.11-12", "192.168.10.12"),
        ("TOURNAMENT_SWITCHES", "stage:192.168.10.31", "192.168.10.31"),
        ("FIREWALL_PING", "vip:192.168.9.1", "192.168.9.1"),
        ("FIREWALL_SNMP_TARGETS", "192.168.9.1", "192.168.9.1"),
        (
            "FIREWALL_UNIT_SNMP_TARGETS",
            "192.168.9.11,192.168.9.12",
            "192.168.9.11",
        ),
        (
            "FIREWALL_UNIT_SNMP_TARGETS",
            "FW-A:192.168.9.11,FW-B:192.168.9.12",
            "192.168.9.12",
        ),
        ("SERVER_PING", "game:192.168.20.10", "192.168.20.10"),
        ("ISP_PING", "telecom:203.0.113.1", "203.0.113.1"),
        ("BIGSCREEN_ISP_IPS", "telecom:203.0.113.2", "203.0.113.2"),
        ("PLAYER_GATEWAYS", "192.168.40.1", "192.168.40.1"),
        (
            "INTERCONNECT_SNMP_TARGETS",
            "192.168.10.41",
            "192.168.10.41",
        ),
        ("TOPOLOGY_DEVICES", "topology:192.168.10.42", "192.168.10.42"),
        ("TOPOLOGY_ARP_DEVICES", "192.168.10.43", "192.168.10.43"),
    ],
)
def test_explicit_infrastructure_targets_are_permanently_protected(
    monkeypatch, capsys, key, value, ip
):
    monkeypatch.setenv(key, value)
    probes = []

    stats = bridge.run_device_auto_delete_cycle(
        now=NOW,
        devices=[device(ip)],
        token="token",
        probe=lambda target: probes.append(target) or False,
    )

    assert stats["candidates"] == 0
    assert probes == []
    assert "[device-auto-delete] protected" in capsys.readouterr().err


@pytest.mark.parametrize(
    "range_key",
    [
        "LIBRENMS_DISCOVERY_TARGETS",
        "FIREWALL_DISCOVERY_RANGE",
        "SWITCH_DISCOVERY_RANGE",
    ],
)
def test_discovery_ranges_do_not_protect_old_devices(
    monkeypatch, range_key
):
    monkeypatch.setenv(range_key, "192.168.10.1-100")
    monkeypatch.setattr(bridge, "DEVICE_AUTO_DELETE_DRY_RUN", False)
    deletes = []

    stats = bridge.run_device_auto_delete_cycle(
        now=NOW,
        devices=[device("192.168.10.18")],
        token="token",
        probe=lambda _ip: False,
        delete=lambda _token, item: deletes.append(item["ip"]) or True,
    )

    assert stats["deleted"] == 1
    assert deletes == ["192.168.10.18"]


def test_reachable_candidate_is_kept(monkeypatch, capsys):
    monkeypatch.setattr(bridge, "DEVICE_AUTO_DELETE_DRY_RUN", False)
    deletes = []

    stats = bridge.run_device_auto_delete_cycle(
        now=NOW,
        devices=[device()],
        token="token",
        probe=lambda _ip: True,
        delete=lambda *_args: deletes.append(True) or True,
    )

    assert stats["candidates"] == 1
    assert deletes == []
    assert "reachable-now" in capsys.readouterr().err


@pytest.mark.parametrize("probe_result", [None, "unknown"])
def test_indeterminate_probe_result_fails_closed(
    monkeypatch, probe_result, capsys
):
    monkeypatch.setattr(bridge, "DEVICE_AUTO_DELETE_DRY_RUN", False)
    deletes = []

    bridge.run_device_auto_delete_cycle(
        now=NOW,
        devices=[device()],
        token="token",
        probe=lambda _ip: probe_result,
        delete=lambda *_args: deletes.append(True) or True,
    )

    assert deletes == []
    assert "probe-error" in capsys.readouterr().err


def test_probe_exception_fails_closed(monkeypatch, capsys):
    monkeypatch.setattr(bridge, "DEVICE_AUTO_DELETE_DRY_RUN", False)
    deletes = []

    def failed_probe(_ip):
        raise TimeoutError("blackbox timeout")

    bridge.run_device_auto_delete_cycle(
        now=NOW,
        devices=[device()],
        token="token",
        probe=failed_probe,
        delete=lambda *_args: deletes.append(True) or True,
    )

    assert deletes == []
    assert "probe-error" in capsys.readouterr().err


def test_dry_run_logs_would_delete_without_calling_delete(capsys):
    deletes = []

    stats = bridge.run_device_auto_delete_cycle(
        now=NOW,
        devices=[device()],
        token="token",
        probe=lambda _ip: False,
        delete=lambda *_args: deletes.append(True) or True,
    )

    assert stats["dry_run"] == 1
    assert deletes == []
    output = capsys.readouterr().err
    assert "candidate" in output
    assert "DRY-RUN would delete" in output


def test_real_delete_success_is_logged(monkeypatch, capsys):
    monkeypatch.setattr(bridge, "DEVICE_AUTO_DELETE_DRY_RUN", False)

    stats = bridge.run_device_auto_delete_cycle(
        now=NOW,
        devices=[device(hostname="old-switch")],
        token="token",
        probe=lambda _ip: False,
        delete=lambda *_args: True,
    )

    assert stats["deleted"] == 1
    output = capsys.readouterr().err
    assert "deleted hostname=old-switch device_id=18" in output
    assert f"offline_seconds={WEEK + 60}" in output


def test_delete_failure_does_not_crash_and_next_cycle_retries(
    monkeypatch, capsys
):
    monkeypatch.setattr(bridge, "DEVICE_AUTO_DELETE_DRY_RUN", False)
    attempts = []

    def retrying_delete(_token, _device):
        attempts.append(True)
        return len(attempts) == 2

    first = bridge.run_device_auto_delete_cycle(
        now=NOW,
        devices=[device()],
        token="token",
        probe=lambda _ip: False,
        delete=retrying_delete,
    )
    second = bridge.run_device_auto_delete_cycle(
        now=NOW,
        devices=[device()],
        token="token",
        probe=lambda _ip: False,
        delete=retrying_delete,
    )

    assert first["delete_failed"] == 1
    assert second["deleted"] == 1
    assert len(attempts) == 2
    assert "retry next cycle" in capsys.readouterr().err


def test_delete_exception_does_not_escape_cycle(monkeypatch):
    monkeypatch.setattr(bridge, "DEVICE_AUTO_DELETE_DRY_RUN", False)

    def failed_delete(*_args):
        raise RuntimeError("temporary API outage")

    stats = bridge.run_device_auto_delete_cycle(
        now=NOW,
        devices=[device()],
        token="token",
        probe=lambda _ip: False,
        delete=failed_delete,
    )

    assert stats["delete_failed"] == 1


def test_cycle_fetches_inventory_once_and_probes_only_candidates(monkeypatch):
    fetched = []
    probes = []
    monkeypatch.setattr(
        bridge,
        "fetch_librenms_devices",
        lambda token: fetched.append(token) or [
            device("192.168.10.18"),
            device("192.168.10.19", status=1),
            device("192.168.10.20", last_polled=NOW - 60),
        ],
    )

    stats = bridge.run_device_auto_delete_cycle(
        now=NOW,
        token="token",
        probe=lambda ip: probes.append(ip) or False,
    )

    assert fetched == ["token"]
    assert probes == ["192.168.10.18"]
    assert stats["scanned"] == 3


def test_device_record_delete_uses_librenms_api_without_inventory_get(
    monkeypatch
):
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        @staticmethod
        def read():
            return json.dumps([{"status": "ok"}]).encode("utf-8")

    def fake_urlopen(req, timeout):
        requests.append((req.get_method(), req.full_url, timeout))
        return Response()

    monkeypatch.setattr(bridge, "LIBRENMS_URL", "http://librenms")
    monkeypatch.setattr(bridge.request, "urlopen", fake_urlopen)

    assert bridge.delete_librenms_device_record(
        "token", device(device_id=42)
    ) is True
    assert requests == [("DELETE", "http://librenms/api/v0/devices/42", 10)]
