import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = ROOT.parent
SH = shutil.which("sh") or r"C:\Program Files\Git\usr\bin\sh.exe"


DOCKER_STUB = r"""#!/bin/sh
printf '%s\n' "$*" >> "$STUB_LOG"

if [ "$1" = "compose" ]; then
  shift
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
          printf '%s\n' bigscreen platform-api alertmanager-feishu-bridge librenms-config
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
    up)
      is_config=false
      for argument in "$@"; do
        [ "$argument" = librenms-config ] && is_config=true
      done
      if [ "$is_config" = true ]; then
        [ "${STUB_LIBRENMS_CONFIG_CREATE_FAIL:-false}" = true ] && exit 1
      elif [ "${STUB_COMPOSE_UP_FAIL:-false}" = true ]; then
        exit 1
      fi
      exit 0
      ;;
    restart)
      [ "${1:-}" = "${STUB_RESTART_FAIL_SERVICE:-}" ] && exit 1
      exit 0
      ;;
    ps)
      for argument in "$@"; do
        [ "$argument" = librenms-config ] && { echo cid-librenms-config; exit 0; }
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
    base.example/runtime:1) [ "${STUB_BASE_IMAGE_PRESENT:-true}" = true ] ;;
    *) exit 1 ;;
  esac
  exit $?
fi

if [ "$1" = pull ]; then
  [ "${STUB_BASE_PULL_FAIL:-false}" = true ] && exit 1
  exit 0
fi

if [ "$1" = inspect ]; then
  format=$3
  case "$format" in
    *State.Status*) echo exited ;;
    *State.ExitCode*) echo "${STUB_LIBRENMS_CONFIG_EXIT:-0}" ;;
    *) exit 1 ;;
  esac
  exit 0
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


def run_deploy(tmp_path: Path, unchanged=True, event_config=None, env_text=ENV_TEXT, **overrides):
    project = tmp_path / "project"
    project.mkdir()
    shutil.copy2(ROOT / "deploy.sh", project / "deploy.sh")
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
        '#!/bin/sh\nprintf "deploy-check %s\\n" "$*" >> "$STUB_LOG"\nexit "${STUB_DEPLOY_CHECK_EXIT:-0}"\n',
    )
    if unchanged:
        (project / ".deploy-local-image.sha256").write_text("aggregatehash\n", encoding="utf-8")

    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    _write_executable(stub_bin / "docker", DOCKER_STUB)
    _write_executable(stub_bin / "sleep", "#!/bin/sh\nexit 0\n")
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


def test_librenms_config_nonzero_exit_fails_with_diagnostic_command(tmp_path):
    completed, log = run_deploy(tmp_path, STUB_LIBRENMS_CONFIG_EXIT="7")

    assert completed.returncode == 1
    assert "librenms-config failed (exit 7)" in completed.stderr
    assert "docker compose logs --tail=100 librenms-config" in completed.stderr
    assert "deploy-check bootstrap" not in log


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


def test_deploy_and_apply_call_the_expected_runtime_checks():
    deploy = (ROOT / "deploy.sh").read_text(encoding="utf-8")
    apply_env = (ROOT / "apply-env.sh").read_text(encoding="utf-8")
    platform_api = (ROOT / "platform-api.py").read_text(encoding="utf-8")

    assert '"$SCRIPT_DIR/deploy-check.sh" bootstrap' in deploy
    assert '"$SCRIPT_DIR/deploy-check.sh" configured' in apply_env
    assert "配置已经写入，但运行状态验证失败" in apply_env
    assert "restore_config_snapshot(Path(snapshot[\"path\"]))" in platform_api
    assert "rollback_result = run_apply_command()" in platform_api
