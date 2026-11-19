"""D2 的 DNS、配置兼容、并发发布与历史外部行为回归。"""
import errno
import hashlib
import io
import json
import os
import socket
import threading
import time
from uuid import uuid4

import dns.message
import dns.rcode
import dns.rrset
import pytest

from platform_api import custom_ping, custom_ping_runtime as runtime
from platform_api.custom_ping_dns import Answer, Refresh, Resolver, hostname
from platform_api.custom_ping_store import PingError, PingStore
from .test_platform_custom_ping import api, prom, store, server_factory, edit_headers, ITEM


@pytest.fixture
def clock(store):
    now = [time.time()]
    store.dns.clock = lambda: now[0]
    return now


def create(store, address="game.example.com", private=None):
    fields = {"kind": "dns", "address": address, "name": "Game", "group": "Remote",
              "privateCidrs": private or []}
    fields = custom_ping.validate_changes(fields, True)
    status, result = store.change("POST", None, None, fields, "admin", str(uuid4()), lambda _: None)
    assert status == 201
    return result["target"]


def refresh(store, ips=("1.1.1.1",), ttl=300, error=""):
    ticket = store.dns_due()[0]
    assert store.resolve_dns(ticket, Answer(tuple(ips), ttl, error), lambda _: None)
    return store.list_targets()["targets"][0]


def patch(store, row, fields):
    return store.change("PATCH", row["id"], row["revision"], fields, "admin", str(uuid4()), lambda _: None)


@pytest.mark.parametrize("value", ["https://a.test/", "a.test:443", "a.test/path", "*.test", "x", "x..test",
                                   "a_.test", "-a.test", "1.2.3.4", "a." * 128, "x.test ", "::1"])
def test_hostname_rejects_non_hostnames(value):
    with pytest.raises((ValueError, UnicodeError)):
        hostname(value)


def test_hostname_normalization():
    assert hostname("GAME.Example.COM.") == "game.example.com"
    assert hostname("例子.example") == "xn--fsqu00a.example"


def test_schema1_legacy_request_replay_survives_schema2(store, api):
    _, request, headers, _ = api
    original_key = edit_headers(headers)
    status, old, _ = request("POST", body=ITEM, headers=original_key)
    assert status == 201
    legacy_bytes = store.manifest.read_bytes()
    assert request("GET")[1]["targets"] == [old["target"]]
    assert store.manifest.read_bytes() == legacy_bytes
    dns = create(store)
    manifest = json.loads(store.manifest.read_bytes())
    assert manifest["schema"] == 2 and manifest["revision"] == 2
    assert manifest["targets"][0] == old["target"]
    status, replay, _ = request("POST", body=ITEM, headers=original_key)
    assert status == 201 and replay["replayed"] and replay["target"] == old["target"]
    assert replay["savedRevision"] == 1 and replay["revision"] == 2
    assert store.list_targets()["targets"][1]["dnsStatus"] == "DNS_PENDING"
    assert store.list_targets()["targets"][1]["id"] == dns["id"]


def test_dns_http_auth_versions_and_invalid_private_policy(store, api):
    _, request, headers, _ = api
    body = {"kind": "dns", "address": "GAME.example.com.", "name": "Cloud", "group": "Remote"}
    assert request("POST", body=body)[0] == 401
    assert request("POST", body=body, headers=edit_headers(headers, **{"X-CSRF-Token": "bad"}))[0] == 403
    assert request("POST", body={**body, "privateCidrs": ["0.0.0.0/0"]},
                   headers=edit_headers(headers))[0] == 400
    status, result, _ = request("POST", body=body, headers=edit_headers(headers))
    assert status == 201
    row = result["target"]
    assert row["address"] == "game.example.com"
    assert request("PATCH", "/" + row["id"], {"name": "Renamed"}, edit_headers(headers))[0] == 428
    assert request("PATCH", "/" + row["id"], {"enabled": False}, edit_headers(headers, 99))[0] == 412
    assert request("POST", body=body, headers=edit_headers(headers))[0] == 409
    assert request("GET", "/" + row["id"] + "/resolutions")[1]["segments"] == []


def test_dns_selection_is_stable_and_admin_revision_does_not_change(store, clock):
    original = create(store)
    first = refresh(store, ("1.1.1.2", "1.1.1.1"))
    assert first["resolvedIp"] == "1.1.1.1" and first["aCount"] == 2
    saved = store.manifest.read_bytes()
    clock[0] += 241
    again = refresh(store, ("1.1.1.2", "1.1.1.1"))
    assert again["resolutionGeneration"] == first["resolutionGeneration"]
    clock[0] += 301
    switched = refresh(store, ("1.1.1.2",))
    assert switched["resolvedIp"] == "1.1.1.2"
    assert switched["resolutionGeneration"] > first["resolutionGeneration"]
    assert store.manifest.read_bytes() == saved
    assert switched["revision"] == original["revision"]
    assert [s["ip"] for s in store.resolution_history(original["id"])["segments"]] == ["1.1.1.1", "1.1.1.2"]


def test_ttl_shorter_than_refresh_cannot_remain_up(store, clock):
    row = create(store)
    refresh(store, ttl=10)
    clock[0] += 11
    assert store.list_targets()["targets"][0]["dnsStatus"] == "DNS_STALE"
    assert store.list_targets()["publication"] == "pending"
    assert store.dns_due() == []
    store.reconcile()
    assert json.loads(store.discovery.read_bytes()) == []
    assert store.resolution_history(row["id"])["segments"][0]["end"] < clock[0]


@pytest.mark.parametrize("error", ["nxdomain", "servfail", "timeout", "no_a", "cname_limit"])
def test_dns_failure_withdraws_endpoint_and_preserves_diagnostic_ip(store, clock, error):
    row = create(store)
    refresh(store)
    clock[0] += 301
    updated = refresh(store, (), 0, error)
    assert updated["dnsStatus"] == "DNS_ERROR" and updated["resolvedIp"] == "1.1.1.1"
    assert json.loads(store.discovery.read_bytes()) == []
    assert store.resolution_history(row["id"])["segments"][0]["end"] is not None
    assert store.dns_due() == []


def test_private_rebinding_requires_explicit_per_domain_cidr(store, clock):
    row = create(store)
    refresh(store)
    clock[0] += 241
    blocked = refresh(store, ("10.70.11.21",))
    assert blocked["dnsStatus"] == "DNS_POLICY_BLOCKED"
    assert json.loads(store.discovery.read_bytes()) == []
    updated = patch(store, row, {"privateCidrs": ["10.70.11.0/24"]})[1]["target"]
    allowed = refresh(store, ("10.70.11.21",))
    assert allowed["resolvedIp"] == "10.70.11.21" and allowed["dnsStatus"] == "DNS_OK"
    assert allowed["endpointRevision"] == updated["revision"]


def test_policy_and_duplicate_checks_apply_to_each_dns_refresh(store, clock, prom):
    create(store, private=["10.70.10.0/24"])
    prom[1].append({"scrapePool": "infra-srv-ping", "discoveredLabels": {"__address__": "10.70.10.22"}})
    row = store.dns_due()[0]
    store.resolve_dns(row, Answer(("10.70.10.22",), 300), prom[0].dns_policy())
    assert store.list_targets()["targets"][0]["dnsStatus"] == "DNS_POLICY_BLOCKED"
    clock[0] += 31
    row = store.dns_due()[0]
    store.resolve_dns(row, Answer(("169.254.169.254",), 300), prom[0].dns_policy())
    assert store.list_targets()["targets"][0]["dnsStatus"] == "DNS_POLICY_BLOCKED"
    assert json.loads(store.discovery.read_bytes()) == []


