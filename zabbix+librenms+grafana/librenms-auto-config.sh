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
FIREWALL_DISCOVERY_RANGE="${FIREWALL_DISCOVERY_RANGE:-}"
FIREWALL_SNMP_COMMUNITY="${FIREWALL_SNMP_COMMUNITY:-${SNMP_COMMUNITY:-public}}"
FEISHU_ROBOT_TOKEN="${FEISHU_ROBOT_TOKEN:-}"
ISP_PING="${ISP_PING:-}"
FIREWALL_PING="${FIREWALL_PING:-}"
SERVER_PING="${SERVER_PING:-}"
ISP_SATURATION_PERCENT="${ISP_SATURATION_PERCENT:-80}"
FIREWALL_WAN_IF_FILTER="${FIREWALL_WAN_IF_FILTER:-telecom,telcom,unicom,isp,WAN}"
LIBRENMS_API_TOKEN="${LIBRENMS_API_TOKEN:-}"
LIBRENMS_ADMIN_USER="${LIBRENMS_ADMIN_USER:-admin}"
LIBRENMS_ADMIN_PASSWORD="${LIBRENMS_ADMIN_PASSWORD:-admin123}"
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

upsert_admin_user() {
  php <<'PHP'
<?php
try {
    $username = getenv('LIBRENMS_ADMIN_USER') ?: 'admin';
    $password = getenv('LIBRENMS_ADMIN_PASSWORD') ?: 'admin123';
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

    $tableExists = function ($table) use ($pdo) {
        $stmt = $pdo->prepare('SHOW TABLES LIKE ?');
        $stmt->execute([$table]);
        return (bool) $stmt->fetchColumn();
    };

    $tableColumns = function ($table) use ($pdo) {
        $columns = [];
        foreach ($pdo->query("SHOW COLUMNS FROM `{$table}`") as $column) {
            $columns[$column['Field']] = $column;
        }
        return $columns;
    };

    $columns = $tableColumns('users');
    $hasColumn = function ($columns, $field) {
        return array_key_exists($field, $columns);
    };
    $columnNames = function ($columns) {
        return array_map(function ($field) {
            return "`{$field}`";
        }, array_keys($columns));
    };
    $hasInsertedColumn = function ($insert, $field) {
        return in_array("`{$field}`", $insert, true);
    };

    $missingRequiredColumn = function ($meta) {
        return $meta['Null'] === 'NO'
            && $meta['Default'] === null
            && stripos($meta['Extra'], 'auto_increment') === false;
    };

    $requiredDefault = function ($type) {
        return preg_match('/int|float|double|decimal|bool/i', $type) ? 0 : '';
    };

    $insertRequiredDefaults = function ($columns, &$insert, &$values, $idColumn = null) use ($missingRequiredColumn, $requiredDefault, $hasInsertedColumn) {
        foreach ($columns as $field => $meta) {
            if ($field === $idColumn || $hasInsertedColumn($insert, $field) || !$missingRequiredColumn($meta)) {
                continue;
            }
            $insert[] = "`{$field}`";
            $values[] = $requiredDefault($meta['Type']);
        }
    };

    $assignAdminRole = function ($userId) use ($pdo, $tableExists, $tableColumns, $hasColumn, $insertRequiredDefaults) {
        if (!$tableExists('roles') || !$tableExists('assigned_roles')) {
            return;
        }

        $roleColumns = $tableColumns('roles');
        $roleIdColumn = $hasColumn($roleColumns, 'id') ? 'id' : ($hasColumn($roleColumns, 'role_id') ? 'role_id' : null);
        if ($roleIdColumn === null || !$hasColumn($roleColumns, 'name')) {
            throw new RuntimeException('Unsupported roles table schema');
        }

        $roleSql = "SELECT `{$roleIdColumn}` FROM roles WHERE name = ?";
        if ($hasColumn($roleColumns, 'scope')) {
            $roleSql .= ' AND scope IS NULL';
        }
        $roleSql .= ' LIMIT 1';
        $roleStmt = $pdo->prepare($roleSql);
        $roleStmt->execute(['admin']);
        $roleId = $roleStmt->fetchColumn();
        if (!$roleId) {
            throw new RuntimeException('LibreNMS admin role was not seeded');
        }

        $assignedColumns = $tableColumns('assigned_roles');
        $entityIdColumn = $hasColumn($assignedColumns, 'entity_id') ? 'entity_id' : ($hasColumn($assignedColumns, 'model_id') ? 'model_id' : null);
        $entityTypeColumn = $hasColumn($assignedColumns, 'entity_type') ? 'entity_type' : ($hasColumn($assignedColumns, 'model_type') ? 'model_type' : null);
        if (!$hasColumn($assignedColumns, 'role_id') || $entityIdColumn === null || $entityTypeColumn === null) {
            throw new RuntimeException('Unsupported assigned_roles table schema');
        }

        $entityType = 'App\\Models\\User';
        $existsSql = "SELECT 1 FROM assigned_roles WHERE role_id = ? AND `{$entityIdColumn}` = ? AND `{$entityTypeColumn}` = ?";
        if ($hasColumn($assignedColumns, 'scope')) {
            $existsSql .= ' AND scope IS NULL';
        }
        $existsSql .= ' LIMIT 1';
        $existsStmt = $pdo->prepare($existsSql);
        $existsStmt->execute([$roleId, $userId, $entityType]);
        if ($existsStmt->fetchColumn()) {
            return;
        }

        $insert = ['`role_id`', "`{$entityIdColumn}`", "`{$entityTypeColumn}`"];
        $values = [$roleId, $userId, $entityType];
        if ($hasColumn($assignedColumns, 'scope')) {
            $insert[] = '`scope`';
            $values[] = null;
        }
        if ($hasColumn($assignedColumns, 'created_at')) {
            $insert[] = '`created_at`';
            $values[] = date('Y-m-d H:i:s');
        }
        if ($hasColumn($assignedColumns, 'updated_at')) {
            $insert[] = '`updated_at`';
            $values[] = date('Y-m-d H:i:s');
        }
        $insertRequiredDefaults($assignedColumns, $insert, $values);
        $placeholders = implode(', ', array_fill(0, count($insert), '?'));
        $pdo->prepare('INSERT INTO assigned_roles (' . implode(', ', $insert) . ') VALUES (' . $placeholders . ')')->execute($values);
    };

    if (!$hasColumn($columns, 'username') || !$hasColumn($columns, 'password')) {
        throw new RuntimeException('Unsupported users table schema');
    }
    $idColumn = $hasColumn($columns, 'user_id') ? 'user_id' : ($hasColumn($columns, 'id') ? 'id' : null);
    if ($idColumn === null) {
        throw new RuntimeException('Unsupported users table schema');
    }

    // Remove broken duplicate admins with NULL/empty auth_type left by earlier
    // runs. The (auth_type, username) unique key treats NULL as distinct, so a
    // stray NULL row slips in alongside the real "mysql" admin and makes LibreNMS
    // find two "admin" rows at login -> 500. This script owns the admin as
    // auth_type='mysql', so any NULL/empty one is stale and safe to drop.
    if ($hasColumn($columns, 'auth_type')) {
        $pdo->prepare("DELETE FROM users WHERE username = ? AND (auth_type IS NULL OR auth_type = '')")->execute([$username]);
    }

    $hash = password_hash($password, PASSWORD_BCRYPT);
    $userStmt = $pdo->prepare("SELECT `{$idColumn}` FROM users WHERE username = ? LIMIT 1");
    $userStmt->execute([$username]);
    $userId = $userStmt->fetchColumn();

    if ($userId) {
        $sets = ['password = ?'];
        $values = [$hash];
        if ($hasColumn($columns, 'email')) {
            $sets[] = 'email = ?';
            $values[] = $email;
        }
        if ($hasColumn($columns, 'realname')) {
            $sets[] = 'realname = ?';
            $values[] = $username;
        }
        if ($hasColumn($columns, 'descr')) {
            $sets[] = 'descr = ?';
            $values[] = 'Auto-created administrator';
        }
        if ($hasColumn($columns, 'auth_type')) {
            $sets[] = 'auth_type = ?';
            $values[] = 'mysql';
        }
        if ($hasColumn($columns, 'level')) {
            $sets[] = 'level = 10';
        }
        if ($hasColumn($columns, 'can_modify_passwd')) {
            $sets[] = 'can_modify_passwd = 1';
        }
        if ($hasColumn($columns, 'updated_at')) {
            $sets[] = 'updated_at = NOW()';
        }

        $values[] = $userId;
        $update = $pdo->prepare('UPDATE users SET ' . implode(', ', $sets) . " WHERE `{$idColumn}` = ?");
        $update->execute($values);
        $assignAdminRole($userId);
        exit(0);
    }

    $insert = [];
    $values = [];
    $add = function ($column, $value) use (&$insert, &$values, $columns, $hasColumn) {
        if ($hasColumn($columns, $column)) {
            $insert[] = "`{$column}`";
            $values[] = $value;
        }
    };

    $add('username', $username);
    $add('password', $hash);
    $add('email', $email);
    $add('realname', $username);
    $add('descr', 'Auto-created administrator');
    $add('auth_type', 'mysql');
    $add('level', 10);
    $add('can_modify_passwd', 1);
    if ($hasColumn($columns, 'created_at')) {
        $insert[] = '`created_at`';
        $values[] = date('Y-m-d H:i:s');
    }
    if ($hasColumn($columns, 'updated_at')) {
        $insert[] = '`updated_at`';
        $values[] = date('Y-m-d H:i:s');
    }

    $insertRequiredDefaults($columns, $insert, $values, $idColumn);

    $placeholders = implode(', ', array_fill(0, count($insert), '?'));
    // Upsert: if the (auth_type, username) row already exists, update the password
    // and admin fields instead of failing on the unique constraint.
    $updates = ['`password` = VALUES(`password`)'];
    foreach (['email', 'realname', 'descr', 'auth_type', 'level', 'can_modify_passwd'] as $uc) {
        if ($hasColumn($columns, $uc)) {
            $updates[] = "`{$uc}` = VALUES(`{$uc}`)";
        }
    }
    if ($hasColumn($columns, 'updated_at')) {
        $updates[] = '`updated_at` = NOW()';
    }
    $sql = 'INSERT INTO users (' . implode(', ', $insert) . ') VALUES (' . $placeholders . ')'
         . ' ON DUPLICATE KEY UPDATE ' . implode(', ', $updates);
    $pdo->prepare($sql)->execute($values);

    // lastInsertId() is 0 on a pure update, so re-resolve the id for role assignment.
    $idStmt = $pdo->prepare("SELECT `{$idColumn}` FROM users WHERE username = ? LIMIT 1");
    $idStmt->execute([$username]);
    $resolvedId = $idStmt->fetchColumn();
    $assignAdminRole($resolvedId ?: $pdo->lastInsertId());
} catch (Throwable $e) {
    fwrite(STDERR, 'LibreNMS admin user upsert failed: ' . $e->getMessage() . PHP_EOL);
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

# Assign the LibreNMS 26.x "admin" role via Spatie. This is the only reliable way:
# there is no role CLI, and a raw DB insert into model_has_roles wouldn't clear
# Spatie's permission cache (so the role wouldn't take effect until cache expiry).
assign_admin_role_lnms() {
  run_lnms tinker --execute="\$u=\\App\\Models\\User::where('username','${LIBRENMS_ADMIN_USER}')->first(); if(\$u && !\$u->hasRole('admin')){\$u->assignRole('admin');}" >/dev/null 2>&1 || true
}

ensure_admin_user() {
  if ! has_lnms_cmd; then
    echo "  WARNING: lnms command not found, falling back to database admin user sync."
    for i in $(seq 1 20); do
      if upsert_admin_user; then
        echo "  Admin user '$LIBRENMS_ADMIN_USER' is ready; password synced from .env."
        return 0
      fi

      echo "  Waiting for LibreNMS database initialization... ($i/20)"
      sleep 10
    done
    return 0
  fi

  for i in $(seq 1 20); do
    output=$(run_lnms user:add \
      --password="$LIBRENMS_ADMIN_PASSWORD" \
      --role=admin \
      --email="$LIBRENMS_ADMIN_EMAIL" \
      --no-interaction \
      "$LIBRENMS_ADMIN_USER" 2>&1) && {
        echo "  Admin user '$LIBRENMS_ADMIN_USER' is ready."
        return 0
      }

    if echo "$output" | grep -qi "already"; then
      if upsert_admin_user; then
        assign_admin_role_lnms
        echo "  Admin user '$LIBRENMS_ADMIN_USER' already exists; password and admin role synced from .env."
        return 0
      fi
    fi

    if upsert_admin_user >/dev/null 2>&1; then
      assign_admin_role_lnms
      echo "  Admin user '$LIBRENMS_ADMIN_USER' is ready; password and admin role synced from .env."
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

  # --- 持续自动发现：网段扫描 ---
  # 把 "192.168.10.1-100,192.168.10.254" 和防火墙范围转成 CIDR 列表写入 nets[]
  configure_nets() {
    idx=0
    seen_cidrs=""
    # 先清掉旧的 nets 配置（忽略报错）
    run_lnms config:set nets '[]' >/dev/null 2>&1 || true

    # 从 IP 范围提取 /24 网段（取第一个 IP 的前三段），去重
    for target in $(echo "${DISCOVERY_TARGETS},${FIREWALL_DISCOVERY_RANGE}" | tr ',' '\n'); do
      target=$(echo "$target" | tr -d '[:space:]')
      [ -z "$target" ] && continue
      base_ip=${target%%-*}      # 取 range 起始 IP，或单 IP
      prefix=$(echo "$base_ip" | sed 's/\.[0-9]*$//')
      cidr="${prefix}.0/24"
      # 跳过重复
      case "$seen_cidrs" in *"$cidr"*) continue ;; esac
      seen_cidrs="$seen_cidrs $cidr"
      run_lnms config:set "nets.${idx}" "$cidr" >/dev/null 2>&1 && \
        echo "  nets[$idx]: $cidr" || \
        echo "  WARNING: Could not set nets[$idx]=$cidr"
      idx=$((idx + 1))
    done
  }
  configure_nets

  # --- 开启 CDP/LLDP 邻居自动发现（从已知设备爬全网） ---
  run_lnms config:set autodiscovery.xdp true >/dev/null 2>&1 && \
    echo "  autodiscovery.xdp (CDP/LLDP): enabled" || \
    echo "  WARNING: Could not enable xdp autodiscovery"
  run_lnms config:set autodiscovery.nets true >/dev/null 2>&1 && \
    echo "  autodiscovery.nets: enabled" || true
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
API_TOKEN="$LIBRENMS_API_TOKEN"

if [ -z "$API_TOKEN" ]; then
  # 直接写数据库——不依赖 LibreNMS 版本，只要容器能访问 DB 就行
  _db_host="${DB_HOST:-librenms-db}"
  _db_user="${DB_USER:-librenms}"
  _db_pass="${DB_PASSWORD:-${DB_PASS:-librenms}}"
  _db_name="${DB_NAME:-librenms}"

  _token=$(php -r "echo bin2hex(random_bytes(32));" 2>/dev/null || true)
  _user_id=$(mysql -h "$_db_host" -u "$_db_user" -p"$_db_pass" "$_db_name" \
    -sN -e "SELECT user_id FROM users WHERE username='${LIBRENMS_ADMIN_USER}' LIMIT 1" 2>/dev/null || true)

  if [ -n "$_token" ] && [ -n "$_user_id" ]; then
    mysql -h "$_db_host" -u "$_db_user" -p"$_db_pass" "$_db_name" -e \
      "INSERT INTO api_tokens (user_id, token_hash, description, disabled)
       VALUES ('$_user_id', '$_token', 'autoconfig', 0)
       ON DUPLICATE KEY UPDATE token_hash='$_token'" 2>/dev/null && \
      API_TOKEN="$_token" && echo "  API Token created via DB"
  fi
fi

if [ -z "$API_TOKEN" ]; then
  echo "  WARNING: Could not create API token."
  echo "  Feishu transport will not be configured automatically."
  echo "  Fix: set LIBRENMS_API_TOKEN in .env, then rerun: docker compose up -d --force-recreate librenms-config"
else
  echo "  API Token ready"
  # 写到共享 volume，让 alertmanager-feishu-bridge 的 device watcher 读取（免手动配置）
  echo "$API_TOKEN" > /data/librenms-api-token 2>/dev/null || true
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

add_ping_device_api() {
  name=$1; ip=$2
  [ -z "$API_TOKEN" ] && return 0
  # ICMP-only 设备：snmp_disable=true 跳过 SNMP，os=ping 走 ping 模块。
  # 不能用 force_add（那会要求 SNMP 信息），sysName 用显示名方便识别。
  result=$(curl -s -X POST "$LIBRENMS_URL/api/v0/devices" \
    -H "X-Auth-Token: $API_TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"hostname\":\"$ip\",\"display_name\":\"$name\",\"snmp_disable\":true,\"os\":\"ping\",\"sysName\":\"$name\",\"hardware\":\"ICMP\"}" 2>/dev/null)
  msg=$(echo "$result" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('message',d.get('error','?')))" 2>/dev/null || echo "parse error")
  echo "  $name ($ip): $msg"
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

echo ""
echo "[4b/5] Adding ping-only devices (ISP / Firewall physical / Servers)..."
for combined in $(echo "${ISP_PING}${ISP_PING:+,}${FIREWALL_PING}${FIREWALL_PING:+,}${SERVER_PING}" | tr ',' '\n'); do
  combined=$(echo "$combined" | tr -d '[:space:]')
  [ -z "$combined" ] && continue
  case "$combined" in *:*)
    name="${combined%%:*}"
    ip_part="${combined#*:}"
    ip="${ip_part%%-*}"
    [ -n "$ip" ] && add_ping_device_api "$name" "$ip"
  ;; esac
done

# --- 防火墙 SNMP 设备（FIREWALL_SNMP_TARGETS）→ LibreNMS SNMP 监控 ---
if [ -n "$FIREWALL_SNMP_TARGETS" ] && [ -n "$API_TOKEN" ]; then
  echo ""
  echo "  Adding firewall SNMP devices to LibreNMS..."
  for combined in $(echo "$FIREWALL_SNMP_TARGETS" | tr ',' '\n'); do
    combined=$(echo "$combined" | tr -d '[:space:]')
    [ -z "$combined" ] && continue
    case "$combined" in *:*)
      name="${combined%%:*}"
      ip="${combined#*:}"
      add_device_api "$name" "$ip" "$FIREWALL_SNMP_COMMUNITY" || true
    ;; esac
  done
fi

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

  _resp=$(curl -s -X POST "$LIBRENMS_URL/api/v0/rules" \
    -H "X-Auth-Token: $API_TOKEN" \
    -H "Content-Type: application/json" \
    -d "$rule_payload" 2>/dev/null)
  _status=$(echo "$_resp" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('status',''))" 2>/dev/null || echo "")
  if [ "$_status" = "ok" ]; then
    echo "  Alert rule: $rule_name - created"
  else
    _msg=$(echo "$_resp" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('message',''))" 2>/dev/null || echo "$_resp")
    echo "  Alert rule: $rule_name - failed: $_msg"
  fi
}

