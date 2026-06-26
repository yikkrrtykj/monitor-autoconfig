#!/bin/sh
# LibreNMS Auto-Configuration Script
# 扫描 SNMP 设备并自动添加到 LibreNMS

set -e

LIBRENMS_URL="${LIBRENMS_URL:-http://librenms:8000}"
SNMP_COMMUNITY="${LIBRENMS_SNMP_COMMUNITY:-${SNMP_COMMUNITY:-global}}"
SNMP_VERSION="${SNMP_VERSION:-v2c}"
SNMP_TIMEOUT="${SNMP_TIMEOUT:-1}"
SNMP_RETRIES="${SNMP_RETRIES:-0}"
CORE_SWITCH_PING="${CORE_SWITCH_PING:-}"
# 核心 IP：优先 LIBRENMS_CORE_IP；留空则取 CORE_SWITCH_PING 第一条的 IP（去掉 "名称:" 前缀和 "-范围"）；都空兜底 .254
if [ -n "${LIBRENMS_CORE_IP:-}" ]; then
  CORE_IP="$LIBRENMS_CORE_IP"
else
  _core="${CORE_SWITCH_PING%%,*}"
  _core="${_core##*:}"
  _core="${_core%%-*}"
  CORE_IP="${_core:-192.168.10.254}"
fi
DISCOVERY_TARGETS="${LIBRENMS_DISCOVERY_TARGETS:-192.168.10.1-100,192.168.10.254}"
FIREWALL_DISCOVERY_RANGE="${FIREWALL_DISCOVERY_RANGE:-}"
FIREWALL_SNMP_COMMUNITY="${FIREWALL_SNMP_COMMUNITY:-${SNMP_COMMUNITY:-public}}"
FEISHU_ROBOT_TOKEN="${FEISHU_ROBOT_TOKEN:-}"
ISP_PING="${ISP_PING:-}"
FIREWALL_PING="${FIREWALL_PING:-}"
SERVER_PING="${SERVER_PING:-}"
BIGSCREEN_ISP_MAX_BANDWIDTH="${BIGSCREEN_ISP_MAX_BANDWIDTH:-1000}"
ISP_SATURATION_PERCENT="${ISP_SATURATION_PERCENT:-80}"
FIREWALL_WAN_IF_FILTER="${FIREWALL_WAN_IF_FILTER:-telecom,telcom,unicom,isp,WAN}"
# 互联/上联口描述关键词（逗号分隔）。只对 ifAlias 含这些词的口做"断链"告警，
# 其它口（选手口等）不报。把上联成员口统一描述成含这些词即可，如 description to-stage1。
UPLINK_IF_FILTER="${UPLINK_IF_FILTER:-to-stage,to-core,to-dist,uplink}"
LIBRENMS_SUPPRESS_STP_EVENTS="${LIBRENMS_SUPPRESS_STP_EVENTS:-true}"
LIBRENMS_API_TOKEN="${LIBRENMS_API_TOKEN:-}"
LIBRENMS_ADMIN_USER="${LIBRENMS_ADMIN_USER:-admin}"
LIBRENMS_ADMIN_PASSWORD="${LIBRENMS_ADMIN_PASSWORD:-admin123}"
LIBRENMS_ADMIN_EMAIL="${LIBRENMS_ADMIN_EMAIL:-admin@example.com}"
LIBRENMS_BASE_URL="${LIBRENMS_BASE_URL:-}"
LIBRENMS_FORCE_BASE_URL="${LIBRENMS_FORCE_BASE_URL:-false}"
LIBRENMS_PORT="${LIBRENMS_PORT:-8002}"
SERVER_IP="${SERVER_IP:-}"
RRDCACHED_SERVER="${RRDCACHED_SERVER:-}"
LIBRENMS_OWN_HOSTNAME="${LIBRENMS_OWN_HOSTNAME:-}"
LIBRENMS_HOME_DASHBOARD_AUTO="${LIBRENMS_HOME_DASHBOARD_AUTO:-true}"
LIBRENMS_HOME_DASHBOARD_NAME="${LIBRENMS_HOME_DASHBOARD_NAME:-赛事网络总览}"
LIBRENMS_HOME_WAN_CARDS="${LIBRENMS_HOME_WAN_CARDS:-true}"
LIBRENMS_HOME_WAN_CARD_LIMIT="${LIBRENMS_HOME_WAN_CARD_LIMIT:-8}"
LIBRENMS_HOME_TOP_INTERFACES="${LIBRENMS_HOME_TOP_INTERFACES:-true}"
LIBRENMS_HOME_TOP_DEVICES="${LIBRENMS_HOME_TOP_DEVICES:-true}"
LIBRENMS_HOME_SWITCH_CPU="${LIBRENMS_HOME_SWITCH_CPU:-true}"
LIBRENMS_HOME_SERVER_STATS="${LIBRENMS_HOME_SERVER_STATS:-auto}"

normalize_base_url() {
  raw=$(printf '%s' "${1:-}" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
  [ -z "$raw" ] && return 0
  case "$raw" in
    *'${'*|*'}'*) return 0 ;;
    http://*|https://*) printf '%s' "$raw" ;;
    *) printf 'http://%s' "$raw" ;;
  esac
}

LIBRENMS_BASE_URL=$(normalize_base_url "$LIBRENMS_BASE_URL")
if [ -z "$LIBRENMS_BASE_URL" ]; then
  if [ -n "$SERVER_IP" ]; then
    LIBRENMS_BASE_URL="http://$SERVER_IP:$LIBRENMS_PORT"
  else
    LIBRENMS_BASE_URL="http://localhost:8002"
  fi
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

  if [ "$LIBRENMS_FORCE_BASE_URL" = "true" ]; then
    run_lnms config:set base_url "$LIBRENMS_BASE_URL" >/dev/null 2>&1 && \
      echo "  base_url: $LIBRENMS_BASE_URL" || \
      echo "  WARNING: Could not set base_url"
  else
    run_lnms config:set base_url "" >/dev/null 2>&1 && \
      echo "  base_url: dynamic request host" || \
      echo "  WARNING: Could not clear base_url"
  fi

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

  run_lnms config:set uptime_warning "${LIBRENMS_UPTIME_WARNING_SECONDS:-0}" >/dev/null 2>&1 && \
    echo "  uptime_warning: ${LIBRENMS_UPTIME_WARNING_SECONDS:-0}s" || \
    echo "  WARNING: Could not set uptime_warning"

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

create_api_token_model() {
  run_as_librenms php <<'PHP'
<?php
try {
    require '/opt/librenms/vendor/autoload.php';
    $app = require '/opt/librenms/bootstrap/app.php';
    $kernel = $app->make(Illuminate\Contracts\Console\Kernel::class);
    $kernel->bootstrap();

    $username = getenv('LIBRENMS_ADMIN_USER') ?: 'admin';
    $user = \App\Models\User::where('username', $username)->first();
    if (!$user) {
        fwrite(STDERR, "User not found: {$username}\n");
        exit(1);
    }

    $token = \App\Models\ApiToken::where('user_id', $user->user_id)
        ->where('description', 'autoconfig')
        ->first();

    if ($token) {
        echo $token->rotateTokenHash();
    } else {
        echo \App\Models\ApiToken::generateToken($user, 'autoconfig')->token_hash;
    }
    exit(0);
} catch (Throwable $e) {
    fwrite(STDERR, 'API token model generation failed: ' . $e->getMessage() . PHP_EOL);
    exit(1);
}
PHP
}

