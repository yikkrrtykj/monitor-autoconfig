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
"""
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import sys
import threading
import time
from urllib import error, request

PORT = int(os.environ.get("FEISHU_BRIDGE_PORT", "5005"))
DRY_RUN = os.environ.get("FEISHU_BRIDGE_DRY_RUN", "").lower() in ("1", "true", "yes", "on")

LIBRENMS_URL = os.environ.get("LIBRENMS_URL", "").rstrip("/")
LIBRENMS_API_TOKEN = os.environ.get("LIBRENMS_API_TOKEN", "")
LIBRENMS_TOKEN_FILE = os.environ.get("LIBRENMS_TOKEN_FILE", "/librenms-data/librenms-api-token")
SWITCH_WATCH_INTERVAL = int(os.environ.get("SWITCH_WATCH_INTERVAL", "120"))


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
    # LibreNMS 的 API transport 走 SimpleTemplate（只支持扁平 {{ key }} 替换，
    # 不支持循环），所以 api-body 模板发来的是扁平标量字段。为兼容历史的
    # devices[] 嵌套格式，扁平字段缺失时回退取 devices[0]。
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

    recovered = state == "0"
    if recovered:
        color = "green"
        emoji = "✅"
        status_text = "已恢复"
    else:
        color = SEVERITY_COLOR.get(severity, "yellow")
        emoji = "🔴" if severity in ("critical", "disaster") else "🟡"
        status_text = "触发"

    dev_display = f"{hostname}（{ip}）" if hostname and ip and hostname != ip else hostname or ip or "?"
    title = f"{emoji} {rule_name} · {dev_display}"[:148]

    lines = [f"🖥 设备：{dev_display}", f"📊 状态：{status_text}"]

    ts = payload.get("timestamp") or ""
    if ts:
        lines.append(f"⏰ 时间：{ts}")

    return _make_card(title, "LibreNMS 告警", color, "\n".join(lines))


def build_device_online_card(device):
    name = device.get("display") or device.get("sysName") or device.get("hostname") or "?"
    ip = device.get("ip") or device.get("hostname") or "?"
    hw = device.get("hardware") or ""
    os_name = device.get("os") or ""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    title = f"🟢 新设备上线 · {name}"[:148]
    lines = [f"🖥 设备：{name}", f"🌐 IP：{ip}"]
    if hw:
        lines.append(f"🔧 型号：{hw}")
    if os_name:
        lines.append(f"💻 系统：{os_name}")
    lines.append(f"⏰ 时间：{ts}")

    return _make_card(title, "LibreNMS 设备发现", "green", "\n".join(lines))


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
        return json.loads(raw.decode("utf-8") or "{}")

    def do_POST(self):
        if self.path == "/librenms":
            return self._handle_librenms()
        return self._send(404, b"not found")

    def _handle_librenms(self):
        try:
            payload = self._read_json()
        except (ValueError, json.JSONDecodeError) as exc:
            log(f"[ERR] librenms invalid json: {exc}")
            return self._send(400, b"invalid json")

        card = build_librenms_card(payload)
        rule_name = payload.get("name") or payload.get("rule") or "LibreNMS 告警"
        log(f"librenms alert: {rule_name} state={payload.get('state')}")
        ok = send_feishu(card)
        return self._send(200 if ok else 502, b"OK" if ok else b"feishu send failed")

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

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
