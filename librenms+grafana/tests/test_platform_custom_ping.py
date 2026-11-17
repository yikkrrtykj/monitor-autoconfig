import errno
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import stat
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from platform_api import auth, custom_ping, custom_ping_store as storage, main


ITEM = {"name": "远端制作", "ip": "192.0.2.11", "group": "Remote"}
ORIGIN = "http://console.test:8088"
AUTH_RECORD = {"username": "test-admin", "passwordHash": "synthetic-test-record"}


@pytest.fixture
def store(tmp_path):
    return storage.PingStore(tmp_path, tmp_path / "sd" / "custom_targets.json")


@pytest.fixture
def server_factory():
    servers = []

    def start(handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True, kwargs={"poll_interval": 0.01})
        thread.start()
        servers.append((server, thread))
        return server.server_port

    yield start
    for server, thread in reversed(servers):
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.fixture
def prom(server_factory, monkeypatch):
    class FakePrometheus(BaseHTTPRequestHandler):
        payload = {"status": "success", "data": {"activeTargets": [{"scrapePool": "prometheus"}]}}

        def do_GET(self):
            assert self.path == "/api/v1/targets?state=active"
            body = json.dumps(self.payload).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    if os.name == "nt":
        # Exercise the same HTTP parser without requiring a loopback socket in restricted Windows tools.
        def open_inventory(url, timeout):
            assert url.endswith("/api/v1/targets?state=active") and timeout == 1
            return io.BytesIO(json.dumps(FakePrometheus.payload).encode())

        monkeypatch.setattr(custom_ping, "urlopen", open_inventory)
        port = 9
    else:
        port = server_factory(FakePrometheus)
    inventory = custom_ping.TargetInventory(f"http://127.0.0.1:{port}", "192.0.2.0/24", "192.0.2.96/28", 1)
    return inventory, FakePrometheus.payload["data"]["activeTargets"]


@pytest.fixture
def api(store, prom, server_factory, monkeypatch, tmp_path):
    context = auth.AuthContext(tmp_path / "auth.json", admin_user=AUTH_RECORD["username"])
    context.auth_path.write_text(json.dumps(AUTH_RECORD), encoding="utf-8")
    app = custom_ping.CustomPingAPI(store, context, True, prom[0])
    monkeypatch.setattr(main, "CUSTOM_PING", app)
    store.reconcile()
    port = 0 if os.name == "nt" else server_factory(main.Handler)

    class MemorySocket:
        def __init__(self, wire):
            self.input = io.BytesIO(wire)
            self.output = bytearray()

        def makefile(self, mode, *args):
            assert mode == "rb"
            return self.input

        def sendall(self, data):
            self.output.extend(data)

        def settimeout(self, timeout):
            assert timeout > 0

    def request(method, path="", body=None, headers=None, raw=None):
        headers = dict(headers or {})
        if body is not None or raw is not None:
            headers.setdefault("Content-Type", "application/json")
        data = raw if raw is not None else json.dumps(body).encode() if body is not None else None
        path = "/ping/targets" + path
        if port == 0:
            headers.setdefault("Content-Length", str(len(data or b"")))
            wire_headers = [f"{method} {path} HTTP/1.0", *[f"{key}: {value}" for key, value in headers.items()], "", ""]
            transport = MemorySocket("\r\n".join(wire_headers).encode() + (data or b""))
            main.Handler(transport, ("127.0.0.1", 0), None)
            reply = http.client.HTTPResponse(MemorySocket(bytes(transport.output)))
            reply.begin()
            assert reply.headers["Cache-Control"] == "no-store"
            assert "Access-Control-Allow-Origin" not in reply.headers
            return reply.status, json.loads(reply.read()), reply.headers
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request(method, path, body=data, headers=headers)
            reply = connection.getresponse()
            assert reply.headers["Cache-Control"] == "no-store"
            assert "Access-Control-Allow-Origin" not in reply.headers
            return reply.status, json.loads(reply.read()), reply.headers
        finally:
            connection.close()

    session = auth.create_session(context, AUTH_RECORD["username"])
    cookies = {"Cookie": f"{context.cookie_name}={session}"}
    status, result, _ = request("GET", "/csrf", headers=cookies)
    assert status == 200
    headers = {
        **cookies, "Origin": ORIGIN, "X-Platform-Public-Origin": ORIGIN,
        "X-CSRF-Token": result["csrfToken"],
    }
    return app, request, headers, session


def edit_headers(headers, revision=None, **overrides):
    result = {**headers, "Idempotency-Key": str(uuid4()), **overrides}
    if revision is not None:
        result["If-Match"] = f'"{revision}"'
    return result


