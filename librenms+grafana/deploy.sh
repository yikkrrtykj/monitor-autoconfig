#!/bin/sh
# Stable first-run deploy helper.
# Pulls images one at a time with retries before starting the stack. This avoids
# losing the whole deploy when Docker Hub/CDN returns a transient 5xx for a layer.

set -eu

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR"

DEPLOY_WARNING_COUNT=0
deploy_warn() {
  DEPLOY_WARNING_COUNT=$((DEPLOY_WARNING_COUNT + 1))
  echo "[deploy] WARN: $*" >&2
}

if ! command -v python3 >/dev/null 2>&1; then
  echo "[deploy] ERROR: python3 is required to read platform and config versions." >&2
  exit 1
fi

version_output=$(python3 "$SCRIPT_DIR/version_info.py")
platform_version=$(printf '%s\n' "$version_output" | sed -n '1p')
platform_git_commit=$(printf '%s\n' "$version_output" | sed -n '2p')
supported_schema=$(printf '%s\n' "$version_output" | sed -n '3p')
export PLATFORM_GIT_COMMIT="$platform_git_commit"
echo "[deploy] Platform version: $platform_version"
echo "[deploy] Git commit: $platform_git_commit"
echo "[deploy] Supported config schema: $supported_schema"

inspect_event_config_schema() {
  [ -f "$SCRIPT_DIR/event-config.yml" ] || return 0
  (cd "$SCRIPT_DIR" && python3 - <<'PY'
import sys
from pathlib import Path

from platform_config import ConfigSchemaError, inspect_config_schema, parse_simple_yaml

try:
    config = parse_simple_yaml(Path("event-config.yml").read_text(encoding="utf-8"))
    status = inspect_config_schema(config)
except (OSError, ValueError, ConfigSchemaError) as exc:
    print(f"[deploy] ERROR: invalid event-config schema: {exc}", file=sys.stderr)
    raise SystemExit(1)

original = status["original_version"]
supported = status["current_supported"]
print(f"[deploy] Event config schema: {original}")
if status["config_too_new"]:
    print(
        f"[deploy] ERROR: event-config schema {original} is newer than supported schema {supported}.",
        file=sys.stderr,
    )
    print(
        "[deploy] Upgrade the monitoring platform before using this configuration.",
        file=sys.stderr,
    )
    raise SystemExit(1)
if status["migration_required"]:
    print(f"[deploy] Config will be migrated in memory to schema {supported}.")
    print("[deploy] event-config.yml will not be rewritten until Save/Apply.")
PY
  )
}

if ! inspect_event_config_schema; then
  exit 1
fi

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
  migrate_env_default UNIFI_AP_DOWN_FOR_SECONDS 180 10
  migrate_env_default UNIFI_AP_DOWN_FOR_SECONDS 90 10
  migrate_env_default UNIFI_AP_POLL_INTERVAL 15 5
  migrate_env_default UNIFI_CONTROLLER_REFRESH_SECONDS 60 5
  migrate_env_default UNIFI_CONTROLLER_REFRESH_SECONDS 10 5
  migrate_env_default UNIFI_SCRAPE_INTERVAL 30s 5s
  migrate_env_default UNIFI_SCRAPE_INTERVAL 10s 5s
  migrate_env_default SWITCH_IFMIB_SCRAPE_INTERVAL 10s 30s
  migrate_env_default STACKWISE_SCRAPE_INTERVAL 30s 60s
  migrate_env_default SWITCH_RESOURCE_SCRAPE_INTERVAL 60s 120s
  migrate_env_default SWITCH_DISCOVERY_WORKERS 32 8
  migrate_env_default PLAYER_SWITCH_PROBE_WORKERS 32 8
  migrate_env_default PLAYER_SWITCH_FULL_SCAN_INTERVAL 1800 21600
  migrate_env_default PLAYER_TARGETS_REFRESH_INTERVAL 300 60
  migrate_env_default TOPOLOGY_POLL_WORKERS 4 1
  migrate_env_default TOPOLOGY_POLL_WORKERS 2 1
  migrate_env_default TOPOLOGY_SNMP_DELAY_MS 100 500
  migrate_env_default TOPOLOGY_SNMP_DELAY_MS 250 500
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
import os
import tempfile
from pathlib import Path
from platform_config import (
    inspect_config_schema,
    merge_env_file,
    migrate_config,
    parse_simple_yaml,
    read_env,
    render_env,
    validate_config,
)
cfg = parse_simple_yaml(Path("event-config.yml").read_text(encoding="utf-8"))
if not isinstance(cfg, dict):
    raise SystemExit("event-config.yml is not a mapping")
