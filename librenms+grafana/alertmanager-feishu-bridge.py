#!/usr/bin/env python3
"""
LibreNMS webhook -> Feishu bot bridge + device-online watcher.

Stdlib only (http.server + urllib + json + threading) so the container
runs on python:3-slim with no requirements.txt.

Env:
  FEISHU_BRIDGE_PORT      listen port (default 5005)
  FEISHU_ROBOT_TOKEN      Feishu bot webhook token
  FEISHU_BRIDGE_DRY_RUN   true = log payloads, never POST to Feishu
  LIBRENMS_URL            LibreNMS internal URL (e.g. http://librenms:8000)
  LIBRENMS_API_TOKEN      LibreNMS API token (falls back to token file)
  LIBRENMS_TOKEN_FILE     path to token file written by librenms-config
                          (default /librenms-data/librenms-api-token)
  SWITCH_WATCH_INTERVAL   seconds between device-list polls (default 120)
  PROMETHEUS_URL          Prometheus internal URL (default http://prometheus:9090)
  ISP_ALERT_ENABLED       true = watch firewall WAN bandwidth (default true)
  ISP_ALERT_FOR_SECONDS   seconds above threshold before alerting (default 10)
  ISP_ALERT_POLL_INTERVAL seconds between checks (default 5)
  ISP_ALERT_RATE_WINDOW   Prometheus rate() window (default 1m)
  ISP_ALERT_STATUS_INTERVAL seconds between status logs (default 30)
  ISP_ALERT_RESOLVE_SECONDS seconds below threshold before recovery (default 30)
  FIREWALL_WAN_IF_FILTER  WAN interface label keywords
  BIGSCREEN_ISP_MAX_BANDWIDTH ISP bandwidth Mbps config
  ISP_SATURATION_PERCENT  alert threshold percent of configured bandwidth
"""
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import sys
import threading
import time
from urllib import error, parse, request

PORT = int(os.environ.get("FEISHU_BRIDGE_PORT", "5005"))
DRY_RUN = os.environ.get("FEISHU_BRIDGE_DRY_RUN", "").lower() in ("1", "true", "yes", "on")

LIBRENMS_URL = os.environ.get("LIBRENMS_URL", "").rstrip("/")
LIBRENMS_API_TOKEN = os.environ.get("LIBRENMS_API_TOKEN", "")
LIBRENMS_TOKEN_FILE = os.environ.get("LIBRENMS_TOKEN_FILE", "/librenms-data/librenms-api-token")
SWITCH_WATCH_INTERVAL = int(os.environ.get("SWITCH_WATCH_INTERVAL", "30"))
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090").rstrip("/")
ISP_ALERT_ENABLED = os.environ.get("ISP_ALERT_ENABLED", "true").lower() in ("1", "true", "yes", "on")
ISP_ALERT_FOR_SECONDS = int(os.environ.get("ISP_ALERT_FOR_SECONDS", "10"))
ISP_ALERT_POLL_INTERVAL = int(os.environ.get("ISP_ALERT_POLL_INTERVAL", "5"))
ISP_ALERT_RATE_WINDOW = os.environ.get("ISP_ALERT_RATE_WINDOW", "1m")
ISP_ALERT_RESOLVE_SECONDS = int(os.environ.get("ISP_ALERT_RESOLVE_SECONDS", "30"))
ISP_ALERT_STATUS_INTERVAL = int(os.environ.get("ISP_ALERT_STATUS_INTERVAL", "30"))
FIREWALL_WAN_IF_FILTER = os.environ.get("FIREWALL_WAN_IF_FILTER", "telecom,telcom,unicom,isp,WAN")
BIGSCREEN_ISP_MAX_BANDWIDTH = os.environ.get("BIGSCREEN_ISP_MAX_BANDWIDTH", "1000")
ISP_SATURATION_PERCENT = float(os.environ.get("ISP_SATURATION_PERCENT", "80") or "80")


def _clean_token(raw):
    t = raw.strip()
    if "/hook/" in t:
        t = t.rsplit("/hook/", 1)[-1]
    return t.strip().strip("/")