def add(store, ip=ITEM["ip"], **fields):
    return store.change("POST", None, None, {**ITEM, "ip": ip, **fields}, "test-admin", str(uuid4()), lambda _: None)


def test_http_lifecycle_replay_and_discovery(store, api, prom, caplog):
    app, request, headers, _ = api
    assert request("GET")[1] == {
        "ok": True, "targets": [], "revision": 0, "publishedRevision": 0, "publication": "published",
    }
    create = edit_headers(headers)
    status, original, reply = request("POST", body=ITEM, headers=create)
    assert status == 201 and reply["ETag"] == '"1"'
    item = original["target"]
    assert item["enabled"] and item["createdAt"] == item["updatedAt"]
    target = "/" + item["id"]
    assert request("POST", body=ITEM, headers=create)[1]["replayed"]
    assert request("POST", body={**ITEM, "name": "changed"}, headers=create)[0] == 409
    assert request("PATCH", target, {"name": "改名"}, edit_headers(headers))[0] == 428
    assert request("PATCH", target, {"name": "改名"}, edit_headers(headers, 9))[0] == 412
    status, changed, reply = request("PATCH", target, {"name": "改名", "group": "Other"}, edit_headers(headers, 1))
    assert status == 200 and reply["ETag"] == '"2"'
    assert changed["target"]["id"] == item["id"] and changed["target"]["createdAt"] == item["createdAt"]
    assert request("GET", "/discovery")[1]["discoveredRevision"] is None
    labels = json.loads(store.discovery.read_bytes())[0]["labels"]
    prom[1].append({
        "scrapePool": "custom-ping", "labels": {"custom_id": item["id"], "target_ip": item["ip"]},
        "discoveredLabels": labels, "health": "up",
    })
    observation = request("GET", "/discovery")[1]
    assert observation["discoveredRevision"] == 2
    assert observation["activeTargets"][0]["scrapeHealth"] == "up"
    disabled = request("PATCH", target, {"enabled": False}, edit_headers(headers, 2))
    assert disabled[0] == 200 and disabled[1]["target"]["enabled"] is False
    assert json.loads(store.discovery.read_bytes()) == []
    assert request("GET", "/discovery")[1]["discoveredRevision"] is None
    restored = request("PATCH", target, {"enabled": True, "ip": "192.0.2.12"}, edit_headers(headers, 3))
    assert restored[0] == 200 and restored[1]["target"]["id"] == item["id"]
    assert json.loads(store.discovery.read_bytes())[0]["targets"] == ["192.0.2.12"]
    assert request("GET", "/discovery")[1]["discoveredRevision"] is None
    delete = edit_headers(headers, 4)
    status, removed, _ = request("DELETE", target, headers=delete)
    assert status == 200 and removed["target"] is None and removed["savedRevision"] == 5
    assert request("DELETE", target, headers=delete)[1]["replayed"]
    assert request("GET")[1]["targets"] == []
    assert json.loads(store.discovery.read_bytes()) == []
    receipts = json.loads(store.manifest.read_bytes())["receipts"]
    assert [row["action"] for row in receipts] == ["POST", "PATCH", "PATCH", "PATCH", "DELETE"]
    assert receipts[-1]["before"]["ip"] == "192.0.2.12" and receipts[-1]["actor"] == AUTH_RECORD["username"]
    assert headers["Cookie"] not in caplog.text and headers["X-CSRF-Token"] not in caplog.text


@pytest.mark.parametrize("method", ["POST", "PATCH", "DELETE"])
@pytest.mark.parametrize("failure,expected", [
    ("auth_disabled", 503), ("anonymous", 401), ("expired", 401), ("write_disabled", 403),
    ("wrong_origin", 403), ("missing_origin", 403), ("missing_csrf", 403), ("other_session", 403),
    ("not_admin", 403), ("fetch_site", 403), ("damaged_auth", 503),
])
def test_mutations_fail_closed(store, api, method, failure, expected):
    app, request, headers, session = api
    headers = edit_headers(headers, 1 if method != "POST" else None)
    if failure == "auth_disabled":
        app.auth_context.enabled = False
    elif failure == "anonymous":
        headers.pop("Cookie")
    elif failure == "expired":
        app.auth_context.sessions[session]["expires"] = 0
    elif failure == "write_disabled":
        app.write_enabled = False
    elif failure == "wrong_origin":
        app.public_origin = ORIGIN
        headers.update({"Origin": "http://other.test", "X-Platform-Public-Origin": "http://other.test"})
    elif failure == "missing_origin":
        headers.pop("Origin")
    elif failure == "missing_csrf":
        headers.pop("X-CSRF-Token")
    elif failure == "other_session":
        other = auth.create_session(app.auth_context, AUTH_RECORD["username"])
        headers["Cookie"] = f"{app.auth_context.cookie_name}={other}"
    elif failure == "not_admin":
        app.auth_context.sessions[session]["username"] = "test-viewer"
    elif failure == "fetch_site":
        headers["Sec-Fetch-Site"] = "same-site"
    elif failure == "damaged_auth":
        app.auth_context.auth_path.write_bytes(b"bad auth")
    path = "" if method == "POST" else "/" + str(uuid4())
    data = ITEM if method == "POST" else {"enabled": False} if method == "PATCH" else None
    assert request(method, path, data, headers)[0] == expected
    assert not store.manifest.exists() and not store.backup.exists()
    assert request("GET")[1]["targets"] == []


