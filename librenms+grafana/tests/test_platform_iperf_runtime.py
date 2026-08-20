import json
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from types import SimpleNamespace

import pytest

from platform_api import iperf_runtime

from .test_platform_network_tools import _iperf_payload
from .test_platform_transactions import load_api


INITIAL_STATUS = {
    "ok": True,
    "state": "idle",
    "phase": "idle",
    "percent": 0,
    "message": "尚未开始测速",
}


class RuntimeDiagnostic(Exception):
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status = status
        self.payload = {"ok": False, "error": message, **extra}


class MutableClock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


def read_json_file(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json_file(path, payload, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def make_context(
    tmp_path,
    *,
    command="iperf3",
    timeout=60,
    connect_timeout_ms=3000,
    allow_internal=True,
    clock=None,
    monotonic=None,
    token_hex=None,
):
    return iperf_runtime.IperfRuntimeContext(
        workdir=tmp_path,
        history_path=tmp_path / "iperf-history.json",
        command=command,
        timeout=timeout,
        connect_timeout_ms=connect_timeout_ms,
        allow_internal=allow_internal,
        error_factory=RuntimeDiagnostic,
        validate_network_host=lambda value, _field: str(value).strip(),
        read_json_file=read_json_file,
        write_json_file=write_json_file,
        host_exec_env=lambda: {"PATH": "fixture"},
        clock=clock or (lambda: 1_000.0),
        monotonic=monotonic or (lambda: 100.0),
        token_hex=token_hex or (lambda _size: "abcdef"),
    )


def reset_runtime_state():
    global_status = iperf_runtime.IPERF_STATUS
    iperf_runtime.IPERF_TASKS.clear()
    iperf_runtime.IPERF_CANCEL_EVENTS.clear()
    iperf_runtime.IPERF_PROCESSES.clear()
    iperf_runtime.IPERF_ACTIVE_TASK_ID = ""
    global_status.clear()
    global_status.update(INITIAL_STATUS)
    if iperf_runtime.IPERF_LOCK.locked():
        iperf_runtime.IPERF_LOCK.release()


@pytest.fixture(autouse=True)
def clean_runtime_state():
    reset_runtime_state()
    yield
    reset_runtime_state()


def assert_error(exc, status, message):
    assert exc.value.status == status
    assert exc.value.payload["error"] == message


def completed_result(port=5201):
    return {
        "mbps": 950.0,
        "seconds": 10.01,
        "retransmits": 3,
        "bytes": 1_187_500_000,
        "sender": {},
        "receiver": {},
        "intervals": [],
        "port": port,
    }


def test_runtime_is_extracted_and_routers_bind_directly_without_wrappers(tmp_path):
    api = load_api(tmp_path)
    read_deps = api._read_api_dependencies()
    write_deps = api._write_api_dependencies()

    assert read_deps.iperf_status_payload.func is iperf_runtime.iperf_status_payload
    assert read_deps.iperf_history_payload.func is iperf_runtime.iperf_history_payload
    assert write_deps.start_iperf_task.func is iperf_runtime.start_iperf_task
    assert write_deps.stop_iperf_task.func is iperf_runtime.stop_iperf_task
    assert read_deps.iperf_status_payload.args[0].history_path == api.IPERF_HISTORY_PATH
    assert write_deps.start_iperf_task.args[0].command == api.IPERF3_COMMAND

    for name in (
        "IPERF_LOCK",
        "IPERF_STATUS_LOCK",
        "IPERF_TASKS_LOCK",
        "IPERF_PROCESS_LOCK",
        "IPERF_TASKS",
        "IPERF_CANCEL_EVENTS",
        "IPERF_PROCESSES",
        "IPERF_ACTIVE_TASK_ID",
        "IPERF_STATUS",
        "IperfCancelled",
        "_set_iperf_status",
        "_public_iperf_payload",
        "iperf_status_payload",
        "iperf_history_payload",
        "_save_iperf_history",
        "_execute_iperf_command",
        "_run_iperf_direction",
        "run_iperf_test",
        "_iperf_task_worker",
        "start_iperf_task",
        "stop_iperf_task",
    ):
        assert not hasattr(api, name)


def test_context_keeps_runtime_dependencies_explicit(tmp_path):
    api = load_api(tmp_path)
    context = api._iperf_runtime_context()

    assert context.workdir == api.WORKDIR
    assert context.history_path == api.IPERF_HISTORY_PATH
    assert context.command == api.IPERF3_COMMAND
    assert context.timeout == api.IPERF3_TIMEOUT
    assert context.connect_timeout_ms == api.IPERF3_CONNECT_TIMEOUT_MS
    assert context.allow_internal == api.IPERF3_ALLOW_INTERNAL
    assert context.error_factory is api.DiagnosticError
    assert context.validate_network_host is api.validate_network_host
    assert context.read_json_file is api.read_json_file
    assert context.write_json_file is api.write_json_file
    assert context.host_exec_env.func is api.platform_apply_runtime.host_exec_env
    assert context.host_exec_env.args == (api._apply_runtime_context(),)


def test_composed_context_preserves_diagnostic_error_type_status_and_payload(tmp_path):
    api = load_api(tmp_path)
    api.IPERF3_ALLOW_INTERNAL = True
    context = api._iperf_runtime_context()

    with pytest.raises(api.DiagnosticError) as exc:
        iperf_runtime.run_iperf_test(
            context,
            {"server": "192.168.10.5", "ports": "5201,5202"},
        )

    assert exc.value.status == HTTPStatus.BAD_REQUEST
    assert exc.value.payload == {
        "ok": False,
        "error": "端口应为单个端口或范围，例如 5201-5210",
    }


def test_initial_status_and_public_payload_copy(tmp_path):
    monotonic = MutableClock(105.25)
    context = make_context(tmp_path, monotonic=monotonic)

    assert iperf_runtime.iperf_status_payload(context) == {
        **INITIAL_STATUS,
        "elapsedSeconds": 0,
    }
    source = {"state": "running", "_startedMonotonic": 100.0}
    assert iperf_runtime._public_iperf_payload(context, source) == {
        "state": "running",
        "elapsedSeconds": 5.2,
    }
    assert source == {"state": "running", "_startedMonotonic": 100.0}


def test_status_updates_task_snapshot_and_hides_private_clock(tmp_path):
    context = make_context(tmp_path, monotonic=lambda: 103.0)
    iperf_runtime.IPERF_TASKS["task-1"] = {"state": "queued"}

    iperf_runtime._set_iperf_status(
        taskId="task-1",
        state="running",
        _startedMonotonic=100.0,
    )

    assert iperf_runtime.IPERF_TASKS["task-1"] == iperf_runtime.IPERF_STATUS
    assert iperf_runtime.iperf_status_payload(context, "task-1") == {
        **INITIAL_STATUS,
        "taskId": "task-1",
        "state": "running",
        "elapsedSeconds": 3.0,
    }


def test_status_falls_back_to_persisted_history_and_rejects_unknown(tmp_path):
    context = make_context(tmp_path)
    write_json_file(context.history_path, [{"taskId": "old", "state": "complete"}])

    assert iperf_runtime.iperf_status_payload(context, "old")["state"] == "complete"
    with pytest.raises(RuntimeDiagnostic) as exc:
        iperf_runtime.iperf_status_payload(context, "missing")
    assert_error(exc, HTTPStatus.NOT_FOUND, "iPerf3 任务不存在或已过期")


def test_history_keeps_five_deduplicated_summaries(tmp_path):
    context = make_context(tmp_path)
    for index in range(7):
        iperf_runtime._save_iperf_history(context, {
            "taskId": f"task-{index}",
            "state": "complete",
            "server": f"node-{index}.example.test",
            "finishedAt": index,
            "results": [],
            "ignored": "private",
        })
    iperf_runtime._save_iperf_history(context, {
        "taskId": "task-4",
        "state": "failed",
        "server": "updated.example.test",
    })

    history = iperf_runtime.iperf_history_payload(context)["history"]
    assert [item["taskId"] for item in history] == [
        "task-4", "task-6", "task-5", "task-3", "task-2",
    ]
    assert history[0]["server"] == "updated.example.test"
    assert "ignored" not in history[0]


def test_history_non_list_falls_back_to_empty(tmp_path):
    context = make_context(tmp_path)
    write_json_file(context.history_path, {"not": "history"})

    assert iperf_runtime.iperf_history_payload(context) == {"ok": True, "history": []}


def test_run_tries_next_port_without_shell_and_keeps_status(monkeypatch, tmp_path):
    context = make_context(tmp_path, timeout=30)
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[command.index("-p") + 1] == "5200":
            return SimpleNamespace(returncode=1, stdout="", stderr="server is busy")
        return SimpleNamespace(returncode=0, stdout=_iperf_payload(), stderr="")

    monkeypatch.setattr(iperf_runtime.subprocess, "run", fake_run)
    result = iperf_runtime.run_iperf_test(context, {
        "server": "speedtest.hkg12.hk.leaseweb.net",
        "ports": "5200-5201",
        "duration": 3,
        "parallel": 1,
        "direction": "upload",
    })

    assert result["protocol"] == "TCP"
    assert result["results"][0]["port"] == 5201
    assert [call[0][call[0].index("-p") + 1] for call in calls] == ["5200", "5201"]
    assert all("shell" not in kwargs for _command, kwargs in calls)
    assert all(kwargs["cwd"] == str(tmp_path) for _command, kwargs in calls)
    assert all(kwargs["env"] == {"PATH": "fixture"} for _command, kwargs in calls)
    assert iperf_runtime.iperf_status_payload(context)["state"] == "complete"
    assert iperf_runtime.iperf_status_payload(context)["percent"] == 100


def test_run_defaults_to_hong_kong_preset(monkeypatch, tmp_path):
    context = make_context(tmp_path)
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=_iperf_payload(), stderr="")

    monkeypatch.setattr(iperf_runtime.subprocess, "run", fake_run)
    iperf_runtime.run_iperf_test(
        context,
        {"duration": 3, "parallel": 1, "direction": "upload"},
    )

    assert calls[0][calls[0].index("-c") + 1] == "speedtest.hkg12.hk.leaseweb.net"
    assert calls[0][calls[0].index("-p") + 1] == "5201"


def test_bidirectional_run_shares_deadline_and_reuses_successful_port(monkeypatch, tmp_path):
    context = make_context(tmp_path, timeout=60)
    calls = []

    def fake_direction(_context, _host, ports, _duration, _parallel, reverse,
                       deadline, *_args):
        calls.append((list(ports), reverse, deadline))
        return completed_result(5202)

    monkeypatch.setattr(iperf_runtime, "_run_iperf_direction", fake_direction)
    result = iperf_runtime.run_iperf_test(context, {
        "ports": "5201-5203",
        "duration": 3,
        "parallel": 1,
        "direction": "both",
    })

    assert [item["direction"] for item in result["results"]] == ["upload", "download"]
    assert calls == [
        ([5201, 5202, 5203], False, 160.0),
        ([5202, 5201, 5203], True, 160.0),
    ]
    assert iperf_runtime.iperf_status_payload(context)["maxSeconds"] == 60


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"ports": "5201,5202"}, "端口应为单个端口或范围，例如 5201-5210"),
        ({"duration": "bad"}, "测试时长和并发数必须是整数"),
        ({"duration": 2}, "测试时长必须在 3-30 秒之间"),
        ({"parallel": 21}, "并发数必须在 1-20 之间"),
        ({"direction": "sideways"}, "测速方向无效"),
    ],
)
def test_run_validation_errors_keep_messages(tmp_path, data, message):
    context = make_context(tmp_path)

    with pytest.raises(RuntimeDiagnostic) as exc:
        iperf_runtime.run_iperf_test(context, data)
    assert_error(exc, HTTPStatus.BAD_REQUEST, message)


