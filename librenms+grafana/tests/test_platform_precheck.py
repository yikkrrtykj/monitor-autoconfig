import io
import json
import urllib.error

from platform_api import precheck

from .test_platform_transactions import load_api, seed


class _Response:
    status = 200

    def __init__(self, data=b"OK", reads=None):
        self.data = data
        self.reads = reads

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, limit=-1):
        if self.reads is not None:
            self.reads.append(limit)
        return self.data


def _sample(job, instance, value="1"):
    return {"metric": {"job": job, "instance": instance}, "value": [0, value]}


def _context(config_issues=lambda: []):
    return precheck.PrecheckContext(
        prom_url="http://prometheus:9090",
        grafana_url="http://grafana:3000",
        bridge_url="http://alertmanager-feishu-bridge:5005",
        bigscreen_url="http://bigscreen",
        librenms_url="http://librenms:8000",
        player_targets_url="http://player-targets:9199",
        config_issues=config_issues,
    )


def _healthy_prom_query(_context, expr):
    if expr == "up":
        return [_sample("prometheus", "prometheus:9090")]
    if expr.startswith('probe_success{job=~"infra-'):
        return [_sample("infra-core-ping", "core")]
    if expr == 'probe_success{job="player-ping"}':
        return [_sample("player-ping", "player-1")]
    return []


def _mock_external_services(monkeypatch, bridge_ready=True, target_count=1, status_payload=None):
    def http_json(url, timeout=5):
        if url.endswith("/health") and "alertmanager" in url:
            return {
                "ok": True,
                "ready": bridge_ready,
                "tokenConfigured": bridge_ready,
                "deadWatchers": [] if bridge_ready else ["device-down"],
                "watchers": {},
            }
        if url.endswith("/status"):
            if status_payload is not None:
                return status_payload
            return {"ok": True, "targets": {"total": target_count}}
        return {"ok": True}

    monkeypatch.setattr(precheck, "_http_json", http_json)
    monkeypatch.setattr(
        precheck.urllib.request,
        "urlopen",
        lambda *args, **kwargs: _Response(),
    )


def test_precheck_module_and_entrypoint_dependency_assembly(tmp_path):
    api = load_api(tmp_path)
    seed(api)

    dependency = api._write_api_dependencies().run_precheck
    context = dependency.args[0]

    assert precheck.PrecheckContext.__module__ == "platform_api.precheck"
    assert precheck.run_precheck.__module__ == "platform_api.precheck"
    assert dependency.func is precheck.run_precheck
    assert context.prom_url == api.PRECHECK_PROM_URL
    assert context.grafana_url == api.PRECHECK_GRAFANA_URL
    assert context.bridge_url == api.BRIDGE_URL
    assert context.bigscreen_url == api.PRECHECK_BIGSCREEN_URL
    assert context.librenms_url == api.PRECHECK_LIBRENMS_URL
    assert context.player_targets_url == api.PRECHECK_PLAYER_TARGETS_URL
    assert context.config_issues() == api.validate_config(
        api.platform_event_config.parse_config_text(
            api.platform_event_config.read_config_text(api._event_config_context())
        )
    )
    assert not hasattr(api, "run_precheck")
    assert not hasattr(api, "_prom_query")
    assert not hasattr(api, "_precheck_result")


def test_http_json_keeps_json_decode_and_timeout_defaults(monkeypatch):
    calls = []

    def urlopen(url, timeout):
        calls.append((url, timeout))
        return _Response(json.dumps({"ok": True}).encode("utf-8"))

    monkeypatch.setattr(precheck.urllib.request, "urlopen", urlopen)

    assert precheck._http_json("http://service/health") == {"ok": True}
    assert precheck._http_json("http://service/status", timeout=11) == {"ok": True}
    assert calls == [
        ("http://service/health", 5),
        ("http://service/status", 11),
    ]


