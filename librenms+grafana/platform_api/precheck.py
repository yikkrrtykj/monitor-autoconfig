"""Pre-match readiness diagnostics for the platform API."""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Callable
import urllib.error
import urllib.request
from urllib.parse import quote


# The target generator refreshes player_targets.json on a short loop (default
# 60s). A status snapshot older than this cannot be trusted to describe the
# current event composition, so it is treated as unavailable instead.
TARGETS_STATUS_MAX_AGE_SECONDS = 600
WIRED_NETWORK = "wired"
WIRELESS_NETWORK = "wireless"


@dataclass(frozen=True)
class PrecheckContext:
    prom_url: str
    grafana_url: str
    bridge_url: str
    bigscreen_url: str
    librenms_url: str
    player_targets_url: str
    config_issues: Callable[[], list[dict]]


def _http_json(url: str, timeout: int = 5):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8") or "{}")


def _prom_query(context: PrecheckContext, expr: str):
    return _http_json(
        f"{context.prom_url}/api/v1/query?query={quote(expr)}"
    ).get("data", {}).get("result", [])


def _target_network(item) -> str:
    """Return the file_sd ``network`` label of a probe result."""
    metric = (item or {}).get("metric") or {}
    return str(metric.get("network") or "").strip().lower()


def _probe_online(item) -> bool:
    """Return whether an instant ``probe_success`` sample answered."""
    value = (item or {}).get("value") or [None, "0"]
    return len(value) > 1 and value[1] == "1"


def _target_status_counts(status) -> dict | None:
    """Return trusted generator-declared target counts, or ``None``.

    Only the target generator can declare which networks the current event is
    supposed to have, so its ``targets.wired`` count is what separates a real
    wireless-only event from wired targets that Prometheus failed to return.
    Missing, errored, malformed or stale payloads are never trusted.
    """
    if not isinstance(status, dict) or status.get("error"):
        return None
    targets = status.get("targets")
    if not isinstance(targets, dict):
        return None
    try:
        counts = {
            "wired": int(targets["wired"]),
            "wireless": int(targets["wireless"]),
            "other": int(targets["other"]),
        }
        reference = float(status["now"])
        updated_at = float(status["updated_at"])
    except (KeyError, TypeError, ValueError):
        return None
    if min(counts.values()) < 0:
        return None
    if reference - updated_at > TARGETS_STATUS_MAX_AGE_SECONDS:
        return None
    return counts


def _player_checks(players: list[dict], status_counts: dict | None) -> list[tuple[str, str]]:
    """Build readiness checks for wired seats and the wireless backup network.

    Labels are classified explicitly: only ``network="wired"`` series count as
    seats, unlabeled/unknown targets are reported instead of being promoted to
    wired. When the generator declares wired targets but Prometheus returns
    fewer (or none), seats are never reported green: the seats are unverifiable
    and the check fails closed. Only a fresh, healthy status snapshot with
    ``targets.wired == 0`` is accepted as a genuine wireless-only event.
    """
    wired = [x for x in players if _target_network(x) == WIRED_NETWORK]
    wireless = [x for x in players if _target_network(x) == WIRELESS_NETWORK]
    ambiguous = [
        x for x in players
        if _target_network(x) not in (WIRED_NETWORK, WIRELESS_NETWORK)
    ]
    expected_wired = None if status_counts is None else status_counts["wired"]

    if not wired and not wireless:
        # No explicit labels at all: keep the legacy merged contract.
        online = sum(1 for x in ambiguous if _probe_online(x))
        total = len(ambiguous)
        if total == 0:
            return [("bad", "选手机位监控目标为 0，不能确认比赛网络状态")]
        if online != total:
            return [("bad", f"选手机位仅 {online}/{total} 在线")]
        return [("good", f"选手机位 {online}/{total} 全部在线")]

    checks: list[tuple[str, str]] = []
    if wired:
        wired_online = sum(1 for x in wired if _probe_online(x))
        if wired_online != len(wired):
            checks.append(("bad", f"选手机位仅 {wired_online}/{len(wired)} 在线"))
        elif expected_wired is None:
            checks.append((
                "warn",
                f"选手机位 {wired_online}/{len(wired)} 在线，但目标状态不可用，无法确认完整性",
            ))
        elif expected_wired == 0:
            checks.append((
                "warn",
                f"选手机位 {wired_online}/{len(wired)} 在线，但目标状态声明没有有线目标",
            ))
        elif len(wired) < expected_wired:
            checks.append((
                "bad",
                f"选手机位数据不完整：Prometheus 仅返回 {len(wired)}/{expected_wired} 个有线目标",
            ))
        else:
            checks.append(("good", f"选手机位 {wired_online}/{len(wired)} 全部在线"))
    elif expected_wired is None:
        checks.append((
            "warn",
            f"选手机位无法判定：只有 {len(wireless)} 个无线目标，且目标状态不可用",
        ))
    elif expected_wired > 0:
        checks.append((
            "bad",
            f"选手机位数据缺失：目标状态声明 {expected_wired} 个有线目标，Prometheus 未返回有线目标",
        ))
    else:
        # Status declares no wired targets: a genuine wireless-only event keeps
        # the legacy contract of treating wireless targets as the seats.
        wireless_online = sum(1 for x in wireless if _probe_online(x))
        if wireless_online != len(wireless):
            checks.append(("bad", f"选手机位仅 {wireless_online}/{len(wireless)} 在线"))
        else:
            checks.append(("good", f"选手机位 {wireless_online}/{len(wireless)} 全部在线"))

    if wireless and (wired or expected_wired != 0):
        wireless_online = sum(1 for x in wireless if _probe_online(x))
        # 备用链路的部分掉线属于常态，只有整体不可达才升级为警告。
        checks.append((
            "good" if wireless_online else "warn",
            f"无线备用网 {wireless_online}/{len(wireless)} 在线",
        ))
    if ambiguous:
        checks.append((
            "warn",
            f"选手目标有 {len(ambiguous)} 个缺少或未知 network 标签，未计入机位判定",
        ))
    return checks


