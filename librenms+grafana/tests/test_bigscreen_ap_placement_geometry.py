"""真实浏览器验收：AP 定位清单在 1920 / 1366 / 390 宽度下的布局与交互。

复用现有 bigscreen 浏览器几何测试的 Edge/Chromium harness；Windows 没有
Playwright 运行时的环境下按既有约定跳过，由 Linux CI 提供真实浏览器证据。
"""
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


def _base_names(api_mac_suffix, level, online, index):
    confirmed = level == "confirmed-direct"
    observed = level == "observed-attachment"
    has_current = confirmed or observed
    ambiguous = level == "ambiguous"
    return {
        "ap_name": f"AP-{index:02d}",
        "ap_ip": f"10.20.0.{index}",
        "ap_mac": f"02:00:00:00:01:{api_mac_suffix:02d}",
        "site": "fixture",
        "level": level,
        "online": online,
        "switch_ip": "10.0.0.11" if has_current else None,
        "switch_ifindex": 10101 if has_current else None,
        "switch_port": "Gi1/0/1" if has_current else None,
        "vlan": 40 if has_current else None,
        "evidence_age_seconds": 30 if has_current else None,
        "last_observed": None if has_current else {
            "switch_ip": "10.0.0.9",
            "switch_port": "Gi1/0/9",
            "vlan": 44,
            "evidence_age_seconds": 600,
            "validated_at": 1300,
        },
        "coverage_status": "partial",
        "reason": "operator_confirmed_core_port" if confirmed
        else "verified_fdb_egress" if observed
        else "ambiguous_fdb_candidates" if ambiguous
        else "offline_no_current_location",
        "evidence": {
            "fdb": {
                "status": "matched" if has_current else "ambiguous" if ambiguous else "no_candidate",
                "raw_candidates": 1 if has_current else 2 if ambiguous else 0,
                "candidate_age_seconds": 10800 if has_current or ambiguous else None,
            },
            "exact_snmp": {
                "status": "matched" if has_current else "ambiguous" if ambiguous else "not_attempted",
                "gets": 1 if has_current else 0,
                "validated_at": 1870 if has_current else None,
                "validation_age_seconds": 30 if has_current else None,
            },
            "manual_confirmation": {"status": "confirmed" if confirmed else "none"},
            "direct_neighbor": (
                {"status": "operator-attested", "source": "manual-allowlist"}
                if confirmed else {"status": "unknown", "source": "none"}
            ),
            "online": {"status": online, "source": "fixture", "reason": "fixture"},
            "coverage": {"status": "partial", "reasons": ["fdb_missing_switch"]},
            "port": {"verified": has_current},
        },
    }


def _payload():
    # P1/#142 contract: confirmed rows are online and carry operator-attested
    # evidence; formal attachments are a separate source and are not joined by B1.
    placements = []
    index = 1
    for _count in range(8):
        placements.append(_base_names(index, "confirmed-direct", "online", index))
        index += 1
    for _count in range(2):
        placements.append(_base_names(index, "observed-attachment", "unknown", index))
        index += 1
    placements.append(_base_names(index, "unresolved", "offline", index))
    # 第一条 AP 名称带 HTML 标记，验证动态文本转义。
    placements[0]["ap_name"] = "<b>AP-XSS</b>"
    return {
        "ok": True,
        "schema_version": 1,
        "generated_at": 1900.0,
        "max_age_seconds": 7200,
        "valid": True,
        "file_age_seconds": 5,
        "counts": {
            "confirmed_direct": 8, "observed_attachment": 2, "ambiguous": 0,
            "unresolved": 1, "online": 8, "offline": 1, "unknown": 2, "total": 11,
        },
        "coverage": {
            "status": "partial", "expected_switches": 2, "polled_switches": 2,
            "missing_switches": 0, "poll_failed_switches": 1,
            "fdb_switches_usable": 1, "fdb_switches_failed": 1,
            "reasons": ["unresolved_switch"],
        },
        "placements": placements,
        "warnings": [],
        "degraded": False,
    }