def test_prom_query_keeps_url_encoding_and_result_shape(monkeypatch):
    calls = []
    expected = [_sample("player-ping", "player-1")]

    def http_json(url, timeout=5):
        calls.append((url, timeout))
        return {"data": {"result": expected}}

    monkeypatch.setattr(precheck, "_http_json", http_json)

    result = precheck._prom_query(
        _context(),
        'probe_success{job="player-ping"}',
    )

    assert result == expected
    assert calls == [(
        "http://prometheus:9090/api/v1/query?query="
        "probe_success%7Bjob%3D%22player-ping%22%7D",
        5,
    )]


def test_prometheus_failure_short_circuits_remaining_checks(monkeypatch):
    monkeypatch.setattr(
        precheck,
        "_prom_query",
        lambda _context, _expr: (_ for _ in ()).throw(OSError("offline")),
    )
    monkeypatch.setattr(
        precheck,
        "_http_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("service probes must not run")
        ),
    )

    result = precheck.run_precheck(
        _context(
            lambda: (_ for _ in ()).throw(
                AssertionError("config validation must not run")
            )
        )
    )

    assert result == {
        "ok": True,
        "verdict": "bad",
        "pass": 0,
        "warn": 0,
        "fail": 1,
        "output": "  ✗ 监控数据服务无法连接。",
    }


def test_precheck_keeps_probe_order_urls_timeouts_and_good_result(monkeypatch):
    events = []
    reads = []

    def prom_query(context, expr):
        events.append(("prom", context.prom_url, expr))
        return _healthy_prom_query(context, expr)

    def http_json(url, timeout=5):
        events.append(("json", url, timeout))
        if "alertmanager" in url:
            return {
                "ready": True,
                "tokenConfigured": True,
                "deadWatchers": [],
                "watchers": {},
            }
        if url.endswith("/status"):
            return {"targets": {"total": 1}}
        return {"ok": True}

    def urlopen(url, timeout):
        events.append(("urlopen", url, timeout))
        return _Response(reads=reads)

    def config_issues():
        events.append(("config",))
        return []

    monkeypatch.setattr(precheck, "_prom_query", prom_query)
    monkeypatch.setattr(precheck, "_http_json", http_json)
    monkeypatch.setattr(precheck.urllib.request, "urlopen", urlopen)

    result = precheck.run_precheck(_context(config_issues))

    assert events == [
        ("prom", "http://prometheus:9090", "up"),
        ("prom", "http://prometheus:9090", 'probe_success{job=~"infra-.*"}'),
        ("json", "http://player-targets:9199/status", 5),
        ("prom", "http://prometheus:9090", 'probe_success{job="player-ping"}'),
        ("json", "http://grafana:3000/api/health", 5),
        ("json", "http://alertmanager-feishu-bridge:5005/health", 5),
        ("urlopen", "http://bigscreen/", 5),
        ("urlopen", "http://librenms:8000/", 5),
        ("config",),
    ]
    assert sum(
        1 for event in events
        if event[0] == "json" and str(event[1]).endswith("/status")
    ) == 1
    assert reads == [1024, 1024]
    assert result == {
        "ok": True,
        "verdict": "good",
        "pass": 9,
        "warn": 0,
        "fail": 0,
        "output": "\n".join([
            "  ✓ 监控数据采集正常，1/1 项可用。",
            "  ✓ 基础设施 1 台全部在线",
            "  ✓ 选手机位 1/1 全部在线",
            "  ✓ Grafana 正常",
            "  ✓ 告警监测服务正常",
            "  ✓ 赛事大屏入口正常",
            "  ✓ 选手监控地址更新正常，共 1 个地址。",
            "  ✓ LibreNMS Web 正常",
            "  ✓ 配置无阻塞项",
        ]),
    }


