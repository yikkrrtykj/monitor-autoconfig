#!/usr/bin/env sh
set -eu

ROOT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
cd "$ROOT_DIR"

if ! command -v docker >/dev/null 2>&1; then
  echo "[offline-install] ERROR: Docker is not installed" >&2
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "[offline-install] ERROR: docker compose v2 is not installed" >&2
  exit 1
fi

sha256_value() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    echo "[offline-install] ERROR: sha256sum or shasum is required" >&2
    return 1
  fi
}

verify_image_archive() {
  image_file=$1
  checksum_file="${image_file}.sha256"
  if [ ! -f "$checksum_file" ]; then
    echo "[offline-install] ERROR: checksum file not found: $checksum_file" >&2
    return 1
  fi
  expected=$(awk 'NR == 1 {print $1}' "$checksum_file")
  actual=$(sha256_value "$image_file")
  if [ -z "$expected" ] || [ "$actual" != "$expected" ]; then
    echo "[offline-install] ERROR: image archive checksum mismatch" >&2
    return 1
  fi
}

if [ -f images.tar ]; then
  verify_image_archive images.tar
  echo "[offline-install] loading images"
  docker load -i images.tar
elif [ -f dist/images.tar ]; then
  verify_image_archive dist/images.tar
  echo "[offline-install] loading images from dist/"
  docker load -i dist/images.tar
else
  echo "[offline-install] ERROR: images.tar not found" >&2
  exit 1
fi

if [ ! -f image-list.txt ]; then
  echo "[offline-install] ERROR: image-list.txt not found" >&2
  exit 1
fi
while IFS= read -r image; do
  [ -n "$image" ] || continue
  if ! docker image inspect "$image" >/dev/null 2>&1; then
    echo "[offline-install] ERROR: imported archive is missing image: $image" >&2
    exit 1
  fi
done < image-list.txt

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
