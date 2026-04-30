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
SCRAPE_INTERVAL="${PROMETHEUS_SCRAPE_INTERVAL:-30s}"
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
  role_label="$3"

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
      - source_labels: [__param_target]
        target_label: instance
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
write_ping_job "infra-core-ping"  "$CORE_SWITCH_PING" "core"
write_ping_job "infra-dist-ping"  "$DIST_SWITCH_PING" "dist"
write_ping_job "infra-fw-ping"    "$FIREWALL_PING"    "firewall"
write_ping_job "infra-srv-ping"   "$SERVER_PING"      "server"

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
  echo "      - targets:" >> "$CONFIG_FILE"
  write_static_targets "$FIREWALL_SNMP_TARGETS" >> "$CONFIG_FILE"
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
