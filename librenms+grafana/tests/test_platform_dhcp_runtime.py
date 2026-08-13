from http import HTTPStatus

import pytest

from platform_api import dhcp_runtime
from platform_api import dhcp_settings

from .test_platform_transactions import load_api


class RuntimeDiagnostic(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.payload = {"ok": False, "error": message}


class MutableClock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class FakeSession:
    def __init__(self):
        self.writes = []
        self.closed = False

    def write(self, data):
        self.writes.append(data)

    def close(self):
        self.closed = True


def make_context(
    *,
    host="192.168.10.254",
    refresh_seconds=60,
    clock=None,
    monotonic=None,
):
    return dhcp_runtime.DhcpRuntimeContext(
        core_host=lambda: host,
        telnet_context=lambda: object(),
        connection_settings=lambda: {"port": 23},
        refresh_seconds=refresh_seconds,
        error_factory=RuntimeDiagnostic,
        clock=clock or (lambda: 1_000.0),
        monotonic=monotonic or (lambda: 100.0),
    )


def base_collection(host="192.168.10.254"):
    return {
        "ok": True,
        "host": host,
        "source": "devices.core.ip",
        "pools": [],
        "conflicts": [],
        "excludedAddresses": [],
        "statistics": {},
        "summary": {"poolCount": 0},
        "warnings": [],
    }


def assert_error(exc, status, message):
    assert exc.value.status == status
    assert exc.value.payload == {"ok": False, "error": message}


@pytest.fixture(autouse=True)
def reset_runtime_state():
    dhcp_runtime.clear_cache()
    assert not dhcp_runtime.DHCP_LOCK.locked()
    yield
    dhcp_runtime.clear_cache()
    if dhcp_runtime.DHCP_LOCK.locked():
        dhcp_runtime.DHCP_LOCK.release()


def test_runtime_is_extracted_and_routers_bind_directly_without_wrappers(tmp_path):
    api = load_api(tmp_path)
    read_deps = api._read_api_dependencies()
    write_deps = api._write_api_dependencies()

    assert read_deps.get_dhcp_bindings.func is dhcp_runtime.get_dhcp_bindings
    assert read_deps.get_dhcp_dashboard.func is dhcp_runtime.get_dhcp_dashboard
    assert write_deps.test_dhcp_connection.func is dhcp_runtime.test_dhcp_connection
    for callable_ in (
        read_deps.get_dhcp_bindings,
        read_deps.get_dhcp_dashboard,
        write_deps.test_dhcp_connection,
    ):
        assert isinstance(callable_.args[0], dhcp_runtime.DhcpRuntimeContext)
    assert not hasattr(api, "DHCP_LOCK")
    assert not hasattr(api, "DHCP_CACHE")
    assert not hasattr(api, "collect_cisco_dhcp")
    assert not hasattr(api, "get_dhcp_bindings")
    assert not hasattr(api, "test_dhcp_connection")
    assert not hasattr(api, "_cached_dhcp_payload")
    assert not hasattr(api, "get_dhcp_dashboard")


def test_runtime_context_keeps_composition_root_dependencies(tmp_path):
    api = load_api(tmp_path)
    context = api._dhcp_runtime_context()

    assert context.core_host is api.configured_core_switch_host
    assert context.telnet_context is api._dhcp_telnet_context
    expected_settings = dhcp_settings.dhcp_connection_settings(
        api._dhcp_settings_context(),
    )
    assert context.connection_settings() == expected_settings
    assert context.refresh_seconds == api.DHCP_REFRESH_SECONDS
    assert context.error_factory is api.DiagnosticError
    assert context.clock is api.time.time
    assert context.monotonic is api.time.monotonic


def test_collect_keeps_command_order_parser_flow_payload_and_cleanup(monkeypatch):
    context = make_context()
    session = FakeSession()
    opened = []
    commands = []
    parser_calls = []
    pools = [{"total": 20, "leased": 7, "excluded": 3}]

    monkeypatch.setattr(
        dhcp_runtime.dhcp_telnet,
        "_open_cisco_telnet",
        lambda telnet_context, host: opened.append((telnet_context, host)) or session,
    )

    def command(_telnet_context, _session, value):
        commands.append(value)
        return {
            "show ip dhcp pool": "pool-output",
            "show ip dhcp conflict": "conflict-output",
            "show ip dhcp server statistics": "statistics-output",
            "show running-config | include ^ip dhcp excluded-address": "excluded-output",
        }.get(value, "")

    monkeypatch.setattr(dhcp_runtime.dhcp_telnet, "_telnet_command", command)
    monkeypatch.setattr(
        dhcp_runtime,
        "parse_cisco_dhcp_pools",
        lambda value: parser_calls.append(("pools", value)) or pools,
    )
    monkeypatch.setattr(
        dhcp_runtime,
        "parse_cisco_dhcp_conflicts",
        lambda value: parser_calls.append(("conflicts", value)) or [{"ip": "192.0.2.9"}],
    )
    monkeypatch.setattr(
        dhcp_runtime,
        "parse_cisco_dhcp_statistics",
        lambda value: parser_calls.append(("statistics", value)) or {"automaticBindings": 7},
    )
    monkeypatch.setattr(
        dhcp_runtime,
        "parse_cisco_dhcp_excluded",
        lambda value: parser_calls.append(("excluded", value)) or ["192.0.2.1"],
    )
    monkeypatch.setattr(
        dhcp_runtime,
        "attach_dhcp_pool_exclusions",
        lambda received, excluded: parser_calls.append(("attach", received, excluded)),
    )

    result = dhcp_runtime.collect_cisco_dhcp(context, "192.168.10.254")

    assert len(opened) == 1
    assert opened[0][1] == "192.168.10.254"
    assert commands == [
        "terminal length 0",
        "show ip dhcp pool",
        "show ip dhcp conflict",
        "show ip dhcp server statistics",
        "show running-config | include ^ip dhcp excluded-address",
    ]
    assert parser_calls == [
        ("pools", "pool-output"),
        ("conflicts", "conflict-output"),
        ("statistics", "statistics-output"),
        ("excluded", "excluded-output"),
        ("attach", pools, ["192.0.2.1"]),
    ]
    assert result == {
        "ok": True,
        "host": "192.168.10.254",
        "source": "devices.core.ip",
        "pools": pools,
        "conflicts": [{"ip": "192.0.2.9"}],
        "excludedAddresses": ["192.0.2.1"],
        "statistics": {"automaticBindings": 7},
        "summary": {
            "poolCount": 1,
            "total": 20,
            "leased": 7,
            "excluded": 3,
            "available": 10,
            "utilization": 41.2,
            "conflictCount": 1,
        },
        "warnings": [],
    }
    assert session.writes == [b"exit\n"]
    assert session.closed is True


def test_collect_keeps_optional_unsupported_warnings(monkeypatch):
    context = make_context()
    session = FakeSession()
    monkeypatch.setattr(
        dhcp_runtime.dhcp_telnet,
        "_open_cisco_telnet",
        lambda _context, _host: session,
    )
    monkeypatch.setattr(
        dhcp_runtime.dhcp_telnet,
        "_telnet_command",
        lambda _context, _session, command: (
            "Pool EMPTY :\nTotal addresses : 0\nLeased addresses : 0"
            if command == "show ip dhcp pool"
            else "% Invalid input detected"
        ),
    )

    result = dhcp_runtime.collect_cisco_dhcp(context, "192.168.10.254")

    assert result["warnings"] == [
        "交换机不支持 show ip dhcp conflict",
        "交换机不支持 show ip dhcp server statistics",
        "交换机不支持 show running-config | include ^ip dhcp excluded-address",
    ]


def test_collect_maps_io_error_and_keeps_session_cleanup(monkeypatch):
    context = make_context()
    session = FakeSession()
    monkeypatch.setattr(
        dhcp_runtime.dhcp_telnet,
        "_open_cisco_telnet",
        lambda _context, _host: session,
    )
    monkeypatch.setattr(
        dhcp_runtime.dhcp_telnet,
        "_telnet_command",
        lambda *_args: (_ for _ in ()).throw(OSError("connection reset")),
    )

    with pytest.raises(RuntimeDiagnostic) as exc:
        dhcp_runtime.collect_cisco_dhcp(context, "192.168.10.254")

    assert_error(
        exc,
        HTTPStatus.BAD_GATEWAY,
        "无法读取核心交换机 DHCP：connection reset",
    )
    assert session.writes == [b"exit\n"]
    assert session.closed is True


def test_bindings_keeps_dhcp_arp_merge_fallback_and_cleanup(monkeypatch):
    clock = MutableClock(1_234.9)
    context = make_context(clock=clock)
    session = FakeSession()
    commands = []
    monkeypatch.setattr(
        dhcp_runtime.dhcp_telnet,
        "_open_cisco_telnet",
        lambda _context, _host: session,
    )

    def command(_context, _session, value):
        commands.append(value)
        return {
            "show ip dhcp binding": "binding-output",
            "show ip arp": "% Unknown command",
            "show arp": "arp-output",
        }.get(value, "")

    monkeypatch.setattr(dhcp_runtime.dhcp_telnet, "_telnet_command", command)
    monkeypatch.setattr(
        dhcp_runtime,
        "parse_cisco_dhcp_bindings",
        lambda value: [{"ip": "192.168.40.21", "raw": value}],
    )
    monkeypatch.setattr(
        dhcp_runtime,
        "parse_cisco_arp_entries",
        lambda value: [{"ip": "192.168.40.5", "raw": value}],
    )

    result = dhcp_runtime.get_dhcp_bindings(context)

    assert commands == [
        "terminal length 0",
        "show ip dhcp binding",
        "show ip arp",
        "show arp",
    ]
    assert result == {
        "ok": True,
        "host": "192.168.10.254",
        "bindings": [{"ip": "192.168.40.21", "raw": "binding-output"}],
        "usedAddresses": ["192.168.40.21"],
        "arpEntries": [{"ip": "192.168.40.5", "raw": "arp-output"}],
        "observedAddresses": ["192.168.40.5"],
        "parserWarning": "",
        "arpWarning": "",
        "capturedAt": 1234,
    }
    assert session.writes == [b"exit\n"]
    assert session.closed is True
    assert not dhcp_runtime.DHCP_LOCK.locked()


def test_bindings_keeps_lock_busy_error():
    context = make_context()
    assert dhcp_runtime.DHCP_LOCK.acquire(blocking=False)

    with pytest.raises(RuntimeDiagnostic) as exc:
        dhcp_runtime.get_dhcp_bindings(context)

    assert_error(
        exc,
        HTTPStatus.CONFLICT,
        "DHCP 面板正在读取交换机，请稍后再查询已用 IP",
    )


@pytest.mark.parametrize(
    ("output", "level", "privileged", "message"),
    [
        ("Current privilege level is 15", 15, True, "Telnet 登录成功，已进入特权模式"),
        ("Current privilege level is 7", 7, False, "Telnet 登录成功，当前权限级别 7"),
        ("Privilege unavailable", None, False, "Telnet 登录成功，交换机未返回权限级别"),
    ],
)
def test_connection_keeps_payload_messages_timing_and_cleanup(
    monkeypatch,
    output,
    level,
    privileged,
    message,
):
    monotonic = MutableClock(10.0)
    context = make_context(clock=lambda: 2_000.9, monotonic=monotonic)
    session = FakeSession()
    commands = []
    monkeypatch.setattr(
        dhcp_runtime.dhcp_telnet,
        "_open_cisco_telnet",
        lambda _context, _host: session,
    )

    def command(_context, _session, value):
        commands.append(value)
        monotonic.value = 10.1234
        return output

    monkeypatch.setattr(dhcp_runtime.dhcp_telnet, "_telnet_command", command)

    result = dhcp_runtime.test_dhcp_connection(context)

    assert result == {
        "ok": True,
        "host": "192.168.10.254",
        "port": 23,
        "login": True,
        "privileged": privileged,
        "privilegeLevel": level,
        "latencyMs": 123,
        "message": message,
        "testedAt": 2000,
    }
    assert commands == ["show privilege"]
    assert session.writes == [b"exit\n"]
    assert session.closed is True
    assert not dhcp_runtime.DHCP_LOCK.locked()


def test_connection_keeps_io_error_text_cleanup_and_lock_release(monkeypatch):
    context = make_context()
    session = FakeSession()
    monkeypatch.setattr(
        dhcp_runtime.dhcp_telnet,
        "_open_cisco_telnet",
        lambda _context, _host: session,
    )
    monkeypatch.setattr(
        dhcp_runtime.dhcp_telnet,
        "_telnet_command",
        lambda *_args: (_ for _ in ()).throw(EOFError("closed")),
    )

    with pytest.raises(RuntimeDiagnostic) as exc:
        dhcp_runtime.test_dhcp_connection(context)

    assert_error(
        exc,
        HTTPStatus.BAD_GATEWAY,
        "无法连接核心交换机 Telnet：closed",
    )
    assert session.writes == [b"exit\n"]
    assert session.closed is True
    assert not dhcp_runtime.DHCP_LOCK.locked()


def test_cached_payload_keeps_copy_age_clamp_and_refreshing_flag():
    monotonic = MutableClock(90.0)
    context = make_context(monotonic=monotonic)
    payload = {**base_collection(), "cached": False, "refreshing": False}
    dhcp_runtime.DHCP_CACHE.update({"payload": payload, "monotonic": 100.0})

    result = dhcp_runtime._cached_dhcp_payload(context, refreshing=True)

    assert result["cached"] is True
    assert result["cacheAgeSeconds"] == 0
    assert result["refreshing"] is True
    assert payload["cached"] is False
    assert payload["refreshing"] is False


def test_dashboard_keeps_cache_hit_force_floor_and_force_refresh(monkeypatch):
    monotonic = MutableClock(100.0)
    context = make_context(monotonic=monotonic)
    calls = []

    def collect(_context, host):
        calls.append(host)
        return base_collection(host)

    monkeypatch.setattr(dhcp_runtime, "collect_cisco_dhcp", collect)

    first = dhcp_runtime.get_dhcp_dashboard(context)
    monotonic.value = 120.0
    second = dhcp_runtime.get_dhcp_dashboard(context)
    forced_inside_floor = dhcp_runtime.get_dhcp_dashboard(context, force=True)
    monotonic.value = 131.0
    forced_after_floor = dhcp_runtime.get_dhcp_dashboard(context, force=True)

    assert first["cached"] is False
    assert first["capturedAt"] == 1000
    assert first["collectionSeconds"] == 0
    assert first["refreshSeconds"] == 60
    assert second["cached"] is True
    assert second["cacheAgeSeconds"] == 20
    assert forced_inside_floor["cached"] is True
    assert forced_after_floor["cached"] is False
    assert calls == ["192.168.10.254", "192.168.10.254"]


def test_dashboard_keeps_refresh_seconds_boundary(monkeypatch):
    monotonic = MutableClock(100.0)
    context = make_context(refresh_seconds=100, monotonic=monotonic)
    calls = []
    monkeypatch.setattr(
        dhcp_runtime,
        "collect_cisco_dhcp",
        lambda _context, host: calls.append(host) or base_collection(host),
    )

    dhcp_runtime.get_dhcp_dashboard(context)
    monotonic.value = 150.0
    within_refresh = dhcp_runtime.get_dhcp_dashboard(context)
    monotonic.value = 195.0
    at_refresh = dhcp_runtime.get_dhcp_dashboard(context)

    assert within_refresh["cached"] is True
    assert at_refresh["cached"] is False
    assert calls == ["192.168.10.254", "192.168.10.254"]


def test_dashboard_returns_refreshing_stale_payload_when_lock_busy():
    monotonic = MutableClock(140.0)
    context = make_context(monotonic=monotonic)
    dhcp_runtime.DHCP_CACHE.update({
        "payload": base_collection(),
        "monotonic": 100.0,
    })
    assert dhcp_runtime.DHCP_LOCK.acquire(blocking=False)

    result = dhcp_runtime.get_dhcp_dashboard(context, force=True)

    assert result["cached"] is True
    assert result["cacheAgeSeconds"] == 40
    assert result["refreshing"] is True


def test_dashboard_lock_busy_without_matching_cache_keeps_conflict():
    context = make_context()
    assert dhcp_runtime.DHCP_LOCK.acquire(blocking=False)

    with pytest.raises(RuntimeDiagnostic) as exc:
        dhcp_runtime.get_dhcp_dashboard(context)

    assert_error(exc, HTTPStatus.CONFLICT, "DHCP 面板正在刷新，请稍后再试")


def test_dashboard_refresh_failure_keeps_stale_cache_and_releases_lock(monkeypatch):
    monotonic = MutableClock(200.0)
    context = make_context(monotonic=monotonic)
    stale = base_collection()
    dhcp_runtime.DHCP_CACHE.update({"payload": stale, "monotonic": 100.0})
    monkeypatch.setattr(
        dhcp_runtime,
        "collect_cisco_dhcp",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("fixture failure")),
    )

    with pytest.raises(RuntimeError, match="fixture failure"):
        dhcp_runtime.get_dhcp_dashboard(context, force=True)

    assert dhcp_runtime.DHCP_CACHE == {
        "payload": stale,
        "monotonic": 100.0,
    }
    assert not dhcp_runtime.DHCP_LOCK.locked()


