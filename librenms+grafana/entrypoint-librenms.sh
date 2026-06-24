#!/bin/sh
# Remove the nginx default site that returns 404 (happens before /init, survives s6 init)
if [ -f /etc/nginx/http.d/default.conf ]; then
  rm -f /etc/nginx/http.d/default.conf
  echo "[librenms-entry] removed nginx default.conf (404 catch-all)"
fi

# LibreNMS device pages render graphs through rrdtool. Some base images emit a
# harmless Fontconfig warning on the first graph render; LibreNMS treats any
# stderr as "Error Drawing Graph". Warm the font cache and filter only that
# exact warning while preserving real rrdtool errors.
export FONTCONFIG_PATH="${FONTCONFIG_PATH:-/etc/fonts}"
export FONTCONFIG_FILE="${FONTCONFIG_FILE:-/etc/fonts/fonts.conf}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-/tmp}"
mkdir -p "$XDG_CACHE_HOME" 2>/dev/null || true
if command -v fc-cache >/dev/null 2>&1; then
  fc-cache -f >/dev/null 2>&1 || true
  echo "[librenms-entry] fontconfig cache warmed"
fi
if [ -x /usr/bin/rrdtool ] && [ ! -x /usr/bin/rrdtool.real ]; then
  mv /usr/bin/rrdtool /usr/bin/rrdtool.real
  cat > /usr/bin/rrdtool <<'EOF'
#!/bin/sh
err_file=$(mktemp)
trap 'rm -f "$err_file"' EXIT
/usr/bin/rrdtool.real "$@" 2>"$err_file"
status=$?
grep -v 'Fontconfig warning: using without calling FcInit()' "$err_file" >&2 || true
exit "$status"
EOF
  chmod +x /usr/bin/rrdtool
  /usr/bin/rrdtool graph /tmp/librenms-font-warmup.png \
    --start now-60 --end now --width 10 --height 10 HRULE:0#000000 >/dev/null 2>&1 || true
  rm -f /tmp/librenms-font-warmup.png 2>/dev/null || true
  echo "[librenms-entry] rrdtool fontconfig warning filter installed"
fi

# Patch RrdCheck.php to suppress progress echo lines that corrupt JSON API responses.
# Applied to source code before s6 starts so it survives cont-init.d regeneration.
if [ -f /opt/librenms/LibreNMS/Validations/RrdCheck.php ]; then
  sed -i "55s/echo/\/\/ echo/" /opt/librenms/LibreNMS/Validations/RrdCheck.php 2>/dev/null || true
  sed -i "67s/echo/\/\/ echo/" /opt/librenms/LibreNMS/Validations/RrdCheck.php 2>/dev/null || true
  sed -i "69s/echo/\/\/ echo/" /opt/librenms/LibreNMS/Validations/RrdCheck.php 2>/dev/null || true
  sed -i "75s/echo/\/\/ echo/" /opt/librenms/LibreNMS/Validations/RrdCheck.php 2>/dev/null || true
  sed -i "81s/echo/\/\/ echo/" /opt/librenms/LibreNMS/Validations/RrdCheck.php 2>/dev/null || true
  sed -i "82s/echo/\/\/ echo/" /opt/librenms/LibreNMS/Validations/RrdCheck.php 2>/dev/null || true
  echo "[librenms-entry] RrdCheck.php echo lines commented out"
fi

# Install the nginx/env/cron patch as a cont-init.d script so it runs AFTER LibreNMS's
# own cont-init.d scripts finish generating nginx.conf and .env. The "99-" prefix
# ensures alphabetical order places us last, so our server_name patch is not overwritten.
#
# IMPORTANT: s6 cont-init.d scripts do not reliably inherit the container's environment,
# so SERVER_IP/LIBRENMS_PORT would be empty there and the patch would silently skip.
# We bake the actual values into the generated wrapper here (entrypoint has the real
# env) and let the wrapper export them before running the patch logic.
mkdir -p /etc/cont-init.d

url_host() {
  printf '%s' "$1" | sed 's#^[a-zA-Z][a-zA-Z0-9+.-]*://##; s#[:/].*##'
}

normalize_base_url() {
  raw=$(printf '%s' "${1:-}" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
  [ -z "$raw" ] && return 0
  case "$raw" in
    *'${'*|*'}'*) return 0 ;;
    http://*|https://*) printf '%s' "$raw" ;;
    *) printf 'http://%s' "$raw" ;;
  esac
}

