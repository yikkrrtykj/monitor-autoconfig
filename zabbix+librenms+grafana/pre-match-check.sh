#!/bin/bash
# Pre-match self-check.
# Verifies the monitoring stack is healthy and ready for live tournament traffic.
# Run from the host where docker-compose is up. Exits non-zero if any critical check fails.
#
# Usage:
#   ./pre-match-check.sh                 # full check
#   ./pre-match-check.sh --quiet         # only print failures
#   PROM_URL=http://1.2.3.4:9090 ./pre-match-check.sh

set -u

if [ -f ./.env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

PROM_URL="${PROM_URL:-http://localhost:${PROMETHEUS_PORT:-9090}}"
GRAFANA_URL="${GRAFANA_URL:-http://localhost:${GRAFANA_PORT:-3000}}"
GRAFANA_USER="${GRAFANA_USER:-admin}"
GRAFANA_PASSWORD="${GRAFANA_PASSWORD:-root}"
FIREWALL_WAN_IF_FILTER="${FIREWALL_WAN_IF_FILTER:-telecom,telcom,unicom,isp,wan}"

QUIET=0
FIX=0
for arg in "$@"; do
  case "$arg" in
    --quiet) QUIET=1 ;;
    --fix)   FIX=1 ;;
    -h|--help)
      echo "Usage: $0 [--quiet] [--fix]"
      echo "  --quiet  only print failures"
      echo "  --fix    attempt automatic repair of known LibreNMS issues"
      exit 0
      ;;
  esac
done

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; NC='\033[0m'
PASS=0; WARN=0; FAIL=0

ok()    { [ $QUIET -eq 0 ] && echo -e "  ${GREEN}✓${NC} $*"; PASS=$((PASS+1)); }
warn()  { echo -e "  ${YELLOW}⚠${NC} $*"; WARN=$((WARN+1)); }
fail()  { echo -e "  ${RED}✗${NC} $*"; FAIL=$((FAIL+1)); }
hdr()   { [ $QUIET -eq 0 ] && echo -e "\n${YELLOW}== $* ==${NC}"; }

# ---- helpers ----
prom_query() {
  curl -s --max-time 5 --get --data-urlencode "query=$1" "${PROM_URL}/api/v1/query" 2>/dev/null
}

prom_value() {
  prom_query "$1" | python3 -c "
import sys, json
try:
  d = json.load(sys.stdin)
  r = d.get('data', {}).get('result', [])
  if not r: print(''); sys.exit()
  print(r[0]['value'][1])
except Exception:
  print('')
" 2>/dev/null
}

prom_targets_summary() {
  job="$1"
  curl -s --max-time 5 "${PROM_URL}/api/v1/targets?state=active" 2>/dev/null | python3 -c "
import json, sys
job = sys.argv[1]
try:
  d = json.load(sys.stdin)
  targets = d.get('data', {}).get('activeTargets', [])
  total = 0
  up = 0
  for target in targets:
    labels = target.get('labels') or {}
    discovered = target.get('discoveredLabels') or {}
    if labels.get('job') == job or discovered.get('job') == job or target.get('scrapePool') == job:
      total += 1
      if target.get('health') == 'up':
        up += 1
  print(f'{total} {up}')
except Exception:
  print('0 0')
" "$job" 2>/dev/null
}

prom_job_count() {
  metric="$1"
  job="$2"
  value=$(prom_value "count(${metric}{job=\"$job\"})")
  printf '%s' "${value:-0}"
}

prom_job_up_count() {
  metric="$1"
  job="$2"
  value=$(prom_value "count(${metric}{job=\"$job\"} == 1)")
  printf '%s' "${value:-0}"
}

