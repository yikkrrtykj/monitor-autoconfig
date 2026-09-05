"""Behavior regressions for truthful deployment completion (external calls mocked)."""

from . import test_deploy_failure_policy as deploy
from .test_librenms_auto_config_flow import _extract_shell_function
import os
import shlex
import subprocess
import shutil
import sys
import time
import pytest


def test_dead_config_container_with_zero_exit_is_not_success(tmp_path):
    completed, log = deploy.run_deploy(tmp_path, STUB_CONFIG_STATE="dead")

    assert completed.returncode != 0
    assert "deploy-check bootstrap" not in log
    assert "compose restart topology-collector" not in log
    assert "compose restart alertmanager-feishu-bridge" not in log


def test_platform_health_probe_preserves_callers_stdin(tmp_path):
    source = (deploy.ROOT / "deploy-check.sh").read_text(encoding="utf-8")
    probe = tmp_path / "probe.sh"
    deploy._write_executable(probe, "#!/bin/sh\n" + """
HTTP_BODY=body
HTTP_ERROR=error
DEPLOY_CHECK_HTTP_TIMEOUT=1
compose() { cat > swallowed; printf '{"ok":true}'; }
record() { :; }
""" + _extract_shell_function(source, "wait_for_platform_api") + "\nwait_for_platform_api\n")
    caller = shlex.quote(probe.as_posix()) + "\nprintf 'CALLER_SENTINEL\\n'\n"
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join((str(deploy.Path(deploy.SH).parent), env.get("PATH", "")))
    completed = subprocess.run([deploy.SH], input=caller, cwd=tmp_path, env=env,
                               capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0
    assert "CALLER_SENTINEL" in completed.stdout
    assert (tmp_path / "swallowed").read_text() == ""


def run_apply_tail(tmp_path, **overrides):
    """Execute the real orchestration tail; config rendering is outside this test."""
    project = tmp_path / "project"
    project.mkdir()
    source = (deploy.ROOT / "apply-env.sh").read_text(encoding="utf-8")
    helper = deploy.ROOT / "deployment-tasks.sh"
    if helper.exists():
        shutil.copy2(helper, project / helper.name)
    prelude = '''#!/bin/sh
set -eu
SCRIPT_DIR=$(pwd)
HOST_PROJECT_DIR=${PLATFORM_HOST_WORKDIR:-}
COMPOSE_CMD="docker compose"
compose() { docker compose "$@"; }
render_env_value() {
  case "$1" in
    COMPOSE_PROFILES) printf '%s' "${TEST_PROFILES:-}" ;;
    FEISHU_APP_ID) printf '%s' "${TEST_APP_ID:-}" ;;
  esac
}
'''
    # Include the production budget setup added immediately before SERVICES.
    marker = '# Deployment completion budget'
    start = source.index(marker) if marker in source else source.index('SERVICES="')
    deploy._write_executable(project / "apply.sh", prelude + source[start:])
    deploy._write_executable(project / "deploy-check.sh",
        '#!/bin/sh\necho "CHECK $*" >> "$STUB_LOG"\nexit 0\n')
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    deploy._write_executable(stub_bin / "docker", deploy.DOCKER_STUB)
    deploy._write_executable(stub_bin / "python3", '#!/bin/sh\nexec ' + shlex.quote(sys.executable.replace('\\', '/')) + ' "$@"\n')
    env = os.environ.copy()
    env.update(PATH=os.pathsep.join((str(stub_bin), str(deploy.Path(deploy.SH).parent), env.get("PATH", ""))),
               STUB_LOG=str(tmp_path / "docker.log"), LIBRENMS_CONFIG_TIMEOUT="0",
               DEPLOY_CHECK_TIMEOUT="30")
    env.update({key: str(value) for key, value in overrides.items()})
    completed = subprocess.run([deploy.SH, "apply.sh"], cwd=project, env=env,
                               capture_output=True, text=True, timeout=15)
    return completed, (tmp_path / "docker.log").read_text()


@pytest.mark.parametrize("entry", ["deploy", "host", "self"])
@pytest.mark.parametrize("failure", [
    {"STUB_GRAFANA_EXIT": "7"}, {"STUB_GRAFANA_STATE": "running"},
    {"STUB_MISSING_TASK": "grafana-setup"}, {"STUB_INSPECT_FAIL": "true"},
    {"STUB_STALE_TASKS": "true"}, {"STUB_PS_FAIL": "true"},
    {"STUB_CONFIG_STATE": "dead"}, {"STUB_CONFIG_STATE": "created"},
    {"STUB_CONFIG_STATE": "restarting"}, {"STUB_LIBRENMS_CONFIG_EXIT": "9"},
    {"STUB_MISSING_TASK": "librenms-config"},
])
def test_required_tasks_fail_closed_for_all_entries(tmp_path, entry, failure):
    if entry == "deploy":
        result, log = deploy.run_deploy(tmp_path, **failure)
    else:
        result, log = run_apply_tail(tmp_path, PLATFORM_API_SELF_APPLY=str(entry == "self").lower(), **failure)
    assert result.returncode != 0
    assert "bootstrap completed successfully" not in result.stdout
    assert "Configuration applied" not in result.stdout
    assert "CHECK configured" not in log


@pytest.mark.parametrize("self_apply", [False, True])
def test_apply_tasks_complete_before_consumers_and_check(tmp_path, self_apply):
    result, log = run_apply_tail(tmp_path, PLATFORM_API_SELF_APPLY=str(self_apply).lower())
    assert result.returncode == 0, result.stderr
    commands = log.splitlines()
    consumer_up = [line for line in commands if line.startswith("compose up") and "topology-collector" in line]
    assert len(consumer_up) == 1
    assert log.index("State.ExitCode") < log.index(consumer_up[0]) < log.index("CHECK configured")
    assert any("platform-api" in line for line in commands if line.startswith("compose up")) != self_apply


@pytest.mark.parametrize("failure", [{}, {"STUB_REMOVE_FAIL":"true"},
    {"STUB_FEISHU_PROJECT":"other"}, {"STUB_FEISHU_SERVICE":"other"}])
def test_disabled_feishu_cleanup_is_project_scoped_and_required(tmp_path, failure):
    result, log = run_apply_tail(tmp_path, STUB_FEISHU_PRESENT="true", **failure)
    if failure:
        assert result.returncode != 0
        assert "Configuration applied" not in result.stdout
        if "STUB_REMOVE_FAIL" not in failure:
            assert "rm -f cid-feishu" not in log
    else:
        assert result.returncode == 0, result.stderr
        assert "rm -f cid-feishu" in log


@pytest.mark.parametrize("profiles,app_id", [("feishu", ""), ("", "app-fixture")])
def test_enabled_feishu_recreates_without_removal(tmp_path, profiles, app_id):
    result, log = run_apply_tail(tmp_path, TEST_PROFILES=profiles, TEST_APP_ID=app_id, STUB_FEISHU_PRESENT="true")
    assert result.returncode == 0, result.stderr
    assert any("feishu-ws" in line for line in log.splitlines() if line.startswith("compose up"))
    assert "rm -f cid-feishu" not in log


def test_failed_startup_does_not_remove_disabled_sidecar(tmp_path):
    result, log = run_apply_tail(tmp_path, STUB_COMPOSE_UP_FAIL="true", STUB_FEISHU_PRESENT="true")
    assert result.returncode != 0
    assert "rm -f cid-feishu" not in log


def test_self_apply_cannot_recreate_api_as_an_implicit_dependency(tmp_path):
    result, log = run_apply_tail(tmp_path, PLATFORM_API_SELF_APPLY="true", STUB_DEPENDENCY_CHECK="true")
    assert result.returncode == 0, result.stderr
    assert "IMPLICIT platform-api" not in log


def test_expired_parent_budget_prevents_any_recreation(tmp_path):
    result, log = run_apply_tail(tmp_path, DEPLOY_CHECK_DEADLINE="1")
    assert result.returncode != 0
    assert "compose up" not in log
    assert "CHECK configured" not in log


def test_host_project_mapping_is_used_by_task_gates(tmp_path):
    result, log = run_apply_tail(tmp_path, PLATFORM_HOST_WORKDIR="/fixture/host-project")
    assert result.returncode == 0, result.stderr
    assert "--project-directory /fixture/host-project ps -a -q librenms-config" in log


def test_feishu_recovery_can_enable_sidecar_after_disabled_apply(tmp_path):
    first, log = run_apply_tail(tmp_path, STUB_FEISHU_PRESENT="true")
    assert first.returncode == 0, first.stderr
    assert "rm -f cid-feishu" in log
    # The existing transaction recovery re-invokes this entrypoint with the
    # restored environment; use the same project and simulated Docker state.
    env = os.environ.copy()
    env.update(PATH=os.pathsep.join((str(tmp_path / "bin"), str(deploy.Path(deploy.SH).parent), env.get("PATH", ""))),
               STUB_LOG=str(tmp_path / "docker.log"), TEST_APP_ID="restored-app",
               LIBRENMS_CONFIG_TIMEOUT="0", DEPLOY_CHECK_TIMEOUT="30")
    # Simulate Docker assigning fresh ids on the recovery recreation.
    (tmp_path / "docker.log.created").unlink()
    restored = subprocess.run([deploy.SH, "apply.sh"], cwd=tmp_path / "project", env=env,
                              capture_output=True, text=True, timeout=15)
    assert restored.returncode == 0, restored.stderr
    recovery_log = (tmp_path / "docker.log").read_text()[len(log):]
    assert any("feishu-ws" in line for line in recovery_log.splitlines() if line.startswith("compose up"))
    assert "rm -f cid-feishu" not in recovery_log


@pytest.mark.parametrize("entry", ["deploy", "host", "self"])
def test_transient_task_states_wait_until_this_runs_success(tmp_path, entry):
    options = dict(STUB_TRANSITION="true", LIBRENMS_CONFIG_TIMEOUT="20", LIBRENMS_CONFIG_INTERVAL="0")
    if entry == "deploy":
        result, log = deploy.run_deploy(tmp_path, **options)
    else:
        result, log = run_apply_tail(tmp_path, PLATFORM_API_SELF_APPLY=str(entry == "self").lower(), **options)
    assert result.returncode == 0, result.stderr
    assert log.count("State.Status}} {{.State.ExitCode") == 8


def test_hung_task_inspection_is_bounded_by_task_budget(tmp_path):
    started = time.monotonic()
    result, log = run_apply_tail(tmp_path, STUB_INSPECT_SLEEP="10", LIBRENMS_CONFIG_TIMEOUT="2")
    assert time.monotonic() - started < 7
    assert result.returncode != 0
    assert "CHECK configured" not in log
    assert "--no-deps topology-collector" not in log
