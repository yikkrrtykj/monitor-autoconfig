#!/bin/sh
# Apply .env changes without pulling images again.

set -eu

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR"

detect_host_project_dir() {
  [ -n "${PLATFORM_HOST_WORKDIR:-}" ] && {
    printf '%s' "$PLATFORM_HOST_WORKDIR"
    return 0
  }
  [ -S /var/run/docker.sock ] || return 0
  command -v docker >/dev/null 2>&1 || return 0
  container_id=$(hostname 2>/dev/null || true)
  [ -n "$container_id" ] || return 0
  docker inspect "$container_id" \
    --format '{{range .Mounts}}{{if eq .Destination "/workspace"}}{{.Source}}{{end}}{{end}}' \
    2>/dev/null || true
}

HOST_PROJECT_DIR=$(detect_host_project_dir)

# Find a working compose command. Order:
#   1. `docker compose` (v2 plugin discovered normally)
#   2. `docker-compose` (v1 standalone on PATH)
#   3. the v2 plugin binary directly from a (host-mounted) cli-plugins dir --
#      when run from a container the plugin often isn't auto-discovered, but the
#      binary is a static Go executable that works when called by full path.
COMPOSE_CMD=""
if docker compose version >/dev/null 2>&1; then
  COMPOSE_CMD="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE_CMD="docker-compose"
else
  for _p in \
    /host/usr/libexec/docker/cli-plugins/docker-compose \
    /host/usr/lib/docker/cli-plugins/docker-compose \
    /host/usr/local/lib/docker/cli-plugins/docker-compose \
    /host/usr/local/libexec/docker/cli-plugins/docker-compose \
    /usr/libexec/docker/cli-plugins/docker-compose \
    /usr/lib/docker/cli-plugins/docker-compose \
    /usr/local/lib/docker/cli-plugins/docker-compose; do
    if [ -x "$_p" ] && "$_p" version >/dev/null 2>&1; then
      COMPOSE_CMD="$_p"
      break
    fi
  done
fi

compose() {
  if [ -n "$HOST_PROJECT_DIR" ]; then
    $COMPOSE_CMD \
      -f "$SCRIPT_DIR/docker-compose.yml" \
      --env-file "$SCRIPT_DIR/.env" \
      --project-directory "$HOST_PROJECT_DIR" \
      "$@"
  else
    $COMPOSE_CMD "$@"
  fi
}

env_value() {
  key=$1
  file=${2:-.env}
  [ -f "$file" ] || return 1
  command -v python3 >/dev/null 2>&1 || return 1
  python3 "$SCRIPT_DIR/platform_config.py" env-get "$file" "$key"
}

render_env_value() {
  env_value "$1" 2>/dev/null || true
}

migrate_env_default() {
  key=$1
  old=$2
  new=$3
  [ -f .env ] || return 0
  current=$(env_value "$key" .env 2>/dev/null || true)
  if [ "$current" = "$old" ]; then
    tmp_env=$(mktemp)
    sed "s|^${key}=.*|${key}=${new}|" .env > "$tmp_env" && mv "$tmp_env" .env
    echo "[apply-env] Migrated old default ${key}: ${old} -> ${new}"
  fi
}

migrate_legacy_defaults() {
  migrate_env_default UNIFI_AP_DOWN_FOR_SECONDS 10 180
  migrate_env_default UNIFI_AP_DOWN_FOR_SECONDS 90 180
  migrate_env_default UNIFI_AP_POLL_INTERVAL 15 5
  migrate_env_default UNIFI_CONTROLLER_REFRESH_SECONDS 60 10
  migrate_env_default UNIFI_SCRAPE_INTERVAL 30s 10s
}

# Keep .env in sync with event-config.yml (the console's source of truth) before
# restarting, so a plain restart can't resurrect a previous event's values.
# merge_env_file only overwrites the keys the config renders, so hand-tuned
# advanced keys in .env are preserved.
sync_env_from_config() {
  [ -f "$SCRIPT_DIR/event-config.yml" ] || return 0
  [ -f "$SCRIPT_DIR/platform_config.py" ] || return 0
  command -v python3 >/dev/null 2>&1 || return 1
  if (cd "$SCRIPT_DIR" && python3 - <<'PY'
from pathlib import Path
from platform_config import parse_simple_yaml, render_env, read_env, merge_env_file, validate_config
cfg = parse_simple_yaml(Path("event-config.yml").read_text(encoding="utf-8"))
if not isinstance(cfg, dict):
    raise SystemExit("event-config.yml is not a mapping")
bad = [item for item in validate_config(cfg) if item.get("level") == "bad"]
if bad:
    for item in bad:
        print(f"{item.get('path')}: {item.get('message')}")
    raise SystemExit("event-config.yml has blocking validation errors")
env = render_env(cfg, read_env(Path(".env")))
Path(".env").write_text(merge_env_file(Path(".env"), env), encoding="utf-8")
PY
  ); then
    echo "[apply-env] .env synced from event-config.yml"
  else
    echo "[apply-env] ERROR: could not validate/sync .env from event-config.yml" >&2
    return 1
  fi
}

