#!/bin/sh
# Remove the nginx default site that returns 404 (happens before /init, survives s6 init)
if [ -f /etc/nginx/http.d/default.conf ]; then
  rm -f /etc/nginx/http.d/default.conf
  echo "[librenms-entry] removed nginx default.conf (404 catch-all)"
fi

# Patch RrdCheck.php to suppress progress echo lines that corrupt JSON API responses.
# Applied to source code before s6 starts so it survives cont-init.d regeneration.
if [ -f /opt/librenms/LibreNMS/Validations/RrdCheck.php ]; then
  sed -i "55s/echo/\/\/ echo/" /opt/librenms/LibreNMS/Validations/RrdCheck.php 2>/dev/null || true
  sed -i "67s/echo/\/\/ echo/" /opt/librenms/LibreNMS/Validations/RrdCheck.php 2>/dev/null || true
  sed -i "69s/echo/\/\/ echo/" /opt/librenms/LibreNMS/Validations/RrdCheck.php 2>/dev/null || true
  sed -i "75s/echo/\/\/ echo/" /opt/librenms/LibreNMS/Validations/RrdCheck.php 2>/dev/null || true
  sed -i "81s/echo/\/\/ echo/" /opt/librenms/LibreNMS/Validations/RrdCheck.php 2>/dev/null || true
  sed -i "82s/echo/\/\/ echo/" /opt/librenms/LibreNMS/Validations/RrdCheck.php 2>/dev/null || true
  echo "[librenms-entry] RrdCheck.php echo lines commented out"
fi

# Install the nginx/env/cron patch as a cont-init.d script so it runs AFTER LibreNMS's
# own cont-init.d scripts finish generating nginx.conf and .env. The "99-" prefix
# ensures alphabetical order places us last, so our server_name patch is not overwritten.
mkdir -p /etc/cont-init.d
cp /librenms-patch-nginx.sh /etc/cont-init.d/99-librenms-patch
chmod +x /etc/cont-init.d/99-librenms-patch
echo "[librenms-entry] installed nginx/scheduler patch as /etc/cont-init.d/99-librenms-patch"

# Register the Laravel scheduler as an s6 long-running service using schedule:work.
# schedule:work keeps the artisan process alive in the process table, which is
# what LibreNMS's validate.php looks for when checking "Scheduler is running".
# (The old schedule:run loop only appeared in ps aux during its 60-second window.)
mkdir -p /etc/services.d/scheduler
cat > /etc/services.d/scheduler/run << 'S6EOF'
#!/bin/sh
exec s6-setuidgid librenms php /opt/librenms/artisan schedule:work --no-ansi --no-interaction
S6EOF
chmod +x /etc/services.d/scheduler/run
echo "[librenms-entry] scheduler s6 service registered (schedule:work)"

exec /init