api_token_works() {
  [ -n "$API_TOKEN" ] || return 1
  _tmp="/tmp/librenms-api-token-check.$$"
  _code=$(curl -s -o "$_tmp" -w "%{http_code}" \
    -H "X-Auth-Token: $API_TOKEN" \
    "$LIBRENMS_URL/api/v0/devices" 2>/dev/null || true)
  rm -f "$_tmp" 2>/dev/null || true
  [ "$_code" = "200" ]
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
  _model_token=$(create_api_token_model 2>/dev/null || true)
  _model_token=$(printf '%s' "$_model_token" | tail -n 1 | tr -d '[:space:]')
  if [ -n "$_model_token" ]; then
    API_TOKEN="$_model_token"
    echo "  API Token created via LibreNMS model"
  fi
fi

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

if [ -n "$API_TOKEN" ] && ! api_token_works; then
  echo "  API Token failed validation; rotating via LibreNMS model..."
  _model_token=$(create_api_token_model 2>/dev/null || true)
  _model_token=$(printf '%s' "$_model_token" | tail -n 1 | tr -d '[:space:]')
  if [ -n "$_model_token" ]; then
    API_TOKEN="$_model_token"
  fi
fi

if [ -z "$API_TOKEN" ] || ! api_token_works; then
  echo "  WARNING: Could not create API token."
  echo "  LibreNMS API automation will be skipped; existing monitoring still runs."
  echo "  Fix: set LIBRENMS_API_TOKEN in .env, then rerun: docker compose up -d --force-recreate librenms-config"
  API_TOKEN=""
else
  echo "  API Token ready"
  # 写到共享 volume，让 alertmanager-feishu-bridge 的 device watcher 读取（免手动配置）
  echo "$API_TOKEN" > /data/librenms-api-token 2>/dev/null || true
fi

expand_targets() {
  old_ifs=$IFS
  IFS=','
  for target in $1; do
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
        octet=$start_octet
        while [ "$octet" -le "$end_octet" ]; do
          echo "$prefix.$octet"
          octet=$((octet + 1))
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

  # display_name 留空时不下发，让 LibreNMS 轮询后用设备自身的 sysName/hostname
  # 作为显示名（例如交换机里配置的 hostname douyu-stage-1），而不是脚本按 IP 末位
  # 编造的 Stage/Device 名——那会盖掉真实主机名。
  if [ -n "$name" ]; then
    _display_field="\"display_name\": \"$name\","
  else
    _display_field=""
  fi

  result=$(curl -s -X POST "$LIBRENMS_URL/api/v0/devices" \
    -H "X-Auth-Token: $API_TOKEN" \
    -H "Content-Type: application/json" \
    -d "{
      \"hostname\": \"$ip\",
      $_display_field
      \"version\": \"$SNMP_VERSION\",
      \"community\": \"$community\",
      \"port\": 161,
      \"transport\": \"udp\",
      \"poller_group\": 0,
      \"disabled\": false
    }" 2>/dev/null)

  msg=$(echo "$result" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('message', d.get('error', 'unknown')))" 2>/dev/null || echo "parse error")
  status=$(echo "$result" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status', ''))" 2>/dev/null || true)
  echo "  ${name:-$ip} ($ip): $msg"

  [ "$status" = "ok" ]
}

add_device_cli() {
  name=$1
  ip=$2
  community=$3

  php /opt/librenms/addhost.php \
    "$ip" "$SNMP_VERSION" "$community" 2>/dev/null && \
    echo "  ${name:-$ip} ($ip): Added via CLI" || \
    echo "  ${name:-$ip} ($ip): Already exists or failed"
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

firewall_snmp_targets() {
  for combined in $(echo "$FIREWALL_SNMP_TARGETS" | tr ',' '\n'); do
    combined=$(echo "$combined" | tr -d '[:space:]')
    [ -z "$combined" ] && continue
    case "$combined" in
      *:*) echo "${combined%%:*}|${combined#*:}" ;;
      *) echo "|$combined" ;;
    esac
  done
}

discover_firewall_ports() {
  [ -n "$FIREWALL_SNMP_TARGETS" ] || return 0
  [ -f /opt/librenms/discovery.php ] || return 0

  echo ""
  echo "  Discovering firewall WAN ports..."
  firewall_snmp_targets | while IFS='|' read -r name ip; do
    [ -n "$ip" ] || continue
    if php /opt/librenms/discovery.php -h "$ip" -m ports >/dev/null 2>&1; then
      echo "  ${name:-$ip} ($ip): ports discovered"
    else
      echo "  ${name:-$ip} ($ip): port discovery deferred"
    fi
  done
}

configure_isp_port_speed_overrides() {
  [ -n "$FIREWALL_SNMP_TARGETS" ] || return 0
  [ -n "$BIGSCREEN_ISP_MAX_BANDWIDTH" ] || return 0

  echo ""
  echo "  Applying ISP WAN port speed overrides from BIGSCREEN_ISP_MAX_BANDWIDTH..."
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

    $quoteIdent = fn(string $name): string => '`' . str_replace('`', '``', $name) . '`';
    $columns = function (string $table) use ($pdo, $quoteIdent): array {
        $cols = [];
        try {
            foreach ($pdo->query("SHOW COLUMNS FROM " . $quoteIdent($table)) as $column) {
                $cols[$column['Field']] = true;
            }
        } catch (Throwable $e) {
            return [];
        }
        return $cols;
    };
    $has = fn(array $cols, string $name): bool => isset($cols[$name]);
    $findAttribTable = function () use ($columns): array {
        foreach (['device_attribs', 'devices_attribs'] as $table) {
            $cols = $columns($table);
            if (! empty($cols)) {
                return [$table, $cols];
            }
        }
        return [null, []];
    };

    $targets = [];
    foreach (explode(',', getenv('FIREWALL_SNMP_TARGETS') ?: '') as $raw) {
        $raw = trim($raw);
        if ($raw === '') {
            continue;
        }
        if (str_contains($raw, ':')) {
            [$name, $ip] = array_map('trim', explode(':', $raw, 2));
        } else {
            $name = '';
            $ip = $raw;
        }
        $ip = preg_replace('/-.*/', '', $ip);
        if ($ip !== '') {
            $targets[] = ['name' => $name, 'ip' => $ip];
        }
    }

    $keywords = array_values(array_filter(array_map(
        fn($v) => strtolower(trim($v)),
        explode(',', getenv('FIREWALL_WAN_IF_FILTER') ?: 'telecom,telcom,unicom,isp,WAN')
    )));

    $parseSpeed = function (string $raw): array {
        $raw = trim($raw);
        $cfg = ['default' => null, 'per' => []];
        if ($raw === '') {
            return $cfg;
        }
        if (preg_match('/^\d+(?:\.\d+)?$/', $raw)) {
            $cfg['default'] = (float) $raw;
            return $cfg;
        }
        foreach (explode(',', $raw) as $item) {
            $item = trim($item);
            if ($item === '' || ! str_contains($item, ':')) {
                continue;
            }
            [$name, $bandwidth] = array_map('trim', explode(':', $item, 2));
            $parts = array_map('trim', explode('/', $bandwidth));
            $down = is_numeric($parts[0] ?? null) ? (float) $parts[0] : null;
            if ($down === null) {
                continue;
            }
            $up = is_numeric($parts[1] ?? null) ? (float) $parts[1] : $down;
            $cfg['per'][] = [
                'label' => strtolower($name),
                'norm' => preg_replace('/[^a-z0-9]+/', '', strtolower($name)),
                'mbps' => max($down, $up),
            ];
        }
        return $cfg;
    };

    $speedCfg = $parseSpeed(getenv('BIGSCREEN_ISP_MAX_BANDWIDTH') ?: '1000');
    if (empty($targets) || empty($keywords)) {
        echo "  WAN speed override skipped: missing FIREWALL_SNMP_TARGETS or FIREWALL_WAN_IF_FILTER\n";
        exit(0);
    }

    $devicesCols = $columns('devices');
    $portsCols = $columns('ports');
    [$attribTable, $attribCols] = $findAttribTable();
    if (! $has($devicesCols, 'device_id') || ! $has($portsCols, 'port_id')) {
        echo "  WAN speed override skipped: unsupported LibreNMS schema\n";
        exit(0);
    }

    $findDevice = function (array $target) use ($pdo, $devicesCols, $has): ?array {
        $where = [];
        $values = [];
        foreach (['hostname' => $target['ip'], 'display' => $target['name'], 'sysName' => $target['name']] as $column => $value) {
            if ($value !== '' && $has($devicesCols, $column)) {
                $where[] = "`{$column}` = ?";
                $values[] = $value;
            }
        }
        if (empty($where)) {
            return null;
        }
        $stmt = $pdo->prepare('SELECT * FROM devices WHERE ' . implode(' OR ', $where) . ' LIMIT 1');
        $stmt->execute($values);
        $row = $stmt->fetch(PDO::FETCH_ASSOC);
        return $row ?: null;
    };

    $matchesWan = function (string $text) use ($keywords): bool {
        $lower = strtolower($text);
        foreach ($keywords as $kw) {
            if ($kw !== '' && str_contains($lower, $kw)) {
                return true;
            }
        }
        return false;
    };

    $portSpeed = function (string $text) use ($speedCfg): ?float {
        $lower = strtolower($text);
        $norm = preg_replace('/[^a-z0-9]+/', '', $lower);
        foreach ($speedCfg['per'] as $entry) {
            if (($entry['label'] !== '' && str_contains($lower, $entry['label'])) ||
                ($entry['norm'] !== '' && str_contains($norm, $entry['norm']))) {
                return $entry['mbps'];
            }
        }
        return $speedCfg['default'];
    };

    $upsertAttrib = function (int $deviceId, string $type, string $value) use ($pdo, $quoteIdent, $attribTable, $attribCols, $has): void {
        if (! $attribTable || ! $has($attribCols, 'device_id') ||
            ! $has($attribCols, 'attrib_type') || ! $has($attribCols, 'attrib_value')) {
            return;
        }
        $tableSql = $quoteIdent($attribTable);
        $select = $pdo->prepare("SELECT attrib_id FROM {$tableSql} WHERE device_id = ? AND attrib_type = ? LIMIT 1");
        $select->execute([$deviceId, $type]);
        $attribId = $select->fetchColumn();
        if ($attribId) {
            $update = $pdo->prepare("UPDATE {$tableSql} SET attrib_value = ? WHERE attrib_id = ?");
            $update->execute([$value, $attribId]);
            return;
        }
        $insert = $pdo->prepare("INSERT INTO {$tableSql} (device_id, attrib_type, attrib_value) VALUES (?, ?, ?)");
        $insert->execute([$deviceId, $type, $value]);
    };

    $updated = 0;
    foreach ($targets as $target) {
        $device = $findDevice($target);
        if (! $device) {
            echo "  {$target['name']} ({$target['ip']}): device not found yet\n";
            continue;
        }
        $deviceId = (int) $device['device_id'];
        $where = ['device_id = ?'];
        $values = [$deviceId];
        if ($has($portsCols, 'deleted')) {
            $where[] = '(deleted = 0 OR deleted IS NULL)';
        }
        $stmt = $pdo->prepare('SELECT * FROM ports WHERE ' . implode(' AND ', $where));
        $stmt->execute($values);
        $matched = 0;
        foreach ($stmt->fetchAll(PDO::FETCH_ASSOC) as $port) {
            $labelParts = [];
            foreach (['ifAlias', 'ifName', 'ifDescr'] as $column) {
                if ($has($portsCols, $column) && ! empty($port[$column])) {
                    $labelParts[] = $port[$column];
                }
            }
            $label = trim(implode(' ', $labelParts));
            if ($label === '' || ! $matchesWan($label)) {
                continue;
            }
            $mbps = $portSpeed($label);
            if ($mbps === null || $mbps <= 0) {
                echo "  {$target['name']} ({$target['ip']}): {$label} matched WAN, but no bandwidth entry matched\n";
                continue;
            }
            $bps = (string) (int) round($mbps * 1000000);
            $ifName = (string) ($port['ifName'] ?? '');
            if ($ifName !== '') {
                $upsertAttrib($deviceId, "ifSpeed:{$ifName}", $bps);
            }

            $sets = [];
            $params = [];
            if ($has($portsCols, 'ifSpeed')) {
                $sets[] = 'ifSpeed = ?';
                $params[] = $bps;
            }
            if ($has($portsCols, 'ifHighSpeed')) {
                $sets[] = 'ifHighSpeed = ?';
                $params[] = (string) (int) round($mbps);
            }
            if ($sets) {
                $params[] = (int) $port['port_id'];
                $upd = $pdo->prepare('UPDATE ports SET ' . implode(', ', $sets) . ' WHERE port_id = ?');
                $upd->execute($params);
            }
            $matched++;
            $updated++;
            echo "  {$target['name']} ({$target['ip']}): {$label} => {$mbps} Mbps\n";
        }
        if ($matched === 0) {
            echo "  {$target['name']} ({$target['ip']}): no WAN ports matched FIREWALL_WAN_IF_FILTER\n";
        }
    }
    if ($updated === 0) {
        echo "  WAN speed override: no ports updated yet; rerun librenms-config after LibreNMS discovers firewall ports\n";
    }
} catch (Throwable $e) {
    echo "  WARNING: WAN speed override failed: " . $e->getMessage() . "\n";
}
PHP
}

