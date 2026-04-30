#!/bin/sh

set -eu

CONFIG_FILE="${PROMETHEUS_CONFIG_FILE:-/tmp/prometheus.yml}"
PING_TARGETS="${PROMETHEUS_PING_TARGETS:-192.168.10.254,192.168.10.11-16}"
INFRA_SWITCH_TARGETS="${INFRA_SWITCH_PING_TARGETS:-}"
INFRA_FIREWALL_TARGETS="${INFRA_FIREWALL_PING_TARGETS:-}"
INFRA_AP_TARGETS="${INFRA_AP_PING_TARGETS:-}"
PLAYER_TARGETS_FILE="${PLAYER_TARGETS_FILE:-/etc/prometheus/player_targets.json}"
SCRAPE_INTERVAL="${PROMETHEUS_SCRAPE_INTERVAL:-30s}"
RETENTION_TIME="${PROMETHEUS_RETENTION_TIME:-15d}"

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

write_target_list() {
  targets=$1
  expand_targets "$targets" | while read -r target; do
    [ -n "$target" ] && echo "        - \"$target\""
  done
}

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

  - job_name: "infra-switch-ping"
    metrics_path: /probe
    params:
      module: [icmp]
    static_configs:
      - targets:
EOF

if [ -n "$INFRA_SWITCH_TARGETS" ]; then
  write_target_list "$INFRA_SWITCH_TARGETS" >> "$CONFIG_FILE"
else
  echo "        []" >> "$CONFIG_FILE"
fi

cat >> "$CONFIG_FILE" <<EOF
        labels:
          infra_role: "switch"
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - source_labels: [__param_target]
        target_label: instance
      - target_label: __address__
        replacement: blackbox-exporter:9115

  - job_name: "infra-firewall-ping"
    metrics_path: /probe
    params:
      module: [icmp]
    static_configs:
      - targets:
EOF

if [ -n "$INFRA_FIREWALL_TARGETS" ]; then
  write_target_list "$INFRA_FIREWALL_TARGETS" >> "$CONFIG_FILE"
else
  echo "        []" >> "$CONFIG_FILE"
fi

cat >> "$CONFIG_FILE" <<EOF
        labels:
          infra_role: "firewall"
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - source_labels: [__param_target]
        target_label: instance
      - target_label: __address__
        replacement: blackbox-exporter:9115

  - job_name: "infra-ap-ping"
    metrics_path: /probe
    params:
      module: [icmp]
    static_configs:
      - targets:
EOF

if [ -n "$INFRA_AP_TARGETS" ]; then
  write_target_list "$INFRA_AP_TARGETS" >> "$CONFIG_FILE"
else
  echo "        []" >> "$CONFIG_FILE"
fi

cat >> "$CONFIG_FILE" <<EOF
        labels:
          infra_role: "ap"
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - source_labels: [__param_target]
        target_label: instance
      - target_label: __address__
        replacement: blackbox-exporter:9115

  - job_name: "player-ping"
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
      - target_label: __address__
        replacement: blackbox-exporter:9115

  - job_name: "network-ping"
    metrics_path: /probe
    params:
      module: [icmp]
    static_configs:
      - targets:
EOF

write_target_list "$PING_TARGETS" >> "$CONFIG_FILE"

cat >> "$CONFIG_FILE" <<EOF
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - source_labels: [__param_target]
        target_label: instance
      - target_label: __address__
        replacement: blackbox-exporter:9115

  - job_name: "blackbox-exporter"
    static_configs:
      - targets: ["blackbox-exporter:9115"]
EOF

echo "Generated Prometheus config:"
echo "  Ping targets: $PING_TARGETS"
echo "  Switch ping:  ${INFRA_SWITCH_TARGETS:-none}"
echo "  Firewall ping: ${INFRA_FIREWALL_TARGETS:-none}"
echo "  AP ping:      ${INFRA_AP_TARGETS:-none}"
echo "  Player targets: $PLAYER_TARGETS_FILE"
echo "  Scrape interval: $SCRAPE_INTERVAL"
echo "  Retention: $RETENTION_TIME"

exec /bin/prometheus \
  --config.file="$CONFIG_FILE" \
  --storage.tsdb.path=/prometheus \
  --web.console.libraries=/etc/prometheus/console_libraries \
  --web.console.templates=/etc/prometheus/consoles \
  --web.enable-lifecycle \
  --storage.tsdb.retention.time="$RETENTION_TIME"