def test_internal_target_gate_and_allow_override(tmp_path):
    blocked = make_context(tmp_path, allow_internal=False)
    allowed = make_context(tmp_path, allow_internal=True)

    with pytest.raises(RuntimeDiagnostic) as exc:
        iperf_runtime.run_iperf_test(blocked, {"server": "192.168.10.5"})
    assert "PLATFORM_IPERF3_ALLOW_INTERNAL" in exc.value.payload["error"]
    with pytest.raises(RuntimeDiagnostic) as allowed_exc:
        iperf_runtime.run_iperf_test(
            allowed,
            {"server": "192.168.10.5", "duration": 99},
        )
    assert allowed_exc.value.payload["error"] == "测试时长必须在 3-30 秒之间"


def test_direct_run_single_flight_conflict(tmp_path):
    context = make_context(tmp_path)
    assert iperf_runtime.IPERF_LOCK.acquire(blocking=False)

    with pytest.raises(RuntimeDiagnostic) as exc:
        iperf_runtime.run_iperf_test(context, {"duration": 3})
    assert_error(exc, HTTPStatus.CONFLICT, "已有 iperf3 测速正在运行，请稍后再试")


def test_direction_maps_file_not_found(monkeypatch, tmp_path):
    context = make_context(tmp_path)

    def missing(*_args, **_kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(iperf_runtime, "_execute_iperf_command", missing)
    with pytest.raises(RuntimeDiagnostic) as exc:
        iperf_runtime._run_iperf_direction(
            context, "example.test", [5201], 3, 1, False, 120.0, 0, 1,
        )
    assert_error(
        exc,
        HTTPStatus.SERVICE_UNAVAILABLE,
        "找不到 iPerf3 客户端，请重新运行 deploy.sh 构建 platform-api 镜像",
    )


def test_direction_retries_timeout_parser_and_process_errors(monkeypatch, tmp_path):
    context = make_context(tmp_path)
    outcomes = [
        subprocess.TimeoutExpired(["iperf3"], 3),
        SimpleNamespace(returncode=0, stdout="bad json", stderr=""),
        SimpleNamespace(returncode=1, stdout="", stderr="connection refused"),
    ]

    def execute(*_args, **_kwargs):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(iperf_runtime, "_execute_iperf_command", execute)
    with pytest.raises(RuntimeDiagnostic) as exc:
        iperf_runtime._run_iperf_direction(
            context,
            "example.test",
            [5201, 5202, 5203],
            3,
            1,
            False,
            120.0,
            0,
            1,
        )
    assert_error(
        exc,
        HTTPStatus.BAD_GATEWAY,
        "iperf3 测速失败：5201: 超时；5202: iperf3 未返回可解析的 JSON；5203: 无法连接",
    )


@pytest.mark.parametrize(("reverse", "has_reverse"), [(False, False), (True, True)])
def test_direction_preserves_command_and_reverse_flag(
    monkeypatch, tmp_path, reverse, has_reverse,
):
    context = make_context(
        tmp_path,
        command="custom-iperf --fixture",
        connect_timeout_ms=4321,
    )
    observed = {}

    def execute(_context, command, timeout, task_id, cancel_event):
        observed.update(
            command=command,
            timeout=timeout,
            task_id=task_id,
            cancel_event=cancel_event,
        )
        return SimpleNamespace(returncode=0, stdout=_iperf_payload(), stderr="")

    cancel_event = threading.Event()
    monkeypatch.setattr(iperf_runtime, "_execute_iperf_command", execute)
    result = iperf_runtime._run_iperf_direction(
        context,
        "speed.example.test",
        [5207],
        3,
        4,
        reverse,
        120.0,
        0,
        1,
        "task-7",
        cancel_event,
    )

    assert result["port"] == 5207
    assert observed["command"][:2] == ["custom-iperf", "--fixture"]
    assert observed["command"][2:] == [
        "-c", "speed.example.test",
        "-p", "5207",
        "--connect-timeout", "4321",
        "-t", "3",
        "-P", "4",
        "-J",
        *(["-R"] if has_reverse else []),
    ]
    assert observed["timeout"] == 8
    assert observed["task_id"] == "task-7"
    assert observed["cancel_event"] is cancel_event


@pytest.mark.parametrize(
    ("error", "state", "message"),
    [
        (iperf_runtime.IperfCancelled("测速已停止"), "cancelled", "测速已停止"),
        (RuntimeDiagnostic(502, "fixture diagnostic"), "failed", "fixture diagnostic"),
        (RuntimeError("fixture failure"), "failed", "fixture failure"),
    ],
)
def test_run_failure_updates_status_and_releases_lock(
    monkeypatch, tmp_path, error, state, message,
):
    context = make_context(tmp_path)

    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(iperf_runtime, "_run_iperf_direction", fail)
    with pytest.raises(type(error)):
        iperf_runtime.run_iperf_test(
            context,
            {"duration": 3, "parallel": 1, "direction": "upload"},
        )

    status = iperf_runtime.iperf_status_payload(context)
    assert status["state"] == state
    assert status["phase"] == state
    assert status["message"] == message
    assert not iperf_runtime.IPERF_LOCK.locked()


def test_execute_direct_command_preserves_subprocess_run_contract(monkeypatch, tmp_path):
    context = make_context(tmp_path)
    observed = {}

    def fake_run(command, **kwargs):
        observed.update(command=command, kwargs=kwargs)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(iperf_runtime.subprocess, "run", fake_run)
    result = iperf_runtime._execute_iperf_command(
        context, ["iperf3", "-J"], timeout=7,
    )

    assert result.stdout == "ok"
    assert observed == {
        "command": ["iperf3", "-J"],
        "kwargs": {
            "cwd": str(tmp_path),
            "env": {"PATH": "fixture"},
            "capture_output": True,
            "text": True,
            "timeout": 7,
            "check": False,
        },
    }


def test_managed_command_does_not_deadlock_on_large_output(tmp_path):
    context = make_context(tmp_path, monotonic=time.monotonic)
    completed = iperf_runtime._execute_iperf_command(
        context,
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 200000)"],
        timeout=5,
        task_id="large-output",
    )

    assert completed.returncode == 0
    assert len(completed.stdout) == 200000
    assert "large-output" not in iperf_runtime.IPERF_PROCESSES


