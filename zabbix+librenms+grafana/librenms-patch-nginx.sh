#!/bin/sh
# Patch nginx server_name to fix LibreNMS "ServerName is set incorrectly" validation
# Also install scheduler cron and start cron daemon

if [ -n "${SERVER_IP:-}" ] && [ "$SERVER_IP" != "" ]; then
  for conf in $(find /etc/nginx -name "*.conf" 2>/dev/null); do
    sed -i "s/server_name [^;]*;/server_name ${SERVER_IP};/" "$conf" 2>/dev/null || true
  done
  echo "[librenms-init] nginx server_name patched to ${SERVER_IP}"
fi

if [ -f /opt/librenms/dist/librenms-scheduler.cron ]; then
  mkdir -p /var/spool/cron/crontabs
  sed "s|php /opt/librenms/artisan|sudo -u librenms php /opt/librenms/artisan|" \
    /opt/librenms/dist/librenms-scheduler.cron > /var/spool/cron/crontabs/root
  chmod 600 /var/spool/cron/crontabs/root 2>/dev/null || true
  echo "[librenms-init] scheduler cron installed (root crontab with sudo -u librenms)"
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
