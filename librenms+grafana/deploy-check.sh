#!/bin/sh
# Verify that the monitoring platform started cleanly. This is deliberately
# separate from pre-match-check.sh: a fresh host can pass bootstrap before any
# venue switches, ISP links, players, UniFi controller, or Feishu app exist.

set -u

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR"

MODE=bootstrap
QUIET=false
JSON=false
MODE_SET=false

usage() {
  echo "usage: $0 [bootstrap|configured] [--quiet] [--json]" >&2
}

for arg in "$@"; do
  case "$arg" in
    bootstrap|configured)
      if [ "$MODE_SET" = true ]; then
        usage
        exit 1
      fi
      MODE=$arg
      MODE_SET=true
      ;;
    --quiet) QUIET=true ;;
    --json) JSON=true ;;
    *)
      usage
      exit 1
      ;;
  esac
done

DEPLOY_CHECK_TIMEOUT=${DEPLOY_CHECK_TIMEOUT:-180}
DEPLOY_CHECK_INTERVAL=${DEPLOY_CHECK_INTERVAL:-5}
DEPLOY_CHECK_HTTP_TIMEOUT=${DEPLOY_CHECK_HTTP_TIMEOUT:-3}
case "$DEPLOY_CHECK_TIMEOUT:$DEPLOY_CHECK_INTERVAL:$DEPLOY_CHECK_HTTP_TIMEOUT" in
  *[!0-9:]*|:*|*::*|*:) echo "deploy-check: timeout values must be non-negative integers" >&2; exit 1 ;;
esac

STARTED_AT=$(date +%s)
DEADLINE=$((STARTED_AT + DEPLOY_CHECK_TIMEOUT))
RESULTS_FILE=$(mktemp)
HTTP_BODY=$(mktemp)
HTTP_ERROR=$(mktemp)
ISP_STATE_BODY=$(mktemp)
ISP_VALIDATION=$(mktemp)
trap 'rm -f "$RESULTS_FILE" "$HTTP_BODY" "$HTTP_ERROR" "$ISP_STATE_BODY" "$ISP_VALIDATION"' EXIT HUP INT TERM

record() {
  status=$1
  check_id=$2
  message=$(printf '%s' "$3" | tr '\t\r\n' '   ')
  printf '%s\t%s\t%s\n' "$status" "$check_id" "$message" >> "$RESULTS_FILE"
}

now_seconds() {
  date +%s
}

deadline_reached() {
  [ "$(now_seconds)" -ge "$DEADLINE" ]
}

wait_interval() {
  sleep "$DEPLOY_CHECK_INTERVAL"
}

env_value() {
  key=$1
  [ -f "$SCRIPT_DIR/.env" ] || return 1
  command -v python3 >/dev/null 2>&1 || return 1
  python3 "$SCRIPT_DIR/platform_config.py" env-get "$SCRIPT_DIR/.env" "$key"
}