def test_managed_process_cancellation_terminates_and_cleans_map(monkeypatch, tmp_path):
    context = make_context(tmp_path)
    cancel_event = threading.Event()
    cancel_event.set()

    class FakeProcess:
        returncode = None

        def __init__(self, *_args, **_kwargs):
            self.terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout):
            assert timeout == 2
            self.returncode = -15

    process = FakeProcess()
    monkeypatch.setattr(iperf_runtime.subprocess, "Popen", lambda *_a, **_k: process)

    with pytest.raises(iperf_runtime.IperfCancelled, match="测速已停止"):
        iperf_runtime._execute_iperf_command(
            context,
            ["iperf3"],
            timeout=5,
            task_id="task-1",
            cancel_event=cancel_event,
        )
    assert process.terminated is True
    assert "task-1" not in iperf_runtime.IPERF_PROCESSES


def test_managed_process_timeout_kills_and_cleans_map(monkeypatch, tmp_path):
    monotonic_values = iter([100.0, 106.0])
    context = make_context(tmp_path, monotonic=lambda: next(monotonic_values))

    class FakeProcess:
        returncode = None

        def __init__(self, *_args, **_kwargs):
            self.killed = False

        def poll(self):
            return None

        def kill(self):
            self.killed = True

        def wait(self, timeout):
            assert timeout == 2
            self.returncode = -9

    process = FakeProcess()
    monkeypatch.setattr(iperf_runtime.subprocess, "Popen", lambda *_a, **_k: process)

    with pytest.raises(subprocess.TimeoutExpired):
        iperf_runtime._execute_iperf_command(
            context, ["iperf3"], timeout=5, task_id="task-2",
        )
    assert process.killed is True
    assert "task-2" not in iperf_runtime.IPERF_PROCESSES


