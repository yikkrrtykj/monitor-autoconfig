#!/bin/sh
# Patch nginx server_name and crond for LibreNMS validation.
# Can be called both as a cont-init.d script (before nginx starts) and standalone.
# When called as cont-init.d/99-librenms-patch, this runs AFTER LibreNMS's own
# cont-init.d scripts that regenerate nginx.conf, so our server_name wins.

# Remove the nginx default site that catches all requests with 404
if [ -f /etc/nginx/http.d/default.conf ]; then
  rm -f /etc/nginx/http.d/default.conf
  echo "[librenms-init] removed nginx default.conf (404 catch-all)"
fi

# Patch server_name in nginx.conf.
# LibreNMS's cont-init.d generates nginx.conf with "server_name localhost;" --
# replace it with the actual server IP so validate.php stops complaining.
if [ -n "${SERVER_IP:-}" ] && [ "$SERVER_IP" != "" ]; then
  # Remove ALL existing server_name lines first, then add ours as the sole entry.
  sed -i "/server_name /d" /etc/nginx/nginx.conf 2>/dev/null || true
  sed -i "/listen \[::\]:8000;/a \        server_name ${SERVER_IP};" /etc/nginx/nginx.conf 2>/dev/null || true
  echo "[librenms-init] nginx server_name set to: ${SERVER_IP}"
fi

# Patch APP_URL in .env so front-end AJAX calls use the real server address.
# .env is created by LibreNMS's cont-init.d, so this must run after those scripts.
if [ -n "${SERVER_IP:-}" ] && [ "$SERVER_IP" != "" ] && [ -f /opt/librenms/.env ]; then
  LIBRENMS_PORT="${LIBRENMS_PORT:-8002}"
  sed -i "s|APP_URL=.*|APP_URL=http://${SERVER_IP}:${LIBRENMS_PORT}|" /opt/librenms/.env 2>/dev/null || true
  echo "[librenms-init] APP_URL patched to http://${SERVER_IP}:${LIBRENMS_PORT}"
fi

# Clear Laravel config cache so PHP picks up the new APP_URL on next request.
if [ -f /opt/librenms/bootstrap/cache/config.php ]; then
  rm -f /opt/librenms/bootstrap/cache/config.php
  echo "[librenms-init] Laravel config cache cleared"
fi

# Reload nginx if it is already running (standalone call), or skip if not yet started
# (cont-init.d call — nginx will read the already-patched config on first start).
if nginx -t >/dev/null 2>&1; then
  nginx -s reload 2>/dev/null && echo "[librenms-init] nginx reloaded" || true
fi

# Restart PHP-FPM so it drops opcache and picks up the new .env.
pkill -USR2 php-fpm 2>/dev/null || pkill php-fpm 2>/dev/null || true

# Install crontab for the Laravel scheduler.
# validate.php checks for this file (belt-and-suspenders alongside the s6 schedule:work service).
if [ -f /opt/librenms/dist/librenms-scheduler.cron ]; then
  mkdir -p /var/spool/cron/crontabs /etc/cron.d
  sed "s|php /opt/librenms/artisan|su librenms -s /bin/sh -c \"php /opt/librenms/artisan\"|" \
    /opt/librenms/dist/librenms-scheduler.cron > /etc/cron.d/librenms 2>/dev/null || true
  chmod 644 /etc/cron.d/librenms 2>/dev/null || true
  echo "[librenms-init] scheduler crontab installed to /etc/cron.d/librenms"
fi

# Start crond so validate.php's cron-based check also passes.
if pgrep crond >/dev/null 2>&1; then
  echo "[librenms-init] crond already running"
elif command -v crond >/dev/null 2>&1; then
  crond -b -l 2 2>/dev/null || crond -l 2 2>/dev/null &
  echo "[librenms-init] crond started"
elif command -v cron >/dev/null 2>&1; then
  cron 2>/dev/null &
  echo "[librenms-init] cron started"
fi
