#!/bin/sh
# Completion gates for deploy/apply, not a general command runner.
# The caller supplies SCRIPT_DIR, COMPOSE_CMD and optional HOST_PROJECT_DIR.

deployment_budget_init() {
  case "$1" in ''|*[!0-9]*) echo "Invalid deployment timeout" >&2; return 1 ;; esac
  command -v timeout >/dev/null 2>&1 || {
    echo "Deployment requires timeout for bounded Docker calls." >&2
    return 1
  }
  local_deadline=$(( $(date +%s) + $1 ))
  if [ -n "${DEPLOY_CHECK_DEADLINE:-}" ]; then
    case "$DEPLOY_CHECK_DEADLINE" in *[!0-9]*) return 1 ;; esac
    [ "$local_deadline" -le "$DEPLOY_CHECK_DEADLINE" ] || local_deadline=$DEPLOY_CHECK_DEADLINE
  fi
  DEPLOY_CHECK_DEADLINE=$local_deadline
  export DEPLOY_CHECK_DEADLINE
}

deployment_command() {
  remaining=$((DEPLOY_CHECK_DEADLINE - $(date +%s)))
  [ "$remaining" -gt 0 ] || {
    echo "Deployment completion budget exhausted." >&2
    return 1
  }
  # Bound even a hung Docker client; do not consume caller heredoc commands.
  timeout --signal=TERM --kill-after=1 "$remaining" "$@" </dev/null
}

deployment_compose() {
  if [ -n "${HOST_PROJECT_DIR:-}" ]; then
    deployment_command $COMPOSE_CMD -f "$SCRIPT_DIR/docker-compose.yml" \
      --env-file "$SCRIPT_DIR/.env" --project-directory "$HOST_PROJECT_DIR" "$@"
  else
    deployment_command $COMPOSE_CMD "$@"
  fi
}

required_task_id() {
  task_id=$(deployment_compose ps -a -q "$1") || return 1
  case "$task_id" in ''|*[!a-zA-Z0-9_-]*) return 1 ;; esac
  printf '%s' "$task_id"
}

run_required_tasks() (
  [ "$#" -gt 0 ] || set -- librenms-config grafana-setup
  task_timeout=${LIBRENMS_CONFIG_TIMEOUT:-180}
  task_interval=${LIBRENMS_CONFIG_INTERVAL:-2}
  case "$task_timeout:$task_interval" in
    *[!0-9:]*|:*|*:) echo "Invalid required-task timeout/interval" >&2; exit 1 ;;
  esac
  task_deadline=$(( $(date +%s) + task_timeout ))
  [ "$task_deadline" -le "$DEPLOY_CHECK_DEADLINE" ] || task_deadline=$DEPLOY_CHECK_DEADLINE
  # timeout=0 retains a single immediate observation, without retrying waits.
  if [ "$task_timeout" -gt 0 ]; then DEPLOY_CHECK_DEADLINE=$task_deadline; fi
  for task do
    old_id=$(deployment_compose ps -a -q "$task") || exit 1
    case "$task" in
      librenms-config) old_config=$old_id ;;
      grafana-setup) old_grafana=$old_id ;;
      *) echo "Invalid required task." >&2; exit 1 ;;
    esac
  done
  if [ "${RECOVER_APPLY:-false}" = true ]; then
    deployment_compose up -d --force-recreate --no-deps --no-build --pull never "$@" || exit 1
  else
    deployment_compose up -d --force-recreate --no-deps "$@" || exit 1
  fi
  for task do
    id=$(required_task_id "$task") || {
      echo "Required $task container missing or ambiguous." >&2; exit 1;
    }
    if [ "$task" = librenms-config ]; then
      old_id=$old_config
      new_config=$id
    else
      old_id=$old_grafana
      new_grafana=$id
    fi
    if [ "$id" = "$old_id" ]; then
      echo "Required task was not recreated for this operation ($task)." >&2
      exit 1
    fi
  done
  for task do
    if [ "$task" = librenms-config ]; then id=$new_config; else id=$new_grafana; fi
    while :; do
      # Inspect the captured id, never a later container with the same name.
      snapshot=$(deployment_command docker inspect --format '{{.State.Status}} {{.State.ExitCode}}' "$id" 2>/dev/null) || {
        echo "Could not inspect required task $task." >&2; exit 1;
      }
      state=${snapshot%% *}
      code=${snapshot#* }
      case "$state" in
        exited)
          if [ "$code" != 0 ]; then
            echo "$task failed (exit $code)." >&2
            echo "Diagnose with: docker compose logs --tail=100 $task" >&2
            exit 1
          fi
          echo "$task completed successfully (this operation, exit 0)."
          break ;;
        created|running|restarting) : ;;
        *) echo "Required task $task has invalid state: $state" >&2; exit 1 ;;
      esac
      task_remaining=$((task_deadline - $(date +%s)))
      [ "$task_remaining" -gt 0 ] || { echo "Required task $task timed out." >&2; exit 1; }
      pause=$task_interval
      [ "$pause" -le "$task_remaining" ] || pause=$task_remaining
      sleep "$pause"
    done
  done
)