is_true() {
  case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

COMPOSE_STYLE=""
COMPOSE_BIN=""
PROJECT_DIR=${DEPLOY_CHECK_HOST_PROJECT_DIR:-$SCRIPT_DIR}

find_compose_v2() {
  if [ -n "${DEPLOY_CHECK_COMPOSE_BIN:-}" ] \
    && [ -x "$DEPLOY_CHECK_COMPOSE_BIN" ] \
    && "$DEPLOY_CHECK_COMPOSE_BIN" version >/dev/null 2>&1; then
    COMPOSE_STYLE=direct
    COMPOSE_BIN=$DEPLOY_CHECK_COMPOSE_BIN
    return 0
  fi
  if docker compose version >/dev/null 2>&1; then
    COMPOSE_STYLE=docker
    return 0
  fi
  for candidate in \
    /host/usr/libexec/docker/cli-plugins/docker-compose \
    /host/usr/lib/docker/cli-plugins/docker-compose \
    /host/usr/local/lib/docker/cli-plugins/docker-compose \
    /host/usr/local/libexec/docker/cli-plugins/docker-compose \
    /usr/libexec/docker/cli-plugins/docker-compose \
    /usr/lib/docker/cli-plugins/docker-compose \
    /usr/local/lib/docker/cli-plugins/docker-compose; do
    if [ -x "$candidate" ] && "$candidate" version >/dev/null 2>&1; then
      COMPOSE_STYLE=direct
      COMPOSE_BIN=$candidate
      return 0
    fi
  done
  return 1
}

compose() {
  if [ "$COMPOSE_STYLE" = docker ]; then
    docker compose \
      -f "$SCRIPT_DIR/docker-compose.yml" \
      --env-file "$SCRIPT_DIR/.env" \
      --project-directory "$PROJECT_DIR" \
      "$@"
  else
    "$COMPOSE_BIN" \
      -f "$SCRIPT_DIR/docker-compose.yml" \
      --env-file "$SCRIPT_DIR/.env" \
      --project-directory "$PROJECT_DIR" \
      "$@"
  fi
}

http_get() {
  url=$1
  if [ "${PLATFORM_API_SELF_APPLY:-false}" = true ]; then
    # The platform container exposes host tools under /host/usr/bin so Docker
    # remains available. A host curl found there cannot load its host libcurl
    # inside python:slim; use the container's own Python for every self-apply
    # HTTP probe instead of treating command presence as runtime capability.
    python3 -c 'import sys, urllib.request; sys.stdout.buffer.write(urllib.request.urlopen(sys.argv[1], timeout=int(sys.argv[2])).read())' \
      "$url" "$DEPLOY_CHECK_HTTP_TIMEOUT"
  elif command -v curl >/dev/null 2>&1; then
    curl -fsS --max-time "$DEPLOY_CHECK_HTTP_TIMEOUT" "$url"
  else
    python3 -c 'import sys, urllib.request; sys.stdout.buffer.write(urllib.request.urlopen(sys.argv[1], timeout=int(sys.argv[2])).read())' \
      "$url" "$DEPLOY_CHECK_HTTP_TIMEOUT"
  fi
}

wait_for_service() {
  check_id=$1
  service=$2
  label=$3
  last_state=unknown
  while :; do
    container_id=$(compose ps -a -q "$service" 2>/dev/null | sed -n '1p')
    if [ -z "$container_id" ]; then
      record FAIL "$check_id" "$label container does not exist"
      return 1
    fi
    state=$(docker inspect --format '{{.State.Status}}' "$container_id" 2>/dev/null || true)
    health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id" 2>/dev/null || true)
    last_state=${state:-inspect-failed}
    [ -n "$health" ] && [ "$health" != none ] && last_state="$last_state/$health"
    case "$state:$health" in
      running:healthy|running:none)
        health_note=""
        [ "$health" = healthy ] && health_note=" and healthy"
        record PASS "$check_id" "$label running$health_note"
        return 0
        ;;
      running:unhealthy)
        record FAIL "$check_id" "$label is unhealthy"
        return 1
        ;;
      exited:*|dead:*|removing:*|paused:*)
        exit_code=$(docker inspect --format '{{.State.ExitCode}}' "$container_id" 2>/dev/null || true)
        record FAIL "$check_id" "$label is $state${exit_code:+ (exit $exit_code)}"
        return 1
        ;;
      running:starting|created:*|restarting:*|running:*) : ;;
      *)
        record FAIL "$check_id" "$label state could not be inspected"
        return 1
        ;;
    esac
    if deadline_reached; then
      record FAIL "$check_id" "$label timed out after ${DEPLOY_CHECK_TIMEOUT}s (last state: $last_state)"
      return 1
    fi
    wait_interval
  done
}