@pytest.mark.parametrize("action", ["edit", "disable", "delete", "newer_resolution"])
def test_late_dns_result_cannot_override_current_intent_or_resolution(store, clock, action):
    row = create(store)
    ticket = store.dns_due()[0]
    if action == "edit":
        patch(store, row, {"kind": "dns", "address": "new.example.com"})
    elif action == "disable":
        patch(store, row, {"enabled": False})
    elif action == "delete":
        store.change("DELETE", row["id"], row["revision"], {}, "admin", str(uuid4()), lambda _: None)
    else:
        store.resolve_dns(ticket, Answer(("1.1.1.2",), 300), lambda _: None)
    saved = (store.manifest.read_bytes(), store.dns.path.read_bytes(), store.discovery.read_bytes())
    assert store.resolve_dns(ticket, Answer(("1.1.1.1",), 300), lambda _: None) is False
    assert saved == (store.manifest.read_bytes(), store.dns.path.read_bytes(), store.discovery.read_bytes())


def test_disable_restore_and_a_b_a_have_new_activation_generations(store, clock):
    row = create(store)
    a = refresh(store)
    clock[0] += 301
    b = refresh(store, ("1.1.1.2",))
    clock[0] += 301
    again = refresh(store)
    assert a["resolutionGeneration"] < b["resolutionGeneration"] < again["resolutionGeneration"]
    assert again["activatedAt"] > a["activatedAt"]
    assert again["sampleNotBefore"] is None
    disabled = patch(store, row, {"enabled": False})[1]["target"]
    store.reconcile()
    assert json.loads(store.discovery.read_bytes()) == []
    patch(store, disabled, {"enabled": True})
    assert store.list_targets()["targets"][0]["dnsStatus"] == "DNS_PENDING"
    restored = refresh(store)
    assert restored["resolutionGeneration"] > again["resolutionGeneration"]


def test_runtime_corruption_and_missing_never_reset_or_overwrite_files(store, clock):
    create(store); refresh(store)
    published = store.discovery.read_bytes()
    store.dns.path.write_bytes(b'{"schema":99}')
    for operation in (store.reconcile, store.list_targets, store.dns_due):
        with pytest.raises(PingError, match="dns_state_unavailable"):
            operation()
    assert store.dns.path.read_bytes() == b'{"schema":99}'
    assert store.discovery.read_bytes() == published
    store.dns.path.unlink()
    with pytest.raises(PingError, match="dns_state_unavailable"):
        store.reconcile()
    assert store.discovery.read_bytes() == published


def test_publication_failure_is_pending_and_restart_uses_same_projection(store, clock, monkeypatch):
    create(store)
    replace = os.replace
    def fail_discovery(source, destination):
        if destination == store.discovery:
            raise OSError("synthetic full discovery filesystem")
        return replace(source, destination)
    with monkeypatch.context() as patching:
        patching.setattr(os, "replace", fail_discovery)
        with pytest.raises(PingError, match="publication_failed"):
            refresh(store)
        assert store.list_targets()["publication"] == "pending"
    restarted = PingStore(store.manifest.parent, store.discovery)
    restarted.reconcile()
    assert restarted.list_targets()["targets"][0]["dnsStatus"] == "DNS_STALE"
    assert json.loads(store.discovery.read_bytes()) == []
    refresh(restarted)
    assert restarted.list_targets()["targets"][0]["dnsStatus"] == "DNS_OK"
    assert json.loads(store.discovery.read_bytes())[0]["targets"] == ["1.1.1.1"]
    assert restarted.list_targets()["publication"] == "published"


def test_runtime_write_failure_leaves_old_state_and_discovery(store, clock, monkeypatch):
    create(store); refresh(store)
    original = (store.dns.path.read_bytes(), store.discovery.read_bytes(), store.manifest.read_bytes())
    clock[0] += 241
    replace = os.replace
    def fail_runtime(source, destination):
        if destination == store.dns.path:
            raise OSError("synthetic full runtime filesystem")
        return replace(source, destination)
    monkeypatch.setattr(os, "replace", fail_runtime)
    with pytest.raises(PingError, match="dns_commit_failed"):
        refresh(store, ("1.1.1.2",))
    assert store.dns.path.read_bytes() == original[0]
    assert store.manifest.read_bytes() == original[2]
    assert store.list_targets()["targets"][0]["dnsStatus"] == "DNS_ERROR"
    assert json.loads(store.discovery.read_bytes()) == []


def test_history_retains_deleted_identity_and_bounds_capacity(store, clock, monkeypatch):
    row = create(store)
    refresh(store)
    monkeypatch.setattr(runtime, "MAX_SEGMENTS", 1)
    clock[0] += 241
    blocked = refresh(store, ("1.1.1.2",))
    assert blocked["dnsError"] == "history_capacity"
    assert json.loads(store.discovery.read_bytes()) == []
    store.change("DELETE", row["id"], row["revision"], {}, "admin", str(uuid4()), lambda _: None)
    history = store.resolution_history(row["id"])
    assert history["segments"][0]["ip"] == "1.1.1.1"
    clock[0] += 7 * 86400 + 1
    store.reconcile()
    assert store.resolution_history(row["id"])["segments"] == []


def test_compatible_pause_preserves_schema2_and_removes_only_dns(store, clock):
    create(store); refresh(store)
    old = store.manifest.read_bytes()
    store.dns_enabled = False
    store.reconcile()
    assert store.manifest.read_bytes() == old
    assert store.list_targets()["dnsEnabled"] is False
    assert json.loads(store.discovery.read_bytes()) == []
    assert store.dns_due() == []
    store.dns_enabled = True
    restored = refresh(store)
    assert restored["resolutionGeneration"] > 1


def test_evidence_needs_discovered_generation_and_post_activation_samples(store, clock, prom, monkeypatch):
    row = create(store); visible = refresh(store)
    snap = store.list_targets()
    found = json.loads(store.discovery.read_bytes())[0]
    prom[1].append({"scrapePool": "custom-ping",
                    "labels": {"custom_id": row["id"], "target_ip": visible["resolvedIp"]},
                    "discoveredLabels": found["labels"], "health": "up"})
    now = clock[0] + 2
    store.dns.clock = lambda: now
    def evidence(stamp, success):
        samples = []
        for name, value in (("up", 1), ("probe_success", success)):
            for kind, reading in (("value", value), ("time", stamp)):
                samples.append({"metric": {"custom_id": row["id"], "target_ip": visible["resolvedIp"],
                                           "sampleMetric": name, "sampleType": kind},
                                "value": [now, str(reading)]})
        prom[2]["/api/v1/query?"] = {"status": "success", "data": {"resultType": "vector", "result": samples}}
        original = prom[0]._prometheus_data
        monkeypatch.setattr(prom[0], "_prometheus_data",
                            lambda path: prom[2]["/api/v1/query?"]["data"] if path.startswith("/api/v1/query?") else
                            original(path))
    evidence(clock[0] + 1, 1)
    assert prom[0].observed(snap, store)["dnsTargets"][0]["pingStatus"] == "UNKNOWN"
    now += 2
    evidence(now - 1, 1)
    assert prom[0].observed(snap, store)["dnsTargets"][0]["pingStatus"] == "UP"
    evidence(now - 1, 0)
    assert prom[0].observed(snap, store)["dnsTargets"][0]["pingStatus"] == "DOWN"
    prom[1][-1]["discoveredLabels"]["__meta_custom_ping_resolution_generation"] = "99"
    assert prom[0].observed(snap, store)["dnsTargets"][0]["pingStatus"] == "UNKNOWN"


