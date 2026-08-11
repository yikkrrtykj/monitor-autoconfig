"""Pre-match readiness diagnostics for the platform API."""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Callable
import urllib.error
import urllib.request
from urllib.parse import quote


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
            add("bad", "Prometheus 可达，但没有任何抓取目标")
        elif failed:
            names = "、".join(
                (x.get("metric") or {}).get("job", "?") + ":" + (x.get("metric") or {}).get("instance", "?")
                for x in failed[:8]
            )
            add("bad", f"Prometheus 有 {len(failed)} 个抓取目标失败（{online}/{len(ups)} 在线）：{names}")
        else:
            add("good", f"Prometheus 正常，抓取目标 {online}/{len(ups)} 全部在线")
    except Exception as exc:
        add("bad", f"Prometheus 不可达（{context.prom_url}）：{exc}")
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
    try:
        players = _prom_query(context, 'probe_success{job="player-ping"}')
        online = sum(1 for x in players if (x.get("value") or [None, "0"])[1] == "1")
        if not players:
            add("bad", "选手机位监控目标为 0，不能确认比赛网络状态")
        elif online != len(players):
            add("bad", f"选手机位仅 {online}/{len(players)} 在线")
        else:
            add("good", f"选手机位 {online}/{len(players)} 全部在线")
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
        if not bridge_health.get("ready"):
            details = []
            if not bridge_health.get("tokenConfigured") and not bridge_health.get("dryRun"):
                details.append("未配置飞书 Token")
            if bridge_health.get("deadWatchers"):
                details.append("后台线程已停止：" + ",".join(bridge_health["deadWatchers"]))
            add("bad", "告警服务未就绪：" + ("；".join(details) or "健康检查未通过"))
        else:
            watcher_errors = [
                f"{name}: {state.get('lastError')}"
                for name, state in (bridge_health.get("watchers") or {}).items()
                if state.get("lastError")
            ]
            if watcher_errors:
                add("warn", "告警服务线程存活，但最近轮询失败：" + "；".join(watcher_errors[:4]))
            else:
                add("good", "告警服务及后台线程正常")
    except Exception as exc:
        add("bad", f"告警服务不可达：{exc}")

    # 6. 用户入口与目标生成器
    try:
        with urllib.request.urlopen(f"{context.bigscreen_url}/", timeout=5) as resp:
            resp.read(1024)
        add("good", "赛事大屏入口正常")
    except Exception as exc:
        add("bad", f"赛事大屏不可达：{exc}")

    try:
        target_status = _http_json(f"{context.player_targets_url}/status")
        target_count = int((target_status.get("targets") or {}).get("total") or 0)
        if target_status.get("error"):
            add("bad", f"选手目标生成器异常：{target_status.get('error')}")
        elif target_count <= 0:
            add("bad", "选手目标生成器尚未生成任何目标")
        else:
            add("good", f"选手目标生成器正常，共 {target_count} 个目标")
    except Exception as exc:
        add("bad", f"选手目标生成器不可达：{exc}")

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
