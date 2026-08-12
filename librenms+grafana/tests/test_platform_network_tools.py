import json
import os
import sys
from types import SimpleNamespace

import pytest

from platform_api import iperf

from .test_platform_transactions import load_api


def _iperf_payload(received_bps=950_000_000, sent_bps=1_000_000_000):
    return json.dumps({
        "intervals": [
            {"sum": {"start": 0, "end": 1, "seconds": 1, "bytes": 118_750_000, "bits_per_second": 950_000_000, "retransmits": 1}},
            {"sum": {"start": 1, "end": 2, "seconds": 1, "bytes": 120_000_000, "bits_per_second": 960_000_000, "retransmits": 2}},
        ],
        "end": {
            "sum_sent": {
                "bits_per_second": sent_bps,
                "seconds": 10.01,
                "retransmits": 3,
                "bytes": 1_250_000_000,
            },
            "sum_received": {
                "bits_per_second": received_bps,
                "seconds": 10.01,
                "bytes": 1_187_500_000,
            },
        },
    })


DHCP_POOL_OUTPUT = """
Pool PLAYERS :
 Utilization mark (high/low)    : 100 / 0
 Subnet size (first/next)       : 0 / 0
 Total addresses                : 254
 Leased addresses               : 81
 Excluded addresses             : 10
 Pending event                  : none
 1 subnet is currently in the pool :
 Current index        IP address range                    Leased/Excluded/Total
 192.168.40.92        192.168.40.1     - 192.168.40.254    81    / 10    / 254

Pool WIRELESS :
 Total addresses                : 126
 Leased addresses               : 113
 Excluded addresses             : 1
 192.168.41.115       192.168.41.1     - 192.168.41.126    113   / 1     / 126
"""


def test_network_host_and_port_range_validation(tmp_path):
    api = load_api(tmp_path)

    assert api.validate_network_host("iperf.online.net") == "iperf.online.net"
    assert api.validate_network_host("192.168.10.254") == "192.168.10.254"
    assert iperf.parse_port_range(
        "5200-5202", error_factory=api.DiagnosticError,
    ) == [5200, 5201, 5202]

    with pytest.raises(api.DiagnosticError):
        api.validate_network_host("iperf.online.net; reboot")
    with pytest.raises(api.DiagnosticError):
        iperf.parse_port_range(
            "5200-5215", error_factory=api.DiagnosticError,
        )


def test_iperf_json_uses_receiver_rate_and_reports_retransmits(tmp_path):
    api = load_api(tmp_path)

    result = iperf.parse_iperf3_json(_iperf_payload())

    assert result["mbps"] == 950.0
    assert result["seconds"] == 10.01
    assert result["retransmits"] == 3
    assert result["bytes"] == 1_187_500_000
    assert result["sender"] == {"mbps": 1000.0, "bytes": 1_250_000_000, "seconds": 10.01, "retransmits": 3}
    assert result["receiver"] == {"mbps": 950.0, "bytes": 1_187_500_000, "seconds": 10.01, "retransmits": 0}
    assert result["intervals"][0] == {
        "start": 0.0,
        "end": 1.0,
        "seconds": 1.0,
        "bytes": 118_750_000,
        "mbps": 950.0,
        "retransmits": 1,
    }


def test_iperf_tries_next_port_without_using_a_shell(monkeypatch, tmp_path):
    api = load_api(tmp_path)
    api.IPERF3_COMMAND = "iperf3"
    api.IPERF3_TIMEOUT = 30
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[command.index("-p") + 1] == "5200":
            return SimpleNamespace(returncode=1, stdout="", stderr="server is busy")
        return SimpleNamespace(returncode=0, stdout=_iperf_payload(), stderr="")

    monkeypatch.setattr(api.subprocess, "run", fake_run)
    monkeypatch.setattr(api, "_host_exec_env", lambda: {})

    result = api.run_iperf_test({
        "server": "speedtest.hkg12.hk.leaseweb.net",
        "ports": "5200-5201",
        "duration": 3,
        "parallel": 1,
        "direction": "upload",
    })

    assert result["protocol"] == "TCP"
    assert result["results"][0]["port"] == 5201
    assert [call[0][call[0].index("-p") + 1] for call in calls] == ["5200", "5201"]
    assert all("shell" not in kwargs for _command, kwargs in calls)
    assert all("--connect-timeout" in command for command, _kwargs in calls)
    assert api.iperf_status_payload()["state"] == "complete"
    assert api.iperf_status_payload()["percent"] == 100


