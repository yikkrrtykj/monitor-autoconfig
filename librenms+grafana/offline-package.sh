#!/usr/bin/env sh
set -eu

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$ROOT_DIR"

STAMP="$(date +%Y%m%d-%H%M%S)"
OUT_DIR="${OFFLINE_OUT_DIR:-dist/monitor-offline-$STAMP}"
ARCHIVE="${OUT_DIR}.tar.gz"

mkdir -p "$OUT_DIR"

echo "[offline] rendering compose image list"
docker compose config --images | sort -u > "$OUT_DIR/image-list.txt"

echo "[offline] building local helper images"
docker compose build player-targets topology-collector rsyslog grafana-setup >/dev/null

echo "[offline] pulling remote images"
docker compose pull --ignore-buildable || true

echo "[offline] saving images"
docker save -o "$OUT_DIR/images.tar" $(cat "$OUT_DIR/image-list.txt")

echo "[offline] copying project files"
tar \
  --exclude='./dist' \
  --exclude='./grafana-data' \
  --exclude='./prometheus-data' \
  --exclude='./librenms-data' \
  --exclude='./librenms-db-data' \
  --exclude='./loki-data' \
  --exclude='./promtail-data' \
  --exclude='./platform-state' \
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
echo "[offline] done: $ARCHIVE"
