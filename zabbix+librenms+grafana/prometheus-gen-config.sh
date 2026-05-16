#!/bin/sh

set -eu

CONFIG_FILE="${PROMETHEUS_CONFIG_FILE:-/tmp/prometheus.yml}"
CORE_SWITCH_PING="${CORE_SWITCH_PING:-}"
DIST_SWITCH_PING="${DIST_SWITCH_PING:-}"
FIREWALL_PING="${FIREWALL_PING:-}"
SERVER_PING="${SERVER_PING:-}"
FIREWALL_SNMP_TARGETS="${FIREWALL_SNMP_TARGETS:-}"
SNMP_AUTH="${SNMP_AUTH:-global}"
PLAYER_TARGETS_FILE="${PLAYER_TARGETS_FILE:-/etc/prometheus/player_targets.json}"
SCRAPE_INTERVAL="${PROMETHEUS_SCRAPE_INTERVAL:-10s}"
RETENTION_TIME="${PROMETHEUS_RETENTION_TIME:-15d}"

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

    echo "      - targets:"
    case "$ip_part" in
      *-*)
        start_ip=${ip_part%-*}
        end_part=${ip_part#*-}
        prefix=${start_ip%.*}
        start_octet=${start_ip##*.}
        end_octet=${end_part##*.}
        octet=$start_octet
        while [ "$octet" -le "$end_octet" ]; do
          echo "          - \"$prefix.$octet\""
          octet=$((octet + 1))
        done
        ;;
      *)
        echo "          - \"$ip_part\""
        ;;
    esac
    echo "        labels:"
    echo "          display_name: \"$name\""
    IFS=','
  done
  IFS=$old_ifs
}

# Write a named ping job to CONFIG_FILE
write_ping_job() {
  job_name="$1"
  targets="$2"

  cat >> "$CONFIG_FILE" <<EOF
  - job_name: "${job_name}"
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

write_static_targets() {
  expand_targets "$1" | while read -r t; do
    [ -n "$t" ] && echo "        - \"$t\""
  done
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

  cat >> "$CONFIG_FILE" <<EOF
  - job_name: "${job_name}"
    metrics_path: /snmp
    params:
      auth: [${SNMP_AUTH}]
      module: [${module}]
    static_configs:
EOF

  if has_labeled_targets "$targets"; then
    write_labeled_targets "$targets" >> "$CONFIG_FILE"
  else
    echo "      - targets: []" >> "$CONFIG_FILE"
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

rule_files:
  - /etc/prometheus/prometheus-alert-rules.yml

alerting:
  alertmanagers:
    - static_configs:
        - targets:
            - alertmanager:9093

scrape_configs:
  - job_name: "prometheus"
    static_configs:
      - targets: ["prometheus:9090"]
        labels:
          app: "prometheus"
EOF

# Infrastructure ping jobs
write_ping_job "infra-core-ping"  "$CORE_SWITCH_PING"
write_ping_job "infra-dist-ping"  "$DIST_SWITCH_PING"
write_ping_job "infra-fw-ping"    "$FIREWALL_PING"
write_ping_job "infra-srv-ping"   "$SERVER_PING"

# Infrastructure SNMP jobs for device uptime
SWITCH_SNMP_TARGETS="${CORE_SWITCH_PING}${CORE_SWITCH_PING:+,}${DIST_SWITCH_PING}"
FIREWALL_SNMP_UPTIME_TARGETS="$(apply_reference_names "$FIREWALL_SNMP_TARGETS" "$FIREWALL_PING")"
write_snmp_job "infra-switch-snmp" "$SWITCH_SNMP_TARGETS" "system_uptime"
write_snmp_job "infra-fw-snmp"     "$FIREWALL_SNMP_UPTIME_TARGETS" "system_uptime"

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

echo "Generated Prometheus config:"
echo "  Core:    ${CORE_SWITCH_PING:-none}"
echo "  Dist:    ${DIST_SWITCH_PING:-none}"
echo "  FW:      ${FIREWALL_PING:-none}"
echo "  Server:  ${SERVER_PING:-none}"
echo "  FW SNMP: ${FIREWALL_SNMP_TARGETS:-none}"
echo "  Players: $PLAYER_TARGETS_FILE"

exec /bin/prometheus \
  --config.file="$CONFIG_FILE" \
  --storage.tsdb.path=/prometheus \
  --web.console.libraries=/etc/prometheus/console_libraries \
  --web.console.templates=/etc/prometheus/consoles \
  --web.enable-lifecycle \
  --storage.tsdb.retention.time="$RETENTION_TIME"