schema = inspect_config_schema(cfg)
if schema["config_too_new"]:
    raise SystemExit(
        f"event-config schema {schema['original_version']} is newer than supported schema {schema['current_supported']}"
    )
cfg = migrate_config(cfg)
devices = cfg.get("devices") if isinstance(cfg.get("devices"), dict) else {}
core = devices.get("core") if isinstance(devices.get("core"), dict) else {}
core_ip = str(core.get("ip") or "").strip()
bad = [
    item for item in validate_config(cfg)
    if item.get("level") == "bad"
    and not (item.get("path") == "devices.core.ip" and not core_ip)
]
if bad:
    for item in bad:
        print(f"{item.get('path')}: {item.get('message')}")
    raise SystemExit("event-config.yml has blocking validation errors")
env = render_env(cfg, read_env(Path(".env")))
target = Path(".env")
fd, temporary = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=target.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(merge_env_file(target, env))
    os.replace(temporary, target)
except Exception:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
    raise
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
    deploy_warn "未能自动探测本机 IP，请手动在 .env 设置 SERVER_IP。"
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

if ! command -v docker >/dev/null 2>&1; then
  echo "[deploy] ERROR: docker is not available. Install Docker first." >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "[deploy] ERROR: docker compose is not available. Install Docker Compose plugin first." >&2
  exit 1
fi

if ! docker compose config --quiet; then
  echo "[deploy] ERROR: docker compose config failed; fix the configuration before deploying." >&2
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

  images=$(docker compose config --images 2>/dev/null) || {
    echo "[deploy] ERROR: could not determine required Compose images after pull failure." >&2
    return 1
  }
  missing_images=""
  for image in $images; do
    case "$image" in
      monitor-*:local) continue ;;
    esac
    if ! docker image inspect "$image" >/dev/null 2>&1; then
      missing_images="${missing_images}${missing_images:+
}${image}"
    fi
  done
  if [ -n "$missing_images" ]; then
    echo "[deploy] ERROR: image pull failed and these required images are not available locally:" >&2
    printf '%s\n' "$missing_images" | sed 's/^/[deploy]   - /' >&2
    return 1
  fi

  deploy_warn "image pull failed after $IMAGE_PULL_RETRIES attempts, but every required remote image exists locally; continuing with the local cache."
  return 0
}