def test_start_task_id_maps_single_flight_and_stop(monkeypatch, tmp_path):
    context = make_context(tmp_path, clock=lambda: 1_234.9, token_hex=lambda _n: "a1b2c3")
    threads = []

    class DeferredThread:
        def __init__(self, target, args, **kwargs):
            threads.append((target, args, kwargs))

        def start(self):
            pass

    monkeypatch.setattr(iperf_runtime.threading, "Thread", DeferredThread)
    data = {"server": "fixture.test"}
    task = iperf_runtime.start_iperf_task(context, data)

    assert task["taskId"] == "iperf-1234-a1b2c3"
    assert task["state"] == "queued"
    assert iperf_runtime.IPERF_ACTIVE_TASK_ID == task["taskId"]
    assert task["taskId"] in iperf_runtime.IPERF_TASKS
    assert task["taskId"] in iperf_runtime.IPERF_CANCEL_EVENTS
    assert threads[0][0] is iperf_runtime._iperf_task_worker
    assert threads[0][1][1] == task["taskId"]
    assert threads[0][1][2] == data
    assert threads[0][1][2] is not data
    assert threads[0][2] == {"name": "iperf-a1b2c3", "daemon": True}

    with pytest.raises(RuntimeDiagnostic) as exc:
        iperf_runtime.start_iperf_task(context, {})
    assert exc.value.status == HTTPStatus.CONFLICT
    assert exc.value.payload["taskId"] == task["taskId"]

    stopped = iperf_runtime.stop_iperf_task(context, {"taskId": task["taskId"]})
    assert stopped == {
        "ok": True,
        "taskId": task["taskId"],
        "state": "stopping",
        "message": "正在停止测速",
    }
    assert iperf_runtime.IPERF_CANCEL_EVENTS[task["taskId"]].is_set()


