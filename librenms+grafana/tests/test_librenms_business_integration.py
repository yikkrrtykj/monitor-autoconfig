"""Real HTTP -> LibreNMSClient -> collector adapter integration."""
import importlib.util
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import sys
import threading
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "librenms"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from librenms_client import LibreNMSClient


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader
    spec.loader.exec_module(module)
    return module


gte = load_module("http_generate_topology_edges", "generate-topology-edges.py")
isp = load_module("http_discover_isp_targets", "discover-isp-targets.py")


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_fake_librenms_http_server_feeds_topology_and_isp_business_adapters(monkeypatch):
    topology_rows = fixture("devices.json")["devices"]
    for row, address in zip(topology_rows, ("192.0.2.1", "192.0.2.45")):
        row["hostname"] = address
        row["ip"] = address
    devices = topology_rows + fixture("firewall-device.json")["devices"]
    payloads = {
        "/api/v0/devices": {"status": "ok", "devices": devices},
        "/api/v0/devices/1/ports": fixture("core-ports.json"),
        "/api/v0/devices/1/links": fixture("core-links.json"),
        "/api/v0/devices/1/port_stack": fixture("core-port-stack.json"),
        "/api/v0/devices/2/ports": fixture("access-ports.json"),
        "/api/v0/devices/2/links": fixture("access-links.json"),
        "/api/v0/devices/2/port_stack": fixture("access-port-stack.json"),
        "/api/v0/devices/7001/ports": fixture("firewall-ports.json"),
        "/api/v0/devices/7001/ip": fixture("firewall-ip.json"),
    }
    observed = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlsplit(self.path)
            observed.append({
                "path": parsed.path,
                "query": parsed.query,
                "token": self.headers.get("X-Auth-Token"),
            })
            payload = payloads.get(parsed.path)
            if payload is None:
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = LibreNMSClient(
        base_url=f"http://127.0.0.1:{server.server_port}",
        token="fixture-token",
        timeout=2,
    )
    monkeypatch.setattr(
        gte, "poll_snmp_neighbors",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("complete fixture must not use adjacency SNMP")
        ),
    )
    monkeypatch.setattr(
        gte, "poll_snmp_lag",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("complete fixture must not use LAG SNMP")
        ),
    )
    try:
        topology_devices = {
            address: gte.poll_device_librenms(
                address, "unused-community", client, collect_arp=False,
                mode="librenms",
            )
            for address in ("192.0.2.1", "192.0.2.45")
        }
        edges, placeholders = gte.build_edges(
            topology_devices, gte.build_name_index(topology_devices),
        )
        firewall, addresses, ports = isp.fetch_librenms_inventory(
            client, "192.0.2.10",
        )
        inventory = isp.librenms_inventory_walks(addresses, ports)
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert placeholders == []
    assert len(edges) == 1
    assert {edges[0]["from_ip"], edges[0]["to_ip"]} == {
        "192.0.2.1", "192.0.2.45",
    }
    assert firewall["device_id"] == 7001
    assert inventory[isp.OID_IF_NAME][f"{isp.OID_IF_NAME}.11"] == "ethernet0/0"
    assert all(item["token"] == "fixture-token" for item in observed)
    assert any(
        item["path"] == "/api/v0/devices/1/ports" and "columns=" in item["query"]
        for item in observed
    )
    assert any(
        item["path"] == "/api/v0/devices/1/port_stack"
        and "valid_mappings=1" in item["query"]
        for item in observed
    )