configure_home_dashboard() {
  if [ "${LIBRENMS_HOME_DASHBOARD_AUTO:-true}" != "true" ]; then
    echo ""
    echo "  LibreNMS home dashboard skipped (LIBRENMS_HOME_DASHBOARD_AUTO=false)"
    return 0
  fi

  echo ""
  echo "  Configuring LibreNMS home dashboard..."

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

    $quoteIdent = fn(string $name): string => '`' . str_replace('`', '``', $name) . '`';
    $columns = function (string $table) use ($pdo, $quoteIdent): array {
        try {
            $stmt = $pdo->query("SHOW COLUMNS FROM " . $quoteIdent($table));
        } catch (Throwable $e) {
            return [];
        }
        $cols = [];
        foreach ($stmt->fetchAll(PDO::FETCH_ASSOC) as $row) {
            $cols[$row['Field']] = true;
        }

        return $cols;
    };
    $has = fn(array $cols, string $name): bool => isset($cols[$name]);
    $truthy = fn(string $name, string $default = 'true'): bool => in_array(
        strtolower(trim((string) (getenv($name) !== false ? getenv($name) : $default))),
        ['1', 'true', 'yes', 'on'],
        true
    );

    $dashboardCols = $columns('dashboards');
    $widgetCols = $columns('users_widgets');
    $userCols = $columns('users');
    $prefCols = $columns('users_prefs');
    $deviceCols = $columns('devices');
    $portCols = $columns('ports');

    foreach ([
        'dashboards' => $dashboardCols,
        'users_widgets' => $widgetCols,
        'users' => $userCols,
    ] as $table => $cols) {
        if (empty($cols)) {
            echo "  Home dashboard skipped: table {$table} not found\n";
            exit(0);
        }
    }
    if (! $has($dashboardCols, 'dashboard_id') || ! $has($widgetCols, 'dashboard_id')) {
        echo "  Home dashboard skipped: unsupported LibreNMS dashboard schema\n";
        exit(0);
    }

    $dashboardName = trim(getenv('LIBRENMS_HOME_DASHBOARD_NAME') ?: '赛事网络总览');
    $adminName = getenv('LIBRENMS_ADMIN_USER') ?: 'admin';
    $userIdColumn = $has($userCols, 'user_id') ? 'user_id' : ($has($userCols, 'id') ? 'id' : null);
    $userNameColumn = $has($userCols, 'username') ? 'username' : ($has($userCols, 'name') ? 'name' : null);
    if (! $userIdColumn || ! $userNameColumn) {
        echo "  Home dashboard skipped: unsupported users schema\n";
        exit(0);
    }

    $stmt = $pdo->prepare("SELECT {$quoteIdent($userIdColumn)} FROM users WHERE {$quoteIdent($userNameColumn)} = ? LIMIT 1");
    $stmt->execute([$adminName]);
    $adminId = (int) $stmt->fetchColumn();
    if ($adminId <= 0) {
        echo "  Home dashboard skipped: admin user {$adminName} not found yet\n";
        exit(0);
    }

    $stmt = $pdo->prepare('SELECT dashboard_id FROM dashboards WHERE user_id = ? AND dashboard_name = ? LIMIT 1');
    $stmt->execute([$adminId, $dashboardName]);
    $dashboardId = (int) $stmt->fetchColumn();
    if ($dashboardId <= 0) {
        $columnsToInsert = ['user_id', 'dashboard_name'];
        $values = [$adminId, $dashboardName];
        if ($has($dashboardCols, 'access')) {
            $columnsToInsert[] = 'access';
            $values[] = 0;
        }
        $sql = 'INSERT INTO dashboards (' . implode(', ', array_map($quoteIdent, $columnsToInsert)) . ') VALUES (' . implode(', ', array_fill(0, count($columnsToInsert), '?')) . ')';
        $insert = $pdo->prepare($sql);
        $insert->execute($values);
        $dashboardId = (int) $pdo->lastInsertId();
    }

    if (! empty($prefCols) && $has($prefCols, 'user_id') && $has($prefCols, 'pref') && $has($prefCols, 'value')) {
        $pref = $pdo->prepare('INSERT INTO users_prefs (user_id, pref, value) VALUES (?, ?, ?) ON DUPLICATE KEY UPDATE value = VALUES(value)');
        $pref->execute([$adminId, 'dashboard', (string) $dashboardId]);
    }

    $managed = 'monitor-autoconfig';
    $selectWidgets = $pdo->prepare('SELECT user_widget_id, widget, title, settings FROM users_widgets WHERE dashboard_id = ?');
    $selectWidgets->execute([$dashboardId]);
    $existingWidgets = $selectWidgets->fetchAll(PDO::FETCH_ASSOC);
    $managedTitles = [
        '设备摘要', '设备状态', '设备在线数', '接口流量排名', '设备流量排名', '交换机 CPU 排名', '服务器状态',
    ];
    $legacyDefaultTitleSet = array_fill_keys($managedTitles, true);
    $titleKeyForWidget = function (string $widget, array $settings): string {
        if (! empty($settings['autoconfig_key'])) {
            return (string) $settings['autoconfig_key'];
        }
        if ($widget === 'generic-graph' &&
            (string) ($settings['graph_type'] ?? '') === 'port_bits' &&
            (string) ($settings['graph_port'] ?? '') !== '') {
            return 'wan_port_' . (string) $settings['graph_port'];
        }
        if ($widget === 'availability-map') {
            return 'availability';
        }
        if ($widget === 'top-interfaces') {
            return 'top_interfaces';
        }
        if ($widget === 'top-devices') {
            $query = (string) ($settings['top_query'] ?? '');
            if ($query === 'traffic') {
                return 'top_devices_traffic';
            }
            if ($query === 'cpu') {
                return 'top_devices_cpu';
            }
        }
        if ($widget === 'server-stats') {
            return 'server_stats';
        }

        return '';
    };

    $json = function (array $settings) use ($managed): string {
        $settings = array_merge(['autoconfig' => $managed], $settings);

        return json_encode($settings, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    };

    $addWidget = function (string $widget, string $title, int $col, int $row, int $sizeX, int $sizeY, array $settings = []) use ($pdo, $adminId, $dashboardId, $json): void {
        $stmt = $pdo->prepare(
            'INSERT INTO users_widgets (user_id, widget, col, row, size_x, size_y, title, refresh, settings, dashboard_id)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)'
        );
        $stmt->execute([
            $adminId,
            $widget,
            $col,
            $row,
            $sizeX,
            $sizeY,
            $title,
            (int) ($settings['refresh'] ?? 60),
            $json($settings),
            $dashboardId,
        ]);
    };

    $deviceDisplay = function (array $device): string {
        foreach (['display', 'sysName', 'hostname'] as $field) {
            if (! empty($device[$field])) {
                return (string) $device[$field];
            }
        }

        return (string) ($device['device_id'] ?? 'device');
    };

    $targets = [];
    foreach (explode(',', getenv('FIREWALL_SNMP_TARGETS') ?: '') as $raw) {
        $raw = trim($raw);
        if ($raw === '') {
            continue;
        }
        if (str_contains($raw, ':')) {
            [$name, $ip] = array_map('trim', explode(':', $raw, 2));
        } else {
            $name = '';
            $ip = $raw;
        }
        $ip = preg_replace('/-.*/', '', $ip);
        if ($ip !== '') {
            $targets[] = ['name' => $name, 'ip' => $ip];
        }
    }

    $keywords = array_values(array_filter(array_map(
        fn($v) => strtolower(trim($v)),
        explode(',', getenv('FIREWALL_WAN_IF_FILTER') ?: 'telecom,telcom,unicom,isp,WAN')
    )));
    $matchesWan = function (string $text) use ($keywords): bool {
        $lower = strtolower($text);
        foreach ($keywords as $kw) {
            if ($kw !== '' && str_contains($lower, $kw)) {
                return true;
            }
        }

        return false;
    };

    $findDevice = function (array $target) use ($pdo, $deviceCols, $has): ?array {
        $where = [];
        $values = [];
        foreach (['hostname' => $target['ip'], 'display' => $target['name'], 'sysName' => $target['name']] as $column => $value) {
            if ($value !== '' && $has($deviceCols, $column)) {
                $where[] = "`{$column}` = ?";
                $values[] = $value;
            }
        }
        if (! $where) {
            return null;
        }
        $stmt = $pdo->prepare('SELECT * FROM devices WHERE ' . implode(' OR ', $where) . ' LIMIT 1');
        $stmt->execute($values);
        $row = $stmt->fetch(PDO::FETCH_ASSOC);

        return $row ?: null;
    };

    $wanCards = [];
    if ($truthy('LIBRENMS_HOME_WAN_CARDS', 'true') &&
        ! empty($targets) &&
        ! empty($keywords) &&
        ! empty($deviceCols) &&
        ! empty($portCols) &&
        $has($portCols, 'port_id') &&
        $has($portCols, 'device_id')) {
        foreach ($targets as $target) {
            $device = $findDevice($target);
            if (! $device) {
                continue;
            }
            $where = ['device_id = ?'];
            $values = [(int) $device['device_id']];
            if ($has($portCols, 'deleted')) {
                $where[] = '(deleted = 0 OR deleted IS NULL)';
            }
            $stmt = $pdo->prepare('SELECT * FROM ports WHERE ' . implode(' AND ', $where) . ' ORDER BY port_id');
            $stmt->execute($values);
            foreach ($stmt->fetchAll(PDO::FETCH_ASSOC) as $port) {
                $labelParts = [];
                foreach (['ifAlias', 'ifName', 'ifDescr'] as $column) {
                    if ($has($portCols, $column) && ! empty($port[$column])) {
                        $value = trim((string) $port[$column]);
                        if ($value !== '' && ! in_array($value, $labelParts, true)) {
                            $labelParts[] = $value;
                        }
                    }
                }
                $label = trim(implode(' ', $labelParts));
                if ($label === '' || ! $matchesWan($label)) {
                    continue;
                }
                $friendly = '';
                foreach (['ifAlias', 'ifName', 'ifDescr'] as $column) {
                    if ($has($portCols, $column) && ! empty($port[$column])) {
                        $friendly = trim((string) $port[$column]);
                        break;
                    }
                }
                $title = 'WAN · ' . $deviceDisplay($device) . ' · ' . ($friendly ?: ('port ' . $port['port_id']));
                $wanCards[] = [
                    'title' => $title,
                    'port_id' => (int) $port['port_id'],
                ];
            }
        }
    }

    $limit = max(0, (int) (getenv('LIBRENMS_HOME_WAN_CARD_LIMIT') ?: 8));
    if ($limit > 0) {
        $wanCards = array_slice($wanCards, 0, $limit);
    }

    $defaultTitleByKey = [
        'availability' => '设备在线数',
        'top_interfaces' => '接口流量排名',
        'top_devices_traffic' => '设备流量排名',
        'top_devices_cpu' => '交换机 CPU 排名',
        'server_stats' => '服务器状态',
    ];
    foreach ($wanCards as $card) {
        $defaultTitleByKey['wan_port_' . $card['port_id']] = $card['title'];
    }

    $preservedTitles = [];
    $deleteIds = [];
    foreach ($existingWidgets as $row) {
        $settings = [];
        if (! empty($row['settings'])) {
            $decoded = json_decode((string) $row['settings'], true);
            if (is_array($decoded)) {
                $settings = $decoded;
            }
        }
        $title = trim((string) ($row['title'] ?? ''));
        $widget = (string) ($row['widget'] ?? '');
        $key = $titleKeyForWidget($widget, $settings);
        $isCurrentWanGraph = str_starts_with($key, 'wan_port_') && isset($defaultTitleByKey[$key]);
        if ($key !== '' &&
            $title !== '' &&
            ! isset($legacyDefaultTitleSet[$title]) &&
            ! str_starts_with($title, 'WAN · ') &&
            (($defaultTitleByKey[$key] ?? '') !== $title)) {
            $preservedTitles[$key] = $title;
        }
        if (($settings['autoconfig'] ?? '') === $managed ||
            $isCurrentWanGraph ||
            str_starts_with($title, 'WAN · ') ||
            in_array($title, $managedTitles, true)) {
            $deleteIds[] = (int) $row['user_widget_id'];
        }
    }
    if ($deleteIds) {
        $placeholders = implode(',', array_fill(0, count($deleteIds), '?'));
        $delete = $pdo->prepare("DELETE FROM users_widgets WHERE user_widget_id IN ({$placeholders})");
        $delete->execute($deleteIds);
    }
    $titleFor = function (string $key, string $default) use (&$preservedTitles): string {
        return $preservedTitles[$key] ?? $default;
    };

    $row = 1;
    foreach ($wanCards as $idx => $card) {
        $key = 'wan_port_' . $card['port_id'];
        $title = $titleFor($key, $card['title']);
        $col = ($idx % 2) === 0 ? 1 : 7;
        $graphRow = $row + (int) floor($idx / 2) * 4;
        $addWidget('generic-graph', $title, $col, $graphRow, 6, 4, [
            'autoconfig_key' => $key,
            'title' => $title,
            'refresh' => 60,
            'graph_type' => 'port_bits',
            'graph_range' => 'day',
            'graph_legend' => 'yes',
            'graph_port' => (string) $card['port_id'],
            'graph_device' => null,
            'graph_application' => null,
            'graph_munin' => null,
            'graph_service' => null,
            'graph_customoid' => null,
            'graph_ports' => [],
            'graph_sensors' => [],
            'graph_stack' => 'no',
            'graph_custom' => [],
            'graph_manual' => null,
            'graph_bill' => null,
        ]);
    }
    if ($wanCards) {
        $row += (int) ceil(count($wanCards) / 2) * 4;
    }

    $key = 'availability';
    $addWidget('availability-map', $titleFor($key, '设备在线数'), 1, $row, 12, 2, [
        'autoconfig_key' => $key,
        'refresh' => 60,
    ]);
    $row += 2;

    if ($truthy('LIBRENMS_HOME_TOP_INTERFACES', 'true')) {
        $key = 'top_interfaces';
        $addWidget('top-interfaces', $titleFor($key, '接口流量排名'), 1, $row, 6, 3, [
            'autoconfig_key' => $key,
            'refresh' => 60,
            'interface_count' => 8,
            'time_interval' => 15,
            'interface_filter' => null,
            'device_group' => null,
            'port_group' => null,
        ]);
    }
    if ($truthy('LIBRENMS_HOME_TOP_DEVICES', 'true')) {
        $key = 'top_devices_traffic';
        $title = $titleFor($key, '设备流量排名');
        $addWidget('top-devices', $title, 7, $row, 6, 3, [
            'autoconfig_key' => $key,
            'title' => $title,
            'refresh' => 60,
            'top_query' => 'traffic',
            'sort_order' => 'desc',
            'device_count' => 8,
            'time_interval' => 15,
            'device_group' => null,
        ]);
    }
    $row += 3;

    if ($truthy('LIBRENMS_HOME_SWITCH_CPU', 'true')) {
        $key = 'top_devices_cpu';
        $title = $titleFor($key, '交换机 CPU 排名');
        $addWidget('top-devices', $title, 1, $row, 12, 3, [
            'autoconfig_key' => $key,
            'title' => $title,
            'refresh' => 60,
            'top_query' => 'cpu',
            'sort_order' => 'desc',
            'device_count' => 10,
            'time_interval' => 15,
            'device_group' => null,
        ]);
        $row += 3;
    }

    $serverMode = strtolower(trim((string) (getenv('LIBRENMS_HOME_SERVER_STATS') ?: 'auto')));
    $hasServerTargets = trim((string) (getenv('SERVER_PING') ?: '')) !== '';
    if ($serverMode === 'true' || ($serverMode === 'auto' && $hasServerTargets)) {
        $key = 'server_stats';
        $addWidget('server-stats', $titleFor($key, '服务器状态'), 1, $row, 12, 3, [
            'autoconfig_key' => $key,
            'refresh' => 60,
        ]);
    }

    $total = 1 + count($wanCards)
        + ($truthy('LIBRENMS_HOME_TOP_INTERFACES', 'true') ? 1 : 0)
        + ($truthy('LIBRENMS_HOME_TOP_DEVICES', 'true') ? 1 : 0)
        + ($truthy('LIBRENMS_HOME_SWITCH_CPU', 'true') ? 1 : 0)
        + (($serverMode === 'true' || ($serverMode === 'auto' && $hasServerTargets)) ? 1 : 0);
    echo "  Home dashboard '{$dashboardName}' ready (id={$dashboardId}, widgets={$total}, WAN cards=" . count($wanCards) . ")\n";
    if (count($wanCards) === 0 && $truthy('LIBRENMS_HOME_WAN_CARDS', 'true')) {
        echo "  Home dashboard: no WAN ports matched yet; rerun librenms-config after firewall port discovery if needed\n";
    }
} catch (Throwable $e) {
    echo "  WARNING: Home dashboard setup failed: " . $e->getMessage() . "\n";
}
PHP
}