TOKEN = _clean_token(os.environ.get("FEISHU_ROBOT_TOKEN", ""))

SEVERITY_COLOR = {
    "critical": "red",
    "high": "orange",
    "warning": "yellow",
    "info": "blue",
    "average": "orange",
    "disaster": "purple",
}


def log(message):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {message}", file=sys.stderr, flush=True)


def _librenms_token():
    if LIBRENMS_API_TOKEN:
        return LIBRENMS_API_TOKEN
    try:
        with open(LIBRENMS_TOKEN_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def fetch_librenms_devices(token):
    req = request.Request(
        f"{LIBRENMS_URL}/api/v0/devices",
        headers={"X-Auth-Token": token},
    )
    with request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8")).get("devices", [])


def build_librenms_card(payload):
    state = str(payload.get("state", "1"))
    rule_name = payload.get("name") or payload.get("rule") or "告警"
    severity = (payload.get("severity") or "warning").lower()

    hostname = payload.get("hostname") or payload.get("sysName") or ""
    ip = payload.get("ip") or ""
    if not hostname and not ip:
        devices = payload.get("devices") or []
        if devices:
            first = devices[0]
            hostname = first.get("hostname") or first.get("sysName") or ""
            ip = first.get("ip") or ""

    uid = str(payload.get("uid") or "").strip()
    elapsed = str(payload.get("elapsed") or "").strip()
    location = str(payload.get("location") or "").strip()
    ts = payload.get("timestamp") or ""

    recovered = state == "0"
    if recovered:
        color = "green"
        emoji = "✅"
        state_text = "UP"
    else:
        color = SEVERITY_COLOR.get(severity, "yellow")
        emoji = "❌" if severity in ("critical", "disaster") else "🔴"
        state_text = "DOWN"

    title = f"#{uid}" if uid and uid != "0" else rule_name

    dev_str = hostname or ip or "?"
    ip_str = f" ({ip})" if ip else ""
    lines = [f"{emoji} {dev_str}{ip_str} {state_text}"]

    if recovered:
        if elapsed and elapsed not in ("0s",):
            lines.append(f"离线时长：{elapsed}")
    else:
        if ts:
            lines.append(ts)

    if elapsed and elapsed not in ("0s",):
        lines.append(f"告警耗时：{elapsed}")

    return _make_card(title, rule_name, color, "\n".join(lines))


def build_device_online_card(device):
    name = device.get("display") or device.get("sysName") or device.get("hostname") or "?"
    ip = device.get("ip") or device.get("hostname") or "?"
    hw = device.get("hardware") or ""
    os_name = device.get("os") or ""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    title = "🟢 新设备上线"
    lines = [f"🖥 设备：{name}", f"🌐 IP：{ip}"]
    if hw:
        lines.append(f"🔧 型号：{hw}")
    if os_name:
        lines.append(f"💻 系统：{os_name}")
    lines.append(f"⏰ 时间：{ts}")

    return _make_card(title, "LibreNMS 设备发现", "green", "\n".join(lines))


def format_bps(value):
    value = float(value or 0)
    units = ["bps", "Kbps", "Mbps", "Gbps", "Tbps"]
    idx = 0
    while abs(value) >= 1000 and idx < len(units) - 1:
        value /= 1000.0
        idx += 1
    decimals = 1 if idx >= 2 else 0
    return f"{value:.{decimals}f} {units[idx]}"


def format_duration(seconds):
    seconds = max(0, int(seconds or 0))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {sec} 秒" if sec else f"{minutes} 分"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分" if minutes else f"{hours} 小时"


def build_isp_bandwidth_card(event, recovered=False):
    title = "🟢 ISP 带宽恢复" if recovered else "🔴 ISP 带宽饱和"
    color = "green" if recovered else "red"
    direction_text = "下载" if event["direction"] == "in" else "上传"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"🌐 ISP：{event['label']}",
        f"📶 方向：{direction_text}",
        f"📈 当前：{format_bps(event['value_bps'])}",
        f"⏰ 时间：{ts}",
        f"⏳ 持续：{format_duration(event['duration'])}",
    ]
    return _make_card(title, "实时 ISP 带宽监控", color, "\n".join(lines))


