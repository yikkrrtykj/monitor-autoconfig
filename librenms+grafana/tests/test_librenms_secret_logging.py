import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from .test_librenms_auto_config_flow import (
    AUTO_CONFIG, SH, _extract_shell_function, _write_shell, _run_compose_wrapper,
)


PASSWORD = "FAKE_ADMIN_BATCH2_78!"
COMMUNITY = "FAKE_COMMUNITY_BATCH2_93"


def run_path(tmp_path, body, setup=""):
    log = tmp_path / "internal-arguments.txt"
    script = tmp_path / "harness.sh"
    _write_shell(script, "#!/bin/sh\n" + setup + "\n" + body)
    env = {"PATH": os.pathsep.join((str(Path(SH).parent), os.environ.get("PATH", ""))),
           "LIBRENMS_ADMIN_PASSWORD": PASSWORD, "SNMP_COMMUNITY": COMMUNITY,
           "LIBRENMS_ADMIN_USER": "test-admin", "LIBRENMS_ADMIN_EMAIL": "test@example.invalid",
           "LIBRENMS_URL": "http://example.invalid", "API_TOKEN": "fake-token",
           "SNMP_VERSION": "v2c", "INTERNAL_LOG": log.as_posix(),
           "LIBRENMS_FORCE_BASE_URL": "false", "SERVER_IP": "192.0.2.1",
           "LIBRENMS_PORT": "8002", "DISCOVERY_TARGETS": "", "CORE_IP": "",
           "LIBRENMS_DISCOVERY_PING_TIMEOUT_MS": "500", "SNMP_TIMEOUT": "1",
           "SNMP_RETRIES": "0", "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
    result = subprocess.run([SH, str(script)], env=env, cwd=tmp_path,
                            capture_output=True, text=True, timeout=20)
    output = result.stdout + result.stderr
    return result.returncode, output, log.read_text() if log.exists() else ""


def assert_safe(output):
    assert PASSWORD not in output
    assert COMMUNITY not in output


@pytest.mark.parametrize("phase", ["startup", "completion"])
def test_real_summaries_hide_secrets(tmp_path, phase):
    source = AUTO_CONFIG.read_text(encoding="utf-8")
    if phase == "startup":
        body = source[source.index('echo "[4/6]'):source.index('discovery_candidates=$(mktemp)')]
    else:
        body = source[source.index('echo "  LibreNMS Discovery Complete!"'):]
    code, output, _ = run_path(tmp_path, body)
    assert code == 0
    assert_safe(output)
    assert "configured" in output or "masked" in output
    assert "SNMP Community:" in output


@pytest.mark.parametrize("mode", ["success", "failure", "database", "already"])
def test_admin_actual_arguments_and_safe_failure_output(tmp_path, mode):
    source = AUTO_CONFIG.read_text(encoding="utf-8")
    setup = '''
sleep() { :; }
has_lnms_cmd() { [ "$TEST_MODE" != database ]; }
run_lnms() {
  printf '%s\n' "$@" >> "$INTERNAL_LOG"
  echo "raw $LIBRENMS_ADMIN_PASSWORD $SNMP_COMMUNITY $TEST_MODE"
  [ "$TEST_MODE" = success ]
}
upsert_admin_user() {
  echo "$LIBRENMS_ADMIN_PASSWORD" >> "$INTERNAL_LOG"
  echo "raw $LIBRENMS_ADMIN_PASSWORD $SNMP_COMMUNITY" >&2
  return 1
}
assign_admin_role_lnms() { :; }
'''
    setup += "\nTEST_MODE=" + shlex.quote(mode) + "\n"
    code, output, arguments = run_path(tmp_path, _extract_shell_function(source, "ensure_admin_user") + "\nensure_admin_user\n", setup)
    assert code == 0
    assert PASSWORD in arguments
    assert_safe(output)
    assert "ready" in output or "Waiting" in output


@pytest.mark.parametrize("mode", ["ok", "error"])
def test_device_api_raw_response_is_not_logged(tmp_path, mode):
    source = AUTO_CONFIG.read_text(encoding="utf-8")
    setup = "python3() { " + shlex.quote(sys.executable.replace('\\', '/')) + ' "$@"; }\n'
    setup += '''
curl() {
  printf '%s\n' "$@" >> "$INTERNAL_LOG"
  printf '{"status":"%s","message":"raw %s %s"}' "$TEST_MODE" "$LIBRENMS_ADMIN_PASSWORD" "$SNMP_COMMUNITY"
}
sync_device_snmp_api() { return 1; }
'''
    setup += "TEST_MODE=" + mode + "\n"
    body = source[source.index("api_result_field() {"):source.index("sync_device_snmp_api() {")]
    body += _extract_shell_function(source, "add_device_api")
    code, output, arguments = run_path(tmp_path, body + '\nadd_device_api test 192.0.2.1 "$SNMP_COMMUNITY"\n', setup)
    assert code == 0
    assert COMMUNITY in arguments
    assert_safe(output)
    assert "added" in output or "failed" in output


@pytest.mark.parametrize("exit_code", [0, 1])
def test_cli_fallback_hides_raw_stdout_and_keeps_arguments(tmp_path, exit_code):
    source = AUTO_CONFIG.read_text(encoding="utf-8")
    setup = '''
php() {
  printf '%s\n' "$@" >> "$INTERNAL_LOG"
  echo "raw $LIBRENMS_ADMIN_PASSWORD $SNMP_COMMUNITY"
  echo "raw $LIBRENMS_ADMIN_PASSWORD $SNMP_COMMUNITY" >&2
  return "$CLI_EXIT"
}
'''
    setup += f"CLI_EXIT={exit_code}\n"
    code, output, arguments = run_path(tmp_path, _extract_shell_function(source, "add_device_cli") + '\nadd_device_cli test 192.0.2.1 "$SNMP_COMMUNITY"\n', setup)
    assert code == 0
    assert COMMUNITY in arguments
    assert_safe(output)
    assert ("Added via CLI" if exit_code == 0 else "Already exists or failed") in output


@pytest.mark.parametrize("code", [0, 1])
def test_compose_wrapper_does_not_echo_credentials(tmp_path, monkeypatch, code):
    monkeypatch.setenv("LIBRENMS_ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setenv("LIBRENMS_SNMP_COMMUNITY", COMMUNITY)
    result = _run_compose_wrapper(tmp_path, code)
    assert result.returncode == code
    assert_safe(result.stdout + result.stderr)