configure_down_port_ignores() {
  if [ "${LIBRENMS_IGNORE_DOWN_PORTS:-true}" != "true" ]; then
    echo ""
    echo "  LibreNMS down-port ignore skipped (LIBRENMS_IGNORE_DOWN_PORTS=false)"
    return 0
  fi

  echo ""
  echo "  Applying LibreNMS down-port ignore for unused ports..."

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

    $quoteIdent = fn(string $name): string => '`' . str_replace('`', '``', $name) . '`';
    $columns = function (string $table) use ($pdo, $quoteIdent): array {
        try {
            $stmt = $pdo->query("SHOW COLUMNS FROM " . $quoteIdent($table));
        } catch (Throwable $e) {
            return [];
        }
        $cols = [];
        foreach ($stmt->fetchAll(PDO::FETCH_ASSOC) as $row) {
            $cols[$row['Field']] = true;
        }
        return $cols;
    };
    $has = fn(array $cols, string $name): bool => isset($cols[$name]);
    $findAttribTable = function () use ($columns): array {
        foreach (['device_attribs', 'devices_attribs'] as $table) {
            $cols = $columns($table);
            if (! empty($cols)) {
                return [$table, $cols];
            }
        }
        return [null, []];
    };

    $portsCols = $columns('ports');
    $devicesCols = $columns('devices');
    [$attribTable, $attribCols] = $findAttribTable();

    if (! $has($portsCols, 'port_id') || ! $has($portsCols, 'device_id') ||
        ! $has($portsCols, 'ignore') || ! $has($portsCols, 'ifOperStatus')) {
        echo "  Down-port ignore skipped: unsupported LibreNMS ports schema\n";
        exit(0);
    }
    if (! $has($devicesCols, 'device_id')) {
        echo "  Down-port ignore skipped: unsupported LibreNMS devices schema\n";
        exit(0);
    }

    $hasAttrib = $has($attribCols, 'attrib_id') && $has($attribCols, 'device_id') &&
        $has($attribCols, 'attrib_type') && $has($attribCols, 'attrib_value');
    $attribTableSql = $hasAttrib && $attribTable ? $quoteIdent($attribTable) : '';
    $attrType = 'autoconfig_ignored_down_ports';

    $statusText = fn($raw): string => strtolower(trim((string) $raw));
    $isUp = fn(string $status): bool => in_array($status, ['up', '1'], true);
    $isDown = fn(string $status): bool => in_array($status, ['down', '2', 'lowerlayerdown', '7', 'notpresent', '6', 'dormant', '5'], true);
    $portLabel = function (array $port) use ($portsCols, $has): string {
        $parts = [];
        foreach (['ifName', 'ifDescr', 'ifAlias'] as $column) {
            if ($has($portsCols, $column) && ! empty($port[$column])) {
                $parts[] = (string) $port[$column];
            }
        }
        return trim(implode(' ', array_unique($parts))) ?: ('port_id=' . ($port['port_id'] ?? '?'));
    };

    $loadAuto = function (int $deviceId) use ($pdo, $hasAttrib, $attribTableSql, $attrType): array {
        if (! $hasAttrib) {
            return [];
        }
        $stmt = $pdo->prepare("SELECT attrib_value FROM {$attribTableSql} WHERE device_id = ? AND attrib_type = ? LIMIT 1");
        $stmt->execute([$deviceId, $attrType]);
        $raw = $stmt->fetchColumn();
        $items = json_decode((string) $raw, true);
        if (! is_array($items)) {
            return [];
        }
        $map = [];
        foreach ($items as $id) {
            if (is_numeric($id)) {
                $map[(int) $id] = true;
            }
        }
        return $map;
    };

    $saveAuto = function (int $deviceId, array $auto) use ($pdo, $hasAttrib, $attribTableSql, $attrType): void {
        if (! $hasAttrib) {
            return;
        }
        $ids = array_values(array_map('intval', array_keys($auto)));
        sort($ids);
        $value = json_encode($ids);
        $select = $pdo->prepare("SELECT attrib_id FROM {$attribTableSql} WHERE device_id = ? AND attrib_type = ? LIMIT 1");
        $select->execute([$deviceId, $attrType]);
        $attribId = $select->fetchColumn();
        if ($attribId) {
            $update = $pdo->prepare("UPDATE {$attribTableSql} SET attrib_value = ? WHERE attrib_id = ?");
            $update->execute([$value, $attribId]);
            return;
        }
        $insert = $pdo->prepare("INSERT INTO {$attribTableSql} (device_id, attrib_type, attrib_value) VALUES (?, ?, ?)");
        $insert->execute([$deviceId, $attrType, $value]);
    };

    $deviceLabel = function (array $device): string {
        foreach (['display', 'sysName', 'hostname'] as $column) {
            if (! empty($device[$column])) {
                return (string) $device[$column];
            }
        }
        return 'device_id=' . ($device['device_id'] ?? '?');
    };

    $devices = $pdo->query('SELECT DISTINCT d.* FROM devices d JOIN ports p ON p.device_id = d.device_id')->fetchAll(PDO::FETCH_ASSOC);
    $updateIgnore = $pdo->prepare('UPDATE ports SET `ignore` = ? WHERE port_id = ?');

    $ignoredTotal = 0;
    $restoredTotal = 0;
    foreach ($devices as $device) {
        $deviceId = (int) $device['device_id'];
        $auto = $loadAuto($deviceId);
        $seen = [];
        $where = ['device_id = ?'];
        if ($has($portsCols, 'deleted')) {
            $where[] = '(`deleted` = 0 OR `deleted` IS NULL)';
        }
        $stmt = $pdo->prepare('SELECT * FROM ports WHERE ' . implode(' AND ', $where));
        $stmt->execute([$deviceId]);

        $ignored = [];
        $restored = [];
        foreach ($stmt->fetchAll(PDO::FETCH_ASSOC) as $port) {
            $portId = (int) $port['port_id'];
            $seen[$portId] = true;
            $status = $statusText($port['ifOperStatus'] ?? '');
            $currentlyIgnored = (int) ($port['ignore'] ?? 0) === 1;

            if ($isUp($status)) {
                if (isset($auto[$portId])) {
                    if ($currentlyIgnored) {
                        $updateIgnore->execute([0, $portId]);
                        $restored[] = $portLabel($port);
                        $restoredTotal++;
                    }
                    unset($auto[$portId]);
                }
                continue;
            }

            if ($isDown($status) && ! $currentlyIgnored) {
                $updateIgnore->execute([1, $portId]);
                $auto[$portId] = true;
                $ignored[] = $portLabel($port);
                $ignoredTotal++;
            }
        }

        foreach (array_keys($auto) as $portId) {
            if (! isset($seen[$portId])) {
                unset($auto[$portId]);
            }
        }
        $saveAuto($deviceId, $auto);

        if ($ignored) {
            $sample = implode(', ', array_slice($ignored, 0, 4));
            $more = count($ignored) > 4 ? '...' : '';
            echo "  " . $deviceLabel($device) . ": ignored " . count($ignored) . " down ports ({$sample}{$more})\n";
        }
        if ($restored) {
            $sample = implode(', ', array_slice($restored, 0, 4));
            $more = count($restored) > 4 ? '...' : '';
            echo "  " . $deviceLabel($device) . ": restored " . count($restored) . " ports now up ({$sample}{$more})\n";
        }
    }

    echo "  Down-port ignore complete: ignored={$ignoredTotal}, restored={$restoredTotal}\n";
} catch (Throwable $e) {
    echo "  WARNING: Down-port ignore failed: " . $e->getMessage() . "\n";
}
PHP
}