def _make_card(title, subtitle, color, body_md):
    return {
        "msg_type": "interactive",
        "card": {
            "schema": "2.0",
            "config": {
                "style": {
                    "text_size": {
                        "normal_v2": {
                            "default": "normal",
                            "pc": "normal",
                            "mobile": "heading",
                        }
                    }
                }
            },
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "subtitle": {"tag": "plain_text", "content": subtitle},
                "template": color,
                "padding": "12px 12px 12px 12px",
            },
            "body": {
                "direction": "vertical",
                "padding": "12px 12px 12px 12px",
                "elements": [
                    {
                        "tag": "markdown",
                        "content": body_md,
                        "text_align": "left",
                        "text_size": "normal_v2",
                        "margin": "0px 0px 0px 0px",
                    }
                ],
            },
        },
    }


def send_feishu(card):
    if DRY_RUN:
        log(f"[DRY] would POST card: {card['card']['header']['title']['content']}")
        return True
    if not TOKEN:
        log("[WARN] FEISHU_ROBOT_TOKEN empty, dropping alert (set token or enable DRY_RUN)")
        return False
    url = f"https://open.feishu.cn/open-apis/bot/v2/hook/{TOKEN}"
    data = json.dumps(card).encode("utf-8")
    req = request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with request.urlopen(req, timeout=5) as resp:
            response_text = resp.read().decode("utf-8", errors="replace")
            log(f"feishu response: {response_text[:200]}")
        return True
    except error.URLError as exc:
        log(f"[ERR] feishu request failed: {exc}")
        return False
    except Exception as exc:
        log(f"[ERR] unexpected: {exc}")
        return False


def _norm_label(value):
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _parse_bandwidth_config(raw):
    raw = str(raw or "").strip()
    cfg = {"default": None, "per": []}
    if not raw:
        return cfg
    try:
        mbps = float(raw)
        cfg["default"] = {"down": mbps, "up": mbps}
        return cfg
    except ValueError:
        pass

    for item in raw.split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        label, bandwidth = [part.strip() for part in item.split(":", 1)]
        parts = [part.strip() for part in bandwidth.split("/", 1)]
        try:
            down = float(parts[0])
        except (TypeError, ValueError):
            continue
        try:
            up = float(parts[1]) if len(parts) > 1 else down
        except (TypeError, ValueError):
            up = down
        cfg["per"].append({
            "label": label.lower(),
            "norm": _norm_label(label),
            "down": down,
            "up": up,
        })
    return cfg


def _wan_keywords():
    return [part.strip().lower() for part in FIREWALL_WAN_IF_FILTER.split(",") if part.strip()]


def _wan_label(metric):
    return (metric.get("ifAlias") or metric.get("ifName") or metric.get("ifDescr") or "").strip()


def _is_wan_port(label):
    lower = label.lower()
    return any(keyword in lower for keyword in _wan_keywords())


def _bandwidth_for_label(label, direction, cfg):
    lower = label.lower()
    norm = _norm_label(label)
    for entry in cfg["per"]:
        if (entry["label"] and entry["label"] in lower) or (entry["norm"] and entry["norm"] in norm):
            return entry["down"] if direction == "in" else entry["up"]
    default = cfg["default"] or {"down": 1000.0, "up": 1000.0}
    return default["down"] if direction == "in" else default["up"]


def prometheus_query(query):
    url = f"{PROMETHEUS_URL}/api/v1/query?{parse.urlencode({'query': query})}"
    with request.urlopen(url, timeout=10) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if payload.get("status") != "success":
        raise RuntimeError(payload.get("error") or "Prometheus query failed")
    return payload.get("data", {}).get("result", [])


def fetch_wan_rates():
    results = []
    for direction, metric in (("in", "ifHCInOctets"), ("out", "ifHCOutOctets")):
        query = f'rate({metric}{{job="firewall-snmp"}}[{ISP_ALERT_RATE_WINDOW}]) * 8'
        for item in prometheus_query(query):
            metric_labels = item.get("metric") or {}
            label = _wan_label(metric_labels)
            if not label or not _is_wan_port(label):
                continue
            try:
                value_bps = float((item.get("value") or [None, "nan"])[1])
            except (TypeError, ValueError):
                continue
            if value_bps < 0:
                continue
            results.append({
                "key": f"{label}|{direction}",
                "label": label,
                "direction": direction,
                "value_bps": value_bps,
                "target_ip": metric_labels.get("target_ip") or metric_labels.get("instance") or "",
            })
    return results


