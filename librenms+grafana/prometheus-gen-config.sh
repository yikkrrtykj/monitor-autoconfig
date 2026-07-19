#!/bin/sh

set -eu

CONFIG_FILE="${PROMETHEUS_CONFIG_FILE:-/tmp/prometheus.yml}"
CORE_SWITCH_PING="${CORE_SWITCH_PING:-}"
DIST_SWITCH_PING="${DIST_SWITCH_PING:-}"
INTERCONNECT_SNMP_TARGETS="${INTERCONNECT_SNMP_TARGETS:-$CORE_SWITCH_PING}"
FIREWALL_PING="${FIREWALL_PING:-}"
SERVER_PING="${SERVER_PING:-}"
ISP_PING="${ISP_PING:-${BIGSCREEN_ISP_IPS:-}}"
FIREWALL_SNMP_TARGETS="${FIREWALL_SNMP_TARGETS:-}"
# Per-unit SNMP for HA firewall physical nodes (health/uptime per box).
# Separate from FIREWALL_SNMP_TARGETS because WAN traffic data must come
# from the logical VIP (always active node); physical IPs only give per-unit
# hardware health.
FIREWALL_UNIT_SNMP_TARGETS="${FIREWALL_UNIT_SNMP_TARGETS:-}"
SNMP_AUTH="${SNMP_AUTH:-global}"
PLAYER_TARGETS_FILE="${PLAYER_TARGETS_FILE:-/etc/prometheus/player_targets.json}"
# Live switches discovered from SWITCH_DISCOVERY_RANGE (written by the topology
# container). Used as a file_sd source so only switches that actually answer are
# pinged/SNMP-scraped, each already named by its real hostname.
SWITCH_TARGETS_FILE="${SWITCH_TARGETS_FILE:-/etc/prometheus/targets/topology/switch_targets.json}"
# ISP 网关自动发现结果（topology 容器从防火墙 SNMP 路由表读出的默认路由下一跳），
# 和手工 ISP_PING 一起进 infra-isp-ping。
ISP_TARGETS_FILE="${ISP_TARGETS_FILE:-/etc/prometheus/targets/topology/isp_targets.json}"
SCRAPE_INTERVAL="${PROMETHEUS_SCRAPE_INTERVAL:-10s}"
# 选手 ICMP 单独的采集间隔：比全局更密（默认 5s），纠纷回查的时间分辨率翻倍。
# blackbox 对几十个选手目标 5s 一轮的负载可以忽略，被探测的选手机器无感知。
PLAYER_PING_SCRAPE_INTERVAL="${PLAYER_PING_SCRAPE_INTERVAL:-5s}"
# 基础设施 ICMP（core/dist/fw/isp/srv）的采集间隔。默认 2s，配合 bridge 的
# device-down watcher 可更贴近 10s 阈值发出离线告警。
INFRA_PING_SCRAPE_INTERVAL="${INFRA_PING_SCRAPE_INTERVAL:-2s}"
# 外网 ISP 单独更密一些，方便抓“拔一下马上插回”的短闪断。
ISP_PING_SCRAPE_INTERVAL="${ISP_PING_SCRAPE_INTERVAL:-1s}"
RETENTION_TIME="${PROMETHEUS_RETENTION_TIME:-15d}"
# 交换机/防火墙 uptime（运行时长）SNMP 单独的采集间隔。运行时长显示的是"天"，
# 不需要跟随全局 5-10s 高频采集；拉长可减轻 2960 等弱 CPU 交换机的控制平面负担，
# 避免其管理口 ICMP 被周期性 SNMP GET 推迟而出现 ping 尖峰。
SNMP_UPTIME_SCRAPE_INTERVAL="${SNMP_UPTIME_SCRAPE_INTERVAL:-600s}"
# Port-channel / LAG interconnect status needs ifOperStatus. Keep this separate
# from uptime so the fast link watcher does not make all SNMP jobs heavy.
SWITCH_IFMIB_SCRAPE_INTERVAL="${SWITCH_IFMIB_SCRAPE_INTERVAL:-10s}"
# UniFi Poller（unpoller）抓取：仅在配置了控制器地址时启用。AP 掉线告警默认
# 10s 确认，Prometheus 这里也保持 10s 抓取，避免控制器状态更新后还多等一轮。
UNIFI_CONTROLLER_URL="${UNIFI_CONTROLLER_URL:-}"
UNIFI_SCRAPE_INTERVAL="${UNIFI_SCRAPE_INTERVAL:-10s}"