def test_precheck_fails_when_no_player_targets(monkeypatch):
    _mock_external_services(monkeypatch, target_count=0)
    monkeypatch.setattr(
        precheck,
        "_prom_query",
        lambda context, expr: (
            []
            if expr == 'probe_success{job="player-ping"}'
            else _healthy_prom_query(context, expr)
        ),
    )

    result = precheck.run_precheck(_context())

    assert result["verdict"] == "bad"
    assert "选手机位监控目标为 0" in result["output"]
    assert "尚未识别到可监控的选手地址。" in result["output"]


def _player_sample(instance, network=None, value="1"):
    metric = {"job": "player-ping", "instance": instance}
    if network:
        metric["network"] = network
    return {"metric": metric, "value": [0, value]}


def _players_prom_query(samples):
    def query(_context, expr):
        if expr == 'probe_success{job="player-ping"}':
            return samples
        return _healthy_prom_query(_context, expr)

    return query


def _target_status(wired=0, wireless=0, other=0, *, now=1_800_000_000, age=0, error=None):
    if error:
        return {
            "error": error,
            "targets": {"total": 0, "wired": 0, "wireless": 0, "other": 0},
        }
    return {
        "now": now,
        "updated_at": now - age,
        "targets": {
            "total": wired + wireless + other,
            "wired": wired,
            "wireless": wireless,
            "other": other,
        },
    }


def _run_player_precheck(monkeypatch, players, status_payload):
    _mock_external_services(monkeypatch, status_payload=status_payload)
    monkeypatch.setattr(precheck, "_prom_query", _players_prom_query(players))
    return precheck.run_precheck(_context())


def _check_levels(result):
    icons = {"✓": "good", "⚠": "warn", "✗": "bad"}
    levels = {}
    for line in result["output"].splitlines():
        text = line.strip()
        if not text:
            continue
        icon, _, label = text.partition(" ")
        levels[label] = icons.get(icon)
    return levels


def _line_level(result, prefix):
    for text, level in _check_levels(result).items():
        if text.startswith(prefix):
            return level, text
    return None, None


def test_precheck_counts_wireless_backup_separately_from_seats(monkeypatch):
    result = _run_player_precheck(monkeypatch, [
        _player_sample("192.168.100.11", "wired"),
        _player_sample("192.168.100.12", "wired"),
        _player_sample("192.168.101.11", "wireless"),
        _player_sample("192.168.101.12", "wireless", "0"),
    ], _target_status(wired=2, wireless=2))

    assert result["verdict"] == "good"
    assert result["fail"] == 0
    assert result["warn"] == 0
    assert "  ✓ 选手机位 2/2 全部在线" in result["output"]
    assert "  ✓ 无线备用网 1/2 在线" in result["output"]


def test_precheck_accepts_declared_wired_and_wireless_targets(monkeypatch):
    players = [
        _player_sample(f"192.168.100.{index}", "wired")
        for index in range(1, 59)
    ] + [
        _player_sample(f"192.168.101.{index}", "wireless")
        for index in range(1, 67)
    ]
    result = _run_player_precheck(monkeypatch, players, _target_status(wired=58, wireless=66))

    assert result["verdict"] == "good"
    assert result["fail"] == 0
    assert "  ✓ 选手机位 58/58 全部在线" in result["output"]
    assert "  ✓ 无线备用网 66/66 在线" in result["output"]


def test_precheck_keeps_wireless_partial_offline_non_failing(monkeypatch):
    players = [
        _player_sample(f"192.168.100.{index}", "wired")
        for index in range(1, 59)
    ] + [
        _player_sample(f"192.168.101.{index}", "wireless", "0" if index == 66 else "1")
        for index in range(1, 67)
    ]
    result = _run_player_precheck(monkeypatch, players, _target_status(wired=58, wireless=66))

    assert result["verdict"] == "good"
    assert result["fail"] == 0
    assert result["warn"] == 0
    assert "  ✓ 选手机位 58/58 全部在线" in result["output"]
    assert "  ✓ 无线备用网 65/66 在线" in result["output"]