@pytest.mark.parametrize("failure", [OSError, custom_ping.HTTPException])
def test_prometheus_evidence_failure_is_unknown(store, clock, prom, monkeypatch, failure):
    row = create(store); visible = refresh(store)
    found = json.loads(store.discovery.read_bytes())[0]
    prom[1].append({"scrapePool": "custom-ping",
                    "labels": {"custom_id": row["id"], "target_ip": visible["resolvedIp"]},
                    "discoveredLabels": found["labels"], "health": "up"})
    original = prom[0]._prometheus_data
    def unavailable(path):
        if path.startswith("/api/v1/query?"):
            raise failure("synthetic metrics connection failure")
        return original(path)
    monkeypatch.setattr(prom[0], "_prometheus_data", unavailable)
    state = prom[0].observed(store.list_targets(), store)
    assert state["discoveredRevision"] == row["revision"]
    assert state["dnsTargets"][0]["pingStatus"] == "UNKNOWN"


@pytest.fixture
def dns_server():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(0.05)
    config = {"mode": "a", "ttl": 90}
    closed = threading.Event()
    def loop():
        while not closed.is_set():
            try:
                raw, peer = sock.recvfrom(65535)
            except socket.timeout:
                continue
            request = dns.message.from_wire(raw)
            if config["mode"] == "timeout":
                continue
            response = dns.message.make_response(request)
            name = request.question[0].name.to_text()
            if config["mode"] in ("nxdomain", "servfail"):
                response.set_rcode(dns.rcode.NXDOMAIN if config["mode"] == "nxdomain" else dns.rcode.SERVFAIL)
            elif config["mode"] == "split":
                config["queries"].append(name)
                if name == "game.example.com.":
                    response.answer.append(dns.rrset.from_text(name, 1, "IN", "CNAME", "alias.example.com."))
                else:
                    config["advance"]()
                    response.answer.append(dns.rrset.from_text(name, 5, "IN", "A", "1.1.1.1"))
            elif config["mode"] == "aaaa":
                response.answer.append(dns.rrset.from_text(name, 90, "IN", "AAAA", "2001:db8::1"))
            else:
                hops = {"a": 0, "cname": 1, "deep": 9, "loop": 2}[config["mode"]]
                for i in range(hops):
                    target = request.question[0].name.to_text() if config["mode"] == "loop" and i == 1 else f"alias{i}.example.com."
                    response.answer.append(dns.rrset.from_text(name, 45, "IN", "CNAME", target))
                    name = target
                response.answer.append(dns.rrset.from_text(name, config["ttl"], "IN", "A", *config.get("ips", ("1.1.1.2", "1.1.1.1"))))
            sock.sendto(response.to_wire(), peer)
    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    yield Resolver(["127.0.0.1"], sock.getsockname()[1]), config
    closed.set(); thread.join(2); sock.close()


@pytest.mark.parametrize("mode,expected", [("a", ""), ("cname", ""), ("deep", "cname_limit"),
                                          ("nxdomain", "nxdomain"), ("servfail", "servfail"),
                                          ("aaaa", "no_a"), ("timeout", "timeout")])
def test_real_isolated_dns_a_cname_and_failures(dns_server, mode, expected):
    resolver, config = dns_server
    config["mode"] = mode
    started = time.monotonic()
    answer = resolver.resolve("game.example.com")
    assert time.monotonic() - started < 3
    assert answer.error == expected
    if not expected:
        assert answer.ips == ("1.1.1.1", "1.1.1.2")
        assert answer.ttl == (45 if mode == "cname" else 90)



def test_mixed_answers_only_publish_one_allowed_address(store, clock, prom):
    prom[0].allowed = ""
    create(store)
    store.resolve_dns(store.dns_due()[0], Answer(("127.0.0.1", "1.1.1.1"), 300), prom[0].dns_policy())
    current = store.list_targets()["targets"][0]
    assert current["dnsStatus"] == "DNS_OK" and current["aCount"] == 2
    assert json.loads(store.discovery.read_bytes())[0]["targets"] == ["1.1.1.1"]


def test_refresh_has_eight_global_workers_and_one_inflight_per_id(store, clock):
    identities = [create(store, f"game{i}.example.com")["id"] for i in range(12)]
    release = threading.Event()
    entered = []
    class HangingResolver:
        def resolve(self, name):
            entered.append(name)
            release.wait(3)
            return Answer(error="timeout")
    worker = Refresh(store, lambda: lambda _: None, HangingResolver())
    try:
        worker.tick()
        limit = time.monotonic() + 2
        while len(entered) < 8 and time.monotonic() < limit:
            time.sleep(0.01)
        assert len(entered) == 8
        worker.tick()
        assert len(worker.pending) == 8 and len(entered) == 8
        assert set(worker.pending) <= set(identities)
    finally:
        release.set()
        worker.stop()


@pytest.mark.skipif(os.name == "nt", reason="cross-process POSIX flock is covered by Linux CI")
def test_dns_writer_and_admin_writer_use_same_process_lock(store, clock):
    create(store)
    ticket = store.dns_due()[0]
    other = PingStore(store.manifest.parent, store.discovery)
    with store._locked():
        with pytest.raises(PingError, match="busy"):
            other.resolve_dns(ticket, Answer(("1.1.1.1",), 300), lambda _: None)


def test_schema2_ipv4_conversion_and_metadata_edits_preserve_identity(store, clock):
    row = create(store); first = refresh(store)
    renamed = patch(store, row, {"name": "Renamed", "group": "Server"})[1]["target"]
    state = store.list_targets()["targets"][0]
    assert state["resolutionGeneration"] == first["resolutionGeneration"]
    assert state["dnsStatus"] == "DNS_OK"
    changed = patch(store, renamed, {"ip": "10.70.20.10"})[1]["target"]
    assert "kind" not in changed and changed["id"] == row["id"]
    store.reconcile()
    assert json.loads(store.discovery.read_bytes())[0]["targets"] == ["10.70.20.10"]
    assert store.resolution_history(row["id"])["segments"][0]["end"] is not None


def test_cname_loop_is_bounded(dns_server):
    resolver, config = dns_server
    config["mode"] = "loop"
    start = time.monotonic()
    assert resolver.resolve("game.example.com").error in ("cname_limit", "invalid_response")
    assert time.monotonic() - start < 3


