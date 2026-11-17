"""D2 的 DNS、配置兼容、并发发布与历史外部行为回归。"""
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
    assert again["sampleNotBefore"] > a["sampleNotBefore"]
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
    assert original == (store.dns.path.read_bytes(), store.discovery.read_bytes(), store.manifest.read_bytes())


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
    monkeypatch.setattr(custom_ping.time, "time", lambda: now)
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
    evidence(visible["sampleNotBefore"] - 1, 1)
    assert prom[0].observed(snap)["dnsTargets"][0]["pingStatus"] == "UNKNOWN"
    evidence(visible["sampleNotBefore"] + 1, 1)
    assert prom[0].observed(snap)["dnsTargets"][0]["pingStatus"] == "UP"
    evidence(visible["sampleNotBefore"] + 1, 0)
    assert prom[0].observed(snap)["dnsTargets"][0]["pingStatus"] == "DOWN"
    prom[1][-1]["discoveredLabels"]["__meta_custom_ping_resolution_generation"] = "99"
    assert prom[0].observed(snap)["dnsTargets"][0]["pingStatus"] == "UNKNOWN"


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
    state = prom[0].observed(store.list_targets())
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
def test_dns_real_prometheus_blackbox_generation_and_history(tmp_path, monkeypatch, dns_server):
    from .test_custom_ping_prometheus import IMAGE, ROOT, SD_DIR, docker, wait_for
    from .test_custom_ping_frontend_integration import BLACKBOX, prometheus_fetch, query

    resolver, dns_config = dns_server
    work = tmp_path / "fixture"
    work.mkdir(mode=0o755)
    work.chmod(0o755)
    private = tmp_path / "private"
    private.mkdir()
    discovery = tmp_path / "sd" / "custom_targets.json"
    local = PingStore(private, discovery)
    local.reconcile()
    fixture = f"{work}:/fixture"
    (work / "blackbox.yml").write_text(
        "modules:\n  icmp:\n    prober: icmp\n    timeout: 1s\n    icmp:\n      preferred_ip_protocol: ip4\n", encoding="utf-8")
    generator = (ROOT / "prometheus-gen-config.sh").read_text(encoding="utf-8")
    (work / "generate.sh").write_text(generator.split("exec /bin/prometheus", 1)[0], encoding="utf-8")
    docker("run", "--rm", "--network", "none", "--user", "0", "-v", fixture, "--entrypoint", "/bin/sh",
           "-e", "PROMETHEUS_CONFIG_FILE=/fixture/prometheus.yml", IMAGE, "/fixture/generate.sh", timeout=120)
    prefix = "dns-ci-" + uuid4().hex[:12]
    net, prom_name, bb, peer = prefix + "-net", prefix + "-prom", prefix + "-bb", prefix + "-peer"
    a, b = "172.31.248.21", "172.31.248.22"
    dns_config["ips"] = [a]
    def prom_open(url, timeout):
        try:
            return io.BytesIO(prometheus_fetch(prom_name, url.removeprefix("http://prom:9090")).encode())
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
               "-v", fixture + ":ro", "-v", f"{discovery.parent}:{SD_DIR}:ro", IMAGE,
               "--config.file=/fixture/prometheus.yml")
        wait_for(lambda: prometheus_fetch(prom_name, "/api/v1/scrape_pools"), lambda raw: "custom-ping" in raw)
        created = create(local, private=["172.31.248.0/24"])
        assert inventory.observed(local.list_targets())["dnsTargets"][0]["pingStatus"] == "UNKNOWN"
        for address in (a, b, a):
            dns_config["ips"] = [address]
            answer = resolver.resolve("game.example.com")
            assert answer.ips == (address,)
            assert local.resolve_dns(ticket(), answer, inventory.dns_policy())
            pending = inventory.observed(local.list_targets())["dnsTargets"][0]
            assert pending["pingStatus"] == "UNKNOWN"
            ready = wait_for(lambda: inventory.observed(local.list_targets()),
                             lambda info: info["dnsTargets"][0]["pingStatus"] == "UP")
            assert ready["dnsTargets"][0]["resolvedIp"] == address
            assert ready["dnsTargets"][0]["discoveredGeneration"] == ready["dnsTargets"][0]["resolutionGeneration"]
        metrics = wait_for(lambda: query(prom_name, 'count by (target_ip) (count_over_time(probe_success{job="custom-ping"}[5m]))'),
                           lambda result: len(result) == 2)
        assert {r["metric"]["target_ip"] for r in metrics} == {a, b}
        dns_config["mode"] = "nxdomain"
        local.resolve_dns(ticket(), resolver.resolve("game.example.com"), inventory.dns_policy())
        assert inventory.observed(local.list_targets())["dnsTargets"][0]["pingStatus"] == "UNKNOWN"
        wait_for(lambda: inventory.observed(local.list_targets()), lambda info: info["activeTargets"] == [])
        assert [s["ip"] for s in local.resolution_history(created["id"])["segments"]] == [a, b, a]
    finally:
        docker("rm", "-f", "-v", prom_name, bb, peer, check=False)
        docker("network", "rm", net, check=False)

