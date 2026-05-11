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
LIBRENMS_BASE_URL="${LIBRENMS_BASE_URL:-http://localhost:8002}"
LIBRENMS_PORT="${LIBRENMS_PORT:-8002}"
SERVER_IP="${SERVER_IP:-}"
RRDCACHED_SERVER="${RRDCACHED_SERVER:-}"
LIBRENMS_OWN_HOSTNAME="${LIBRENMS_OWN_HOSTNAME:-}"

if [ "$LIBRENMS_BASE_URL" = "http://localhost:8002" ] && [ -n "$SERVER_IP" ]; then
  LIBRENMS_BASE_URL="http://$SERVER_IP:$LIBRENMS_PORT"
fi

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

reset_admin_user_password() {
  php <<'PHP'
<?php
try {
    $username = getenv('LIBRENMS_ADMIN_USER') ?: 'admin';
    $password = getenv('LIBRENMS_ADMIN_PASSWORD') ?: 'admin';
    $email = getenv('LIBRENMS_ADMIN_EMAIL') ?: 'admin@example.com';
    $host = getenv('DB_HOST') ?: 'librenms-db';
    $database = getenv('DB_NAME') ?: 'librenms';
    $dbUser = getenv('DB_USER') ?: 'librenms';
    $dbPass = getenv('DB_PASSWORD') ?: (getenv('DB_PASS') ?: 'librenms');

    $pdo = new PDO(
        "mysql:host={$host};dbname={$database};charset=utf8mb4",
        $dbUser,
        $dbPass,
        [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION]
    );

    $columns = [];
    foreach ($pdo->query('SHOW COLUMNS FROM users') as $column) {
        $columns[$column['Field']] = true;
    }
    $idColumn = isset($columns['user_id']) ? 'user_id' : (isset($columns['id']) ? 'id' : null);
    if ($idColumn === null || !isset($columns['username']) || !isset($columns['password'])) {
        throw new RuntimeException('Unsupported users table schema');
    }

    $userStmt = $pdo->prepare("SELECT `{$idColumn}` FROM users WHERE username = ? LIMIT 1");
    $userStmt->execute([$username]);
    $userId = $userStmt->fetchColumn();
    if (!$userId) {
        exit(2);
    }

    $sets = ['password = ?'];
    $values = [password_hash($password, PASSWORD_BCRYPT)];
    if (isset($columns['email'])) {
        $sets[] = 'email = ?';
        $values[] = $email;
    }
    if (isset($columns['auth_type'])) {
        $sets[] = 'auth_type = ?';
        $values[] = 'mysql';
    }
    if (isset($columns['level'])) {
        $sets[] = 'level = 10';
    }
    if (isset($columns['can_modify_passwd'])) {
        $sets[] = 'can_modify_passwd = 1';
    }

    $values[] = $userId;
    $update = $pdo->prepare('UPDATE users SET ' . implode(', ', $sets) . " WHERE `{$idColumn}` = ?");
    $update->execute($values);
} catch (Throwable $e) {
    fwrite(STDERR, 'LibreNMS admin password sync failed: ' . $e->getMessage() . PHP_EOL);
    exit(1);
}
PHP
}

has_lnms_cmd() {
  [ -x /opt/librenms/lnms ] && return 0
  command -v lnms >/dev/null 2>&1 && return 0
  [ -x /opt/librenms/artisan ] && return 0
  return 1
}

run_as_librenms() {
  if [ "$(id -u)" = "0" ] && command -v s6-setuidgid >/dev/null 2>&1; then
    s6-setuidgid librenms "$@"
    return $?
  fi

  "$@"
}

run_lnms() {
  if [ -x /opt/librenms/lnms ]; then
    run_as_librenms /opt/librenms/lnms "$@"
    return $?
  fi

  if command -v lnms >/dev/null 2>&1; then
    run_as_librenms "$(command -v lnms)" "$@"
    return $?
  fi

  if [ -x /opt/librenms/artisan ]; then
    run_as_librenms php /opt/librenms/artisan "$@"
    return $?
  fi

  return 1
}

ensure_admin_user() {
  if ! has_lnms_cmd; then
    echo "  WARNING: lnms command not found, skipping admin user creation."
    return 0
  fi

  for i in $(seq 1 20); do
    output=$(run_lnms user:add \
      --password="$LIBRENMS_ADMIN_PASSWORD" \
      --role=admin \
      --email="$LIBRENMS_ADMIN_EMAIL" \
      --no-interaction \
      "$LIBRENMS_ADMIN_USER" 2>&1) && {
        reset_admin_user_password >/dev/null 2>&1 || true
        echo "  Admin user '$LIBRENMS_ADMIN_USER' is ready."
        return 0
      }

    if reset_admin_user_password; then
      echo "  Admin user '$LIBRENMS_ADMIN_USER' password synced from .env."
      return 0
    fi

    echo "  Waiting for LibreNMS database initialization... ($i/20)"
    [ -n "$output" ] && echo "  Last output: $output"
    sleep 10
  done

  echo "  WARNING: Could not create admin user automatically."
  echo "  Last output: $output"
  return 0
}

