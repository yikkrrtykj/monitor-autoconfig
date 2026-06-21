#!/bin/sh
# Apply .env changes without pulling images again.

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR"

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

if ! docker compose version >/dev/null 2>&1; then
  echo "[apply-env] ERROR: docker compose is not available." >&2
  exit 1
fi

render_grafana_provisioning

echo "[apply-env] Recreating services that read .env..."
docker compose up -d --force-recreate \
  prometheus \
  snmp-exporter \
  player-targets \
  topology-collector \
  blackbox-exporter \
  alertmanager-feishu-bridge \
  rsyslog \
  librenms \
  librenms-dispatcher \
  librenms-config \
  bigscreen \
  grafana \
  grafana-setup

echo "[apply-env] Done. Watch LibreNMS config progress with:"
echo "  docker logs -f librenms-config"