def test_precheck_warns_only_when_wireless_backup_is_fully_offline(monkeypatch):
    result = _run_player_precheck(monkeypatch, [
        _player_sample("192.168.100.11", "wired"),
        _player_sample("192.168.101.11", "wireless", "0"),
        _player_sample("192.168.101.12", "wireless", "0"),
    ], _target_status(wired=1, wireless=2))

    assert result["verdict"] == "warn"
    assert result["fail"] == 0
    assert result["warn"] == 1
    assert "  ✓ 选手机位 1/1 全部在线" in result["output"]
    assert "  ⚠ 无线备用网 0/2 在线" in result["output"]


def test_precheck_keeps_wired_seat_failure_when_wireless_is_online(monkeypatch):
    result = _run_player_precheck(monkeypatch, [
        _player_sample("192.168.100.11", "wired"),
        _player_sample("192.168.100.12", "wired", "0"),
        _player_sample("192.168.101.11", "wireless"),
    ], _target_status(wired=2, wireless=1))

    assert result["verdict"] == "bad"
    assert result["fail"] == 1
    assert "  ✗ 选手机位仅 1/2 在线" in result["output"]
    assert "  ✓ 无线备用网 1/1 在线" in result["output"]


def test_precheck_keeps_merged_count_for_targets_without_network_label(monkeypatch):
    result = _run_player_precheck(monkeypatch, [
        _player_sample("192.168.100.11"),
        _player_sample("192.168.100.12", value="0"),
    ], {"ok": True, "targets": {"total": 2}})

    assert result["verdict"] == "bad"
    assert result["fail"] == 1
    assert "  ✗ 选手机位仅 1/2 在线" in result["output"]
    assert "无线备用网" not in result["output"]
    assert "network 标签" not in result["output"]


def test_precheck_uses_wireless_targets_as_seats_on_wireless_only_events(monkeypatch):
    result = _run_player_precheck(monkeypatch, [
        _player_sample("192.168.101.11", "wireless"),
        _player_sample("192.168.101.12", "wireless", "0"),
    ], _target_status(wired=0, wireless=2))

    assert result["verdict"] == "bad"
    assert result["fail"] == 1
    assert "  ✗ 选手机位仅 1/2 在线" in result["output"]
    assert "无线备用网" not in result["output"]


def test_precheck_keeps_pure_wireless_event_healthy(monkeypatch):
    result = _run_player_precheck(monkeypatch, [
        _player_sample("192.168.101.11", "wireless"),
        _player_sample("192.168.101.12", "wireless"),
    ], _target_status(wired=0, wireless=2))

    assert result["verdict"] == "good"
    assert result["fail"] == 0
    assert "  ✓ 选手机位 2/2 全部在线" in result["output"]
    assert "无线备用网" not in result["output"]


def test_precheck_rejects_wireless_only_data_when_wired_targets_are_declared(monkeypatch):
    players = [
        _player_sample(f"192.168.101.{index}", "wireless")
        for index in range(1, 67)
    ]
    result = _run_player_precheck(monkeypatch, players, _target_status(wired=58, wireless=66))

    assert result["verdict"] == "bad"
    assert result["fail"] == 1
    assert "  ✗ 选手机位数据缺失" in result["output"]
    assert "58" in result["output"]
    assert "选手机位 66/66 全部在线" not in result["output"]
    assert "  ✓ 无线备用网 66/66 在线" in result["output"]


def test_precheck_rejects_incomplete_wired_series(monkeypatch):
    players = [
        _player_sample(f"192.168.100.{index}", "wired")
        for index in range(1, 51)
    ]
    result = _run_player_precheck(monkeypatch, players, _target_status(wired=58))

    assert result["verdict"] == "bad"
    assert result["fail"] == 1
    assert "  ✗ 选手机位数据不完整" in result["output"]
    assert "50/58" in result["output"]


