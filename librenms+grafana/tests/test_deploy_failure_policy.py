import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import pytest

from .test_librenms_auto_config_flow import _extract_shell_function


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = ROOT.parent
SH = shutil.which("sh") or r"C:\Program Files\Git\usr\bin\sh.exe"


DOCKER_STUB = r"""#!/bin/sh
printf '%s\n' "$*" >> "$STUB_LOG"
advance_clock() {
  [ -n "${STUB_CLOCK:-}" ] || return 0
  current=$(cat "$STUB_CLOCK")
  printf '%s\n' "$((current + $1))" > "$STUB_CLOCK"
}

if [ "$1" = "compose" ]; then
  shift
  while [ "$#" -gt 0 ]; do
    case "$1" in --profile|-f|--env-file|--project-directory) shift 2 ;; *) break ;; esac
  done
  command=${1:-}
  [ "$#" -eq 0 ] || shift
  case "$command" in
    version) exit 0 ;;
    config)
      case "${1:-}" in
        --images)
          echo remote.example/platform:1
          echo monitor-platform-api:local
          ;;
        --services)
          if [ "${STUB_SECOND_SERVICES_HANG:-false}" = true ]; then
            if [ -f "$STUB_LOG.services-first" ]; then
              printf '%s\n' "$DEPLOY_CHECK_DEADLINE" > "$STUB_LOG.services-deadline"
              echo 'SECOND_SERVICES_BLOCKED' >> "$STUB_LOG"
              # Real sleep, deliberately bypassing the fixture's no-op sleep.
              /usr/bin/sleep 8
              exit 42
            fi
            touch "$STUB_LOG.services-first"
          fi
          printf '%s\n' bigscreen platform-api topology-collector alertmanager-feishu-bridge librenms-config grafana-setup
          ;;
      esac
      exit 0
      ;;
    pull)
      [ "${1:-}" = "--help" ] && { echo "--ignore-buildable"; exit 0; }
      [ "${STUB_PULL_FAIL:-false}" = true ] && exit 1
      exit 0
      ;;
    rm) exit 0 ;;
    build)
      advance_clock "${STUB_BUILD_ADVANCE:-0}"
      [ "${STUB_BUILD_FAIL:-false}" != true ] || exit 1
      exit 0 ;;
    up)
      no_deps=false
      has_bigscreen=false
      for argument in "$@"; do
        if [ "$argument" = --build ]; then
          advance_clock "${STUB_BUILD_ADVANCE:-0}"
          [ "${STUB_BUILD_FAIL:-false}" != true ] || exit 1
        fi
        [ "$argument" != --no-deps ] || no_deps=true
        [ "$argument" != bigscreen ] || has_bigscreen=true
      done
      if [ "${STUB_DEPENDENCY_CHECK:-false}" = true ] && [ "$no_deps" = false ] && [ "$has_bigscreen" = true ]; then
        echo "IMPLICIT platform-api dependency" >> "$STUB_LOG"
      fi
      is_config=false
      for argument in "$@"; do
        [ "$argument" = librenms-config ] && is_config=true
      done
      if [ "$is_config" = true ]; then
        [ "${STUB_LIBRENMS_CONFIG_CREATE_FAIL:-false}" = true ] && exit 1
        [ "${STUB_STALE_TASKS:-false}" = true ] || touch "$STUB_LOG.created"
      elif [ "${STUB_COMPOSE_UP_FAIL:-false}" = true ]; then
        exit 1
      fi
      if [ "${STUB_SECOND_SERVICES_HANG:-false}" = true ]; then
        /usr/bin/sleep 2
        echo 'RESIDENT_START_SUCCEEDED' >> "$STUB_LOG"
      fi
      exit 0
      ;;
    restart)
      [ "${1:-}" = "${STUB_RESTART_FAIL_SERVICE:-}" ] && exit 1
      exit 0
      ;;
    ps)
      for argument in "$@"; do
        case "$argument" in
          librenms-config|grafana-setup)
            [ "${STUB_PS_FAIL:-false}" != true ] || exit 1
            [ "$argument" != "${STUB_MISSING_TASK:-}" ] || exit 0
            generation=old
            [ ! -f "$STUB_LOG.created" ] || generation=new
            echo "cid-$argument-$generation"; exit 0 ;;
          prometheus) echo cid-prometheus; exit 0 ;;
          feishu-ws)
            [ ! -f "$STUB_LOG.removed" ] || exit 0
            [ "${STUB_FEISHU_PRESENT:-false}" != true ] || echo cid-feishu
            exit 0 ;;
        esac
      done
      exit 0
      ;;
  esac
  exit 0
fi

if [ "$1" = image ] && [ "$2" = inspect ]; then
  image=$3
  case "$image" in
    monitor-*:local) [ "${STUB_LOCAL_IMAGES_READY:-true}" = true ] ;;
    remote.example/platform:1) [ "${STUB_REMOTE_IMAGE_PRESENT:-true}" = true ] ;;
    base.example/runtime:1) [ "${STUB_BASE_IMAGE_PRESENT:-true}" = true ] || [ -f "$STUB_LOG.base-pulled" ] ;;
    *) exit 1 ;;
  esac
  exit $?
fi

if [ "$1" = pull ]; then
  advance_clock "${STUB_BASE_PULL_ADVANCE:-0}"
  [ "${STUB_BASE_PULL_FAIL:-false}" = true ] && exit 1
  touch "$STUB_LOG.base-pulled"
  exit 0
fi

if [ "$1" = inspect ]; then
  format=$3
  [ "${STUB_INSPECT_FAIL:-false}" != true ] || exit 1
  case "$format" in
    *State.Status*State.ExitCode*)
      if [ "${STUB_TRANSITION:-false}" = true ]; then
        counter_file="$STUB_LOG.$4.polls"
        counter=0
        [ ! -f "$counter_file" ] || counter=$(cat "$counter_file")
        printf '%s\n' "$((counter + 1))" > "$counter_file"
        case "$counter" in 0) echo 'created 0' ;; 1) echo 'running 0' ;; 2) echo 'restarting 0' ;; *) echo 'exited 0' ;; esac
        exit 0
      fi
      [ -z "${STUB_INSPECT_SLEEP:-}" ] || /usr/bin/sleep "$STUB_INSPECT_SLEEP"
      case "$4" in
        *grafana-setup*) echo "${STUB_GRAFANA_STATE:-exited} ${STUB_GRAFANA_EXIT:-0}" ;;
        *) echo "${STUB_CONFIG_STATE:-exited} ${STUB_LIBRENMS_CONFIG_EXIT:-0}" ;;
      esac ;;
    *State.Status*)
      case "$4" in
        *grafana-setup*) echo "${STUB_GRAFANA_STATE:-exited}" ;;
        *) echo "${STUB_CONFIG_STATE:-exited}" ;;
      esac ;;
    *State.ExitCode*)
      case "$4" in
        *grafana-setup*) echo "${STUB_GRAFANA_EXIT:-0}" ;;
        *) echo "${STUB_LIBRENMS_CONFIG_EXIT:-0}" ;;
      esac ;;
    *com.docker.compose.project*)
      if [ "$4" = cid-feishu ]; then echo "${STUB_FEISHU_PROJECT:-fixture}"; else echo fixture; fi ;;
    *com.docker.compose.service*) echo "${STUB_FEISHU_SERVICE:-feishu-ws}" ;;
    *) exit 1 ;;
  esac
  exit 0
fi

if [ "$1" = rm ]; then
  [ "${STUB_REMOVE_FAIL:-false}" != true ] || exit 1
  touch "$STUB_LOG.removed"
fi

exit 0
"""


