import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
SH = shutil.which("sh") or r"C:\Program Files\Git\usr\bin\sh.exe"


DOCKER_STUB = r"""#!/bin/sh
[ -z "${STUB_COMPOSE_LOG:-}" ] || printf '%s\n' "$*" >> "$STUB_COMPOSE_LOG"
if [ "$1" = "compose" ]; then
  shift
  while [ "$#" -gt 0 ]; do
    case "$1" in
      -f|--env-file|--project-directory) shift 2 ;;
      *) break ;;
    esac
  done
  command=${1:-}
  [ "$#" -eq 0 ] || shift
  case "$command" in
    version) exit 0 ;;
    config) exit 0 ;;
    ps)
      service=""
      for argument in "$@"; do service=$argument; done
      [ "$service" = "${STUB_MISSING_SERVICE:-}" ] && exit 0
      printf 'cid-%s\n' "$service"
      exit 0
      ;;
    exec)
      [ "${1:-}" = "-T" ] && shift
      service=${1:-}
      [ "$#" -eq 0 ] || shift
      if [ "$service" = platform-api ]; then
        if [ "${STUB_PLATFORM_API_INTERNAL_HEALTH:-ok}" = fail ]; then
          echo "Platform API container health request failed" >&2
          exit 1
        fi
        printf '{"ok":true}\n'
        exit 0
      fi
      exit 1
      ;;
  esac
  exit 0
fi

if [ "$1" = "inspect" ]; then
  format=$3
  container=$4
  service=${container#cid-}
  case "$format" in
    *State.Status*)
      if [ "$service" = "${STUB_EXITED_SERVICE:-}" ]; then
        echo exited
      elif [ "$service" = "${STUB_RESTARTING_SERVICE:-}" ]; then
        echo restarting
      else
        echo running
      fi
      ;;
    *State.Health*)
      if [ "$service" = "${STUB_HEALTH_SERVICE:-}" ]; then
        case "${STUB_HEALTH_MODE:-}" in
          starting_once)
            counter="$STUB_STATE_DIR/health-counter"
            if [ ! -f "$counter" ]; then
              echo 1 > "$counter"
              echo starting
            else
              echo healthy
            fi
            ;;
          starting) echo starting ;;
          unhealthy) echo unhealthy ;;
          *) echo none ;;
        esac
      else
        echo none
      fi
      ;;
    *State.ExitCode*) echo "${STUB_EXIT_CODE:-1}" ;;
    *) exit 1 ;;
  esac
  exit 0
fi

exit 0
"""


CURL_STUB = r"""#!/bin/sh
url=""
for argument in "$@"; do url=$argument; done
[ -z "${STUB_HTTP_LOG:-}" ] || printf '%s\n' "$url" >> "$STUB_HTTP_LOG"
[ -z "${STUB_HTTP_CLIENT_LOG:-}" ] || printf 'curl %s\n' "$url" >> "$STUB_HTTP_CLIENT_LOG"
if [ "${STUB_CURL_BROKEN:-false}" = true ]; then
  echo "curl: error while loading shared libraries: libcurl.so.4" >&2
  exit 127
fi
case "$url" in
  http://127.0.0.1:9200/health)
    if [ "${STUB_HOST_PLATFORM_API_FAIL:-false}" = true ]; then
      echo "host port 9200 is not published" >&2
      exit 22
    fi
    printf '{"ok":true}\n'
    ;;
  */metrics)
    echo "prometheus_config_last_reload_successful ${STUB_RELOAD_SUCCESS:-1}"
    ;;
  */api/v1/targets*)
    if [ -n "${STUB_TARGETS_JSON:-}" ]; then
      printf '%s\n' "$STUB_TARGETS_JSON"
    else
      printf '{"status":"success","data":{"activeTargets":[]}}\n'
    fi
    ;;
  */player-targets/status)
    if [ -n "${STUB_PLAYER_STATUS:-}" ]; then
      printf '%s\n' "$STUB_PLAYER_STATUS"
    else
      printf '{"ok":true,"targets":{"total":0}}\n'
    fi
    ;;
  */topology/isp_targets.json)
    printf '%s\n' "${STUB_ISP_INVENTORY_JSON:-[]}"
    ;;
  */topology/isp-discovery-state.json)
    if [ -n "${STUB_ISP_STATE_JSON:-}" ]; then
      printf '%s\n' "$STUB_ISP_STATE_JSON"
    else
      printf '{"status":"ok","last_success_at":%s,"last_error_at":null,"count":0}\n' "$(date +%s)"
    fi
    ;;
  *) printf '{}\n' ;;
esac
"""


