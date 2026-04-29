#!/bin/sh
# LibreNMS Auto-Configuration Script
# 扫描 SNMP 设备并自动添加到 LibreNMS

set -e

LIBRENMS_URL="${LIBRENMS_URL:-http://librenms:8000}"
SNMP_COMMUNITY="${LIBRENMS_SNMP_COMMUNITY:-${SNMP_COMMUNITY:-global}}"
SNMP_VERSION="${SNMP_VERSION:-v2c}"
SNMP_TIMEOUT="${SNMP_TIMEOUT:-1}"
SNMP_RETRIES="${SNMP_RETRIES:-0}"
CORE_IP="${LIBRENMS_CORE_IP:-192.168.10.254}"
STAGE_START_OCTET="${LIBRENMS_STAGE_START_OCTET:-11}"
DISCOVERY_TARGETS="${LIBRENMS_DISCOVERY_TARGETS:-192.168.10.1-100,192.168.10.254}"
LIBRENMS_ADMIN_USER="${LIBRENMS_ADMIN_USER:-admin}"
LIBRENMS_ADMIN_PASSWORD="${LIBRENMS_ADMIN_PASSWORD:-admin}"
LIBRENMS_ADMIN_EMAIL="${LIBRENMS_ADMIN_EMAIL:-admin@example.com}"

echo "============================================"
echo "  LibreNMS Auto-Discovery Configuration"
echo "============================================"
echo ""

# Wait for LibreNMS to be ready
echo "[1/5] Waiting for LibreNMS to be ready..."
for i in $(seq 1 90); do
  if curl -s -f "$LIBRENMS_URL/" > /dev/null 2>&1; then
    echo "  LibreNMS is ready!"
    break
  fi
  if [ "$i" -eq 90 ]; then
    echo "  ERROR: LibreNMS did not start in time"
    exit 1
  fi
  echo "  Waiting... ($i/90)"
  sleep 10
done

ensure_admin_user() {
  if [ -x /opt/librenms/lnms ]; then
    lnms_cmd="/opt/librenms/lnms"
  elif command -v lnms > /dev/null 2>&1; then
    lnms_cmd=$(command -v lnms)
  else
    echo "  WARNING: lnms command not found, skipping admin user creation."
    return 0
  fi

  for i in $(seq 1 20); do
    output=$("$lnms_cmd" user:add \
      --password="$LIBRENMS_ADMIN_PASSWORD" \
      --role=admin \
      --email="$LIBRENMS_ADMIN_EMAIL" \
      --no-interaction \
      "$LIBRENMS_ADMIN_USER" 2>&1) && {
        echo "  Admin user '$LIBRENMS_ADMIN_USER' is ready."
        return 0
      }

    if echo "$output" | grep -qi "already"; then
      echo "  Admin user '$LIBRENMS_ADMIN_USER' already exists."
      return 0
    fi

    echo "  Waiting for LibreNMS database initialization... ($i/20)"
    sleep 10
  done

  echo "  WARNING: Could not create admin user automatically."
  echo "  Last output: $output"
  return 0
}

echo ""
echo "[2/5] Ensuring LibreNMS admin user..."
ensure_admin_user

# Create API token
echo ""
echo "[3/5] Creating API token..."
API_TOKEN=$(curl -s -X POST "$LIBRENMS_URL/api/v0/auth/legacy-token" \
  -H "Content-Type: application/json" \
  -d "{\"username\":\"$LIBRENMS_ADMIN_USER\",\"password\":\"$LIBRENMS_ADMIN_PASSWORD\"}" 2>/dev/null | \
  python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null || true)

if [ -z "$API_TOKEN" ]; then
  echo "  WARNING: Could not create API token automatically."
  echo "  Attempting CLI fallback for device addition..."
else
  echo "  API Token created successfully"
fi

