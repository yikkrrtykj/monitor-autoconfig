"""Tiny HTTP server: POST /rescan triggers an immediate player-targets re-scan."""
import http.server
import json
import os
import time

TRIGGER = "/targets/rescan.trigger"
TARGETS_FILE = os.environ.get("PLAYER_TARGETS_FILE", "/targets/player_targets.json")


def _target_status():
    status = {
        "targets_file": TARGETS_FILE,
        "targets": {"total": 0, "wired": 0, "wireless": 0, "other": 0},
        "updated_at": None,
        "trigger_pending": os.path.exists(TRIGGER),
    }
    try:
        stat = os.stat(TARGETS_FILE)
        status["updated_at"] = int(stat.st_mtime)
        with open(TARGETS_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        for item in data if isinstance(data, list) else []:
            labels = item.get("labels") or {}
            network = str(labels.get("network") or "other").lower()
            if network not in ("wired", "wireless"):
                network = "other"
            status["targets"][network] += 1
            status["targets"]["total"] += 1
    except FileNotFoundError:
        status["error"] = "targets file not found"
    except Exception as exc:  # pragma: no cover - defensive runtime guard
        status["error"] = str(exc)
    return status


class RescanHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.rstrip("/") != "/status":
            self.send_response(404)
            self.end_headers()
            return
        payload = {
            "ok": True,
            "now": int(time.time()),
            **_target_status(),
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        open(TRIGGER, "w").close()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(b"OK")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST")
        self.end_headers()

    def log_message(self, fmt, *args):
        print(f"[rescan-api] {fmt % args}", flush=True)


if __name__ == "__main__":
    server = http.server.HTTPServer(("0.0.0.0", 9199), RescanHandler)
    print("[rescan-api] listening on :9199", flush=True)
    server.serve_forever()