def log_isp_status(rates, bandwidth_cfg):
    if not rates:
        log(
            "[ISP] no WAN traffic series matched "
            f"FIREWALL_WAN_IF_FILTER={FIREWALL_WAN_IF_FILTER!r}; "
            "check Prometheus job=firewall-snmp labels ifAlias/ifName/ifDescr"
        )
        return

    rows = []
    for sample in sorted(rates, key=lambda item: item["value_bps"], reverse=True)[:6]:
        capacity_mbps = _bandwidth_for_label(sample["label"], sample["direction"], bandwidth_cfg)
        threshold_bps = capacity_mbps * 1000000 * (ISP_SATURATION_PERCENT / 100.0)
        rows.append(
            f"{sample['label']} {sample['direction']}="
            f"{format_bps(sample['value_bps'])}/{format_bps(threshold_bps)}"
        )
    log("[ISP] rates " + "; ".join(rows))


def isp_bandwidth_watcher():
    if not ISP_ALERT_ENABLED:
        log("[ISP] realtime bandwidth watcher disabled")
        return
    time.sleep(30)
    bandwidth_cfg = _parse_bandwidth_config(BIGSCREEN_ISP_MAX_BANDWIDTH)
    states = {}
    last_status_log = 0.0
    log(
        "[ISP] realtime bandwidth watcher enabled "
        f"(threshold={ISP_SATURATION_PERCENT:g}%, for={ISP_ALERT_FOR_SECONDS}s, "
        f"poll={ISP_ALERT_POLL_INTERVAL}s, rate_window={ISP_ALERT_RATE_WINDOW}, prometheus={PROMETHEUS_URL})"
    )

    while True:
        now = time.time()
        try:
            rates = fetch_wan_rates()
        except Exception as exc:
            log(f"[ISP] poll failed: {exc}")
            time.sleep(ISP_ALERT_POLL_INTERVAL)
            continue

        if now - last_status_log >= ISP_ALERT_STATUS_INTERVAL:
            log_isp_status(rates, bandwidth_cfg)
            last_status_log = now

        seen = set()
        for sample in rates:
            seen.add(sample["key"])
            capacity_mbps = _bandwidth_for_label(sample["label"], sample["direction"], bandwidth_cfg)
            threshold_bps = capacity_mbps * 1000000 * (ISP_SATURATION_PERCENT / 100.0)
            state = states.setdefault(sample["key"], {
                "active_since": None,
                "clear_since": None,
                "alerting": False,
                "last_value": 0.0,
            })
            state["last_value"] = sample["value_bps"]

            if sample["value_bps"] >= threshold_bps:
                if state["active_since"] is None:
                    state["active_since"] = now
                state["clear_since"] = None
                duration = now - state["active_since"]
                if not state["alerting"] and duration >= ISP_ALERT_FOR_SECONDS:
                    state["alerting"] = True
                    event = {
                        **sample,
                        "threshold_bps": threshold_bps,
                        "capacity_mbps": capacity_mbps,
                        "percent": ISP_SATURATION_PERCENT,
                        "duration": duration,
                    }
                    log(
                        f"[ISP] ALERT {sample['label']} {sample['direction']} "
                        f"{format_bps(sample['value_bps'])} >= {format_bps(threshold_bps)}"
                    )
                    send_feishu(build_isp_bandwidth_card(event, recovered=False))
            else:
                state["active_since"] = None
                if state["alerting"]:
                    if state["clear_since"] is None:
                        state["clear_since"] = now
                    clear_duration = now - state["clear_since"]
                    if clear_duration >= ISP_ALERT_RESOLVE_SECONDS:
                        state["alerting"] = False
                        event = {
                            **sample,
                            "threshold_bps": threshold_bps,
                            "capacity_mbps": capacity_mbps,
                            "percent": ISP_SATURATION_PERCENT,
                            "duration": clear_duration,
                        }
                        log(
                            f"[ISP] RECOVER {sample['label']} {sample['direction']} "
                            f"{format_bps(sample['value_bps'])} < {format_bps(threshold_bps)}"
                        )
                        send_feishu(build_isp_bandwidth_card(event, recovered=True))
                else:
                    state["clear_since"] = None

        for key, state in list(states.items()):
            if key in seen:
                continue
            if state.get("alerting") and state.get("clear_since") is None:
                state["clear_since"] = now
            elif state.get("clear_since") and now - state["clear_since"] >= ISP_ALERT_RESOLVE_SECONDS:
                states.pop(key, None)

        time.sleep(ISP_ALERT_POLL_INTERVAL)