@pytest.mark.skipif(
    os.environ.get("RUN_CUSTOM_PING_PROMETHEUS") != "1" and os.environ.get("GITHUB_ACTIONS") != "true",
    reason="real Docker DNS/Prometheus/Blackbox integration runs in Linux CI",
)
def test_dns_real_prometheus_blackbox_generation_and_history(tmp_path, monkeypatch, dns_server, capsys):
    from .test_custom_ping_prometheus import IMAGE, ROOT, SD_DIR, docker, wait_for
    from .test_custom_ping_frontend_integration import BLACKBOX, prometheus_fetch, query

    resolver, dns_config = dns_server
    work = tmp_path / "fixture"
    work.mkdir(mode=0o755)
    work.chmod(0o755)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    discovery = tmp_path / "sd" / "custom_targets.json"
    collector = tmp_path / "collector" / "custom_targets.json"
    collector.parent.mkdir(mode=0o755)
    collector.parent.chmod(0o755)
    local = PingStore(private, discovery)
    local.reconcile()

    def let_prometheus_read():
        staged = collector.with_suffix(".tmp")
        staged.write_bytes(discovery.read_bytes())
        staged.chmod(0o644)
        os.replace(staged, collector)

    let_prometheus_read()
    fixture = f"{work}:/fixture"
    (work / "blackbox.yml").write_text(
        "modules:\n  icmp:\n    prober: icmp\n    timeout: 1s\n    icmp:\n      preferred_ip_protocol: ip4\n", encoding="utf-8")
    generator = (ROOT / "prometheus-gen-config.sh").read_text(encoding="utf-8")
    (work / "generate.sh").write_text(generator.split("exec /bin/prometheus", 1)[0], encoding="utf-8")
    authority = work / "authority.json"
    authority.write_text("[]", encoding="ascii")
    authority.chmod(0o644)
    docker("run", "--rm", "--network", "none", "--user", "0", "-v", fixture, "--entrypoint", "/bin/sh",
           "-e", "SWITCH_TARGETS_FILE=/fixture/authority.json",
           "-e", "PROMETHEUS_CONFIG_FILE=/fixture/prometheus.yml", IMAGE, "/fixture/generate.sh", timeout=120)
    prefix = "dns-ci-" + uuid4().hex[:12]
    net, prom_name, bb, peer = prefix + "-net", prefix + "-prom", prefix + "-bb", prefix + "-peer"
    a, b = "172.31.248.21", "172.31.248.22"
    dns_config["ips"] = [a]
    query_body = [None]
    def prom_open(url, timeout):
        try:
            endpoint = url.removeprefix("http://prom:9090")
            if query_body[0] and endpoint.startswith("/api/v1/query?"):
                return io.BytesIO(query_body[0])
            body = prometheus_fetch(prom_name, endpoint).encode()
            return io.BytesIO(body)
        except Exception as exc:
            raise OSError("isolated Prometheus unavailable") from exc
    monkeypatch.setattr(custom_ping, "urlopen", prom_open)
    inventory = custom_ping.TargetInventory("http://prom:9090", "", "", 2)
    def ticket():
        item = json.loads(local.manifest.read_bytes())["targets"][0]
        saved = local.dns.read(required=True)["rows"].get(item["id"])
        from platform_api.custom_ping_store import encode_json
        return {**item, "_runtimeDigest": hashlib.sha256(encode_json(saved)).hexdigest()}
    docker("network", "create", "--internal", "--subnet", "172.31.248.0/24", net)
    try:
        docker("run", "-d", "--name", bb, "--user", "0", "--network", net, "--ip", a,
               "--network-alias", "blackbox-exporter", "--cap-add", "NET_RAW", "-v", fixture + ":ro",
               BLACKBOX, "--config.file=/fixture/blackbox.yml", timeout=120)
        docker("run", "-d", "--name", peer, "--user", "0", "--network", net, "--ip", b,
               "--cap-add", "NET_RAW", "-v", fixture + ":ro", BLACKBOX, "--config.file=/fixture/blackbox.yml")
        docker("run", "-d", "--name", prom_name, "--network", net,
               "-v", fixture + ":ro", "-v", f"{collector.parent}:{SD_DIR}:ro", IMAGE,
               "--config.file=/fixture/prometheus.yml", "--storage.tsdb.path=/prometheus")
        wait_for(lambda: prometheus_fetch(prom_name, "/api/v1/scrape_pools"), lambda raw: "custom-ping" in raw)
        created = create(local, private=["172.31.248.0/24"])
        assert inventory.observed(local.list_targets(), local)["dnsTargets"][0]["pingStatus"] == "UNKNOWN"
        for address in (a, b, a):
            dns_config["ips"] = [address]
            answer = resolver.resolve("game.example.com")
            assert answer.ips == (address,)
            assert local.resolve_dns(ticket(), answer, inventory.dns_policy())
            let_prometheus_read()
            pending = inventory.observed(local.list_targets(), local)["dnsTargets"][0]
            assert pending["pingStatus"] == "UNKNOWN"
            ready = wait_for(lambda: inventory.observed(local.list_targets(), local),
                             lambda info: info["dnsTargets"][0]["pingStatus"] == "UP")
            assert ready["dnsTargets"][0]["resolvedIp"] == address
            assert ready["dnsTargets"][0]["discoveredGeneration"] == ready["dnsTargets"][0]["resolutionGeneration"]
        metrics = wait_for(lambda: query(prom_name, 'count by (target_ip) (count_over_time(probe_success{job="custom-ping"}[5m]))'),
                           lambda result: len(result) == 2)
        assert {r["metric"]["target_ip"] for r in metrics} == {a, b}
        # Leave Prometheus on the last live A while the API commits B and
        # then a new A. Repeat with B publication itself failing.
        expected_history = [a, b, a]
        for publication_failed in (False, True):
            from urllib.parse import urlencode

            baseline = inventory.observed(local.list_targets(), local)["dnsTargets"][0]
            dns_config["ips"] = [b]
            with monkeypatch.context() as busted:
                if publication_failed:
                    original = os.replace
                    def fail_B(source, destination):
                        if destination == discovery:
                            raise OSError(errno.ENOSPC, "fixture file_sd volume full")
                        return original(source, destination)
                    busted.setattr(os, "replace", fail_B)
                    with pytest.raises(PingError, match="publication_failed"):
                        local.resolve_dns(ticket(), resolver.resolve("game.example.com"), inventory.dns_policy())
                    assert local.list_targets()["publication"] == "pending"
                else:
                    assert local.resolve_dns(ticket(), resolver.resolve("game.example.com"), inventory.dns_policy())
                assert inventory.observed(local.list_targets(), local)["dnsTargets"][0]["pingStatus"] == "UNKNOWN"
            actual = next(row for row in inventory.active() if row["scrapePool"] == "custom-ping")
            assert actual["labels"]["target_ip"] == a
            assert int(actual["discoveredLabels"]["__meta_custom_ping_resolution_generation"]) == baseline["resolutionGeneration"]

            dns_config["ips"] = [a]
            assert local.resolve_dns(ticket(), resolver.resolve("game.example.com"), inventory.dns_policy())
            current = local.list_targets()["targets"][0]
            # Capture real old-A samples that are newer than the new A intent.
            selector = '{job="custom-ping",__name__=~"up|probe_success"}'
            named = 'label_replace(' + selector + ',"sampleMetric","$1","__name__","(.+)")'
            expr = ('label_replace(' + named + ',"sampleType","value","job",".*") or '
                    'label_replace(timestamp(' + named + '),"sampleType","time","job",".*")')
            def captured_old_A():
                body = prometheus_fetch(prom_name, "/api/v1/query?" + urlencode({"query": expr}))
                samples = json.loads(body)["data"]["result"]
                stamps = [float(item["value"][1]) for item in samples
                          if item["metric"]["target_ip"] == a and item["metric"]["sampleType"] == "time"]
                return body, stamps

            old_body, timestamps = wait_for(captured_old_A,
                                            lambda result: len(result[1]) == 2 and min(result[1]) > current["activatedAt"])
            assert current["sampleNotBefore"] is None
            query_body[0] = old_body.encode()
            let_prometheus_read()
            first = wait_for(lambda: inventory.observed(local.list_targets(), local)["dnsTargets"][0],
                             lambda value: value["discoveredGeneration"] == current["resolutionGeneration"])
            assert first["sampleNotBefore"] > max(timestamps)
            assert first["pingStatus"] == "UNKNOWN"
            query_body[0] = None
            live = wait_for(lambda: inventory.observed(local.list_targets(), local)["dnsTargets"][0],
                            lambda value: value["pingStatus"] == "UP")
            assert live["sampleNotBefore"] == first["sampleNotBefore"]
            expected_history.extend((b, a))
        dns_config["mode"] = "nxdomain"
        local.resolve_dns(ticket(), resolver.resolve("game.example.com"), inventory.dns_policy())
        let_prometheus_read()
        assert inventory.observed(local.list_targets(), local)["dnsTargets"][0]["pingStatus"] == "UNKNOWN"
        wait_for(lambda: inventory.observed(local.list_targets(), local), lambda info: info["activeTargets"] == [])
        segments = local.resolution_history(created["id"])["segments"]
        assert [s["ip"] for s in segments] == expected_history
        assert all(s["sampleNotBefore"] is None for s in segments[3::2])
        assert all(s["sampleNotBefore"] > s["start"] for s in segments[4::2])
        if os.environ.get("RUN_CUSTOM_PING_DOMAIN_BROWSER") == "1":
            from .ping_domain_browser_fixture import run_domain_browser
            run_domain_browser(monkeypatch, ROOT, local, inventory, resolver, dns_config, let_prometheus_read,
                               lambda url: prometheus_fetch(prom_name, url), created["id"], a, b)
        else:
            from .test_custom_ping_restore_fixture import restore_cold_archive
            with capsys.disabled():
                restore_cold_archive(tmp_path, monkeypatch, local, prom_name, net, fixture,
                                     inventory, resolver, dns_config, created, (a, b))
    finally:
        docker("rm", "-f", "-v", prom_name, bb, peer, check=False)
        docker("network", "rm", net, check=False)



