import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from platform_api import apply_runtime

from .test_platform_transactions import load_api, seed


def make_context(tmp_path: Path, **overrides):
    values = {
        "workdir": tmp_path,
        "apply_enabled": True,
        "apply_command": './apply-env.sh --label "two words"',
        "apply_timeout": 300,
        "verify_timeout": 90,
        "prom_url": "http://prometheus:9090",
        "grafana_url": "http://grafana:3000",
        "bridge_url": "http://alertmanager-feishu-bridge:5005",
        "bigscreen_url": "http://bigscreen",
    }
    values.update(overrides)
    return apply_runtime.ApplyRuntimeContext(**values)


class Response:
    def __init__(self, status=200, reads=None):
        self.status = status
        self.reads = reads

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, limit=-1):
        if self.reads is not None:
            self.reads.append(limit)
        return b"ok"


def test_context_is_explicit_and_immutable(tmp_path):
    context = make_context(tmp_path)

    assert context.workdir == tmp_path
    assert context.apply_enabled is True
    assert context.apply_command == './apply-env.sh --label "two words"'
    assert context.apply_timeout == 300
    assert context.verify_timeout == 90
    assert context.prom_url == "http://prometheus:9090"
    assert context.grafana_url == "http://grafana:3000"
    assert context.bridge_url == "http://alertmanager-feishu-bridge:5005"
    assert context.bigscreen_url == "http://bigscreen"
    with pytest.raises(FrozenInstanceError):
        context.apply_timeout = 1


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        (b"stdout-\xff", "stdout-\ufffd"),
        ("stderr", "stderr"),
        (123, "123"),
    ],
)
def test_process_output_text_preserves_conversion(value, expected):
    assert apply_runtime._process_output_text(value) == expected


@pytest.mark.parametrize(
    ("parts", "expected"),
    [
        ((b"stdout-\xff", b"stderr"), "stdout-\ufffd\nstderr"),
        ((" stdout ", None, "stderr\n"), "stdout \nstderr"),
        ((None, "", None), ""),
    ],
)
def test_combined_process_output_preserves_order_and_empty_filter(parts, expected):
    assert apply_runtime._combined_process_output(*parts) == expected


def test_host_exec_env_preserves_self_apply_timeout_and_plugin_paths(
    monkeypatch, tmp_path,
):
    context = make_context(tmp_path, apply_timeout=300)
    monkeypatch.setenv("DEPLOY_CHECK_TIMEOUT", "999")
    monkeypatch.setenv("DOCKER_CLI_PLUGIN_EXTRA_DIRS", "/custom/plugins")

    env = apply_runtime.host_exec_env(context)

    assert env["PATH"] == "/usr/local/bin:/usr/bin:/bin:/host/usr/bin"
    assert env["PLATFORM_API_SELF_APPLY"] == "true"
    assert env["DEPLOY_CHECK_TIMEOUT"] == "270"
    assert env["DOCKER_CLI_PLUGIN_EXTRA_DIRS"] == (
        "/host/usr/libexec/docker/cli-plugins:"
        "/host/usr/lib/docker/cli-plugins:"
        "/host/usr/local/lib/docker/cli-plugins:"
        "/custom/plugins"
    )


def test_host_exec_env_leaves_invalid_deploy_check_timeout_for_child_validation(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("DEPLOY_CHECK_TIMEOUT", "invalid")

    env = apply_runtime.host_exec_env(make_context(tmp_path))

    assert env["DEPLOY_CHECK_TIMEOUT"] == "invalid"


def test_apply_operation_timeout_includes_primary_recovery_verify_and_grace(tmp_path):
    context = make_context(tmp_path, apply_timeout=300, verify_timeout=90)

    assert apply_runtime.apply_operation_timeout_seconds(context) == 810


def test_disabled_apply_returns_existing_manual_redeploy_payload(
    monkeypatch, tmp_path,
):
    context = make_context(tmp_path, apply_enabled=False)
    monkeypatch.setattr(
        apply_runtime.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("disabled apply must not start a process")
        ),
    )

    assert apply_runtime.run_apply_command(context) == {
        "needsRedeploy": True,
        "nextStep": "cd librenms+grafana && ./apply-env.sh",
        "applyOutput": "automatic apply is disabled",
    }


