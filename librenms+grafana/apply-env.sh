#!/bin/sh
# Apply .env changes without pulling images again.

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
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

# Prefer the v2 plugin (`docker compose`); fall back to the v1 standalone
# (`docker-compose`) so hosts that only have the old binary still work.
if docker compose version >/dev/null 2>&1; then
  COMPOSE_CMD="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE_CMD="docker-compose"
else
  COMPOSE_CMD=""
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
  value=$(sed -n "s/^${key}=//p" "$file" | tail -n 1)
  [ -n "$value" ] || return 1
  printf '%s' "$value"
}

render_env_value() {
  env_value "$1" 2>/dev/null || true
}

migrate_env_default() {
  key=$1
  old=$2
  new=$3
  [ -f .env ] || return 0
  current=$(sed -n "s/^${key}=//p" .env | tail -n 1)
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

migrate_legacy_defaults
render_grafana_provisioning

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

compose_up() {
  compose up -d --force-recreate $SERVICES
}

cleanup_conflicting_containers() {
  echo "[apply-env] Recreate failed. Cleaning possibly half-recreated containers and retrying..."
  for name in $SERVICES; do
    docker rm -f "$name" >/dev/null 2>&1 || true
  done
}

echo "[apply-env] Recreating services that read .env..."
if ! compose_up; then
  cleanup_conflicting_containers
  compose_up
fi

echo "[apply-env] Done. Watch LibreNMS config progress with:"
echo "  docker logs -f librenms-config"