LIBRENMS_FORCE_BASE_URL="${LIBRENMS_FORCE_BASE_URL:-false}"
NORMALIZED_LIBRENMS_BASE_URL=$(normalize_base_url "${LIBRENMS_BASE_URL:-}")
NORMALIZED_APP_URL=$(normalize_base_url "${APP_URL:-}")
if [ "$LIBRENMS_FORCE_BASE_URL" = "true" ] && [ -n "$NORMALIZED_LIBRENMS_BASE_URL" ]; then
  LIBRENMS_BASE_URL="$NORMALIZED_LIBRENMS_BASE_URL"
  APP_URL="$NORMALIZED_LIBRENMS_BASE_URL"
elif [ -n "$NORMALIZED_APP_URL" ] && [ "$(url_host "$NORMALIZED_APP_URL")" != "localhost" ]; then
  APP_URL="$NORMALIZED_APP_URL"
else
  APP_URL="http://localhost:${LIBRENMS_PORT:-8002}"
fi
export LIBRENMS_BASE_URL APP_URL LIBRENMS_FORCE_BASE_URL

# Resolve the public host for nginx server_name. Prefer the explicit external
# LibreNMS URL when present; SERVER_IP is only the fallback for LAN-only installs.
RESOLVED_IP=""
if [ "$LIBRENMS_FORCE_BASE_URL" = "true" ] && [ -n "${LIBRENMS_BASE_URL:-}" ]; then
  RESOLVED_IP=$(url_host "$LIBRENMS_BASE_URL")
fi
if [ -z "$RESOLVED_IP" ] && [ -n "${APP_URL:-}" ]; then
  RESOLVED_IP=$(url_host "$APP_URL")
fi
[ -z "$RESOLVED_IP" ] && RESOLVED_IP="${SERVER_IP:-}"
[ -z "$RESOLVED_IP" ] && RESOLVED_IP="${LIBRENMS_OWN_HOSTNAME:-}"
[ "$RESOLVED_IP" = "localhost" ] && RESOLVED_IP=""

shell_quote() {
  printf "'"
  printf '%s' "$1" | sed "s/'/'\\\\''/g"
  printf "'"
}

Q_SERVER_IP=$(shell_quote "$RESOLVED_IP")
Q_LIBRENMS_PORT=$(shell_quote "${LIBRENMS_PORT:-8002}")
Q_LIBRENMS_BASE_URL=$(shell_quote "${LIBRENMS_BASE_URL:-}")
Q_APP_URL=$(shell_quote "${APP_URL:-}")
Q_LIBRENMS_FORCE_BASE_URL=$(shell_quote "${LIBRENMS_FORCE_BASE_URL:-false}")

# The patch script is bind-mounted read-only without an executable bit, so we run it
# with `sh <script>` (no exec bit needed) and force a zero exit -- a non-zero exit from
# an s6 cont-init.d script halts the whole container. We bake the resolved values in
# because s6 cont-init.d scripts do not reliably inherit the container environment.
cat > /etc/cont-init.d/99-librenms-patch <<EOF
#!/bin/sh
export SERVER_IP=${Q_SERVER_IP}
export LIBRENMS_PORT=${Q_LIBRENMS_PORT}
export LIBRENMS_BASE_URL=${Q_LIBRENMS_BASE_URL}
export LIBRENMS_FORCE_BASE_URL=${Q_LIBRENMS_FORCE_BASE_URL}
export APP_URL=${Q_APP_URL}
sh /librenms-patch-nginx.sh || true
exit 0
EOF
chmod +x /etc/cont-init.d/99-librenms-patch
echo "[librenms-entry] installed nginx/scheduler patch as /etc/cont-init.d/99-librenms-patch (server_name=${RESOLVED_IP:-unset})"

sql_quote() {
  printf '%s' "$1" | sed "s/'/''/g"
}

# If a previous deploy wrote a malformed base_url into the DB, LibreNMS can crash
# during its own schema update before our cont-init patch gets a chance to run.
# Best-effort repair it before handing control to /init.
if [ "$LIBRENMS_FORCE_BASE_URL" = "true" ]; then
  PREINIT_BASE_URL="${LIBRENMS_BASE_URL:-}"
else
  PREINIT_BASE_URL=""
fi
if [ "$LIBRENMS_FORCE_BASE_URL" = "true" ] && [ -z "$PREINIT_BASE_URL" ]; then
  PREINIT_BASE_URL="${APP_URL:-}"
fi
if [ -n "$PREINIT_BASE_URL" ] && [ "$(url_host "$PREINIT_BASE_URL")" = "localhost" ]; then
  PREINIT_BASE_URL=""
fi
if [ "$LIBRENMS_FORCE_BASE_URL" = "true" ] && [ -z "$PREINIT_BASE_URL" ] && [ -n "${SERVER_IP:-}" ]; then
  PREINIT_BASE_URL="http://${SERVER_IP}:${LIBRENMS_PORT:-8002}"