configure_runtime() {
  mkdir -p /data/rrd
  chmod 775 /data/rrd 2>/dev/null || true

  if ! has_lnms_cmd; then
    echo "  WARNING: lnms command not found, skipping runtime config."
    return 0
  fi

  run_lnms config:set auth_mechanism mysql >/dev/null 2>&1 && \
    echo "  auth_mechanism: mysql" || \
    echo "  WARNING: Could not set auth_mechanism"

  run_lnms config:set base_url "$LIBRENMS_BASE_URL" >/dev/null 2>&1 && \
    echo "  base_url: $LIBRENMS_BASE_URL" || \
    echo "  WARNING: Could not set base_url"

  if [ -n "$LIBRENMS_OWN_HOSTNAME" ]; then
    run_lnms config:set own_hostname "$LIBRENMS_OWN_HOSTNAME" >/dev/null 2>&1 && \
      echo "  own_hostname: $LIBRENMS_OWN_HOSTNAME" || \
      echo "  WARNING: Could not set own_hostname"
  fi

  if [ -n "$RRDCACHED_SERVER" ]; then
    run_lnms config:set rrdcached "$RRDCACHED_SERVER" >/dev/null 2>&1 && \
      echo "  rrdcached: $RRDCACHED_SERVER" || \
      echo "  WARNING: Could not set rrdcached"
  fi

  run_lnms config:set distributed_poller true >/dev/null 2>&1 && \
    echo "  distributed_poller: enabled for dispatcher service" || \
    echo "  WARNING: Could not enable distributed_poller"

  for task in poller services discovery alerting billing ping; do
    run_lnms config:set "schedule_type.$task" dispatcher >/dev/null 2>&1 && \
      echo "  schedule_type.$task: dispatcher" || \
      echo "  WARNING: Could not set schedule_type.$task"
  done

  run_lnms config:set service_poller_workers "${LIBRENMS_POLLER_WORKERS:-4}" >/dev/null 2>&1 || true
  run_lnms config:set service_discovery_workers "${LIBRENMS_DISCOVERY_WORKERS:-2}" >/dev/null 2>&1 || true
}

echo ""
echo "[2/5] Ensuring LibreNMS admin user..."
ensure_admin_user

echo ""
echo "[2b/5] Applying LibreNMS runtime settings..."
configure_runtime

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

# Idempotency: GET existing rules once, skip POST if name already exists.
# Re-running this script no longer creates duplicate rule entries.
upsert_rule() {
  rule_name="$1"
  rule_payload="$2"

  if echo "$EXISTING_RULES" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    rules = data if isinstance(data, list) else data.get('rules', [])
    sys.exit(0 if any(r.get('name') == sys.argv[1] for r in rules) else 1)
except Exception:
    sys.exit(1)
" "$rule_name" 2>/dev/null; then
    echo "  Alert rule: $rule_name - skipped (already exists)"
    return 0
  fi

  curl -s -X POST "$LIBRENMS_URL/api/v0/rules" \
    -H "X-Auth-Token: $API_TOKEN" \
    -H "Content-Type: application/json" \
    -d "$rule_payload" > /dev/null 2>&1 \
    && echo "  Alert rule: $rule_name - created" \
    || echo "  Alert rule: $rule_name - failed"
}

if [ -n "$API_TOKEN" ]; then
  EXISTING_RULES=$(curl -s -H "X-Auth-Token: $API_TOKEN" "$LIBRENMS_URL/api/v0/rules" 2>/dev/null || echo '{"rules":[]}')

  upsert_rule "设备离线告警" '{
    "name": "设备离线告警",
    "builder": "{\"condition\":\"AND\",\"rules\":[{\"id\":\"macros.device_down\",\"field\":\"macros.device_down\",\"type\":\"boolean\",\"input\":\"radio\",\"operator\":\"equal\",\"value\":\"1\"}],\"valid\":true}",
    "severity": "critical",
    "disabled": 0
  }'

  upsert_rule "高丢包告警" '{
    "name": "高丢包告警",
    "builder": "{\"condition\":\"AND\",\"rules\":[{\"id\":\"macros.device_up\",\"field\":\"macros.device_up\",\"type\":\"boolean\",\"input\":\"radio\",\"operator\":\"equal\",\"value\":\"1\"},{\"id\":\"device_perf_loss\",\"field\":\"device_perf_loss\",\"type\":\"text\",\"operator\":\"greater\",\"value\":\"10\"}],\"valid\":true}",
    "severity": "warning",
    "disabled": 0
  }'
fi

echo ""
echo "============================================"
echo "  LibreNMS Discovery Complete!"
echo "============================================"
echo ""
echo "  Web UI:    $LIBRENMS_BASE_URL"
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