def test_iperf_error_summary_hides_raw_json(tmp_path):
    api = load_api(tmp_path)

    detail = iperf._iperf_error_summary(
        json.dumps({"start": {}, "intervals": [], "end": {}, "error": "control socket has closed unexpectedly"}),
        "",
        1,
    )

    assert detail == "服务器中途关闭连接"


def test_iperf_defaults_to_hong_kong_preset(monkeypatch, tmp_path):
    api = load_api(tmp_path)
    api.IPERF3_COMMAND = "iperf3"
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=_iperf_payload(), stderr="")

    monkeypatch.setattr(api.subprocess, "run", fake_run)
    monkeypatch.setattr(api, "_host_exec_env", lambda: {})

    api.run_iperf_test({"duration": 3, "parallel": 1, "direction": "upload"})

    assert calls[0][calls[0].index("-c") + 1] == "speedtest.hkg12.hk.leaseweb.net"
    assert calls[0][calls[0].index("-p") + 1] == "5201"


def test_iperf_bidirectional_test_shares_one_total_deadline(monkeypatch, tmp_path):
    api = load_api(tmp_path)
    api.IPERF3_TIMEOUT = 60
    deadlines = []

    def fake_direction(_host, _ports, _duration, _parallel, reverse, deadline, *_args):
        deadlines.append(deadline)
        return {"mbps": 100.0, "seconds": 3.0, "bytes": 1, "retransmits": 0,
                "sender": {}, "receiver": {}, "intervals": [], "port": 5201,
                "reverse": reverse}

    monkeypatch.setattr(api, "_run_iperf_direction", fake_direction)
    result = api.run_iperf_test({"duration": 3, "parallel": 1, "direction": "both"})

    assert len(result["results"]) == 2
    assert deadlines[0] == deadlines[1]
    assert api.iperf_status_payload()["maxSeconds"] == 60


def test_iperf_tasks_are_isolated_stoppable_and_report_conflict(monkeypatch, tmp_path):
    api = load_api(tmp_path)
    started_threads = []

    class DeferredThread:
        def __init__(self, target, args, **_kwargs):
            self.target = target
            self.args = args

        def start(self):
            started_threads.append(self)

    monkeypatch.setattr(api.threading, "Thread", DeferredThread)
    first = api.start_iperf_task({"server": "speed.example.test"})
    task_id = first["taskId"]

    assert task_id.startswith("iperf-")
    assert api.iperf_status_payload(task_id)["state"] == "queued"
    assert len(started_threads) == 1
    with pytest.raises(api.DiagnosticError) as conflict:
        api.start_iperf_task({"server": "other.example.test"})
    assert conflict.value.status == 409
    assert conflict.value.payload["taskId"] == task_id

    stopped = api.stop_iperf_task({"taskId": task_id})
    assert stopped["state"] == "stopping"
    assert api.IPERF_CANCEL_EVENTS[task_id].is_set()


def test_iperf_history_keeps_five_and_survives_memory_expiry(tmp_path):
    api = load_api(tmp_path)
    for index in range(7):
        api._save_iperf_history({
            "taskId": f"task-{index}",
            "state": "complete",
            "server": f"node-{index}.example.test",
            "finishedAt": index,
            "results": [],
        })

    payload = api.iperf_history_payload()
    assert [item["taskId"] for item in payload["history"]] == [
        "task-6", "task-5", "task-4", "task-3", "task-2",
    ]
    assert api.IPERF_TASKS == {}
    assert api.iperf_status_payload("task-4")["server"] == "node-4.example.test"


def test_managed_iperf_command_does_not_deadlock_on_large_json_output(tmp_path):
    api = load_api(tmp_path)
    completed = api._execute_iperf_command(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 200000)"],
        timeout=5,
        task_id="large-output",
    )

    assert completed.returncode == 0
    assert len(completed.stdout) == 200000
    assert "large-output" not in api.IPERF_PROCESSES


