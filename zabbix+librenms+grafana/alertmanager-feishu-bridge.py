#!/usr/bin/env python3
"""
LibreNMS webhook -> Feishu bot bridge.

Stdlib only (http.server + urllib + json) so the container runs on
python:3-slim with no requirements.txt.

Env:
  FEISHU_BRIDGE_PORT    listen port (default 5005)
  FEISHU_ROBOT_TOKEN    Feishu bot webhook token
  FEISHU_BRIDGE_DRY_RUN true = log payloads, never POST to Feishu
"""
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import sys
from urllib import error, request

PORT = int(os.environ.get("FEISHU_BRIDGE_PORT", "5005"))
DRY_RUN = os.environ.get("FEISHU_BRIDGE_DRY_RUN", "").lower() in ("1", "true", "yes", "on")


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


def build_librenms_card(payload):
    state = payload.get("state", 1)
    rule_name = payload.get("name") or payload.get("rule") or "告警"
    severity = (payload.get("severity") or "warning").lower()
    devices = payload.get("devices") or []
    faults = payload.get("faults") or {}

    if state == 0:
        color = "green"
        emoji = "✅"
        status_text = "已恢复"
    else:
        color = SEVERITY_COLOR.get(severity, "yellow")
        emoji = "🔴" if severity in ("critical", "disaster") else "🟡"
        status_text = "触发"

    dev_count = len(devices)
    first = devices[0] if devices else {}
    hostname = first.get("hostname") or first.get("sysName") or ""
    ip = first.get("ip") or ""
    suffix = f" 等{dev_count}台" if dev_count > 1 else ""
    dev_display = f"{hostname}（{ip}）" if hostname and ip and hostname != ip else hostname or ip or "?"
    title = f"{emoji} {rule_name} · {dev_display}{suffix}"[:148]

    lines = []
    for dev in devices:
        h = dev.get("hostname") or dev.get("sysName") or ""
        i = dev.get("ip") or ""
        d = f"{h}（{i}）" if h and i and h != i else h or i or "?"
        lines.append(f"🖥 设备：{d}")
        if state != 0 and faults:
            dev_id = str(dev.get("device_id") or "")
            for fault in (faults.get(dev_id) or []):
                msg = fault.get("string") or fault.get("value") or ""
                if msg:
                    lines.append(f"  📋 {msg}")
    if not lines:
        lines = [f"🖥 {status_text}"]

    ts = payload.get("timestamp") or ""
    if ts:
        lines.append(f"⏰ 时间：{ts}")

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
                "subtitle": {"tag": "plain_text", "content": "LibreNMS 告警"},
                "template": color,
                "padding": "12px 12px 12px 12px",
            },
            "body": {
                "direction": "vertical",
                "padding": "12px 12px 12px 12px",
                "elements": [
                    {
                        "tag": "markdown",
                        "content": "\n".join(lines),
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
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