wait_for_http() {
  check_id=$1
  label=$2
  url=$3
  last_error="unreachable"
  while :; do
    if http_get "$url" > "$HTTP_BODY" 2> "$HTTP_ERROR"; then
      record PASS "$check_id" "$label"
      return 0
    fi
    last_error=$(tr '\r\n' '  ' < "$HTTP_ERROR" | sed 's/[[:space:]][[:space:]]*/ /g; s/^ //; s/ $//')
    [ -n "$last_error" ] || last_error="temporarily unreachable"
    if deadline_reached; then
      record FAIL "$check_id" "$label timed out after ${DEPLOY_CHECK_TIMEOUT}s (last error: $last_error)"
      return 1
    fi
    wait_interval
  done
}

wait_for_platform_api() {
  if [ "${PLATFORM_API_SELF_APPLY:-false}" = true ]; then
    wait_for_http platform_api_http "Platform API reachable" \
      "http://127.0.0.1:9200/health"
    return $?
  fi

  last_error="container health endpoint unreachable"
  while :; do
    if compose exec -T platform-api python -c '
import sys
import urllib.request

with urllib.request.urlopen(
    "http://127.0.0.1:9200/health", timeout=int(sys.argv[1])
) as response:
    response.read()
    if not 200 <= response.status < 300:
        raise SystemExit(1)
' "$DEPLOY_CHECK_HTTP_TIMEOUT" > "$HTTP_BODY" 2> "$HTTP_ERROR"; then
      record PASS platform_api_http "Platform API reachable"
      return 0
    fi
    last_error=$(tr '\r\n' '  ' < "$HTTP_ERROR" | sed 's/[[:space:]][[:space:]]*/ /g; s/^ //; s/ $//')
    [ -n "$last_error" ] || last_error="container health endpoint temporarily unreachable"
    if deadline_reached; then
      record FAIL platform_api_http "Platform API container /health timed out after ${DEPLOY_CHECK_TIMEOUT}s (last error: $last_error)"
      return 1
    fi
    wait_interval
  done
}

wait_for_prometheus_reload() {
  check_id=prometheus_reload
  last_state="reload metric unavailable"
  while :; do
    if http_get "$PROMETHEUS_URL/metrics" > "$HTTP_BODY" 2> "$HTTP_ERROR"; then
      if grep -Eq '^prometheus_config_last_reload_successful[[:space:]]+1([.]0)?$' "$HTTP_BODY"; then
        record PASS "$check_id" "Prometheus last configuration reload succeeded"
        return 0
      fi
      last_state="last reload has not succeeded"
    else
      last_state=$(tr '\r\n' '  ' < "$HTTP_ERROR" | sed 's/[[:space:]][[:space:]]*/ /g; s/^ //; s/ $//')
      [ -n "$last_state" ] || last_state="Prometheus metrics unreachable"
    fi
    if deadline_reached; then
      record FAIL "$check_id" "Prometheus reload check timed out after ${DEPLOY_CHECK_TIMEOUT}s (last state: $last_state)"
      return 1
    fi
    wait_interval
  done
}

profile_enabled() {
  profile=$1
  normalized=$(printf '%s' "$COMPOSE_PROFILES_VALUE" | tr ' ' ',')
  case ",$normalized," in
    *,$profile,*) return 0 ;;
    *) return 1 ;;
  esac
}

extract_ipv4() {
  printf '%s' "$1" | grep -Eo '([0-9]{1,3}\.){3}[0-9]{1,3}' | sort -u || true
}

wait_for_prometheus_targets() {
  check_id=$1
  label=$2
  configured=$3
  required_marker=${4:-}
  expected_ips=$(extract_ipv4 "$configured")
  if [ -z "$expected_ips" ] && [ -z "$required_marker" ]; then
    record FAIL "$check_id" "$label (configured value contains no IPv4 target)"
    return 1
  fi
  last_state="targets not loaded"
  while :; do
    if http_get "$PROMETHEUS_URL/api/v1/targets?state=any" > "$HTTP_BODY" 2> "$HTTP_ERROR"; then
      found=true
      if [ -n "$required_marker" ] && ! grep -Fq "$required_marker" "$HTTP_BODY"; then
        found=false
        last_state="expected job $required_marker is absent"
      fi
      for expected_ip in $expected_ips; do
        if ! grep -Fq "$expected_ip" "$HTTP_BODY"; then
          found=false
          last_state="target $expected_ip is absent"
          break
        fi
      done
      if [ "$found" = true ]; then
        record PASS "$check_id" "$label"
        return 0
      fi
    else
      last_state=$(tr '\r\n' '  ' < "$HTTP_ERROR" | sed 's/[[:space:]][[:space:]]*/ /g; s/^ //; s/ $//')
      [ -n "$last_state" ] || last_state="Prometheus targets API unreachable"
    fi
    if deadline_reached; then
      record FAIL "$check_id" "$label timed out after ${DEPLOY_CHECK_TIMEOUT}s (last state: $last_state)"
      return 1
    fi
    wait_interval
  done
}

