"""Run real file_sd and Prometheus checks in a container without network access."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from time import monotonic, sleep
from urllib.parse import urlencode
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from platform_api import custom_ping
from platform_api.custom_ping import TargetInventory
from platform_api.custom_ping_store import PingError, PingStore

IMAGE = "prom/prometheus:v2.53.1"
SD_DIR = "/etc/prometheus/targets/custom"
pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_CUSTOM_PING_PROMETHEUS") != "1" and os.environ.get("GITHUB_ACTIONS") != "true",
    reason="isolated Docker integration",
)


def docker(*args, check=True, timeout=25):
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True, timeout=timeout)


def wait_for(fetch, condition):
    deadline = monotonic() + 35
    observed = None
    while monotonic() < deadline:
        try:
            observed = fetch()
        except subprocess.CalledProcessError:
            sleep(0.2)
            continue
        if condition(observed):
            return observed
        sleep(0.2)
    pytest.fail(f"Prometheus did not converge. Last result: {observed}")


def test_compose_custom_discovery_is_shared_only_between_api_and_prometheus():
    compose = docker(
        "compose", "--env-file", str(ROOT / ".env.example"), "-f", str(ROOT / "docker-compose.yml"),
        "config", "--format", "json",
    )
    services = json.loads(compose.stdout)["services"]
    consumers = {
        service: (volume["target"], volume.get("read_only", False))
        for service, spec in services.items()
        for volume in spec.get("volumes", []) if volume.get("source") == "custom-ping-discovery"
    }
    assert consumers == {"platform-api": (SD_DIR, False), "prometheus": (SD_DIR, True)}
    assert all(v.get("source") != "platform-state" for v in services["prometheus"]["volumes"])


def test_prometheus_reads_atomic_updates_and_preserves_series(tmp_path, monkeypatch):
    sandbox = tmp_path / "fixture"
    sandbox.mkdir(mode=0o755)
    sandbox.chmod(0o755)
    state_dir = tmp_path / "private"
    state_dir.mkdir(mode=0o700)
    discovery_path = tmp_path / "sd" / "custom_targets.json"
    store = PingStore(state_dir, discovery_path)
    store.reconcile()
    assert discovery_path.read_text() == "[]\n"

    # Keep the generator unchanged up to its final exec. This lets promtool check
    # the exact config the production entrypoint would otherwise run immediately.
    source = (ROOT / "prometheus-gen-config.sh").read_text(encoding="utf-8")
    (sandbox / "generate.sh").write_text(source.split("exec /bin/prometheus", 1)[0], encoding="utf-8")
    (sandbox / "player_targets.json").write_text('[{"targets":["172.31.249.90"],"labels":{"network":"wired"}}]')
    fixture_mount = f"{sandbox}:/fixture"
    docker(
        "run", "--rm", "--network", "none", "--user", "0", "-v", fixture_mount, "--entrypoint", "/bin/sh",
        "-e", "PROMETHEUS_CONFIG_FILE=/fixture/prometheus.yml", "-e", "PLAYER_TARGETS_FILE=/fixture/player_targets.json",
        "-e", "CORE_SWITCH_PING=fixture:172.31.249.91", IMAGE, "/fixture/generate.sh", timeout=120,
    )

    mounts = ("-v", fixture_mount + ":ro", "-v", f"{discovery_path.parent}:{SD_DIR}:ro")
    docker(
        "run", "--rm", "--network", "none", *mounts, "--entrypoint", "/bin/promtool", IMAGE,
        "check", "config", "/fixture/prometheus.yml",
    )
    config_path = sandbox / "prometheus.yml"
    full_config = sandbox / "prometheus-full.yml"
    config_path.rename(full_config)
    without_custom, job, _ = full_config.read_text().partition('  - job_name: "custom-ping"')
    assert job
    config_path.write_text(without_custom)
    name = "custom-ping-ci-" + uuid4().hex

    def fetch(path):
        result = docker("exec", name, "wget", "-qO-", f"http://127.0.0.1:9090{path}")
        response = json.loads(result.stdout)
        assert response["status"] == "success"
        return response["data"]

    def active():
        return fetch("/api/v1/targets?state=active")["activeTargets"]

    def targets(job):
        return [row for row in active() if row["scrapePool"] == job]

    def query(expr):
        return fetch("/api/v1/query?" + urlencode({"query": expr}))["result"]

    def write(method, target_id=None, revision=None, changes=None):
        status, result = store.change(
            method, target_id, revision, changes or {}, "fixture-admin", str(uuid4()), inventory.check,
        )
        assert status in (200, 201) and result["publication"] == "published"
        return result["target"]


    def open_inventory(url, timeout):
        # Forward the Platform API request to the real Prometheus on its isolated loopback.
        try:
            result = docker("exec", name, "wget", "-qO-", url, timeout=timeout + 5)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise OSError("isolated Prometheus request failed") from exc
        return io.BytesIO(result.stdout.encode())

    monkeypatch.setattr(custom_ping, "urlopen", open_inventory)
    inventory = TargetInventory("http://127.0.0.1:9090", "172.31.249.0/24", "", 1)

    def observe():
        return inventory.observed(store.list_targets())

    try:
        docker(
            "run", "-d", "--name", name, "--network", "none", *mounts, IMAGE,
            "--config.file=/fixture/prometheus.yml", "--storage.tsdb.path=/prometheus",
        )
        wait_for(active, lambda rows: {"prometheus", "player-ping", "infra-core-ping"} <= {r["scrapePool"] for r in rows})
        missing = observe()
        assert missing["publishedRevision"] == 0 and missing["discoveredRevision"] is None
        assert missing["activeTargets"] == []
        assert "custom-ping" not in fetch("/api/v1/scrape_pools")["scrapePools"]

        full_config.replace(config_path)
        docker("kill", "--signal=HUP", name)
        initial = wait_for(observe, lambda state: state["discoveredRevision"] == 0)
        assert initial["publishedRevision"] == 0 and initial["activeTargets"] == []
        assert "custom-ping" in fetch("/api/v1/scrape_pools")["scrapePools"]
        assert targets("player-ping")[0]["labels"]["instance"] == "172.31.249.90"
        assert targets("infra-core-ping")[0]["labels"]["instance"] == "fixture"
        for reserved in ("172.31.249.90", "172.31.249.91"):
            with pytest.raises(PingError, match="duplicate_ip"):
                inventory.check(reserved)

        target = write("POST", changes={"name": "Remote", "ip": "172.31.249.21", "group": "Remote"})
        original = wait_for(lambda: targets("custom-ping"), bool)[0]
        target_id = target["id"]
        assert original["labels"] == {
            "job": "custom-ping", "custom_id": target_id, "instance": target_id, "target_ip": "172.31.249.21",
        }
        assert original["scrapeInterval"] == "2s"
        assert "module=icmp" in original["scrapeUrl"] and "target=172.31.249.21" in original["scrapeUrl"]
        assert "blackbox-exporter:9115/probe?" in original["scrapeUrl"]
        assert inventory.observed(store.list_targets())["discoveredRevision"] == 1

        target = write("PATCH", target_id, 1, {"name": "Renamed", "group": "Server"})
        renamed = wait_for(
            lambda: targets("custom-ping"),
            lambda rows: bool(rows) and rows[0]["discoveredLabels"].get("display_name") == "Renamed",
        )[0]
        assert renamed["labels"] == original["labels"]
        assert renamed["discoveredLabels"]["custom_group"] == "Server"
        assert inventory.observed(store.list_targets())["discoveredRevision"] == 2
        expr = 'count by (target_ip) (count_over_time(up{job="custom-ping"}[5m]))'
        wait_for(lambda: query(expr), bool)

        target = write("PATCH", target_id, 2, {"ip": "172.31.249.22"})
        moved = wait_for(lambda: targets("custom-ping"), lambda rows: bool(rows) and rows[0]["labels"]["target_ip"] == "172.31.249.22")
        assert moved[0]["labels"]["instance"] == target_id
        historic = wait_for(lambda: query(expr), lambda rows: len(rows) == 2)
        assert {row["metric"]["target_ip"] for row in historic} == {"172.31.249.21", "172.31.249.22"}

        write("PATCH", target_id, 3, {"enabled": False})
        disabled = wait_for(observe, lambda state: state["discoveredRevision"] == 4)
        assert disabled["activeTargets"] == []
        assert len(query(expr)) == 2
        write("PATCH", target_id, 4, {"enabled": True})
        wait_for(lambda: targets("custom-ping"), bool)

        result = docker("exec", "--user", "0", name, "/bin/sh", "-c",
                        'printf "[]" > /etc/prometheus/targets/custom/custom_targets.json', check=False)
        assert result.returncode != 0 and "Read-only file system" in result.stderr
        assert targets("custom-ping")[0]["labels"]["target_ip"] == "172.31.249.22"
        docker("restart", name)
        wait_for(lambda: targets("custom-ping"), bool)
        write("DELETE", target_id, 5)
        deleted = wait_for(observe, lambda state: state["discoveredRevision"] == 6)
        assert deleted["activeTargets"] == []
        broken = discovery_path.with_suffix(".tmp")
        broken.write_bytes(b"broken file_sd")
        broken.replace(discovery_path)
        dirty = observe()
        assert dirty["publishedRevision"] is None and dirty["discoveredRevision"] is None
        store.reconcile()
        assert observe()["discoveredRevision"] == 6
        assert len(query(expr)) == 2
        assert len(targets("player-ping")) == len(targets("infra-core-ping")) == 1
        docker("stop", "-t", "5", name)
        with pytest.raises(PingError, match="inventory_unavailable") as error:
            observe()
        assert error.value.status == 503
    finally:
        docker("rm", "-f", "-v", name, check=False)