# Parse "NAME:IP" or "NAME:IP-START-IP-END" format.
# Outputs Prometheus static_config target lines with display_name label.
write_labeled_targets() {
  old_ifs=$IFS
  IFS=','
  for entry in $1; do
    IFS=$old_ifs
    entry=$(echo "$entry" | tr -d '[:space:]')
    [ -z "$entry" ] && continue

    name="${entry%%:*}"
    ip_part="${entry#*:}"
    [ -z "$name" ] && name="$ip_part"
    [ -z "$ip_part" ] && continue

    case "$ip_part" in
      *-*)
        start_ip=${ip_part%-*}
        end_part=${ip_part#*-}
        prefix=${start_ip%.*}
        start_octet=${start_ip##*.}
        end_octet=${end_part##*.}
        octet=$start_octet
        idx=1
        while [ "$octet" -le "$end_octet" ]; do
          echo "      - targets:"
          echo "          - \"$prefix.$octet\""
          echo "        labels:"
          echo "          display_name: \"${name}${idx}\""
          octet=$((octet + 1))
          idx=$((idx + 1))
        done
        ;;
      *)
        echo "      - targets:"
        echo "          - \"$ip_part\""
        echo "        labels:"
        echo "          display_name: \"$name\""
        ;;
    esac
    IFS=','
  done
  IFS=$old_ifs
}

# Write a named ping job to CONFIG_FILE. A 3rd arg, when set, adds a file_sd
# source (e.g. discovered switch targets) alongside the static ones.
write_ping_job() {
  job_name="$1"
  targets="$2"
  file_sd="${3:-}"
  interval="$INFRA_PING_SCRAPE_INTERVAL"
  if [ "$job_name" = "infra-isp-ping" ]; then
    interval="$ISP_PING_SCRAPE_INTERVAL"
  fi

  cat >> "$CONFIG_FILE" <<EOF
  - job_name: "${job_name}"
    scrape_interval: ${interval}
    metrics_path: /probe
    params:
      module: [icmp]
    static_configs:
EOF

  if [ -z "$targets" ]; then
    echo "      - targets: []" >> "$CONFIG_FILE"
  else
    write_labeled_targets "$targets" >> "$CONFIG_FILE"
  fi

  if [ -n "$file_sd" ]; then
    cat >> "$CONFIG_FILE" <<EOF
    file_sd_configs:
      - files:
          - "${file_sd}"
        refresh_interval: 60s
EOF
  fi

  cat >> "$CONFIG_FILE" <<'RELABEL'
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - source_labels: [display_name]
        target_label: instance
      - source_labels: [__param_target]
        target_label: target_ip
      - target_label: __address__
        replacement: blackbox-exporter:9115
RELABEL
}

find_label_for_target() {
  needle="$1"
  references="$2"
  old_ifs=$IFS
  IFS=','
  for ref in $references; do
    IFS=$old_ifs
    ref=$(echo "$ref" | tr -d '[:space:]')
    case "$ref" in
      *:*)
        ref_name="${ref%%:*}"
        ref_target="${ref#*:}"
        if [ "$ref_target" = "$needle" ]; then
          echo "$ref_name"
          IFS=$old_ifs
          return 0
        fi
        ;;
    esac
    IFS=','
  done
  IFS=$old_ifs
  return 1
}