if [ -n "$API_TOKEN" ]; then
  EXISTING_RULES=$(curl -s -H "X-Auth-Token: $API_TOKEN" "$LIBRENMS_URL/api/v0/rules" 2>/dev/null || echo '{"rules":[]}')

  upsert_rule "设备离线告警" '{
    "name": "设备离线告警",
    "devices": [-1],
    "builder": "{\"condition\":\"AND\",\"rules\":[{\"id\":\"macros.device_down\",\"field\":\"macros.device_down\",\"type\":\"boolean\",\"input\":\"radio\",\"operator\":\"equal\",\"value\":\"1\"}],\"valid\":true}",
    "severity": "critical",
    "disabled": 0
  }'

  upsert_rule "高丢包告警" '{
    "name": "高丢包告警",
    "devices": [-1],
    "builder": "{\"condition\":\"AND\",\"rules\":[{\"id\":\"macros.device_up\",\"field\":\"macros.device_up\",\"type\":\"boolean\",\"input\":\"radio\",\"operator\":\"equal\",\"value\":\"1\"},{\"id\":\"device_perf_loss\",\"field\":\"device_perf_loss\",\"type\":\"text\",\"operator\":\"greater\",\"value\":\"10\"}],\"valid\":true}",
    "severity": "warning",
    "disabled": 0
  }'

  build_wan_or_rules() {
    result=""
    for kw in $(echo "$FIREWALL_WAN_IF_FILTER" | tr ',' '\n'); do
      kw=$(echo "$kw" | tr -d '[:space:]')
      [ -z "$kw" ] && continue
      r="{\"id\":\"ports.ifAlias\",\"field\":\"ports.ifAlias\",\"type\":\"string\",\"input\":\"text\",\"operator\":\"contains\",\"value\":\"${kw}\"}"
      result="${result}${result:+,}${r}"
    done
    printf '%s' "$result"
  }
  wan_or=$(build_wan_or_rules)
  isp_builder="{\"condition\":\"AND\",\"rules\":[{\"id\":\"macros.port_usage_perc\",\"field\":\"macros.port_usage_perc\",\"type\":\"double\",\"input\":\"number\",\"operator\":\"greater_or_equal\",\"value\":\"${ISP_SATURATION_PERCENT}\"},{\"condition\":\"OR\",\"rules\":[${wan_or}]}],\"valid\":true}"
  isp_builder_escaped=$(printf '%s' "$isp_builder" | python3 -c 'import sys,json;print(json.dumps(sys.stdin.read()))')
  upsert_rule "ISP 带宽饱和告警" "{\"name\":\"ISP 带宽饱和告警\",\"devices\":[-1],\"builder\":${isp_builder_escaped},\"severity\":\"warning\",\"disabled\":0}"