def test_precheck_does_not_green_light_when_target_status_is_unavailable(monkeypatch):
    result = _run_player_precheck(monkeypatch, [
        _player_sample("192.168.100.11", "wired"),
        _player_sample("192.168.100.12", "wired"),
    ], _target_status(error="targets file not found"))

    assert result["verdict"] == "bad"
    assert "  ✓ 选手机位" not in result["output"]
    assert "  ⚠ 选手机位 2/2 在线，但目标状态不可用" in result["output"]
    assert "选手监控地址更新异常：targets file not found" in result["output"]


def test_precheck_treats_unreachable_target_status_as_unavailable(monkeypatch):
    _mock_external_services(monkeypatch)
    original_http_json = precheck._http_json

    def http_json(url, timeout=5):
        if url.endswith("/status"):
            raise OSError("connection refused")
        return original_http_json(url, timeout)

    monkeypatch.setattr(precheck, "_http_json", http_json)
    monkeypatch.setattr(precheck, "_prom_query", _players_prom_query([
        _player_sample("192.168.100.11", "wired"),
    ]))

    result = precheck.run_precheck(_context())

    assert result["verdict"] == "bad"
    assert "  ⚠ 选手机位 1/1 在线，但目标状态不可用" in result["output"]
    assert "选手监控地址更新异常：connection refused" in result["output"]


def test_precheck_does_not_green_light_when_target_status_is_stale(monkeypatch):
    result = _run_player_precheck(monkeypatch, [
        _player_sample("192.168.100.11", "wired"),
        _player_sample("192.168.100.12", "wired"),
    ], _target_status(wired=2, age=3601))

    assert result["verdict"] == "warn"
    assert "  ✓ 选手机位" not in result["output"]
    assert "  ⚠ 选手机位 2/2 在线，但目标状态不可用" in result["output"]


def test_precheck_does_not_count_unknown_network_labels_as_wired(monkeypatch):
    result = _run_player_precheck(monkeypatch, [
        _player_sample("192.168.100.11", "wired"),
        _player_sample("192.168.100.12", "wired"),
        _player_sample("192.168.101.11", "wifi"),
    ], _target_status(wired=2))

    assert result["verdict"] == "warn"
    assert "选手机位 3/3" not in result["output"]
    assert "  ✓ 选手机位 2/2 全部在线" in result["output"]
    assert "  ⚠ 选手目标有 1 个未知 network 标签，未计入机位判定" in result["output"]


def test_precheck_does_not_count_unlabeled_targets_as_wired_when_labels_are_mixed(monkeypatch):
    result = _run_player_precheck(monkeypatch, [
        _player_sample("192.168.100.11", "wired"),
        _player_sample("192.168.100.12"),
    ], _target_status(wired=1))

    assert result["verdict"] == "warn"
    assert "选手机位 2/2" not in result["output"]
    assert "  ✓ 选手机位 1/1 全部在线" in result["output"]
    assert "  ⚠ 选手目标有 1 个缺少 network 标签，未计入机位判定" in result["output"]


def test_precheck_rejects_untagged_targets_when_wired_targets_are_declared(monkeypatch):
    players = [
        _player_sample(f"192.168.100.{index}")
        for index in range(1, 59)
    ]
    result = _run_player_precheck(monkeypatch, players, _target_status(wired=58))

    assert result["verdict"] == "bad"
    assert result["fail"] == 1
    seat_level, seat_text = _line_level(result, "选手机位")
    assert seat_level == "bad"
    assert "数据缺失" in seat_text
    assert "全部在线" not in seat_text
    label_level, label_text = _line_level(result, "选手目标有")
    assert label_level == "warn"
    assert "58 个缺少 network 标签" in label_text


def test_precheck_rejects_unknown_labels_when_wired_targets_are_declared(monkeypatch):
    players = [
        _player_sample(f"192.168.100.{index}", "wifi")
        for index in range(1, 59)
    ]
    result = _run_player_precheck(monkeypatch, players, _target_status(wired=58))

    assert result["verdict"] == "bad"
    assert result["fail"] == 1
    seat_level, seat_text = _line_level(result, "选手机位")
    assert seat_level == "bad"
    assert "全部在线" not in seat_text
    label_level, label_text = _line_level(result, "选手目标有")
    assert label_level == "warn"
    assert "58 个未知 network 标签" in label_text


