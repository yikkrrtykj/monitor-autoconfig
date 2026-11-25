"""用生产生成的 Nginx 配置验证页面路由，不访问生产服务。"""
import http.client
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_functional_pages_do_not_redirect_to_internal_port(tmp_path):
    nginx = shutil.which("nginx")
    if not nginx and Path("/usr/sbin/nginx").is_file():
        nginx = "/usr/sbin/nginx"
    if not nginx:
        pytest.skip("Nginx HTTP regression requires nginx (installed by Linux CI)")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    server = compose.split("cat > /etc/nginx/conf.d/default.conf <<'EOF'", 1)[1].split("\n        EOF", 1)[0]
    server = server.replace("$$", "$")
    app = tmp_path / "app"
    shutil.copytree(ROOT / "bigscreen", app)
    # Ensure directory collisions occur even for routes without a source folder.
    pages = (app / "pages.js").read_text(encoding="utf-8")
    routes = re.findall(r'path: "(/[^" ]+)"', pages)
    for route in routes:
        (app / route.lstrip("/")).mkdir(exist_ok=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = server.replace("listen 80;", f"listen 127.0.0.1:{port};")
    server = server.replace("root /app;", f'root "{app.as_posix()}";')
    server = server.replace("/usr/share/nginx/html/config.js", (app / "config.js").as_posix())
    topology = tmp_path / "topology"
    topology.mkdir()
    public_files = {
        "edges.json": "edges-public.json",
        "ap-attachments.json": "ap-attachments-public.json",
        "isp_targets.json": "isp_targets.json",
        "isp-discovery-state.json": "isp-discovery-state.json",
    }
    for disk_name in public_files.values():
        (topology / disk_name).write_text(f'{{"fixture":"{disk_name}"}}', encoding="utf-8")
    internal_files = (
        "ap-placement.json", "switch-discovery-state.json", "switch_targets.json",
        "stackwise_targets.json", "ap-ping-targets.json", "server-attachments.json",
        "topology-diagnostics.json", ".collector.lock", "fixture.json",
    )
    secret_fixture = b'{"internal":"fixture-private"}'
    for disk_name in (*internal_files, "edges.json", "ap-attachments.json"):
        (topology / disk_name).write_bytes(secret_fixture)
    server = server.replace("/srv/topology/", topology.as_posix() + "/")
    # Same fixture and Nginx binary, using the previous broad alias as a control.
    legacy_server = f"""
      server {{
        listen 127.0.0.1:{port};
        server_name legacy-fixture.invalid;
        location /topology/ {{
          alias "{topology.as_posix()}/";
          try_files $uri =404;
        }}
      }}
    """
    config = tmp_path / "nginx.conf"
    config.write_text(f'pid "{(tmp_path / "nginx.pid").as_posix()}";\n'
                      'error_log stderr; events {} http { access_log off;\n'
                      + server + legacy_server + "\n}\n", encoding="utf-8")
    process = subprocess.Popen([nginx, "-p", str(tmp_path) + "/", "-c", str(config), "-g", "daemon off;"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    def request(path, host="fixture.invalid:8088"):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            # Emulate host port forwarding: listener port differs from public Host.
            connection.request("GET", path, headers={"Host": host})
            response = connection.getresponse()
            return response.status, response.getheader("Location"), response.read()
        finally:
            connection.close()
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                request("/")
                break
            except OSError:
                if process.poll() is not None or time.monotonic() >= deadline:
                    pytest.fail("Nginx did not start within five seconds")
                time.sleep(0.05)
        assert request("/topology/ap-placement.json", "legacy-fixture.invalid")[::2] == (200, secret_fixture)
        expected = (app / "index.html").read_bytes()
        for route in routes:
            status, location, body = request(route + "?fixture=1")
            assert (status, location, body) == (200, None, expected), route
            status, location, _ = request(route + "/?fixture=1")
            assert (status, location) == (302, route + "?fixture=1"), route
        for asset in ("control/auth-controller.js", "control/incident-registry.js", "dhcp/dhcp-panel.js"):
            status, location, body = request("/" + asset)
            assert (status, location, body) == (200, None, (app / asset).read_bytes())
        for retired in ("/incident?at=fixture", "/incident/", "/incident.js", "/incident/incident-panel.js"):
            assert request(retired)[:2] == (410, None), retired
        # The files exist in the mounted volume. Denials must not depend on absence.
        denied = [f"/topology/{name}" for name in internal_files]
        denied += [
            "/topology/ap-placement.json?fixture=1",
            "/topology/%61p-placement.json",
            "/topology//ap-placement.json",
            "/topology%2Fap-placement.json",
            "/topology/./ap-placement.json",
            "/topology/Ap-Placement.json",
            "/Topology/ap-placement.json",
            "/topology/ap-placement.json/",
            "/topology/edges-public.json",
            "/topology/ap-attachments-public.json",
            "/topology/unknown/directory/",
            "/topology/unknown.json",
        ]
        for path in denied:
            status, _, body = request(path)
            assert status in (403, 404), (path, status)
            assert b"fixture-private" not in body, path
        for uri, disk_name in public_files.items():
            assert request(f"/topology/{uri}?fixture=1")[::2] == (
                200, (topology / disk_name).read_bytes(),
            )
        # A missing projection never falls back to the raw file.
        (topology / "edges-public.json").unlink()
        assert request("/topology/edges.json")[0] == 404
        (topology / "edges-public.json").symlink_to(topology / "edges.json")
        status, _, body = request("/topology/edges.json")
        assert status in (403, 404)
        assert secret_fixture not in body
        # Non-page directory redirects must also remain relative.
        (app / "fixture-directory").mkdir()
        status, location, _ = request("/fixture-directory")
        assert (status, location) == (301, "/fixture-directory/")
    finally:
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)
