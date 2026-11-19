"""Restore a cold synthetic custom-ping archive into fresh Linux Docker volumes."""
import hashlib
import io
import json
import shutil
import subprocess
from stat import S_IMODE
from time import time
from uuid import uuid4

from platform_api import custom_ping
from platform_api.custom_ping_store import PingStore

from .test_custom_ping_frontend_integration import prometheus_fetch, query
from .test_custom_ping_prometheus import IMAGE, SD_DIR, docker, wait_for


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def restore_cold_archive(
    tmp_path, monkeypatch, source, prom_name, net, fixture, inventory, resolver, dns_config, dns_row, ips,
):
    """Reuse the running DNS test fixture, then switch to independent restored volumes."""
    a, b = ips
    suffix = uuid4().hex[:12]
    destination = "ping-restore-" + suffix
    fixed_body = {"ip": b, "name": "Fixed fixture", "group": "Remote"}
    receipt_key = str(uuid4())
    status, fixed = source.change("POST", None, None, fixed_body, "fixture", receipt_key, inventory.check)
    assert status == 201
    fixed_id = fixed["target"]["id"]
    dns_id = dns_row["id"]
    dns_config.update(mode="a", ips=[a], ttl=300)
    intent, _ = source._read()
    item = next(row for row in intent["targets"] if row["id"] == dns_id)
    saved = source.dns.read(required=True)["rows"][dns_id]
    ticket = {**item, "_runtimeDigest": source._dns_digest(item, saved)}
    assert source.resolve_dns(ticket, resolver.resolve(item["address"]), inventory.dns_policy())
    collector = tmp_path / "collector" / "custom_targets.json"
    temporary = collector.with_suffix(".restore-fixture")
    temporary.write_bytes(source.discovery.read_bytes())
    temporary.chmod(0o644)
    temporary.replace(collector)

    def observed(store):
        return inventory.observed(store.list_targets(), store)

    def dns(store):
        return observed(store)["dnsTargets"][0]

    confirmation = wait_for(lambda: dns(source), lambda row: row["pingStatus"] == "UP")
    original = source.list_targets()
    original_history = source.resolution_history(dns_id)
    # Freeze an older evaluation point so a scrape still in flight at shutdown
    # cannot add a sample to the very interval being compared.
    last_point = time() - 3
    historic_query = 'count by (custom_id,target_ip) (count_over_time(probe_success{job="custom-ping"}[5m]))'

    def historic_rows(name):
        return sorted(query(name, historic_query, last_point),
                      key=lambda row: (row["metric"]["custom_id"], row["metric"]["target_ip"]))

    history_rows = historic_rows(prom_name)
    assert {row["metric"]["target_ip"] for row in history_rows if row["metric"]["custom_id"] == dns_id} == {a, b}
    docker("stop", "-t", "15", prom_name, timeout=25)
    container = json.loads(docker("inspect", prom_name).stdout)[0]
    assert container["State"]["ExitCode"] == 0
    if "Health" not in container["State"]:
        guarded = docker("inspect", "--format", "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
                         prom_name, check=False)
        print(f"PING_DOCKER_OPTIONAL_HEALTH guarded_go_template_exit={guarded.returncode} "
              f"missing_key={'map has no entry' in guarded.stderr}")
    tsdb = next(mount["Name"] for mount in container["Mounts"] if mount["Destination"] == "/prometheus")

    backup = tmp_path / "private-backup"
    backup.mkdir(mode=0o700)
    config = backup / ".env"
    config.touch(mode=0o600)
    config.write_text("PLATFORM_CUSTOM_PING_DNS_ENABLED=true\n", encoding="ascii")
    sources = {"platform-state": source.manifest.parent, "custom-ping-discovery": source.discovery.parent,
               "prometheus-data": tsdb}
    archived = [config]
    volumes = []
    restored_paths = {}
    backup_mount = f"{backup}:/backup"

    def archive_command(mount, mode, volume, filename):
        docker(
            "run", "--rm", "--network", "none", "--user", "0", "--entrypoint", "/bin/tar",
            "-v", mount, "-v", volume, IMAGE, mode, "/backup/" + filename, "-C", "/data", ".",
        )

    try:
        for name, source_volume in sources.items():
            filename = name + ".tar"
            path = backup / filename
            # Create the archive as the test runner before root writes through the bind mount.
            path.touch(mode=0o600)
            archive_command(backup_mount, "-cpf", f"{source_volume}:/data:ro", filename)
            assert path.stat().st_size > 0
            docker(
                "run", "--rm", "--network", "none", "--user", "0", "--entrypoint", "/bin/tar",
                "-v", backup_mount + ":ro", IMAGE, "-tf", "/backup/" + filename,
            )
            archived.append(path)

        sums = backup / "SHA256SUMS"
        sums.touch(mode=0o600)
        sums.write_text("".join(f"{digest(path)}  {path.name}\n" for path in archived), encoding="ascii")
        for entry in sums.read_text(encoding="ascii").splitlines():
            expected, filename = entry.split("  ", 1)
            assert digest(backup / filename) == expected
        assert S_IMODE(backup.stat().st_mode) == 0o700
        assert all(S_IMODE(path.stat().st_mode) == 0o600 for path in [*archived, sums])

        for name in sources:
            volume = destination + "-" + name
            options = []
            if name != "prometheus-data":
                path = tmp_path / volume
                path.mkdir(mode=0o755 if name == "custom-ping-discovery" else 0o700)
                restored_paths[name] = path
                options = ["--opt", "type=none", "--opt", "o=bind", "--opt", "device=" + str(path)]
            docker("volume", "create", *options, volume)
            volumes.append(volume)
            archive_command(backup_mount + ":ro", "-xpf", f"{volume}:/data", name + ".tar")
        restored_config_dir = tmp_path / "restored-private-config"
        restored_config_dir.mkdir(mode=0o700)
        restored_config = restored_config_dir / ".env"
        shutil.copy2(config, restored_config)
        assert digest(restored_config) == digest(config) and S_IMODE(restored_config.stat().st_mode) == 0o600
        state = restored_paths["platform-state"]
        discovery = restored_paths["custom-ping-discovery"] / source.discovery.name
        paused = PingStore(state, discovery)
        assert json.loads(paused.manifest.read_bytes())["schema"] == 2
        assert paused.manifest.read_bytes() == source.manifest.read_bytes()
        assert paused.backup.read_bytes() == source.backup.read_bytes()
        assert paused.dns.path.read_bytes() == source.dns.path.read_bytes()
        assert paused.discovery.read_bytes() == source.discovery.read_bytes()
        # The restored process has no live DNS lease. Reading history can close
        # the previously open segment without changing any archived bytes.
        restored_history = paused.resolution_history(dns_id)
        last = restored_history["segments"][-1]
        assert original_history["segments"][-1]["end"] is None
        assert last["sampleNotBefore"] <= last["end"] <= time()
        last["end"] = None
        assert restored_history == original_history

        paused.dns_enabled = False
        paused.reconcile()
        assert paused.list_targets()["dnsEnabled"] is False and paused.dns_due() == []
        status, replay = paused.change("POST", None, None, fixed_body, "fixture", receipt_key, inventory.check)
        assert status == 201 and replay["replayed"] and replay["target"]["id"] == fixed_id
        assert paused.manifest.read_bytes() == source.manifest.read_bytes()
        assert paused.list_targets()["revision"] == original["revision"]

        pause_started = time()
        docker(
            "run", "-d", "--name", destination, "--user", "0", "--network", net,
            "-v", fixture + ":ro", "-v", f"{destination}-custom-ping-discovery:{SD_DIR}:ro",
            "-v", f"{destination}-prometheus-data:/prometheus", IMAGE,
            "--config.file=/fixture/prometheus.yml", "--storage.tsdb.path=/prometheus",
        )

        def restored_inventory(url, timeout):
            try:
                endpoint = url.removeprefix("http://prom:9090")
                return io.BytesIO(prometheus_fetch(destination, endpoint).encode())
            except subprocess.CalledProcessError as error:
                raise OSError("restored Prometheus unavailable") from error

        with monkeypatch.context() as restored:
            restored.setattr(custom_ping, "urlopen", restored_inventory)
            wait_for(lambda: prometheus_fetch(destination, "/-/ready"), bool)
            assert historic_rows(destination) == history_rows
            observed_fixed = wait_for(
                lambda: observed(paused), lambda found: found["discoveredRevision"] == original["revision"],
            )
            assert {target["id"] for target in observed_fixed["activeTargets"]} == {fixed_id}
            assert dns(paused)["pingStatus"] == "UNKNOWN"
            fixed_sample = f'timestamp(probe_success{{custom_id="{fixed_id}"}} == 1)'
            wait_for(lambda: query(destination, fixed_sample),
                     lambda rows: len(rows) == 1 and float(rows[0]["value"][1]) > pause_started)

            active = PingStore(state, discovery)
            active.reconcile()
            assert dns(active)["pingStatus"] == "UNKNOWN"
            ticket, = active.dns_due()
            assert ticket["id"] == dns_id
            assert active.resolve_dns(ticket, resolver.resolve(ticket["address"]), inventory.dns_policy())
            assert dns(active)["pingStatus"] == "UNKNOWN"
            ready = wait_for(lambda: dns(active), lambda row: row["pingStatus"] == "UP")
            assert ready["resolutionGeneration"] > confirmation["resolutionGeneration"]
            assert ready["discoveredGeneration"] == ready["resolutionGeneration"]
            assert ready["sampleNotBefore"] > pause_started and ready["resolvedIp"] == a
            assert active.list_targets()["revision"] == original["revision"]
            assert historic_rows(destination) == history_rows
            print("PING_SCHEMA2_DOCKER_RESTORE sections=4 sha256=4 permissions=PASS history=PASS pause_resume=PASS")
            authority = tmp_path / "fixture" / "authority.json"
            staged = authority.with_suffix(".tmp")
            staged.write_text(json.dumps([{"targets": [a], "labels": {"display_name": "new authority"}}]), encoding="ascii")
            staged.chmod(0o644)
            staged.replace(authority)
            wait_for(inventory.active, lambda rows: any(
                row["scrapePool"] == "infra-dist-ping" and row["labels"]["target_ip"] == a for row in rows
            ))
            manifest, _ = active._read()
            item = next(row for row in manifest["targets"] if row["id"] == dns_id)
            saved = active.dns.read(required=True)["rows"][dns_id]
            ticket = {**item, "_runtimeDigest": active._dns_digest(item, saved)}
            assert active.resolve_dns(ticket, resolver.resolve(item["address"]), inventory.dns_policy())
            blocked = wait_for(lambda: dns(active), lambda row: not row["withdrawalPending"])
            assert blocked["dnsStatus"] == "DNS_POLICY_BLOCKED" and blocked["pingStatus"] == "UNKNOWN"

            dns_config["ips"] = ["172.31.248.23"]
            saved = active.dns.read(required=True)["rows"][dns_id]
            ticket = {**item, "_runtimeDigest": active._dns_digest(item, saved)}
            assert active.resolve_dns(ticket, resolver.resolve(item["address"]), inventory.dns_policy())
            down = wait_for(lambda: dns(active), lambda row: row["pingStatus"] == "DOWN")
            assert down["dnsStatus"] == "DNS_OK" and down["resolvedIp"] == "172.31.248.23"
            assert query(destination, fixed_sample)
            print("PING_DNS_DYNAMIC_AUTHORITY duplicate=BLOCKED fixed_ipv4=UP resolved_icmp=DOWN")

    finally:
        docker("rm", "-f", "-v", destination, check=False)
        if volumes:
            docker("volume", "rm", *volumes, check=False)
