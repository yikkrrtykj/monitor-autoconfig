from .test_platform_transactions import load_api, seed


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    @staticmethod
    def read(limit=-1):
        return b"OK"


def _sample(job, instance, value="1"):
    return {"metric": {"job": job, "instance": instance}, "value": [0, value]}


def _mock_external_services(monkeypatch, api, bridge_ready=True, target_count=1):
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

    monkeypatch.setattr(api, "_http_json", http_json)
    monkeypatch.setattr(api.urllib.request, "urlopen", lambda *args, **kwargs: _Response())


def test_precheck_fails_when_no_player_targets(monkeypatch, tmp_path):
    api = load_api(tmp_path)
    seed(api)
    _mock_external_services(monkeypatch, api, target_count=0)

    def prom_query(expr):
        if expr == "up":
            return [_sample("prometheus", "prometheus:9090")]
        if expr.startswith('probe_success{job=~"infra-'):
            return [_sample("infra-core-ping", "core")]
        if expr == 'probe_success{job="player-ping"}':
            return []
        return []

    monkeypatch.setattr(api, "_prom_query", prom_query)

    result = api.run_precheck()

    assert result["verdict"] == "bad"
    assert "选手机位监控目标为 0" in result["output"]
    assert "选手目标生成器尚未生成任何目标" in result["output"]


def test_precheck_fails_when_bridge_is_not_ready(monkeypatch, tmp_path):
    api = load_api(tmp_path)
    seed(api)
    _mock_external_services(monkeypatch, api, bridge_ready=False)
    monkeypatch.setattr(api, "_prom_query", lambda expr: (
        [_sample("prometheus", "prometheus:9090")] if expr == "up" else
        [_sample("infra-core-ping", "core")] if expr.startswith('probe_success{job=~"infra-') else
        [_sample("player-ping", "player-1")] if expr == 'probe_success{job="player-ping"}' else
        []
    ))

    result = api.run_precheck()

    assert result["verdict"] == "bad"
    assert "告警服务未就绪" in result["output"]