def run_precheck(context: PrecheckContext) -> dict:
    """Run the console readiness check against services on the stack network."""
    checks: list[dict] = []

    def add(level, text):
        checks.append({"level": level, "text": text})

    # 1. Prometheus 可达 + 抓取目标
    try:
        ups = _prom_query(context, "up")
        online = sum(1 for x in ups if (x.get("value") or [None, "0"])[1] == "1")
        failed = [x for x in ups if (x.get("value") or [None, "0"])[1] != "1"]
        if not ups:
            add("bad", "监控数据服务可连接，但尚无采集项。")
        elif failed:
            names = "、".join(
                (x.get("metric") or {}).get("job", "?") + ":" + (x.get("metric") or {}).get("instance", "?")
                for x in failed[:8]
            )
            add("bad", f"监控数据采集有 {len(failed)} 项失败（{online}/{len(ups)} 项可用）：{names}")
        else:
            add("good", f"监控数据采集正常，{online}/{len(ups)} 项可用。")
    except Exception as exc:
        add("bad", "监控数据服务无法连接。")
        # Without Prometheus the rest can't be judged.
        return _precheck_result(checks)

    # 2. 基础设施设备在线率（ping）
    try:
        infra = _prom_query(context, 'probe_success{job=~"infra-.*"}')
        down = [x for x in infra if (x.get("value") or [None, "1"])[1] != "1"]
        if not infra:
            add("warn", "还没有基础设施 ping 目标（配置未填或未应用？）")
        elif down:
            names = "、".join((x.get("metric") or {}).get("display_name") or (x.get("metric") or {}).get("instance", "?") for x in down[:8])
            add("bad", f"{len(down)} 台基础设施设备离线：{names}")
        else:
            add("good", f"基础设施 {len(infra)} 台全部在线")
    except Exception as exc:
        add("warn", f"无法查询设备在线状态：{exc}")

    # 3. 选手机位 ping 目标
    # 目标生成器的 /status 只在这里取一次：它声明本场应有哪些网络目标，
    # 用于区分真正的纯无线赛事和有线目标采集丢失；第 6 项复用同一份数据。
    target_status = None
    target_status_error = None
    try:
        target_status = _http_json(f"{context.player_targets_url}/status")
    except Exception as exc:
        target_status_error = exc
    try:
        players = _prom_query(context, 'probe_success{job="player-ping"}')
        for level, text in _player_checks(players, _target_status_counts(target_status)):
            add(level, text)
    except Exception as exc:
        add("warn", f"无法查询选手目标：{exc}")

    # 4. Grafana
    try:
        _http_json(f"{context.grafana_url}/api/health")
        add("good", "Grafana 正常")
    except Exception as exc:
        add("bad", f"Grafana 不可达（{context.grafana_url}）：{exc}")

    # 5. 飞书告警链路
    try:
        try:
            bridge_health = _http_json(f"{context.bridge_url}/health")
        except urllib.error.HTTPError as exc:
            # 看门狗线程死亡时桥接按 503 返回同样的 JSON——读出来照常展示细节
            bridge_health = json.loads(exc.read().decode("utf-8", errors="replace") or "{}")
        watcher_names = {
            "device-online": "设备上线",
            "sysname-change": "设备名称变更",
            "device-auto-delete": "自动清理",
            "isp-bandwidth": "ISP 带宽",
            "interconnect": "聚合链路",
            "device-down": "设备离线",
            "unifi-ap": "无线 AP",
            "device-resources": "设备资源",
            "syslog": "网络事件",
        }
        if not bridge_health.get("ready"):
            details = []
            if not bridge_health.get("tokenConfigured") and not bridge_health.get("dryRun"):
                details.append("未配置飞书 Token")
            if bridge_health.get("deadWatchers"):
                details.append("、".join(
                    f"{watcher_names.get(name, name)}监测已停止"
                    for name in bridge_health["deadWatchers"]
                ))
            add("bad", "告警服务未就绪：" + ("；".join(details) or "健康检查未通过"))
        else:
            watcher_errors = [
                f"{watcher_names.get(name, name)}：{state.get('lastError')}"
                for name, state in (bridge_health.get("watchers") or {}).items()
                if state.get("lastError")
            ]
            if watcher_errors:
                add("warn", "告警监测仍在运行，但最近一次检查失败：" + "；".join(watcher_errors[:4]))
            else:
                add("good", "告警监测服务正常")
    except Exception as exc:
        add("bad", f"告警服务不可达：{exc}")

    # 6. 用户入口与目标生成器
    try:
        with urllib.request.urlopen(f"{context.bigscreen_url}/", timeout=5) as resp:
            resp.read(1024)
        add("good", "赛事大屏入口正常")
    except Exception as exc:
        add("bad", f"赛事大屏不可达：{exc}")

    # 目标状态已在第 3 项取到，这里不重复请求。
    if target_status_error is not None:
        add("bad", f"选手监控地址更新异常：{target_status_error}")
    else:
        try:
            target_count = int((target_status.get("targets") or {}).get("total") or 0)
            if target_status.get("error"):
                add("bad", f"选手监控地址更新异常：{target_status.get('error')}")
            elif target_count <= 0:
                add("bad", "尚未识别到可监控的选手地址。")
            else:
                add("good", f"选手监控地址更新正常，共 {target_count} 个地址。")
        except Exception as exc:
            add("bad", f"选手监控地址更新异常：{exc}")

    try:
        with urllib.request.urlopen(f"{context.librenms_url}/", timeout=5) as resp:
            resp.read(1024)
        add("good", "LibreNMS Web 正常")
    except Exception as exc:
        add("bad", f"LibreNMS 不可达：{exc}")

    # 7. 配置阻塞项
    try:
        issues = context.config_issues()
        blocking = [i for i in issues if i.get("level") == "bad"]
        if blocking:
            for i in blocking[:6]:
                add("bad", f"配置缺项：{i.get('message')}（{i.get('path')}）")
        else:
            add("good", "配置无阻塞项")
    except Exception as exc:
        add("warn", f"配置检查失败：{exc}")

    return _precheck_result(checks)


def _precheck_result(checks: list[dict]) -> dict:
    icon = {"good": "✓", "warn": "⚠", "bad": "✗"}
    passed = sum(1 for c in checks if c["level"] == "good")
    warned = sum(1 for c in checks if c["level"] == "warn")
    failed = sum(1 for c in checks if c["level"] == "bad")
    verdict = "bad" if failed else ("warn" if warned else "good")
    output = "\n".join(f"  {icon[c['level']]} {c['text']}" for c in checks)
    return {"ok": True, "verdict": verdict, "pass": passed, "warn": warned, "fail": failed, "output": output}