def test_cisco_dhcp_pool_parser_reports_capacity_and_thresholds(tmp_path):
    api = load_api(tmp_path)

    pools = api.parse_cisco_dhcp_pools(DHCP_POOL_OUTPUT)

    assert pools[0] == {
        "name": "PLAYERS",
        "range": "192.168.40.1 - 192.168.40.254",
        "total": 254,
        "leased": 81,
        "excluded": 10,
        "available": 163,
        "utilization": 33.2,
        "level": "good",
    }
    assert pools[1]["utilization"] == 90.4
    assert pools[1]["level"] == "bad"


def test_sanitized_c3850_fixture_matches_field_output_shape(tmp_path):
    api = load_api(tmp_path)
    fixtures = os.path.join(os.path.dirname(__file__), "fixtures")
    with open(os.path.join(fixtures, "c3850_dhcp_pool_sanitized.txt"), encoding="utf-8") as handle:
        pools = api.parse_cisco_dhcp_pools(handle.read())
    with open(os.path.join(fixtures, "c3850_dhcp_aux_sanitized.txt"), encoding="utf-8") as handle:
        auxiliary = handle.read()

    assert [pool["name"] for pool in pools] == ["PLAYER-A", "OFFICE-A", "EMPTY-A"]
    assert pools[0]["available"] == 34
    assert pools[2]["utilization"] == 0
    assert api.parse_cisco_dhcp_conflicts(auxiliary) == []
    assert api.parse_cisco_dhcp_statistics(auxiliary)["automaticBindings"] == 171
    excluded = api.parse_cisco_dhcp_excluded(auxiliary)
    assert len(excluded) == 190
    api.attach_dhcp_pool_exclusions(pools, excluded)
    assert pools[0]["excludedAddresses"][0] == "198.18.0.101"


def test_cisco_dhcp_conflict_and_statistics_parsers(tmp_path):
    api = load_api(tmp_path)

    conflicts = api.parse_cisco_dhcp_conflicts("""
IP address        Detection method   Detection time          VRF
192.168.40.55     Ping               Jul 17 2026 10:00 AM
192.168.40.55     Gratuitous ARP     Jul 17 2026 10:01 AM
192.168.41.9      Ping               Jul 17 2026 10:02 AM
""")
    statistics = api.parse_cisco_dhcp_statistics("""
Automatic bindings 194
Manual bindings 2
Expired bindings 7
Malformed messages 1
""")

    assert conflicts == ["192.168.40.55", "192.168.41.9"]
    assert statistics == {
        "automaticBindings": 194,
        "manualBindings": 2,
        "expiredBindings": 7,
        "malformedMessages": 1,
    }


def test_cisco_dhcp_binding_parser_accepts_ios_variants(tmp_path):
    api = load_api(tmp_path)
    bindings = api.parse_cisco_dhcp_bindings("""
IP address       Client-ID/              Lease expiration        Type
192.168.40.21    0100.1122.3344.55       Jul 22 2026 10:00 AM    Automatic
192.168.41.8     aabb.ccdd.eeff          Infinite                Manual
""")

    assert [item["ip"] for item in bindings] == ["192.168.40.21", "192.168.41.8"]
    assert "0100.1122.3344.55" in bindings[0]["detail"]


def test_cisco_dhcp_binding_parser_keeps_wrapped_address_only_rows(tmp_path):
    api = load_api(tmp_path)
    bindings = api.parse_cisco_dhcp_bindings("""
IP address       Client-ID/              Lease expiration        Type
192.168.58.121
                    0100.1122.3344.55    Jul 22 2026 10:00 AM    Automatic
""")
    assert bindings == [{"ip": "192.168.58.121", "detail": ""}]


def test_cisco_arp_parser_keeps_only_complete_ipv4_neighbours(tmp_path):
    api = load_api(tmp_path)
    entries = api.parse_cisco_arp_entries("""
Protocol  Address          Age (min)  Hardware Addr   Type   Interface
Internet  192.168.42.1            2   aabb.ccdd.eeff  ARPA   Vlan42
Internet  192.168.42.26           -   0011.2233.4455  ARPA   Vlan42
Internet  192.168.42.27           0   Incomplete      ARPA
""")

    assert entries == [
        {"ip": "192.168.42.1", "detail": "ARP aabb.ccdd.eeff · Vlan42"},
        {"ip": "192.168.42.26", "detail": "ARP 0011.2233.4455 · Vlan42"},
    ]