print_known_jobs() {
  metric="$1"
  title="$2"
  rows=$(prom_query "count by (job) (${metric})" | python3 -c "
import json, sys
try:
  d = json.load(sys.stdin)
  rows = []
  for r in d.get('data', {}).get('result', []):
    job = r.get('metric', {}).get('job', '?')
    value = r.get('value', ['', '0'])[1]
    rows.append((job, value))
  rows.sort()
  for job, value in rows:
    print(f'         - {job}: {value}')
except Exception:
  pass
" 2>/dev/null)
  if [ -n "$rows" ]; then
    echo "       ${title}:"
    echo "$rows"
  fi
}

wan_filter_regex() {
  printf '%s' "$FIREWALL_WAN_IF_FILTER" | python3 -c "
import re, sys
raw = sys.stdin.read().strip()
parts = [p.strip() for p in raw.split(',') if p.strip()]
print('|'.join(re.escape(p) for p in parts) or 'telecom|telcom|unicom|isp|wan')
" 2>/dev/null
}

# ---- 1. Containers up ----
hdr "1. Docker 容器状态"

if ! command -v docker >/dev/null 2>&1; then
  fail "docker 命令不可用"
else
  for svc in prometheus grafana blackbox-exporter snmp-exporter player-targets zabbix-server librenms; do
    state=$(docker inspect -f '{{.State.Status}}' "$svc" 2>/dev/null || echo "missing")
    if [ "$state" = "running" ]; then ok "$svc 运行中"
    elif [ "$state" = "missing" ]; then warn "$svc 容器不存在（如果是该服务未启用，忽略）"
    else fail "$svc 状态异常: $state"
    fi
  done
fi

# ---- 2. Prometheus 自身 ----
hdr "2. Prometheus 服务"

if ! curl -s --max-time 5 "${PROM_URL}/-/healthy" >/dev/null 2>&1; then
  fail "Prometheus API 不可达 (${PROM_URL})"
  echo
  echo "无法继续检查，先解决 Prometheus 连通性问题"
  exit 1
fi
ok "Prometheus API 可达"
[ $QUIET -eq 0 ] && echo "       使用 Prometheus: ${PROM_URL}"

cfg_reload=$(prom_value 'prometheus_config_last_reload_successful')
if [ "$cfg_reload" = "1" ]; then ok "配置文件最近一次 reload 成功"
else fail "配置文件 reload 失败 — 检查 prometheus 容器日志"
fi

# ---- 3. 抓取目标健康度 ----
hdr "3. Prometheus 抓取目标 (targets)"

target_total_all=0
for job in infra-core-ping infra-dist-ping infra-fw-ping infra-srv-ping firewall-snmp player-ping; do
  read -r total up <<EOF
$(prom_targets_summary "$job")
EOF
  if [ "${total:-0}" = "0" ]; then
    total=$(prom_job_count up "$job")
    up=$(prom_job_up_count up "$job")
  fi
  total=${total:-0}; up=${up:-0}
  target_total_all=$((target_total_all + ${total%.*}))
  if [ "$total" = "0" ]; then warn "$job: 0 个目标（如未配置可忽略）"
  elif [ "$up" = "$total" ]; then ok "$job: $up/$total"
  else fail "$job: $up/$total — 有目标抓取失败"
  fi
done

if [ "$target_total_all" = "0" ]; then
  warn "当前 Prometheus 没查到任何本项目 target；如果 8088 有数据，请确认脚本的 PROM_URL 是否指向同一个 Prometheus"
  print_known_jobs up "当前 up 指标里的 job"
  print_known_jobs probe_success "当前 probe_success 指标里的 job"
fi

# ---- 4. 设备 ping 联通性 ----
hdr "4. 设备 ICMP 联通性"

for job in infra-core-ping infra-dist-ping infra-fw-ping infra-srv-ping; do
  total=$(prom_value "count(probe_success{job=\"$job\"})")
  ok_count=$(prom_value "count(probe_success{job=\"$job\"} == 1)")
  total=${total:-0}; ok_count=${ok_count:-0}
  if [ "$total" = "0" ]; then warn "$job: 0 个目标"
  elif [ "$ok_count" = "$total" ]; then ok "$job 全部 ping 通 ($ok_count/$total)"
  else
    fail "$job 有设备 ping 不通 ($ok_count/$total)"
    # List which ones are down
    curl -s --max-time 5 --get --data-urlencode "query=probe_success{job=\"$job\"} == 0" "${PROM_URL}/api/v1/query" 2>/dev/null | \
      python3 -c "
import sys, json
try:
  d = json.load(sys.stdin)
  for r in d.get('data', {}).get('result', []):
    print('     - ' + r['metric'].get('instance', '?'))
except Exception:
  pass
" 2>/dev/null
  fi
done

if [ "$(prom_value 'count(probe_success{job=~"infra-core-ping|infra-dist-ping|infra-fw-ping|infra-srv-ping"})')" = "0" ]; then
  print_known_jobs probe_success "可用的 probe_success job"
fi

# ---- 5. 选手 targets 注册情况 ----
hdr "5. 选手 targets 生成"

player_total=$(prom_value 'count(probe_success{role="player"})')
player_total=${player_total:-0}
if [ "$player_total" = "0" ]; then
  warn "未注册任何选手 targets"
  echo "         可能原因:"
  echo "         - TOURNAMENT_SWITCHES 未配置"
  echo "         - 交换机端口 description 未按 teamNN-MM 命名"
  echo "         - WIRELESS_SUBNETS 未配置，或无线扫描未发现在线 IP"
  echo "         - WiFi-only 比赛未配置 PLAYER_STATIC_TARGETS"
  echo "         - generate-player-targets.py 还没跑完第一轮（每 60s 一次）"
else
  ok "已注册 $player_total 个选手 targets"

  # Network split
  wired=$(prom_value 'count(probe_success{role="player",network="wired"})')
  wireless=$(prom_value 'count(probe_success{role="player",network="wireless"})')
  wired=${wired:-0}; wireless=${wireless:-0}
  echo "         有线: $wired, 无线: $wireless"

  if [ "$wireless" -gt 0 ] && [ "$wired" = "0" ]; then
    warn "当前只注册到无线选手 targets — 如果现场只接 WiFi，这是正常的；否则检查 PLAYER_SUBNETS / WIRELESS_SUBNETS"
  fi

  # Online ratio (only wired counts — wireless scan is a separate sanity signal)
  online=$(prom_value 'count(probe_success{role="player",network="wired"} == 1) or vector(0)')
  online=${online:-0}
  if [ "$wired" -gt 0 ] && [ "$online" = "$wired" ]; then
    ok "全部有线选手在线 ($online/$wired)"
  elif [ "$wired" -gt 0 ]; then
    warn "$online/$wired 有线选手在线（赛前正常，开赛时应全部在线）"
  fi

  # Team count + distribution — wired only so headcount isn't polluted by wireless-scan synthetic seats
  teams=$(prom_value 'count(count by (team) (probe_success{role="player",network="wired"}))')
  teams=${teams:-0}
  echo "         有线选手队伍数: $teams"

  if [ "$teams" -gt 0 ]; then
    teams_per_size=$(curl -s --max-time 5 "${PROM_URL}/api/v1/query?query=count%20by%20(team)%20(probe_success%7Brole%3D%22player%22%2Cnetwork%3D%22wired%22%7D)" 2>/dev/null | \
      python3 -c "
import sys, json
try:
  d = json.load(sys.stdin)
  rows = sorted(d['data']['result'], key=lambda x: int(x['metric'].get('team','0')))
  for r in rows:
    t = r['metric'].get('team','?')
    n = r['value'][1]
    print(f'           Team {t}: {n} 人')
except Exception:
  pass
" 2>/dev/null)
    [ -n "$teams_per_size" ] && echo "$teams_per_size"
  fi
fi

# ---- 6. 防火墙 SNMP 流量 ----
hdr "6. 防火墙 SNMP（ISP 流量）"

snmp_total=$(prom_value 'count(up{job="firewall-snmp"})')
snmp_up=$(prom_value 'count(up{job="firewall-snmp"} == 1)')
snmp_total=${snmp_total:-0}; snmp_up=${snmp_up:-0}
snmp_metric_total=$(prom_value 'count(ifHCInOctets{job="firewall-snmp"})')
snmp_metric_total=${snmp_metric_total:-0}

if [ "$snmp_total" = "0" ] && [ "$snmp_metric_total" = "0" ]; then
  warn "未配置 FIREWALL_SNMP_TARGETS"
elif [ "$snmp_up" != "$snmp_total" ]; then
  fail "防火墙 SNMP 抓取失败 ($snmp_up/$snmp_total) — 检查 SNMP community / 防火墙策略"
else
  if [ "$snmp_total" = "0" ]; then
    ok "检测到 firewall-snmp 流量指标 ($snmp_metric_total 条)"
  else
    ok "防火墙 SNMP 全部抓通 ($snmp_up/$snmp_total)"
  fi

  # ISP interfaces detected
  wan_regex="$(wan_filter_regex)"
  isp_count=$(prom_value "count((count by (ifAlias) (ifHCInOctets{job=\"firewall-snmp\",ifAlias=~\".+\",ifAlias=~\"(?i).*(${wan_regex}).*\"})) or (count by (ifName) (ifHCInOctets{job=\"firewall-snmp\",ifAlias=\"\",ifName=~\".+\",ifName=~\"(?i).*(${wan_regex}).*\"})) or (count by (ifDescr) (ifHCInOctets{job=\"firewall-snmp\",ifAlias=\"\",ifName=\"\",ifDescr=~\".+\",ifDescr=~\"(?i).*(${wan_regex}).*\"})))")
  isp_count=${isp_count:-0}
  if [ "$isp_count" = "0" ]; then
    warn "未检测到 ISP/WAN 接口 — 防火墙端口 description 是否包含 telecom/unicom/isp/wan?"
  else
    ok "检测到 $isp_count 条 ISP/WAN 链路"
  fi
fi

# ---- 7. Grafana ----
hdr "7. Grafana"

if ! curl -s --max-time 5 "${GRAFANA_URL}/api/health" >/dev/null 2>&1; then
  fail "Grafana 不可达 (${GRAFANA_URL})"
else
  ok "Grafana API 可达"

  GAUTH=$(printf '%s:%s' "$GRAFANA_USER" "$GRAFANA_PASSWORD" | base64 -w0 2>/dev/null || printf '%s:%s' "$GRAFANA_USER" "$GRAFANA_PASSWORD" | base64)
  ds=$(curl -s --max-time 5 -H "Authorization: Basic $GAUTH" "${GRAFANA_URL}/api/datasources" 2>/dev/null | \
    python3 -c "
import sys, json
try:
  d = json.load(sys.stdin)
  print(','.join(x.get('name','?') for x in d))
except Exception: print('')
" 2>/dev/null)
  if echo "$ds" | grep -q Prometheus; then ok "Prometheus 数据源已注册"
  else fail "Prometheus 数据源未注册 — 检查 grafana-setup.sh 是否跑完"
  fi

  # Dashboard count
  dash_count=$(curl -s --max-time 5 -H "Authorization: Basic $GAUTH" "${GRAFANA_URL}/api/search?type=dash-db" 2>/dev/null | \
    python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo 0)
  if [ "$dash_count" -ge 5 ]; then ok "Dashboard 已加载 ($dash_count 个)"
  else warn "Dashboard 数量偏少 ($dash_count)"
  fi
fi

# ---- 8. LibreNMS ----
hdr "8. LibreNMS"

if ! docker inspect librenms >/dev/null 2>&1; then
  warn "librenms 容器不存在 — 如果不用 LibreNMS 可忽略"
else
  # 8a. dispatcher container (scheduler in docker setup)
  disp_state=$(docker inspect -f '{{.State.Status}}' librenms-dispatcher 2>/dev/null || echo missing)
  if [ "$disp_state" = "running" ]; then
    ok "librenms-dispatcher (scheduler) 运行中"
  else
    fail "librenms-dispatcher 未运行 ($disp_state) — validate.php 会报 Scheduler is not running"
    if [ $FIX -eq 1 ]; then
      echo "       [FIX] docker compose up -d librenms-dispatcher"
      docker compose up -d librenms-dispatcher >/dev/null 2>&1 && \
        echo "       重启成功，给 30 秒注册时间" && sleep 30
    fi
  fi

  # 8b. validate.php
  if [ "$disp_state" = "running" ] || [ $FIX -eq 1 ]; then
    validate=$(docker exec -u librenms librenms sh -lc 'php /opt/librenms/validate.php 2>&1' 2>/dev/null || true)
    if [ -z "$validate" ]; then
      fail "无法运行 validate.php（容器可能没就绪）"
    else
      # 已知可忽略的 WARN（docker 部署专属）
      ignore_warn='Updates are managed through the official Docker image'
      fail_lines=$(echo "$validate" | grep -E '^\[FAIL\]' | grep -v 'Scheduler is not running' || true)
      # 上面单独检查了 dispatcher，所以 validate 报的 "Scheduler is not running" 在 dispatcher 已 up 时是误报
      warn_lines=$(echo "$validate" | grep -E '^\[WARN\]' | grep -v "$ignore_warn" || true)
      no_devices=$(echo "$validate" | grep -c 'You have no devices' || true)

      if [ -n "$fail_lines" ]; then
        echo "$fail_lines" | while IFS= read -r line; do
          fail "validate.php: ${line#\[FAIL\]  }"
        done
      else
        ok "validate.php 没有真实 FAIL"
      fi

      if [ "$no_devices" -gt 0 ]; then
        fail "LibreNMS 还没添加任何设备"
        # 看一下 librenms-config 一次性容器的退出状态
        cfg_status=$(docker inspect -f '{{.State.Status}}' librenms-config 2>/dev/null || echo missing)
        cfg_exit=$(docker inspect -f '{{.State.ExitCode}}' librenms-config 2>/dev/null || echo "?")
        if [ "$cfg_status" = "missing" ]; then
          echo "       librenms-config 容器从未运行过 — 检查 docker-compose.yml"
        elif [ "$cfg_status" = "exited" ] && [ "$cfg_exit" != "0" ]; then
          echo "       librenms-config 退出码=$cfg_exit（异常）— 看日志: docker logs librenms-config"
        else
          echo "       librenms-config 状态: $cfg_status (exit=$cfg_exit)"
        fi
        echo "       .env 里 LIBRENMS_DISCOVERY_TARGETS=${LIBRENMS_DISCOVERY_TARGETS:-未设置}"
        if [ $FIX -eq 1 ]; then
          echo "       [FIX] 重跑 librenms-config 触发自动发现"
          docker compose up -d --force-recreate librenms-config >/dev/null 2>&1 && \
            echo "       已重启，看进度: docker logs -f librenms-config"
        else
          echo "       手工修复: docker compose up -d --force-recreate librenms-config"
        fi
      else
        # 已经有设备，数一下
        dev_count=$(docker exec -u librenms librenms sh -lc 'php /opt/librenms/lnms device:list 2>/dev/null | grep -c "^|" || true' 2>/dev/null || echo 0)
        dev_count=${dev_count:-0}
        if [ "$dev_count" -gt 0 ]; then
          ok "LibreNMS 已注册 $dev_count 个设备"
        fi
      fi

      # 其他 WARN（不计入"设备空"重复）
      other_warns=$(echo "$warn_lines" | grep -v 'You have no devices' || true)
      if [ -n "$other_warns" ]; then
        echo "$other_warns" | while IFS= read -r line; do
          warn "validate.php: ${line#\[WARN\]  }"
        done
      fi
    fi
  fi
fi


echo
echo "=========================================="
printf "  ${GREEN}通过 %d${NC}    ${YELLOW}警告 %d${NC}    ${RED}失败 %d${NC}\n" "$PASS" "$WARN" "$FAIL"
echo "=========================================="

if [ $FAIL -gt 0 ]; then
  echo "❌ 有失败项，必须解决后再开赛"
  [ $FIX -eq 0 ] && echo "   提示：跑 $0 --fix 尝试自动修复部分已知问题"
  exit 1
elif [ $WARN -gt 0 ]; then
  echo "⚠ 有警告项，确认是否预期"
  exit 0
else
  echo "✅ 全部通过，可以开赛"
  exit 0
fi
