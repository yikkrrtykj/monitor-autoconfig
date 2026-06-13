#!/bin/sh
# Patch nginx server_name and the scheduler crontab for LibreNMS validation.
# Runs as the s6 cont-init.d script 99-librenms-patch, i.e. BEFORE the nginx /
# php-fpm services start, and as root. It therefore only edits config FILES --
# it must never run or signal nginx/php-fpm, because those services run as the
# librenms user (uid 1000) and any pid/log/temp files we touch as root would
# become root-owned and block the uid-1000 services from starting.

# Remove the nginx default site that catches all requests with 404.
if [ -f /etc/nginx/http.d/default.conf ]; then
  rm -f /etc/nginx/http.d/default.conf
  echo "[librenms-init] removed nginx default.conf (404 catch-all)"
fi

# Patch server_name on the LibreNMS server block so the webserver reports the real
# IP (validate.php compares this against base_url, e.g. "192.168.40.251 localhost").
# Edit the config FILE only; back up first and revert if the result looks wrong.
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
    # Sanity check via file content only (no `nginx -t`, which would create
    # root-owned log/pid files): our server_name landed and a listen still exists.
    if grep -q "server_name ${SERVER_IP};" "$target" && grep -q 'listen' "$target"; then
      rm -f "$backup"
      echo "[librenms-init] nginx server_name set to ${SERVER_IP} in $target"
    else
      mv "$backup" "$target"
      echo "[librenms-init] WARNING: server_name edit looked wrong -- reverted"
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

# Install the scheduler crontab file. validate.php's cron check looks for this file;
# the image's own cron service runs it, so we only drop the file (no daemon start).
if [ -f /opt/librenms/dist/librenms-scheduler.cron ]; then
  mkdir -p /etc/cron.d
  sed "s|php /opt/librenms/artisan|su librenms -s /bin/sh -c \"php /opt/librenms/artisan\"|" \
    /opt/librenms/dist/librenms-scheduler.cron > /etc/cron.d/librenms 2>/dev/null || true
  chmod 644 /etc/cron.d/librenms 2>/dev/null || true
  echo "[librenms-init] scheduler crontab installed to /etc/cron.d/librenms"
fi

# Always succeed: this runs as an s6 cont-init.d script, and a non-zero exit there
# halts the whole container. None of the steps above are fatal if they fail.
exit 0