def test_cisco_dhcp_exclusions_expand_and_attach_to_matching_pool(tmp_path):
    api = load_api(tmp_path)
    exclusions = api.parse_cisco_dhcp_excluded("""
ip dhcp excluded-address 192.168.40.1 192.168.40.3
ip dhcp excluded-address 192.168.40.10
ip dhcp excluded-address 192.168.41.1
""")
    pools = api.parse_cisco_dhcp_pools(DHCP_POOL_OUTPUT)
    api.attach_dhcp_pool_exclusions(pools, exclusions)

    assert exclusions == ["192.168.40.1", "192.168.40.2", "192.168.40.3", "192.168.40.10", "192.168.41.1"]
    assert pools[0]["excludedAddresses"] == ["192.168.40.1", "192.168.40.2", "192.168.40.3", "192.168.40.10"]
    assert pools[1]["excludedAddresses"] == ["192.168.41.1"]


def test_dhcp_dashboard_reuses_configured_core_and_short_cache(monkeypatch, tmp_path):
    api = load_api(tmp_path)
    api.CONFIG_PATH.write_text("devices:\n  core:\n    ip: 192.168.10.254\n", encoding="utf-8")
    api.DHCP_CACHE.clear()
    api.DHCP_REFRESH_SECONDS = 60
    calls = []

    def fake_collect(host):
        calls.append(host)
        return {
            "ok": True,
            "host": host,
            "source": "devices.core.ip",
            "pools": [],
            "conflicts": [],
            "statistics": {},
            "summary": {"poolCount": 0},
            "warnings": [],
        }

    monkeypatch.setattr(api, "collect_cisco_dhcp", fake_collect)

    first = api.get_dhcp_dashboard()
    second = api.get_dhcp_dashboard()
    forced = api.get_dhcp_dashboard(force=True)

    assert first["host"] == "192.168.10.254"
    assert first["cached"] is False
    assert second["cached"] is True
    assert forced["cached"] is True
    assert calls == ["192.168.10.254"]


def test_dhcp_collection_uses_one_session_and_skips_full_binding_list(monkeypatch, tmp_path):
    api = load_api(tmp_path)
    opened = []
    commands = []

    class FakeSession:
        def __init__(self):
            self.closed = False

        def write(self, _data):
            pass

        def close(self):
            self.closed = True

    session = FakeSession()
    monkeypatch.setattr(api, "_open_cisco_telnet", lambda host: opened.append(host) or session)

    def fake_command(_session, command):
        commands.append(command)
        return {
            "show ip dhcp pool": DHCP_POOL_OUTPUT,
            "show ip dhcp conflict": "No conflicts detected",
            "show ip dhcp server statistics": "Automatic bindings 194",
            "show running-config | include ^ip dhcp excluded-address": (
                "ip dhcp excluded-address 192.168.40.1 192.168.40.10"
            ),
        }.get(command, "")

    monkeypatch.setattr(api, "_telnet_command", fake_command)

    result = api.collect_cisco_dhcp("192.168.10.254")

    assert result["summary"]["poolCount"] == 2
    assert opened == ["192.168.10.254"]
    assert commands == [
        "terminal length 0",
        "show ip dhcp pool",
        "show ip dhcp conflict",
        "show ip dhcp server statistics",
        "show running-config | include ^ip dhcp excluded-address",
    ]
    assert "show ip dhcp binding" not in commands
    assert result["pools"][0]["excludedAddresses"] == [f"192.168.40.{value}" for value in range(1, 11)]
    assert session.closed is True


