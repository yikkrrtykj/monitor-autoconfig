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
STATUS_TRUSTED = "trusted"
STATUS_LEGACY = "legacy"
STATUS_UNAVAILABLE = "unavailable"


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


def _target_status(status) -> tuple[str, dict | None]:
    """Classify the target generator status payload.

    A payload with a fresh ``wired``/``wireless``/``other`` breakdown is the
    authoritative declaration of what the current event should monitor. A
    payload with only ``total`` is the pre-network-label structure: it cannot
    declare per-network expectations, so it only enables the legacy merged
    contract. Errored, malformed or stale payloads are ``unavailable`` and
    never turn a check into good.
    """
    if not isinstance(status, dict) or status.get("error"):
        return STATUS_UNAVAILABLE, None
    targets = status.get("targets")
    if not isinstance(targets, dict):
        return STATUS_UNAVAILABLE, None
    if not any(key in targets for key in ("wired", "wireless", "other")):
        if "total" in targets:
            return STATUS_LEGACY, None
        return STATUS_UNAVAILABLE, None
    try:
        counts = {
            "wired": int(targets["wired"]),
            "wireless": int(targets["wireless"]),
            "other": int(targets["other"]),
        }
        reference = float(status["now"])
        updated_at = float(status["updated_at"])
    except (KeyError, TypeError, ValueError):
        return STATUS_UNAVAILABLE, None
    if min(counts.values()) < 0:
        return STATUS_UNAVAILABLE, None
    if reference - updated_at > TARGETS_STATUS_MAX_AGE_SECONDS:
        return STATUS_UNAVAILABLE, None
    return STATUS_TRUSTED, counts


def _classify_player_targets(players):
    """Split probe series into explicit, legacy and malformed label groups."""
    wired = [x for x in players if _target_network(x) == WIRED_NETWORK]
    wireless = [x for x in players if _target_network(x) == WIRELESS_NETWORK]
    legacy = [x for x in players if not _target_network(x)]
    unknown = [
        x for x in players
        if _target_network(x) not in (WIRED_NETWORK, WIRELESS_NETWORK, "")
    ]
    return wired, wireless, legacy, unknown


def _ambiguous_label_text(legacy, unknown) -> str:
    parts = []
    if legacy:
        parts.append(f"{len(legacy)} 个缺少 network 标签")
    if unknown:
        parts.append(f"{len(unknown)} 个未知 network 标签")
    return "选手目标有 " + "、".join(parts) + "，未计入机位判定"


def _wired_seat_check(wired, expected_wired):
    if not wired:
        return (
            "bad",
            f"选手机位数据缺失：目标状态声明 {expected_wired} 个有线目标，Prometheus 未返回有线目标",
        )
    online = sum(1 for x in wired if _probe_online(x))
    if len(wired) < expected_wired:
        detail = f"（其中 {online} 个在线）" if online != len(wired) else ""
        return (
            "bad",
            f"选手机位数据不完整：Prometheus 仅返回 {len(wired)}/{expected_wired} 个有线目标{detail}",
        )
    if len(wired) > expected_wired:
        if online != len(wired):
            return (
                "bad",
                f"选手机位仅 {online}/{len(wired)} 在线（目标状态声明 {expected_wired} 个有线目标）",
            )
        return (
            "warn",
            f"选手机位数量异常：Prometheus 返回 {len(wired)} 个有线目标，目标状态声明 {expected_wired} 个",
        )
    if online != len(wired):
        return ("bad", f"选手机位仅 {online}/{len(wired)} 在线")
    return ("good", f"选手机位 {online}/{len(wired)} 全部在线")


def _wireless_seat_check(wireless, expected_wireless):
    if not wireless:
        return (
            "bad",
            f"选手机位数据缺失：目标状态声明 {expected_wireless} 个无线目标，Prometheus 未返回无线目标",
        )
    online = sum(1 for x in wireless if _probe_online(x))
    if len(wireless) < expected_wireless:
        detail = f"（其中 {online} 个在线）" if online != len(wireless) else ""
        return (
            "bad",
            f"选手机位数据不完整：Prometheus 仅返回 {len(wireless)}/{expected_wireless} 个无线目标{detail}",
        )
    if len(wireless) > expected_wireless:
        if online != len(wireless):
            return (
                "bad",
                f"选手机位仅 {online}/{len(wireless)} 在线（目标状态声明 {expected_wireless} 个无线目标）",
            )
        return (
            "warn",
            f"选手机位数量异常：Prometheus 返回 {len(wireless)} 个无线目标，目标状态声明 {expected_wireless} 个",
        )
    if online != len(wireless):
        return ("bad", f"选手机位仅 {online}/{len(wireless)} 在线")
    return ("good", f"选手机位 {online}/{len(wireless)} 全部在线")