@pytest.mark.parametrize("ttl,delay,expected", [(1, 2, "DNS_STALE"), (5, 6, "DNS_STALE"),
                                               (5, 2, "DNS_OK")])
def test_refresh_ttl_includes_inventory_and_commit_delay(store, clock, prom, dns_server, ttl, delay, expected):
    resolver, config = dns_server
    config.update(ttl=ttl, ips=["1.1.1.1"])
    prom[0].allowed = ""
    wall_start = clock[0]
    ticks = [10.]
    resolver.clock = lambda: ticks[0]
    resolver.wall_clock = store.dns.clock
    store.dns.monotonic = resolver.clock
    create(store)

    def delayed_inventory():
        policy = prom[0].dns_policy()
        clock[0] += delay
        ticks[0] += delay
        return policy

    worker = Refresh(store, delayed_inventory, resolver)
    try:
        worker.tick()
        next(iter(worker.pending.values())).result(5)
    finally:
        worker.stop()
    row = store.list_targets()["targets"][0]
    assert row["dnsStatus"] == expected
    projected = json.loads(store.discovery.read_bytes())
    if expected == "DNS_OK":
        assert row["expiresAt"] == wall_start + ttl
        assert projected[0]["targets"] == ["1.1.1.1"]
    else:
        assert projected == []


@pytest.mark.parametrize("address,networks,allowed", [
    ("10.70.11.0", ["10.70.11.0/24"], False),
    ("10.70.11.255", ["10.70.11.0/24"], False),
    ("10.70.11.0", ["10.70.11.0/24", "10.70.11.0/31"], True),
    ("10.70.11.1", ["10.70.11.0/31"], True),
    ("10.70.11.255", ["10.70.11.255/32"], True),
    ("10.70.11.127", ["10.70.11.0/24", "10.70.11.0/25"], False),
])
def test_domain_cidr_uses_most_specific_prefix(store, clock, prom, address, networks, allowed):
    prom[0].allowed = ""
    create(store, private=networks)
    store.resolve_dns(store.dns_due()[0], Answer((address,), 300), prom[0].dns_policy())
    targets = json.loads(store.discovery.read_bytes())
    assert [item["targets"][0] for item in targets] == ([address] if allowed else [])
    assert store.list_targets()["targets"][0]["dnsStatus"] == ("DNS_OK" if allowed else "DNS_POLICY_BLOCKED")


def test_http_dns_rename_requires_new_private_authorization(store, api):
    _, request, headers, _ = api
    row = create(store, private=["10.70.11.0/24"])
    path = "/" + row["id"]
    old_ticket = store.dns_due()[0]
    status, same, _ = request("PATCH", path, {"kind": "dns", "address": "GAME.example.COM.", "name": "New"},
                              edit_headers(headers, row["revision"]))
    assert status == 200
    assert same["target"]["privateCidrs"] == ["10.70.11.0/24"]
    assert same["target"]["endpointRevision"] == row["endpointRevision"]
    status, renamed, _ = request("PATCH", path, {"kind": "dns", "address": "other.example.com"},
                                 edit_headers(headers, same["target"]["revision"]))
    assert status == 200 and renamed["target"]["privateCidrs"] == []
    assert not store.resolve_dns(old_ticket, Answer(("10.70.11.3",), 300), lambda _: None)
    explicit = {"kind": "dns", "address": "third.example.com", "privateCidrs": ["10.70.11.3/32"]}
    key = edit_headers(headers, renamed["target"]["revision"])
    status, authorized, _ = request("PATCH", path, explicit, key)
    assert status == 200 and authorized["target"]["privateCidrs"] == explicit["privateCidrs"]
    assert request("PATCH", path, explicit, key)[1]["replayed"]



def test_cname_followup_cannot_restart_earlier_ttl(store, clock, dns_server, prom):
    resolver, config = dns_server
    ticks = [10.]
    def advance():
        ticks[0] += 1.5
        clock[0] += 1.5

    resolver.clock = store.dns.monotonic = lambda: ticks[0]
    resolver.wall_clock = store.dns.clock
    config.update(mode="split", queries=[], advance=advance)
    prom[0].allowed = ""
    create(store)
    worker = Refresh(store, prom[0].dns_policy, resolver)
    try:
        worker.tick()
        next(iter(worker.pending.values())).result(5)
    finally:
        worker.stop()
    assert config["queries"] == ["game.example.com.", "alias.example.com."]
    assert store.list_targets()["targets"][0]["dnsError"] == "ttl_expired"
    assert json.loads(store.discovery.read_bytes()) == []


@pytest.mark.parametrize("phase", ["lock", "runtime_stage", "projection_stage"])
def test_ttl_is_checked_at_lock_and_file_boundaries(store, clock, monkeypatch, phase):
    from contextlib import contextmanager
    from platform_api import custom_ping_store as storage

    ticks = [10.]
    store.dns.monotonic = lambda: ticks[0]
    row = create(store)
    ticket = store.dns_due()[0]
    answer = Answer(("1.1.1.1",), 1, started_at=clock[0], started_monotonic=ticks[0])

    def advance():
        # Monotonic elapsed time remains authoritative if UTC did not advance.
        ticks[0] += 2

    if phase == "lock":
        locked = store._locked
        @contextmanager
        def delayed_lock():
            with locked():
                advance()
                yield
        monkeypatch.setattr(store, "_locked", delayed_lock)
    else:
        module = runtime if phase == "runtime_stage" else storage
        stage = module.staged_file
        path = store.dns.path if phase == "runtime_stage" else store.discovery
        @contextmanager
        def delayed_stage(destination, data, mode):
            with stage(destination, data, mode) as tmp:
                if destination == path:
                    advance()
                yield tmp
        monkeypatch.setattr(module, "staged_file", delayed_stage)

    assert store.resolve_dns(ticket, answer, lambda _: None)
    assert store.list_targets()["targets"][0]["dnsStatus"] == "DNS_STALE"
    assert json.loads(store.discovery.read_bytes()) == []
    if phase != "projection_stage":
        assert store.resolution_history(row["id"])["segments"] == []


def project_as_discovered(store, prom):
    observed = []
    for entry in json.loads(store.discovery.read_bytes()):
        observed.append({"scrapePool": "custom-ping",
                         "labels": {"custom_id": entry["labels"]["custom_id"], "target_ip": entry["targets"][0]},
                         "health": "up",
                         "discoveredLabels": {**entry["labels"], "__address__": entry["targets"][0]}})
    prom[1][1:] = observed


