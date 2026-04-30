#!/bin/sh
# Remove the nginx default site that returns 404, otherwise it will block LibreNMS
if [ -f /etc/nginx/http.d/default.conf ]; then
  rm -f /etc/nginx/http.d/default.conf
  echo "[librenms-entry] removed nginx default.conf (404 catch-all)"
fi

# The LibreNMS Docker image's nginx.conf server block has no server_name directive,
# which causes "ServerName is set incorrectly" validation error.
# We must INSERT server_name after the listen directive, not try to replace it.
if [ -n "${SERVER_IP:-}" ] && [ "$SERVER_IP" != "" ]; then
  sed -i "/server_name /d" /etc/nginx/nginx.conf 2>/dev/null || true
  sed -i "/listen \[::\]:8000;/a \        server_name ${SERVER_IP};" /etc/nginx/nginx.conf 2>/dev/null || true
  echo "[librenms-entry] nginx server_name inserted: ${SERVER_IP}"
fi

mkdir -p /etc/services.d/scheduler
cat > /etc/services.d/scheduler/run << 'S6EOF'
#!/bin/sh
exec s6-setuidgid librenms sh -c 'while true; do php /opt/librenms/artisan schedule:run --no-ansi --no-interaction > /dev/null 2>&1; sleep 60; done'
S6EOF
chmod +x /etc/services.d/scheduler/run

# Fix APP_URL in .env before starting
if [ -n "${SERVER_IP:-}" ] && [ "$SERVER_IP" != "" ] && [ -f /opt/librenms/.env ]; then
  LIBRENMS_PORT="${LIBRENMS_PORT:-8002}"
  sed -i "s|APP_URL=.*|APP_URL=http://${SERVER_IP}:${LIBRENMS_PORT}|" /opt/librenms/.env 2>/dev/null || true
  echo "[librenms-entry] APP_URL patched to http://${SERVER_IP}:${LIBRENMS_PORT}"
fi

# Patch RrdCheck.php to comment out progress echo lines that break JSON API responses.
# LibreNMS prints "Scanning X rrd files..." to stdout during web validate,
# which corrupts the JSON response body and causes front-end parse failure.
if [ -f /opt/librenms/LibreNMS/Validations/RrdCheck.php ]; then
  sed -i '/Scanning.*rrd files/s/^\(\s*\)echo/\1\/\/ echo/' /opt/librenms/LibreNMS/Validations/RrdCheck.php 2>/dev/null || true
  sed -i '/echo \$test_status;/s/^\(\s*\)echo/\1\/\/ echo/' /opt/librenms/LibreNMS/Validations/RrdCheck.php 2>/dev/null || true
  sed -i "/Status:.*Complete/s/^\(\s*\)echo/\1\/\/ echo/" /opt/librenms/LibreNMS/Validations/RrdCheck.php 2>/dev/null || true
  sed -i '/echo "\\\\033\[/s/^\(\s*\)echo/\1\/\/ echo/' /opt/librenms/LibreNMS/Validations/RrdCheck.php 2>/dev/null || true
  echo "[librenms-entry] RrdCheck.php echo lines commented out"
fi

exec /init
