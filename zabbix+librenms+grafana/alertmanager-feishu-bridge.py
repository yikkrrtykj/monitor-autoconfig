#!/usr/bin/env python3
"""
Alertmanager → Feishu bridge.

Receives Prometheus Alertmanager v2 webhook payloads on POST /webhook,
formats each group as a Feishu interactive card, and forwards to the
Feishu bot endpoint configured via FEISHU_ROBOT_TOKEN.

Standard library only (http.server + urllib + json) so the container
can run with just python:3-slim, no requirements.txt.

Env:
  FEISHU_BRIDGE_PORT       listen port (default 5005)
  FEISHU_ROBOT_TOKEN       Feishu bot webhook token (the part after /hook/)
  FEISHU_BRIDGE_DRY_RUN    true = log payloads, never POST to Feishu
"""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import sys
from urllib import error, request

PORT = int(os.environ.get("FEISHU_BRIDGE_PORT", "5005"))
TOKEN = os.environ.get("FEISHU_ROBOT_TOKEN", "").strip()
DRY_RUN = os.environ.get("FEISHU_BRIDGE_DRY_RUN", "").lower() in ("1", "true", "yes", "on")

# Feishu interactive-card header colors per severity. Resolution always green.
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


def parse_iso_timestamp(raw):
    """Parse Alertmanager's ISO-8601 timestamps into a local-time string.

    Returns the original string if parsing fails so the message still
    renders rather than disappear.
    """
    if not raw:
        return ""
    text = raw
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
        return parsed.astimezone().strftime("%H:%M:%S")
    except ValueError:
        return raw


def format_alert(alert):
    status = alert.get("status", "firing")
    labels = alert.get("labels", {}) or {}
    annotations = alert.get("annotations", {}) or {}
    alertname = labels.get("alertname", "?")
    severity = labels.get("severity", "warning")
    instance = labels.get("instance", "")
    target = labels.get("target_ip", "")
    summary = annotations.get("summary", "")
    description = annotations.get("description", "")
    starts_at = parse_iso_timestamp(alert.get("startsAt"))
    ends_at = parse_iso_timestamp(alert.get("endsAt"))

    icon = "✅" if status == "resolved" else ("🔴" if severity == "critical" else "⚠️")
    lines = [f"{icon} **{alertname}** · `{severity}`"]
    if summary:
        lines.append(f"摘要：{summary}")
    if description:
        lines.append(f"详情：{description}")
    if instance or target:
        target_repr = " / ".join(filter(None, [instance, target]))
        lines.append(f"实例：`{target_repr}`")
    if status == "resolved":
        lines.append(f"⏱ 起 {starts_at} · 止 {ends_at}（已恢复）")
    else:
        lines.append(f"⏱ 起 {starts_at}（持续中）")
    return "\n".join(lines)


def derive_title_color(payload):
    alerts = payload.get("alerts", []) or []
    fires = [a for a in alerts if a.get("status") == "firing"]
    resolves = [a for a in alerts if a.get("status") == "resolved"]
    common_labels = payload.get("commonLabels", {}) or {}
    severity = (common_labels.get("severity") or "warning").lower()
    alertname = common_labels.get("alertname", "Alerts")

    if not fires and resolves:
        color = "green"
        prefix = f"[RESOLVED · {len(resolves)}]"
    elif fires:
        color = SEVERITY_COLOR.get(severity, "yellow")
        prefix = f"[{severity.upper()} · {len(fires)}{f' +{len(resolves)}已恢复' if resolves else ''}]"
    else:
        color = "grey"
        prefix = "[INFO]"

    title = f"{prefix} {alertname}"
    return title, color


def build_card(payload):
    title, color = derive_title_color(payload)
    sections = [format_alert(alert) for alert in (payload.get("alerts", []) or [])]
    body_md = "\n\n---\n\n".join(sections) or "(空告警包)"
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
                "title": {"tag": "plain_text", "content": title[:148]},
                "subtitle": {"tag": "plain_text", "content": "Prometheus 告警"},
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


class Handler(BaseHTTPRequestHandler):
    server_version = "feishu-bridge/1.0"

    def _send(self, status, body=b"OK", content_type="text/plain; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/webhook":
            return self._send(404, b"not found")
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, json.JSONDecodeError) as exc:
            log(f"[ERR] invalid json: {exc}")
            return self._send(400, b"invalid json")

        alerts = payload.get("alerts", []) or []
        title, _ = derive_title_color(payload)
        log(f"received group: {len(alerts)} alert(s) · {title}")

        card = build_card(payload)
        ok = send_feishu(card)
        return self._send(200 if ok else 502, b"OK" if ok else b"feishu send failed")

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, b"OK")
        return self._send(404, b"not found")

    def log_message(self, fmt, *args):
        # silence the noisy default access log; we log explicitly in do_POST
        pass


def main():
    log(f"listening on 0.0.0.0:{PORT}  dry_run={DRY_RUN}  token_set={bool(TOKEN)}")
    if not TOKEN and not DRY_RUN:
        log("[WARN] no FEISHU_ROBOT_TOKEN set; alerts will be received but not forwarded")
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
