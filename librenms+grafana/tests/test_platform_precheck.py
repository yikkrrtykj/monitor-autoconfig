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


def _mock_external_services(monkeypatch, bridge_ready=True, target_count=1):
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
        "output": "  ✗ Prometheus 不可达（http://prometheus:9090）：offline",
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
        ("prom", "http://prometheus:9090", 'probe_success{job="player-ping"}'),
        ("json", "http://grafana:3000/api/health", 5),
        ("json", "http://alertmanager-feishu-bridge:5005/health", 5),
        ("urlopen", "http://bigscreen/", 5),
        ("json", "http://player-targets:9199/status", 5),
        ("urlopen", "http://librenms:8000/", 5),
        ("config",),
    ]
    assert reads == [1024, 1024]
    assert result == {
        "ok": True,
        "verdict": "good",
        "pass": 9,
        "warn": 0,
        "fail": 0,
        "output": "\n".join([
            "  ✓ Prometheus 正常，抓取目标 1/1 全部在线",
            "  ✓ 基础设施 1 台全部在线",
            "  ✓ 选手机位 1/1 全部在线",
            "  ✓ Grafana 正常",
            "  ✓ 告警服务及后台线程正常",
            "  ✓ 赛事大屏入口正常",
            "  ✓ 选手目标生成器正常，共 1 个目标",
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
    assert "选手目标生成器尚未生成任何目标" in result["output"]


def test_precheck_fails_when_bridge_is_not_ready(monkeypatch):
    _mock_external_services(monkeypatch, bridge_ready=False)
    monkeypatch.setattr(precheck, "_prom_query", _healthy_prom_query)

    result = precheck.run_precheck(_context())

    assert result["verdict"] == "bad"
    assert "告警服务未就绪：未配置飞书 Token；后台线程已停止：device-down" in result["output"]


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
    assert "告警服务未就绪：后台线程已停止：incident" in result["output"]
    assert "告警服务不可达" not in result["output"]


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