def test_settings_save_clears_runtime_cache_through_composition_root(tmp_path):
    api = load_api(tmp_path)
    api.CONFIG_PATH.write_text(
        "devices:\n  core:\n    ip: 192.168.10.254\n",
        encoding="utf-8",
    )
    dhcp_runtime.DHCP_CACHE.update({
        "payload": base_collection(),
        "monotonic": 100.0,
    })

    dhcp_settings.save_dhcp_settings(api._dhcp_settings_context(), {
        "username": "operator",
        "password": "secret",
        "enablePassword": "enable-secret",
        "port": 23,
    })

    assert dhcp_runtime.DHCP_CACHE == {}


def test_router_partials_keep_context_first_and_route_arguments(tmp_path, monkeypatch):
    api = load_api(tmp_path)
    calls = []
    monkeypatch.setattr(
        dhcp_runtime,
        "get_dhcp_bindings",
        lambda context: calls.append(("bindings", context)) or {"ok": True},
    )
    monkeypatch.setattr(
        dhcp_runtime,
        "get_dhcp_dashboard",
        lambda context, force=False: (
            calls.append(("dashboard", context, force)) or {"ok": True}
        ),
    )
    monkeypatch.setattr(
        dhcp_runtime,
        "test_dhcp_connection",
        lambda context: calls.append(("test", context)) or {"ok": True},
    )

    read_deps = api._read_api_dependencies()
    write_deps = api._write_api_dependencies()
    assert read_deps.get_dhcp_bindings() == {"ok": True}
    assert read_deps.get_dhcp_dashboard(True) == {"ok": True}
    assert write_deps.test_dhcp_connection() == {"ok": True}
    assert [item[0] for item in calls] == ["bindings", "dashboard", "test"]
    assert calls[1][2] is True
    assert all(isinstance(item[1], dhcp_runtime.DhcpRuntimeContext) for item in calls)