wait_for_player_generator() {
  check_id=player_generator
  last_state="status unavailable"
  while :; do
    if http_get "$BIGSCREEN_URL/player-targets/status" > "$HTTP_BODY" 2> "$HTTP_ERROR"; then
      if grep -Eq '"ok"[[:space:]]*:[[:space:]]*true' "$HTTP_BODY"; then
        record PASS "$check_id" "Player target generator completed successfully"
        return 0
      fi
      last_state="generator reported an error"
    else
      last_state=$(tr '\r\n' '  ' < "$HTTP_ERROR" | sed 's/[[:space:]][[:space:]]*/ /g; s/^ //; s/ $//')
      [ -n "$last_state" ] || last_state="player target status unreachable"
    fi
    if deadline_reached; then
      record FAIL "$check_id" "Player target generator timed out after ${DEPLOY_CHECK_TIMEOUT}s (last state: $last_state)"
      return 1
    fi
    wait_interval
  done
}

wait_for_isp_inventory() {
  check_id=configured_isp
  names=$1
  ips=$2
  bandwidth=$3
  refresh_seconds=$4
  case "$refresh_seconds" in
    ''|*[!0-9]*) refresh_seconds=60 ;;
  esac
  max_age=$((refresh_seconds * 3 + 60))
  [ "$max_age" -ge 300 ] || max_age=300
  last_state="inventory unavailable"

  while :; do
    if http_get "$BIGSCREEN_URL/topology/isp_targets.json" > "$HTTP_BODY" 2> "$HTTP_ERROR" \
      && http_get "$BIGSCREEN_URL/topology/isp-discovery-state.json" > "$ISP_STATE_BODY" 2> "$HTTP_ERROR" \
      && python3 - "$HTTP_BODY" "$ISP_STATE_BODY" "$names" "$ips" "$bandwidth" "$max_age" \
        > "$ISP_VALIDATION" 2> "$HTTP_ERROR" <<'PY'
import ipaddress
import json
import sys
import time

inventory_path, state_path, names_raw, ips_raw, bandwidth_raw, max_age_raw = sys.argv[1:]
with open(inventory_path, encoding="utf-8") as handle:
    inventory = json.load(handle)
with open(state_path, encoding="utf-8") as handle:
    state = json.load(handle)
if not isinstance(inventory, list):
    raise SystemExit("inventory is not an array")
if not inventory:
    raise SystemExit("inventory is empty")

seen = set()
rows = []
for index, entry in enumerate(inventory):
    labels = entry.get("labels") if isinstance(entry, dict) else None
    targets = entry.get("targets") if isinstance(entry, dict) else None
    if not isinstance(labels, dict) or not isinstance(targets, list) or len(targets) != 1:
        raise SystemExit(f"entry {index} has invalid file_sd schema")
    name = str(labels.get("display_name") or "").strip()
    if not name:
        raise SystemExit(f"entry {index} has no display_name")
    key = name.casefold()
    if key in seen:
        raise SystemExit(f"duplicate display_name: {name}")
    seen.add(key)
    try:
        gateway = str(ipaddress.IPv4Address(str(targets[0]).strip()))
    except ipaddress.AddressValueError:
        raise SystemExit(f"entry {index} has invalid gateway")
    wan_ip = str(labels.get("wan_ip") or "").strip()
    if wan_ip:
        try:
            wan_ip = str(ipaddress.IPv4Address(wan_ip))
        except ipaddress.AddressValueError:
            raise SystemExit(f"entry {index} has invalid wan_ip")
    rows.append({"name": name, "gateway": gateway, "wan_ip": wan_ip})

