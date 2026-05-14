#!/bin/sh
set -eu

src="${GRAFANA_PROVISIONING_SRC:-/grafana-provisioning-src}"
out="${GRAFANA_PROVISIONING_OUT:-/grafana-provisioning-out}"

is_true() {
  case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

csv_to_regex() {
  printf '%s' "$1" | jq -Rr '
    split(",")
    | map(gsub("^\\s+|\\s+$"; ""))
    | map(select(length > 0))
    | join("|")
  '
}

rm -rf "$out"/*
mkdir -p "$out"
cp -R "$src"/. "$out"/

dashboard_file="$out/dashboard-json/event-infra.json"
if [ -f "$dashboard_file" ]; then
  if is_true "${BIGSCREEN_ISP_AUTO_DISCOVER:-false}"; then
    wan_filter="$(csv_to_regex "${FIREWALL_WAN_IF_FILTER:-telecom,telcom,unicom,isp,WAN}")"
  else
    wan_filter="$(csv_to_regex "${BIGSCREEN_ISP_NAMES:-ISP1,ISP2}")"
  fi
  [ -n "$wan_filter" ] || wan_filter="ISP1|ISP2"

  tmp_file="${dashboard_file}.tmp"
  jq --arg wan_filter "$wan_filter" '
    (.templating.list[] | select(.name == "wan_filter") | .current.text) = $wan_filter
    | (.templating.list[] | select(.name == "wan_filter") | .current.value) = $wan_filter
    | (.templating.list[] | select(.name == "wan_filter") | .query) = $wan_filter
    | .version = ((.version // 0) + 1)
  ' "$dashboard_file" > "$tmp_file" && mv "$tmp_file" "$dashboard_file"

  echo "Event Infrastructure WAN filter: $wan_filter"
fi