def test_precheck_flags_extra_wired_series(monkeypatch):
    players = [
        _player_sample(f"192.168.100.{index}", "wired")
        for index in range(1, 60)
    ]
    result = _run_player_precheck(monkeypatch, players, _target_status(wired=58))

    assert result["verdict"] == "warn"
    assert result["fail"] == 0
    seat_level, seat_text = _line_level(result, "选手机位")
    assert seat_level == "warn"
    assert "数量异常" in seat_text
    assert "全部在线" not in seat_text


def test_precheck_requires_declared_wireless_series_completeness(monkeypatch):
    result = _run_player_precheck(monkeypatch, [
        _player_sample("192.168.101.11", "wireless"),
    ], _target_status(wired=0, wireless=66))

    assert result["verdict"] == "bad"
    assert result["fail"] == 1
    seat_level, seat_text = _line_level(result, "选手机位")
    assert seat_level == "bad"
    assert "1/66" in seat_text
    assert "全部在线" not in seat_text


def test_precheck_flags_missing_wireless_backup_series(monkeypatch):
    players = [
        _player_sample(f"192.168.100.{index}", "wired")
        for index in range(1, 59)
    ] + [
        _player_sample(f"192.168.101.{index}", "wireless")
        for index in range(1, 51)
    ]
    result = _run_player_precheck(monkeypatch, players, _target_status(wired=58, wireless=66))

    assert result["verdict"] == "warn"
    assert result["fail"] == 0
    seat_level, _ = _line_level(result, "选手机位")
    assert seat_level == "good"
    backup_level, backup_text = _line_level(result, "无线备用网")
    assert backup_level == "warn"
    assert "50/66" in backup_text


def test_precheck_flags_wireless_backup_series_excess(monkeypatch):
    players = [
        _player_sample(f"192.168.100.{index}", "wired")
        for index in range(1, 59)
    ] + [
        _player_sample(f"192.168.101.{index}", "wireless")
        for index in range(1, 71)
    ]
    result = _run_player_precheck(monkeypatch, players, _target_status(wired=58, wireless=66))

    assert result["verdict"] == "warn"
    backup_level, backup_text = _line_level(result, "无线备用网")
    assert backup_level == "warn"
    assert "70" in backup_text
    assert "66" in backup_text


def test_precheck_keeps_legacy_contract_when_status_shape_is_legacy(monkeypatch):
    players = [
        _player_sample(f"192.168.100.{index}")
        for index in range(1, 59)
    ]
    result = _run_player_precheck(monkeypatch, players, {"ok": True, "targets": {"total": 58}})

    assert result["verdict"] == "good"
    seat_level, seat_text = _line_level(result, "选手机位")
    assert seat_level == "good"
    assert "58/58" in seat_text
    assert _line_level(result, "选手目标有") == (None, None)


def test_precheck_rejects_unknown_labels_with_legacy_status(monkeypatch):
    players = [
        _player_sample(f"192.168.100.{index}", "wifi")
        for index in range(1, 59)
    ]
    result = _run_player_precheck(monkeypatch, players, {"ok": True, "targets": {"total": 58}})

    assert result["verdict"] == "warn"
    seat_level, seat_text = _line_level(result, "选手机位")
    assert seat_level == "warn"
    assert "全部在线" not in seat_text
    label_level, label_text = _line_level(result, "选手目标有")
    assert label_level == "warn"
    assert "未知 network 标签" in label_text