fi

# Configure Feishu alert transport
echo ""
echo "[6/6] Setting up Feishu alert transport..."

configure_feishu_transport() {
  if [ -z "$FEISHU_ROBOT_TOKEN" ]; then
    echo "  FEISHU_ROBOT_TOKEN not set, skipping Feishu transport"
    return 0
  fi

  # LibreNMS 没有创建 transport 的 API 路由（只有 GET），所以直接写 DB。
  # 用 PHP PDO 绑定参数插入，json_encode 处理 transport_config 转义，无需手工转义。
  # api transport 走 SimpleTemplate（扁平 {{ key }}），api-body 用安全标量字段拼
  # 成 JSON 发给 bridge 的 /librenms 端点。hostname/ip/severity/state/timestamp
  # 都不含引号或换行，拼出的 JSON 一定合法。
  php <<'PHP'
<?php
try {
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

    $name = 'Feishu';
    $exists = $pdo->prepare('SELECT transport_id FROM alert_transports WHERE transport_name = ? LIMIT 1');
    $exists->execute([$name]);
    $transportId = $exists->fetchColumn();

    $config = json_encode([
        'api-url'    => 'http://alertmanager-feishu-bridge:5005/librenms',
        'api-method' => 'POST',
        'api-body'   => '{"state":"{{ state }}","name":"{{ name }}","severity":"{{ severity }}","hostname":"{{ hostname }}","sysName":"{{ sysName }}","ip":"{{ ip }}","timestamp":"{{ timestamp }}"}',
    ], JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);

    if ($transportId) {
        $upd = $pdo->prepare('UPDATE alert_transports SET transport_type = ?, is_default = 1, transport_config = ? WHERE transport_id = ?');
        $upd->execute(['api', $config, $transportId]);
        fwrite(STDERR, 'updated');
        exit(0);
    }

    $ins = $pdo->prepare('INSERT INTO alert_transports (transport_name, transport_type, is_default, transport_config) VALUES (?, ?, 1, ?)');
    $ins->execute([$name, 'api', $config]);
    fwrite(STDERR, 'created');
    exit(0);
} catch (Throwable $e) {
    fwrite(STDERR, 'ERROR: ' . $e->getMessage());
    exit(1);
}
PHP
}

_ft_out=$(configure_feishu_transport 2>&1) || true
case "$_ft_out" in
  *created*) echo "  Feishu transport created (default, → bridge /librenms)" ;;
  *updated*) echo "  Feishu transport updated (default, → bridge /librenms)" ;;
  *"not set"*) echo "$_ft_out" ;;
  *) echo "  WARNING: Could not create Feishu transport: $_ft_out" ;;
esac

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
echo "  2. 确认发现到的设备已开始采集（约 5 分钟后自动发现）"
echo "  3. 添加 UniFi AP 或调整 LIBRENMS_DISCOVERY_TARGETS / FIREWALL_DISCOVERY_RANGE"
if [ -z "$FEISHU_ROBOT_TOKEN" ]; then
  echo "  4. 填写 FEISHU_ROBOT_TOKEN 后重启以启用飞书告警推送"
else
  echo "  4. 飞书告警已配置 → alertmanager-feishu-bridge:5005/librenms"
fi
echo ""
