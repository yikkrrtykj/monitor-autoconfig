#!/bin/sh
# Remove the nginx default site that returns 404 (happens before /init, survives s6 init)
if [ -f /etc/nginx/http.d/default.conf ]; then
  rm -f /etc/nginx/http.d/default.conf
  echo "[librenms-entry] removed nginx default.conf (404 catch-all)"
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
# Resolve the real server IP/host for the nginx server_name patch. SERVER_IP may be
# unset while the address is only known via LIBRENMS_BASE_URL / APP_URL, so fall back
# to parsing the host out of those. "localhost" is treated as "no real name".
RESOLVED_IP="${SERVER_IP:-}"
[ -z "$RESOLVED_IP" ] && RESOLVED_IP="${LIBRENMS_OWN_HOSTNAME:-}"
if [ -z "$RESOLVED_IP" ] && [ -n "${LIBRENMS_BASE_URL:-}" ]; then
  RESOLVED_IP=$(printf '%s' "$LIBRENMS_BASE_URL" | sed 's#^[a-z]*://##; s#[:/].*##')
fi
if [ -z "$RESOLVED_IP" ] && [ -n "${APP_URL:-}" ]; then
  RESOLVED_IP=$(printf '%s' "$APP_URL" | sed 's#^[a-z]*://##; s#[:/].*##')
fi
[ "$RESOLVED_IP" = "localhost" ] && RESOLVED_IP=""

# The patch script is bind-mounted read-only without an executable bit, so we run it
# with `sh <script>` (no exec bit needed) and force a zero exit -- a non-zero exit from
# an s6 cont-init.d script halts the whole container. We bake the resolved values in
# because s6 cont-init.d scripts do not reliably inherit the container environment.
cat > /etc/cont-init.d/99-librenms-patch <<EOF
#!/bin/sh
export SERVER_IP='${RESOLVED_IP}'
export LIBRENMS_PORT='${LIBRENMS_PORT:-8002}'
sh /librenms-patch-nginx.sh || true
exit 0
EOF
chmod +x /etc/cont-init.d/99-librenms-patch
echo "[librenms-entry] installed nginx/scheduler patch as /etc/cont-init.d/99-librenms-patch (server_name=${RESOLVED_IP:-unset})"

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
