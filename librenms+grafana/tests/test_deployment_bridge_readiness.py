import json
import os
import shlex
import shutil
import subprocess
import sys

import pytest

from platform_api import apply_runtime
from .test_platform_apply_runtime import make_context, Response
from . import test_deploy_check as check
from .test_librenms_auto_config_flow import _extract_shell_function


def health(**changes):
    payload = {"ok": True, "ready": True, "dryRun": False,
               "tokenConfigured": True, "appConfigured": False,
               "deadWatchers": [], "watchers": {"device-online": {"alive": True}}}
    payload.update(changes)
    return payload


@pytest.mark.parametrize("payload", [health(ready=False), {}, [],
    health(ready="true"), health(deadWatchers=["device-online"]),
    health(watchers={"device-online": {"alive": False}}),
    health(tokenConfigured=False), "malformed"])
def test_final_runtime_verification_rejects_unready_bridge(tmp_path, monkeypatch, payload):
    (tmp_path / ".env").write_text("FEISHU_ROBOT_TOKEN=fixture-only\n")
    ticks = [0.0]
    monkeypatch.setattr(apply_runtime.time, "monotonic", lambda: ticks[0])
    monkeypatch.setattr(apply_runtime.time, "sleep", lambda seconds: ticks.__setitem__(0, ticks[0] + seconds))
    class HealthResponse(Response):
        def read(self, limit=-1):
            return b"invalid" if payload == "malformed" else json.dumps(payload).encode()
    monkeypatch.setattr(apply_runtime.urllib.request, "urlopen", lambda *args, **kwargs: HealthResponse())
    result = apply_runtime.verify_runtime_after_apply(make_context(tmp_path, verify_timeout=1))
    assert result["ok"] is False
    assert "告警服务" in result["errors"]


@pytest.mark.parametrize("payload", [health(ready=False), {}, [],
    health(watchers={"x":{"alive":"true"}}), health(deadWatchers=["x"])])
def test_deploy_check_rejects_bad_bridge_payload(tmp_path, payload):
    envelope = {"health": payload, "librenmsTokenAvailable": True}
    result, report = check.run_check(tmp_path, env_text=check.BASE_ENV + "FEISHU_ROBOT_TOKEN=fixture-only\n",
                                   STUB_BRIDGE_HEALTH=json.dumps(envelope))
    assert result.returncode != 0
    assert check.checks_by_id(report)["bridge_readiness"]["status"] == "FAIL"


def test_fresh_bridge_skips_notifications_but_requires_readonly_token(tmp_path):
    payload = {"health": health(ready=False, tokenConfigured=False), "librenmsTokenAvailable": True}
    result, report = check.run_check(tmp_path, STUB_BRIDGE_HEALTH=json.dumps(payload))
    assert result.returncode == 0, result.stderr
    ids = check.checks_by_id(report)
    assert ids["bridge_liveness"]["status"] == "PASS"
    assert ids["bridge_notifications"]["status"] == "SKIP"
    assert ids["librenms_consumer_token"]["status"] == "PASS"


def test_missing_librenms_token_cannot_be_masked_by_feishu_token(tmp_path):
    payload = {"health": health(), "librenmsTokenAvailable": False}
    result, report = check.run_check(tmp_path, STUB_BRIDGE_HEALTH=json.dumps(payload))
    assert result.returncode != 0
    assert check.checks_by_id(report)["bridge_readiness"]["status"] == "FAIL"


@pytest.mark.parametrize("env_text,payload", [
    ("FEISHU_ROBOT_TOKEN=fixture\n", health()),
    ("FEISHU_APP_ID=fixture\nFEISHU_APP_SECRET=fixture\n", health(tokenConfigured=False, appConfigured=True)),
    ("FEISHU_BRIDGE_DRY_RUN=true\n", health(tokenConfigured=False, dryRun=True)),
])
def test_enabled_notification_capabilities_pass_without_delivery(tmp_path, env_text, payload):
    envelope = {"health": payload, "librenmsTokenAvailable": True}
    result, report = check.run_check(tmp_path, env_text=check.BASE_ENV + env_text,
                                   STUB_BRIDGE_HEALTH=json.dumps(envelope))
    assert result.returncode == 0
    assert check.checks_by_id(report)["bridge_notifications"]["status"] == "PASS"


def test_bridge_connection_failure_is_not_ready(tmp_path):
    result, report = check.run_check(tmp_path, STUB_BRIDGE_FAIL="true")
    assert result.returncode != 0
    assert check.checks_by_id(report)["bridge_readiness"]["status"] == "FAIL"