pull_base_images() {
  # 本地构建镜像的基础镜像（docker/*/Dockerfile 里的 FROM）不在 compose pull 的
  # 服务镜像清单里，build 阶段才由 BuildKit 联网解析；BuildKit 在 registry 镜像站
  # 报错时不会像 docker pull 那样回退官方源，镜像站一抽风 build 直接失败。
  # 这里提前用 docker pull（带回退、带重试）把基础镜像备到本地，build 即离线。
  base_images=$(sed -nE 's/^[[:space:]]*FROM[[:space:]]+([^[:space:]]+).*/\1/p' docker/*/Dockerfile 2>/dev/null | sort -u)
  [ -n "$base_images" ] || return 0
  missing_images=""
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
        break
      fi
      echo "[deploy] Pull failed; retrying in ${IMAGE_PULL_RETRY_DELAY}s..."
      sleep "$IMAGE_PULL_RETRY_DELAY"
      attempt=$((attempt + 1))
    done
    if ! docker image inspect "$image" >/dev/null 2>&1; then
      missing_images="${missing_images}${missing_images:+
}${image}"
    fi
  done
  if [ -n "$missing_images" ]; then
    echo "[deploy] ERROR: local service images need rebuilding, but these Dockerfile base images could not be pulled and are not available locally:" >&2
    printf '%s\n' "$missing_images" | sed 's/^/[deploy]   - /' >&2
    return 1
  fi
  return 0
}

render_grafana_provisioning
if ! pull_images; then
  exit 1
fi

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
  needs_build=false
  echo "[deploy] Local image Dockerfiles unchanged; skipping rebuild."
else
  needs_build=true
  echo "[deploy] Local image missing or Dockerfile changed; building once (layers are cached)."
fi

if [ "$needs_build" = true ]; then
  if ! pull_base_images; then
    exit 1
  fi
  if ! docker compose up -d --remove-orphans --build; then
    echo "[deploy] ERROR: docker compose up --build failed." >&2
    exit 1
  fi
  if [ -n "$image_hash" ]; then
    stamp_tmp=$(mktemp "$SCRIPT_DIR/.deploy-local-image.sha256.XXXXXX")
    printf '%s\n' "$image_hash" > "$stamp_tmp"
    mv "$stamp_tmp" "$image_stamp"
  fi
else
  if ! docker compose up -d --remove-orphans; then
    echo "[deploy] ERROR: docker compose up failed." >&2
    exit 1
  fi
fi

# These services load bind-mounted source only when their process starts. A
# normal `compose up` may keep an existing container when only source files
# changed, leaving nginx's copied web files or Python's imported modules stale.
configured_services=$(docker compose config --services) || {
  echo "[deploy] ERROR: could not determine the enabled Compose services." >&2
  exit 1
}
service_is_enabled() {
  printf '%s\n' "$configured_services" | grep -Fxq "$1"
}

restart_failed=false
for service in bigscreen platform-api feishu-ws; do
  if ! service_is_enabled "$service"; then
    echo "[deploy] SKIP: restart $service (profile not enabled)."
    continue
  fi
  docker compose restart "$service" || {
    echo "[deploy] ERROR: restart $service failed." >&2
    restart_failed=true
    continue
  }
  echo "[deploy] Restarted $service."
done
# librenms-config is a one-shot container. Recreate it as well so source-only
# auto-config fixes (including existing-device SNMP credential synchronization)
# are applied by a normal deploy, not only after a console Apply operation.
if ! docker compose up -d --force-recreate --no-deps librenms-config; then
  echo "[deploy] ERROR: could not recreate librenms-config." >&2
  exit 1
fi

LIBRENMS_CONFIG_TIMEOUT=${LIBRENMS_CONFIG_TIMEOUT:-180}
LIBRENMS_CONFIG_INTERVAL=${LIBRENMS_CONFIG_INTERVAL:-2}
config_started=$(date +%s)
config_deadline=$((config_started + LIBRENMS_CONFIG_TIMEOUT))
while :; do
  config_id=$(docker compose ps -a -q librenms-config 2>/dev/null | sed -n '1p')
  if [ -z "$config_id" ]; then
    echo "[deploy] ERROR: librenms-config container does not exist after recreation." >&2
    echo "[deploy] Diagnose with: docker compose logs --tail=100 librenms-config" >&2
    exit 1
  fi
  config_state=$(docker inspect --format '{{.State.Status}}' "$config_id" 2>/dev/null || true)
  case "$config_state" in
    exited|dead)
      config_exit=$(docker inspect --format '{{.State.ExitCode}}' "$config_id" 2>/dev/null || true)
      if [ "$config_exit" = 0 ]; then
        echo "[deploy] librenms-config completed successfully (exit 0)."
        break
      fi
      echo "[deploy] ERROR: librenms-config failed (exit ${config_exit:-unknown})." >&2
      echo "[deploy] Diagnose with: docker compose logs --tail=100 librenms-config" >&2
      exit 1
      ;;
    running|created|restarting) : ;;
    *)
      echo "[deploy] ERROR: could not determine librenms-config state (${config_state:-unknown})." >&2
      echo "[deploy] Diagnose with: docker compose logs --tail=100 librenms-config" >&2
      exit 1
      ;;
  esac
  if [ "$(date +%s)" -ge "$config_deadline" ]; then
    echo "[deploy] ERROR: librenms-config did not finish within ${LIBRENMS_CONFIG_TIMEOUT}s (last state: $config_state)." >&2
    echo "[deploy] Diagnose with: docker compose logs --tail=100 librenms-config" >&2
    exit 1
  fi
  sleep "$LIBRENMS_CONFIG_INTERVAL"
done

# librenms-config may create or rotate /data/librenms-api-token. Restart only
# the long-running consumers that can have begun a collection cycle with the
# previous token, and do it only after the one-shot config container exits 0.
for service in topology-collector alertmanager-feishu-bridge; do
  if ! service_is_enabled "$service"; then
    echo "[deploy] SKIP: restart $service (profile not enabled)."
    continue
  fi
  docker compose restart "$service" || {
    echo "[deploy] ERROR: restart $service failed." >&2
    restart_failed=true
    continue
  }
  echo "[deploy] Restarted $service after librenms-config."
done

if [ "$restart_failed" = true ]; then
  echo "[deploy] ERROR: one or more required bind-mounted services failed to restart." >&2
  exit 1
fi

echo "[deploy] Current service status:"
docker compose ps

if ! "$SCRIPT_DIR/deploy-check.sh" bootstrap; then
  echo "[deploy] ERROR: platform bootstrap verification failed." >&2
  exit 1
fi

server_ip=$(env_value SERVER_IP 2>/dev/null || true)
server_ip=${server_ip:-SERVER_IP}
if [ "$DEPLOY_WARNING_COUNT" -gt 0 ]; then
  echo "[deploy] Result: PASS_WITH_WARNINGS (${DEPLOY_WARNING_COUNT} warning(s))."
else
  echo "[deploy] Result: PASS."
fi
echo "[deploy] Platform bootstrap completed successfully."
echo "[deploy] Open http://${server_ip}:8088/control and configure the event."
