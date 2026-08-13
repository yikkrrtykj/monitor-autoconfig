"""State, process, and task orchestration for platform iPerf diagnostics."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
import secrets
import shlex
import subprocess
import tempfile
import threading
import time

from platform_api import iperf


IPERF_LOCK = threading.Lock()
IPERF_STATUS_LOCK = threading.Lock()
IPERF_TASKS_LOCK = threading.Lock()
IPERF_PROCESS_LOCK = threading.Lock()
IPERF_TASKS: dict[str, dict] = {}
IPERF_CANCEL_EVENTS: dict[str, threading.Event] = {}
IPERF_PROCESSES: dict[str, subprocess.Popen] = {}
IPERF_ACTIVE_TASK_ID = ""
IPERF_HISTORY_LIMIT = 5
IPERF_STATUS: dict = {
    "ok": True,
    "state": "idle",
    "phase": "idle",
    "percent": 0,
    "message": "尚未开始测速",
}


@dataclass(frozen=True)
class IperfRuntimeContext:
    workdir: Path
    history_path: Path
    command: str
    timeout: int
    connect_timeout_ms: int
    allow_internal: bool
    error_factory: type[Exception]
    validate_network_host: Callable[[object, str], str]
    read_json_file: Callable[[Path, object], object]
    write_json_file: Callable[..., None]
    host_exec_env: Callable[[], dict]
    clock: Callable[[], float] = time.time
    monotonic: Callable[[], float] = time.monotonic
    token_hex: Callable[[int], str] = secrets.token_hex


def _set_iperf_status(**updates) -> None:
    with IPERF_STATUS_LOCK:
        IPERF_STATUS.update(updates)
        task_id = str(IPERF_STATUS.get("taskId") or "")
        snapshot = dict(IPERF_STATUS)
    if task_id:
        with IPERF_TASKS_LOCK:
            if task_id in IPERF_TASKS:
                IPERF_TASKS[task_id] = snapshot


def _public_iperf_payload(context: IperfRuntimeContext, payload: dict) -> dict:
    payload = dict(payload or {})
    started = payload.pop("_startedMonotonic", None)
    if started is not None:
        payload["elapsedSeconds"] = round(
            max(0, context.monotonic() - started), 1,
        )
    else:
        payload.setdefault("elapsedSeconds", 0)
    return payload


def iperf_status_payload(
    context: IperfRuntimeContext,
    task_id: str = "",
) -> dict:
    task_id = str(task_id or "").strip()
    if task_id:
        with IPERF_TASKS_LOCK:
            task = dict(IPERF_TASKS.get(task_id) or {})
        if not task:
            # Completed task summaries survive a platform-api restart.  This
            # keeps history buttons useful without retaining an unbounded
            # in-memory task dictionary.
            history = context.read_json_file(context.history_path, [])
            if isinstance(history, list):
                task = next((
                    dict(item) for item in history
                    if isinstance(item, dict) and item.get("taskId") == task_id
                ), {})
            if not task:
                raise context.error_factory(
                    HTTPStatus.NOT_FOUND,
                    "iPerf3 任务不存在或已过期",
                )
        return _public_iperf_payload(context, task)
    with IPERF_STATUS_LOCK:
        payload = dict(IPERF_STATUS)
    return _public_iperf_payload(context, payload)


def iperf_history_payload(context: IperfRuntimeContext) -> dict:
    history = context.read_json_file(context.history_path, [])
    if not isinstance(history, list):
        history = []
    return {"ok": True, "history": history[:IPERF_HISTORY_LIMIT]}


def _save_iperf_history(context: IperfRuntimeContext, payload: dict) -> None:
    history = context.read_json_file(context.history_path, [])
    if not isinstance(history, list):
        history = []
    summary = {
        "taskId": payload.get("taskId"),
        "startedAt": payload.get("startedAt"),
        "finishedAt": payload.get("finishedAt"),
        "state": payload.get("state"),
        "server": payload.get("server"),
        "requestedPorts": payload.get("requestedPorts"),
        "duration": payload.get("duration"),
        "parallel": payload.get("parallel"),
        "results": payload.get("results") or [],
        "message": payload.get("message") or "",
    }
    history = [summary, *[
        item for item in history
        if isinstance(item, dict) and item.get("taskId") != summary["taskId"]
    ]]
    context.write_json_file(
        context.history_path,
        history[:IPERF_HISTORY_LIMIT],
        mode=0o600,
    )


class IperfCancelled(Exception):
    pass


def _execute_iperf_command(
    context: IperfRuntimeContext,
    command: list[str],
    timeout: float,
    task_id: str = "",
    cancel_event: threading.Event | None = None,
):
    """Run one iperf process and make only managed background tasks stoppable.

    Direct calls retain ``subprocess.run`` for compatibility with the parser's
    unit tests. Browser-started tasks use Popen so the stop endpoint can
    terminate exactly that task without touching any other process.
    """
    if not task_id:
        return subprocess.run(
            command,
            cwd=str(context.workdir),
            env=context.host_exec_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    # A 30-second, 20-stream JSON result can exceed an OS pipe buffer. Waiting
    # for process exit before communicate() would then deadlock. Temporary
    # files keep output bounded by disk/state resources while preserving stop.
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stdout_handle, \
            tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr_handle:
        process = subprocess.Popen(
            command,
            cwd=str(context.workdir),
            env=context.host_exec_env(),
            stdout=stdout_handle,
            stderr=stderr_handle,
            text=True,
        )
        with IPERF_PROCESS_LOCK:
            IPERF_PROCESSES[task_id] = process
        end = context.monotonic() + timeout
        try:
            while process.poll() is None:
                if cancel_event and cancel_event.is_set():
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    raise IperfCancelled("测速已停止")
                if context.monotonic() >= end:
                    process.kill()
                    process.wait(timeout=2)
                    raise subprocess.TimeoutExpired(command, timeout)
                time.sleep(0.1)
            if cancel_event and cancel_event.is_set():
                raise IperfCancelled("测速已停止")
            stdout_handle.seek(0)
            stderr_handle.seek(0)
            return subprocess.CompletedProcess(
                command,
                process.returncode,
                stdout_handle.read(),
                stderr_handle.read(),
            )
        finally:
            with IPERF_PROCESS_LOCK:
                if IPERF_PROCESSES.get(task_id) is process:
                    IPERF_PROCESSES.pop(task_id, None)


def _run_iperf_direction(
    context: IperfRuntimeContext,
    host: str,
    ports: list[int],
    duration: int,
    parallel: int,
    reverse: bool,
    deadline: float,
    direction_index: int,
    direction_total: int,
    task_id: str = "",
    cancel_event: threading.Event | None = None,
) -> dict:
    attempts: list[str] = []
    direction_name = "download" if reverse else "upload"
    direction_label = "下载" if reverse else "上传"
    for attempt_index, port in enumerate(ports, 1):
        remaining = deadline - context.monotonic()
        if remaining <= 0:
            break
        progress = (
            (direction_index + (attempt_index - 1) / max(1, len(ports)))
            / direction_total
        ) * 100
        _set_iperf_status(
            state="running",
            phase=direction_name,
            direction=direction_name,
            currentPort=port,
            attempt=attempt_index,
            totalAttempts=len(ports),
            percent=round(progress, 1),
            message=(
                f"正在测试{direction_label}，端口 {port}"
                f"（第 {attempt_index}/{len(ports)} 个）"
            ),
        )
        command = [
            *shlex.split(context.command),
            "-c", host,
            "-p", str(port),
            "--connect-timeout", str(context.connect_timeout_ms),
            "-t", str(duration),
            "-P", str(parallel),
            "-J",
        ]
        if reverse:
            command.append("-R")
        try:
            completed = _execute_iperf_command(
                context,
                command,
                max(1, min(duration + 5, remaining)),
                task_id,
                cancel_event,
            )
        except FileNotFoundError:
            raise context.error_factory(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "找不到 iPerf3 客户端，请重新运行 deploy.sh 构建 platform-api 镜像",
            )
        except subprocess.TimeoutExpired:
            attempts.append(f"{port}: 超时")
            continue

        output = (completed.stdout or "").strip()
        error = (completed.stderr or "").strip()
        if completed.returncode == 0:
            try:
                result = iperf.parse_iperf3_json(output)
                _set_iperf_status(
                    percent=round(
                        ((direction_index + 1) / direction_total) * 100,
                        1,
                    ),
                    message=f"{direction_label}完成，端口 {port}",
                )
                return {**result, "port": port}
            except (ValueError, TypeError) as exc:
                attempts.append(f"{port}: {exc}")
        else:
            attempts.append(
                f"{port}: {iperf._iperf_error_summary(output, error, completed.returncode)}"
            )
    detail = "；".join(attempts[-4:]) or "没有端口完成测试"
    raise context.error_factory(
        HTTPStatus.BAD_GATEWAY,
        f"iperf3 测速失败：{detail}",
    )


def run_iperf_test(
    context: IperfRuntimeContext,
    data: dict,
    task_id: str = "",
    cancel_event: threading.Event | None = None,
) -> dict:
    host = context.validate_network_host(
        data.get("server") or "speedtest.hkg12.hk.leaseweb.net",
        "测速服务器",
    )
    if not context.allow_internal and iperf._iperf_target_is_internal(host):
        raise context.error_factory(
            HTTPStatus.BAD_REQUEST,
            "测速目标是内网地址。默认仅允许公网节点；确需测内网请在 .env 设置 "
            "PLATFORM_IPERF3_ALLOW_INTERNAL=true 后重新应用配置",
        )
    ports = iperf.parse_port_range(
        data.get("ports"),
        "5201-5210",
        10,
        error_factory=context.error_factory,
    )
    try:
        duration = int(data.get("duration") or 10)
        parallel = int(data.get("parallel") or 10)
    except (TypeError, ValueError):
        raise context.error_factory(
            HTTPStatus.BAD_REQUEST,
            "测试时长和并发数必须是整数",
        )
    if not 3 <= duration <= 30:
        raise context.error_factory(
            HTTPStatus.BAD_REQUEST,
            "测试时长必须在 3-30 秒之间",
        )
    if not 1 <= parallel <= 20:
        raise context.error_factory(
            HTTPStatus.BAD_REQUEST,
            "并发数必须在 1-20 之间",
        )
    direction = str(data.get("direction") or "both").strip().lower()
    if direction not in ("upload", "download", "both"):
        raise context.error_factory(HTTPStatus.BAD_REQUEST, "测速方向无效")
    if not IPERF_LOCK.acquire(blocking=False):
        raise context.error_factory(
            HTTPStatus.CONFLICT,
            "已有 iperf3 测速正在运行，请稍后再试",
        )
    directions = []
    if direction in ("upload", "both"):
        directions.append(("upload", False))
    if direction in ("download", "both"):
        directions.append(("download", True))
    started_monotonic = context.monotonic()
    deadline = started_monotonic + context.timeout
    _set_iperf_status(
        ok=True,
        state="running",
        phase="preparing",
        server=host,
        currentPort=None,
        attempt=0,
        totalAttempts=len(ports),
        direction="",
        directionIndex=0,
        directionTotal=len(directions),
        percent=0,
        startedAt=int(context.clock()),
        finishedAt=None,
        _startedMonotonic=started_monotonic,
        elapsedSeconds=0,
        # One cap covers the entire task. A blocked public node must not consume
        # the timeout once for upload and then a second time for download.
        maxSeconds=context.timeout,
        message="正在准备测速",
        taskId=task_id or None,
    )
    try:
        results = []
        preferred_ports = list(ports)
        for direction_index, (direction_name, reverse) in enumerate(directions):
            _set_iperf_status(directionIndex=direction_index + 1)
            result = _run_iperf_direction(
                context,
                host,
                preferred_ports,
                duration,
                parallel,
                reverse,
                deadline,
                direction_index,
                len(directions),
                task_id,
                cancel_event,
            )
            results.append({"direction": direction_name, **result})
            preferred_ports = [
                result["port"],
                *[port for port in ports if port != result["port"]],
            ]
        payload = {
            "ok": True,
            "protocol": "TCP",
            "server": host,
            "requestedPorts": (
                f"{ports[0]}-{ports[-1]}" if len(ports) > 1 else str(ports[0])
            ),
            "duration": duration,
            "parallel": parallel,
            "results": results,
            "taskId": task_id or None,
        }
        _set_iperf_status(
            state="complete",
            phase="complete",
            percent=100,
            finishedAt=int(context.clock()),
            _startedMonotonic=None,
            elapsedSeconds=round(context.monotonic() - started_monotonic, 1),
            message="测速完成",
        )
        return payload
    except IperfCancelled as exc:
        _set_iperf_status(
            state="cancelled",
            phase="cancelled",
            finishedAt=int(context.clock()),
            _startedMonotonic=None,
            elapsedSeconds=round(context.monotonic() - started_monotonic, 1),
            message=str(exc),
        )
        raise
    except context.error_factory as exc:
        _set_iperf_status(
            state="failed",
            phase="failed",
            finishedAt=int(context.clock()),
            _startedMonotonic=None,
            elapsedSeconds=round(context.monotonic() - started_monotonic, 1),
            message=exc.payload.get("error", str(exc)),
        )
        raise
    except Exception as exc:
        _set_iperf_status(
            state="failed",
            phase="failed",
            finishedAt=int(context.clock()),
            _startedMonotonic=None,
            elapsedSeconds=round(context.monotonic() - started_monotonic, 1),
            message=str(exc),
        )
        raise
    finally:
        IPERF_LOCK.release()


def _iperf_task_worker(
    context: IperfRuntimeContext,
    task_id: str,
    data: dict,
    cancel_event: threading.Event,
) -> None:
    global IPERF_ACTIVE_TASK_ID
    try:
        result = run_iperf_test(context, data, task_id, cancel_event)
        with IPERF_STATUS_LOCK:
            status = dict(IPERF_STATUS)
        final = {**status, **result, "state": "complete", "phase": "complete"}
    except IperfCancelled as exc:
        with IPERF_STATUS_LOCK:
            status = dict(IPERF_STATUS)
        final = {
            **status,
            "ok": False,
            "taskId": task_id,
            "state": "cancelled",
            "phase": "cancelled",
            "message": str(exc),
        }
    except context.error_factory as exc:
        with IPERF_STATUS_LOCK:
            status = dict(IPERF_STATUS)
        final = {
            **status,
            "ok": False,
            "taskId": task_id,
            "state": "failed",
            "phase": "failed",
            "message": exc.payload.get("error", str(exc)),
        }
    except Exception as exc:
        with IPERF_STATUS_LOCK:
            status = dict(IPERF_STATUS)
        final = {
            **status,
            "ok": False,
            "taskId": task_id,
            "state": "failed",
            "phase": "failed",
            "message": str(exc),
        }
    final.pop("_startedMonotonic", None)
    final.setdefault("finishedAt", int(context.clock()))
    with IPERF_TASKS_LOCK:
        IPERF_TASKS[task_id] = final
        IPERF_CANCEL_EVENTS.pop(task_id, None)
        if IPERF_ACTIVE_TASK_ID == task_id:
            IPERF_ACTIVE_TASK_ID = ""
    _save_iperf_history(context, final)


def start_iperf_task(context: IperfRuntimeContext, data: dict) -> dict:
    global IPERF_ACTIVE_TASK_ID
    with IPERF_TASKS_LOCK:
        if IPERF_ACTIVE_TASK_ID:
            active = IPERF_TASKS.get(IPERF_ACTIVE_TASK_ID) or {}
            if active.get("state") in ("queued", "running"):
                raise context.error_factory(
                    HTTPStatus.CONFLICT,
                    "已有 iPerf3 测速正在运行，请先等待或停止当前任务",
                    taskId=IPERF_ACTIVE_TASK_ID,
                )
        task_id = f"iperf-{int(context.clock())}-{context.token_hex(3)}"
        cancel_event = threading.Event()
        task = {
            "ok": True,
            "taskId": task_id,
            "state": "queued",
            "phase": "preparing",
            "percent": 0,
            "startedAt": int(context.clock()),
            "maxSeconds": context.timeout,
            "message": "测速任务已创建",
        }
        IPERF_TASKS[task_id] = task
        IPERF_CANCEL_EVENTS[task_id] = cancel_event
        IPERF_ACTIVE_TASK_ID = task_id
        # Keep active plus a small recent window in memory. Persistent history
        # is separately capped at five entries.
        if len(IPERF_TASKS) > 20:
            removable = [
                key for key, value in IPERF_TASKS.items()
                if key != task_id and value.get("state") not in ("queued", "running")
            ]
            for key in removable[:len(IPERF_TASKS) - 20]:
                IPERF_TASKS.pop(key, None)
    threading.Thread(
        target=_iperf_task_worker,
        args=(context, task_id, dict(data or {}), cancel_event),
        name=f"iperf-{task_id[-6:]}",
        daemon=True,
    ).start()
    return task


def stop_iperf_task(context: IperfRuntimeContext, data: dict) -> dict:
    task_id = str((data or {}).get("taskId") or "").strip()
    if not task_id:
        raise context.error_factory(
            HTTPStatus.BAD_REQUEST,
            "缺少 iPerf3 任务编号",
        )
    with IPERF_TASKS_LOCK:
        task = dict(IPERF_TASKS.get(task_id) or {})
        cancel_event = IPERF_CANCEL_EVENTS.get(task_id)
    if not task:
        raise context.error_factory(
            HTTPStatus.NOT_FOUND,
            "iPerf3 任务不存在或已过期",
        )
    if task.get("state") not in ("queued", "running") or cancel_event is None:
        return {
            "ok": True,
            "taskId": task_id,
            "state": task.get("state"),
            "message": "任务已经结束",
        }
    cancel_event.set()
    with IPERF_PROCESS_LOCK:
        process = IPERF_PROCESSES.get(task_id)
    if process and process.poll() is None:
        process.terminate()
    return {
        "ok": True,
        "taskId": task_id,
        "state": "stopping",
        "message": "正在停止测速",
    }