configure_stp_noise_suppression() {
  if [ "$LIBRENMS_SUPPRESS_STP_EVENTS" != "true" ]; then
    echo ""
    echo "  LibreNMS STP event suppression skipped (LIBRENMS_SUPPRESS_STP_EVENTS=false)"
    return 0
  fi

  echo ""
  echo "  Suppressing LibreNMS STP topology-change noise..."
  run_lnms config:set poller_modules.stp false >/dev/null 2>&1 && \
    echo "  STP poller module: disabled" || \
    echo "  WARNING: Could not disable STP poller module"
  run_lnms config:set discovery_modules.stp false >/dev/null 2>&1 && \
    echo "  STP discovery module: disabled" || true

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

    $exists = $pdo->prepare('SHOW TABLES LIKE ?');
    $exists->execute(['eventlog']);
    if (! $exists->fetchColumn()) {
        echo "  STP event cleanup skipped: eventlog table not found\n";
        exit(0);
    }
    $columns = [];
    foreach ($pdo->query('SHOW COLUMNS FROM eventlog') as $column) {
        $columns[$column['Field']] = true;
    }
    if (! isset($columns['type'])) {
        echo "  STP event cleanup skipped: eventlog.type not found\n";
        exit(0);
    }
    $stmt = $pdo->prepare("DELETE FROM eventlog WHERE type = 'stp'");
    $stmt->execute();
    echo "  STP event cleanup: removed " . $stmt->rowCount() . " old topology-change event(s)\n";
} catch (Throwable $e) {
    echo "  WARNING: STP event cleanup failed: " . $e->getMessage() . "\n";
}
PHP
}