def metric_response(monkeypatch, prom, readings):
    original = prom[0]._prometheus_data
    def fetch(path):
        if not path.startswith("/api/v1/query?"):
            return original(path)
        timestamp, success = readings()
        samples = []
        for target in prom[1][1:]:
            labels = target["discoveredLabels"]
            for metric, value in (("up", 1), ("probe_success", success)):
                for kind, reading in (("value", value), ("time", timestamp)):
                    samples.append({"metric": {"custom_id": labels["custom_id"], "target_ip": labels["__address__"],
                                               "sampleMetric": metric, "sampleType": kind},
                                    "value": [timestamp, str(reading)]})
        return {"resultType": "vector", "result": samples}
    monkeypatch.setattr(prom[0], "_prometheus_data", fetch)


@pytest.mark.parametrize("failure,result,can_withdraw", [
    ("write", Answer(error="nxdomain"), True),
    ("replace", Answer(("169.254.169.254",), 300), True),
    ("fsync", Answer(("1.1.1.3",), 300), True),
    ("replace", Answer(error="nxdomain"), False),
])
def test_failed_runtime_transition_invalidates_only_affected_health(
        store, clock, prom, api, monkeypatch, failure, result, can_withdraw):
    from contextlib import contextmanager

    _, request, _, _ = api
    ticks = [10.]
    store.dns.monotonic = lambda: ticks[0]
    prom[0].allowed = ""
    row = create(store)
    other = create(store, "other.example.com")
    store.change("POST", None, None, {**ITEM, "ip": "10.70.10.54"}, "admin", str(uuid4()), prom[0].check)
    for ip in ("1.1.1.1", "1.1.1.2"):
        store.resolve_dns(store.dns_due()[0], Answer((ip,), 300), prom[0].dns_policy())
    project_as_discovered(store, prom)
    metric_response(monkeypatch, prom, lambda: (clock[0] - .5, 1))
    discover = lambda: {item["id"]: item for item in request("GET", "/discovery")[1]["dnsTargets"]}
    assert discover()[row["id"]]["pingStatus"] == "UNKNOWN"
    clock[0] += 241
    assert all(discover()[target]["pingStatus"] == "UP" for target in (row["id"], other["id"]))
    ticket = store.dns_due()[0]
    manifest = store.manifest.read_bytes()
    replace = os.replace

    @contextmanager
    def fail_write(*args):
        raise OSError(errno.ENOSPC, "fixture write failed")
        yield

    def fail_sync(directory):
        raise OSError(errno.ENOSPC, "fixture directory fsync failed")

    def fail_replace(source, destination):
        if failure == "replace" and destination == store.dns.path or not can_withdraw and destination == store.discovery:
            raise OSError(errno.ENOSPC, "fixture replace failed")
        return replace(source, destination)

    with monkeypatch.context() as busted:
        busted.setattr(os, "replace", fail_replace)
        if failure == "write":
            busted.setattr(runtime, "staged_file", fail_write)
        if failure == "fsync":
            busted.setattr(runtime, "sync_directory", fail_sync)
        with pytest.raises(PingError, match="dns_commit_failed"):
            store.resolve_dns(ticket, result, prom[0].dns_policy())
        catalog = request("GET")[1]
        assert catalog["targets"][0]["dnsError"] == "storage"
        assert catalog["publication"] == ("published" if can_withdraw else "pending")
        state = discover()
        assert state[row["id"]]["pingStatus"] == "UNKNOWN" and state[row["id"]]["withdrawalPending"]
        assert state[other["id"]]["pingStatus"] == "UP"
        expected = {"1.1.1.2", "10.70.10.54"} | (set() if can_withdraw else {"1.1.1.1"})
        assert {entry["targets"][0] for entry in json.loads(store.discovery.read_bytes())} == expected
        # The previous target may still appear in Prometheus until its next SD update.
        if can_withdraw:
            project_as_discovered(store, prom)
            assert not discover()[row["id"]]["withdrawalPending"]
        try:
            store.reconcile()
        except PingError:
            pass
        assert {entry["targets"][0] for entry in json.loads(store.discovery.read_bytes())} == expected

    assert store.manifest.read_bytes() == manifest
    assert store.resolution_history(row["id"])["segments"][0]["end"] is not None
    restarted = PingStore(store.manifest.parent, store.discovery)
    restarted.dns.clock = store.dns.clock
    restart_snapshot = restarted.list_targets()
    assert restart_snapshot["targets"][0]["dnsStatus"] != "DNS_OK"
    assert prom[0].observed(restart_snapshot, restarted)["dnsTargets"][0]["pingStatus"] == "UNKNOWN"
    restarted.reconcile()
    assert [entry["targets"][0] for entry in json.loads(store.discovery.read_bytes())] == ["10.70.10.54"]
    # A fresh verified answer can recover after storage is writable.
    tickets = {ticket["id"]: ticket for ticket in restarted.dns_due()}
    recovery = tickets.get(row["id"])
    if recovery is None:
        clock[0] += 31
        recovery = next(t for t in restarted.dns_due() if t["id"] == row["id"])
    assert restarted.resolve_dns(recovery, Answer(("1.1.1.4",), 300), prom[0].dns_policy())
    assert restarted.list_targets()["targets"][0]["resolvedIp"] == "1.1.1.4"


def test_history_byte_limit_keeps_seven_day_history_and_withdraws(store, clock, monkeypatch):
    from platform_api.custom_ping_store import encode_json

    row = create(store)
    refresh(store)
    before = store.resolution_history(row["id"])["segments"]
    state = store.dns.read(required=True)
    # Exhaust only the history budget while leaving the reserved close/error space.
    cap = len(encode_json({**state, "rows": {}})) + runtime.RUNTIME_RESERVE_BYTES + runtime.OPEN_SEGMENT_RESERVE_BYTES
    monkeypatch.setattr(runtime, "MAX_BYTES", cap)
    clock[0] += 241
    result = refresh(store, ("1.1.1.2",))
    assert result["dnsError"] == "history_capacity" and result["dnsStatus"] == "DNS_ERROR"
    assert json.loads(store.discovery.read_bytes()) == []
    segments = store.resolution_history(row["id"])["segments"]
    assert len(segments) == 1 and segments[0]["ip"] == before[0]["ip"]
    assert segments[0]["end"] == clock[0]
    clock[0] += runtime.RETENTION - 1
    store.reconcile()
    assert store.resolution_history(row["id"])["segments"] == segments


@pytest.mark.parametrize("name", ["localhost", "node.localhost", "node.local", "NODE.LOCAL."])
def test_dns_api_refuses_local_resolution_suffixes(api, name):
    _, request, headers, _ = api
    status, _, _ = request("POST", body={"kind": "dns", "address": name, "name": "Local", "group": "Remote"},
                           headers=edit_headers(headers))
    assert status == 400



def test_late_targets_and_metrics_cannot_confirm_superseded_generation(store, clock, prom, monkeypatch):
    row = create(store)
    refresh(store)
    project_as_discovered(store, prom)
    original_active = prom[0].active
    old_snapshot = store.list_targets()
    old_active = original_active()
    clock[0] += 241
    refresh(store, ("1.1.1.2",))
    clock[0] += 241
    current = refresh(store)
    project_as_discovered(store, prom)

    metric_response(monkeypatch, prom, lambda: (clock[0] - .5, 1))
    latest = prom[0].observed(store.list_targets(), store)["dnsTargets"][0]
    assert latest["pingStatus"] == "UNKNOWN"
    boundary = latest["sampleNotBefore"]
    with monkeypatch.context() as delayed:
        delayed.setattr(prom[0], "active", lambda: old_active)
        old = prom[0].observed(old_snapshot, store)["dnsTargets"][0]
        assert old["discoveredGeneration"] is None and old["pingStatus"] == "UNKNOWN"

    clock[0] += 2
    recent = prom[0].observed(store.list_targets(), store)["dnsTargets"][0]
    assert recent["pingStatus"] == "UP" and recent["sampleNotBefore"] == boundary
    segment = store.resolution_history(row["id"])["segments"][-1]
    assert segment["generation"] == current["resolutionGeneration"]
    assert segment["start"] <= segment["sampleNotBefore"] == boundary

    fetch = prom[0]._prometheus_data
    def delayed_metrics(path):
        data = fetch(path)
        if path.startswith("/api/v1/query?"):
            store.change("PATCH", row["id"], row["revision"], {"enabled": False},
                         "admin", str(uuid4()), lambda _: None)
        return data

    monkeypatch.setattr(prom[0], "_prometheus_data", delayed_metrics)
    data = prom[0].observed(store.list_targets(), store)["dnsTargets"][0]
    assert data["pingStatus"] == "UNKNOWN" and data["discoveredGeneration"] is None
    assert json.loads(store.discovery.read_bytes()) == []