def test_stop_running_process_terminates_but_finished_task_is_unchanged(tmp_path):
    context = make_context(tmp_path)
    cancel_event = threading.Event()

    class Process:
        terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

    process = Process()
    iperf_runtime.IPERF_TASKS["running"] = {"state": "running"}
    iperf_runtime.IPERF_CANCEL_EVENTS["running"] = cancel_event
    iperf_runtime.IPERF_PROCESSES["running"] = process
    iperf_runtime.IPERF_TASKS["done"] = {"state": "complete"}

    assert iperf_runtime.stop_iperf_task(context, {"taskId": "running"})["state"] == "stopping"
    assert cancel_event.is_set()
    assert process.terminated is True
    assert iperf_runtime.stop_iperf_task(context, {"taskId": "done"}) == {
        "ok": True,
        "taskId": "done",
        "state": "complete",
        "message": "任务已经结束",
    }


@pytest.mark.parametrize(
    ("data", "status", "message"),
    [
        ({}, HTTPStatus.BAD_REQUEST, "缺少 iPerf3 任务编号"),
        ({"taskId": "missing"}, HTTPStatus.NOT_FOUND, "iPerf3 任务不存在或已过期"),
    ],
)
def test_stop_rejects_missing_and_unknown_tasks(tmp_path, data, status, message):
    context = make_context(tmp_path)

    with pytest.raises(RuntimeDiagnostic) as exc:
        iperf_runtime.stop_iperf_task(context, data)
    assert_error(exc, status, message)