recover_required_tasks() (
  hashes=$(deployment_compose config --hash '*') || exit 1
  set --
  for task in librenms-config grafana-setup; do
    expected=$(printf '%s\n' "$hashes" | awk -v task="$task" '$1 == task {print $2}')
    case "$expected" in
      ''|*[!a-f0-9]*) echo "Missing or invalid Compose hash for $task." >&2; exit 1 ;;
    esac
    [ "${#expected}" -eq 64 ] || { echo "Invalid Compose hash for $task." >&2; exit 1; }
    id=$(deployment_compose ps -a -q "$task") || exit 1
    case "$id" in *[!a-zA-Z0-9_-]*) echo "Ambiguous $task container." >&2; exit 1 ;; esac
    observed=
    if [ -n "$id" ]; then
      observed=$(deployment_command docker inspect --format \
        '{{.State.Status}} {{.State.ExitCode}} {{index .Config.Labels "com.docker.compose.config-hash"}}' "$id") || exit 1
    fi
    if [ "$observed" != "exited 0 $expected" ]; then
      set -- "$@" "$task"
    fi
  done
  # A successful task for exactly the restored spec has no state to undo.
  [ "$#" -eq 0 ] || run_required_tasks "$@"
)

cleanup_disabled_profile() (
  profile=$1
  service_name=$2
  label=$3
  # Explicit profile discovers stopped/disabled services within this project.
  sidecar=$(deployment_compose --profile "$profile" ps -a -q "$service_name") || exit 1
  if [ -z "$sidecar" ]; then
    echo "SKIP: $label disabled; no project sidecar exists."
    exit 0
  fi
  case "$sidecar" in *[!a-zA-Z0-9_-]*) echo "Ambiguous $label sidecar." >&2; exit 1 ;; esac
  reference=$(required_task_id prometheus) || exit 1
  project=$(deployment_command docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$reference") || exit 1
  owner=$(deployment_command docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$sidecar") || exit 1
  service=$(deployment_command docker inspect --format '{{index .Config.Labels "com.docker.compose.service"}}' "$sidecar") || exit 1
  if [ -z "$project" ] || [ "$project" = '<no value>' ] || [ "$project" != "$owner" ] || [ "$service" != "$service_name" ]; then
    echo "$label sidecar ownership mismatch; no container removed." >&2
    exit 1
  fi
  deployment_command docker rm -f "$sidecar" >/dev/null || {
    echo "$label sidecar removal failed." >&2; exit 1;
  }
  remaining_sidecar=$(deployment_compose --profile "$profile" ps -a -q "$service_name") || exit 1
  [ -z "$remaining_sidecar" ] || { echo "$label sidecar still exists." >&2; exit 1; }
  echo "$label disabled; project sidecar stopped and removed."
)

cleanup_disabled_feishu() {
  cleanup_disabled_profile feishu feishu-ws Feishu
}