def device_watcher():
    log(f"[WATCHER] starting, interval={SWITCH_WATCH_INTERVAL}s, url={LIBRENMS_URL}")
    time.sleep(60)  # 等 LibreNMS 就绪后再开始

    token = _librenms_token()
    if not token:
        log("[WATCHER] no API token available, retrying in 60s...")
        time.sleep(60)
        token = _librenms_token()
        if not token:
            log("[WATCHER] still no token, watcher disabled")
            return

    try:
        initial = fetch_librenms_devices(token)
        seen = {d.get("hostname") or d.get("ip") for d in initial if d.get("hostname") or d.get("ip")}
        log(f"[WATCHER] initialized with {len(seen)} existing devices")
    except Exception as exc:
        log(f"[WATCHER] init failed: {exc}")
        seen = set()

    while True:
        time.sleep(SWITCH_WATCH_INTERVAL)
        try:
            token = _librenms_token()
            if not token:
                log("[WATCHER] token lost, skipping poll")
                continue
            devices = fetch_librenms_devices(token)
        except Exception as exc:
            log(f"[WATCHER] poll failed: {exc}")
            continue

        for dev in devices:
            key = dev.get("hostname") or dev.get("ip")
            if key and key not in seen:
                log(f"[WATCHER] new device detected: {key}")
                send_feishu(build_device_online_card(dev))
                seen.add(key)


class Handler(BaseHTTPRequestHandler):
    server_version = "feishu-bridge/1.0"

    def _send(self, status, body=b"OK", content_type="text/plain; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b"{}"
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except (ValueError, json.JSONDecodeError):
            pass

        form = parse.parse_qs(text, keep_blank_values=True) if ("=" in text or "&" in text) else {}
        if form:
            payload = {key: values[-1] if values else "" for key, values in form.items()}
            body = payload.get("body")
            if body:
                try:
                    nested = json.loads(body)
                    if isinstance(nested, dict):
                        payload.update(nested)
                except (ValueError, json.JSONDecodeError):
                    pass
            return payload

        return {"name": "LibreNMS transport test", "raw": text}

    def do_POST(self):
        if self.path == "/librenms":
            return self._handle_librenms()
        return self._send(404, b"not found")

    def _handle_librenms(self):
        payload = self._read_json()
        card = build_librenms_card(payload)
        rule_name = payload.get("name") or payload.get("rule") or "LibreNMS 告警"
        log(f"librenms alert: {rule_name} state={payload.get('state')}")
        send_feishu(card)
        return self._send(200, b"OK")

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, b"OK")
        return self._send(404, b"not found")

    def log_message(self, fmt, *args):
        pass


def main():
    log(f"listening on 0.0.0.0:{PORT}  dry_run={DRY_RUN}  token_set={bool(TOKEN)}")
    if not TOKEN and not DRY_RUN:
        log("[WARN] no FEISHU_ROBOT_TOKEN set; LibreNMS alerts will not be forwarded")

    if LIBRENMS_URL:
        log(f"[WATCHER] device watcher enabled (librenms_url={LIBRENMS_URL})")
        threading.Thread(target=device_watcher, daemon=True).start()
    else:
        log("[WATCHER] LIBRENMS_URL not set, device watcher disabled")

    if PROMETHEUS_URL:
        threading.Thread(target=isp_bandwidth_watcher, daemon=True).start()

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
