"""B2 consumption of real B1 file_sd and ICMP samples in isolated Docker."""
import io
import json
import re
import subprocess
from time import monotonic, time
from urllib.parse import urlencode
from uuid import uuid4

from test_custom_ping_prometheus import IMAGE, ROOT, SD_DIR, docker, pytestmark, wait_for
from platform_api import custom_ping
from platform_api.custom_ping import TargetInventory
from platform_api.custom_ping_store import PingStore

BLACKBOX = "prom/blackbox-exporter:v0.25.0"
MODULES = """
const model = require("./librenms+grafana/bigscreen/ping/model.js");
const custom = require("./librenms+grafana/bigscreen/ping/custom-model.js");
const {createPingData} = require("./librenms+grafana/bigscreen/ping/data.js");
const fs = require("node:fs");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const settings = model.policy();
async function run() {
  const expressions = [];
  const ping = createPingData({settings, query: async (expression) => {
    expressions.push(expression);
    return input.samples || [];
  }});
  if (input.samples) {
    const samples = await ping.custom(input.at, new AbortController().signal);
    const targets = custom.present(input.catalog, input.discovery, samples, settings);
    console.log(JSON.stringify(targets.map(({ip,status,rtt}) => ({ip,status,rtt}))));
  } else {
    await ping.summary(input.at);
    await ping.custom(input.at);
    console.log(JSON.stringify(expressions));
  }
}
run().catch((error) => { console.error(error); process.exitCode = 1; });
"""


