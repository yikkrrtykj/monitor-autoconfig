#!/usr/bin/env sh
set -eu

ROOT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
cd "$ROOT_DIR"

STAMP="$(date +%Y%m%d-%H%M%S)"
OUT_DIR="${OFFLINE_OUT_DIR:-dist/monitor-offline-$STAMP}"
ARCHIVE="${OUT_DIR}.tar.gz"

for command_name in docker tar xargs; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "[offline] ERROR: required command not found: $command_name" >&2
    exit 1
  fi
done
if ! docker compose version >/dev/null 2>&1; then
  echo "[offline] ERROR: docker compose v2 is required" >&2
  exit 1
fi

# Project files are streamed into OUT_DIR below. Keeping an in-tree custom
# output outside dist would make tar copy the package into itself recursively.
mkdir -p "$(dirname "$OUT_DIR")"
OUT_PARENT="$(CDPATH='' cd -- "$(dirname "$OUT_DIR")" && pwd)"
case "$OUT_PARENT" in
  "$ROOT_DIR"|"$ROOT_DIR"/*)
    case "$OUT_PARENT" in
      "$ROOT_DIR/dist"|"$ROOT_DIR/dist"/*) ;;
      *)
        echo "[offline] ERROR: in-project OFFLINE_OUT_DIR must be under dist/" >&2
        exit 1
        ;;
    esac
    ;;
esac
if [ -e "$OUT_DIR" ] || [ -e "$ARCHIVE" ]; then
  echo "[offline] ERROR: output already exists: $OUT_DIR" >&2
  exit 1
fi
mkdir -p "$OUT_DIR"

sha256_file() {
  file=$1
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$(basename "$file")" > "$(basename "$file").sha256"
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$(basename "$file")" > "$(basename "$file").sha256"
  else
    echo "[offline] ERROR: sha256sum or shasum is required" >&2
    return 1
  fi
}

echo "[offline] rendering compose image list"
# Include optional profile services (currently UniFi) even when the packaging
# machine has that profile disabled; the offline site may enable it later.
docker compose --profile '*' config --images | sort -u > "$OUT_DIR/image-list.txt"

echo "[offline] building local helper images"
docker compose --profile '*' build player-targets topology-collector rsyslog grafana-setup >/dev/null

echo "[offline] pulling remote images"
if ! docker compose --profile '*' pull --ignore-buildable; then
  echo "[offline] ERROR: one or more remote images could not be pulled" >&2
  exit 1
fi

echo "[offline] saving images"
xargs docker save -o "$OUT_DIR/images.tar" < "$OUT_DIR/image-list.txt"
(cd "$OUT_DIR" && sha256_file images.tar)

echo "[offline] copying project files"
tar \
  --exclude='./dist' \
  --exclude='./.git' \
  --exclude='./.env' \
  --exclude='./event-config.yml' \
  --exclude='./.pytest_cache' \
  --exclude='./__pycache__' \
  --exclude='./grafana-data' \
  --exclude='./prometheus-data' \
  --exclude='./librenms-data' \
  --exclude='./librenms-db-data' \
  --exclude='./loki-data' \
  --exclude='./promtail-data' \
  --exclude='./platform-state' \
  --exclude='./grafana-provisioning-rendered' \
  --exclude='./librenms-rrdcached-journal' \
  --exclude='./*.tar.gz' \
  -cf - . | tar -xf - -C "$OUT_DIR"

cat > "$OUT_DIR/OFFLINE-README.txt" <<'EOF'
离线部署:
1. 把整个目录复制到现场服务器
2. cd monitor-offline-*
3. ./install-offline.sh
4. 编辑 .env 或 event-config.yml
5. ./deploy.sh
EOF

echo "[offline] writing archive $ARCHIVE"
tar -czf "$ARCHIVE" -C "$(dirname "$OUT_DIR")" "$(basename "$OUT_DIR")"
(cd "$(dirname "$ARCHIVE")" && sha256_file "$(basename "$ARCHIVE")")
echo "[offline] done: $ARCHIVE"
