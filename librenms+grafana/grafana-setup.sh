#!/bin/sh
set -eu

GRAFANA_URL="${GRAFANA_URL:-http://grafana:3000}"
GRAFANA_USER="${GRAFANA_USER:-admin}"
GRAFANA_PASSWORD="${GRAFANA_PASSWORD:-root}"
GRAFANA_SETUP_TIMEOUT="${GRAFANA_SETUP_TIMEOUT:-180}"
GRAFANA_AUTH="${GRAFANA_AUTH:-Basic $(printf '%s' "${GRAFANA_USER}:${GRAFANA_PASSWORD}" | base64)}"

grafana_get() {
  curl -fsS -H "Authorization: $GRAFANA_AUTH" "$GRAFANA_URL$1"
}

grafana_send() {
  method=$1
  path=$2
  payload=$3

  curl -fsS -X "$method" \
    -H "Content-Type: application/json" \
    -H "Authorization: $GRAFANA_AUTH" \
    -d "$payload" \
    "$GRAFANA_URL$path"
}

upsert_datasource() {
  name=$1
  payload=$2

  existing=$(grafana_get "/api/datasources/name/$name" 2>/dev/null || true)
  datasource_id=$(echo "$existing" | jq -r '.id // empty' 2>/dev/null || true)

  if [ -n "$datasource_id" ]; then
    response=$(grafana_send PUT "/api/datasources/$datasource_id" "$payload")
    echo "  $name datasource updated: $(echo "$response" | jq -r '.message // .name // "ok"' 2>/dev/null || echo "$response")"
  else
    response=$(grafana_send POST "/api/datasources" "$payload")
    echo "  $name datasource created: $(echo "$response" | jq -r '.message // .name // "ok"' 2>/dev/null || echo "$response")"
  fi
}

echo "Waiting for Grafana to be ready..."
elapsed=0
while ! curl -fsS "$GRAFANA_URL/api/health" > /dev/null 2>&1; do
  if [ "$elapsed" -ge "$GRAFANA_SETUP_TIMEOUT" ]; then
    echo "ERROR: Grafana was not ready after ${GRAFANA_SETUP_TIMEOUT}s" >&2
    exit 1
  fi
  echo "Grafana not ready, waiting..."
  sleep 5
  elapsed=$((elapsed + 5))
done
echo "Grafana is ready!"

echo "Ensuring Prometheus datasource..."
prometheus_payload='{"name":"Prometheus","uid":"prometheus","type":"prometheus","url":"http://prometheus:9090","access":"proxy","isDefault":true,"editable":true}'
upsert_datasource "Prometheus" "$prometheus_payload"

echo "Dashboards are provisioned from /etc/grafana/provisioning/dashboard-json"

echo "Grafana setup completed!"
