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

# Patch server_name on the LibreNMS server block so the webserver reports the real
# IP (validate.php compares this against base_url, e.g. "192.168.40.251 localhost").
# Done defensively: back up the file, edit it, then `nginx -t`; if the edit produced
# an invalid config we revert, so the site always comes up even if the layout differs.
if [ -n "${SERVER_IP:-}" ] && [ "$SERVER_IP" != "" ]; then
  # Locate the file that holds the block listening on 8000 (fall back to nginx.conf).
  target=$(grep -rls 'listen[^;]*8000' /etc/nginx 2>/dev/null | head -n1)
  [ -z "$target" ] && target=/etc/nginx/nginx.conf
  if [ -f "$target" ]; then
    backup="$target.bak.$$"
    cp "$target" "$backup"
    if grep -q '^[[:space:]]*server_name' "$target"; then
      # Replace the existing server_name value(s) in place -- a 1:1 line rewrite
      # that cannot change the surrounding block structure.
      sed -i "s/^\([[:space:]]*\)server_name[[:space:]].*/\1server_name ${SERVER_IP};/" "$target" 2>/dev/null || true
    else
      # No server_name yet: insert one right after the first listen-on-8000 line.
      awk -v ip="$SERVER_IP" '
        { print }
        !added && $0 ~ /listen[^;]*8000/ { print "        server_name " ip ";"; added=1 }
      ' "$target" > "$target.tmp" 2>/dev/null && mv "$target.tmp" "$target"
    fi
    # Rewrite any stray "server_name localhost" in OTHER included files too.
    for f in $(grep -rls '^[[:space:]]*server_name[[:space:]]*localhost' /etc/nginx 2>/dev/null); do
      [ "$f" = "$target" ] && continue
      sed -i "s/^\([[:space:]]*\)server_name[[:space:]]*localhost;/\1server_name ${SERVER_IP};/" "$f" 2>/dev/null || true
    done
    if nginx -t >/dev/null 2>&1; then
      rm -f "$backup"
      echo "[librenms-init] nginx server_name set to ${SERVER_IP} in $target"
    else
      mv "$backup" "$target"
      echo "[librenms-init] WARNING: server_name edit produced invalid nginx config -- reverted"
    fi
  fi
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

# Always succeed: this runs as an s6 cont-init.d script, and a non-zero exit there
# halts the whole container. None of the steps above are fatal if they fail.
exit 0
