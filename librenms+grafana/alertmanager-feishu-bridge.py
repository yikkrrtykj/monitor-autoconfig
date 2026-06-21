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
from urllib import error, parse, request

PORT = int(os.environ.get("FEISHU_BRIDGE_PORT", "5005"))
DRY_RUN = os.environ.get("FEISHU_BRIDGE_DRY_RUN", "").lower() in ("1", "true", "yes", "on")

LIBRENMS_URL = os.environ.get("LIBRENMS_URL", "").rstrip("/")
LIBRENMS_API_TOKEN = os.environ.get("LIBRENMS_API_TOKEN", "")
LIBRENMS_TOKEN_FILE = os.environ.get("LIBRENMS_TOKEN_FILE", "/librenms-data/librenms-api-token")
SWITCH_WATCH_INTERVAL = int(os.environ.get("SWITCH_WATCH_INTERVAL", "30"))


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

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