def test_command_success_preserves_subprocess_arguments_and_payload(
    monkeypatch, tmp_path,
):
    context = make_context(tmp_path)
    observed = {}
    verification = {"ok": True, "services": ["Grafana"]}

    def run(args, **kwargs):
        observed.update(args=args, kwargs=kwargs)
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=" apply complete\n",
            stderr="warning",
        )

    monkeypatch.setattr(apply_runtime.subprocess, "run", run)
    monkeypatch.setattr(
        apply_runtime,
        "verify_runtime_after_apply",
        lambda passed_context: verification
        if passed_context is context
        else (_ for _ in ()).throw(AssertionError("wrong context")),
    )

    result = apply_runtime.run_apply_command(context)

    assert observed["args"] == ["./apply-env.sh", "--label", "two words"]
    assert observed["kwargs"] == {
        "cwd": str(tmp_path),
        "env": apply_runtime.host_exec_env(context),
        "capture_output": True,
        "text": True,
        "timeout": 300,
        "check": False,
    }
    assert result == {
        "applied": True,
        "needsRedeploy": False,
        "applyOutput": "apply complete\n\nwarning",
        "verification": verification,
    }


def test_command_failure_preserves_error_payload_and_combined_output(
    monkeypatch, tmp_path,
):
    context = make_context(tmp_path)
    monkeypatch.setattr(
        apply_runtime.subprocess,
        "run",
        lambda args, **_kwargs: subprocess.CompletedProcess(
            args=args,
            returncode=17,
            stdout="compose stdout",
            stderr="compose stderr",
        ),
    )

    assert apply_runtime.run_apply_command(context) == {
        "ok": False,
        "error": "配置已写入，但自动应用失败",
        "needsRedeploy": True,
        "nextStep": "cd librenms+grafana && ./apply-env.sh",
        "applyOutput": "compose stdout\ncompose stderr",
    }


def test_command_not_found_preserves_error_payload(monkeypatch, tmp_path):
    context = make_context(tmp_path)

    def missing(*_args, **_kwargs):
        raise FileNotFoundError("fixture command missing")

    monkeypatch.setattr(apply_runtime.subprocess, "run", missing)

    assert apply_runtime.run_apply_command(context) == {
        "ok": False,
        "error": "配置已写入，但自动应用失败：找不到 apply 命令",
        "needsRedeploy": True,
        "nextStep": "cd librenms+grafana && ./apply-env.sh",
        "applyOutput": "fixture command missing",
    }


def test_command_timeout_preserves_bytes_decode_error_text_and_truncation(
    monkeypatch, tmp_path,
):
    context = make_context(tmp_path, apply_timeout=45)

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(
            ["./apply-env.sh"],
            45,
            output=b"partial-\xff",
            stderr=b"health stalled",
        )

    monkeypatch.setattr(apply_runtime.subprocess, "run", timeout)

    assert apply_runtime.run_apply_command(context) == {
        "ok": False,
        "error": "配置已写入，但自动应用超时（45s）",
        "needsRedeploy": True,
        "nextStep": "cd librenms+grafana && ./apply-env.sh",
        "applyOutput": "partial-\ufffd\nhealth stalled",
    }


