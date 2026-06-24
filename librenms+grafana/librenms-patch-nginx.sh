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

url_host() {
  printf '%s' "$1" | sed 's#^[a-zA-Z][a-zA-Z0-9+.-]*://##; s#[:/].*##'
}

sed_replacement() {
  printf '%s' "$1" | sed 's/[&|\\]/\\&/g'
}

shell_quote() {
  printf "'"
  printf '%s' "$1" | sed "s/'/'\\\\''/g"
  printf "'"
}

LIBRENMS_PORT="${LIBRENMS_PORT:-8002}"
EFFECTIVE_BASE_URL="${LIBRENMS_BASE_URL:-${APP_URL:-}}"
if [ -z "$EFFECTIVE_BASE_URL" ] && [ -n "${SERVER_IP:-}" ]; then
  EFFECTIVE_BASE_URL="http://${SERVER_IP}:${LIBRENMS_PORT}"
fi
PUBLIC_HOST=""
if [ -n "$EFFECTIVE_BASE_URL" ]; then
  PUBLIC_HOST="$(url_host "$EFFECTIVE_BASE_URL")"
fi
if [ -z "$PUBLIC_HOST" ] || [ "$PUBLIC_HOST" = "localhost" ]; then
  PUBLIC_HOST="${SERVER_IP:-}"
fi

# Patch server_name on the LibreNMS server block so the webserver reports the real
# IP (validate.php compares this against base_url, e.g. "192.168.40.251 localhost").
# Edit the config FILE only; back up first and revert if the result looks wrong.
if [ -n "${PUBLIC_HOST:-}" ] && [ "$PUBLIC_HOST" != "" ]; then
  # Locate the file that holds the block listening on 8000 (fall back to nginx.conf).
  target=$(grep -rls 'listen[^;]*8000' /etc/nginx 2>/dev/null | head -n1)
  [ -z "$target" ] && target=/etc/nginx/nginx.conf
  if [ -f "$target" ]; then
    backup="$target.bak.$$"
    cp "$target" "$backup"
    if grep -q '^[[:space:]]*server_name' "$target"; then
      # Replace the existing server_name value(s) in place -- a 1:1 line rewrite
      # that cannot change the surrounding block structure.
      sed -i "s/^\([[:space:]]*\)server_name[[:space:]].*/\1server_name ${PUBLIC_HOST};/" "$target" 2>/dev/null || true
    else
      # No server_name yet: insert one right after the first listen-on-8000 line.
      awk -v host="$PUBLIC_HOST" '
        { print }
        !added && $0 ~ /listen[^;]*8000/ { print "        server_name " host ";"; added=1 }
      ' "$target" > "$target.tmp" 2>/dev/null && mv "$target.tmp" "$target"
    fi
    # Rewrite any stray "server_name localhost" in OTHER included files too.
    for f in $(grep -rls '^[[:space:]]*server_name[[:space:]]*localhost' /etc/nginx 2>/dev/null); do
      [ "$f" = "$target" ] && continue
      sed -i "s/^\([[:space:]]*\)server_name[[:space:]]*localhost;/\1server_name ${PUBLIC_HOST};/" "$f" 2>/dev/null || true
    done
    # Sanity check via file content only (no `nginx -t`, which would create
    # root-owned log/pid files): our server_name landed and a listen still exists.
    if grep -q "server_name ${PUBLIC_HOST};" "$target" && grep -q 'listen' "$target"; then
      rm -f "$backup"
      echo "[librenms-init] nginx server_name set to ${PUBLIC_HOST} in $target"
    else
      mv "$backup" "$target"
      echo "[librenms-init] WARNING: server_name edit looked wrong -- reverted"
    fi
  fi
fi

# Patch APP_URL in .env so front-end AJAX calls use the real server address.
# .env is created by LibreNMS's cont-init.d, so this must run after those scripts.
if [ -n "${EFFECTIVE_BASE_URL:-}" ] && [ -f /opt/librenms/.env ]; then
  EFFECTIVE_BASE_URL_SED=$(sed_replacement "$EFFECTIVE_BASE_URL")
  sed -i "s|APP_URL=.*|APP_URL=${EFFECTIVE_BASE_URL_SED}|" /opt/librenms/.env 2>/dev/null || true
  echo "[librenms-init] APP_URL patched to ${EFFECTIVE_BASE_URL}"
fi

# Set base_url in LibreNMS's database config table so the /validate ServerName
# check passes. APP_URL in .env is a fallback; the DB config table wins when an
# entry exists (typically written by LibreNMS's own cont-init scripts). Running
# artisan config:set here overwrites any stale "localhost" value in the DB.
# Runs as the librenms user so artisan doesn't create root-owned cache files.
if [ -n "${EFFECTIVE_BASE_URL:-}" ]; then
  EFFECTIVE_BASE_URL_Q=$(shell_quote "$EFFECTIVE_BASE_URL")
  su librenms -s /bin/sh -c \
    "php /opt/librenms/artisan config:set base_url ${EFFECTIVE_BASE_URL_Q}" \
    2>/dev/null || true
  echo "[librenms-init] base_url set in DB to ${EFFECTIVE_BASE_URL}"
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
