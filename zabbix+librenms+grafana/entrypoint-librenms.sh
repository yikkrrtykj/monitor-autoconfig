#!/bin/sh
if [ -n "${SERVER_IP:-}" ] && [ "$SERVER_IP" != "" ]; then
  find /etc/nginx -name "*.conf" -exec sed -i "s/server_name [^;]*;/server_name ${SERVER_IP};/" {} \; 2>/dev/null || true
fi
su librenms -c 'while true; do php /opt/librenms/artisan schedule:run --no-ansi --no-interaction > /dev/null 2>&1; sleep 60; done' &
exec /init
