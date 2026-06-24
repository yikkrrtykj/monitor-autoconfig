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
# Resolve the public host for nginx server_name. Prefer the explicit external
# LibreNMS URL when present; SERVER_IP is only the fallback for LAN-only installs.
RESOLVED_IP=""
if [ -n "${LIBRENMS_BASE_URL:-}" ]; then
  RESOLVED_IP=$(printf '%s' "$LIBRENMS_BASE_URL" | sed 's#^[a-zA-Z][a-zA-Z0-9+.-]*://##; s#[:/].*##')
fi
if [ -z "$RESOLVED_IP" ] && [ -n "${APP_URL:-}" ]; then
  RESOLVED_IP=$(printf '%s' "$APP_URL" | sed 's#^[a-zA-Z][a-zA-Z0-9+.-]*://##; s#[:/].*##')
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

# The patch script is bind-mounted read-only without an executable bit, so we run it
# with `sh <script>` (no exec bit needed) and force a zero exit -- a non-zero exit from
# an s6 cont-init.d script halts the whole container. We bake the resolved values in
# because s6 cont-init.d scripts do not reliably inherit the container environment.
cat > /etc/cont-init.d/99-librenms-patch <<EOF
#!/bin/sh
export SERVER_IP=${Q_SERVER_IP}
export LIBRENMS_PORT=${Q_LIBRENMS_PORT}
export LIBRENMS_BASE_URL=${Q_LIBRENMS_BASE_URL}
export APP_URL=${Q_APP_URL}
sh /librenms-patch-nginx.sh || true
exit 0
EOF
chmod +x /etc/cont-init.d/99-librenms-patch
echo "[librenms-entry] installed nginx/scheduler patch as /etc/cont-init.d/99-librenms-patch (server_name=${RESOLVED_IP:-unset})"

valid_base_url() {
  case "$1" in
    http://*|https://*) ;;
    *) return 1 ;;
  esac
  case "$1" in
    *'${'*|*'}'*) return 1 ;;
  esac
  return 0
}

sql_quote() {
  printf '%s' "$1" | sed "s/'/''/g"
}

# If a previous deploy wrote a malformed base_url into the DB, LibreNMS can crash
# during its own schema update before our cont-init patch gets a chance to run.
# Best-effort repair it before handing control to /init.
PREINIT_BASE_URL="${LIBRENMS_BASE_URL:-}"
if ! valid_base_url "$PREINIT_BASE_URL"; then
  PREINIT_BASE_URL=""
fi
if [ -z "$PREINIT_BASE_URL" ] && [ -n "${SERVER_IP:-}" ]; then
  PREINIT_BASE_URL="http://${SERVER_IP}:${LIBRENMS_PORT:-8002}"
fi
if ! valid_base_url "$PREINIT_BASE_URL"; then
  PREINIT_BASE_URL="http://localhost:${LIBRENMS_PORT:-8002}"
fi

MYSQL_BIN=""
if command -v mariadb >/dev/null 2>&1; then
  MYSQL_BIN="mariadb"
elif command -v mysql >/dev/null 2>&1; then
  MYSQL_BIN="mysql"
fi
if [ -n "$MYSQL_BIN" ] && [ -n "${DB_HOST:-}" ]; then
  PREINIT_BASE_URL_SQL=$(sql_quote "$PREINIT_BASE_URL")
  MYSQL_PWD="${DB_PASS:-}" "$MYSQL_BIN" \
    -h "${DB_HOST}" \
    -P "${DB_PORT:-3306}" \
    -u "${DB_USER:-librenms}" \
    "${DB_NAME:-librenms}" \
    -e "UPDATE config SET config_value='${PREINIT_BASE_URL_SQL}' WHERE config_name='base_url';" \
    >/dev/null 2>&1 && \
    echo "[librenms-entry] pre-init base_url repaired to ${PREINIT_BASE_URL}" || true
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