if not isinstance(state, dict) or state.get("status") != "ok":
    raise SystemExit(f"discovery state is {state.get('status', 'invalid') if isinstance(state, dict) else 'invalid'}")
last_success = state.get("last_success_at")
if not isinstance(last_success, (int, float)):
    raise SystemExit("discovery state has no valid last_success_at")
age = time.time() - float(last_success)
if age < -300 or age > int(max_age_raw):
    raise SystemExit(f"inventory is stale ({max(0, int(age))}s old)")
if state.get("count") != len(rows):
    raise SystemExit("discovery state count does not match inventory")

configured_ips = {}
for item in ips_raw.replace("\n", ",").split(","):
    name, separator, value = item.strip().partition(":")
    if separator and name.strip() and value.strip():
        configured_ips[name.strip().casefold()] = value.strip()

metadata_names = [name.strip() for name in names_raw.split(",") if name.strip()]
for item in bandwidth_raw.split(","):
    name, separator, _value = item.strip().rpartition(":")
    if separator and name.strip() and name.strip() != "*":
        metadata_names.append(name.strip())
for item in ips_raw.replace("\n", ",").split(","):
    name, separator, _value = item.strip().partition(":")
    if separator and name.strip():
        metadata_names.append(name.strip())

for name in dict.fromkeys(metadata_names):
    matches = [row for row in rows if row["name"].casefold() == name.casefold()]
    if len(matches) != 1:
        raise SystemExit(f"manual ISP metadata is not safely matched: {name}")
    configured_ip = configured_ips.get(name.casefold(), "")
    if configured_ip and matches[0]["wan_ip"] != configured_ip:
        raise SystemExit(f"manual ISP WAN IP does not match discovery: {name}")

print(f"validated {len(rows)} fresh ISP target(s)")
PY
    then
      validation=$(tr '\r\n' '  ' < "$ISP_VALIDATION" | sed 's/[[:space:]][[:space:]]*/ /g; s/^ //; s/ $//')
      record PASS "$check_id" "Automatic ISP inventory is valid and fresh${validation:+ ($validation)}"
      return 0
    fi

    last_state=$(tr '\r\n' '  ' < "$HTTP_ERROR" | sed 's/[[:space:]][[:space:]]*/ /g; s/^ //; s/ $//')
    [ -n "$last_state" ] || last_state="inventory or state endpoint unavailable"
    if deadline_reached; then
      record FAIL "$check_id" "Automatic ISP inventory validation failed (last state: $last_state)"
      return 1
    fi
    wait_interval
  done
}

finalize() {
  ended_at=$(now_seconds)
  duration=$((ended_at - STARTED_AT))
  passed=$(awk -F '\t' '$1 == "PASS" {count++} END {print count + 0}' "$RESULTS_FILE")
  failed=$(awk -F '\t' '$1 == "FAIL" {count++} END {print count + 0}' "$RESULTS_FILE")
  skipped=$(awk -F '\t' '$1 == "SKIP" {count++} END {print count + 0}' "$RESULTS_FILE")
  result=PASS
  [ "$failed" -eq 0 ] || result=FAIL

  if [ "$JSON" = true ]; then
    python3 - "$MODE" "$result" "$passed" "$failed" "$skipped" "$duration" "$RESULTS_FILE" <<'PY'
import json
import sys

mode, result, passed, failed, skipped, duration, path = sys.argv[1:]
checks = []
with open(path, encoding="utf-8") as handle:
    for line in handle:
        status, check_id, message = line.rstrip("\n").split("\t", 2)
        checks.append({"id": check_id, "status": status, "message": message})
print(json.dumps({
    "mode": mode,
    "result": result,
    "passed": int(passed),
    "failed": int(failed),
    "skipped": int(skipped),
    "checks": checks,
    "duration_seconds": int(duration),
}, ensure_ascii=False, separators=(",", ":")))
PY
  else
    if [ "$QUIET" != true ]; then
      echo "Deployment check:"
      awk -F '\t' '{printf "%-5s %s\n", $1, $3}' "$RESULTS_FILE"
      echo ""
    fi
    summary="$passed passed"
    [ "$failed" -eq 0 ] || summary="$summary, $failed failed"
    [ "$skipped" -eq 0 ] || summary="$summary, $skipped skipped"
    echo "Result: $result ($summary)"
  fi

  [ "$failed" -eq 0 ]
}

