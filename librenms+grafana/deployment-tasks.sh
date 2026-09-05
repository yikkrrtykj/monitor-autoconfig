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
  task_timeout=${LIBRENMS_CONFIG_TIMEOUT:-180}
  task_interval=${LIBRENMS_CONFIG_INTERVAL:-2}
  case "$task_timeout:$task_interval" in
    *[!0-9:]*|:*|*:) echo "Invalid required-task timeout/interval" >&2; exit 1 ;;
  esac
  task_deadline=$(( $(date +%s) + task_timeout ))
  [ "$task_deadline" -le "$DEPLOY_CHECK_DEADLINE" ] || task_deadline=$DEPLOY_CHECK_DEADLINE
  # timeout=0 retains a single immediate observation, without retrying waits.
  if [ "$task_timeout" -gt 0 ]; then DEPLOY_CHECK_DEADLINE=$task_deadline; fi
  old_config=$(deployment_compose ps -a -q librenms-config) || exit 1
  old_grafana=$(deployment_compose ps -a -q grafana-setup) || exit 1
  deployment_compose up -d --force-recreate --no-deps librenms-config grafana-setup || exit 1
  new_config=$(required_task_id librenms-config) || {
    echo "Required librenms-config container missing or ambiguous." >&2; exit 1;
  }
  new_grafana=$(required_task_id grafana-setup) || {
    echo "Required grafana-setup container missing or ambiguous." >&2; exit 1;
  }
  if [ "$new_config" = "$old_config" ] || [ "$new_grafana" = "$old_grafana" ]; then
    echo "Required task was not recreated for this operation." >&2
    exit 1
  fi
  for task in librenms-config grafana-setup; do
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

cleanup_disabled_feishu() (
  # Explicit profile discovers stopped/disabled services within this project.
  sidecar=$(deployment_compose --profile feishu ps -a -q feishu-ws) || exit 1
  if [ -z "$sidecar" ]; then
    echo "SKIP: Feishu disabled; no project sidecar exists."
    exit 0
  fi
  case "$sidecar" in *[!a-zA-Z0-9_-]*) echo "Ambiguous Feishu sidecar." >&2; exit 1 ;; esac
  reference=$(required_task_id prometheus) || exit 1
  project=$(deployment_command docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$reference") || exit 1
  owner=$(deployment_command docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$sidecar") || exit 1
  service=$(deployment_command docker inspect --format '{{index .Config.Labels "com.docker.compose.service"}}' "$sidecar") || exit 1
  if [ -z "$project" ] || [ "$project" = '<no value>' ] || [ "$project" != "$owner" ] || [ "$service" != feishu-ws ]; then
    echo "Feishu sidecar ownership mismatch; no container removed." >&2
    exit 1
  fi
  deployment_command docker rm -f "$sidecar" >/dev/null || {
    echo "Feishu sidecar removal failed." >&2; exit 1;
  }
  remaining_sidecar=$(deployment_compose --profile feishu ps -a -q feishu-ws) || exit 1
  [ -z "$remaining_sidecar" ] || { echo "Feishu sidecar still exists." >&2; exit 1; }
  echo "Feishu disabled; project sidecar stopped and removed."
)
