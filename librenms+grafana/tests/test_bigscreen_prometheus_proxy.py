import http.client
import json
import subprocess
import time
import urllib.parse
import uuid
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "docker-compose.yml"
ENV_FILE = ROOT / ".env.example"


def _run_docker(*args, timeout=120, check=True):
    completed = subprocess.run(
        ["docker", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and completed.returncode != 0:
        pytest.fail(
            f"docker {' '.join(args)} failed ({completed.returncode}): "
            f"{completed.stderr.strip()}"
        )
    return completed


def _render_bigscreen_service():
    completed = _run_docker(
        "compose",
        "--env-file", str(ENV_FILE),
        "-f", str(COMPOSE_FILE),
        "config", "--format", "json",
    )
    service = json.loads(completed.stdout)["services"]["bigscreen"]
    return service["image"], service["entrypoint"]


def _wait_for_proxy(container, timeout=15):
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        port_result = _run_docker("port", container, "80/tcp", check=False)
        if port_result.returncode == 0 and port_result.stdout.strip():
            address = port_result.stdout.strip().splitlines()[0]
            port = int(address.rsplit(":", 1)[1])
            try:
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
                connection.request("GET", "/config.js")
                response = connection.getresponse()
                response.read()
                connection.close()
                if response.status == 200:
                    return port
                last_error = f"HTTP {response.status}"
            except OSError as exc:
                last_error = str(exc)
        else:
            last_error = port_result.stderr.strip()
        time.sleep(0.1)
    logs = _run_docker("logs", container, check=False).stdout
    pytest.fail(f"Bigscreen proxy did not become ready: {last_error}\n{logs}")


def _request(port, method, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request(method, path)
    response = connection.getresponse()
    body = response.read()
    headers = {key.lower(): value for key, value in response.getheaders()}
    connection.close()
    return response.status, headers, body


def _wait_for_query_proxy(port, timeout=15):
    deadline = time.monotonic() + timeout
    last_status = None
    while time.monotonic() < deadline:
        try:
            last_status, unused_headers, unused_body = _request(
                port, "GET", "/prometheus/api/v1/query?query=vector%281%29",
            )
            if last_status == 200:
                return
        except OSError:
            pass
        time.sleep(0.1)
    pytest.fail(f"Prometheus query proxy did not become ready: HTTP {last_status}")


def _upstream_requests(records):
    if not records.exists():
        return []
    return [line for line in records.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture(scope="module")
def prometheus_proxy(tmp_path_factory):
    work = tmp_path_factory.mktemp("bigscreen-prometheus-proxy")
    records_dir = work / "records"
    records_dir.mkdir()
    records = records_dir / "access.log"
    upstream_config = work / "upstream.conf"
    upstream_config.write_text(
        """events {}
http {
  log_format capture '$request_method|$request_uri';
  access_log /records/access.log capture;
  server {
    listen 9090;
    if ($arg_fake_status = 503) { return 503; }
    location / {
      add_header X-Fake-Upstream true always;
      add_header X-Fake-Method $request_method always;
      add_header X-Fake-Uri $request_uri always;
      return 200 '$request_method|$request_uri';
    }
  }
}
""",
        encoding="utf-8",
    )

    image, entrypoint = _render_bigscreen_service()
    assert entrypoint[:2] == ["/bin/sh", "-c"]
    assert len(entrypoint) == 3

    suffix = uuid.uuid4().hex[:12]
    network = f"bigscreen-prometheus-test-{suffix}"
    upstream = f"bigscreen-prometheus-upstream-{suffix}"
    proxy = f"bigscreen-prometheus-proxy-{suffix}"
    _run_docker("network", "create", network)
    try:
        _run_docker(
            "run", "--detach", "--name", upstream,
            "--network", network, "--network-alias", "prometheus",
            "--volume", f"{upstream_config}:/etc/nginx/nginx.conf:ro",
            "--volume", f"{records_dir}:/records",
            image,
        )
        _run_docker(
            "run", "--detach", "--name", proxy,
            "--network", network,
            "--publish", "127.0.0.1::80",
            "--volume", f"{ROOT / 'bigscreen'}:/app:ro",
            "--entrypoint", entrypoint[0],
            image, entrypoint[1], entrypoint[2],
        )
        port = _wait_for_proxy(proxy)
        _wait_for_query_proxy(port)
        syntax = _run_docker("exec", proxy, "nginx", "-t")
        assert "test is successful" in syntax.stderr
        yield port, records
    finally:
        _run_docker("rm", "--force", proxy, upstream, check=False)
        _run_docker("network", "rm", network, check=False)


def test_exact_query_endpoints_preserve_method_path_and_query(prometheus_proxy):
    port, records = prometheus_proxy
    cases = [
        (
            "/prometheus/api/v1/query?"
            + urllib.parse.urlencode({"query": 'label_replace(vector(1),"说明","中文/+&","","")'}),
            "/api/v1/query?",
        ),
        (
            "/prometheus/api/v1/query_range?"
            + urllib.parse.urlencode({
                "query": 'rate(metric_total{site="上海/A+B&C"}[5m])',
                "start": "1", "end": "2", "step": "15",
            }),
            "/api/v1/query_range?",
        ),
    ]

    for path, upstream_prefix in cases:
        expected_upstream_uri = path.removeprefix("/prometheus")
        status, headers, body = _request(port, "GET", path)
        assert status == 200
        assert headers["x-fake-upstream"] == "true"
        assert headers["x-fake-method"] == "GET"
        assert headers["x-fake-uri"] == expected_upstream_uri
        assert body.decode("utf-8") == f"GET|{expected_upstream_uri}"
        assert expected_upstream_uri.startswith(upstream_prefix)

        status, headers, body = _request(port, "HEAD", path)
        assert status == 200
        assert headers["x-fake-upstream"] == "true"
        assert headers["x-fake-method"] == "HEAD"
        assert headers["x-fake-uri"] == expected_upstream_uri
        assert body == b""

    before = len(_upstream_requests(records))
    status, unused_headers, unused_body = _request(
        port, "GET", "/prometheus/api/v1/query?query=vector%281%29&fake_status=503",
    )
    assert status == 503
    assert len(_upstream_requests(records)) == before + 1


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
@pytest.mark.parametrize("path", [
    "/prometheus/api/v1/query",
    "/prometheus/api/v1/query_range",
])
def test_mutating_methods_never_reach_allowed_query_paths(prometheus_proxy, method, path):
    port, records = prometheus_proxy
    before = len(_upstream_requests(records))
    status, unused_headers, unused_body = _request(port, method, path)
    assert status == 403
    assert len(_upstream_requests(records)) == before


@pytest.mark.parametrize("method,path,expected_status", [
    ("GET", "/prometheus", 404),
    ("GET", "/prometheus/", 404),
    ("GET", "/prometheus/-/quit", 404),
    ("HEAD", "/prometheus/-/reload", 404),
    ("POST", "/prometheus/-/quit", 404),
    ("PUT", "/prometheus/-/reload", 404),
    ("GET", "/prometheus/metrics", 404),
    ("GET", "/prometheus/api/v1/targets", 404),
    ("GET", "/prometheus/api/v1/status/config", 404),
    ("GET", "/prometheus/admin", 404),
    ("GET", "/prometheus/unknown", 404),
    ("GET", "/prometheus/api/v1/query/", 404),
    ("GET", "/prometheus/api/v1/query/child", 404),
    ("GET", "/Prometheus/api/v1/query", 404),
    ("GET", "/prometheus//-/quit", 404),
    ("GET", "/prometheus/api/v1/query/../../-/quit", 404),
    ("GET", "/prometheus/%2d%2fquit", None),
])
def test_other_prometheus_paths_are_rejected_without_upstream_access(
    prometheus_proxy, method, path, expected_status,
):
    port, records = prometheus_proxy
    before = len(_upstream_requests(records))
    status, unused_headers, unused_body = _request(port, method, path)
    if expected_status is None:
        assert 400 <= status < 500
    else:
        assert status == expected_status
    assert len(_upstream_requests(records)) == before