apply_reference_names() {
  targets="$1"
  references="$2"
  result=""
  old_ifs=$IFS
  IFS=','
  for entry in $targets; do
    IFS=$old_ifs
    entry=$(echo "$entry" | tr -d '[:space:]')
    [ -z "$entry" ] && continue
    case "$entry" in
      *:*)
        named_entry="$entry"
        ;;
      *)
        label="$(find_label_for_target "$entry" "$references" || true)"
        if [ -n "$label" ]; then
          named_entry="$label:$entry"
        else
          named_entry="$entry"
        fi
        ;;
    esac
    result="${result}${result:+,}${named_entry}"
    IFS=','
  done
  IFS=$old_ifs
  echo "$result"
}

has_labeled_targets() {
  compact=$(echo "$1" | tr -d '[:space:],')
  [ -n "$compact" ]
}

write_snmp_job() {
  job_name="$1"
  targets="$2"
  module="${3:-if_mib}"
  interval="${4:-}"
  file_sd="${5:-}"

  {
    echo "  - job_name: \"${job_name}\""
    if [ -n "$interval" ]; then
      echo "    scrape_interval: ${interval}"
    fi
    echo "    metrics_path: /snmp"
    echo "    params:"
    echo "      auth: [${SNMP_AUTH}]"
    echo "      module: [${module}]"
    echo "    static_configs:"
  } >> "$CONFIG_FILE"

  if has_labeled_targets "$targets"; then
    write_labeled_targets "$targets" >> "$CONFIG_FILE"
  else
    echo "      - targets: []" >> "$CONFIG_FILE"
  fi

  if [ -n "$file_sd" ]; then
    cat >> "$CONFIG_FILE" <<EOF
    file_sd_configs:
      - files:
          - "${file_sd}"
        refresh_interval: 60s
EOF
  fi

  cat >> "$CONFIG_FILE" <<'RELABEL'
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - source_labels: [display_name]
        target_label: instance
      - source_labels: [__param_target]
        target_label: target_ip
      - target_label: __address__
        replacement: snmp-exporter:9116
RELABEL
}

# ---- Generate config ----
cat > "$CONFIG_FILE" <<EOF
global:
  scrape_interval: $SCRAPE_INTERVAL
  evaluation_interval: $SCRAPE_INTERVAL

scrape_configs:
  - job_name: "prometheus"
    static_configs:
      - targets: ["prometheus:9090"]
        labels:
          app: "prometheus"
EOF

# Infrastructure ping jobs
write_ping_job "infra-isp-ping"   "$ISP_PING" "$ISP_TARGETS_FILE"
write_ping_job "infra-core-ping"  "$CORE_SWITCH_PING"
# Dist switches: explicit targets plus any discovered from SWITCH_DISCOVERY_RANGE.
write_ping_job "infra-dist-ping"  "$DIST_SWITCH_PING" "$SWITCH_TARGETS_FILE"
write_ping_job "infra-fw-ping"    "$FIREWALL_PING"
# HA 物理防火墙单机 ping：一台一台下机/换机时能发离线告警。逻辑 VIP 永远有
# 一台在应答，只 ping VIP 看不到单机下线。独立 job 是为了不进大屏拓扑
# （拓扑只画 infra-fw-ping 的防火墙节点）。
write_ping_job "infra-fw-unit-ping" "$FIREWALL_UNIT_SNMP_TARGETS"
write_ping_job "infra-srv-ping"   "$SERVER_PING"

