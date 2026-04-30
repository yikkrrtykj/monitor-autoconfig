#!/bin/sh
# Patch nginx server_name to fix LibreNMS "ServerName is set incorrectly" validation
# Also remove the default.conf that returns 404 to prevent it from blocking LibreNMS
# Also install scheduler cron and start cron daemon

# Remove the nginx default site that returns 404, otherwise it will block LibreNMS
# because it listens on default_server and the LibreNMS site config is not default_server
if [ -f /etc/nginx/http.d/default.conf ]; then
  rm -f /etc/nginx/http.d/default.conf
  echo "[librenms-init] removed nginx default.conf (404 catch-all)"
fi

# The LibreNMS Docker image's nginx.conf server block has no server_name directive,
# which causes "ServerName is set incorrectly" validation error.
# We must INSERT server_name after the listen directive, not try to replace it.
if [ -n "${SERVER_IP:-}" ] && [ "$SERVER_IP" != "" ]; then
  # Remove any existing server_name to avoid duplicates
  sed -i "/server_name /d" /etc/nginx/nginx.conf 2>/dev/null || true
  # Insert server_name after the IPv6 listen line
  sed -i "/listen \[::\]:8000;/a \        server_name ${SERVER_IP};" /etc/nginx/nginx.conf 2>/dev/null || true
  echo "[librenms-init] nginx server_name inserted: ${SERVER_IP}"
fi

# Fix APP_URL in .env - otherwise front-end AJAX calls fail because it points to localhost
if [ -n "${SERVER_IP:-}" ] && [ "$SERVER_IP" != "" ] && [ -f /opt/librenms/.env ]; then
  LIBRENMS_PORT="${LIBRENMS_PORT:-8002}"
  sed -i "s|APP_URL=.*|APP_URL=http://${SERVER_IP}:${LIBRENMS_PORT}|" /opt/librenms/.env 2>/dev/null || true
  echo "[librenms-init] APP_URL patched to http://${SERVER_IP}:${LIBRENMS_PORT}"
fi

# Delete Laravel config cache so PHP re-reads .env on next request.
# If we don't do this, config:cache keeps serving the old APP_URL=localhost
# even after .env was modified.
if [ -f /opt/librenms/bootstrap/cache/config.php ]; then
  rm -f /opt/librenms/bootstrap/cache/config.php
  echo "[librenms-init] Laravel config cache removed"
fi

# Reload nginx to apply changes
if nginx -t >/dev/null 2>&1; then
  nginx -s reload 2>/dev/null || true
  echo "[librenms-init] nginx reloaded"
else
  echo "[librenms-init] WARNING: nginx config test failed"
fi

# Restart PHP-FPM so it drops opcache and reads the new .env
pkill -USR2 php-fpm 2>/dev/null || pkill php-fpm 2>/dev/null || true
echo "[librenms-init] PHP-FPM restarted"

# Patch RrdCheck.php to comment out progress echo lines that break JSON API responses.
# LibreNMS 26.4.1 prints "Scanning X rrd files..." and "Status: X/Y" to stdout
# during the web validate check, which corrupts the JSON response body.
# We use line-number sed because regex fails on shell-escaping \033[\ etc.
if [ -f /opt/librenms/LibreNMS/Validations/RrdCheck.php ]; then
  sed -i "55s/echo/\/\/ echo/" /opt/librenms/LibreNMS/Validations/RrdCheck.php 2>/dev/null || true
  sed -i "67s/echo/\/\/ echo/" /opt/librenms/LibreNMS/Validations/RrdCheck.php 2>/dev/null || true
  sed -i "69s/echo/\/\/ echo/" /opt/librenms/LibreNMS/Validations/RrdCheck.php 2>/dev/null || true
  sed -i "75s/echo/\/\/ echo/" /opt/librenms/LibreNMS/Validations/RrdCheck.php 2>/dev/null || true
  sed -i "81s/echo/\/\/ echo/" /opt/librenms/LibreNMS/Validations/RrdCheck.php 2>/dev/null || true
  sed -i "82s/echo/\/\/ echo/" /opt/librenms/LibreNMS/Validations/RrdCheck.php 2>/dev/null || true
  echo "[librenms-init] RrdCheck.php echo lines commented out"
fi

if [ -f /opt/librenms/dist/librenms-scheduler.cron ]; then
  mkdir -p /var/spool/cron/crontabs
  sed "s|php /opt/librenms/artisan|su librenms -s /bin/sh -c \"php /opt/librenms/artisan\"|" \
    /opt/librenms/dist/librenms-scheduler.cron > /var/spool/cron/crontabs/root
  chmod 600 /var/spool/cron/crontabs/root 2>/dev/null || true
  echo "[librenms-init] scheduler cron installed"
fi

if [ -S /var/run/cron.sock ] || pgrep crond >/dev/null 2>&1; then
  echo "[librenms-init] cron daemon already running"
elif command -v crond >/dev/null 2>&1; then
  crond -b -l 2 2>/dev/null || crond -l 2 2>/dev/null &
  echo "[librenms-init] crond started"
elif command -v cron >/dev/null 2>&1; then
  cron 2>/dev/null &
  echo "[librenms-init] cron started"
fi