if command -v docker >/dev/null 2>&1; then
  record PASS docker "Docker command available"
else
  record FAIL docker "Docker command is not available"
  finalize
  exit $?
fi

if find_compose_v2; then
  record PASS compose_v2 "Docker Compose v2 available"
else
  record FAIL compose_v2 "Docker Compose v2 is not available"
  finalize
  exit $?
fi

if compose config --quiet > "$HTTP_BODY" 2> "$HTTP_ERROR"; then
  record PASS compose_config "Compose configuration valid"
else
  compose_error=$(tr '\r\n' '  ' < "$HTTP_ERROR" | sed 's/[[:space:]][[:space:]]*/ /g; s/^ //; s/ $//')
  record FAIL compose_config "Compose configuration invalid${compose_error:+: $compose_error}"
  finalize
  exit $?
fi

if [ -f "$SCRIPT_DIR/event-config.yml" ]; then
  if python3 - "$SCRIPT_DIR" <<'PY' > "$HTTP_BODY" 2> "$HTTP_ERROR"
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from platform_config import parse_simple_yaml

path = Path(sys.argv[1]) / "event-config.yml"
config = parse_simple_yaml(path.read_text(encoding="utf-8"))
if not isinstance(config, dict):
    raise SystemExit("event-config.yml is not a mapping")
PY
  then
    record PASS event_config "event-config.yml parsed safely"
  else
    config_error=$(tr '\r\n' '  ' < "$HTTP_ERROR" | sed 's/[[:space:]][[:space:]]*/ /g; s/^ //; s/ $//')
    record FAIL event_config "event-config.yml parse failed${config_error:+: $config_error}"
  fi
else
  record SKIP event_config "event-config.yml not present"
fi

if [ -f "$SCRIPT_DIR/.env" ] \
  && python3 - "$SCRIPT_DIR" <<'PY' > "$HTTP_BODY" 2> "$HTTP_ERROR"
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from platform_config import read_env

read_env(Path(sys.argv[1]) / ".env")
PY
then
  record PASS env "Environment file parsed safely"
else
  env_error=$(tr '\r\n' '  ' < "$HTTP_ERROR" | sed 's/[[:space:]][[:space:]]*/ /g; s/^ //; s/ $//')
  record FAIL env "Environment file could not be parsed${env_error:+: $env_error}"
fi

if grep -q '^FAIL' "$RESULTS_FILE"; then
  finalize
  exit $?
fi

PROMETHEUS_PORT=$(env_value PROMETHEUS_PORT 2>/dev/null || true)
PROMETHEUS_PORT=${PROMETHEUS_PORT:-9090}
GRAFANA_PORT=$(env_value GRAFANA_PORT 2>/dev/null || true)
GRAFANA_PORT=${GRAFANA_PORT:-3000}
LIBRENMS_PORT=$(env_value LIBRENMS_PORT 2>/dev/null || true)
LIBRENMS_PORT=${LIBRENMS_PORT:-8002}
BIGSCREEN_PORT=$(env_value BIGSCREEN_PORT 2>/dev/null || true)
BIGSCREEN_PORT=${BIGSCREEN_PORT:-8088}
BLACKBOX_EXPORTER_PORT=$(env_value BLACKBOX_EXPORTER_PORT 2>/dev/null || true)
BLACKBOX_EXPORTER_PORT=${BLACKBOX_EXPORTER_PORT:-9115}
SNMP_EXPORTER_PORT=$(env_value SNMP_EXPORTER_PORT 2>/dev/null || true)
SNMP_EXPORTER_PORT=${SNMP_EXPORTER_PORT:-9116}
COMPOSE_PROFILES_VALUE=${COMPOSE_PROFILES:-$(env_value COMPOSE_PROFILES 2>/dev/null || true)}