def _section_markup():
    index = (ROOT / "bigscreen/index.html").read_text(encoding="utf-8")
    match = re.search(r'<section class="ap-placement"[\s\S]*?</section>', index)
    assert match, "apPlacementSection must exist in index.html"
    return match.group(0)


def _fixture(enabled):
    payload = json.dumps(_payload(), ensure_ascii=False)
    section = _section_markup()
    script_paths = [
        ROOT / "bigscreen" / name for name in ("utils.js", "ap-placement.js")
    ]
    scripts = "".join(
        f'<script src="{path.as_uri()}"></script>' for path in script_paths)
    inline = """
<script>
const payload = %s;
const fetchCalls = [];
const doc = document;
const panel = window.BSApPlacement.createApPlacementPanel({
  document: document,
  window: window,
  escapeHtml: window.BSUtils.escapeHtml,
  fetchApPlacement: async () => { fetchCalls.push(1); return payload; },
  canRead: async () => true,
  enabled: %s,
  intervalMs: 30000,
  now: () => Date.now()
});
panel.start();
setTimeout(() => {
  try {
    const section = document.getElementById('apPlacementSection');
    const list = document.getElementById('apPlacementList');
    const rows = list.querySelectorAll('.ap-placement-row');
    const details = list.querySelectorAll('.ap-placement-evidence');
    const firstDetails = details[0] || null;
    const closedInitially = firstDetails ? !firstDetails.open : false;
    if (firstDetails) firstDetails.open = true;
    const search = document.getElementById('apPlacementSearch');
    search.value = 'AP-11';
    search.dispatchEvent(new Event('input', { bubbles: true }));
    const filteredRows = list.querySelectorAll('.ap-placement-row').length;
    search.value = '';
    search.dispatchEvent(new Event('input', { bubbles: true }));
    const refresh = document.getElementById('apPlacementRefresh');
    refresh.focus();
    const levelLabels = Array.from(list.querySelectorAll('.ap-placement-level'))
      .map((element) => element.textContent.trim());
    const lastRow = rows[rows.length - 1] || null;
    const lastFacts = lastRow ? lastRow.querySelector('.ap-placement-facts') : null;
    const lastDetails = lastRow ? lastRow.querySelector('.ap-placement-evidence') : null;
    const result = {
      pass: true,
      enabled: %s,
      sectionVisible: !section.hidden,
      fetchCalls: fetchCalls.length,
      rowCount: rows.length,
      closedInitially: closedInitially,
      filteredRows: filteredRows,
      levelLabels: levelLabels,
      unknownVisible: list.textContent.includes('未知（无可靠心跳）'),
      exactAgeVisible: list.textContent.includes('30 秒'),
      fdbAgeVisible: list.textContent.includes('3 小时'),
      historyInDetailsOnly: Boolean(lastFacts && lastDetails
        && !lastFacts.textContent.includes('10.0.0.9')
        && lastDetails.textContent.includes('10.0.0.9')),
      confirmedRowsOnline: Array.from(
        list.querySelectorAll('.ap-placement-row.level-confirmed-direct'))
        .every((row) => row.querySelector('.ap-placement-facts')
          .textContent.includes('在线')),
      confirmedAttestation: list.textContent.includes('operator-attested'),
      offlineVisible: list.textContent.includes('离线'),
      unresolvedNoCurrent: Boolean(lastFacts
        && !lastFacts.textContent.includes('10.0.0.11')
        && lastFacts.textContent.includes('—')),
      refreshFocused: document.activeElement === refresh,
      htmlEscaped: list.querySelectorAll('b').length === 0
        && list.textContent.includes('<b>AP-XSS</b>'),
      innerWidth: window.innerWidth,
      viewportOverflow: document.documentElement.scrollWidth - window.innerWidth,
      sectionOverflow: section.scrollWidth - section.clientWidth,
      listOverflow: list.scrollWidth - list.clientWidth
    };
    document.getElementById('browserResult').textContent = JSON.stringify(result);
  } catch (error) {
    document.getElementById('browserResult').textContent = JSON.stringify(
      { pass: false, error: String(error) });
  }
}, 400);
</script>
""" % (payload, "true" if enabled else "false", "true" if enabled else "false")
    return f"""<!doctype html><meta charset="utf-8">
<link rel="stylesheet" href="{(ROOT / "bigscreen/platform.css").as_uri()}">
<body><main class="screen wireless-mode"><section class="wireless-panel" id="wirelessPanel">
<div class="wireless-controls"></div><div class="wireless-summary"></div><div class="wireless-board"></div>
{section}
</section></main><pre id="browserResult"></pre>{scripts}{inline}</body>"""


