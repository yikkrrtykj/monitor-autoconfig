"""Apply command execution and post-apply runtime verification."""
from __future__ import annotations

import os
import shlex
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path


APPLY_CHILD_TIMEOUT_MARGIN_SECONDS = 30
APPLY_OPERATION_GRACE_SECONDS = 30


@dataclass(frozen=True)
class ApplyRuntimeContext:
    workdir: Path
    apply_enabled: bool
    apply_command: str
    apply_timeout: int
    verify_timeout: int
    prom_url: str
    grafana_url: str
    bridge_url: str
    bigscreen_url: str


def host_exec_env(context: ApplyRuntimeContext) -> dict:
    """Build the environment used by host-facing runtime commands."""
    env = os.environ.copy()
    env["PATH"] = "/usr/local/bin:/usr/bin:/bin:/host/usr/bin"
    # apply-env runs inside platform-api for console applies. Recreating the
    # caller here would kill it before the durable operation result is written.
    # A direct host apply does not set this flag and therefore refreshes the API.
    env["PLATFORM_API_SELF_APPLY"] = "true"
    requested_check_timeout = env.get("DEPLOY_CHECK_TIMEOUT", "180")
    try:
        requested_check_seconds = max(0, int(requested_check_timeout))
    except ValueError:
        # Preserve deploy-check's existing validation and explicit diagnostic
        # for a malformed operator-provided value.
        pass
    else:
        child_maximum = max(
            0,
            context.apply_timeout - APPLY_CHILD_TIMEOUT_MARGIN_SECONDS,
        )
        env["DEPLOY_CHECK_TIMEOUT"] = str(
            min(requested_check_seconds, child_maximum)
        )
    plugin_dirs = ":".join([
        "/host/usr/libexec/docker/cli-plugins",
        "/host/usr/lib/docker/cli-plugins",
        "/host/usr/local/lib/docker/cli-plugins",
        env.get("DOCKER_CLI_PLUGIN_EXTRA_DIRS", ""),
    ]).strip(":")
    if plugin_dirs:
        env["DOCKER_CLI_PLUGIN_EXTRA_DIRS"] = plugin_dirs
    return env


def verify_runtime_after_apply(context: ApplyRuntimeContext) -> dict:
    """Wait until the user-facing core services answer after recreation."""
    checks = {
        "Prometheus": f"{context.prom_url}/-/ready",
        "Grafana": f"{context.grafana_url}/api/health",
        "告警服务": f"{context.bridge_url}/health",
        "大屏": f"{context.bigscreen_url}/",
    }
    deadline = time.monotonic() + context.verify_timeout
    last_errors: dict[str, str] = {}
    while time.monotonic() < deadline:
        last_errors = {}
        for name, url in checks.items():
            try:
                with urllib.request.urlopen(url, timeout=5) as response:
                    response.read(4096)
                    if not 200 <= response.status < 400:
                        raise RuntimeError(f"HTTP {response.status}")
            except Exception as exc:
                last_errors[name] = str(exc)
        if not last_errors:
            return {"ok": True, "services": sorted(checks)}
        time.sleep(2)
    return {"ok": False, "errors": last_errors}


def _process_output_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _combined_process_output(*parts) -> str:
    return "\n".join(
        text for text in (_process_output_text(part) for part in parts) if text
    ).strip()


def apply_operation_timeout_seconds(context: ApplyRuntimeContext) -> int:
    """Upper bound for primary apply plus one deterministic recovery apply."""
    return 2 * (context.apply_timeout + context.verify_timeout) + (
        APPLY_OPERATION_GRACE_SECONDS
    )


def run_apply_command(context: ApplyRuntimeContext) -> dict:
    if not context.apply_enabled:
        return {
            "needsRedeploy": True,
            "nextStep": "cd librenms+grafana && ./apply-env.sh",
            "applyOutput": "automatic apply is disabled",
        }

    env = host_exec_env(context)

    try:
        completed = subprocess.run(
            shlex.split(context.apply_command),
            cwd=str(context.workdir),
            env=env,
            capture_output=True,
            text=True,
            timeout=context.apply_timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "error": "配置已写入，但自动应用失败：找不到 apply 命令",
            "needsRedeploy": True,
            "nextStep": "cd librenms+grafana && ./apply-env.sh",
            "applyOutput": str(exc),
        }
    except subprocess.TimeoutExpired as exc:
        output = _combined_process_output(exc.stdout, exc.stderr)
        return {
            "ok": False,
            "error": f"配置已写入，但自动应用超时（{context.apply_timeout}s）",
            "needsRedeploy": True,
            "nextStep": "cd librenms+grafana && ./apply-env.sh",
            "applyOutput": output[-4000:],
        }

    output = _combined_process_output(completed.stdout, completed.stderr)
    if completed.returncode != 0:
        return {
            "ok": False,
            "error": "配置已写入，但自动应用失败",
            "needsRedeploy": True,
            "nextStep": "cd librenms+grafana && ./apply-env.sh",
            "applyOutput": output[-4000:],
        }
    verification = verify_runtime_after_apply(context)
    if not verification.get("ok"):
        errors = "；".join(
            f"{name}: {message}"
            for name, message in verification.get("errors", {}).items()
        )
        return {
            "ok": False,
            "error": "容器重建命令已完成，但关键服务未能恢复",
            "needsRedeploy": True,
            "nextStep": "cd librenms+grafana && ./apply-env.sh",
            "applyOutput": (output + "\n运行验证失败：" + errors)[-4000:],
            "verification": verification,
        }
    return {
        "applied": True,
        "needsRedeploy": False,
        "applyOutput": output[-4000:],
        "verification": verification,
    }