if [ "${PLATFORM_API_SELF_APPLY:-false}" = true ]; then
  PROMETHEUS_URL="http://prometheus:9090"
  GRAFANA_URL="http://grafana:3000"
  LIBRENMS_URL="http://librenms:8000"
  BIGSCREEN_URL="http://bigscreen"
  BLACKBOX_EXPORTER_URL="http://blackbox-exporter:9115"
  SNMP_EXPORTER_URL="http://snmp-exporter:9116"
else
  PROMETHEUS_URL="http://127.0.0.1:$PROMETHEUS_PORT"
  GRAFANA_URL="http://127.0.0.1:$GRAFANA_PORT"
  LIBRENMS_URL="http://127.0.0.1:$LIBRENMS_PORT"
  BIGSCREEN_URL="http://127.0.0.1:$BIGSCREEN_PORT"
  BLACKBOX_EXPORTER_URL="http://127.0.0.1:$BLACKBOX_EXPORTER_PORT"
  SNMP_EXPORTER_URL="http://127.0.0.1:$SNMP_EXPORTER_PORT"
fi

for service_spec in \
  'prometheus|prometheus|Prometheus' \
  'grafana|grafana|Grafana' \
  'topology_collector|topology-collector|Topology collector' \
  'player_targets|player-targets|Player targets' \
  'bigscreen|bigscreen|Bigscreen' \
  'platform_api|platform-api|Platform API' \
  'blackbox_exporter|blackbox-exporter|Blackbox exporter' \
  'snmp_exporter|snmp-exporter|SNMP exporter' \
  'librenms|librenms|LibreNMS' \
  'librenms_dispatcher|librenms-dispatcher|LibreNMS dispatcher' \
  'librenms_db|librenms-db|LibreNMS database' \
  'librenms_redis|librenms-redis|LibreNMS Redis' \
  'librenms_rrdcached|librenms-rrdcached|LibreNMS RRDCached'; do
  check_id=${service_spec%%|*}
  remainder=${service_spec#*|}
  service=${remainder%%|*}
  label=${remainder#*|}
  wait_for_service "$check_id" "$service" "$label" || true
done

if profile_enabled unifi; then
  wait_for_service unifi_profile unpoller "UniFi profile service" || true
else
  record SKIP unifi_profile "UniFi profile not enabled"
fi
if profile_enabled feishu; then
  wait_for_service feishu_profile feishu-ws "Feishu profile service" || true
else
  record SKIP feishu_profile "Feishu profile not enabled"
fi

wait_for_http prometheus_http "Prometheus healthy" "$PROMETHEUS_URL/-/healthy" || true
wait_for_http grafana_http "Grafana API healthy" "$GRAFANA_URL/api/health" || true
wait_for_http librenms_http "LibreNMS reachable" "$LIBRENMS_URL/" || true
wait_for_http bigscreen_http "Bigscreen reachable" "$BIGSCREEN_URL/" || true
wait_for_platform_api || true
wait_for_http blackbox_http "Blackbox exporter reachable" "$BLACKBOX_EXPORTER_URL/" || true
wait_for_http snmp_http "SNMP exporter reachable" "$SNMP_EXPORTER_URL/" || true
wait_for_prometheus_reload || true

if [ "$MODE" = configured ]; then
  # Configured checks share the bootstrap deadline. Resetting it here allowed
  # one invocation to consume two full timeout windows and outlive its parent
  # platform-api apply process.
  CORE_SWITCH_VALUE=$(env_value CORE_SWITCH_PING 2>/dev/null || true)
  TOURNAMENT_SWITCHES_VALUE=$(env_value TOURNAMENT_SWITCHES 2>/dev/null || true)
  FIREWALL_VALUE=$(env_value FIREWALL_PING 2>/dev/null || true)
  FIREWALL_SNMP_VALUE=$(env_value FIREWALL_SNMP_TARGETS 2>/dev/null || true)
  if ISP_AUTO_VALUE=$(env_value BIGSCREEN_ISP_AUTO_DISCOVER 2>/dev/null); then
    : # An explicitly empty value is false, like every other non-truthy alias.
  elif ISP_AUTO_VALUE=$(env_value ISP_GATEWAY_AUTO_DISCOVER 2>/dev/null); then
    :
  else
    ISP_AUTO_VALUE=true
  fi
  ISP_PING_VALUE=$(env_value ISP_PING 2>/dev/null || true)
  ISP_NAMES_VALUE=$(env_value BIGSCREEN_ISP_NAMES 2>/dev/null || true)
  ISP_IPS_VALUE=$(env_value BIGSCREEN_ISP_IPS 2>/dev/null || true)
  ISP_BANDWIDTH_VALUE=$(env_value BIGSCREEN_ISP_MAX_BANDWIDTH 2>/dev/null || true)
  ISP_REFRESH_VALUE=$(env_value ISP_DISCOVERY_REFRESH_INTERVAL 2>/dev/null || true)
  PLAYER_SUBNETS_VALUE=$(env_value PLAYER_SUBNETS 2>/dev/null || true)

  if [ -n "$CORE_SWITCH_VALUE" ]; then
    wait_for_prometheus_targets configured_core "Configured core switch is present in Prometheus targets" "$CORE_SWITCH_VALUE" || true
  else
    record SKIP configured_core "Core switch not configured"
  fi
  if [ -n "$TOURNAMENT_SWITCHES_VALUE" ]; then
    wait_for_prometheus_targets configured_stage "Configured stage switches are present in discovery targets" "$TOURNAMENT_SWITCHES_VALUE" || true
  else
    record SKIP configured_stage "Stage switches not configured"
  fi
  if [ -n "$FIREWALL_VALUE$FIREWALL_SNMP_VALUE" ]; then
    wait_for_prometheus_targets configured_firewall "Configured firewall is present in Prometheus targets" "$FIREWALL_VALUE,$FIREWALL_SNMP_VALUE" || true
  else
    record SKIP configured_firewall "Firewall not configured"
  fi
  if is_true "$ISP_AUTO_VALUE" && [ -n "$FIREWALL_SNMP_VALUE" ] && [ -z "$ISP_PING_VALUE" ]; then
    wait_for_isp_inventory \
      "$ISP_NAMES_VALUE" "$ISP_IPS_VALUE" "$ISP_BANDWIDTH_VALUE" "$ISP_REFRESH_VALUE" || true
  elif [ -n "$ISP_PING_VALUE" ] \
    || { ! is_true "$ISP_AUTO_VALUE" && [ -n "$ISP_IPS_VALUE" ]; }; then
    ISP_MANUAL_TARGETS_VALUE=$ISP_PING_VALUE
    [ -n "$ISP_MANUAL_TARGETS_VALUE" ] || ISP_MANUAL_TARGETS_VALUE=$ISP_IPS_VALUE
    wait_for_prometheus_targets configured_isp "Configured ISP monitoring target is present" "$ISP_MANUAL_TARGETS_VALUE" "infra-isp-ping" || true
  else
    record SKIP configured_isp "ISP not configured"
  fi
  if [ -n "$PLAYER_SUBNETS_VALUE" ] && [ -n "$TOURNAMENT_SWITCHES_VALUE" ]; then
    wait_for_player_generator || true
  else
    record SKIP player_generator "Player target generator check skipped until player networks and stage switches are configured"
  fi
fi

finalize