class ShortThreadLock:
    """Use a real thread lock with a shorter test-only acquisition deadline."""
    def __init__(self):
        self.lock = threading.Lock()

    def acquire(self, timeout):
        return self.lock.acquire(timeout=min(timeout, .05))

    def release(self):
        self.lock.release()


@pytest.fixture
def refresh_busy(store, monkeypatch, request):
    from contextlib import contextmanager

    mode = request.param if hasattr(request, "param") else "thread"
    if mode == "thread":
        lock = ShortThreadLock()
        monkeypatch.setattr(store, "lock", lock)

    @contextmanager
    def hold():
        if mode == "thread":
            with lock.lock:
                yield
        else:
            from platform_api import custom_ping_store as storage
            fd = os.open(store.lockfile, os.O_RDWR)
            try:
                storage.fcntl.flock(fd, storage.fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)

    return hold


@pytest.mark.parametrize("refresh_busy", [
    "thread",
    pytest.param("flock", marks=pytest.mark.skipif(os.name == "nt", reason="POSIX flock")),
], indirect=True)
def test_refresh_lock_failure_revokes_old_up_before_slow_retry(store, clock, prom, api, monkeypatch, refresh_busy):
    from concurrent.futures import ThreadPoolExecutor

    _, request, _, _ = api
    ticks = [10.]
    store.dns.monotonic = lambda: ticks[0]
    prom[0].allowed = ""
    row = create(store)
    other = create(store, "other.example.com")
    store.change("POST", None, None, {**ITEM, "ip": "10.70.10.54"}, "admin", str(uuid4()), prom[0].check)
    for ip, ttl in (("1.1.1.1", 300), ("1.1.1.2", 900)):
        store.resolve_dns(store.dns_due()[0], Answer((ip,), ttl), prom[0].dns_policy())
    project_as_discovered(store, prom)
    metric_response(monkeypatch, prom, lambda: (clock[0] - .5, 1))

    def discover():
        status, body, _ = request("GET", "/discovery")
        assert status == 200
        return {item["id"]: item for item in body["dnsTargets"]}

    assert discover()[row["id"]]["pingStatus"] == "UNKNOWN"
    clock[0] += 241
    ticks[0] += 241
    assert discover()[row["id"]]["pingStatus"] == "UP"
    ticket = store.dns_due()[0]
    started, release = threading.Event(), threading.Event()

    class ResolverWithSlowRetry:
        calls = 0

        def resolve(self, name):
            assert name == row["address"]
            self.calls += 1
            if self.calls == 1:
                return Answer(error="nxdomain")
            started.set()
            if not release.wait(5):
                return Answer(error="timeout")
            return Answer(("1.1.1.3",), 300)

    worker = Refresh(store, prom[0].dns_policy, ResolverWithSlowRetry())
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        with refresh_busy():
            failed = pool.submit(worker._resolve, ticket)
            with pytest.raises(PingError, match="custom_ping_busy"):
                failed.result(2)
        # DNS work at t+31 is in flight while the old t+300 TTL is still valid.
        ticks[0] += 31
        clock[0] += 31
        worker.tick()
        assert started.wait(2)
        data = discover()
        assert data[row["id"]]["pingStatus"] == "UNKNOWN"
        assert data[row["id"]]["withdrawalPending"]
        assert data[other["id"]]["pingStatus"] == "UP"
        store.reconcile()
        assert {entry["targets"][0] for entry in json.loads(store.discovery.read_bytes())} == {
            "1.1.1.2", "10.70.10.54",
        }
        release.set()
        worker.pending[row["id"]].result(5)
        assert store.list_targets()["targets"][0]["resolvedIp"] == "1.1.1.3"
        project_as_discovered(store, prom)
        assert discover()[row["id"]]["pingStatus"] == "UNKNOWN"
        clock[0] += 2
        assert discover()[row["id"]]["pingStatus"] == "UP"
    finally:
        release.set()
        worker.stop()
        pool.shutdown()


def test_refresh_fairness_for_100_with_slow_inventory(store, clock, capsys):
    targets = [create(store, f"g{i}.example.com") for i in range(100)]
    ticks = [10.]
    store.dns.monotonic = lambda: ticks[0]
    barrier = threading.Condition()
    joined = []
    calls = []
    step = [0]
    peak = [0]

    class ResolverWithShortTTL:
        def resolve(self, name):
            (label, _) = name.split(".", 1)
            value = int(label[1:])
            calls.append((name, step[0]))
            return Answer((f"1.1.2.{value + 1}",), 15)

    def slow_inventory():
        with barrier:
            current = step[0]
            joined.append(current)
            peak[0] = max(peak[0], len(joined))
            barrier.notify_all()
            if not barrier.wait_for(lambda: step[0] > current, timeout=10):
                raise AssertionError("batch never released")
        return lambda _: None

    worker = Refresh(store, slow_inventory, ResolverWithShortTTL())
    try:
        for wave in range(43):
            if wave == 14:
                assert len({name for name, _ in calls}) == len(targets)
            if wave == 28:
                assert len({name for name, turn in calls if 14 <= turn < 28}) == len(targets)
            worker.tick()
            batch = list(worker.pending.values())
            assert len(batch) == 8
            with barrier:
                assert barrier.wait_for(lambda: len(joined) == len(batch), timeout=10)
                if wave == 28:
                    rows = {row["id"]: row for row in store.list_targets()["targets"]}
                    editing, disabling, deleting = [rows[identity] for identity in list(worker.pending)[:3]]
                    edited = patch(store, editing, {"kind": "dns", "address": "g101.example.com"})[1]["target"]
                    patch(store, disabling, {"enabled": False})
                    store.change("DELETE", deleting["id"], deleting["revision"], {},
                                 "admin", str(uuid4()), lambda _: None)
                    added = create(store, "g100.example.com")
                joined.clear()
                clock[0] += 6
                ticks[0] += 6
                step[0] += 1
                barrier.notify_all()
            for future in batch:
                future.result(10)
        assert peak[0] == 8
        for wave in range(43):
            names = [name for name, turn in calls if turn == wave]
            assert len(names) == len(set(names)) == 8
        expected = {row["address"] for row in targets}
        for removed in (editing, disabling, deleting):
            expected.remove(removed["address"])
        expected.update((edited["address"], added["address"]))
        assert {name for name, wave in calls if wave > 28} == expected
        for row in (edited, added):
            assert store.resolution_history(row["id"])["segments"][-1]["address"] == row["address"]
        with capsys.disabled():
            print(f"PING_DNS_REFRESH coverage=100/100 first_window_waves=14 second_window_waves=14 "
                  f"peak={peak[0]} one_in_flight_per_id=PASS fixture_ttl_seconds=15")
    finally:
        with barrier:
            step[0] += 1
            barrier.notify_all()
        worker.stop()



