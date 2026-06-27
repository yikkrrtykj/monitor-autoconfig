#!/bin/sh
# Stable first-run deploy helper.
# Pulls images one at a time with retries before starting the stack. This avoids
# losing the whole deploy when Docker Hub/CDN returns a transient 5xx for a layer.

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

migrate_env_default() {
  key=$1
  old=$2
  new=$3
  [ -f .env ] || return 0
  current=$(sed -n "s/^${key}=//p" .env | tail -n 1)
  if [ "$current" = "$old" ]; then
    tmp_env=$(mktemp)
    sed "s|^${key}=.*|${key}=${new}|" .env > "$tmp_env" && mv "$tmp_env" .env
    echo "[deploy] Migrated old default ${key}: ${old} -> ${new}"
  fi
}

migrate_legacy_defaults() {
  migrate_env_default UNIFI_AP_DOWN_FOR_SECONDS 10 180
  migrate_env_default UNIFI_AP_DOWN_FOR_SECONDS 90 180
  migrate_env_default UNIFI_AP_POLL_INTERVAL 15 5
  migrate_env_default UNIFI_CONTROLLER_REFRESH_SECONDS 60 10
  migrate_env_default UNIFI_SCRAPE_INTERVAL 30s 10s
}

# 探测本机主 IP：优先默认路由源 IP（ip route get），退而用 python UDP socket（不发包），再退 hostname -I。
detect_host_ip() {
  _ip=""
  if command -v ip >/dev/null 2>&1; then
    _ip=$(ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')
  fi
  if [ -z "$_ip" ] && command -v python3 >/dev/null 2>&1; then
    _ip=$(python3 -c 'import socket; s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.connect(("1.1.1.1",80)); print(s.getsockname()[0]); s.close()' 2>/dev/null)
  fi
  if [ -z "$_ip" ]; then
    _ip=$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -vE '^(127\.|169\.254\.|$)' | head -n 1)
  fi
  printf '%s' "$_ip"
}

if [ ! -f .env ]; then
  if [ -f .env.example ]; then
    cp .env.example .env
    echo "[deploy] .env not found; copied .env.example to .env"
    echo "[deploy] Edit .env for the venue IPs/passwords before production use."
  else
    echo "[deploy] ERROR: .env and .env.example are both missing." >&2
    exit 1
  fi
fi

migrate_legacy_defaults

# SERVER_IP 留空时自动探测本机 IP 并写回 .env（换场地清空 SERVER_IP= 即可重新探测）。
if ! env_value SERVER_IP >/dev/null 2>&1; then
  detected_ip=$(detect_host_ip)
  if [ -n "$detected_ip" ]; then
    if grep -qE '^SERVER_IP=' .env; then
      tmp_env=$(mktemp)
      sed "s|^SERVER_IP=.*|SERVER_IP=${detected_ip}|" .env > "$tmp_env" && mv "$tmp_env" .env
    else
      printf 'SERVER_IP=%s\n' "$detected_ip" >> .env
    fi
    export SERVER_IP="$detected_ip"
    echo "[deploy] SERVER_IP 为空，已自动探测本机 IP -> ${detected_ip}（写入 .env）"
  else
    echo "[deploy] WARN: 未能自动探测本机 IP，请手动在 .env 设置 SERVER_IP。" >&2
  fi
fi

COMPOSE_PARALLEL_LIMIT="${COMPOSE_PARALLEL_LIMIT:-$(env_value COMPOSE_PARALLEL_LIMIT 2>/dev/null || true)}"
COMPOSE_PARALLEL_LIMIT="${COMPOSE_PARALLEL_LIMIT:-1}"
IMAGE_PULL_RETRIES="${IMAGE_PULL_RETRIES:-$(env_value IMAGE_PULL_RETRIES 2>/dev/null || true)}"
IMAGE_PULL_RETRIES="${IMAGE_PULL_RETRIES:-5}"
IMAGE_PULL_RETRY_DELAY="${IMAGE_PULL_RETRY_DELAY:-$(env_value IMAGE_PULL_RETRY_DELAY 2>/dev/null || true)}"
IMAGE_PULL_RETRY_DELAY="${IMAGE_PULL_RETRY_DELAY:-20}"
export COMPOSE_PARALLEL_LIMIT

render_env_value() {
  env_value "$1" 2>/dev/null || true
}

render_grafana_provisioning() {
  if ! command -v python3 >/dev/null 2>&1; then
    echo "[deploy] ERROR: python3 is required to render Grafana provisioning." >&2
    echo "[deploy] Install python3 on the host, then rerun ./deploy.sh." >&2
    return 1
  fi

  export GRAFANA_PROVISIONING_SRC="$SCRIPT_DIR/grafana-provisioning"
  export GRAFANA_PROVISIONING_OUT="$SCRIPT_DIR/grafana-provisioning-rendered"
  export BIGSCREEN_ISP_NAMES="${BIGSCREEN_ISP_NAMES:-$(render_env_value BIGSCREEN_ISP_NAMES)}"
  export BIGSCREEN_ISP_AUTO_DISCOVER="${BIGSCREEN_ISP_AUTO_DISCOVER:-$(render_env_value BIGSCREEN_ISP_AUTO_DISCOVER)}"
  export BIGSCREEN_ISP_AUTO_DISCOVER="${BIGSCREEN_ISP_AUTO_DISCOVER:-true}"
  export FIREWALL_WAN_IF_FILTER="${FIREWALL_WAN_IF_FILTER:-$(render_env_value FIREWALL_WAN_IF_FILTER)}"
  export FIREWALL_WAN_IF_FILTER="${FIREWALL_WAN_IF_FILTER:-telecom,telcom,unicom,isp,WAN}"

  echo "[deploy] Rendering Grafana provisioning..."
  /bin/sh "$SCRIPT_DIR/render-grafana-provisioning.sh"
}

if ! docker compose version >/dev/null 2>&1; then
  echo "[deploy] ERROR: docker compose is not available. Install Docker Compose plugin first." >&2
  exit 1
fi

pull_images() {
  attempt=1
  while [ "$attempt" -le "$IMAGE_PULL_RETRIES" ]; do
    echo "[deploy] Pulling images (attempt $attempt/$IMAGE_PULL_RETRIES, parallel=$COMPOSE_PARALLEL_LIMIT)..."
    if docker compose pull; then
      return 0
    fi

    if [ "$attempt" -eq "$IMAGE_PULL_RETRIES" ]; then
      break
    fi

    echo "[deploy] Pull failed; retrying in ${IMAGE_PULL_RETRY_DELAY}s..."
    sleep "$IMAGE_PULL_RETRY_DELAY"
    attempt=$((attempt + 1))
  done

  echo "[deploy] ERROR: image pull failed after $IMAGE_PULL_RETRIES attempts." >&2
  echo "[deploy] This is usually Docker Hub/CDN/network instability. Configure a registry mirror or retry later." >&2
  return 1
}

render_grafana_provisioning
pull_images

echo "[deploy] Starting monitoring stack..."
docker compose rm -sf grafana-provisioning-render >/dev/null 2>&1 || true
docker compose up -d --remove-orphans

echo "[deploy] Current service status:"
docker compose ps