expand_targets() {
  old_ifs=$IFS
  IFS=','
  for target in $DISCOVERY_TARGETS; do
    IFS=$old_ifs
    target=$(echo "$target" | tr -d '[:space:]')
    [ -z "$target" ] && continue

    case "$target" in
      *-*)
        start_ip=${target%-*}
        end_part=${target#*-}
        prefix=${start_ip%.*}
        start_octet=${start_ip##*.}
        end_octet=${end_part##*.}

        for octet in $(seq "$start_octet" "$end_octet"); do
          echo "$prefix.$octet"
        done
        ;;
      *)
        echo "$target"
        ;;
    esac
    IFS=','
  done
  IFS=$old_ifs
}

device_name() {
  ip=$1
  if [ "$ip" = "$CORE_IP" ]; then
    echo "Core"
    return
  fi

  last_octet=${ip##*.}
  if [ "$last_octet" -ge "$STAGE_START_OCTET" ] 2>/dev/null; then
    stage_no=$((last_octet - STAGE_START_OCTET + 1))
    echo "Stage$stage_no"
  else
    echo "Device-$ip"
  fi
}

snmp_reachable() {
  ip=$1

  if ! command -v snmpget > /dev/null 2>&1; then
    echo "  snmpget not found, adding configured target without probe: $ip"
    return 0
  fi

  snmpget -v2c -c "$SNMP_COMMUNITY" -t "$SNMP_TIMEOUT" -r "$SNMP_RETRIES" \
    "$ip" 1.3.6.1.2.1.1.1.0 > /dev/null 2>&1
}

add_device_api() {
  name=$1
  ip=$2
  community=$3

  [ -z "$API_TOKEN" ] && return 1

  result=$(curl -s -X POST "$LIBRENMS_URL/api/v0/devices" \
    -H "X-Auth-Token: $API_TOKEN" \
    -H "Content-Type: application/json" \
    -d "{
      \"hostname\": \"$ip\",
      \"display_name\": \"$name\",
      \"version\": \"$SNMP_VERSION\",
      \"community\": \"$community\",
      \"port\": 161,
      \"transport\": \"udp\",
      \"poller_group\": 0,
      \"disabled\": false
    }" 2>/dev/null)

  msg=$(echo "$result" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('message', d.get('error', 'unknown')))" 2>/dev/null || echo "parse error")
  status=$(echo "$result" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status', ''))" 2>/dev/null || true)
  echo "  $name ($ip): $msg"

  [ "$status" = "ok" ]
}

add_device_cli() {
  name=$1
  ip=$2
  community=$3

  php /opt/librenms/addhost.php \
    "$ip" "$SNMP_VERSION" "$community" 2>/dev/null && \
    echo "  $name ($ip): Added via CLI" || \
    echo "  $name ($ip): Already exists or failed"
}

echo ""
echo "[4/5] Discovering SNMP devices..."
echo "  Targets: $DISCOVERY_TARGETS"
echo "  SNMP Community: $SNMP_COMMUNITY"
echo "  SNMP Probe: timeout ${SNMP_TIMEOUT}s, retries $SNMP_RETRIES"
echo ""

expand_targets | while read -r ip; do
  [ -z "$ip" ] && continue
  name=$(device_name "$ip")

  if snmp_reachable "$ip"; then
    add_device_api "$name" "$ip" "$SNMP_COMMUNITY" || \
      add_device_cli "$name" "$ip" "$SNMP_COMMUNITY"
  else
    echo "  $name ($ip): No SNMP response, skipped"
  fi
done

# Configure alert rules
echo ""
echo "[5/5] Setting up alert rules..."

if [ -n "$API_TOKEN" ]; then
  curl -s -X POST "$LIBRENMS_URL/api/v0/rules" \
    -H "X-Auth-Token: $API_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{
      "name": "设备离线告警",
      "builder": "{\"condition\":\"AND\",\"rules\":[{\"id\":\"macros.device_down\",\"field\":\"macros.device_down\",\"type\":\"boolean\",\"input\":\"radio\",\"operator\":\"equal\",\"value\":\"1\"}],\"valid\":true}",
      "severity": "critical",
      "disabled": 0
    }' > /dev/null 2>&1 && echo "  Alert rule: 设备离线告警 - OK" || echo "  Alert rule: 设备离线告警 - skipped"

  curl -s -X POST "$LIBRENMS_URL/api/v0/rules" \
    -H "X-Auth-Token: $API_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{
      "name": "高延迟告警",
      "builder": "{\"condition\":\"AND\",\"rules\":[{\"id\":\"macros.device_up\",\"field\":\"macros.device_up\",\"type\":\"boolean\",\"input\":\"radio\",\"operator\":\"equal\",\"value\":\"1\"},{\"id\":\"device_perf_loss\",\"field\":\"device_perf_loss\",\"type\":\"text\",\"operator\":\"greater\",\"value\":\"5\"}],\"valid\":true}",
      "severity": "warning",
      "disabled": 0
    }' > /dev/null 2>&1 && echo "  Alert rule: 高延迟告警 - OK" || echo "  Alert rule: 高延迟告警 - skipped"
fi

echo ""
echo "============================================"
echo "  LibreNMS Discovery Complete!"
echo "============================================"
echo ""
echo "  Web UI:    http://localhost:8002"
echo "  Username:  $LIBRENMS_ADMIN_USER"
echo "  Password:  $LIBRENMS_ADMIN_PASSWORD"
echo ""
echo "  Discovery targets: $DISCOVERY_TARGETS"
echo "  Core IP:           $CORE_IP"
echo "  SNMP Community:    $SNMP_COMMUNITY"
echo ""
echo "  下一步:"
echo "  1. 登录 LibreNMS 修改默认密码"
echo "  2. 确认发现到的设备已开始采集"
echo "  3. 添加 UniFi AP 或调整 LIBRENMS_DISCOVERY_TARGETS"
echo "  4. 配置通知渠道 (飞书/邮件等)"
echo ""