@pytest.mark.parametrize("change", ["edit", "disable", "delete", "new_resolution"])
def test_failed_old_ticket_is_ignored_after_concurrent_update(store, clock, refresh_busy, change):
    from concurrent.futures import ThreadPoolExecutor

    row = create(store)
    refresh(store)
    clock[0] += 241
    old_ticket = store.dns_due()[0]
    completed, release = threading.Event(), threading.Event()

    class ResolverWithOldAnswer:
        def resolve(self, name):
            return Answer(("169.254.169.254",), 300)

    def wait_for_inventory():
        completed.set()
        if not release.wait(5):
            raise AssertionError("test did not release inventory")
        return lambda _: None

    worker = Refresh(store, wait_for_inventory, ResolverWithOldAnswer())
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        task = pool.submit(worker._resolve, old_ticket)
        assert completed.wait(2)
        if change == "edit":
            patch(store, row, {"kind": "dns", "address": "renamed.example.com"})
        elif change == "disable":
            patch(store, row, {"enabled": False})
        elif change == "delete":
            store.change("DELETE", row["id"], row["revision"], {}, "admin", str(uuid4()), lambda _: None)
        if change in {"edit", "new_resolution"}:
            # The old worker loses its lock while an already committed replacement is live.
            assert store.resolve_dns(store.dns_due()[0], Answer(("1.1.1.2",), 300), lambda _: None)
        before = store.list_targets()
        projected = store.discovery.read_bytes()
        with refresh_busy():
            release.set()
            with pytest.raises(PingError, match="custom_ping_busy"):
                task.result(2)
        assert store.list_targets() == before
        store.reconcile()
        assert store.discovery.read_bytes() == projected
        assert [item["targets"][0] for item in json.loads(projected)] == (
            ["1.1.1.2"] if change in {"edit", "new_resolution"} else [])
    finally:
        release.set()
        worker.stop()
        pool.shutdown()


@pytest.mark.parametrize("failure", ["lock", "runtime_fsync"])
def test_reconcile_can_withdraw_with_failed_state_fsync_and_recovered_discovery(
        store, clock, prom, api, monkeypatch, refresh_busy, failure):
    from platform_api import custom_ping_store as storage

    _, request, _, _ = api
    ticks = [10.]
    store.dns.monotonic = lambda: ticks[0]
    row = create(store)
    active = create(store, "active.example.com")
    waiting = create(store, "waiting.example.com")
    prom[0].allowed = ""
    store.change("POST", None, None, {**ITEM, "ip": "10.70.10.54"}, "admin", str(uuid4()), prom[0].check)
    for ip, ttl in (("1.1.1.1", 300), ("1.1.1.2", 900)):
        assert store.resolve_dns(store.dns_due()[0], Answer((ip,), ttl), prom[0].dns_policy())
    project_as_discovered(store, prom)
    metric_response(monkeypatch, prom, lambda: (clock[0] - .5, 1))
    assert request("GET", "/discovery")[1]["dnsTargets"][0]["pingStatus"] == "UNKNOWN"
    clock[0] += 241
    ticks[0] += 241
    assert request("GET", "/discovery")[1]["dnsTargets"][0]["pingStatus"] == "UP"
    replace = os.replace
    sync = storage.sync_directory
    discovery_failed = [True]
    state_failed = [False]

    def fail_replace(src, dst):
        if dst == store.discovery and discovery_failed[0]:
            raise OSError(errno.ENOSPC, "fixture discovery volume full")
        return replace(src, dst)

    def fail_fsync(directory):
        if directory == store.manifest.parent and state_failed[0]:
            raise OSError(errno.EIO, "fixture state directory fsync failed")
        return sync(directory)

    monkeypatch.setattr(os, "replace", fail_replace)
    monkeypatch.setattr(storage, "sync_directory", fail_fsync)
    monkeypatch.setattr(runtime, "sync_directory", fail_fsync)
    tickets = {t["id"]: t for t in store.dns_due()}
    with pytest.raises(PingError, match="publication_failed"):
        store.resolve_dns(tickets[waiting["id"]], Answer(("1.1.1.3",), 300), prom[0].dns_policy())
    state_failed[0] = True
    if failure == "lock":
        with refresh_busy(), pytest.raises(PingError, match="custom_ping_busy"):
            store.resolve_dns(tickets[row["id"]], Answer(error="nxdomain"), prom[0].dns_policy())
    else:
        with pytest.raises(PingError, match="dns_commit_failed"):
            store.resolve_dns(tickets[row["id"]], Answer(("1.1.1.4",), 300), prom[0].dns_policy())
    with pytest.raises(PingError, match="publication_failed"):
        store.reconcile()
    result = request("GET", "/discovery")[1]
    dns = {item["id"]: item for item in result["dnsTargets"]}
    assert dns[row["id"]]["pingStatus"] == "UNKNOWN" and dns[row["id"]]["withdrawalPending"]
    assert dns[active["id"]]["pingStatus"] == "UP"
    assert request("GET")[1]["publication"] == "pending"

    def published_ips():
        return {item["targets"][0] for item in json.loads(store.discovery.read_bytes())}

    assert published_ips() == {"1.1.1.1", "1.1.1.2", "10.70.10.54"}
    discovery_failed[0] = False
    with pytest.raises(PingError, match="publication_failed"):
        store.reconcile()
    # A durable but pending addition and a failed-fsync replacement are
    # both absent from the removal-only write.
    assert published_ips() == {"1.1.1.2", "10.70.10.54"}
    project_as_discovered(store, prom)
    assert not request("GET", "/discovery")[1]["dnsTargets"][0]["withdrawalPending"]

    restarted = PingStore(store.manifest.parent, store.discovery)
    restarted.dns.clock = store.dns.clock
    assert restarted.list_targets()["targets"][0]["dnsStatus"] != "DNS_OK"
    with pytest.raises(PingError, match="publication_failed"):
        restarted.reconcile()
    assert published_ips() == {"10.70.10.54"}
    state_failed[0] = False
    restarted.reconcile()
    recovery = next(t for t in restarted.dns_due() if t["id"] == row["id"])
    assert restarted.resolve_dns(recovery, Answer(("1.1.1.5",), 300), prom[0].dns_policy())
    assert published_ips() == {"1.1.1.5", "10.70.10.54"}



def test_failed_admin_commit_does_not_discard_lock_failure_for_live_ticket(
        store, clock, monkeypatch, refresh_busy):
    from concurrent.futures import ThreadPoolExecutor

    row = create(store)
    refresh(store)
    clock[0] += 241
    ticket = store.dns_due()[0]
    commit = store._commit
    replace = os.replace

    def fail_manifest(src, dst):
        if dst == store.manifest:
            raise OSError(errno.ENOSPC, "fixture manifest write failed")
        return replace(src, dst)

    def commit_while_dns_worker_times_out(candidate, previous):
        with ThreadPoolExecutor(max_workers=1) as pool:
            task = pool.submit(store.resolve_dns, ticket, Answer(error="nxdomain"), lambda _: None)
            with pytest.raises(PingError, match="custom_ping_busy"):
                task.result(2)
        return commit(candidate, previous)

    monkeypatch.setattr(os, "replace", fail_manifest)
    monkeypatch.setattr(store, "_commit", commit_while_dns_worker_times_out)
    with pytest.raises(PingError, match="custom_ping_commit_failed"):
        patch(store, row, {"kind": "dns", "address": "never-committed.example.com"})
    catalog = store.list_targets()
    assert catalog["targets"][0]["address"] == row["address"]
    assert catalog["targets"][0]["dnsStatus"] == "DNS_ERROR"
    assert catalog["publication"] == "pending"
    store.reconcile()
    assert json.loads(store.discovery.read_bytes()) == []