def test_full_dhcp_bindings_are_only_read_by_manual_endpoint(monkeypatch, tmp_path):
    api = load_api(tmp_path)
    api.CONFIG_PATH.write_text("devices:\n  core:\n    ip: 192.168.10.254\n", encoding="utf-8")
    commands = []

    class FakeSession:
        def write(self, _data):
            pass

        def close(self):
            pass

    monkeypatch.setattr(api, "_open_cisco_telnet", lambda _host: FakeSession())
    def fake_command(_session, command):
        commands.append(command)
        if command == "show ip dhcp binding":
            return "192.168.40.21 0100.1122.3344.55 Jul 22 2026 Automatic"
        if command == "show ip arp":
            return "Internet  192.168.40.5  1  aabb.ccdd.eeff  ARPA  Vlan40"
        return ""

    monkeypatch.setattr(api, "_telnet_command", fake_command)

    result = api.get_dhcp_bindings()

    assert result["usedAddresses"] == ["192.168.40.21"]
    assert result["observedAddresses"] == ["192.168.40.5"]
    assert commands == ["terminal length 0", "show ip dhcp binding", "show ip arp"]


def test_telnet_command_handles_more_prompts_and_strict_device_prompt(tmp_path):
    api = load_api(tmp_path)

    class FakeSession:
        def __init__(self):
            self.writes = []
            self.outputs = iter([
                (1, None, b"show ip dhcp pool\r\nfirst page\r\n--More--"),
                (0, None, b"second page\r\ncore-sw#\r\n"),
            ])

        def write(self, data):
            self.writes.append(data)

        def expect(self, _patterns, _timeout):
            return next(self.outputs)

    session = FakeSession()
    output = api._telnet_command(session, "show ip dhcp pool")

    assert output == "first page\nsecond page"
    assert session.writes == [b"show ip dhcp pool\n", b" "]
    assert api.re.search(api.CISCO_PROMPT_RE, b"counter value is >\r\n") is None
    assert api.re.search(api.CISCO_PROMPT_RE, b"core-sw#\r\n") is not None


def test_dhcp_connection_test_only_checks_login_and_privilege(monkeypatch, tmp_path):
    api = load_api(tmp_path)
    api.CONFIG_PATH.write_text("devices:\n  core:\n    ip: 192.168.10.254\n", encoding="utf-8")
    commands = []

    class FakeSession:
        def __init__(self):
            self.closed = False

        def write(self, _data):
            pass

        def close(self):
            self.closed = True

    session = FakeSession()
    monkeypatch.setattr(api, "_open_cisco_telnet", lambda host: session)
    monkeypatch.setattr(
        api,
        "_telnet_command",
        lambda _session, command: commands.append(command) or "Current privilege level is 15",
    )

    result = api.test_dhcp_connection()

    assert result["host"] == "192.168.10.254"
    assert result["login"] is True
    assert result["privileged"] is True
    assert result["privilegeLevel"] == 15
    assert commands == ["show privilege"]
    assert session.closed is True


def test_dhcp_console_settings_are_private_and_override_environment(tmp_path):
    api = load_api(tmp_path)
    api.CONFIG_PATH.write_text("devices:\n  core:\n    ip: 192.168.10.254\n", encoding="utf-8")
    api.DHCP_SWITCH_USERNAME = "env-user"
    api.DHCP_SWITCH_PASSWORD = "env-password"
    api.DHCP_SWITCH_ENABLE_PASSWORD = ""
    api.DHCP_SWITCH_PORT = 23

    saved = api.save_dhcp_settings({
        "username": "console-user",
        "password": "console-password",
        "enablePassword": "enable-password",
        "port": 2323,
    })

    assert saved["username"] == "console-user"
    assert saved["passwordConfigured"] is True
    assert saved["enablePasswordConfigured"] is True
    assert "password" not in saved
    assert "enablePassword" not in saved
    stored = json.loads(api.DHCP_SETTINGS_PATH.read_text(encoding="utf-8"))
    assert stored["password"] == "console-password"
    assert stored["enablePassword"] == "enable-password"
    # Windows does not expose POSIX chmod bits through stat(). Production runs
    # in the Linux platform-api container, where the private mode is enforced.
    if os.name != "nt":
        assert api.DHCP_SETTINGS_PATH.stat().st_mode & 0o077 == 0
    runtime = api.dhcp_connection_settings()
    assert runtime["username"] == "console-user"
    assert runtime["password"] == "console-password"
    assert runtime["port"] == 2323

    api.save_dhcp_settings({"username": "renamed", "password": "", "enablePassword": "", "port": 23})
    preserved = api.dhcp_connection_settings()
    assert preserved["username"] == "renamed"
    assert preserved["password"] == "console-password"
    assert preserved["enablePassword"] == "enable-password"