@pytest.mark.parametrize("ip", [
    "localhost", "https://192.0.2.11/", "192.0.2.1:80", "192.0.2.0/24",
    "192.0.2.1,192.0.2.2", "::1", "192.0.2.01", "0.0.0.0", "127.0.0.2",
    "169.254.169.254", "224.1.0.1", "255.255.255.255", True,
])
def test_reject_non_unicast_or_non_literal(api, ip):
    _, request, headers, _ = api
    assert request("POST", body={**ITEM, "ip": ip}, headers=edit_headers(headers))[0] == 400


def test_schema_request_limits_and_address_policy(store, api):
    _, request, headers, _ = api
    for override in (
        {"enabled": False}, {"id": str(uuid4())}, {"group": "ISP"}, {"group": []},
        {"name": " "}, {"name": "\r\nalert"}, {"name": "🌐" * 80}, {"name": "\ud800"},
    ):
        assert request("POST", body={**ITEM, **override}, headers=edit_headers(headers))[0] == 400
    assert request("POST", body=ITEM, headers=headers)[0] == 400
    assert request("POST", raw=b"a" * 4097, headers=edit_headers(headers))[0] == 413
    assert request("POST", raw=b'{"ip": "192.0.2.11", "ip": "192.0.2.12"}', headers=edit_headers(headers))[0] == 400
    assert request("POST", raw=b"[" * 2048 + b"]" * 2048, headers=edit_headers(headers))[0] == 400
    assert request("POST", body=ITEM, headers=edit_headers(headers, **{"Content-Type": "text/plain"}))[0] == 415
    for ip in ("198.51.100.1", "192.0.2.255"):
        assert request("POST", body={**ITEM, "ip": ip}, headers=edit_headers(headers))[0] == 403
    reserved = request("POST", body={**ITEM, "ip": "192.0.2.99"}, headers=edit_headers(headers))
    assert reserved[0] == 409 and reserved[1]["source"] == "player-network"
    assert not store.manifest.exists()


@pytest.mark.parametrize("job", ["player-ping", "infra-ap-ping", "infra-isp-ping", "infra-fw-unit-ping", "infra-srv-ping", "firewall-snmp"])
def test_other_source_duplicate_and_missing_inventory(api, prom, job):
    _, request, headers, _ = api
    prom[1].append({"scrapePool": job, "discoveredLabels": {"__address__": ITEM["ip"]}})
    result = request("POST", body=ITEM, headers=edit_headers(headers))
    assert result[0] == 409 and result[1]["source"] == job
    prom[1].clear()
    assert request("POST", body=ITEM, headers=edit_headers(headers))[0] == 503


def test_duplicate_disabled_and_concurrent_clients(store):
    row = add(store)[1]["target"]
    checks = []
    with ThreadPoolExecutor(max_workers=2) as workers:
        def change(name):
            try:
                return store.change("PATCH", row["id"], row["revision"], {"name": name},
                                    "test-admin", str(uuid4()), checks.append)[0]
            except storage.PingError as error:
                return error.status

        assert sorted(workers.map(change, ("A", "B"))) == [200, 412]
    assert checks == []
    store.change("PATCH", row["id"], 2, {"enabled": False}, "test-admin", str(uuid4()), checks.append)
    with pytest.raises(storage.PingError, match="duplicate_ip") as duplicate:
        add(store, name="different")
    assert duplicate.value.status == 409
    add(store, ip="192.0.2.21", name=ITEM["name"])
    assert store.list_targets()["revision"] == 4