def _details_fixture():
    payload = json.dumps(_payload(), ensure_ascii=False)
    section = _section_markup()
    scripts = "".join(
        f'<script src="{(ROOT / "bigscreen" / name).as_uri()}"></script>'
        for name in ("utils.js", "ap-placement.js")
    )
    inline = """
<script>
let current = %s;
const panel = window.BSApPlacement.createApPlacementPanel({
  document: document,
  window: window,
  escapeHtml: window.BSUtils.escapeHtml,
  fetchApPlacement: async () => current,
  canRead: async () => true,
  enabled: true,
  intervalMs: 30000,
  now: () => Date.now()
});
panel.start();
setTimeout(() => {
  try {
    const list = document.getElementById('apPlacementList');
    const rows = list.querySelectorAll('.ap-placement-row');
    const first = rows[0];
    const second = rows[1];
    const firstMac = first.dataset.mac;
    const secondMac = second.dataset.mac;
    first.querySelector('.ap-placement-evidence').open = true;
    second.querySelector('.ap-placement-evidence').open = true;
    first.querySelector('summary').focus();
    const rowCountBefore = rows.length;
    panel.refresh();
    setTimeout(() => {
      const afterRows = Array.from(list.querySelectorAll('.ap-placement-row'));
      const firstAfter = afterRows.find((row) => row.dataset.mac === firstMac);
      const secondAfter = afterRows.find((row) => row.dataset.mac === secondMac);
      const expandedAfterRefresh = Boolean(firstAfter
        && firstAfter.querySelector('.ap-placement-evidence').open);
      const secondExpanded = Boolean(secondAfter
        && secondAfter.querySelector('.ap-placement-evidence').open);
      const focusAfterRefresh = Boolean(firstAfter
        && document.activeElement === firstAfter.querySelector('summary'));
      const search = document.getElementById('apPlacementSearch');
      search.value = 'AP-02';
      search.dispatchEvent(new Event('input', { bubbles: true }));
      const filteredRows = Array.from(list.querySelectorAll('.ap-placement-row'));
      const filterPreservedSecond = filteredRows.some((row) => row.dataset.mac === secondMac
        && row.querySelector('.ap-placement-evidence').open);
      const filterWrongAssociation = filteredRows.some((row) => row.dataset.mac !== secondMac
        && row.querySelector('.ap-placement-evidence').open);
      search.value = '';
      search.dispatchEvent(new Event('input', { bubbles: true }));
      current = {
        ...current,
        placements: current.placements.slice(1),
        counts: {
          confirmed_direct: 7, observed_attachment: 2, ambiguous: 0,
          unresolved: 1, online: 7, offline: 1, unknown: 2, total: 10
        }
      };
      panel.refresh();
      setTimeout(() => {
        const finalRows = Array.from(list.querySelectorAll('.ap-placement-row'));
        const removedFirst = !finalRows.some((row) => row.dataset.mac === firstMac);
        const staleOpen = finalRows.some((row) => row.dataset.mac === firstMac
          && row.querySelector('.ap-placement-evidence').open);
        panel.clear();
        const afterClear = list.querySelectorAll('.ap-placement-row').length;
        document.getElementById('browserResult').textContent = JSON.stringify({
          pass: true,
          rowCountBefore: rowCountBefore,
          rowCountAfterRemoval: finalRows.length,
          expandedAfterRefresh: expandedAfterRefresh,
          secondExpanded: secondExpanded,
          focusAfterRefresh: focusAfterRefresh,
          filterPreservedSecond: filterPreservedSecond,
          filterWrongAssociation: filterWrongAssociation,
          removedFirst: removedFirst,
          staleOpen: staleOpen,
          rowsAfterClear: afterClear
        });
      }, 350);
    }, 350);
  } catch (error) {
    document.getElementById('browserResult').textContent = JSON.stringify(
      { pass: false, error: String(error) });
  }
}, 350);
</script>
""" % payload
    return f"""<!doctype html><meta charset="utf-8">
<link rel="stylesheet" href="{(ROOT / "bigscreen/platform.css").as_uri()}">
<body><main class="screen wireless-mode"><section class="wireless-panel" id="wirelessPanel">
<div class="wireless-controls"></div><div class="wireless-summary"></div><div class="wireless-board"></div>
{section}
</section></main><pre id="browserResult"></pre>{scripts}{inline}</body>"""


