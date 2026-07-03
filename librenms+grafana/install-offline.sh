#!/usr/bin/env sh
set -eu

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$ROOT_DIR"

if [ -f images.tar ]; then
  echo "[offline-install] loading images"
  docker load -i images.tar
elif [ -f dist/images.tar ]; then
  echo "[offline-install] loading images from dist/"
  docker load -i dist/images.tar
else
  echo "[offline-install] images.tar not found, skipping image load"
fi

if [ ! -f .env ] && [ -f .env.example ]; then
  cp .env.example .env
  echo "[offline-install] created .env from .env.example"
fi

if [ ! -f event-config.yml ] && [ -f event-config.example.yml ]; then
  cp event-config.example.yml event-config.yml
  echo "[offline-install] created event-config.yml from example"
fi

chmod +x ./*.sh 2>/dev/null || true
echo "[offline-install] ready. Edit .env/event-config.yml, then run ./deploy.sh"
