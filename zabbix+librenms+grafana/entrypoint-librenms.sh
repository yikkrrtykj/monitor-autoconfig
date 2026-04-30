#!/bin/sh
if [ -n "${SERVER_IP:-}" ] && [ "$SERVER_IP" != "" ]; then
  find /etc/nginx -name "*.conf" -exec sed -i "s/server_name [^;]*;/server_name ${SERVER_IP};/" {} \; 2>/dev/null || true
fi

mkdir -p /etc/services.d/scheduler
cat > /etc/services.d/scheduler/run << 'S6EOF'
#!/bin/sh
exec s6-setuidgid librenms sh -c 'while true; do php /opt/librenms/artisan schedule:run --no-ansi --no-interaction > /dev/null 2>&1; sleep 60; done'
S6EOF
chmod +x /etc/services.d/scheduler/run

exec /init
