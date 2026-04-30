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
  cp /opt/librenms/dist/librenms-scheduler.cron /etc/cron.d/librenms-scheduler 2>/dev/null
  echo "[librenms-init] scheduler cron installed"
fi

if command -v cron >/dev/null 2>&1; then
  cron 2>/dev/null &
  echo "[librenms-init] cron started (cron)"
elif command -v crond >/dev/null 2>&1; then
  crond -b 2>/dev/null || crond 2>/dev/null &
  echo "[librenms-init] cron started (crond)"
fi
