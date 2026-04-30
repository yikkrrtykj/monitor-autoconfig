#!/bin/sh
mkdir -p /etc/crontabs
cat > /etc/crontabs/librenms << 'CRONEOF'
* * * * * php /opt/librenms/artisan schedule:run --no-ansi --no-interaction > /dev/null 2>&1
CRONEOF
chmod 644 /etc/crontabs/librenms 2>/dev/null || true
crond -b -l 2 2>/dev/null || true
if [ -n "${SERVER_IP:-}" ] && [ "$SERVER_IP" != "" ]; then
  find /etc/nginx -name "*.conf" -exec sed -i "s/server_name [^;]*;/server_name ${SERVER_IP};/" {} \; 2>/dev/null || true
fi
exec /init
