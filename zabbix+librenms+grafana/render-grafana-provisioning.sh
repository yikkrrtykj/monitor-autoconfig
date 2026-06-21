#!/bin/sh
set -eu

# 渲染 Grafana provisioning：把 ISP/WAN 过滤词注入仪表盘模板变量。
# 用 python3（python:3-alpine 自带）而不是运行时 apk 装 jq——避免在弱网/被墙环境
# 卡在 apk add，导致整个 deploy 一直起不来。

src="${GRAFANA_PROVISIONING_SRC:-/grafana-provisioning-src}"
out="${GRAFANA_PROVISIONING_OUT:-/grafana-provisioning-out}"

is_true() {
  case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

# CSV 关键词 -> 正则（逐个转义元字符，用 | 连接）。
csv_to_regex() {
  python3 - "${1:-}" <<'PY'
import sys, re
parts = [p.strip() for p in sys.argv[1].split(",") if p.strip()]
print("|".join(re.escape(p) for p in parts))
PY
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

  python3 - "$dashboard_file" "$wan_filter" <<'PY'
import sys, json
path, wan = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as f:
    d = json.load(f)
for v in d.get("templating", {}).get("list", []):
    if v.get("name") == "wan_filter":
        cur = v.setdefault("current", {})
        cur["text"] = wan
        cur["value"] = wan
        v["query"] = wan
d["version"] = (d.get("version") or 0) + 1
with open(path, "w", encoding="utf-8") as f:
    json.dump(d, f, ensure_ascii=False, indent=2)
PY

  echo "Event Infrastructure WAN filter: $wan_filter"
fi