def test_text_and_json_report_the_same_bridge_checks(tmp_path):
    json_dir, text_dir = tmp_path / "json", tmp_path / "text"
    json_dir.mkdir()
    text_dir.mkdir()
    _, report = check.run_check(json_dir)
    _, rendered = check.run_check(text_dir, output="text")
    for item in report["checks"]:
        if item["id"].startswith("bridge") or item["id"] == "librenms_consumer_token":
            assert item["message"] in rendered


def test_runtime_probes_use_remaining_budget_and_stop_launching(monkeypatch, tmp_path):
    ticks = [0.0]
    calls = []
    monkeypatch.setattr(apply_runtime.time, "monotonic", lambda: ticks[0])
    monkeypatch.setattr(apply_runtime.time, "sleep", lambda seconds: ticks.__setitem__(0, ticks[0] + seconds))
    def slow(url, timeout):
        calls.append(timeout)
        ticks[0] += timeout
        raise TimeoutError("offline")
    monkeypatch.setattr(apply_runtime.urllib.request, "urlopen", slow)
    report = apply_runtime.verify_runtime_after_apply(make_context(tmp_path, verify_timeout=6))
    assert report["ok"] is False
    assert calls == [5, 1]
    assert ticks[0] == 6


@pytest.mark.parametrize("size,expected", [(5000, True), (70000, False)])
def test_runtime_reads_complete_but_bounded_health_json(monkeypatch, tmp_path, size, expected):
    payload = json.dumps(health(delivery={"diagnostic": "x" * size})).encode()
    ticks = [0.0]
    monkeypatch.setattr(apply_runtime.time, "monotonic", lambda: ticks[0])
    monkeypatch.setattr(apply_runtime.time, "sleep", lambda seconds: ticks.__setitem__(0, ticks[0] + seconds))
    class CompleteResponse(Response):
        def read(self, limit=-1):
            return payload[:limit]
    monkeypatch.setattr(apply_runtime.urllib.request, "urlopen", lambda *args, **kwargs: CompleteResponse())
    result = apply_runtime.verify_runtime_after_apply(make_context(tmp_path, verify_timeout=1))
    assert result["ok"] is expected


def test_effective_feishu_profile_requires_app_readiness(tmp_path):
    result, report = check.run_check(tmp_path, COMPOSE_PROFILES="feishu")
    assert result.returncode != 0
    assert check.checks_by_id(report)["bridge_readiness"]["status"] == "FAIL"