def _wireless_backup_check(wireless, expected_wireless):
    online = sum(1 for x in wireless if _probe_online(x))
    if expected_wireless is not None and len(wireless) < expected_wireless:
        return (
            "warn",
            f"无线备用网目标不完整：Prometheus 返回 {len(wireless)}/{expected_wireless} 个无线目标（{online} 个在线）",
        )
    if expected_wireless is not None and len(wireless) > expected_wireless:
        return (
            "warn",
            f"无线备用网目标异常：Prometheus 返回 {len(wireless)} 个无线目标，目标状态声明 {expected_wireless} 个（{online} 个在线）",
        )
    if online:
        return ("good", f"无线备用网 {online}/{len(wireless)} 在线")
    return ("warn", f"无线备用网 0/{len(wireless)} 在线")


def _trusted_player_checks(wired, wireless, expected_wired, expected_wireless):
    if expected_wired > 0:
        checks = [_wired_seat_check(wired, expected_wired)]
        if wireless:
            checks.append(_wireless_backup_check(wireless, expected_wireless))
        return checks
    if expected_wireless > 0:
        if wired:
            checks = [(
                "warn",
                f"选手机位数量异常：目标状态声明 0 个有线目标，Prometheus 返回 {len(wired)} 个有线目标",
            )]
            if wireless:
                checks.append(_wireless_backup_check(wireless, expected_wireless))
            return checks
        return [_wireless_seat_check(wireless, expected_wireless)]
    observed = len(wired) + len(wireless)
    if observed:
        return [(
            "warn",
            f"选手机位数量异常：目标状态声明没有选手目标，Prometheus 返回 {observed} 个",
        )]
    return [("warn", "选手机位无法判定：目标状态未声明任何选手目标")]


def _unverified_player_checks(wired, wireless, status_kind):
    reason = (
        "目标状态未声明网络目标"
        if status_kind == STATUS_LEGACY
        else "目标状态不可用"
    )
    if wired:
        online = sum(1 for x in wired if _probe_online(x))
        if online != len(wired):
            checks = [("bad", f"选手机位仅 {online}/{len(wired)} 在线")]
        else:
            checks = [(
                "warn",
                f"选手机位 {online}/{len(wired)} 在线，但{reason}，无法确认完整性",
            )]
        if wireless:
            checks.append(_wireless_backup_check(wireless, None))
        return checks
    if wireless:
        return [
            ("warn", f"选手机位无法判定：只有 {len(wireless)} 个无线目标，且{reason}"),
            _wireless_backup_check(wireless, None),
        ]
    return [("warn", f"选手机位无法判定：{reason}，且没有可识别的 network 标签")]


def _player_checks(
    players: list[dict],
    status_kind: str,
    status_counts: dict | None,
) -> list[tuple[str, str]]:
    """Build readiness checks for wired seats and the wireless backup network.

    Priority: a fresh generator declaration > verifiable Prometheus labels >
    the legacy merged contract. Only explicit ``network="wired"`` series count
    as seats; unlabeled/unknown targets are reported and never promoted to
    wired. Declared counts are verified against observed series so missing or
    extra targets cannot be reported green. Only a legacy status structure with
    entirely unlabeled targets keeps the old merged behaviour.
    """
    if not players:
        return [("bad", "选手机位监控目标为 0，不能确认比赛网络状态")]

    wired, wireless, legacy, unknown = _classify_player_targets(players)
    expected_wired = status_counts["wired"] if status_counts else None
    expected_wireless = status_counts["wireless"] if status_counts else None

    if status_kind == STATUS_LEGACY and not wired and not wireless and not unknown:
        # Genuine legacy deployment: old status shape and no network labels.
        online = sum(1 for x in legacy if _probe_online(x))
        if online != len(legacy):
            return [("bad", f"选手机位仅 {online}/{len(legacy)} 在线")]
        return [("good", f"选手机位 {online}/{len(legacy)} 全部在线")]

    if status_kind == STATUS_TRUSTED:
        checks = _trusted_player_checks(
            wired, wireless, expected_wired, expected_wireless
        )
    else:
        checks = _unverified_player_checks(wired, wireless, status_kind)

    if legacy or unknown:
        checks.append(("warn", _ambiguous_label_text(legacy, unknown)))
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
        status_kind, status_counts = _target_status(target_status)
        players = _prom_query(context, 'probe_success{job="player-ping"}')
        for level, text in _player_checks(players, status_kind, status_counts):
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
