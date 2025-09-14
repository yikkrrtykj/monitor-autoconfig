#!/bin/sh

# Install required packages
apk add --no-cache curl jq

echo "Waiting for Grafana to be ready..."
while ! curl -s http://grafana:3000/api/health > /dev/null 2>&1; do
  echo "Grafana not ready, waiting..."
  sleep 5
done
echo "Grafana is ready!"

echo "Enabling Zabbix plugin..."
curl -s -X POST \
  -H "Content-Type: application/json" \
  -H "Authorization: Basic YWRtaW46cm9vdA==" \
  -d '{"enabled": true, "pinned": true}' \
  http://grafana:3000/api/plugins/alexanderzobnin-zabbix-app/settings

echo "Adding Prometheus datasource..."
curl -s -X POST \
  -H "Content-Type: application/json" \
  -H "Authorization: Basic YWRtaW46cm9vdA==" \
  -d '{"name":"Prometheus","type":"prometheus","url":"http://prometheus:9090","access":"proxy","isDefault":false}' \
  http://grafana:3000/api/datasources

# Get Prometheus datasource UID dynamically
echo "Getting Prometheus datasource UID..."
PROMETHEUS_UID=$(curl -s -H "Authorization: Basic YWRtaW46cm9vdA==" \
  http://grafana:3000/api/datasources/name/Prometheus | jq -r '.uid')
echo "Prometheus UID: $PROMETHEUS_UID"

echo "Adding Zabbix datasource..."
curl -s -X POST \
  -H "Content-Type: application/json" \
  -H "Authorization: Basic YWRtaW46cm9vdA==" \
  -d '{"name":"Zabbix","type":"alexanderzobnin-zabbix-datasource","url":"http://zabbix-web:8080/api_jsonrpc.php","access":"proxy","basicAuth":false,"jsonData":{"username":"Admin","trends":true,"trendsFrom":"7d","trendsRange":"4h","cacheTTL":"1h","dbConnectionEnable":true,"dbConnectionDatasourceId":1},"secureJsonData":{"password":"zabbix"}}' \
  http://grafana:3000/api/datasources

echo "Importing SNMP Exporter dashboard..."
if [ -f /snmp-exporter.json ]; then
  import_payload=$(jq -n --slurpfile dash /snmp-exporter.json '{dashboard: $dash[0], overwrite: true, folderId: 0, inputs: [{name:"DS_PROMETHEUS", type:"datasource", pluginId:"prometheus", value:"Prometheus"}]}')
  curl -s -X POST \
    -H "Content-Type: application/json" \
    -H "Authorization: Basic YWRtaW46cm9vdA==" \
    -d "$import_payload" \
    http://grafana:3000/api/dashboards/import
  echo "SNMP Exporter dashboard import attempted"
fi

echo "Importing SNMP Stats dashboard..."
if [ -f /snmp-stats.json ]; then
  # Create a temporary file with updated UID and remove id field
  jq 'del(.id)' /snmp-stats.json > /tmp/snmp-stats-temp.json
  # Replace placeholder UID with the dynamic one
  sed -i "s/PROMETHEUS_UID_PLACEHOLDER/$PROMETHEUS_UID/g" /tmp/snmp-stats-temp.json
  
  import_payload=$(jq -n --slurpfile dash /tmp/snmp-stats-temp.json '{dashboard: $dash[0], overwrite: true, folderId: 0}')
  curl -s -X POST \
    -H "Content-Type: application/json" \
    -H "Authorization: Basic YWRtaW46cm9vdA==" \
    -d "$import_payload" \
    http://grafana:3000/api/dashboards/import
  echo "SNMP Stats dashboard import attempted"
  rm -f /tmp/snmp-stats-temp.json
fi

echo "Importing Blackbox ICMP dashboard..."
if [ -f /blackbox-icmp.json ]; then
  # Create a temporary file with updated UID and remove id field
  jq 'del(.id)' /blackbox-icmp.json > /tmp/blackbox-icmp-temp.json
  # Replace placeholder UID with the dynamic one
  sed -i "s/PROMETHEUS_UID_PLACEHOLDER/$PROMETHEUS_UID/g" /tmp/blackbox-icmp-temp.json
  
  import_payload=$(jq -n --slurpfile dash /tmp/blackbox-icmp-temp.json '{dashboard: $dash[0], overwrite: true, folderId: 0}')
  curl -s -X POST \
    -H "Content-Type: application/json" \
    -H "Authorization: Basic YWRtaW46cm9vdA==" \
    -d "$import_payload" \
    http://grafana:3000/api/dashboards/import
  echo "Blackbox ICMP dashboard import attempted"
  rm -f /tmp/blackbox-icmp-temp.json
fi

echo "Grafana setup completed!"