def test_enabled_and_total_capacity(store, monkeypatch):
    monkeypatch.setattr(storage, "MAX_ACTIVE", 2)
    monkeypatch.setattr(storage, "MAX_TARGETS", 3)
    first, second = (add(store, ip=f"192.0.2.{n}")[1]["target"] for n in (10, 11))
    with pytest.raises(storage.PingError, match="capacity"):
        add(store, ip="192.0.2.12")
    store.change("PATCH", first["id"], first["revision"], {"enabled": False},
                 "test-admin", str(uuid4()), lambda _: None)
    add(store, ip="192.0.2.12")
    with pytest.raises(storage.PingError, match="capacity"):
        store.change("PATCH", first["id"], 3, {"enabled": True}, "test-admin", str(uuid4()), lambda _: None)
    store.change("PATCH", second["id"], second["revision"], {"enabled": False},
                 "test-admin", str(uuid4()), lambda _: None)
    with pytest.raises(storage.PingError, match="capacity"):
        add(store, ip="192.0.2.13")


@pytest.mark.parametrize("failure", ["stage_fsync", "backup_replace", "manifest_replace"])
def test_disk_failure_before_commit_preserves_manifest_and_discovery(store, monkeypatch, failure):
    add(store)
    old = (store.manifest.read_bytes(), store.discovery.read_bytes())
    replace = os.replace

    def failed_replace(src, dest):
        if dest == getattr(store, failure.removesuffix("_replace"), None):
            raise OSError(errno.ENOSPC, "synthetic disk full")
        return replace(src, dest)

    def failed_fsync(_fd):
        raise OSError(errno.ENOSPC, "synthetic disk full")

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", failed_replace)
        if failure == "stage_fsync":
            patch.setattr(os, "fsync", failed_fsync)
        with pytest.raises(storage.PingError, match="commit_failed"):
            add(store, ip="192.0.2.17")
    assert (store.manifest.read_bytes(), store.discovery.read_bytes()) == old
    assert not list(store.manifest.parent.rglob(".custom-ping-*"))


def test_late_failure_replay_and_restart_recovers_from_manifest(store, monkeypatch):
    add(store)
    initial = store.manifest.read_bytes(), store.discovery.read_bytes()
    replace = os.replace

    def failed_publish(src, dest):
        if dest == store.discovery:
            raise OSError(errno.ENOSPC, "synthetic disk full during publish")
        return replace(src, dest)

    new_key = str(uuid4())
    args = ("POST", None, None, {**ITEM, "ip": "192.0.2.17"}, "test-admin", new_key, lambda _: None)
    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", failed_publish)
        status, result = store.change(*args)
        assert status == 202 and result["savedRevision"] == 2 and result["publishedRevision"] is None
        assert store.backup.read_bytes() == initial[0] and store.discovery.read_bytes() == initial[1]
        assert store.change(*args)[0] == 202
    restarted = storage.PingStore(store.manifest.parent, store.discovery)
    restarted.reconcile()
    status, replayed = restarted.change(*args)
    assert status == 201 and replayed["replayed"] and replayed["target"] == result["target"]
    assert replayed["publishedRevision"] == 2 and len(json.loads(store.discovery.read_bytes())) == 2


@pytest.mark.parametrize("damaged", [
    b'{"schema":1', b"[]", b"\xff", b" " * (storage.MAX_BYTES + 1),
    b'{"schema":1,"schema":1,"revision":0,"targets":[],"receipts":[]}', None,
], ids=["truncated", "wrong-root", "invalid-utf8", "oversized", "duplicate-key", "missing"])
def test_corruption_or_missing_manifest_never_wipes_live_file(store, damaged):
    add(store)
    add(store, ip="192.0.2.30")
    published, backup = store.discovery.read_bytes(), store.backup.read_bytes()
    if damaged is None:
        store.manifest.unlink()
    else:
        store.manifest.write_bytes(damaged)
    restarted = storage.PingStore(store.manifest.parent, store.discovery)
    for operation in (restarted.reconcile, restarted.list_targets, lambda: add(restarted, ip="192.0.2.23")):
        with pytest.raises(storage.PingError, match="manifest_unavailable"):
            operation()
    assert store.discovery.read_bytes() == published and store.backup.read_bytes() == backup
    if damaged is not None:
        assert store.manifest.read_bytes() == damaged


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes and flock")
def test_file_permissions_and_independent_writer_lock(store):
    add(store)
    add(store, ip="192.0.2.18")
    for path, expected in ((store.manifest, 0o600), (store.backup, 0o600),
                           (store.lockfile, 0o600), (store.discovery, 0o644)):
        assert stat.S_IMODE(path.stat().st_mode) == expected
    competing = storage.PingStore(store.manifest.parent, store.discovery)
    with store._locked():
        with pytest.raises(storage.PingError, match="busy"):
            add(competing, ip="192.0.2.19")
    assert competing.list_targets()["revision"] == 2
