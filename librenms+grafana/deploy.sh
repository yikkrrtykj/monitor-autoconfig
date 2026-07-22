#!/bin/sh
# Stable first-run deploy helper.
# Pulls images one at a time with retries before starting the stack. This avoids
# losing the whole deploy when Docker Hub/CDN returns a transient 5xx for a layer.

set -eu

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR"

env_value() {
  key=$1
  file=${2:-.env}
  [ -f "$file" ] || return 1
  command -v python3 >/dev/null 2>&1 || return 1
  python3 "$SCRIPT_DIR/platform_config.py" env-get "$file" "$key"
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

sync_env_from_config() {
  [ -f "$SCRIPT_DIR/event-config.yml" ] || return 0
  command -v python3 >/dev/null 2>&1 || {
    echo "[deploy] ERROR: python3 is required to validate event-config.yml." >&2
    return 1
  }
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
    echo "[deploy] .env synced from event-config.yml"
  else
    echo "[deploy] ERROR: could not validate/sync .env from event-config.yml" >&2
    return 1
  fi
}

if ! sync_env_from_config; then
  exit 1
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
  # monitor-*:local 这几个镜像是本地构建的，任何仓库里都没有，pull 必然报错。
  # compose v2.15+ 用 --ignore-buildable 直接跳过它们；老版本跳不过时，pull 的
  # 失败不再中断部署（后面的 up -d 会自行构建本地镜像、补拉缺失镜像）。
  pull_args=""
  if docker compose pull --help 2>/dev/null | grep -q -- "--ignore-buildable"; then
    pull_args="--ignore-buildable"
  fi

  attempt=1
  while [ "$attempt" -le "$IMAGE_PULL_RETRIES" ]; do
    echo "[deploy] Pulling images (attempt $attempt/$IMAGE_PULL_RETRIES, parallel=$COMPOSE_PARALLEL_LIMIT)..."
    if docker compose pull $pull_args; then
      return 0
    fi

    if [ "$attempt" -eq "$IMAGE_PULL_RETRIES" ]; then
      break
    fi

    echo "[deploy] Pull failed; retrying in ${IMAGE_PULL_RETRY_DELAY}s..."
    sleep "$IMAGE_PULL_RETRY_DELAY"
    attempt=$((attempt + 1))
  done

  echo "[deploy] WARN: image pull still failing after $IMAGE_PULL_RETRIES attempts; continuing anyway." >&2
  echo "[deploy] WARN: 本地构建镜像(monitor-*:local)拉不到是正常的；若真缺镜像，下面 up 阶段会报出来。" >&2
  return 0
}

pull_base_images() {
  # 本地构建镜像的基础镜像（docker/*/Dockerfile 里的 FROM）不在 compose pull 的
  # 服务镜像清单里，build 阶段才由 BuildKit 联网解析；BuildKit 在 registry 镜像站
  # 报错时不会像 docker pull 那样回退官方源，镜像站一抽风 build 直接失败。
  # 这里提前用 docker pull（带回退、带重试）把基础镜像备到本地，build 即离线。
  base_images=$(sed -nE 's/^[[:space:]]*FROM[[:space:]]+([^[:space:]]+).*/\1/p' docker/*/Dockerfile 2>/dev/null | sort -u)
  [ -n "$base_images" ] || return 0
  for image in $base_images; do
    [ "$image" = "scratch" ] && continue
    if docker image inspect "$image" >/dev/null 2>&1; then
      echo "[deploy] Base image $image already present."
      continue
    fi
    attempt=1
    while [ "$attempt" -le "$IMAGE_PULL_RETRIES" ]; do
      echo "[deploy] Pulling base image $image (attempt $attempt/$IMAGE_PULL_RETRIES)..."
      if docker pull "$image"; then
        break
      fi
      if [ "$attempt" -eq "$IMAGE_PULL_RETRIES" ]; then
        echo "[deploy] WARN: base image $image 拉取失败，build 阶段可能因此报错；可稍后手动 docker pull $image 再重跑。" >&2
        break
      fi
      echo "[deploy] Pull failed; retrying in ${IMAGE_PULL_RETRY_DELAY}s..."
      sleep "$IMAGE_PULL_RETRY_DELAY"
      attempt=$((attempt + 1))
    done
  done
  return 0
}

render_grafana_provisioning
pull_images
pull_base_images

echo "[deploy] Checking local service images and starting monitoring stack..."
docker compose rm -sf grafana-provisioning-render >/dev/null 2>&1 || true
# Application source is bind-mounted, so a normal git pull does not require a
# Docker rebuild. Rebuild only when a local image is missing or a Dockerfile
# changed; this avoids repeatedly contacting apt/apk/pip on restricted links.
image_stamp="$SCRIPT_DIR/.deploy-local-image.sha256"
image_hash=""
if command -v sha256sum >/dev/null 2>&1; then
  image_hash=$(
    find docker -type f -name Dockerfile -print | sort | while IFS= read -r file; do sha256sum "$file"; done \
      | sha256sum | awk '{print $1}'
  )
fi
images_ready=true
for image in monitor-grafana-setup:local monitor-platform-api:local monitor-rsyslog:local monitor-player-tools:local; do
  if ! docker image inspect "$image" >/dev/null 2>&1; then
    images_ready=false
    break
  fi
done
previous_hash=$([ -f "$image_stamp" ] && cat "$image_stamp" || true)
if [ -n "$image_hash" ] && [ "$image_hash" = "$previous_hash" ] && [ "$images_ready" = true ]; then
  echo "[deploy] Local image Dockerfiles unchanged; skipping rebuild."
  docker compose up -d --remove-orphans
else
  echo "[deploy] Local image missing or Dockerfile changed; building once (layers are cached)."
  docker compose up -d --remove-orphans --build
  if [ -n "$image_hash" ]; then
    printf '%s\n' "$image_hash" > "$image_stamp"
  fi
fi

# These services load bind-mounted source only when their process starts. A
# normal `compose up` may keep an existing container when only source files
# changed, leaving nginx's copied web files or Python's imported modules stale.
# Restart individually with || true: under `set -e`, one absent/not-yet-created
# service must not abort the deploy after the stack already came up fine.
for service in bigscreen platform-api alertmanager-feishu-bridge feishu-ws; do
  docker compose restart "$service" || echo "[deploy] WARN: restart $service failed (service missing or not running)"
done
# librenms-config is a one-shot container. Recreate it as well so source-only
# auto-config fixes (including existing-device SNMP credential synchronization)
# are applied by a normal deploy, not only after a console Apply operation.
docker compose up -d --force-recreate --no-deps librenms-config || echo "[deploy] WARN: recreate librenms-config failed"

echo "[deploy] Current service status:"
docker compose ps