ENV_TEXT = """SERVER_IP=127.0.0.1
COMPOSE_PARALLEL_LIMIT=1
IMAGE_PULL_RETRIES=1
IMAGE_PULL_RETRY_DELAY=0
"""


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(0o755)


def run_deploy(tmp_path: Path, unchanged=True, event_config=None, env_text=ENV_TEXT,
               fake_clock=False, check_script=None, **overrides):
    project = tmp_path / "project"
    project.mkdir()
    shutil.copy2(ROOT / "deploy.sh", project / "deploy.sh")
    if (ROOT / "deployment-tasks.sh").exists():
        shutil.copy2(ROOT / "deployment-tasks.sh", project / "deployment-tasks.sh")
    shutil.copy2(ROOT / "platform_config.py", project / "platform_config.py")
    shutil.copy2(ROOT / "version_info.py", project / "version_info.py")
    shutil.copy2(REPOSITORY / "VERSION", project / "VERSION")
    if env_text is not None:
        (project / ".env").write_text(env_text, encoding="utf-8")
    if event_config is not None:
        (project / "event-config.yml").write_text(event_config, encoding="utf-8")
    (project / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    docker_dir = project / "docker" / "test"
    docker_dir.mkdir(parents=True)
    (docker_dir / "Dockerfile").write_text("FROM base.example/runtime:1\n", encoding="utf-8")
    _write_executable(project / "render-grafana-provisioning.sh", "#!/bin/sh\nexit 0\n")
    _write_executable(
        project / "deploy-check.sh",
        check_script or '#!/bin/sh\nprintf "deploy-check %s\\n" "$*" >> "$STUB_LOG"\nexit "${STUB_DEPLOY_CHECK_EXIT:-0}"\n',
    )
    if unchanged:
        (project / ".deploy-local-image.sha256").write_text("aggregatehash\n", encoding="utf-8")

    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    _write_executable(stub_bin / "docker", DOCKER_STUB)
    _write_executable(stub_bin / "sleep", '''#!/bin/sh
if [ -n "${STUB_CLOCK:-}" ]; then
  current=$(cat "$STUB_CLOCK")
  printf '%s\n' "$((current + ${1:-0}))" > "$STUB_CLOCK"
fi
exit 0
''')
    if fake_clock:
        (tmp_path / "clock").write_text("1000\n")
        _write_executable(stub_bin / "date", '''#!/bin/sh
if [ "${1:-}" = +%s ]; then cat "$STUB_CLOCK"; else exec /usr/bin/date "$@"; fi
''')
    _write_executable(
        stub_bin / "sha256sum",
        "#!/bin/sh\nif [ \"$#\" -gt 0 ]; then echo \"filehash  $1\"; else cat >/dev/null; echo \"aggregatehash  -\"; fi\n",
    )
    python_path = Path(sys.executable).as_posix()
    _write_executable(stub_bin / "python3", f'#!/bin/sh\nexec "{python_path}" "$@"\n')

    log_path = tmp_path / "docker.log"
    env = os.environ.copy()
    env.update({
        "PATH": os.pathsep.join((str(stub_bin), str(Path(SH).parent), env.get("PATH", ""))),
        "STUB_LOG": str(log_path),
        "LIBRENMS_CONFIG_TIMEOUT": "0",
        "LIBRENMS_CONFIG_INTERVAL": "0",
    })
    env.update({key: str(value) for key, value in overrides.items()})
    if fake_clock:
        env["STUB_CLOCK"] = str(tmp_path / "clock")
    completed = subprocess.run(
        [SH, str(project / "deploy.sh")],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    log = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    return completed, log


def test_pull_failure_continues_when_all_remote_images_exist_locally(tmp_path):
    completed, _ = run_deploy(tmp_path, STUB_PULL_FAIL="true", STUB_REMOTE_IMAGE_PRESENT="true")

    assert completed.returncode == 0
    assert "every required remote image exists locally" in completed.stderr
    assert "Result: PASS_WITH_WARNINGS (1 warning(s))" in completed.stdout
    assert "Platform bootstrap completed successfully" in completed.stdout


def test_fresh_event_config_without_core_switch_can_bootstrap(tmp_path):
    event_config = "devices:\n  core:\n    ip:\n  stage_switches: []\n"
    completed, _ = run_deploy(
        tmp_path,
        event_config=event_config,
    )

    assert completed.returncode == 0
    assert "event-config.yml has blocking validation errors" not in completed.stderr
    assert "[deploy] Event config schema: 0" in completed.stdout
    assert "Config will be migrated in memory to schema 1" in completed.stdout
    assert "event-config.yml will not be rewritten until Save/Apply" in completed.stdout
    assert (tmp_path / "project" / "event-config.yml").read_text(encoding="utf-8") == event_config


def test_deploy_reports_platform_git_and_supported_schema(tmp_path):
    completed, _ = run_deploy(tmp_path, PLATFORM_GIT_COMMIT="testcommit")
    version = (REPOSITORY / "VERSION").read_text(encoding="utf-8").strip()

    assert completed.returncode == 0
    assert f"[deploy] Platform version: {version}" in completed.stdout
    assert "[deploy] Git commit: testcommit" in completed.stdout
    assert "[deploy] Supported config schema: 1" in completed.stdout


def test_newer_event_schema_fails_before_config_or_env_can_change(tmp_path):
    event_config = "schema_version: 2\ncustom_future:\n  keep: safe\n"
    completed, log = run_deploy(
        tmp_path,
        event_config=event_config,
        env_text=None,
    )

    assert completed.returncode == 1
    assert "event-config schema 2 is newer than supported schema 1" in completed.stderr
    assert "Upgrade the monitoring platform" in completed.stderr
    assert (tmp_path / "project" / "event-config.yml").read_text(encoding="utf-8") == event_config
    assert not (tmp_path / "project" / ".env").exists()
    assert log == ""


def test_pull_failure_stops_and_lists_remote_images_missing_locally(tmp_path):
    completed, log = run_deploy(tmp_path, STUB_PULL_FAIL="true", STUB_REMOTE_IMAGE_PRESENT="false")

    assert completed.returncode == 1
    assert "remote.example/platform:1" in completed.stderr
    assert "deploy-check bootstrap" not in log


def test_unchanged_dockerfiles_and_complete_local_images_skip_base_pull(tmp_path):
    completed, log = run_deploy(
        tmp_path,
        unchanged=True,
        STUB_PULL_FAIL="true",
        STUB_REMOTE_IMAGE_PRESENT="true",
        STUB_BASE_IMAGE_PRESENT="false",
        STUB_BASE_PULL_FAIL="true",
    )

    assert completed.returncode == 0
    assert "pull base.example/runtime:1" not in log
    assert "--build" not in log


def test_required_rebuild_uses_cached_base_without_registry_pull(tmp_path):
    completed, log = run_deploy(
        tmp_path,
        unchanged=False,
        STUB_BASE_IMAGE_PRESENT="true",
        STUB_BASE_PULL_FAIL="true",
    )

    assert completed.returncode == 0
    assert "Base image base.example/runtime:1 already present" in completed.stdout
    assert "pull base.example/runtime:1" not in log
    assert log.splitlines().count("compose build") == 1
    assert "up -d --remove-orphans --no-build" in log


def test_required_rebuild_fails_when_base_image_is_unavailable(tmp_path):
    completed, log = run_deploy(
        tmp_path,
        unchanged=False,
        STUB_BASE_IMAGE_PRESENT="false",
        STUB_BASE_PULL_FAIL="true",
    )

    assert completed.returncode == 1
    assert "base.example/runtime:1" in completed.stderr
    assert "--build" not in log


def test_librenms_config_exit_zero_allows_deploy_to_finish(tmp_path):
    completed, log = run_deploy(tmp_path, STUB_LIBRENMS_CONFIG_EXIT="0")

    assert completed.returncode == 0
    assert "librenms-config completed successfully" in completed.stdout
    assert "Result: PASS." in completed.stdout
    assert "deploy-check bootstrap" in log
    assert completed.stdout.rfind("Platform bootstrap completed successfully.") > (
        completed.stdout.rfind("Result: PASS.")
    )
    assert completed.stdout.rstrip().endswith(
        "Open http://127.0.0.1:8088/control and configure the event."
    )


def test_token_consumers_restart_once_after_librenms_config_succeeds(tmp_path):
    completed, log = run_deploy(tmp_path, STUB_LIBRENMS_CONFIG_EXIT="0")

    assert completed.returncode == 0
    config_start = log.index(
        "compose up -d --force-recreate --no-deps librenms-config"
    )
    config_success = log.index("State.ExitCode")
    topology_restart = log.index("compose restart topology-collector")
    bridge_restart = log.index("compose restart alertmanager-feishu-bridge")
    bootstrap = log.index("deploy-check bootstrap")
    assert config_start < config_success < topology_restart < bootstrap
    assert config_start < config_success < bridge_restart < bootstrap
    assert log.count("compose restart topology-collector") == 1
    assert log.count("compose restart alertmanager-feishu-bridge") == 1


def test_librenms_config_nonzero_exit_fails_with_diagnostic_command(tmp_path):
    completed, log = run_deploy(tmp_path, STUB_LIBRENMS_CONFIG_EXIT="7")

    assert completed.returncode == 1
    assert "librenms-config failed (exit 7)" in completed.stderr
    assert "docker compose logs --tail=100 librenms-config" in completed.stderr
    assert "deploy-check bootstrap" not in log
    assert "restart topology-collector" not in log
    assert "restart alertmanager-feishu-bridge" not in log


def test_compose_up_failure_is_fatal(tmp_path):
    completed, log = run_deploy(tmp_path, STUB_COMPOSE_UP_FAIL="true")

    assert completed.returncode == 1
    assert "docker compose up failed" in completed.stderr
    assert "restart bigscreen" not in log


def test_required_restart_failure_is_fatal_but_disabled_profile_is_skipped(tmp_path):
    completed, log = run_deploy(tmp_path, STUB_RESTART_FAIL_SERVICE="platform-api")

    assert completed.returncode == 1
    assert "restart platform-api failed" in completed.stderr
    assert "restart feishu-ws" not in log
    assert "profile not enabled" in completed.stdout


def test_bootstrap_check_failure_makes_deploy_fail_before_success(tmp_path):
    completed, log = run_deploy(tmp_path, STUB_DEPLOY_CHECK_EXIT="1")

    assert completed.returncode == 1
    assert "deploy-check bootstrap" in log
    assert "Platform bootstrap completed successfully" not in completed.stdout


def test_apply_entrypoint_runs_the_configured_runtime_check():
    apply_env = (ROOT / "apply-env.sh").read_text(encoding="utf-8")

    assert '"$SCRIPT_DIR/deploy-check.sh" configured' in apply_env
    assert "配置已经写入，但运行状态验证失败" in apply_env


@pytest.mark.parametrize("slow_phase", ["build", "base_pull"])
def test_r2_image_preparation_does_not_spend_runtime_budget(tmp_path, slow_phase):
    result, log = run_deploy(
        tmp_path, unchanged=False, fake_clock=True,
        LIBRENMS_CONFIG_TIMEOUT="1", DEPLOY_CHECK_TIMEOUT="1",
        STUB_BUILD_ADVANCE="3" if slow_phase == "build" else "0",
        STUB_BASE_PULL_ADVANCE="3" if slow_phase == "base_pull" else "0",
        STUB_BASE_IMAGE_PRESENT="false" if slow_phase == "base_pull" else "true",
    )
    assert result.returncode == 0, result.stderr
    assert "Platform bootstrap completed successfully" in result.stdout
    assert (tmp_path / "clock").read_text().strip() == "1003"
    builds = [line for line in log.splitlines() if line.startswith("compose build")]
    assert builds == ["compose build"]
    task_starts = [line for line in log.splitlines() if line.startswith("compose up") and "librenms-config" in line]
    assert len(task_starts) == 1
    assert task_starts[0].endswith("librenms-config grafana-setup")
    assert log.index("compose build") < log.index("compose up")
    assert "--build" not in log


def test_r2_failed_build_never_starts_runtime_tasks(tmp_path):
    result, log = run_deploy(tmp_path, unchanged=False, fake_clock=True, STUB_BUILD_FAIL="true")
    assert result.returncode != 0
    assert "compose up" not in log
    assert "deploy-check bootstrap" not in log
    assert "completed successfully" not in result.stdout


def test_r2_cache_hit_skips_build_and_base_preparation(tmp_path):
    result, log = run_deploy(tmp_path, fake_clock=True, STUB_BUILD_FAIL="true",
                             STUB_BASE_PULL_FAIL="true", LIBRENMS_CONFIG_TIMEOUT="1",
                             DEPLOY_CHECK_TIMEOUT="1")
    assert result.returncode == 0, result.stderr
    assert "compose build" not in log
    assert "--build" not in log
    assert "pull base.example" not in log


def test_r2_stuck_task_after_slow_build_remains_bounded(tmp_path):
    result, log = run_deploy(tmp_path, unchanged=False, fake_clock=True,
                             STUB_BUILD_ADVANCE="3", STUB_CONFIG_STATE="running",
                             LIBRENMS_CONFIG_TIMEOUT="1", LIBRENMS_CONFIG_INTERVAL="1",
                             DEPLOY_CHECK_TIMEOUT="1")
    assert result.returncode != 0
    assert "--no-deps librenms-config grafana-setup" in log
    assert "deploy-check bootstrap" not in log
    assert "restart topology-collector" not in log
    assert (tmp_path / "clock").read_text().strip() == "1004"


def test_r2_final_health_timeout_after_slow_build_still_fails(tmp_path):
    source = (ROOT / "deploy-check.sh").read_text(encoding="utf-8")
    # Execute the actual health-wait loop with a clock-driven unavailable HTTP
    # double; no real health endpoint or Docker daemon is involved.
    script = '''#!/bin/sh
DEPLOY_CHECK_INTERVAL=1
DEADLINE=$(( $(date +%s) + DEPLOY_CHECK_TIMEOUT ))
[ "$DEADLINE" -le "$DEPLOY_CHECK_DEADLINE" ] || DEADLINE=$DEPLOY_CHECK_DEADLINE
HTTP_ERROR="$STUB_LOG.http-error"
HTTP_BODY="$STUB_LOG.http-body"
record() { printf '%s %s %s\n' "$1" "$2" "$3" >> "$STUB_LOG"; }
http_get() { echo unavailable >&2; return 1; }
'''
    for name in ("now_seconds", "deadline_reached", "wait_interval", "wait_for_http"):
        script += _extract_shell_function(source, name) + "\n"
    script += 'wait_for_http final_health "Final health" "http://fixture.invalid"\n'
    result, log = run_deploy(tmp_path, unchanged=False, fake_clock=True, check_script=script,
                             STUB_BUILD_ADVANCE="3", LIBRENMS_CONFIG_TIMEOUT="1",
                             DEPLOY_CHECK_TIMEOUT="1")
    assert result.returncode != 0
    assert "grafana-setup completed successfully" in result.stdout
    assert "FAIL final_health Final health timed out after 1s" in log
    assert "Platform bootstrap completed successfully" not in result.stdout
    assert (tmp_path / "clock").read_text().strip() == "1004"


def test_second_service_listing_hang_uses_remaining_shared_budget(tmp_path):
    result, log = run_deploy(
        tmp_path, STUB_SECOND_SERVICES_HANG="true",
        LIBRENMS_CONFIG_TIMEOUT="4", DEPLOY_CHECK_TIMEOUT="2",
    )
    finished = time.time()
    deadline = int((tmp_path / "docker.log.services-deadline").read_text())
    assert (tmp_path / "docker.log.services-first").exists()
    assert log.count("compose config --services") == 2
    assert log.index("RESIDENT_START_SUCCEEDED") < log.index("SECOND_SERVICES_BLOCKED")
    assert result.returncode != 0
    assert "could not determine the enabled Compose services" in result.stderr
    assert "compose restart" not in log
    assert "--no-deps librenms-config grafana-setup" not in log
    assert "deploy-check bootstrap" not in log
    assert "Platform bootstrap completed successfully" not in result.stdout
    # Compare with the actual inherited absolute deadline, not a fresh timeout
    # starting at the second read. Allow process teardown/scheduler tolerance.
    assert finished <= deadline + 1.5, (finished, deadline, result.stderr)
