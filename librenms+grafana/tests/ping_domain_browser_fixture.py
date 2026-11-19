"""Expose the isolated D2 services to the actual D3 browser for acceptance."""
from http.server import ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import subprocess
import threading
from urllib.parse import urlsplit
from uuid import uuid4

from platform_api import auth, custom_ping, main


def run_domain_browser(monkeypatch, root, local, inventory, resolver, dns_config, project, fetch_prom, prior, a, b):
    # This test runs only inside the disposable Docker/UDP fixture.
    row = next(row for row in local.list_targets()["targets"] if row["id"] == prior)
    local.change("DELETE", row["id"], row["revision"], {}, "browser-fixture", str(uuid4()), inventory.check)
    project()
    static = root / "bigscreen"
    context = auth.AuthContext(local.manifest.parent / "browser-auth.json", admin_user="d3-admin")
    context.auth_path.write_text(json.dumps({
        "username": "d3-admin", "passwordHash": auth.hash_password("only-isolated-d3-fixture"),
    }), encoding="utf-8")
    app = custom_ping.CustomPingAPI(local, context, True, inventory)

    class Gateway(main.Handler):
        def _send_fixture_bytes(self, body, content_type="application/json", status=200):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _platform(self, method):
            if not self.path.startswith("/platform-api/"):
                self.send_error(404)
                return
            self.path = self.path.removeprefix("/platform-api")
            method()

        def do_GET(self):
            path = urlsplit(self.path).path
            if path.startswith("/platform-api/"):
                self._platform(super().do_GET)
            elif path in ("/prometheus/api/v1/query", "/prometheus/api/v1/query_range"):
                self._send_fixture_bytes(fetch_prom(self.path.removeprefix("/prometheus")).encode())
            elif path == "/config.js":
                self._send_fixture_bytes(b"window.BIGSCREEN_CONFIG={pingQualityRttMs:1000};", "text/javascript")
            else:
                file = (static / ("index.html" if path == "/ping" else path.lstrip("/"))).resolve()
                if not file.is_relative_to(static.resolve()) or not file.is_file():
                    self.send_error(404)
                    return
                self._send_fixture_bytes(file.read_bytes(), mimetypes.guess_type(file)[0] or "application/octet-stream")

        def do_POST(self):
            if not self.path.startswith("/fixture/dns/"):
                return self._platform(super().do_POST)
            phase = self.path.removeprefix("/fixture/dns/")
            if phase == "discover":
                project()
            elif phase in ("multi", "b-pending", "a-pending", "b", "error-hold", "policy", "timeout"):
                dns_config["mode"] = {"error-hold": "nxdomain", "timeout": "timeout"}.get(phase, "a")
                dns_config["ips"] = {
                    "multi": [b, "172.31.248.23", a], "b": [b], "b-pending": [b],
                    "policy": ["127.0.0.1"],
                }.get(phase, [a])
                manifest, _ = local._read()
                item = next(row for row in manifest["targets"] if row.get("kind") == "dns" and row["enabled"])
                saved = local.dns.read(required=True)["rows"].get(item["id"])
                ticket = {**item, "_runtimeDigest": local._dns_digest(item, saved)}
                assert local.resolve_dns(ticket, resolver.resolve(item["address"]), inventory.dns_policy())
                if phase not in ("b-pending", "a-pending", "error-hold"):
                    project()
            else:
                self.send_error(404)
                return
            self._send_fixture_bytes(json.dumps(local.list_targets()).encode())

        def do_PATCH(self):
            self._platform(super().do_PATCH)

        def do_DELETE(self):
            self._platform(super().do_DELETE)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
    app.public_origin = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(main, "AUTH_CONTEXT", context)
    monkeypatch.setattr(main, "CUSTOM_PING", app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log = Path(os.environ["PING_BROWSER_OUTPUT"]) / "isolated-browser.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        env = {**os.environ, "PING_D3_LIVE": app.public_origin}
        with log.open("w", encoding="utf-8") as stream:
            result = subprocess.run(
                ["node", str(root / "tests" / "chromium_ping_domains.js")],
                cwd=root.parent, env=env, stdout=stream, stderr=subprocess.STDOUT, timeout=155,
            )
        assert result.returncode == 0, log.read_text(encoding="utf-8")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
