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

COMPOSE_PARALLEL_LIMIT="${COMPOSE_PARALLEL_LIMIT:-$(env_value COMPOSE_PARALLEL_LIMIT 2>/dev/null || true)}"
COMPOSE_PARALLEL_LIMIT="${COMPOSE_PARALLEL_LIMIT:-1}"
IMAGE_PULL_RETRIES="${IMAGE_PULL_RETRIES:-$(env_value IMAGE_PULL_RETRIES 2>/dev/null || true)}"
IMAGE_PULL_RETRIES="${IMAGE_PULL_RETRIES:-5}"
IMAGE_PULL_RETRY_DELAY="${IMAGE_PULL_RETRY_DELAY:-$(env_value IMAGE_PULL_RETRY_DELAY 2>/dev/null || true)}"
IMAGE_PULL_RETRY_DELAY="${IMAGE_PULL_RETRY_DELAY:-20}"
export COMPOSE_PARALLEL_LIMIT

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

pull_images

echo "[deploy] Starting monitoring stack..."
docker compose up -d --remove-orphans

echo "[deploy] Current service status:"
docker compose ps