# Infrastructure SNMP jobs for device uptime
SWITCH_SNMP_TARGETS="${CORE_SWITCH_PING}${CORE_SWITCH_PING:+,}${DIST_SWITCH_PING}"
FIREWALL_SNMP_UPTIME_TARGETS="$(apply_reference_names "$FIREWALL_SNMP_TARGETS" "$FIREWALL_PING")"
# Same discovered switches also get SNMP-scraped so sysName/uptime resolve.
write_snmp_job "infra-switch-snmp"   "$SWITCH_SNMP_TARGETS"           "system_uptime" "$SNMP_UPTIME_SCRAPE_INTERVAL" "$SWITCH_TARGETS_FILE"
write_snmp_job "infra-fw-snmp"       "$FIREWALL_SNMP_UPTIME_TARGETS"  "system_uptime" "$SNMP_UPTIME_SCRAPE_INTERVAL"
write_snmp_job "infra-fw-unit-snmp"  "$FIREWALL_UNIT_SNMP_TARGETS"    "system_uptime" "$SNMP_UPTIME_SCRAPE_INTERVAL"
write_snmp_job "infra-switch-ifmib"  "$INTERCONNECT_SNMP_TARGETS"     "if_mib"        "$SWITCH_IFMIB_SCRAPE_INTERVAL"

# Firewall SNMP
cat >> "$CONFIG_FILE" <<EOF
  - job_name: "firewall-snmp"
    metrics_path: /snmp
    params:
      auth: [${SNMP_AUTH}]
      module: [if_mib]
    static_configs:
EOF

if [ -n "$FIREWALL_SNMP_TARGETS" ]; then
  write_labeled_targets "$FIREWALL_SNMP_TARGETS" >> "$CONFIG_FILE"
else
  echo "      - targets: []" >> "$CONFIG_FILE"
fi

cat >> "$CONFIG_FILE" <<EOF
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - source_labels: [__param_target]
        target_label: instance
      - target_label: __address__
        replacement: snmp-exporter:9116

  - job_name: "player-ping"
    scrape_interval: ${PLAYER_PING_SCRAPE_INTERVAL}
    metrics_path: /probe
    params:
      module: [icmp]
    file_sd_configs:
      - files:
          - "${PLAYER_TARGETS_FILE}"
        refresh_interval: 60s
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - source_labels: [__param_target]
        target_label: instance
      - source_labels: [__param_target]
        regex: '\d+\.\d+\.\d+\.(\d+)'
        target_label: ip_last
        replacement: '\${1}'
      - target_label: __address__
        replacement: blackbox-exporter:9115

  - job_name: "blackbox-exporter"
    static_configs:
      - targets: ["blackbox-exporter:9115"]
EOF

# UniFi Poller —— 仅当配置了控制器地址时才抓取（unpoller 在 "unifi" profile 里，
# 没配 UniFi 的部署根本没这个容器，也就别去抓它，免得 Prometheus 报无效 target）。
if [ -n "$UNIFI_CONTROLLER_URL" ]; then
  cat >> "$CONFIG_FILE" <<EOF

  - job_name: "unifi"
    scrape_interval: ${UNIFI_SCRAPE_INTERVAL}
    metrics_path: /metrics
    static_configs:
      - targets: ["unpoller:9130"]
EOF
fi

echo "Generated Prometheus config:"
echo "  Core:    ${CORE_SWITCH_PING:-none}"
echo "  Dist:    ${DIST_SWITCH_PING:-none}"
echo "  Link IF: ${INTERCONNECT_SNMP_TARGETS:-none}"
echo "  FW:      ${FIREWALL_PING:-none}"
echo "  Server:  ${SERVER_PING:-none}"
echo "  ISP:     ${ISP_PING:-none}"
echo "  FW SNMP: ${FIREWALL_SNMP_TARGETS:-none}"
echo "  FW Unit: ${FIREWALL_UNIT_SNMP_TARGETS:-none}"
echo "  UniFi:   ${UNIFI_CONTROLLER_URL:-none}"
echo "  Players: $PLAYER_TARGETS_FILE"

exec /bin/prometheus \
  --config.file="$CONFIG_FILE" \
  --storage.tsdb.path=/prometheus \
  --web.console.libraries=/etc/prometheus/console_libraries \
  --web.console.templates=/etc/prometheus/consoles \
  --web.enable-lifecycle \
  --storage.tsdb.retention.time="$RETENTION_TIME"
