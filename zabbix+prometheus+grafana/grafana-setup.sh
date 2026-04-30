#!/bin/sh
set -u

# Install required packages
apk add --no-cache curl jq

GRAFANA_URL="${GRAFANA_URL:-http://grafana:3000}"
GRAFANA_AUTH="${GRAFANA_AUTH:-Basic YWRtaW46cm9vdA==}"

grafana_get() {
  curl -s -H "Authorization: $GRAFANA_AUTH" "$GRAFANA_URL$1"
}

grafana_send() {
  method=$1
  path=$2
  payload=$3

  curl -s -X "$method" \
    -H "Content-Type: application/json" \
    -H "Authorization: $GRAFANA_AUTH" \
    -d "$payload" \
    "$GRAFANA_URL$path"
}

upsert_datasource() {
  name=$1
  payload=$2

  existing=$(grafana_get "/api/datasources/name/$name")
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
while ! curl -s "$GRAFANA_URL/api/health" > /dev/null 2>&1; do
  echo "Grafana not ready, waiting..."
  sleep 5
done
echo "Grafana is ready!"

echo "Enabling Zabbix plugin..."
curl -s -X POST \
  -H "Content-Type: application/json" \
  -H "Authorization: $GRAFANA_AUTH" \
  -d '{"enabled": true, "pinned": true}' \
  "$GRAFANA_URL/api/plugins/alexanderzobnin-zabbix-app/settings" > /dev/null || true

echo "Ensuring Prometheus datasource..."
prometheus_payload='{"name":"Prometheus","uid":"prometheus","type":"prometheus","url":"http://prometheus:9090","access":"proxy","isDefault":true,"editable":true}'
upsert_datasource "Prometheus" "$prometheus_payload"

echo "Ensuring Zabbix datasource..."
zabbix_payload='{"name":"Zabbix","type":"alexanderzobnin-zabbix-datasource","url":"http://zabbix-web:8080/api_jsonrpc.php","access":"proxy","basicAuth":false,"jsonData":{"username":"Admin","trends":true,"trendsFrom":"7d","trendsRange":"4h","cacheTTL":"1h"},"secureJsonData":{"password":"zabbix"},"editable":true}'
upsert_datasource "Zabbix" "$zabbix_payload"

echo "Dashboards are provisioned from /etc/grafana/provisioning/dashboard-json"

echo "Grafana setup completed!"
