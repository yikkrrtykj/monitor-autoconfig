#!/usr/bin/env python3
"""
Alertmanager v2 webhook -> Feishu bot bridge.

Stdlib only (http.server + urllib + json) so the container runs on
python:3-slim with no requirements.txt.

Env:
  FEISHU_BRIDGE_PORT       listen port (default 5005)
  FEISHU_ROBOT_TOKEN       Feishu bot webhook token (part after /hook/)
  FEISHU_BRIDGE_DRY_RUN    true = log payloads, never POST to Feishu
"""
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import sys
from urllib import error, request

PORT = int(os.environ.get("FEISHU_BRIDGE_PORT", "5005"))
TOKEN = os.environ.get("FEISHU_ROBOT_TOKEN", "").strip()
# Accept either the bare token or a full webhook URL pasted in (a common mistake) —
# keep only the token segment after "/hook/" so both forms work.
if "/hook/" in TOKEN:
    TOKEN = TOKEN.rsplit("/hook/", 1)[-1]
TOKEN = TOKEN.strip().strip("/")
DRY_RUN = os.environ.get("FEISHU_BRIDGE_DRY_RUN", "").lower() in ("1", "true", "yes", "on")

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


def parse_dt(raw):
    if not raw:
        return None
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        return datetime.fromisoformat(text).astimezone()
    except ValueError:
        return None


def fmt_dt(dt):
    return dt.strftime("%m-%d %H:%M:%S") if dt else "?"


def humanize(seconds):
    s = int(max(0, seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h{m}m"
    if m:
        return f"{m}m"
    return f"{sec}s"


def cn_name(summary):
    # The rule summary is "<中文名>: <设备>"; take the part before the colon.
    for sep in ("：", ":"):
        if sep in summary:
            return summary.split(sep, 1)[0].strip()
    return summary.strip() or "告警"


def alert_fields(alert):
    labels = alert.get("labels", {}) or {}
    ann = alert.get("annotations", {}) or {}
    status = alert.get("status", "firing")
    name = labels.get("instance") or labels.get("host") or labels.get("isp") or ""
    ip = labels.get("target_ip") or labels.get("ip") or ""
    if name and ip and name != ip:
        device = f"{name}（{ip}）"
    else:
        device = name or ip or "?"
    start = parse_dt(alert.get("startsAt"))
    end = parse_dt(alert.get("endsAt"))
    if status == "resolved":
        dur = humanize((end - start).total_seconds()) if (start and end) else "?"
    elif start:
        dur = f"持续中（已 {humanize((datetime.now().astimezone() - start).total_seconds())}）"
    else:
        dur = "持续中"
    return {
        "status": status,
        "device": device,
        "iface": labels.get("iface") or "",
        "desc": ann.get("description", "").strip(),
        "start": start,
        "end": end,
        "dur": dur,
    }


def format_full(alert):
    f = alert_fields(alert)
    lines = [f"🖥 设备：{f['device']}"]
    if f["iface"]:
        lines.append(f"🔌 接口：{f['iface']}")
    if f["desc"]:
        lines.append(f"📝 详情：{f['desc']}")
    if f["status"] == "resolved":
        lines.append(f"⏰ 开始：{fmt_dt(f['start'])} · 恢复：{fmt_dt(f['end'])}")
    else:
        lines.append(f"⏰ 开始：{fmt_dt(f['start'])}")
    lines.append(f"⏳ 持续：{f['dur']}")
    return "\n".join(lines)


def format_compact(alert):
    f = alert_fields(alert)
    parts = [f"🖥 {f['device']}"]
    if f["iface"]:
        parts.append(f"🔌{f['iface']}")
    if f["desc"]:
        parts.append(f["desc"])
    if f["status"] == "resolved":
        parts.append(f"⏰ {fmt_dt(f['start'])}→{fmt_dt(f['end'])}｜{f['dur']}")
    else:
        parts.append(f"⏰ {fmt_dt(f['start'])} 起｜{f['dur']}")
    return "｜".join(parts)


def derive_header(payload):
    alerts = payload.get("alerts", []) or []
    fires = [a for a in alerts if a.get("status") != "resolved"]
    resolved = [a for a in alerts if a.get("status") == "resolved"]
    sample = (fires or resolved or [{}])[0]
    labels = sample.get("labels", {}) or {}
    cn = cn_name((sample.get("annotations", {}) or {}).get("summary", ""))
    dev = labels.get("instance") or labels.get("host") or labels.get("isp") or ""
    count = len(fires) if fires else len(resolved)
    suffix = f" 等{count}台" if count > 1 else ""
    sep = " · " if dev else ""
    if not fires and resolved:
        return f"✅ {cn}（已恢复）{sep}{dev}{suffix}"[:148], "green"
    sev = (payload.get("commonLabels", {}) or {}).get("severity") or labels.get("severity") or "warning"
    emoji = "🔴" if sev in ("critical", "high", "disaster") else "🟡"
    return f"{emoji} {cn}{sep}{dev}{suffix}"[:148], SEVERITY_COLOR.get(sev, "yellow")


def build_card(payload):
    title, color = derive_header(payload)
    alerts = payload.get("alerts", []) or []
    if len(alerts) <= 1:
        body_md = format_full(alerts[0]) if alerts else "(空告警包)"
    else:
        body_md = "\n".join(format_compact(a) for a in alerts)
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
        title, _ = derive_header(payload)
        log(f"received group: {len(alerts)} alert(s) · {title}")

        card = build_card(payload)
        ok = send_feishu(card)
        return self._send(200 if ok else 502, b"OK" if ok else b"feishu send failed")

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, b"OK")
        return self._send(404, b"not found")

    def log_message(self, fmt, *args):
        # override the default access log; do_POST logs explicitly
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