ALERT_TRANSPORT_IDS=""

configure_feishu_transport() {
  echo ""
  echo "[5/6] Setting up Feishu alert transport..."

  if [ -z "$FEISHU_ROBOT_TOKEN" ]; then
    echo "  FEISHU_ROBOT_TOKEN not set, alert rules will be created without push transport"
    ALERT_TRANSPORT_IDS=""
    return 0
  fi

  _ft_out=$(php <<'PHP'
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
        'api-method' => 'POST',
        'api-as-form' => false,
        'api-url' => 'http://alertmanager-feishu-bridge:5005/librenms',
        'api-options' => '',
        'api-headers' => 'Content-Type=application/json',
        'api-body' => '{"state":"{{ state }}","name":"{{ name }}","severity":"{{ severity }}","hostname":"{{ hostname }}","sysName":"{{ sysName }}","ip":"{{ ip }}","timestamp":"{{ timestamp }}","uid":"{{ uid }}","elapsed":"{{ elapsed }}"}',
        'api-auth-username' => '',
        'api-auth-password' => '',
    ], JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);

    if ($transportId) {
        $upd = $pdo->prepare('UPDATE alert_transports SET transport_type = ?, is_default = 1, transport_config = ? WHERE transport_id = ?');
        $upd->execute(['api', $config, $transportId]);
        echo "updated:{$transportId}";
        exit(0);
    }

    $ins = $pdo->prepare('INSERT INTO alert_transports (transport_name, transport_type, is_default, transport_config) VALUES (?, ?, 1, ?)');
    $ins->execute([$name, 'api', $config]);
    echo 'created:' . $pdo->lastInsertId();
    exit(0);
} catch (Throwable $e) {
    echo 'ERROR: ' . $e->getMessage();
    exit(1);
}
PHP
  ) || true

  case "$_ft_out" in
    created:*)
      ALERT_TRANSPORT_IDS="${_ft_out#created:}"
      echo "  Feishu transport created (id=$ALERT_TRANSPORT_IDS, → bridge /librenms)"
      ;;
    updated:*)
      ALERT_TRANSPORT_IDS="${_ft_out#updated:}"
      echo "  Feishu transport updated (id=$ALERT_TRANSPORT_IDS, → bridge /librenms)"
      ;;
    *)
      ALERT_TRANSPORT_IDS=""
      echo "  WARNING: Could not create Feishu transport: $_ft_out"
      ;;
  esac
}

