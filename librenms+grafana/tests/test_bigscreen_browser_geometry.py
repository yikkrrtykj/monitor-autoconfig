"""真实 Chromium/Edge 回归；复用生产 DOM/CSS 与完整 prepare/toggle/render。"""
import html
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _browser():
    for name in ("google-chrome", "chromium", "chromium-browser", "msedge"):
        executable = shutil.which(name)
        if executable:
            return executable
    edge = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    if edge.is_file():
        return str(edge)
    raise RuntimeError("Browser regression requires Chromium or Edge")


@pytest.mark.parametrize("width,height,count", [(1280, 900, 12), (760, 620, 3)])
@pytest.mark.parametrize("status_visible", [False, True])
def test_ap_toggle_real_browser_geometry_and_labels(tmp_path, width, height, count, status_visible):
    index = (ROOT / "bigscreen/index.html").read_text(encoding="utf-8")
    section = re.search(r'<section class="topology-panel"[\s\S]*?</section>', index).group()
    section = section.replace(' hidden>', '>', 1)
    if status_visible:
        section = section.replace('id="topologyNetworkStatus" role="status" hidden>', 'id="topologyNetworkStatus" role="status">脱敏降级提示')
    paths = [ROOT / "bigscreen" / name for name in (
        "utils.js", "api.js", "topology.js", "ap-topology.js", "charts/topology-panel.js"
    )] + [ROOT / "tests/fixtures/topology/ap-browser-acceptance.js"]
    scripts = "".join(f'<script src="{path.as_uri()}"></script>' for path in paths)
    page = tmp_path / "ap-browser.html"
    page.write_text(f'<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="{(ROOT / "bigscreen/style.css").as_uri()}">'
                    f'<body data-wired-count="{count}"><main class="screen topology-mode"><header class="topbar">脱敏测试</header>{section}</main>{scripts}</body>',
                    encoding="utf-8")
    command = [
        _browser(), "--headless", "--no-sandbox", "--disable-gpu", "--allow-file-access-from-files",
        "--disable-background-networking", "--no-first-run", "--no-default-browser-check",
        f"--user-data-dir={tmp_path / 'profile'}", f"--window-size={width},{height}",
        "--virtual-time-budget=5000", "--dump-dom", page.as_uri(),
    ]
    if os.name == "nt":
        if not os.environ.get("PLAYWRIGHT_MODULE_PATH"):
            pytest.skip("Windows browser driver requires an existing workspace Playwright runtime")
        command = [shutil.which("node"), str(ROOT / "tests/browser-fixture-runner.js"), _browser(), page.as_uri(), str(width), str(height)]
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=30,
       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    assert completed.returncode == 0, "headless browser failed"
    match = re.search(r'<pre id="browserResult"[^>]*>(.*?)</pre>', completed.stdout, re.S)
    assert match and match.group(1), "browser regression did not complete"
    result = json.loads(html.unescape(match.group(1)))
    assert result.get("pass"), result
    assert result["wiredNodes"] >= count and result["apNodes"] == 4 and result["scale"] > 0


@pytest.mark.parametrize("width", [360, 760, 980, 1280, 1920])
@pytest.mark.parametrize("form_width", [660, 1100])
def test_config_cards_and_collapsed_app_real_browser(tmp_path, width, form_width):
    scripts = "".join(f'<script src="{(ROOT / "bigscreen" / name).as_uri()}"></script>'
                      for name in ("config/config-model.js", "config/config-editor.js"))
    page = tmp_path / "config-browser.html"
    page.write_text(f'''<!doctype html><meta charset="utf-8">
<link rel="stylesheet" href="{(ROOT / "bigscreen/style.css").as_uri()}">
<link rel="stylesheet" href="{(ROOT / "bigscreen/platform.css").as_uri()}">
<form class="control-form" id="controlConfigForm" style="max-width: {form_width}px"></form>{scripts}
<script>
try {{
  const editor = BSConfigEditor.createConfigEditor({{
    document, window, HTMLInputElement, pages: [], teamLayouts: {{}},
    escapeHtml: (value) => String(value ?? '').replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;'),
    model: BSConfigModel
  }});
  editor.render({{ok: true, config: {{devices: {{stage_switches: [{{name: 'Fixture Switch', ip: '10.0.0.1'}}],
    access_switches: [{{name: 'Fixture Access', ip: '10.0.0.2'}}]}},
    alerts: {{feishu_app_id: 'cli_fixture', feishu_app_secret: 'fixture-secret'}}}}}});
  const advanced = document.getElementById('controlFeishuAppConfig');
  if (advanced.open || document.querySelector('[data-config-path="alerts.feishu_app_id"]').checkVisibility())
    throw new Error('App configuration must be collapsed');
  advanced.open = true;
  for (const section of document.querySelectorAll('.config-section')) {{
    const card = section.getBoundingClientRect();
    for (const el of section.querySelectorAll('input, select, textarea, button')) {{
      const rect = el.getBoundingClientRect();
      if (rect.width && (rect.left < card.left - 1 || rect.right > card.right + 1))
        throw new Error('Control escaped card: ' + (el.dataset.configPath || el.textContent));
    }}
  }}
  const result = document.createElement('pre'); result.id = 'browserResult';
  result.textContent = JSON.stringify({{pass: true}}); document.body.append(result);
}} catch (error) {{
  const result = document.createElement('pre'); result.id = 'browserResult';
  result.textContent = JSON.stringify({{pass: false, error: error.message}}); document.body.append(result);
}}
</script>''', encoding="utf-8")
    if os.name == "nt":
        if not os.environ.get("PLAYWRIGHT_MODULE_PATH"):
            pytest.skip("Windows browser driver requires an existing workspace Playwright runtime")
        command = [shutil.which("node"), str(ROOT / "tests/browser-fixture-runner.js"), _browser(), page.as_uri(), str(width), "900"]
    else:
        command = [_browser(), "--headless", "--no-sandbox", "--disable-gpu", "--allow-file-access-from-files",
                   f"--user-data-dir={tmp_path / 'profile'}", f"--window-size={width},900",
                   "--virtual-time-budget=5000", "--dump-dom", page.as_uri()]
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=30,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    assert completed.returncode == 0, "headless browser failed"
    match = re.search(r'<pre id="browserResult"[^>]*>(.*?)</pre>', completed.stdout, re.S)
    assert match, "browser regression did not complete"
    result = json.loads(html.unescape(match.group(1)))
    assert result.get("pass"), result
