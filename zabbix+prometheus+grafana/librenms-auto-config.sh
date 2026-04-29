#!/bin/sh
# LibreNMS Auto-Configuration Script
# 自动添加交换机设备到 LibreNMS
# 设计为在 librenms-config 容器内运行，通过 API 操作 librenms 容器

set -e

LIBRENMS_URL="http://librenms:8000"
SNMP_COMMUNITY="global"
SNMP_VERSION="v2c"

echo "============================================"
echo "  LibreNMS Auto-Configuration"
echo "============================================"
echo ""

# Wait for LibreNMS to be ready
echo "[1/4] Waiting for LibreNMS to be ready..."
for i in $(seq 1 90); do
  if curl -s -f "$LIBRENMS_URL/" > /dev/null 2>&1; then
    echo "  LibreNMS is ready!"
    break
  fi
  if [ $i -eq 90 ]; then
    echo "  ERROR: LibreNMS did not start in time"
    exit 1
  fi
  echo "  Waiting... ($i/90)"
  sleep 10
done

# Create API token
echo ""
echo "[2/4] Creating API token..."
API_TOKEN=$(curl -s -X POST "$LIBRENMS_URL/api/v0/auth/legacy-token" \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin"}' 2>/dev/null | \
  python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null || true)

if [ -z "$API_TOKEN" ]; then
  echo "  WARNING: Could not create API token automatically."
  echo "  Please create one manually in LibreNMS Web UI:"
  echo "  http://localhost:8002 → Gear icon → API → Create API token"
  echo ""
  echo "  Attempting CLI fallback for device addition..."
  API_TOKEN=""
else
  echo "  API Token created successfully"
fi

# Function to add a device via API
add_device_api() {
  local name=$1
  local ip=$2
  local community=$3

  if [ -z "$API_TOKEN" ]; then
    return 1
  fi

  local result
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

  local msg
  msg=$(echo "$result" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('message', d.get('error', 'unknown')))" 2>/dev/null || echo "parse error")
  echo "  $name ($ip): $msg"
}

# Function to add a device via CLI (fallback)
add_device_cli() {
  local name=$1
  local ip=$2
  local community=$3

  php /opt/librenms/addhost.php \
    "$ip" "$SNMP_VERSION" "$community" 2>/dev/null && \
    echo "  $name ($ip): Added via CLI" || \
    echo "  $name ($ip): Already exists or failed"
}

# Add devices
echo ""
echo "[3/4] Adding network devices..."
echo "  SNMP Community: $SNMP_COMMUNITY"
echo ""

# Core Switch
add_device_api "Core" "192.168.10.254" "$SNMP_COMMUNITY" || \
  add_device_cli "Core" "192.168.10.254" "$SNMP_COMMUNITY"

# Access Switch
add_device_api "SW1" "192.168.10.11" "$SNMP_COMMUNITY" || \
  add_device_cli "SW1" "192.168.10.11" "$SNMP_COMMUNITY"

# Stage Switches (from monitor-autoconfig)
for i in 3 4 5 6 7 8; do
  add_device_api "Stage$((i-2))" "172.25.10.$i" "$SNMP_COMMUNITY" || \
    add_device_cli "Stage$((i-2))" "172.25.10.$i" "$SNMP_COMMUNITY"
done

# Configure alert rules
echo ""
echo "[4/4] Setting up alert rules..."

if [ -n "$API_TOKEN" ]; then
  # Device down alert
  curl -s -X POST "$LIBRENMS_URL/api/v0/rules" \
    -H "X-Auth-Token: $API_TOKEN" \
    -H "Content-Type: application/json" \
    -d '{
      "name": "设备离线告警",
      "builder": "{\"condition\":\"AND\",\"rules\":[{\"id\":\"macros.device_down\",\"field\":\"macros.device_down\",\"type\":\"boolean\",\"input\":\"radio\",\"operator\":\"equal\",\"value\":\"1\"}],\"valid\":true}",
      "severity": "critical",
      "disabled": 0
    }' > /dev/null 2>&1 && echo "  Alert rule: 设备离线告警 - OK" || echo "  Alert rule: 设备离线告警 - skipped"

  # High latency alert
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
echo "  LibreNMS Configuration Complete!"
echo "============================================"
echo ""
echo "  Web UI:    http://localhost:8002"
echo "  Username:  admin"
echo "  Password:  admin"
echo ""
echo "  已添加设备:"
echo "    Core     192.168.10.254  (核心交换机)"
echo "    SW1      192.168.10.11   (接入交换机)"
echo "    Stage1-6 172.25.10.3-8   (舞台交换机)"
echo ""
echo "  SNMP Community: $SNMP_COMMUNITY"
echo ""
echo "  下一步:"
echo "  1. 登录 LibreNMS 修改默认密码"
echo "  2. 确认设备已被发现并开始采集"
echo "  3. 添加 UniFi AP (Web UI → Add Device)"
echo "  4. 配置通知渠道 (飞书/邮件等)"
echo ""