def test_platform_api_image_contains_iperf_and_telnet_clients(tmp_path):
    api = load_api(tmp_path)
    root = api.Path(__file__).resolve().parents[1]

    dockerfile = (root / "docker" / "platform-api" / "Dockerfile").read_text(encoding="utf-8")
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")

    assert "iperf3" in dockerfile
    assert "PLATFORM_IPERF3_COMMAND" in compose
    assert 'PLATFORM_IPERF3_COMMAND:-iperf3' in compose
    assert "docker exec player-targets iperf3" not in compose
    assert "monitor-platform-api:local" in compose
    assert "docker/platform-api" in compose
    assert "telnetlib3==4.0.5" in dockerfile


def test_dhcp_page_and_platform_service_wiring(tmp_path):
    api = load_api(tmp_path)
    root = api.Path(__file__).resolve().parents[1]
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    pages = (root / "bigscreen" / "pages.js").read_text(encoding="utf-8")
    app = (root / "bigscreen" / "app.js").read_text(encoding="utf-8")
    iperf_ui = (root / "bigscreen" / "iperf.js").read_text(encoding="utf-8")
    iperf_nodes = json.loads((root / "bigscreen" / "iperf-servers.json").read_text(encoding="utf-8"))
    index = (root / "bigscreen" / "index.html").read_text(encoding="utf-8")

    assert "PLATFORM_DHCP_SWITCH_PASSWORD" in compose
    assert 'path: "/dhcp"' in pages
    assert 'id="dhcpPanel"' in index
    assert 'id="dhcpConnectionTest"' not in index
    assert 'id="controlDhcpSettingsForm"' in app
    assert 'id="controlDhcpHost"' not in app
    assert 'href="/control#core-telnet"' in app
    assert "核心/防火墙" in app
    assert "testDhcpConnection" in app
    assert "fetchDhcpSettings" in app
    assert app.index('postPlatform("/config/save"') < app.index("saveDhcpSettings(credentials)")
    configured_nodes = json.dumps(iperf_nodes, ensure_ascii=False)
    assert "speedtest.hkg12.hk.leaseweb.net" in configured_nodes
    assert "speedtest.sin1.sg.leaseweb.net" in configured_nodes
    assert "iperf.scbd.net.id" in configured_nodes
    assert "23.249.58.14" in configured_nodes
    assert "84.17.57.129" in configured_nodes
    assert "sgp.proof.ovh.net" in configured_nodes
    assert "speedtest.tangerang2.myrepublic.net.id" in configured_nodes
    assert "土耳其·伊斯坦布尔" in app
    assert "中国大陆（自有服务器）" not in app
    assert "马来西亚（自有服务器）" not in app
    assert "泰国（自有服务器）" not in app
    assert '<option value="custom">自定义</option>' in app
    assert "window.confirm" not in app
    assert 'id="iperfConfirm"' in app
    assert 'id="iperfProgress"' in app
    assert "fetchIperfStatus" in app
    assert "最长约 60 秒" in app
    assert 'id="iperfPublicServer"' in app
    assert 'id="iperfPorts"' in app and "readonly" in app
    assert "iperfServer.readOnly = !view.isCustom" in app
    assert "iperfPorts.readOnly = !view.isCustom" in app
    assert "iperf-interval-table" in iperf_ui
    assert "接收端全程平均" in iperf_ui
    assert 'src="./iperf.js' in index
    assert 'document.visibilityState === "hidden"' in app
    assert "stopDhcpRefresh()" in app


