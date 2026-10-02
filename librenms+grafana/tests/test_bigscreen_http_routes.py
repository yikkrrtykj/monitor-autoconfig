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
    (topology / "fixture.json").write_text('{"fixture":true}', encoding="utf-8")
    server = server.replace("/srv/topology/", topology.as_posix() + "/")
    config = tmp_path / "nginx.conf"
    config.write_text(f'pid "{(tmp_path / "nginx.pid").as_posix()}";\n'
                      'error_log stderr; events {} http { access_log off;\n'
                      + server + "\n}\n", encoding="utf-8")
    process = subprocess.Popen([nginx, "-p", str(tmp_path) + "/", "-c", str(config), "-g", "daemon off;"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    def request(path):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            # Emulate host port forwarding: listener port differs from public Host.
            connection.request("GET", path, headers={"Host": "fixture.invalid:8088"})
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
        expected = (app / "index.html").read_bytes()
        for route in routes:
            status, location, body = request(route + "?fixture=1")
            assert (status, location, body) == (200, None, expected), route
            status, location, _ = request(route + "/?fixture=1")
            assert (status, location) == (302, route + "?fixture=1"), route
        for asset in ("control/auth-controller.js", "incident/incident-panel.js", "dhcp/dhcp-panel.js"):
            status, location, body = request("/" + asset)
            assert (status, location, body) == (200, None, (app / asset).read_bytes())
        assert request("/topology/fixture.json")[::2] == (200, b'{"fixture":true}')
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
