"""T-05.2：当前 AP 身份驱动有界 ICMP file_sd，不引入告警或 SNMP。"""
import json

import pytest
import yaml
import ap_topology as ap
from .test_ap_topology import identity
from .test_topology_server_librenms import gte
from .test_deployment_contracts import _render_prometheus_config, read


@pytest.mark.parametrize("count", [0, 1, 512])
def test_current_identity_ping_targets_atomic_replacement(tmp_path, monkeypatch, count):
    rows = [identity(n) for n in range(1, count + 1)]
    for n, row in enumerate(rows):
        row["metric"]["ip"] = f"10.1.{n // 254}.{n % 254 + 1}"
        row["metric"]["mac"] = f"02:00:00:00:{n // 256:02x}:{n % 256:02x}"
    aps = ap.current_ap_identities(rows)
    target = tmp_path / "ap-ping-targets.json"
    target.write_text('[{"targets":["192.0.2.99"]}]')
    monkeypatch.setattr(gte, "fetch_current_aps", lambda: aps)
    monkeypatch.setattr(gte, "load_device_list", lambda: [])
    monkeypatch.setattr(gte.subprocess, "run", lambda *a, **kw: pytest.fail("ping target publication must not probe SNMP"))
    monkeypatch.setenv("TOPOLOGY_OUTPUT_DIR", str(tmp_path))
    original = gte.write_json_atomic
    published = []
    def write(path, payload, **kwargs):
        if path.endswith("ap-ping-targets.json"):
            assert json.loads(target.read_text())[0]["targets"] == ["192.0.2.99"]
            published.append(payload)
        original(path, payload, **kwargs)
    monkeypatch.setattr(gte, "write_json_atomic", write)
    assert gte._run_collection() == 0
    result = json.loads(target.read_text())
    assert published == [result] and len(result) == count
    assert result == [{"targets": [item["ip"]], "labels": {"display_name": item["ip"]}} for item in aps]


def test_inventory_failure_clears_stale_ping_targets(tmp_path, monkeypatch):
    target = tmp_path / "ap-ping-targets.json"
    target.write_text('[{"targets":["192.0.2.99"]}]')
    monkeypatch.setattr(gte, "fetch_current_aps", lambda: (_ for _ in ()).throw(ValueError("fixture")))
    monkeypatch.setattr(gte, "load_device_list", lambda: [])
    monkeypatch.setenv("TOPOLOGY_OUTPUT_DIR", str(tmp_path))
    assert gte._run_collection() == 0
    assert json.loads(target.read_text()) == []


def test_ping_config_uses_only_file_sd_and_is_alert_isolated(tmp_path):
    config = yaml.safe_load(_render_prometheus_config(tmp_path, "true"))
    jobs = config["scrape_configs"]
    job = next(j for j in jobs if j["job_name"] == "infra-ap-ping")
    assert job["scrape_interval"] == "5s"
    assert job["params"] == {"module": ["icmp"]}
    assert job["static_configs"] == [{"targets": []}]
    assert job["file_sd_configs"][0]["files"] == ["/etc/prometheus/targets/topology/ap-ping-targets.json"]
    assert {"source_labels": ["__param_target"], "target_label": "target_ip"} in job["relabel_configs"]
    for name in ["docker-compose.yml", ".env.example"]:
        down_jobs = [line for line in read(name).splitlines() if "DEVICE_DOWN_JOBS" in line]
        assert down_jobs and all("infra-ap-ping" not in line for line in down_jobs)
