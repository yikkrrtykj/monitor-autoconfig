"""Tiny HTTP server: POST /rescan triggers an immediate player-targets re-scan."""
import http.server
import os

TRIGGER = "/targets/rescan.trigger"


class RescanHandler(http.server.BaseHTTPRequestHandler):
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
        self.send_header("Access-Control-Allow-Methods", "POST")
        self.end_headers()

    def log_message(self, fmt, *args):
        print(f"[rescan-api] {fmt % args}", flush=True)


if __name__ == "__main__":
    server = http.server.HTTPServer(("0.0.0.0", 9199), RescanHandler)
    print("[rescan-api] listening on :9199", flush=True)
    server.serve_forever()