cleanup_legacy_alert_rules() {
  echo ""
  echo "  Cleaning legacy broken LibreNMS alert rules..."

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

    $quoteIdent = fn(string $name): string => '`' . str_replace('`', '``', $name) . '`';
    $columns = function (string $table) use ($pdo, $quoteIdent): array {
        try {
            $stmt = $pdo->query("SHOW COLUMNS FROM " . $quoteIdent($table));
        } catch (Throwable $e) {
            return [];
        }
        $cols = [];
        foreach ($stmt->fetchAll(PDO::FETCH_ASSOC) as $row) {
            $cols[$row['Field']] = true;
        }
        return $cols;
    };
    $tableExists = function (string $table) use ($pdo): bool {
        $stmt = $pdo->prepare('SHOW TABLES LIKE ?');
        $stmt->execute([$table]);
        return (bool) $stmt->fetchColumn();
    };
    $has = fn(array $cols, string $name): bool => isset($cols[$name]);

    if (! $tableExists('alert_rules')) {
        echo "  Legacy alert cleanup skipped: alert_rules table not found\n";
        exit(0);
    }

    $ruleCols = $columns('alert_rules');
    $idCol = $has($ruleCols, 'id') ? 'id' : ($has($ruleCols, 'rule_id') ? 'rule_id' : null);
    if ($idCol === null) {
        echo "  Legacy alert cleanup skipped: unsupported alert_rules schema\n";
        exit(0);
    }

    $conditions = [];
    $params = [];
    if ($has($ruleCols, 'name')) {
        foreach (['接口错误告警', '接口丢弃告警', '高丢包告警'] as $name) {
            $conditions[] = $quoteIdent('name') . ' = ?';
            $params[] = $name;
        }
    }
    if ($has($ruleCols, 'rule')) {
        foreach (['ifInDiscards_rate', 'ifOutDiscards_rate', 'ifInErrors_rate', 'ifOutErrors_rate', 'device_perf.loss'] as $token) {
            $conditions[] = $quoteIdent('rule') . ' LIKE ?';
            $params[] = '%' . $token . '%';
        }
    }

    if (! $conditions) {
        echo "  Legacy alert cleanup skipped: no name/rule columns\n";
        exit(0);
    }

    $cleanupLegacyEvents = function () use ($pdo, $tableExists, $columns, $has): int {
        if (! $tableExists('eventlog')) {
            return 0;
        }
        $eventCols = $columns('eventlog');
        if (! $has($eventCols, 'message')) {
            return 0;
        }
        $tokens = ['ifInDiscards_rate', 'ifOutDiscards_rate', 'ifInErrors_rate', 'ifOutErrors_rate', 'device_perf.loss', '接口错误告警', '接口丢弃告警', '高丢包告警'];
        $where = [];
        $params = [];
        foreach ($tokens as $token) {
            $where[] = '`message` LIKE ?';
            $params[] = '%' . $token . '%';
        }
        $del = $pdo->prepare('DELETE FROM eventlog WHERE ' . implode(' OR ', $where));
        $del->execute($params);
        return $del->rowCount();
    };
    $removedEvents = $cleanupLegacyEvents();

    $selectCols = [$quoteIdent($idCol)];
    if ($has($ruleCols, 'name')) {
        $selectCols[] = $quoteIdent('name');
    }
    if ($has($ruleCols, 'rule')) {
        $selectCols[] = $quoteIdent('rule');
    }
    $stmt = $pdo->prepare(
        'SELECT ' . implode(', ', $selectCols) .
        ' FROM alert_rules WHERE ' . implode(' OR ', $conditions)
    );
    $stmt->execute($params);
    $rules = $stmt->fetchAll(PDO::FETCH_ASSOC);
    if (! $rules) {
        echo "  Legacy alert cleanup: no broken legacy rules found\n";
        if ($removedEvents > 0) {
            echo "  Legacy alert cleanup: removed {$removedEvents} old broken alert event(s)\n";
        }
        exit(0);
    }

    $ids = [];
    $names = [];
    foreach ($rules as $rule) {
        $ids[] = (int) $rule[$idCol];
        $name = trim((string) ($rule['name'] ?? ''));
        $names[] = $name !== '' ? $name : ('rule_id=' . $rule[$idCol]);
    }
    $ids = array_values(array_unique($ids));
    $placeholders = implode(',', array_fill(0, count($ids), '?'));

    foreach (['alert_device_map', 'alert_group_map', 'alert_location_map', 'alert_transport_map', 'alert_schedule', 'alert_template_map'] as $table) {
        if (! $tableExists($table)) {
            continue;
        }
        $cols = $columns($table);
        foreach (['rule_id', 'alert_rule_id'] as $col) {
            if ($has($cols, $col)) {
                $del = $pdo->prepare('DELETE FROM ' . $quoteIdent($table) . ' WHERE ' . $quoteIdent($col) . " IN ({$placeholders})");
                $del->execute($ids);
                break;
            }
        }
    }

    $delRules = $pdo->prepare('DELETE FROM alert_rules WHERE ' . $quoteIdent($idCol) . " IN ({$placeholders})");
    $delRules->execute($ids);
    echo '  Legacy alert cleanup: removed ' . count($ids) . ' broken rule(s): ' . implode(', ', array_unique($names)) . "\n";
    if ($removedEvents > 0) {
        echo "  Legacy alert cleanup: removed {$removedEvents} old broken alert event(s)\n";
    }
} catch (Throwable $e) {
    echo "  WARNING: Legacy alert cleanup failed: " . $e->getMessage() . "\n";
}
PHP
}

echo ""
echo "[4/6] Discovering SNMP devices..."
echo "  Targets: $DISCOVERY_TARGETS"
echo "  SNMP Community: $SNMP_COMMUNITY"
echo "  SNMP Probe: timeout ${SNMP_TIMEOUT}s, retries $SNMP_RETRIES"
echo ""

expand_targets "$DISCOVERY_TARGETS" | while read -r ip; do
  [ -z "$ip" ] && continue

  # 不再按 IP 末位编造名字；留空让 LibreNMS 用设备真实 sysName/hostname。
  if snmp_reachable "$ip"; then
    add_device_api "" "$ip" "$SNMP_COMMUNITY" || \
      add_device_cli "" "$ip" "$SNMP_COMMUNITY"
  else
    echo "  $ip: No SNMP response, skipped"
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

discover_firewall_ports
configure_isp_port_speed_overrides
configure_home_dashboard
configure_down_port_ignores
configure_stp_noise_suppression
configure_feishu_transport

# Configure alert rules
echo ""
echo "[6/6] Setting up alert rules..."
cleanup_legacy_alert_rules

rule_id_by_name() {
  echo "$EXISTING_RULES" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    rules = data if isinstance(data, list) else data.get('rules', [])
    for rule in rules:
        if rule.get('name') == sys.argv[1]:
            print(rule.get('id') or rule.get('rule_id') or '')
            break
except Exception:
    pass
" "$1" 2>/dev/null || true
}

rule_payload_with_operations() {
  python3 - "$1" "$ALERT_TRANSPORT_IDS" "${2:-}" <<'PY'
import json
import sys

payload = json.loads(sys.argv[1])
rule_id = (sys.argv[3] or "").strip()
if rule_id:
    payload["rule_id"] = rule_id

ids = []
for part in (sys.argv[2] or "").split(","):
    part = part.strip()
    if part.isdigit():
        ids.append(int(part))

if ids:
    payload["default_operation_step_duration"] = payload.get("default_operation_step_duration") or "5 m"
    payload["operations"] = [{
        "name": "Feishu",
        "operation_phase": "problem",
        "escalation_step_from": 1,
        "escalation_step_to": None,
        "start_in_seconds": 0,
        "step_duration_seconds": 86400,
        "transports": ids
    }]

print(json.dumps(payload, ensure_ascii=False))
PY
}

rule_has_operation() {
  _rule_id="$1"
  [ -n "$_rule_id" ] || return 1
  _rule_json=$(curl -s -H "X-Auth-Token: $API_TOKEN" "$LIBRENMS_URL/api/v0/rules/$_rule_id" 2>/dev/null || echo "{}")
  echo "$_rule_json" | python3 -c '
import sys, json
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
rule = data.get("rule") if isinstance(data, dict) else None
if not rule and isinstance(data, dict):
    rules = data.get("rules")
    if isinstance(rules, list) and rules:
        rule = rules[0]
if not rule and isinstance(data, dict):
    rule = data
if not isinstance(rule, dict):
    sys.exit(1)
op_id = rule.get("alert_operation_id")
ops = rule.get("operations") or []
sys.exit(0 if op_id or ops else 1)
' 2>/dev/null
}

