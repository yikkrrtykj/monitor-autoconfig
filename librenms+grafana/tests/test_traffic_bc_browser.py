"""Chromium and Platform API against an isolated Prometheus TSDB fixture."""
import os
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer
from uuid import uuid4

import pytest

from platform_api import main, traffic_bc as bc
from platform_config import dump_simple_yaml

from .test_custom_ping_prometheus import IMAGE, ROOT, docker, wait_for


def fixture_series(end):
    lines = []
    tunnel_rows, svi_rows = [], []
    for i in range(8):
        instance = f"192.0.2.{10 + i}"
        target_ip = f"192.0.2.{20 + i}"
        tunnel = f"tunnel{i + 1}"
        vlan = 20 + i
        tunnel_rows.append({"name": f"Remote OB {i + 1}", "instance": instance, "if_name": tunnel})
        svi_rows.append({"name": f"Production {vlan}", "target_ip": target_ip, "vlan": vlan})
        for job, target, name, index in (
            ("firewall-snmp", f'instance="{instance}"', tunnel, 59 + i),
            ("infra-switch-ifmib", f'target_ip="{target_ip}"', f"Vl{vlan}", 148 + i),
        ):
            source = f'job="{job}",{target}'
            iface = f'{source},ifName="{name}",ifIndex="{index}"'
            for sample in range(4):
                scrape_at = end - 93 + sample * 30
                # OB 2 has its last scrape missing, SVI 22 rolls over and
                # SVI 23 has two ifIndexes with the same name.
                if name == "tunnel2" and sample == 3:
                    continue
                received = 5_000_000 + sample * 3_750_000
                sent = 8_000_000 + sample * 7_500_000
                if name == "Vl20":
                    received, sent = 510_000_000_000, 11_000_000_000
                if name == "Vl22" and sample == 3:
                    received, sent = 5, 6
                lines.append(f"up{{{source}}} 1 {scrape_at}")
                fields = {"ifHCInOctets": received, "ifHCOutOctets": sent, "ifOperStatus": 1}
                if job == "infra-switch-ifmib":
                    fields["ifCounterDiscontinuityTime"] = 4 if name == "Vl22" and sample == 3 else 0
                indexes = [iface, iface.replace('ifIndex="151"', 'ifIndex="999"')] if name == "Vl23" else [iface]
                for labels in indexes:
                    lines.extend(f"{metric}{{{labels}}} {value} {scrape_at}" for metric, value in fields.items())
    def config(tunnels, vlans):
        return {"traffic": {"tunnels": tunnels, "business_vlans": vlans}}
    scenarios = {
        "unconfigured": config([], []),
        "normal": config(tunnel_rows[:1], svi_rows[:1]),
        "missing": config(tunnel_rows[1:2], svi_rows[:1]),
        "reset": config(tunnel_rows[:1], svi_rows[2:3]),
        "ambiguous": config(tunnel_rows[:1], svi_rows[3:4]),
        "maximum": config(tunnel_rows, svi_rows),
    }
    return "\n".join([*sorted(lines, key=lambda line: (line.split()[0], int(line.rsplit(" ", 1)[1]))),
                     "# EOF", ""]), scenarios


@pytest.mark.skipif(os.environ.get("RUN_TRAFFIC_BC_BROWSER") != "1", reason="isolated Linux Chromium job")
def test_live_traffic_bc_browser(tmp_path, monkeypatch):
    end = int(time.time() // bc.STEP) * bc.STEP
    metrics, scenarios = fixture_series(end)
    work = tmp_path / "fixture"
    work.mkdir()
    (work / "metrics.om").write_text(metrics, encoding="ascii")
    (work / "prometheus.yml").write_text("scrape_configs: []\n", encoding="ascii")
    config = tmp_path / "event.yml"
    current = work / "tsdb"
    current.mkdir()
    mount = ("-v", f"{work}:/fixture")
    docker("run", "--rm", "--network", "none", "--user", "0", *mount,
           "--entrypoint", "/bin/promtool", IMAGE, "tsdb", "create-blocks-from", "openmetrics",
           "/fixture/metrics.om", "/fixture/tsdb", timeout=90)
    name = "bc-ci-" + uuid4().hex[:12]

    def set_config(scenario):
        candidate = config.with_suffix(".tmp")
        candidate.write_text(dump_simple_yaml(scenarios[scenario]) + "\n", encoding="utf-8")
        candidate.replace(config)

    class Gateway(main.Handler):
        def do_GET(self):
            if self.path != "/platform-api/network/traffic-bc":
                self.send_error(404)
                return
            self.path = self.path.removeprefix("/platform-api")
            super().do_GET()

        def do_POST(self):
            case = self.path.removeprefix("/fixture/traffic/")
            if case not in scenarios:
                self.send_error(404)
                return
            set_config(case)
            self._send_json({"ok": True})

    calls = []
    try:
        docker("run", "-d", "--name", name, "--user", "0", "--publish", "127.0.0.1::9090", *mount, IMAGE,
               "--config.file=/fixture/prometheus.yml", "--storage.tsdb.path=/fixture/tsdb", timeout=60)
        wait_for(lambda: docker("exec", name, "wget", "-qO-", "http://127.0.0.1:9090/-/ready").stdout,
                 lambda body: "Ready" in body)
        port = int(docker("port", name, "9090/tcp").stdout.strip().removeprefix("127.0.0.1:"))

        def fetch(_url, query, at):
            started = time.monotonic()
            result = bc._query_range(f"http://127.0.0.1:{port}", query, at, 7)
            calls.append({"series": len(result), "elapsed_ms": round((time.monotonic() - started) * 1000, 1)})
            return result

        set_config("unconfigured")
        monkeypatch.setattr(main, "CONFIG_PATH", config)
        monkeypatch.setattr(main, "BC_SAMPLER", bc.SharedSampler(clock=lambda: end, fetch=fetch))
        with ThreadingHTTPServer(("127.0.0.1", 0), Gateway) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                env = {**os.environ, "BC_LIVE_ORIGIN": f"http://127.0.0.1:{server.server_port}"}
                result = subprocess.run(["node", str(ROOT / "tests" / "chromium_infra_overview.js")],
                                        cwd=ROOT.parent, env=env, capture_output=True, text=True, timeout=105)
                assert result.returncode == 0, result.stdout + result.stderr
                print(result.stdout, end="")
            finally:
                server.shutdown()
                thread.join(3)
        assert len(calls) == 6, calls
        print(f"B/C live Prometheus queries: {calls}")
    finally:
        docker("rm", "-fv", name, check=False)