def test_precheck_flags_wired_series_when_status_declares_none(monkeypatch):
    players = [
        _player_sample(f"192.168.100.{index}", "wired")
        for index in range(1, 3)
    ] + [
        _player_sample(f"192.168.101.{index}", "wireless")
        for index in range(1, 3)
    ]
    result = _run_player_precheck(monkeypatch, players, _target_status(wired=0, wireless=2))

    assert result["verdict"] == "warn"
    seat_level, seat_text = _line_level(result, "选手机位")
    assert seat_level == "warn"
    assert "数量异常" in seat_text
    backup_level, _ = _line_level(result, "无线备用网")
    assert backup_level == "good"


def test_precheck_fails_when_bridge_is_not_ready(monkeypatch):
    _mock_external_services(monkeypatch, bridge_ready=False)
    monkeypatch.setattr(precheck, "_prom_query", _healthy_prom_query)

    result = precheck.run_precheck(_context())

    assert result["verdict"] == "bad"
    assert "告警服务未就绪：未配置飞书 Token；设备离线监测已停止" in result["output"]


def test_bridge_http_error_body_keeps_health_detail_semantics(monkeypatch):
    _mock_external_services(monkeypatch)
    original_http_json = precheck._http_json

    def http_json(url, timeout=5):
        if "alertmanager" in url:
            raise urllib.error.HTTPError(
                url,
                503,
                "Service Unavailable",
                None,
                io.BytesIO(json.dumps({
                    "ready": False,
                    "tokenConfigured": True,
                    "deadWatchers": ["incident"],
                    "watchers": {},
                }).encode("utf-8")),
            )
        return original_http_json(url, timeout)

    monkeypatch.setattr(precheck, "_http_json", http_json)
    monkeypatch.setattr(precheck, "_prom_query", _healthy_prom_query)

    result = precheck.run_precheck(_context())

    assert result["verdict"] == "bad"
    assert "告警服务未就绪：incident监测已停止" in result["output"]
    assert "告警服务不可达" not in result["output"]


def test_watcher_errors_use_operator_facing_monitor_names(monkeypatch):
    labels = {
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
    for watcher_key, label in labels.items():
        _mock_external_services(monkeypatch)
        base_http_json = precheck._http_json

        def http_json(url, timeout=5, key=watcher_key):
            if "alertmanager" in url:
                return {
                    "ready": True,
                    "tokenConfigured": True,
                    "deadWatchers": [],
                    "watchers": {key: {"lastError": "fixture detail"}},
                }
            return base_http_json(url, timeout)

        monkeypatch.setattr(precheck, "_http_json", http_json)
        monkeypatch.setattr(precheck, "_prom_query", _healthy_prom_query)
        result = precheck.run_precheck(_context())
        assert f"告警监测仍在运行，但最近一次检查失败：{label}：fixture detail" in result["output"]


def test_config_check_keeps_only_first_six_blocking_issues(monkeypatch):
    _mock_external_services(monkeypatch)
    monkeypatch.setattr(precheck, "_prom_query", _healthy_prom_query)
    issues = [
        {"level": "bad", "message": f"missing-{index}", "path": f"path.{index}"}
        for index in range(8)
    ] + [{"level": "warn", "message": "ignored warning", "path": "warn"}]

    result = precheck.run_precheck(_context(lambda: issues))

    assert result["pass"] == 8
    assert result["warn"] == 0
    assert result["fail"] == 6
    assert "missing-0（path.0）" in result["output"]
    assert "missing-5（path.5）" in result["output"]
    assert "missing-6" not in result["output"]
    assert "ignored warning" not in result["output"]


def test_precheck_result_keeps_bad_over_warn_verdict_and_output_format():
    result = precheck._precheck_result([
        {"level": "good", "text": "good"},
        {"level": "warn", "text": "warn"},
        {"level": "bad", "text": "bad"},
    ])

    assert result == {
        "ok": True,
        "verdict": "bad",
        "pass": 1,
        "warn": 1,
        "fail": 1,
        "output": "  ✓ good\n  ⚠ warn\n  ✗ bad",
    }
