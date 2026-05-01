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

PROM_URL="${PROM_URL:-http://localhost:9090}"
GRAFANA_URL="${GRAFANA_URL:-http://localhost:3000}"
GRAFANA_USER="${GRAFANA_USER:-admin}"
GRAFANA_PASSWORD="${GRAFANA_PASSWORD:-root}"

QUIET=0
[ "${1:-}" = "--quiet" ] && QUIET=1

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; NC='\033[0m'
PASS=0; WARN=0; FAIL=0

ok()    { [ $QUIET -eq 0 ] && echo -e "  ${GREEN}✓${NC} $*"; PASS=$((PASS+1)); }
warn()  { echo -e "  ${YELLOW}⚠${NC} $*"; WARN=$((WARN+1)); }
fail()  { echo -e "  ${RED}✗${NC} $*"; FAIL=$((FAIL+1)); }
hdr()   { [ $QUIET -eq 0 ] && echo -e "\n${YELLOW}== $* ==${NC}"; }

# ---- helpers ----
prom_query() {
  curl -s --max-time 5 "${PROM_URL}/api/v1/query?query=$1" 2>/dev/null
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

cfg_reload=$(prom_value 'prometheus_config_last_reload_successful')
if [ "$cfg_reload" = "1" ]; then ok "配置文件最近一次 reload 成功"
else fail "配置文件 reload 失败 — 检查 prometheus 容器日志"
fi

# ---- 3. 抓取目标健康度 ----
hdr "3. Prometheus 抓取目标 (targets)"

for job in infra-core-ping infra-dist-ping infra-fw-ping infra-srv-ping firewall-snmp player-ping; do
  total=$(prom_value "count(up{job=\"$job\"})")
  up=$(prom_value "count(up{job=\"$job\"} == 1)")
  total=${total:-0}; up=${up:-0}
  if [ "$total" = "0" ]; then warn "$job: 0 个目标（如未配置可忽略）"
  elif [ "$up" = "$total" ]; then ok "$job: $up/$total"
  else fail "$job: $up/$total — 有目标抓取失败"
  fi
done

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
    curl -s --max-time 5 "${PROM_URL}/api/v1/query?query=probe_success{job=\"$job\"}==0" 2>/dev/null | \
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

# ---- 5. 选手 targets 注册情况 ----
hdr "5. 选手 targets 生成"

player_total=$(prom_value 'count(probe_success{role="player"})')
player_total=${player_total:-0}
if [ "$player_total" = "0" ]; then
  warn "未注册任何选手 targets"
  echo "         可能原因:"
  echo "         - TOURNAMENT_SWITCHES 未配置"
  echo "         - 交换机端口 description 未按 teamNN-MM 命名"
  echo "         - generate-player-targets.py 还没跑完第一轮（每 60s 一次）"
else
  ok "已注册 $player_total 个选手 targets"

  # Network split
  wired=$(prom_value 'count(probe_success{role="player",network="wired"})')
  wireless=$(prom_value 'count(probe_success{role="player",network="wireless"})')
  wired=${wired:-0}; wireless=${wireless:-0}
  echo "         有线: $wired, 无线: $wireless"

  if [ "$wireless" -gt 0 ] && [ "$wired" = "0" ]; then
    fail "所有选手都被打成无线 — 检查 PLAYER_SUBNETS / WIRELESS_SUBNETS 是否搞反"
  fi

  # Online ratio
  online=$(prom_value 'count(probe_success{role="player"} == 1) or vector(0)')
  online=${online:-0}
  if [ "$online" = "$player_total" ]; then
    ok "全部选手在线 ($online/$player_total)"
  else
    warn "$online/$player_total 选手在线（赛前正常，开赛时应全部在线）"
  fi

  # Team count
  teams=$(prom_value 'count(count by (team) (probe_success{role="player"}))')
  teams=${teams:-0}
  echo "         队伍数: $teams"

  # Team distribution sanity
  if [ "$teams" -gt 0 ]; then
    teams_per_size=$(curl -s --max-time 5 "${PROM_URL}/api/v1/query?query=count%20by%20(team)%20(probe_success%7Brole%3D%22player%22%7D)" 2>/dev/null | \
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

if [ "$snmp_total" = "0" ]; then
  warn "未配置 FIREWALL_SNMP_TARGETS"
elif [ "$snmp_up" != "$snmp_total" ]; then
  fail "防火墙 SNMP 抓取失败 ($snmp_up/$snmp_total) — 检查 SNMP community / 防火墙策略"
else
  ok "防火墙 SNMP 全部抓通 ($snmp_up/$snmp_total)"

  # ISP interfaces detected
  isp_count=$(prom_value 'count(count by (ifAlias) (ifHCInOctets{job="firewall-snmp",ifAlias=~"(?i)telecom|telcom|unicom|isp|wan"}))')
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

# ---- 总结 ----
echo
echo "=========================================="
printf "  ${GREEN}通过 %d${NC}    ${YELLOW}警告 %d${NC}    ${RED}失败 %d${NC}\n" "$PASS" "$WARN" "$FAIL"
echo "=========================================="

if [ $FAIL -gt 0 ]; then
  echo "❌ 有失败项，建议解决后再开赛"
  exit 1
elif [ $WARN -gt 0 ]; then
  echo "⚠ 有警告项，确认是否预期"
  exit 0
else
  echo "✅ 全部通过，可以开赛"
  exit 0
fi