rule_api_status() {
  echo "$1" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('status',''))" 2>/dev/null || echo ""
}

rule_api_message() {
  echo "$1" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('message',''))" 2>/dev/null || echo "$1"
}

# Idempotency: GET existing rules once and update them in place. Re-running this
# script refreshes WAN matching and attaches the Feishu transport operation.
upsert_rule() {
  rule_name="$1"
  base_payload="$2"
  rule_id="$(rule_id_by_name "$rule_name")"

  if [ -n "$rule_id" ]; then
    rule_payload="$(rule_payload_with_operations "$base_payload" "$rule_id")"
    _resp=$(curl -s -X PUT "$LIBRENMS_URL/api/v0/rules" \
      -H "X-Auth-Token: $API_TOKEN" \
      -H "Content-Type: application/json" \
      -d "$rule_payload" 2>/dev/null)
    _action="updated"
  else
    rule_payload="$(rule_payload_with_operations "$base_payload" "")"
    _resp=$(curl -s -X POST "$LIBRENMS_URL/api/v0/rules" \
      -H "X-Auth-Token: $API_TOKEN" \
      -H "Content-Type: application/json" \
      -d "$rule_payload" 2>/dev/null)
    _action="created"
  fi

  _status=$(rule_api_status "$_resp")
  if [ "$_status" = "ok" ]; then
    if [ -n "$rule_id" ] && [ -n "$ALERT_TRANSPORT_IDS" ] && ! rule_has_operation "$rule_id"; then
      echo "  Alert rule: $rule_name - existing rule has no operation, recreating with Feishu transport"
      curl -s -X DELETE "$LIBRENMS_URL/api/v0/rules/$rule_id" \
        -H "X-Auth-Token: $API_TOKEN" >/dev/null 2>&1 || true
      rule_payload="$(rule_payload_with_operations "$base_payload" "")"
      _resp=$(curl -s -X POST "$LIBRENMS_URL/api/v0/rules" \
        -H "X-Auth-Token: $API_TOKEN" \
        -H "Content-Type: application/json" \
        -d "$rule_payload" 2>/dev/null)
      _status=$(rule_api_status "$_resp")
      _action="recreated"
    fi
  fi

  if [ "$_status" = "ok" ]; then
    echo "  Alert rule: $rule_name - $_action"
  else
    _msg=$(rule_api_message "$_resp")
    echo "  Alert rule: $rule_name - $_action failed: $_msg"
  fi
}

if [ -n "$API_TOKEN" ]; then
  EXISTING_RULES=$(curl -s -H "X-Auth-Token: $API_TOKEN" "$LIBRENMS_URL/api/v0/rules" 2>/dev/null || echo '{"rules":[]}')

  # 设备离线改由 bridge 的实时 device-down watcher 处理（blackbox 每 5s ping，
  # ~10s 告警，飞书卡片显示 名字(IP) + 离线时长），比 LibreNMS 分钟级轮询快得多。
  down_rule_id="$(rule_id_by_name "设备离线告警")"
  if [ -n "$down_rule_id" ]; then
    curl -s -X DELETE "$LIBRENMS_URL/api/v0/rules/$down_rule_id" \
      -H "X-Auth-Token: $API_TOKEN" >/dev/null 2>&1 || true
    echo "  Alert rule: 设备离线告警 - removed (handled by realtime device-down watcher)"
  else
    echo "  Alert rule: 设备离线告警 - handled by realtime device-down watcher"
  fi

  # 高丢包告警：device_perf 表已被 LibreNMS 上游移除（2024-04），这条规则永远不会触发；
  # 丢包/掉线改由 bridge 的实时 blackbox watcher 负责。这里只做一次性清理——老部署里若还
  # 残留这条死规则就删掉，正常新部署本就没有、静默跳过（不再打印误导性的 "disabled"）。
  loss_rule_id="$(rule_id_by_name "高丢包告警")"
  if [ -n "$loss_rule_id" ]; then
    curl -s -X DELETE "$LIBRENMS_URL/api/v0/rules/$loss_rule_id" \
      -H "X-Auth-Token: $API_TOKEN" >/dev/null 2>&1 || true
    echo "  Alert rule: 高丢包告警 - removed (legacy dead rule; loss handled by realtime watcher)"
  fi

  isp_rule_id="$(rule_id_by_name "ISP 带宽饱和告警")"
  if [ -n "$isp_rule_id" ]; then
    curl -s -X DELETE "$LIBRENMS_URL/api/v0/rules/$isp_rule_id" \
      -H "X-Auth-Token: $API_TOKEN" >/dev/null 2>&1 || true
    echo "  Alert rule: ISP 带宽饱和告警 - removed (handled by realtime Feishu bridge)"
  else
    echo "  Alert rule: ISP 带宽饱和告警 - handled by realtime Feishu bridge"
  fi

  # 老版本里可能残留接口错误/丢弃规则。不同 LibreNMS 版本的 ports *_rate 字段
  # schema 不一致，字段不存在时会每 5 分钟写 SQL 错误日志；这里先清掉，避免误导。
  for legacy_rule in "接口错误告警" "接口丢弃告警"; do
    legacy_rule_id="$(rule_id_by_name "$legacy_rule")"
    if [ -n "$legacy_rule_id" ]; then
      curl -s -X DELETE "$LIBRENMS_URL/api/v0/rules/$legacy_rule_id" \
        -H "X-Auth-Token: $API_TOKEN" >/dev/null 2>&1 || true
      echo "  Alert rule: $legacy_rule - removed (legacy schema-dependent rule)"
    fi
  done

  # 互联口断链由 bridge 直接看 Prometheus ifOperStatus，按每个 Port-channel/LAG 单独告警。
  # 删除旧 LibreNMS 设备级规则，避免重复推送且缺少具体接口。
  interconnect_rule_id="$(rule_id_by_name "互联口断链告警")"
  if [ -n "$interconnect_rule_id" ]; then
    curl -s -X DELETE "$LIBRENMS_URL/api/v0/rules/$interconnect_rule_id" \
      -H "X-Auth-Token: $API_TOKEN" >/dev/null 2>&1 || true
    echo "  Alert rule: 互联口断链告警 - removed (handled per port-channel by realtime Feishu bridge)"
  else
    echo "  Alert rule: 互联口断链告警 - handled per port-channel by realtime Feishu bridge"
  fi

  # sysName 变更告警改由 bridge 自己轮询 /api/v0/devices 对比 sysName 实现，
  # 卡片能显示 旧→新（webhook 只带当前值，做不到）。LibreNMS 告警规则也没有可靠的
  # "changed" 算子。这里清理早期版本可能创建的该规则，避免重复/失效告警。
  sysname_rule_id="$(rule_id_by_name "sysName 变更告警")"
  if [ -n "$sysname_rule_id" ]; then
    curl -s -X DELETE "$LIBRENMS_URL/api/v0/rules/$sysname_rule_id" \
      -H "X-Auth-Token: $API_TOKEN" >/dev/null 2>&1 || true
    echo "  Alert rule: sysName 变更告警 - removed (handled by realtime sysName watcher in Feishu bridge)"
  else
    echo "  Alert rule: sysName 变更告警 - handled by realtime sysName watcher in Feishu bridge"
  fi
fi

echo ""
echo "============================================"
echo "  LibreNMS Discovery Complete!"
echo "============================================"
echo ""
if [ "$LIBRENMS_FORCE_BASE_URL" = "true" ]; then
  echo "  Web UI:    $LIBRENMS_BASE_URL"
else
  if [ -n "$SERVER_IP" ]; then
    echo "  Web UI:    dynamic request host (LAN: http://$SERVER_IP:$LIBRENMS_PORT)"
  else
    echo "  Web UI:    dynamic request host"
  fi
  if [ -n "$LIBRENMS_BASE_URL" ] && [ "$LIBRENMS_BASE_URL" != "http://localhost:8002" ]; then
    echo "             external hint: $LIBRENMS_BASE_URL"
  fi
fi
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
