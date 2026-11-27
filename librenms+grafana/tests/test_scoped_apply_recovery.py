"""Run the recovery command against disposable Compose services and mounts."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from time import monotonic, sleep
from uuid import uuid4

import pytest


ROOT = Path(__file__).resolve().parents[1]
IMAGE = "alpine:3.22"
RESIDENTS = (
    "prometheus", "snmp-exporter", "blackbox-exporter", "rsyslog", "librenms",
    "librenms-dispatcher", "bigscreen", "grafana", "platform-api",
    "player-targets", "topology-collector", "alertmanager-feishu-bridge",
)


@pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("MONITOR_TEST_SCOPED_RECOVERY") != "1",
    reason="Requires an isolated Linux Docker daemon",
)
def test_recovery_restores_effective_config_without_recreating_unaffected_containers(tmp_path):
    project = tmp_path
    env = {key: value for key, value in os.environ.items() if not key.startswith("COMPOSE_")}
    env.update(
        COMPOSE_PROJECT_NAME=f"monitor-recovery-{uuid4().hex[:12]}",
        PLATFORM_API_SELF_APPLY="true",
        LIBRENMS_CONFIG_TIMEOUT="30",
        DEPLOY_CHECK_TIMEOUT="30",
    )

    def run(*command, check=True):
        result = subprocess.run(
            command, cwd=project, env=env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=120,
        )
        if check:
            assert result.returncode == 0, result.stdout + result.stderr
        return result

    def compose(*args):
        return run("docker", "compose", *args).stdout.strip()

    def container(service):
        return compose("--profile", "*", "ps", "-a", "-q", service)

    def health(container_id):
        return run("docker", "inspect", "--format", "{{.State.Health.Status}}", container_id).stdout.strip()

    def wait_health(container_id, expected):
        deadline = monotonic() + 15
        while monotonic() < deadline:
            observed = health(container_id)
            if observed == expected:
                return
            sleep(0.2)
        pytest.fail(f"Expected {expected} for {container_id}, observed {observed}")

    def replace_env(text):
        path = project / ".replacement"
        path.write_text(text, encoding="utf-8")
        path.replace(project / ".env")

    services = {name: {"image": IMAGE, "command": ["sleep", "600"]} for name in RESIDENTS}
    for name in ("prometheus", "grafana"):
        services[name]["command"] = ["sh", "-ec", "touch /tmp/ready; exec sleep 600"]
        services[name]["healthcheck"] = {
            "test": ["CMD", "test", "-f", "/tmp/ready"],
            "interval": "1s", "timeout": "1s", "retries": 1,
        }
    for name in ("player-targets", "topology-collector"):
        services[name]["volumes"] = ["./.env:/config/.env:ro"]
    services["bigscreen"]["environment"] = {"EVENT_NAME": "${EVENT_NAME}"}
    services["unpoller"] = {"image": IMAGE, "command": ["sleep", "600"], "profiles": ["unifi"]}
    services["feishu-ws"] = {"image": IMAGE, "command": ["sleep", "600"], "profiles": ["feishu"]}
    services["librenms-config"] = {
        "image": IMAGE,
        "command": ["sh", "-ec", 'test "$$TASK_MODE" = old'],
        "environment": {"TASK_MODE": "${TASK_MODE}"},
    }
    services["grafana-setup"] = {"image": IMAGE, "command": ["true"]}
    (project / "docker-compose.yml").write_text(json.dumps({"services": services}), encoding="utf-8")
    for file in ("apply-env.sh", "deployment-tasks.sh", "platform_config.py", "version_info.py"):
        shutil.copy2(ROOT / file, project / file)
    (project / "render-grafana-provisioning.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    check_script = project / "deploy-check.sh"
    check_script.write_text("#!/bin/sh\ntest \"$1\" = configured\n", encoding="utf-8")
    check_script.chmod(0o755)
    restored = "EVENT_NAME=old\nTASK_MODE=old\nCOMPOSE_PROFILES=feishu\n"
    attempted = "EVENT_NAME=attempted\nTASK_MODE=attempted\nCOMPOSE_PROFILES=unifi\n"

    run("docker", "pull", IMAGE)
    try:
        replace_env(restored)
        compose("up", "-d", "--no-deps", "--pull", "never",
                *RESIDENTS, "librenms-config", "grafana-setup", "feishu-ws")
        for task in ("librenms-config", "grafana-setup"):
            assert run("docker", "wait", container(task)).stdout.strip() == "0"
        wait_health(container("prometheus"), "healthy")
        wait_health(container("grafana"), "healthy")

        # Model a partial forward apply. The stopped task has a failed
        # execution and player-targets holds a bind to the attempted .env inode.
        replace_env(attempted)
        compose("up", "-d", "--no-deps", "--pull", "never", "--force-recreate",
                "bigscreen", "player-targets", "librenms-config", "unpoller", "prometheus")
        compose("--profile", "feishu", "rm", "-sf", "feishu-ws")
        assert run("docker", "wait", container("librenms-config")).stdout.strip() == "1"
        before = {name: container(name) for name in services}
        assert before["unpoller"] and not before["feishu-ws"]
        assert run("docker", "exec", before["player-targets"], "cat", "/config/.env").stdout == attempted
        run("docker", "exec", before["prometheus"], "rm", "/tmp/ready")
        wait_health(before["prometheus"], "unhealthy")

        replace_env(restored)
        # Reproduce ordinary reconciliation preserving an unhealthy container
        # whose resolved configuration matches the restored configuration.
        compose("up", "-d", "--no-deps", "--no-build", "--pull", "never", "prometheus")
        assert container("prometheus") == before["prometheus"]
        assert health(before["prometheus"]) == "unhealthy"

        # Unreadable profile configuration must not be interpreted as disabled.
        parser = project / "platform_config.py"
        parser_source = parser.read_bytes()
        parser.write_text("raise RuntimeError('fixture parser failure')\n", encoding="utf-8")
        try:
            failed = run("/bin/sh", str(project / "apply-env.sh"), "--recover", check=False)
            assert failed.returncode != 0
            assert "could not read the restored profile configuration" in failed.stderr
            assert {name: container(name) for name in services} == before
        finally:
            parser.write_bytes(parser_source)

        run("/bin/sh", str(project / "apply-env.sh"), "--recover")
        after = {name: container(name) for name in services}

        changed = {"bigscreen", "player-targets", "librenms-config", "unpoller", "feishu-ws", "prometheus"}
        for name in services:
            assert (after[name] != before[name]) == (name in changed), name
        assert not after["unpoller"] and after["feishu-ws"]
        wait_health(after["prometheus"], "healthy")
        assert health(after["grafana"]) == "healthy"
        assert run("docker", "exec", after["bigscreen"], "printenv", "EVENT_NAME").stdout.strip() == "old"
        assert (project / ".env").read_text(encoding="utf-8") == restored
        for name in ("player-targets", "topology-collector"):
            assert run("docker", "exec", after[name], "cat", "/config/.env").stdout == restored
        assert run("docker", "wait", after["librenms-config"]).stdout.strip() == "0"

        run("/bin/sh", str(project / "apply-env.sh"), "--recover")
        assert {name: container(name) for name in services} == after
    finally:
        compose("--profile", "*", "down", "--remove-orphans")
