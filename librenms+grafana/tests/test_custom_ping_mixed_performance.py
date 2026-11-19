"""Profiles the existing Ping queries with preloaded samples and live lab ICMP."""
import json
import os
from pathlib import Path
import re
from statistics import mean
import subprocess
from time import monotonic, sleep, time
from uuid import uuid4

from .test_custom_ping_frontend_integration import BLACKBOX, prometheus_fetch, query, query_response
from .test_custom_ping_prometheus import IMAGE, ROOT, SD_DIR, docker, pytestmark, wait_for


CASES = [(50, 13), (100, 25), (300, 100)]
JOBS = [("infra-isp-ping", 1), ("infra-ap-ping", 5), ("infra-dist-ping", 2)]


def targets_for(total, custom):
    targets = []
    for i in range(total):
        addr = f"172.31.{246 + i // 250}.{i % 250 + 2}"
        if i < custom:
            uid = f"abcd1234-1000-4000-8000-{i + 1:012x}"
            targets.append(("custom-ping", 2, addr, uid, i % 2 == 0))
        else:
            job, interval = JOBS[min((i - custom) % 5, 2)]
            targets.append((job, interval, addr, f"fixture-{i}", False))
    return targets


def browser_queries(targets, at):
    script = """
const {createPingData} = require("./librenms+grafana/bigscreen/ping/data.js");
const model = require("./librenms+grafana/bigscreen/ping/model.js");
const {at, dns} = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
const requests = [];
const ping = createPingData({settings: model.policy(), query: async (expression) => {
  requests.push(expression);
  return [];
}});
(async () => {
  await ping.summary(at);
  await ping.custom(at, undefined, dns.map(id => ({metric: {custom_id: id}})));
  console.log(JSON.stringify(requests));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    data = {"at": at, "dns": [uid for _, _, _, uid, domain in targets if domain]}
    process = subprocess.run(["node", "-e", script], cwd=ROOT.parent, input=json.dumps(data),
                             capture_output=True, text=True, timeout=10, check=True)
    return json.loads(process.stdout)


def profile(container, targets, at, expect_preloaded):
    expressions = browser_queries(targets, at)
    assert len(expressions) == 3
    fixed = sum(job == "custom-ping" and not domain for job, _, _, _, domain in targets)
    dns = sum(domain for _, _, _, _, domain in targets)
    infra = len(targets) - fixed - dns

    def server_resources():
        body = prometheus_fetch(container, "/metrics")
        names = ("process_cpu_seconds_total", "process_resident_memory_bytes", "prometheus_tsdb_head_series")
        return {name: float(re.search(rf"(?m)^{name} ([\d.eE+-]+)$", body)[1]) for name in names}

    before = server_resources()
    server_ms = {name: [] for name in ("infra_summary", "custom_aggregate", "dns_raw")}
    dns_samples = []
    round_wall_ms = []
    labels = set()
    for _ in range(20):
        start = monotonic()
        readings = [query_response(container, expr, at, stats=True) for expr in expressions]
        round_wall_ms.append(1000 * (monotonic() - start))
        for name, data in zip(server_ms, readings, strict=True):
            server_ms[name].append(1000 * data["stats"]["timings"]["execTotalTime"])
        for data, expected in zip(readings[:2], (infra, fixed + dns), strict=True):
            assert sum(row["metric"].get("ping_field") == "source" for row in data["result"]) == expected
        raw = [row for row in readings[2]["result"] if row["metric"]["__name__"] == "probe_success"]
        assert len(raw) == dns
        assert all(all(sample[1] == "1" for sample in row["values"]) for row in raw)
        labels.update(row["metric"]["custom_id"] for row in raw)
        dns_samples = [len(row["values"]) for row in raw]
    assert len(labels) == dns
    if expect_preloaded:
        assert set(dns_samples) == {30}
    else:
        assert min(dns_samples) >= 26
    after = server_resources()

    def latency(values):
        return {key: round(value, 2) for key, value in
                zip(("avg", "p95", "max"), (mean(values), sorted(values)[18], max(values)), strict=True)}

    sizes = docker("exec", container, "du", "-sk", "/prometheus").stdout.split()
    host_ram = next(line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines()
                    if line.startswith("MemTotal:"))
    return {
        "total": len(targets), "infrastructure": infra, "fixed": fixed, "dns": dns,
        "probe_intervals": {str(n): sum(interval == n for _, interval, *_ in targets) for n in (1, 2, 5)},
        "query_errors": 0, "queries": 60, "source_coverage": [infra, fixed + dns, len(labels)],
        "dns_sample_coverage_60s": [min(dns_samples), max(dns_samples)],
        "prom_ms": {name: latency(times) for name, times in server_ms.items()},
        "docker_round_ms": latency(round_wall_ms),
        "prom_cpu_seconds_during_queries": round(after["process_cpu_seconds_total"] - before["process_cpu_seconds_total"], 3),
        "prom_rss_mib_before_after": [round(stats["process_resident_memory_bytes"] / (1024 ** 2), 2)
                                     for stats in (before, after)],
        "tsdb_head_series": int(after["prometheus_tsdb_head_series"]), "tsdb_kib": int(sizes[0]),
        "runner_cpu_threads": os.cpu_count(), "runner_ram_kib": int(host_ram),
    }


def bind_path(path):
    path.mkdir(mode=0o755)
    path.chmod(0o755)
    return path


def test_dns_mixed_preloaded_profile(tmp_path, capsys):
    """Use realistic sample spacing in preloaded blocks, with no actual scrapes."""
    rows = []
    for total, custom in CASES:
        targets = targets_for(total, custom)
        at = int(time()) - 90
        name = "ping-mixed-blocks-" + uuid4().hex[:12]
        work = bind_path(tmp_path / name)
        bind_path(work / "tsdb")
        config = work / "bench.yml"
        config.write_text("global:\n  scrape_interval: 10s\nscrape_configs: []\n", encoding="ascii")
        with (work / "series.om").open("w", encoding="ascii", newline="\n") as stream:
            for job, interval, ip, uid, _ in targets:
                labels = f'job="{job}",instance="{uid}",target_ip="{ip}"'
                if job == "custom-ping":
                    labels += f',custom_id="{uid}"'
                for stamp in range(at - 60 + interval, at + 1, interval):
                    stream.write(f'up{{{labels}}} 1 {stamp}\nprobe_success{{{labels}}} 1 {stamp}\n')
                    stream.write(f'probe_icmp_duration_seconds{{{labels},phase="rtt"}} 0.012 {stamp}\n')
            stream.write("# EOF\n")
        mount = f"{work}:/fixture"
        docker("run", "--rm", "--network", "none", "--user", "0", "-v", mount,
               "--entrypoint", "/bin/promtool", IMAGE,
               "tsdb", "create-blocks-from", "openmetrics", "/fixture/series.om", "/fixture/tsdb", timeout=60)
        try:
            docker("run", "-d", "--name", name, "--network", "none", "--user", "0", "-v", mount + ":ro",
                   "-v", f"{work / 'tsdb'}:/prometheus", IMAGE, "--config.file=/fixture/bench.yml",
                   "--storage.tsdb.path=/prometheus")
            wait_for(lambda: prometheus_fetch(name, "/-/ready"), bool)
            rows.append(profile(name, targets, at, expect_preloaded=True))
        finally:
            docker("rm", "-f", "-v", name, check=False)
    with capsys.disabled():
        print("PING_DNS_MIXED_PRELOADED " + json.dumps(rows, separators=(",", ":")))


def test_dns_mixed_real_icmp_profile(tmp_path, capsys):
    """Use the production scrape jobs on a private Docker network with real ICMP."""
    names = "ping-mixed-live-" + uuid4().hex[:12]
    net, exporter, receiver = (names + "-" + label for label in ("net", "blackbox", "peer"))
    suite = []
    work = bind_path(tmp_path / "live")
    (work / "blackbox.yml").write_text(
        "modules:\n  icmp:\n    prober: icmp\n    timeout: 1s\n    icmp:\n      preferred_ip_protocol: ip4\n",
        encoding="ascii",
    )
    generator = (ROOT / "prometheus-gen-config.sh").read_text(encoding="utf-8")
    (work / "generate.sh").write_text(generator.split("exec /bin/prometheus", 1)[0], encoding="utf-8")
    discovery = bind_path(work / "discovery")
    mount = f"{work}:/fixture"
    docker("network", "create", "--internal", "--subnet", "172.31.246.0/23", net)
    try:
        docker("run", "-d", "--name", exporter, "--user", "0", "--network", net, "--ip", "172.31.247.252",
               "--network-alias", "blackbox-exporter", "--cap-add", "NET_RAW", "-v", mount + ":ro",
               BLACKBOX, "--config.file=/fixture/blackbox.yml", timeout=120)
        docker("run", "-d", "--name", receiver, "--user", "0", "--network", net, "--ip", "172.31.247.253",
               "--cap-add", "NET_ADMIN", "--entrypoint", "/bin/sh", IMAGE, "-c", "sleep 900")
        addresses = [ip for _, _, ip, _, _ in targets_for(*CASES[-1])]
        docker("exec", receiver, "/bin/sh", "-ec", 'for addr do ip addr add "$addr/32" dev eth0; done',
               "fixture", *addresses)
        for total, custom in CASES:
            targets = targets_for(total, custom)
            ap = [{"targets": [ip], "labels": {"display_name": uid}}
                  for job, _, ip, uid, _ in targets if job == "infra-ap-ping"]
            custom_items = [{"targets": [ip], "labels": {"custom_id": uid}}
                            for job, _, ip, uid, _ in targets if job == "custom-ping"]
            for file, entries in ((work / "ap-ping-targets.json", ap),
                                  (discovery / "custom_targets.json", custom_items)):
                file.write_text(json.dumps(entries), encoding="ascii")
                file.chmod(0o644)
            env = []
            for var, job in (("ISP_PING", "infra-isp-ping"), ("DIST_SWITCH_PING", "infra-dist-ping")):
                env.extend(["-e", var + "=" + ",".join(f"{uid}:{ip}" for kind, _, ip, uid, _ in targets if kind == job)])
            docker("run", "--rm", "--network", "none", "--user", "0", "-v", mount, "--entrypoint", "/bin/sh",
                   "-e", "PROMETHEUS_CONFIG_FILE=/fixture/prometheus.yml", *env, IMAGE, "/fixture/generate.sh", timeout=120)
            prom = names + "-" + str(total)
            try:
                docker("run", "-d", "--name", prom, "--network", net, "--ip", "172.31.247.254", "-v", mount + ":ro",
                       "-v", mount.replace(":/fixture", ":/etc/prometheus/targets/topology:ro"),
                       "-v", f"{discovery}:{SD_DIR}:ro", IMAGE,
                       "--config.file=/fixture/prometheus.yml", "--storage.tsdb.path=/prometheus")
                wait_for(lambda: prometheus_fetch(prom, "/-/ready"), bool)
                selector = 'probe_success{job=~"infra-(isp|ap|dist)-ping|custom-ping"}'
                wait_for(lambda: query(prom, selector), lambda rows: len(rows) == total)
                # Fill an actual 60-second sample window at the configured 1/2/5s cadences.
                sleep(61)
                at = time()
                coverage = query(prom, f"count_over_time({selector}[60s])", at)
                assert len(coverage) == total
                by_ip = {row["metric"]["target_ip"]: int(row["value"][1]) for row in coverage}
                assert len(by_ip) == total
                assert all(by_ip[ip] >= 60 // interval - 2 for _, interval, ip, *_ in targets)
                successes = query(prom, selector, at)
                assert len(successes) == total and all(item["value"][1] == "1" for item in successes)
                result = profile(prom, targets, at, expect_preloaded=False)
                result["icmp_success_coverage"] = len(successes)
                result["icmp_samples_60s"] = sum(by_ip.values())
                suite.append(result)
            finally:
                docker("rm", "-f", "-v", prom, check=False)
    finally:
        docker("rm", "-f", "-v", exporter, receiver, check=False)
        docker("network", "rm", net, check=False)
    with capsys.disabled():
        print("PING_DNS_MIXED_LIVE " + json.dumps(suite, separators=(",", ":")))
