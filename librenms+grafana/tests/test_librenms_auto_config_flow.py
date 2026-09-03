import os
from pathlib import Path
import shlex
import shutil
import subprocess
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[1]
AUTO_CONFIG = ROOT / "librenms-auto-config.sh"
SH = shutil.which("sh")
if not SH:
    windows_sh = Path(r"C:\Program Files\Git\usr\bin\sh.exe")
    if windows_sh.is_file():
        SH = str(windows_sh)


pytestmark = pytest.mark.skipif(not SH, reason="POSIX shell is unavailable")


def _extract_shell_function(source: str, name: str) -> str:
    start = source.index(f"{name}() {{")
    end = source.index("\n}\n", start) + len("\n}\n")
    return source[start:end]


def _auto_config_device_phase(source: str) -> str:
    start = source.index(
        'echo "[4b/5] Adding ping-only devices (ISP / Firewall / Servers)..."'
    )
    end_marker = 'echo "[6/6] Setting up alert rules..."'
    end = source.index(end_marker, start) + len(end_marker)
    return source[start:end]


def _write_shell(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8", newline="\n")
    path.chmod(0o755)


def _run_device_phase(tmp_path: Path, *, real_retire=False, **environment):
    source = AUTO_CONFIG.read_text(encoding="utf-8")
    flow_log = tmp_path / "flow.log"
    harness = tmp_path / "auto-config-device-phase.sh"
    if real_retire:
        retire_setup = f"""
PLAYER_SUBNETS="${{PLAYER_SUBNETS:-}}"
DISCOVERY_TARGETS="${{DISCOVERY_TARGETS:-}}"
CORE_SWITCH_PING="${{CORE_SWITCH_PING:-}}"
DIST_SWITCH_PING="${{DIST_SWITCH_PING:-}}"
TOURNAMENT_SWITCHES="${{TOURNAMENT_SWITCHES:-}}"
PLAYER_GATEWAYS="${{PLAYER_GATEWAYS:-}}"
DEVICES_JSON="${{DEVICES_JSON:-{{\"devices\":[]}}}}"
RETIRE_OUTPUT="${{RETIRE_OUTPUT:-}}"
RETIRE_PYTHON_EXIT="${{RETIRE_PYTHON_EXIT:-0}}"

curl() {{
  printf '%s' "$DEVICES_JSON"
}}

python3() {{
  printf 'retire-cli|%s\n' "$*" >> "$FLOW_LOG"
  while IFS= read -r _line; do :; done
  [ "$RETIRE_PYTHON_EXIT" -eq 0 ] || return "$RETIRE_PYTHON_EXIT"
  [ -z "$RETIRE_OUTPUT" ] || printf '%s\n' "$RETIRE_OUTPUT"
}}

disable_librenms_device_api() {{
  printf 'disable|%s\n' "$1" >> "$FLOW_LOG"
  return 0
}}

{_extract_shell_function(source, "retire_unmanaged_player_devices")}
"""
    else:
        retire_setup = (
            "retire_unmanaged_player_devices() { "
            "echo 'downstream|retire' >> \"$FLOW_LOG\"; }\n"
        )
    harness_source = f"""#!/bin/sh
set -e

FLOW_LOG={shlex.quote(flow_log.as_posix())}
ISP_PING="${{ISP_PING:-}}"
BIGSCREEN_ISP_IPS="${{BIGSCREEN_ISP_IPS:-}}"
FIREWALL_PING="${{FIREWALL_PING:-}}"
FIREWALL_SNMP_TARGETS="${{FIREWALL_SNMP_TARGETS:-}}"
FIREWALL_UNIT_SNMP_TARGETS="${{FIREWALL_UNIT_SNMP_TARGETS:-}}"
FIREWALL_SNMP_COMMUNITY="${{FIREWALL_SNMP_COMMUNITY:-global}}"
SERVER_PING="${{SERVER_PING:-}}"
API_TOKEN="${{API_TOKEN:-test-token}}"
FAIL_PING_IP="${{FAIL_PING_IP:-}}"

parse_named_targets() {{
  targets=${{1:-}}
  old_ifs=$IFS
  IFS=','
  for target in $targets; do
    IFS=$old_ifs
    case "$target" in
      *:*) name=${{target%%:*}}; ip=${{target#*:}} ;;
      *) name=""; ip=$target ;;
    esac
    [ -n "$ip" ] && printf '%s|%s\n' "$name" "$ip"
    IFS=','
  done
  IFS=$old_ifs
}}

add_ping_device_api() {{
  printf 'ping|%s|%s\n' "$1" "$2" >> "$FLOW_LOG"
  [ "$2" != "$FAIL_PING_IP" ]
}}

add_device_api() {{
  printf 'snmp|%s|%s|%s\n' "$1" "$2" "$3" >> "$FLOW_LOG"
  return 0
}}

{retire_setup}
discover_firewall_ports() {{ echo 'downstream|firewall-ports' >> "$FLOW_LOG"; }}
configure_isp_port_speed_overrides() {{ echo 'downstream|isp-speed' >> "$FLOW_LOG"; }}
configure_home_dashboard() {{ echo 'downstream|dashboard' >> "$FLOW_LOG"; }}
configure_down_port_ignores() {{ echo 'downstream|down-port-ignores' >> "$FLOW_LOG"; }}
configure_stp_noise_suppression() {{ echo 'downstream|stp' >> "$FLOW_LOG"; }}

{_extract_shell_function(source, "add_optional_ping_devices")}

{_auto_config_device_phase(source)}
"""
    _write_shell(harness, harness_source)
    run_env = os.environ.copy()
    run_env["PATH"] = os.pathsep.join(
        (str(Path(SH).parent), run_env.get("PATH", ""))
    )
    run_env.update({key: str(value) for key, value in environment.items()})
    completed = subprocess.run(
        [SH, str(harness)],
        cwd=tmp_path,
        env=run_env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    log = flow_log.read_text(encoding="utf-8") if flow_log.exists() else ""
    return completed, log


def _librenms_config_wrapper() -> str:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    service = compose.split("  librenms-config:", 1)[1].split("  grafana:", 1)[0]
    command = service.split("      - |\n", 1)[1]
    return textwrap.dedent(command).replace("$$", "$")


def _run_compose_wrapper(tmp_path: Path, exit_code: int):
    auto_config = tmp_path / "librenms-auto-config.sh"
    _write_shell(auto_config, f"#!/bin/sh\nexit {exit_code}\n")
    command = _librenms_config_wrapper()
    command = command.replace("sleep 30", ": # test skips initialization delay")
    command = command.replace(
        "/bin/sh /librenms-auto-config.sh",
        f"/bin/sh {shlex.quote(auto_config.as_posix())}",
    )
    harness = tmp_path / "compose-wrapper.sh"
    _write_shell(harness, f"#!/bin/sh\n{command}")
    return subprocess.run(
        [SH, str(harness)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )


def test_optional_server_failure_does_not_block_ha_firewalls_or_later_steps(
    tmp_path,
):
    completed, log = _run_device_phase(
        tmp_path,
        ISP_PING="telecom:192.0.2.1",
        FIREWALL_PING="192.168.9.1",
        FIREWALL_SNMP_TARGETS="192.168.9.1",
        FIREWALL_UNIT_SNMP_TARGETS="192.168.9.11,192.168.9.12",
        FIREWALL_SNMP_COMMUNITY="global",
        SERVER_PING="server1:192.168.42.201,server2:192.168.42.202",
        FAIL_PING_IP="192.168.42.201",
    )

    assert completed.returncode == 0
    assert "optional ping-only device failed: server1 (192.168.42.201)" in completed.stderr
    assert "ping|server1|192.168.42.201" in log
    assert "ping|server2|192.168.42.202" in log
    assert "snmp||192.168.9.11|global" in log
    assert "snmp||192.168.9.12|global" in log
    assert "downstream|retire" in log
    assert "downstream|firewall-ports" in log
    assert "downstream|isp-speed" in log
    assert "downstream|dashboard" in log
    assert "downstream|down-port-ignores" in log
    assert "downstream|stp" in log
    assert "[6/6] Setting up alert rules..." in completed.stdout


def test_ping_only_enrollment_deduplicates_by_ip_and_prefers_nonempty_name(
    tmp_path,
):
    completed, log = _run_device_phase(
        tmp_path,
        FIREWALL_PING="192.168.9.1",
        FIREWALL_SNMP_TARGETS="HA VIP:192.168.9.1",
        FIREWALL_UNIT_SNMP_TARGETS="192.168.9.11,192.168.9.12",
    )

    assert completed.returncode == 0
    vip_calls = [line for line in log.splitlines() if line.endswith("|192.168.9.1")]
    assert vip_calls == ["ping|HA VIP|192.168.9.1"]


def test_real_retire_path_disables_candidate_and_continues_through_ha_flow(
    tmp_path,
):
    completed, log = _run_device_phase(
        tmp_path,
        real_retire=True,
        PLAYER_SUBNETS="192.168.70.0/24",
        RETIRE_OUTPUT="192.168.70.100",
        FIREWALL_SNMP_TARGETS="192.168.9.1",
        FIREWALL_UNIT_SNMP_TARGETS="192.168.9.11,192.168.9.12",
        FIREWALL_SNMP_COMMUNITY="global",
    )

    assert completed.returncode == 0
    assert "retire-cli|/target_utils.py retire-player-candidates" in log
    assert "disable|192.168.70.100" in log
    assert "snmp||192.168.9.11|global" in log
    assert "snmp||192.168.9.12|global" in log
    assert "[6/6] Setting up alert rules..." in completed.stdout


def test_retire_python_failure_is_fatal_and_stops_later_configuration(tmp_path):
    completed, log = _run_device_phase(
        tmp_path,
        real_retire=True,
        PLAYER_SUBNETS="192.168.70.0/24",
        RETIRE_PYTHON_EXIT="9",
        FIREWALL_UNIT_SNMP_TARGETS="192.168.9.11,192.168.9.12",
    )

    assert completed.returncode == 9
    assert "retire-cli|/target_utils.py retire-player-candidates" in log
    assert "snmp|" not in log
    assert "[6/6] Setting up alert rules..." not in completed.stdout


def test_librenms_config_wrapper_propagates_fatal_auto_config_exit(tmp_path):
    completed = _run_compose_wrapper(tmp_path, 7)

    assert completed.returncode == 7
    assert "LibreNMS auto-configuration failed (exit=7)" in completed.stderr
    assert "UniFi APs: optional" not in completed.stdout


def test_librenms_config_wrapper_returns_zero_after_success(tmp_path):
    completed = _run_compose_wrapper(tmp_path, 0)

    assert completed.returncode == 0
    assert "LibreNMS auto-configuration failed" not in completed.stderr
    assert "UniFi APs: optional" in completed.stdout
