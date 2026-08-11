#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
MODE="${1:---static}"
ENV_FILE="${SMOKE_ENV_FILE:-$WORK_DIR/.env.example}"

cd "$WORK_DIR"
if [[ ! -x "$WORK_DIR/deploy.sh" ]]; then
  echo "[smoke] deploy.sh is not executable" >&2
  exit 1
fi
docker compose --env-file "$ENV_FILE" config --quiet
python3 -m py_compile ./*.py
python3 - <<'PY'
import json
from pathlib import Path
for path in Path("grafana-provisioning/dashboard-json").glob("*.json"):
    json.loads(path.read_text(encoding="utf-8-sig"))
print("[smoke] static configuration, Python and dashboard JSON: OK")
PY

if [[ "$MODE" == "--static" ]]; then
  exit 0
fi
if [[ "$MODE" != "--live" ]]; then
  echo "usage: $0 [--static|--live]" >&2
  exit 2
fi

for url in \
  http://127.0.0.1:9090/-/healthy \
  http://127.0.0.1:3000/api/health \
  http://127.0.0.1:9200/health; do
  curl --fail --silent --show-error --max-time 5 "$url" >/dev/null
done

# The bridge is intentionally internal-only; verify it from its own container
# instead of weakening the deployment by publishing port 5005 on the host.
docker compose exec -T alertmanager-feishu-bridge python -c \
  'import json,urllib.request; data=json.load(urllib.request.urlopen("http://127.0.0.1:5005/health", timeout=5)); assert data.get("ready") is True; print("[smoke] Feishu bridge internal health: OK")'

docker compose exec -T platform-api python -c \
  'import telnetlib3, shutil; assert shutil.which("iperf3"); print("[smoke] telnetlib3 + iperf3 client: OK")'

# Optional authenticated read-only checks. No switch session, alert, config
# write or bandwidth test is ever started by this script.
if [[ -n "${SMOKE_ADMIN_PASSWORD:-}" ]]; then
  COOKIE_JAR="$(mktemp)"
  trap 'rm -f -- "$COOKIE_JAR"' EXIT
  SMOKE_LOGIN_JSON="$(python3 -c 'import json,os; print(json.dumps({"username": os.environ.get("SMOKE_ADMIN_USER", "admin"), "password": os.environ["SMOKE_ADMIN_PASSWORD"]}))')"
  curl --fail --silent --show-error --max-time 5 \
    -c "$COOKIE_JAR" \
    -H 'Content-Type: application/json' \
    --data "$SMOKE_LOGIN_JSON" \
    http://127.0.0.1:9200/auth/login >/dev/null
  curl --fail --silent --show-error --max-time 5 \
    -b "$COOKIE_JAR" http://127.0.0.1:9200/auth/status >/dev/null
  curl --fail --silent --show-error --max-time 5 \
    -b "$COOKIE_JAR" http://127.0.0.1:9200/network/dhcp/settings >/dev/null
fi

echo "[smoke] live service health and non-invasive runtime checks: OK"