def test_verify_success_preserves_check_order_timeout_and_read_limit(
    monkeypatch, tmp_path,
):
    context = make_context(tmp_path)
    calls = []
    reads = []

    def urlopen(url, timeout):
        calls.append((url, timeout))
        return Response(reads=reads)

    monkeypatch.setattr(apply_runtime.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(apply_runtime.time, "monotonic", lambda: 0)

    assert apply_runtime.verify_runtime_after_apply(context) == {
        "ok": True,
        "services": ["Grafana", "Prometheus", "告警服务", "大屏"],
    }
    assert calls == [
        ("http://prometheus:9090/-/ready", 5),
        ("http://grafana:3000/api/health", 5),
        ("http://alertmanager-feishu-bridge:5005/health", 5),
        ("http://bigscreen/", 5),
    ]
    assert reads == [4096, 4096, 4096, 4096]


def test_verify_failure_preserves_last_errors_sleep_and_return_shape(
    monkeypatch, tmp_path,
):
    context = make_context(tmp_path, verify_timeout=10)
    ticks = iter([0, 0, 10])
    sleeps = []

    def unavailable(url, timeout):
        assert timeout == 5
        raise OSError(f"offline {url}")

    monkeypatch.setattr(apply_runtime.urllib.request, "urlopen", unavailable)
    monkeypatch.setattr(apply_runtime.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(apply_runtime.time, "sleep", sleeps.append)

    result = apply_runtime.verify_runtime_after_apply(context)

    assert result == {
        "ok": False,
        "errors": {
            "Prometheus": "offline http://prometheus:9090/-/ready",
            "Grafana": "offline http://grafana:3000/api/health",
            "告警服务": "offline http://alertmanager-feishu-bridge:5005/health",
            "大屏": "offline http://bigscreen/",
        },
    }
    assert sleeps == [2]


def test_verify_non_success_status_keeps_http_reason(monkeypatch, tmp_path):
    context = make_context(tmp_path, verify_timeout=1)
    ticks = iter([0, 0, 1])
    monkeypatch.setattr(
        apply_runtime.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: Response(status=503),
    )
    monkeypatch.setattr(apply_runtime.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(apply_runtime.time, "sleep", lambda _seconds: None)

    result = apply_runtime.verify_runtime_after_apply(context)

    assert result["ok"] is False
    assert result["errors"] == {
        "Prometheus": "HTTP 503",
        "Grafana": "HTTP 503",
        "告警服务": "HTTP 503",
        "大屏": "HTTP 503",
    }


def test_successful_command_with_verify_failure_preserves_combined_diagnostic(
    monkeypatch, tmp_path,
):
    context = make_context(tmp_path)
    verification = {
        "ok": False,
        "errors": {"Prometheus": "connection refused", "大屏": "HTTP 503"},
    }
    monkeypatch.setattr(
        apply_runtime.subprocess,
        "run",
        lambda args, **_kwargs: subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="compose complete",
            stderr="",
        ),
    )
    monkeypatch.setattr(
        apply_runtime,
        "verify_runtime_after_apply",
        lambda _context: verification,
    )

    assert apply_runtime.run_apply_command(context) == {
        "ok": False,
        "error": "容器重建命令已完成，但关键服务未能恢复",
        "needsRedeploy": True,
        "nextStep": "cd librenms+grafana && ./apply-env.sh",
        "applyOutput": (
            "compose complete\n运行验证失败："
            "Prometheus: connection refused；大屏: HTTP 503"
        ),
        "verification": verification,
    }


def test_entrypoint_context_maps_current_composition_dependencies(tmp_path):
    api = load_api(tmp_path)
    context = api._apply_runtime_context()

    assert context == apply_runtime.ApplyRuntimeContext(
        workdir=api.WORKDIR,
        apply_enabled=api.APPLY_ENABLED,
        apply_command=api.APPLY_COMMAND,
        apply_timeout=api.APPLY_TIMEOUT,
        verify_timeout=api.APPLY_VERIFY_TIMEOUT,
        prom_url=api.PRECHECK_PROM_URL,
        grafana_url=api.PRECHECK_GRAFANA_URL,
        bridge_url=api.BRIDGE_URL,
        bigscreen_url=api.PRECHECK_BIGSCREEN_URL,
    )


def test_entrypoint_calls_runtime_directly_and_keeps_one_context(
    monkeypatch, tmp_path,
):
    api = load_api(tmp_path)
    seed(api)
    contexts = []

    def fail_then_recover(context):
        contexts.append(context)
        if len(contexts) == 1:
            return {"ok": False, "error": "fixture failure", "applyOutput": "bad"}
        return {"applied": True, "needsRedeploy": False, "applyOutput": "restored"}

    monkeypatch.setattr(apply_runtime, "run_apply_command", fail_then_recover)

    result = api.apply_config(None, operation_id="apply-runtime-direct")

    assert result["ok"] is False
    assert result["rolledBack"] is True
    assert len(contexts) == 2
    assert contexts[0] is contexts[1]
    assert contexts[0] == api._apply_runtime_context()


def test_entrypoint_keeps_no_apply_runtime_compatibility_symbols(tmp_path):
    api = load_api(tmp_path)

    for symbol in (
        "_host_exec_env",
        "verify_runtime_after_apply",
        "_process_output_text",
        "_combined_process_output",
        "apply_operation_timeout_seconds",
        "run_apply_command",
    ):
        assert not hasattr(api, symbol)