def _run_browser(width, height, page):
    command = [
        _browser(), "--headless", "--no-sandbox", "--disable-gpu",
        "--allow-file-access-from-files", "--disable-background-networking",
        "--no-first-run", "--no-default-browser-check",
        f"--user-data-dir={page.parent / 'profile'}",
        f"--window-size={width},{height}", "--virtual-time-budget=6000",
        "--dump-dom", page.as_uri(),
    ]
    completed = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", timeout=40,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    assert completed.returncode == 0, "headless browser failed"
    match = re.search(r'<pre id="browserResult"[^>]*>(.*?)</pre>', completed.stdout, re.S)
    assert match and match.group(1), "browser fixture did not complete"
    return json.loads(html.unescape(match.group(1)))


@pytest.mark.parametrize(("width", "height"), [(1920, 1080), (1366, 768), (390, 844)])
def test_ap_placement_real_browser_layout(tmp_path, width, height):
    page = tmp_path / "ap-placement.html"
    page.write_text(_fixture(True), encoding="utf-8")
    result = _run_browser(width, height, page)
    assert result.get("pass"), result
    assert result["sectionVisible"] is True
    assert result["fetchCalls"] == 1
    assert result["rowCount"] == 11
    assert result["closedInitially"] is True
    assert result["filteredRows"] == 1
    assert "已确认直连" in result["levelLabels"]
    assert "已验证转发出口，物理直连未确认" in result["levelLabels"]
    assert result["unknownVisible"] is True
    assert result["exactAgeVisible"] is True
    assert result["fdbAgeVisible"] is True
    assert result["historyInDetailsOnly"] is True
    assert result["confirmedRowsOnline"] is True
    assert result["confirmedAttestation"] is True
    assert result["offlineVisible"] is True
    assert result["unresolvedNoCurrent"] is True
    assert result["refreshFocused"] is True
    assert result["htmlEscaped"] is True
    assert result["viewportOverflow"] <= 1
    assert result["sectionOverflow"] <= 1
    assert result["listOverflow"] <= 1


def test_ap_placement_real_browser_feature_disabled(tmp_path):
    page = tmp_path / "ap-placement-disabled.html"
    page.write_text(_fixture(False), encoding="utf-8")
    result = _run_browser(1366, 768, page)
    assert result.get("pass"), result
    assert result["enabled"] is False
    assert result["sectionVisible"] is False
    assert result["fetchCalls"] == 0
    assert result["rowCount"] == 0


def test_ap_placement_real_browser_details_survive_refresh(tmp_path):
    page = tmp_path / "ap-placement-details.html"
    page.write_text(_details_fixture(), encoding="utf-8")
    result = _run_browser(1366, 768, page)
    assert result.get("pass"), result
    assert result["rowCountBefore"] == 11
    assert result["expandedAfterRefresh"] is True
    assert result["secondExpanded"] is True
    assert result["focusAfterRefresh"] is True
    assert result["filterPreservedSecond"] is True
    assert result["filterWrongAssociation"] is False
    assert result["removedFirst"] is True
    assert result["staleOpen"] is False
    assert result["rowsAfterClear"] == 0
    assert result["rowCountAfterRemoval"] == 10