def run_real_bridge_probe(tmp_path, *, module_present=True, token_present=True,
                          payload=None, omit_workdir=False):
    """Execute production Python verbatim in a separate process, outside /app.

    Only Compose's filesystem mapping and HTTP transport are simulated. The
    real LibreNMSClient module must be found through the requested exec cwd.
    """
    caller = tmp_path / "caller"
    app = tmp_path / "application-mount"
    caller.mkdir()
    app.mkdir()
    if module_present:
        shutil.copy2(check.ROOT / "librenms_client.py", app / "librenms_client.py")
    (caller / "platform_api").mkdir()
    for name in ("platform_config.py", "version_info.py"):
        shutil.copy2(check.ROOT / name, caller / name)
    shutil.copy2(check.ROOT / "platform_api" / "deployment_health.py",
                 caller / "platform_api" / "deployment_health.py")
    (caller / ".env").write_text("FEISHU_ROBOT_TOKEN=fixture-webhook\n")
    token = tmp_path / "api-token"
    secret = "R1_FAKE_LIBRENMS_SECRET_DO_NOT_LOG"
    if token_present:
        token.write_text(secret)
    (tmp_path / "health.json").write_text(json.dumps(payload if payload is not None else health()))
    # The hook rejects every URL except the read-only health request. No real
    # LibreNMS calls or notifications can escape this test.
    hook = r'''
import json, os, pathlib, sys, urllib.request
def health_only(url, timeout):
    assert url == "http://127.0.0.1:5005/health"
    assert timeout == 3
    assert sys.stdin.read() == ""
    pathlib.Path(os.environ["R1_REQUESTS"]).write_text(url)
    class Reply:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, size):
            assert size == 65537
            return pathlib.Path(os.environ["R1_HEALTH"]).read_bytes()[:size]
    return Reply()
urllib.request.urlopen = health_only
'''
    (tmp_path / "http-hook.py").write_text(hook)
    compose = tmp_path / "compose-double.py"
    compose.write_text(r'''
import os, pathlib, subprocess, sys
args = sys.argv[1:]
assert args.pop(0) == "exec"
assert args.pop(0) == "-T"
cwd = pathlib.Path.cwd()
if args[0] in ("-w", "--workdir"):
    args.pop(0)
    assert args.pop(0) == "/app"
    cwd = pathlib.Path(os.environ["R1_APP"])
assert args.pop(0) == "alertmanager-feishu-bridge"
assert args[:2] == ["python", "-c"]
pathlib.Path(os.environ["R1_CWD_LOG"]).write_text(str(cwd))
hook = pathlib.Path(os.environ["R1_HOOK"]).read_text()
result = subprocess.run([sys.executable, "-c", hook + "\n" + args[2], *args[3:]],
                        cwd=cwd, capture_output=True, text=True, timeout=5)
sys.stdout.write(result.stdout)
sys.stderr.write(result.stderr)
raise SystemExit(result.returncode)
''')
    source = (check.ROOT / "deploy-check.sh").read_text(encoding="utf-8")
    function = _extract_shell_function(source, "wait_for_bridge")
    if omit_workdir:
        function = function.replace(" -w /app", "")
    python = shlex.quote(sys.executable.replace("\\", "/"))
    prelude = ("#!/bin/sh\nSCRIPT_DIR=" + shlex.quote(caller.as_posix()) + "\n"
               + "compose() { " + python + " " + shlex.quote(compose.as_posix()) + ' "$@"; }\n'
               + "python3() { " + python + ' "$@"; }\n')
    prelude += '''
HTTP_BODY=health-body
HTTP_ERROR=health-error
RESULTS_FILE=checks
DEPLOY_CHECK_HTTP_TIMEOUT=3
deadline_reached() { return 0; }
record() { printf '%s %s %s\n' "$1" "$2" "$3"; }
'''
    check._write_executable(caller / "probe.sh", prelude + function + "\nwait_for_bridge\n")
    env = os.environ.copy()
    for key in ("PYTHONPATH", "COMPOSE_PROFILES", "LIBRENMS_API_TOKEN"):
        env.pop(key, None)
    env.update(R1_APP=str(app), R1_HOOK=str(tmp_path / "http-hook.py"),
               R1_HEALTH=str(tmp_path / "health.json"), R1_REQUESTS=str(tmp_path / "requests"),
               R1_CWD_LOG=str(tmp_path / "cwd"), LIBRENMS_TOKEN_FILE=str(token),
               PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
               # Preserve the container path when Git Bash calls native Python.
               MSYS2_ARG_CONV_EXCL="*", MSYS_NO_PATHCONV="1")
    result = subprocess.run([check.SH, "probe.sh"], cwd=caller, env=env,
                            input="CALLER_INPUT_MUST_NOT_REACH_PROBE", text=True,
                            capture_output=True, timeout=10)
    checks = (caller / "checks").read_text() if (caller / "checks").exists() else ""
    error = (caller / "health-error").read_text()
    assert secret not in result.stdout + result.stderr + checks + error
    return result, checks, error


def test_r1_real_probe_imports_only_from_explicit_application_cwd(tmp_path):
    result, checks, error = run_real_bridge_probe(tmp_path)
    assert result.returncode == 0, error
    assert (tmp_path / "cwd").read_text() == str(tmp_path / "application-mount")
    assert "librenms_consumer_token" in checks
    assert "bridge_readiness" in result.stdout
    assert (tmp_path / "requests").read_text() == "http://127.0.0.1:5005/health"


def test_r1_unfixed_probe_reproduces_import_failure_outside_app(tmp_path):
    result, checks, error = run_real_bridge_probe(tmp_path, omit_workdir=True)
    assert result.returncode != 0
    assert "ModuleNotFoundError: No module named 'librenms_client'" in error
    assert not checks
    assert not (tmp_path / "requests").exists()


@pytest.mark.parametrize("options,reason", [
    ({"module_present": False}, "ModuleNotFoundError"),
    ({"token_present": False}, "failed validation"),
    ({"payload": health(ready=False)}, "failed validation"),
])
def test_r1_real_probe_keeps_health_and_token_failures_closed(tmp_path, options, reason):
    result, checks, error = run_real_bridge_probe(tmp_path, **options)
    assert result.returncode != 0
    assert "FAIL bridge_readiness" in result.stdout
    assert not checks
    assert reason in error