render_grafana_provisioning() {
  if ! command -v python3 >/dev/null 2>&1; then
    echo "[apply-env] ERROR: python3 is required to render Grafana provisioning." >&2
    return 1
  fi

  export GRAFANA_PROVISIONING_SRC="$SCRIPT_DIR/grafana-provisioning"
  export GRAFANA_PROVISIONING_OUT="$SCRIPT_DIR/grafana-provisioning-rendered"
  export BIGSCREEN_ISP_NAMES="${BIGSCREEN_ISP_NAMES:-$(render_env_value BIGSCREEN_ISP_NAMES)}"
  export BIGSCREEN_ISP_AUTO_DISCOVER="${BIGSCREEN_ISP_AUTO_DISCOVER:-$(render_env_value BIGSCREEN_ISP_AUTO_DISCOVER)}"
  export BIGSCREEN_ISP_AUTO_DISCOVER="${BIGSCREEN_ISP_AUTO_DISCOVER:-true}"
  export FIREWALL_WAN_IF_FILTER="${FIREWALL_WAN_IF_FILTER:-$(render_env_value FIREWALL_WAN_IF_FILTER)}"
  export FIREWALL_WAN_IF_FILTER="${FIREWALL_WAN_IF_FILTER:-telecom,telcom,unicom,isp,WAN}"

  echo "[apply-env] Rendering Grafana provisioning..."
  /bin/sh "$SCRIPT_DIR/render-grafana-provisioning.sh"
}

if [ -z "$COMPOSE_CMD" ]; then
  echo "[apply-env] ERROR: 找不到 docker compose（v2 插件）或 docker-compose（v1）。" >&2
  echo "[apply-env]        请在服务器上确认 docker compose 可用，或手动执行：cd librenms+grafana && ./apply-env.sh" >&2
  exit 1
fi

if [ -n "$HOST_PROJECT_DIR" ]; then
  echo "[apply-env] Using host project directory: $HOST_PROJECT_DIR"
fi

if ! sync_env_from_config; then
  exit 1
fi
migrate_legacy_defaults
# Never restart Grafana against stale or partially rendered provisioning. A
# render failure makes the whole apply fail so platform-api can restore the
# paired config/.env snapshot instead of reporting a false success.
if ! render_grafana_provisioning; then
  echo "[apply-env] ERROR: Grafana provisioning render failed; no services were recreated." >&2
  exit 1
fi

SERVICES="
  prometheus
  snmp-exporter
  player-targets
  topology-collector
  blackbox-exporter
  alertmanager-feishu-bridge
  rsyslog
  librenms
  librenms-dispatcher
  librenms-config
  bigscreen
  grafana
  grafana-setup
"

# A host-side apply must refresh platform-api too so changes to its auth/apply
# settings take effect. Console applies are executed by that same container and
# set PLATFORM_API_SELF_APPLY=true; recreating the caller would kill it before
# it can persist the operation result and perform rollback when necessary.
if [ "${PLATFORM_API_SELF_APPLY:-false}" != "true" ]; then
  SERVICES="${SERVICES}  platform-api
"
fi

# unpoller reads the controller URL and credentials only when its container is
# created. A plain restart leaves the old values in the container, so include
# it in the recreate set whenever the UniFi compose profile is enabled.
COMPOSE_PROFILES_VALUE=$(render_env_value COMPOSE_PROFILES)
REMOVE_UNPOLLER=false
case ",${COMPOSE_PROFILES_VALUE}," in
  *,unifi,*) SERVICES="${SERVICES}  unpoller
" ;;
  *)
    if docker inspect unpoller >/dev/null 2>&1; then
      REMOVE_UNPOLLER=true
    fi
    ;;
esac

compose_up() {
  compose up -d --force-recreate $SERVICES
}

echo "[apply-env] Recreating services that read .env..."
if ! compose_up; then
  echo "[apply-env] ERROR: service recreation failed; containers were left intact for diagnosis/rollback." >&2
  exit 1
fi

if [ "$REMOVE_UNPOLLER" = "true" ]; then
  echo "[apply-env] UniFi profile disabled; removing the existing unpoller container."
  docker rm -f unpoller >/dev/null
fi

echo "[apply-env] Done. Watch LibreNMS config progress with:"
echo "  docker logs -f librenms-config"