@pytest.mark.parametrize(
    ("outcome", "state", "message"),
    [
        ({"ok": True, "taskId": "task", "results": []}, "complete", None),
        (iperf_runtime.IperfCancelled("测速已停止"), "cancelled", "测速已停止"),
        (RuntimeDiagnostic(502, "fixture diagnostic"), "failed", "fixture diagnostic"),
        (RuntimeError("fixture failure"), "failed", "fixture failure"),
    ],
)
def test_worker_finalizes_history_and_cleans_active_state(
    monkeypatch, tmp_path, outcome, state, message,
):
    context = make_context(tmp_path)
    cancel_event = threading.Event()
    iperf_runtime.IPERF_ACTIVE_TASK_ID = "task"
    iperf_runtime.IPERF_TASKS["task"] = {"state": "queued"}
    iperf_runtime.IPERF_CANCEL_EVENTS["task"] = cancel_event

    def run(*_args, **_kwargs):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(iperf_runtime, "run_iperf_test", run)
    iperf_runtime._iperf_task_worker(context, "task", {}, cancel_event)

    final = iperf_runtime.IPERF_TASKS["task"]
    assert final["state"] == state
    assert final["phase"] == state
    if message is not None:
        assert final["message"] == message
    assert "task" not in iperf_runtime.IPERF_CANCEL_EVENTS
    assert iperf_runtime.IPERF_ACTIVE_TASK_ID == ""
    assert iperf_runtime.iperf_history_payload(context)["history"][0]["taskId"] == "task"


def test_task_map_prunes_only_finished_entries(monkeypatch, tmp_path):
    context = make_context(tmp_path)

    class DeferredThread:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(iperf_runtime.threading, "Thread", DeferredThread)
    for index in range(21):
        iperf_runtime.IPERF_TASKS[f"old-{index}"] = {"state": "complete"}

    task = iperf_runtime.start_iperf_task(context, {})

    assert len(iperf_runtime.IPERF_TASKS) == 20
    assert task["taskId"] in iperf_runtime.IPERF_TASKS
    assert "old-0" not in iperf_runtime.IPERF_TASKS
    assert "old-1" not in iperf_runtime.IPERF_TASKS