fi
if [ "$LIBRENMS_FORCE_BASE_URL" = "true" ] && [ -z "$PREINIT_BASE_URL" ]; then
  PREINIT_BASE_URL="http://localhost:${LIBRENMS_PORT:-8002}"
fi
[ -z "${APP_URL:-}" ] && APP_URL="http://localhost:${LIBRENMS_PORT:-8002}"
export APP_URL

MYSQL_BIN=""
if command -v mariadb >/dev/null 2>&1; then
  MYSQL_BIN="mariadb"
elif command -v mysql >/dev/null 2>&1; then
  MYSQL_BIN="mysql"
fi
if [ -n "$MYSQL_BIN" ] && [ -n "${DB_HOST:-}" ]; then
  PREINIT_BASE_URL_SQL=$(sql_quote "$PREINIT_BASE_URL")
  if [ "$LIBRENMS_FORCE_BASE_URL" = "true" ]; then
    PREINIT_SQL="UPDATE config SET config_value='${PREINIT_BASE_URL_SQL}' WHERE config_name='base_url';"
  else
    PREINIT_SQL="DELETE FROM config WHERE config_name='base_url';"
  fi
  MYSQL_PWD="${DB_PASS:-}" "$MYSQL_BIN" -h "${DB_HOST}" -P "${DB_PORT:-3306}" \
    -u "${DB_USER:-librenms}" "${DB_NAME:-librenms}" -e "$PREINIT_SQL" >/dev/null 2>&1 && \
    echo "[librenms-entry] pre-init base_url force=${LIBRENMS_FORCE_BASE_URL}" || true
fi
if command -v php >/dev/null 2>&1 && [ -n "${DB_HOST:-}" ]; then
  export PREINIT_BASE_URL
  export LIBRENMS_FORCE_BASE_URL
  cat > /tmp/librenms-repair-base-url.php <<'PHPEOF'
<?php
$url = getenv('PREINIT_BASE_URL') ?: '';
$force = (getenv('LIBRENMS_FORCE_BASE_URL') ?: 'false') === 'true';
$host = getenv('DB_HOST') ?: '';
$port = getenv('DB_PORT') ?: '3306';
$db = getenv('DB_NAME') ?: 'librenms';
$user = getenv('DB_USER') ?: 'librenms';
$pass = getenv('DB_PASS') ?: '';

if (($force && $url === '') || $host === '') {
    exit(2);
}

try {
    $dsn = "mysql:host={$host};port={$port};dbname={$db};charset=utf8mb4";
    $pdo = new PDO($dsn, $user, $pass, array(PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION));
    $check = $pdo->prepare("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = ? AND table_name = 'config'");
    $check->execute(array($db));
    if ((int) $check->fetchColumn() < 1) {
        exit(3);
    }
    if ($force) {
        $stmt = $pdo->prepare("UPDATE config SET config_value = ? WHERE config_name = 'base_url'");
        $stmt->execute(array($url));
    } else {
        $pdo->exec("DELETE FROM config WHERE config_name = 'base_url'");
    }
} catch (Throwable $e) {
    fwrite(STDERR, $e->getMessage() . "\n");
    exit(1);
}
PHPEOF
  repaired_base_url=""
  for attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    if php /tmp/librenms-repair-base-url.php >/dev/null 2>&1; then
      repaired_base_url=1
      break
    fi
    sleep 2
  done
  if [ "$repaired_base_url" = "1" ]; then
    if [ "$LIBRENMS_FORCE_BASE_URL" = "true" ]; then
      echo "[librenms-entry] pre-init base_url repaired through PHP to ${PREINIT_BASE_URL}"
    else
      echo "[librenms-entry] pre-init base_url cleared through PHP for dual-host access"
    fi
  else
    echo "[librenms-entry] WARNING: pre-init base_url repair through PHP did not complete"
  fi
  rm -f /tmp/librenms-repair-base-url.php 2>/dev/null || true
fi

# Register the Laravel scheduler as an s6 long-running service using schedule:work.
# schedule:work keeps the artisan process alive in the process table, which is
# what LibreNMS's validate.php looks for when checking "Scheduler is running".
# (The old schedule:run loop only appeared in ps aux during its 60-second window.)
mkdir -p /etc/services.d/scheduler
cat > /etc/services.d/scheduler/run << 'S6EOF'
#!/bin/sh
exec s6-setuidgid librenms php /opt/librenms/artisan schedule:work --no-ansi --no-interaction
S6EOF
chmod +x /etc/services.d/scheduler/run
echo "[librenms-entry] scheduler s6 service registered (schedule:work)"

exec /init
