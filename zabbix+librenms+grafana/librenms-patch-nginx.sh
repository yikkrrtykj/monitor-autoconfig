#!/bin/sh
# Patch nginx server_name to fix LibreNMS "ServerName is set incorrectly" validation
if [ -n "${SERVER_IP:-}" ] && [ "$SERVER_IP" != "" ]; then
  for conf in /etc/nginx/http.d/default.conf /etc/nginx/conf.d/default.conf /etc/nginx/sites-enabled/default; do
    if [ -f "$conf" ]; then
      sed -i "s/server_name [^;]*;/server_name ${SERVER_IP};/" "$conf" 2>/dev/null || true
    fi
  done
fi