def frontend(data):
    result = subprocess.run(
        ["node", "-e", MODULES], cwd=ROOT.parent, input=json.dumps(data),
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def prometheus_fetch(container, endpoint):
    return docker("exec", container, "wget", "-qO-", "http://127.0.0.1:9090" + endpoint).stdout


def query(container, expression, at=None):
    params = {"query": expression}
    if at is not None:
        params["time"] = at
    result = json.loads(prometheus_fetch(container, "/api/v1/query?" + urlencode(params)))
    assert result["status"] == "success"
    return result["data"]["result"]


def test_frontend_uses_real_discovery_and_blackbox_icmp(tmp_path, monkeypatch):
    suffix = uuid4().hex[:12]
    net = "ping-b2-" + suffix
    prom = net + "-prom"
    bb = net + "-blackbox"
    sandbox = tmp_path / "fixture"
    sandbox.mkdir(mode=0o755)
    sandbox.chmod(0o755)
    discovery = tmp_path / "sd" / "custom_targets.json"
    store = PingStore(tmp_path, discovery)
    store.reconcile()
    sandbox.joinpath("blackbox.yml").write_text(
        "modules:\n  icmp:\n    prober: icmp\n    timeout: 1s\n    icmp:\n      preferred_ip_protocol: ip4\n", encoding="utf-8",
    )
    generator = ROOT.joinpath("prometheus-gen-config.sh").read_text(encoding="utf-8")
    sandbox.joinpath("generate.sh").write_text(generator.split("exec /bin/prometheus", 1)[0], encoding="utf-8")
    fixture = f"{sandbox}:/fixture"
    docker(
        "run", "--rm", "--network", "none", "--user", "0", "-v", fixture, "--entrypoint", "/bin/sh", "-e",
        "PROMETHEUS_CONFIG_FILE=/fixture/prometheus.yml", IMAGE, "/fixture/generate.sh", timeout=120,
    )

    def open_inventory(url, timeout):
        suffix = url.removeprefix("http://127.0.0.1:9090")
        try:
            return io.BytesIO(prometheus_fetch(prom, suffix).encode())
        except subprocess.CalledProcessError as error:
            raise OSError("isolated Prometheus unavailable") from error

    monkeypatch.setattr(custom_ping, "urlopen", open_inventory)
    inventory = TargetInventory("http://127.0.0.1:9090", "192.0.2.0/24", "", 2)
    docker("network", "create", "--internal", "--subnet", "192.0.2.0/24", net)
    try:
        reachable, unreachable = "192.0.2.21", "192.0.2.22"
        docker("run", "-d", "--name", bb, "--user", "0", "--network", net, "--ip", reachable, "--network-alias",
               "blackbox-exporter", "--cap-add", "NET_RAW", "-v", fixture + ":ro", BLACKBOX,
               "--config.file=/fixture/blackbox.yml", timeout=120)
        docker("run", "-d", "--name", prom, "--network", net, "--network-alias", "prometheus",
               "-v", fixture + ":ro", "-v", f"{discovery.parent}:{SD_DIR}:ro", IMAGE, "--config.file=/fixture/prometheus.yml")
        wait_for(lambda: prometheus_fetch(prom, "/api/v1/scrape_pools"), lambda data: "custom-ping" in data)
        for ip in (reachable, unreachable):
            assert store.change("POST", None, None, {"name": ip, "ip": ip, "group": "Other"},
                                "fixture", str(uuid4()), inventory.check)[0] == 201
        catalog = store.list_targets()
        found = wait_for(lambda: inventory.observed(catalog), lambda data: data["discoveredRevision"] == 2)
        wait_for(lambda: query(prom, 'probe_success{job="custom-ping"}'),
                 lambda data: {row["metric"]["target_ip"]: row["value"][1] for row in data}
                 == {reachable: "1", unreachable: "0"})
        at = time()
        _, expression = frontend({"at": at})
        samples = query(prom, expression, at)
        rendered = {target["ip"]: target for target in frontend({
            "at": at, "samples": samples, "catalog": catalog, "discovery": found,
        })}
        assert rendered[reachable]["status"] == "UP" and rendered[reachable]["rtt"] > 0
        assert rendered[unreachable]["status"] == "DOWN" and rendered[unreachable]["rtt"] is None

        docker("stop", "-t", "3", bb)
        wait_for(lambda: query(prom, 'up{job="custom-ping"}'),
                 lambda values: len(values) == 2 and all(row["value"][1] == "0" for row in values))
        at = time()
        rendered = frontend({"at": at, "samples": query(prom, expression, at), "catalog": catalog, "discovery": found})
        assert len(rendered) == 2 and all(row["status"] == "UNKNOWN" for row in rendered)
    finally:
        docker("rm", "-f", "-v", prom, bb, check=False)
        docker("network", "rm", net, check=False)


def preload_prometheus(work, count, custom_count, at):
    work.mkdir(mode=0o755)
    work.chmod(0o755)
    work.joinpath("tsdb").mkdir()
    work.joinpath("bench.yml").write_text("global:\n  scrape_interval: 10s\nscrape_configs: []\n", encoding="utf-8")
    with work.joinpath("series.om").open("w", encoding="utf-8", newline="\n") as output:
        for i in range(count):
            ip = f"192.0.{i // 250}.{i % 250 + 1}"
            if i < custom_count:
                identity = "abcd1234-1000-4000-8000-" + f"{i+1:012x}"
                labels = f'job="custom-ping",instance="{identity}",custom_id="{identity}",target_ip="{ip}"'
            else:
                labels = f'job="infra-dist-ping",instance="fixture-{i}",target_ip="{ip}"'
            for stamp in range(at - 60, at + 1, 2):
                output.write(f'up{{{labels}}} 1 {stamp}\nprobe_success{{{labels}}} 1 {stamp}\n')
                output.write(f'probe_icmp_duration_seconds{{{labels},phase="rtt"}} 0.012 {stamp}\n')
        output.write("# EOF\n")
    fixture = f"{work}:/fixture"
    docker("run", "--rm", "--network", "none", "--user", "0", "-v", fixture, "--entrypoint", "/bin/promtool", IMAGE,
           "tsdb", "create-blocks-from", "openmetrics", "/fixture/series.om", "/fixture/tsdb", timeout=60)
    return fixture


def test_prometheus_mixed_query_performance(tmp_path, capsys):
    """Report actual query timings on preloaded synthetic series, separate from live ICMP."""
    at = int(time()) - 90
    infra, custom = frontend({"at": at})
    results = []
    for size in (50, 100, 300):
        for variant, custom_count in (("baseline", 0), ("b2", size // 4)):
            name = "ping-b2-perf-" + uuid4().hex[:12]
            mount = preload_prometheus(tmp_path / name, size, custom_count, at)
            try:
                docker("run", "-d", "--name", name, "--network", "none", "--user", "0",
                       "-v", mount, IMAGE, "--config.file=/fixture/bench.yml", "--storage.tsdb.path=/fixture/tsdb")
                wait_for(lambda: prometheus_fetch(name, "/-/ready"), bool)

                def measures():
                    metrics = prometheus_fetch(name, "/metrics")
                    return {field: float(re.search(rf"(?m)^{field} ([\d.eE+-]+)$", metrics)[1])
                            for field in ("process_cpu_seconds_total", "process_resident_memory_bytes")}

                before = measures()
                elapsed = []
                for _ in range(10):
                    start = monotonic()
                    assert sum(row["metric"].get("ping_field") == "source" for row in query(name, infra, at)) == size - custom_count
                    if custom_count:
                        assert sum(row["metric"].get("ping_field") == "source" for row in query(name, custom, at)) == custom_count
                    elapsed.append((monotonic() - start) * 1000)
                after = measures()
                results.append({"size": size, "custom": custom_count, "variant": variant,
                                "docker_exec_e2e_p95_ms": round(max(elapsed), 2),
                                "prom_cpu_s_delta": round(after["process_cpu_seconds_total"] - before["process_cpu_seconds_total"], 3),
                                "prom_rss_bytes": int(after["process_resident_memory_bytes"])})
            finally:
                docker("rm", "-f", "-v", name, check=False)
    with capsys.disabled():
        print("PING_B2_PROM_PERF " + json.dumps(results, separators=(",", ":")))