def test_network_overview_precedes_dhcp_and_non_24_pools_are_grouped_by_c_block(tmp_path):
    api = load_api(tmp_path)
    root = api.Path(__file__).resolve().parents[1]
    pages = (root / "bigscreen" / "pages.js").read_text(encoding="utf-8")
    app = (root / "bigscreen" / "app.js").read_text(encoding="utf-8")
    css = (root / "bigscreen" / "platform.css").read_text(encoding="utf-8")

    assert pages.index('id: "infra"') < pages.index('id: "dhcp"')
    assert "dhcp-address-blocks" in app
    assert "${block.prefix}.0/24" in app
    assert 'addressBlockCount > 1 ? " multi-block"' in app
    assert 'id="dhcpPoolSearch"' in (root / "bigscreen" / "index.html").read_text(encoding="utf-8")
    assert "renderDhcpPoolBrowser" in app
    assert ".dhcp-address-block" in css
    assert ".dhcp-pool-directory" in css
    assert ".dhcp-pool-detail" in css
    assert ".dhcp-pool-card.multi-block" in css
    assert "grid-template-columns: 300px minmax(0, 1fr)" in css
    assert 'const directoryScrollTop = previousDirectory ? previousDirectory.scrollTop : 0;' in app
    assert 'nextDirectory.scrollTop = selectionChanged ? 0 : directoryScrollTop' in app
    assert 'if (detail) detail.scrollTop = 0;' in app
    assert "查询已用 IP" in app
    assert "/network/dhcp/bindings" in (root / "bigscreen" / "api.js").read_text(encoding="utf-8")
    assert "content-visibility: auto" not in css


def test_config_editor_migrates_legacy_feishu_env_credentials(tmp_path):
    api = load_api(tmp_path)
    api.CONFIG_PATH.write_text(
        "devices:\n  core:\n    ip: 192.168.10.254\nalerts:\n  feishu_robot_token:\n",
        encoding="utf-8",
    )
    api.ENV_PATH.write_text(
        "FEISHU_APP_ID=cli_legacy\nFEISHU_APP_SECRET=legacy-secret\nFEISHU_CHAT_ID=oc_legacy\n",
        encoding="utf-8",
    )

    payload = api.config_payload()
    assert payload["config"]["alerts"]["feishu_app_id"] == "cli_legacy"
    assert payload["config"]["alerts"]["feishu_app_secret"] == "legacy-secret"
    assert payload["config"]["alerts"]["feishu_chat_id"] == "oc_legacy"


def test_iperf_internal_targets_gated_by_env(tmp_path):
    api = load_api(tmp_path)
    # 字面量内网/环回地址一律判内网;公网字面量放行
    assert iperf._iperf_target_is_internal("192.168.10.5") is True
    assert iperf._iperf_target_is_internal("10.0.0.1") is True
    assert iperf._iperf_target_is_internal("127.0.0.1") is True
    assert iperf._iperf_target_is_internal("169.254.169.254") is True
    assert iperf._iperf_target_is_internal("23.249.58.14") is False
    # 默认开关关闭
    assert api.IPERF3_ALLOW_INTERNAL is False
    with pytest.raises(api.DiagnosticError) as exc:
        api.run_iperf_test({"server": "192.168.10.5"})
    assert "PLATFORM_IPERF3_ALLOW_INTERNAL" in str(exc.value.payload.get("error"))
    # 打开开关后内网地址通过校验(走到端口/参数阶段而不是被内网拦截)
    api.IPERF3_ALLOW_INTERNAL = True
    try:
        with pytest.raises(api.DiagnosticError) as exc2:
            api.run_iperf_test({"server": "192.168.10.5", "duration": 99})
        assert "测试时长" in str(exc2.value.payload.get("error"))
    finally:
        api.IPERF3_ALLOW_INTERNAL = False


def test_iperf_json_rejects_non_object_payloads(tmp_path):
    api = load_api(tmp_path)
    for bad in ("[1,2,3]", "42", '"text"'):
        with pytest.raises(ValueError):
            iperf.parse_iperf3_json(bad)
    # 非 dict 的嵌套结构不炸 AttributeError,按 ValueError 处理
    with pytest.raises(ValueError):
        iperf.parse_iperf3_json(json.dumps({"end": [], "intervals": "x"}))


def test_dhcp_credentials_reject_control_characters(tmp_path):
    api = load_api(tmp_path)
    api.CONFIG_PATH.write_text("devices:\n  core:\n    ip: 192.168.10.254\n", encoding="utf-8")
    with pytest.raises(api.DiagnosticError):
        api.save_dhcp_settings({"username": "admin\nshow run", "password": "x", "enablePassword": "", "port": 23})
    with pytest.raises(api.DiagnosticError):
        api.save_dhcp_settings({"username": "admin", "password": "pass\r\nenable", "enablePassword": "", "port": 23})