BASE_ENV = """SERVER_IP=127.0.0.1
COMPOSE_PROFILES=
CORE_SWITCH_PING=
TOURNAMENT_SWITCHES=
FIREWALL_PING=
FIREWALL_SNMP_TARGETS=
BIGSCREEN_ISP_AUTO_DISCOVER=false
ISP_GATEWAY_AUTO_DISCOVER=false
ISP_PING=
BIGSCREEN_ISP_NAMES=
BIGSCREEN_ISP_IPS=
BIGSCREEN_ISP_MAX_BANDWIDTH=1000
ISP_DISCOVERY_REFRESH_INTERVAL=60
PLAYER_SUBNETS=
"""

AUTO_FIREWALL_TARGETS = json.dumps({
    "status": "success",
    "data": {"activeTargets": [{
        "labels": {"job": "infra-fw-ping"},
        "discoveredLabels": {"__address__": "192.168.9.1"},
    }]},
})


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(0o755)


def run_check(tmp_path: Path, mode="bootstrap", env_text=BASE_ENV, output="json", **overrides):
    project = tmp_path / "project"
    project.mkdir()
    shutil.copy2(ROOT / "deploy-check.sh", project / "deploy-check.sh")
    shutil.copy2(ROOT / "platform_config.py", project / "platform_config.py")
    shutil.copy2(ROOT / "version_info.py", project / "version_info.py")
    (project / "docker-compose.yml").write_text(
        "services:\n  platform-api:\n    image: fixture-platform-api\n",
        encoding="utf-8",
    )
    (project / ".env").write_text(env_text, encoding="utf-8")

    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    _write_executable(stub_bin / "docker", DOCKER_STUB)
    _write_executable(stub_bin / "curl", CURL_STUB)
    _write_executable(
        stub_bin / "sleep",
        """#!/bin/sh
if [ -n "${STUB_FAKE_TIME_FILE:-}" ]; then
  current=$(cat "$STUB_FAKE_TIME_FILE")
  advance=${STUB_SLEEP_ADVANCE:-${1:-0}}
  printf '%s\n' "$((current + advance))" > "$STUB_FAKE_TIME_FILE"
fi
exit 0
""",
    )
    python_path = Path(sys.executable).as_posix()
    _write_executable(
        stub_bin / "python3",
        f'''#!/bin/sh
if [ "${{1:-}}" = "-c" ]; then
  case "${{2:-}}" in
    *urllib.request.urlopen*)
      url=${{3:-}}
      [ -z "${{STUB_HTTP_LOG:-}}" ] || printf '%s\n' "$url" >> "$STUB_HTTP_LOG"
      [ -z "${{STUB_HTTP_CLIENT_LOG:-}}" ] || printf 'python %s\n' "$url" >> "$STUB_HTTP_CLIENT_LOG"
      case "$url" in
        */metrics) echo "prometheus_config_last_reload_successful ${{STUB_RELOAD_SUCCESS:-1}}" ;;
        */api/v1/targets*) printf '%s\n' "${{STUB_TARGETS_JSON:-{{\"status\":\"success\",\"data\":{{\"activeTargets\":[]}}}}}}" ;;
        */player-targets/status) printf '%s\n' "${{STUB_PLAYER_STATUS:-{{\"ok\":true,\"targets\":{{\"total\":0}}}}}}" ;;
        */topology/isp_targets.json) printf '%s\n' "${{STUB_ISP_INVENTORY_JSON:-[]}}" ;;
        */topology/isp-discovery-state.json)
          if [ -n "${{STUB_ISP_STATE_JSON:-}}" ]; then
            printf '%s\n' "$STUB_ISP_STATE_JSON"
          else
            printf '{{\"status\":\"ok\",\"last_success_at\":%s,\"last_error_at\":null,\"count\":0}}\n' "$(date +%s)"
          fi
          ;;
        *) printf '{{"ok":true}}\n' ;;
      esac
      exit 0
      ;;
  esac
fi
exec "{python_path}" "$@"
''',
    )

    if str(overrides.get("STUB_FAKE_TIME", "false")).lower() == "true":
        fake_time = tmp_path / "fake-time"
        fake_time.write_text("0\n", encoding="utf-8")
        _write_executable(
            stub_bin / "date",
            """#!/bin/sh
if [ "${1:-}" = "+%s" ]; then
  cat "$STUB_FAKE_TIME_FILE"
  exit 0
fi
exec /usr/bin/date "$@"
""",
        )

    env = os.environ.copy()
    env.update({
        "PATH": os.pathsep.join((str(stub_bin), str(Path(SH).parent), env.get("PATH", ""))),
        "DEPLOY_CHECK_TIMEOUT": "0",
        "DEPLOY_CHECK_INTERVAL": "0",
        "STUB_STATE_DIR": str(tmp_path),
        "STUB_HTTP_LOG": str(tmp_path / "http.log"),
        "STUB_HTTP_CLIENT_LOG": str(tmp_path / "http-client.log"),
        "STUB_COMPOSE_LOG": str(tmp_path / "compose.log"),
    })
    if str(overrides.get("STUB_FAKE_TIME", "false")).lower() == "true":
        env["STUB_FAKE_TIME_FILE"] = str(tmp_path / "fake-time")
    env.update({key: str(value) for key, value in overrides.items()})
    arguments = [SH, str(project / "deploy-check.sh")]
    if mode is not None:
        arguments.append(mode)
    if output == "json":
        arguments.append("--json")
    elif output == "quiet":
        arguments.append("--quiet")
    completed = subprocess.run(
        arguments,
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert completed.stdout, completed.stderr
    payload = json.loads(completed.stdout) if output == "json" else completed.stdout
    return completed, payload


def checks_by_id(payload):
    return {item["id"]: item for item in payload["checks"]}


def test_bootstrap_does_not_require_player_targets_or_core_switch(tmp_path):
    completed, payload = run_check(tmp_path)

    assert completed.returncode == 0
    assert payload["result"] == "PASS"
    ids = checks_by_id(payload)
    assert "configured_core" not in ids
    assert "player_generator" not in ids
    assert ids["topology_collector"]["status"] == "PASS"
    assert ids["player_targets"]["status"] == "PASS"


@pytest.mark.parametrize("mode", ["bootstrap", "configured"])
@pytest.mark.parametrize(
    ("service", "check_id"),
    [
        ("topology-collector", "topology_collector"),
        ("player-targets", "player_targets"),
    ],
)
def test_runtime_generator_restarting_fails_deploy_check(
    tmp_path, mode, service, check_id
):
    completed, payload = run_check(
        tmp_path,
        mode=mode,
        STUB_RESTARTING_SERVICE=service,
    )
    check = checks_by_id(payload)[check_id]

    assert completed.returncode == 1
    assert check["status"] == "FAIL"
    assert "restarting" in check["message"]


def test_disabled_optional_profiles_are_skipped(tmp_path):
    _, payload = run_check(tmp_path)
    checks = checks_by_id(payload)

    assert checks["unifi_profile"]["status"] == "SKIP"
    assert checks["feishu_profile"]["status"] == "SKIP"


def test_enabled_optional_profiles_are_checked(tmp_path):
    env_text = BASE_ENV.replace("COMPOSE_PROFILES=", "COMPOSE_PROFILES=unifi,feishu")
    completed, payload = run_check(tmp_path, env_text=env_text)
    checks = checks_by_id(payload)

    assert completed.returncode == 0
    assert checks["unifi_profile"]["status"] == "PASS"
    assert checks["feishu_profile"]["status"] == "PASS"


@pytest.mark.parametrize("failure_kind", ["missing", "exited"])
def test_required_container_missing_or_exited_fails(tmp_path, failure_kind):
    override = (
        {"STUB_MISSING_SERVICE": "prometheus"}
        if failure_kind == "missing"
        else {"STUB_EXITED_SERVICE": "prometheus", "STUB_EXIT_CODE": "2"}
    )
    completed, payload = run_check(tmp_path, **override)

    assert completed.returncode == 1
    assert payload["result"] == "FAIL"
    assert checks_by_id(payload)["prometheus"]["status"] == "FAIL"


def test_health_starting_then_healthy_passes(tmp_path):
    completed, payload = run_check(
        tmp_path,
        STUB_HEALTH_SERVICE="prometheus",
        STUB_HEALTH_MODE="starting_once",
        DEPLOY_CHECK_TIMEOUT="10",
    )

    assert completed.returncode == 0
    assert checks_by_id(payload)["prometheus"]["status"] == "PASS"


def test_health_starting_until_timeout_fails_with_last_state(tmp_path):
    completed, payload = run_check(
        tmp_path,
        STUB_HEALTH_SERVICE="prometheus",
        STUB_HEALTH_MODE="starting",
    )

    check = checks_by_id(payload)["prometheus"]
    assert completed.returncode == 1
    assert check["status"] == "FAIL"
    assert "running/starting" in check["message"]


def test_configured_checks_only_components_that_are_actually_configured(tmp_path):
    env_text = BASE_ENV.replace("CORE_SWITCH_PING=", "CORE_SWITCH_PING=core:192.0.2.10")
    completed, payload = run_check(
        tmp_path,
        mode="configured",
        env_text=env_text,
        STUB_TARGETS_JSON='{"job":"infra-core-ping","target":"192.0.2.10"}',
    )
    checks = checks_by_id(payload)

    assert completed.returncode == 0
    assert checks["configured_core"]["status"] == "PASS"
    assert checks["configured_stage"]["status"] == "SKIP"
    assert checks["configured_firewall"]["status"] == "SKIP"
    assert checks["configured_isp"]["status"] == "SKIP"
    assert checks["player_generator"]["status"] == "SKIP"


def _auto_isp_env(manual_names="", auto_value="true"):
    return BASE_ENV.replace(
        "FIREWALL_SNMP_TARGETS=",
        "FIREWALL_SNMP_TARGETS=firewall:192.168.9.1",
    ).replace(
        "BIGSCREEN_ISP_AUTO_DISCOVER=false",
        f"BIGSCREEN_ISP_AUTO_DISCOVER={auto_value}",
    ).replace(
        "ISP_GATEWAY_AUTO_DISCOVER=false",
        f"ISP_GATEWAY_AUTO_DISCOVER={auto_value}",
    ).replace(
        "BIGSCREEN_ISP_NAMES=",
        f"BIGSCREEN_ISP_NAMES={manual_names}",
    )


def _isp_state(inventory, *, status="ok", age=0, count=None, inventory_hash=None):
    inventory_text = inventory if isinstance(inventory, str) else json.dumps(
        inventory, ensure_ascii=False
    )
    if count is None:
        try:
            count = len(json.loads(inventory_text))
        except (TypeError, ValueError):
            count = 0
    return json.dumps({
        "status": status,
        "last_success_at": int(time.time()) - age,
        "last_error_at": None,
        "count": count,
        "inventory_count": count,
        "inventory_sha256": inventory_hash or hashlib.sha256(
            (inventory_text + "\n").encode("utf-8")
        ).hexdigest(),
        "generation_id": "test-generation",
    })


def _production_isp_inventory():
    return json.loads(
        (ROOT / "tests" / "fixtures" / "isp" / "production-ha-inventory.json")
        .read_text(encoding="utf-8")
    )


def test_configured_auto_isp_accepts_five_inventory_with_four_manual_metadata(tmp_path):
    inventory = _production_isp_inventory()
    inventory_text = json.dumps(inventory, ensure_ascii=False)
    manual = "telcom-100M-长期,telcom-1000M,unicom-1000M,MLBB-telcom-300M"
    completed, payload = run_check(
        tmp_path,
        mode="configured",
        env_text=_auto_isp_env(manual),
        STUB_TARGETS_JSON=AUTO_FIREWALL_TARGETS,
        STUB_ISP_INVENTORY_JSON=inventory_text,
        STUB_ISP_STATE_JSON=_isp_state(inventory_text),
    )

    check = checks_by_id(payload)["configured_isp"]
    assert completed.returncode == 0
    assert check["status"] == "PASS"
    assert "validated 5 fresh ISP(s), 5 availability target(s)" in check["message"]


def test_configured_auto_isp_accepts_interface_without_gateway(tmp_path):
    inventory = [{
        "targets": [],
        "labels": {
            "display_name": "PPPoE WAN",
            "metric_name": "ethernet0/8",
            "metric_target": "192.0.2.1",
            "metric_ifindex": "8",
            "wan_ip": "203.0.113.2",
            "discovery_source": "interface_only",
        },
    }]
    inventory_text = json.dumps(inventory)
    completed, payload = run_check(
        tmp_path,
        mode="configured",
        env_text=_auto_isp_env(),
        STUB_TARGETS_JSON=AUTO_FIREWALL_TARGETS,
        STUB_ISP_INVENTORY_JSON=inventory_text,
        STUB_ISP_STATE_JSON=_isp_state(inventory_text),
    )

    check = checks_by_id(payload)["configured_isp"]
    assert completed.returncode == 0
    assert check["status"] == "PASS"
    assert "validated 1 fresh ISP(s), 0 availability target(s)" in check["message"]


@pytest.mark.parametrize(
    ("state_update", "message"),
    [
        ({"count": 4, "inventory_count": 4}, "count does not match inventory"),
        ({"inventory_sha256": "0" * 64}, "hash does not match inventory"),
    ],
)
def test_configured_auto_isp_rejects_state_inventory_mismatch(
    tmp_path, state_update, message
):
    inventory = _production_isp_inventory()
    inventory_text = json.dumps(inventory, ensure_ascii=False)
    state = json.loads(_isp_state(inventory_text))
    state.update(state_update)
    completed, payload = run_check(
        tmp_path,
        mode="configured",
        env_text=_auto_isp_env(),
        STUB_TARGETS_JSON=AUTO_FIREWALL_TARGETS,
        STUB_ISP_INVENTORY_JSON=inventory_text,
        STUB_ISP_STATE_JSON=json.dumps(state),
    )

    check = checks_by_id(payload)["configured_isp"]
    assert completed.returncode == 1
    assert check["status"] == "FAIL"
    assert message in check["message"]


def test_configured_check_rejects_conflicting_auto_discovery_flags(tmp_path):
    inventory = _production_isp_inventory()
    inventory_text = json.dumps(inventory, ensure_ascii=False)
    env_text = _auto_isp_env().replace(
        "BIGSCREEN_ISP_AUTO_DISCOVER=true", "BIGSCREEN_ISP_AUTO_DISCOVER=false"
    )
    completed, payload = run_check(
        tmp_path,
        mode="configured",
        env_text=env_text,
        STUB_TARGETS_JSON=AUTO_FIREWALL_TARGETS,
        STUB_ISP_INVENTORY_JSON=inventory_text,
        STUB_ISP_STATE_JSON=_isp_state(inventory_text),
    )

    check = checks_by_id(payload)["configured_isp_auto_flags"]
    assert completed.returncode == 1
    assert check["status"] == "FAIL"
    assert "flags disagree" in check["message"]


@pytest.mark.parametrize("auto_value", ("true", "1", "yes", "on", "TRUE", "YES", "ON"))
def test_configured_isp_truthy_aliases_run_auto_inventory_validation(tmp_path, auto_value):
    inventory = _production_isp_inventory()
    inventory_text = json.dumps(inventory, ensure_ascii=False)
    completed, payload = run_check(
        tmp_path,
        mode="configured",
        env_text=_auto_isp_env(auto_value=auto_value),
        STUB_TARGETS_JSON=AUTO_FIREWALL_TARGETS,
        STUB_ISP_INVENTORY_JSON=inventory_text,
        STUB_ISP_STATE_JSON=_isp_state(inventory_text),
    )

    check = checks_by_id(payload)["configured_isp"]
    assert completed.returncode == 0
    assert check["status"] == "PASS"
    assert "validated 5 fresh ISP(s), 5 availability target(s)" in check["message"]


@pytest.mark.parametrize("auto_value", ("false", "0", "no", "off", ""))
def test_configured_isp_false_aliases_use_inert_contract_without_manual_targets(
    tmp_path, auto_value
):
    completed, payload = run_check(
        tmp_path,
        mode="configured",
        env_text=_auto_isp_env(auto_value=auto_value),
        STUB_TARGETS_JSON=AUTO_FIREWALL_TARGETS,
    )

    check = checks_by_id(payload)["configured_isp"]
    assert completed.returncode == 0
    assert check["status"] == "SKIP"
    assert "ISP not configured" in check["message"]
    http_log = (tmp_path / "http.log").read_text(encoding="utf-8")
    assert "/topology/isp_targets.json" not in http_log


_DUPLICATE_NAME_INVENTORY = json.dumps([
    {"targets": ["8.8.8.1"], "labels": {
        "display_name": "same", "metric_name": "wan-a",
        "metric_target": "192.0.2.1", "metric_ifindex": "1",
    }},
    {"targets": ["1.1.1.1"], "labels": {
        "display_name": "same", "metric_name": "wan-b",
        "metric_target": "192.0.2.1", "metric_ifindex": "2",
    }},
])
_INVALID_GATEWAY_INVENTORY = json.dumps([{
    "targets": ["not-an-ip"], "labels": {
        "display_name": "ISP-A", "metric_name": "wan-a",
        "metric_target": "192.0.2.1", "metric_ifindex": "1",
    },
}])
_STALE_INVENTORY = json.dumps([{
    "targets": ["8.8.8.1"], "labels": {
        "display_name": "ISP-A", "metric_name": "wan-a",
        "metric_target": "192.0.2.1", "metric_ifindex": "1",
    },
}])


@pytest.mark.parametrize(
    ("inventory", "state", "message"),
    [
        ("{broken", _isp_state("{broken", count=5), "Expecting property name"),
        (_DUPLICATE_NAME_INVENTORY, _isp_state(_DUPLICATE_NAME_INVENTORY), "duplicate display_name"),
        (_INVALID_GATEWAY_INVENTORY, _isp_state(_INVALID_GATEWAY_INVENTORY), "invalid availability target"),
        (_STALE_INVENTORY, _isp_state(_STALE_INVENTORY, age=10_000), "inventory is stale"),
    ],
)
def test_configured_auto_isp_rejects_invalid_duplicate_gateway_or_stale_inventory(
    tmp_path, inventory, state, message
):
    completed, payload = run_check(
        tmp_path,
        mode="configured",
        env_text=_auto_isp_env(),
        STUB_TARGETS_JSON=AUTO_FIREWALL_TARGETS,
        STUB_ISP_INVENTORY_JSON=inventory,
        STUB_ISP_STATE_JSON=state,
    )

    check = checks_by_id(payload)["configured_isp"]
    assert completed.returncode == 1
    assert check["status"] == "FAIL"
    assert message in check["message"]


def test_configured_auto_isp_rejects_unmatched_identity_or_bandwidth_override(tmp_path):
    inventory = _production_isp_inventory()
    inventory_text = json.dumps(inventory, ensure_ascii=False)
    env_text = _auto_isp_env("missing-carrier").replace(
        "BIGSCREEN_ISP_MAX_BANDWIDTH=1000",
        "BIGSCREEN_ISP_MAX_BANDWIDTH=*:1000,missing-carrier:200",
    )
    completed, payload = run_check(
        tmp_path,
        mode="configured",
        env_text=env_text,
        STUB_TARGETS_JSON=AUTO_FIREWALL_TARGETS,
        STUB_ISP_INVENTORY_JSON=inventory_text,
        STUB_ISP_STATE_JSON=_isp_state(inventory_text),
    )

    check = checks_by_id(payload)["configured_isp"]
    assert completed.returncode == 1
    assert check["status"] == "FAIL"
    assert "manual ISP metadata is not safely matched: missing-carrier" in check["message"]


def test_configured_manual_isp_preserves_prometheus_target_check(tmp_path):
    env_text = BASE_ENV.replace(
        "ISP_PING=", "ISP_PING=manual-a:8.8.8.8,manual-b:1.1.1.1"
    ).replace(
        "BIGSCREEN_ISP_NAMES=", "BIGSCREEN_ISP_NAMES=manual-a,manual-b"
    )
    targets = json.dumps({
        "status": "success",
        "data": {"activeTargets": [{
            "labels": {"job": "infra-isp-ping"},
            "discoveredLabels": {"__address__": "8.8.8.8"},
        }, {
            "labels": {"job": "infra-isp-ping"},
            "discoveredLabels": {"__address__": "1.1.1.1"},
        }]},
    })

    completed, payload = run_check(
        tmp_path, mode="configured", env_text=env_text, STUB_TARGETS_JSON=targets
    )

    assert completed.returncode == 0
    assert checks_by_id(payload)["configured_isp"]["status"] == "PASS"


@pytest.mark.parametrize("auto_value", ("false", "0", "no", "off", ""))
def test_configured_manual_mode_uses_public_ip_metadata_fallback(tmp_path, auto_value):
    env_text = _auto_isp_env(auto_value=auto_value).replace(
        "BIGSCREEN_ISP_IPS=", "BIGSCREEN_ISP_IPS=manual-a:8.8.8.8"
    ).replace(
        "FIREWALL_SNMP_TARGETS=firewall:192.168.9.1", "FIREWALL_SNMP_TARGETS="
    )
    targets = json.dumps({
        "status": "success",
        "data": {"activeTargets": [{
            "labels": {"job": "infra-isp-ping"},
            "discoveredLabels": {"__address__": "8.8.8.8"},
        }]},
    })

    completed, payload = run_check(
        tmp_path, mode="configured", env_text=env_text, STUB_TARGETS_JSON=targets
    )

    assert completed.returncode == 0
    assert checks_by_id(payload)["configured_isp"]["status"] == "PASS"


def test_configured_player_generator_does_not_require_players_online(tmp_path):
    env_text = BASE_ENV.replace(
        "TOURNAMENT_SWITCHES=",
        "TOURNAMENT_SWITCHES=stage-1:192.0.2.20",
    ).replace("PLAYER_SUBNETS=", "PLAYER_SUBNETS=198.51.100.0/24")
    completed, payload = run_check(
        tmp_path,
        mode="configured",
        env_text=env_text,
        STUB_TARGETS_JSON='{"job":"infra-dist-ping","target":"192.0.2.20"}',
        STUB_PLAYER_STATUS='{"ok":true,"targets":{"total":0}}',
    )

    assert completed.returncode == 0
    assert checks_by_id(payload)["player_generator"]["status"] == "PASS"


def test_json_output_has_the_documented_shape(tmp_path):
    completed, payload = run_check(tmp_path)

    assert completed.stderr == ""
    assert set(payload) == {
        "mode",
        "result",
        "passed",
        "failed",
        "skipped",
        "checks",
        "duration_seconds",
    }
    assert payload["mode"] == "bootstrap"
    assert isinstance(payload["checks"], list)
    assert isinstance(payload["duration_seconds"], int)


def test_default_mode_is_bootstrap_and_quiet_only_prints_result(tmp_path):
    completed, output = run_check(tmp_path, mode=None, output="quiet")

    assert completed.returncode == 0
    assert output.startswith("Result: PASS")
    assert "Deployment check:" not in output


def test_console_apply_checks_services_on_the_internal_compose_network(tmp_path):
    completed, _ = run_check(
        tmp_path,
        mode="configured",
        PLATFORM_API_SELF_APPLY="true",
        STUB_CURL_BROKEN="true",
    )
    urls = (tmp_path / "http.log").read_text(encoding="utf-8")
    clients = (tmp_path / "http-client.log").read_text(encoding="utf-8")
    compose_calls = (tmp_path / "compose.log").read_text(encoding="utf-8")

    assert completed.returncode == 0
    assert "http://prometheus:9090/-/healthy" in urls
    assert "http://grafana:3000/api/health" in urls
    assert "http://bigscreen/" in urls
    assert "http://127.0.0.1:9200/health" in urls
    assert "python http://prometheus:9090/-/healthy" in clients
    assert "curl " not in clients
    assert "exec -T platform-api" not in compose_calls


def test_configured_mode_uses_one_total_timeout_budget(tmp_path):
    env_text = BASE_ENV.replace(
        "CORE_SWITCH_PING=", "CORE_SWITCH_PING=core:192.0.2.10",
    )
    completed, payload = run_check(
        tmp_path,
        mode="configured",
        env_text=env_text,
        DEPLOY_CHECK_TIMEOUT="10",
        DEPLOY_CHECK_INTERVAL="6",
        STUB_HEALTH_SERVICE="prometheus",
        STUB_HEALTH_MODE="starting_once",
        STUB_FAKE_TIME="true",
        STUB_SLEEP_ADVANCE="6",
    )

    assert completed.returncode == 1
    assert checks_by_id(payload)["configured_core"]["status"] == "FAIL"
    assert payload["duration_seconds"] == 12


def test_host_bootstrap_checks_platform_api_inside_unpublished_container(tmp_path):
    completed, payload = run_check(
        tmp_path,
        STUB_HOST_PLATFORM_API_FAIL="true",
    )
    urls = (tmp_path / "http.log").read_text(encoding="utf-8")
    compose_calls = (tmp_path / "compose.log").read_text(encoding="utf-8")

    assert completed.returncode == 0
    assert checks_by_id(payload)["platform_api_http"]["status"] == "PASS"
    assert "http://127.0.0.1:9200/health" not in urls
    assert "exec -T platform-api python -c" in compose_calls


def test_host_bootstrap_fails_when_platform_api_internal_health_fails(tmp_path):
    completed, payload = run_check(
        tmp_path,
        STUB_PLATFORM_API_INTERNAL_HEALTH="fail",
    )
    check = checks_by_id(payload)["platform_api_http"]

    assert completed.returncode == 1
    assert check["status"] == "FAIL"
    assert "Platform API container /health" in check["message"]
    assert "container health request failed" in check["message"]


def test_deploy_check_never_sources_the_environment_file():
    script = (ROOT / "deploy-check.sh").read_text(encoding="utf-8")

    assert "source .env" not in script
    assert ". .env" not in script
    assert "from platform_config import read_env" in script
