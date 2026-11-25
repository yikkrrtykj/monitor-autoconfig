const assert = require("node:assert/strict");
const { execFileSync } = require("node:child_process");
const { createHash } = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const { identity } = require("../bigscreen/ping/model.js");
const { maxWans } = require("../bigscreen/infra/overview-model.js");

const root = path.resolve(__dirname, "../bigscreen");
const output = process.env.INFRA_BROWSER_OUTPUT || path.join(os.tmpdir(), "infra-browser-evidence");
const origin = "http://infra-fixture.invalid";
const fixedAt = 1760000000;
const dims = [[1920, 1080], [1366, 768], [390, 844]];
const modifiedAssets = ["index.html", "infra/overview.css", "infra/overview-view.js"];
const modes = { "match-5v5": 10, "tournament-6": 24, "tournament-64-2layer": 64,
  "tournament-64-233": 64, "tournament-64-332": 64 };
const routes = new Set(["/", "/infra", "/latency", "/control", "/ping", ...Object.keys(modes).map((mode) => "/" + mode)]);
const longName = "Distribution-" + "abcdefgh".repeat(16);
const longWan = "WAN-" + "InternationalUplink".repeat(8);
const device = (job, instance, target_ip, extra = {}) => ({ job: `infra-${job}-ping`, instance, target_ip, ...extra });
const core = device("core", "Core A", "192.0.2.1");
const vip = device("fw", "Firewall VIP", "192.0.2.2");
const members = [device("fw-unit", "Firewall A", "192.0.2.3"), device("fw-unit", "Firewall B", "192.0.2.4")];
const dist = device("dist", longName, "192.0.2.5");
const stage = device("dist", "Stage Switch", "192.0.2.6");
const firewall = "192.0.2.2";
const vector = (metric, value, at) => ({ metric, value: [at, String(value)] });
const matrix = (metric, values) => ({ metric, values: values.map(([t, v]) => [t, String(v)]) });
const part = (page, name) => page.locator(`[data-overview="${name}"]`);

function fixture(options = {}) {
  const { count = 6, shared = false, vipMissing = false, wanFailed = false, long = false } = options;
  assert.ok(count <= maxWans);
  const inventory = Array.from({ length: count }, (_, i) => ({
    targets: [`198.51.100.${shared && i === 1 ? 1 : i + 1}`],
    labels: { display_name: i === 2 && long ? longWan : `WAN ${i + 1}`,
      metric_target: firewall, metric_ifindex: String(i + 1) }
  }));
  const gateways = inventory.flatMap(({ targets: [ip], labels }, i) => shared && i === 1 ? [] : [
    device("isp", `ISP Gateway ${i + 1}`, ip, shared && i === 0 ? {} : {
      metric_target: firewall, metric_ifindex: labels.metric_ifindex
    })
  ]);
  if (!count) gateways.push(device("isp", "Manually monitored ISP", "198.51.100.1"));
  const status = new Map([[dist, options.down ? "DOWN" : "UP"],
    [device("ap", "AP noise", "192.0.2.30"), "DOWN"],
    [{ job: "player-ping", instance: "203.0.113.1", team: "1", seat: "1" }, "DOWN"]]);
  for (const metric of [core, ...(!vipMissing ? [vip] : []), ...members, ...gateways]) status.set(metric, "UP");
  if (options.many) {
    for (let i = 0; i < 9; i++) status.set(device("dist", `Access ${i + 1}`, `192.0.2.${40 + i}`), "DOWN");
  }
  if (options.unknown) status.set(members[1], "UNKNOWN");
  if (options.degraded) status.set(core, "DEGRADED");

  const inventoryBody = JSON.stringify(inventory);
  const config = { eventName: "赛事网络 · 浏览器验收", ispAutoDiscovery: "true", firewallPing: `VIP:${vip.target_ip}`,
    ispNames: inventory.map(({ labels }) => labels.display_name).join(","),
    ispMaxBandwidthMbps: long ? `WAN 1:1000/500,${longWan}:1750/2500` : "WAN 1:1000/500" };
  function taggedPing(query, at) {
    return [...status].flatMap(([metric, state]) => {
      const samples = metric.job === "infra-isp-ping" ? 60 : 30;
      const fields = query.includes('"target"') ? { target: 1 } : {};
      if (state !== "UNKNOWN") Object.assign(fields, {
        source: 1, source_at: at - 1, probe: state === "DOWN" ? 0 : 1,
        probe_at: at - 1, count: samples, ok: state === "DOWN" ? 0 : state === "DEGRADED" ? samples * 5 / 6 : samples
      });
      if (state === "UP" || state === "DEGRADED") Object.assign(fields, { rtt: 0.012, rtt_at: at - 1 });
      return Object.entries(fields).map(([ping_field, value]) => vector({ ...metric, ping_field }, value, at));
    });
  }
  function taggedWan(at) {
    const times = Array.from({ length: 61 }, (_, i) => at - (60 - i) * 15);
    const series = (overview_field, fn, labels = {}) =>
      matrix({ overview_field, instance: firewall, ...labels }, times.flatMap((t, i) => {
        const value = fn(t, i);
        return value === null ? [] : [[t, value]];
      }));
    return [series("up", () => 1), series("up_at", (t) => t - 1), ...inventory.flatMap(({ labels }, i) => {
      if (long && i === 3) return [];
      const fields = {
        in: (t, k) => i === 0 && k === 27 ? null : i === 0 && k === 60 ? 0 :
          long && i === 2 ? 986e6 : 12e6 + (k % 7) * 1e6,
        out: () => i === 0 ? 24 : 3e6,
        in_at: (t, k) => i === 0 && (k === 27 || (options.longWarning && k === 60)) ? null : t - 1,
        out_at: (t, k) => long && i === 2 && k === 60 ? null : t - 1,
        in_resets: () => 0, out_resets: () => options.longWarning && i === 0 ? 1 : 0, speed: () => 1000
      };
      return Object.entries(fields).map(([field, fn]) => series(field, fn, { ifIndex: labels.metric_ifindex }));
    })];
  }
  function rawSamples(query, at) {
    const duration = Number(/\[(\d+)s\]$/.exec(query)?.[1] || 300);
    return [...status].flatMap(([metric, state]) => {
      if (state === "UNKNOWN") return [];
      const interval = metric.job === "infra-isp-ping" ? 1 : 2;
      const points = Array.from({ length: Math.min(duration / interval, 300) }, (_, i) => at - interval * (i + 1)).reverse();
      return ["up", "probe_success", "probe_icmp_duration_seconds"].map((name) => matrix({
        ...metric, __name__: name, ...(name === "probe_icmp_duration_seconds" ? { phase: "rtt" } : {})
      }, points.map((t, i) => [t, name === "up" ? 1 : name === "probe_success" ? Number(state !== "DOWN") :
        (i % 2 ? 0.011 : 0.013)])));
    });
  }
  const playerMetrics = Array.from({ length: 64 }, (_, i) => ({
    job: "player-ping", instance: `203.0.113.${i + 1}`, team: String(Math.floor(i / 4) + 1),
    seat: String(i % 4 + 1), network: "wired", role: "player"
  }));
  const matchMetrics = Array.from({ length: 10 }, (_, i) => ({
    ...playerMetrics[i], team: String(Math.floor(i / 5) + 1), seat: String(i % 5 + 1)
  }));
  function legacy(query, url, at, routeName) {
    const range = url.pathname.endsWith("/query_range");
    const sample = (metric, value) => range ? matrix(metric, [[at - 5, value], [at, value]]) : vector(metric, value, at);
    if (query.includes('role="player"')) {
      const capacity = modes[routeName.slice(1)] || 64;
      const rows = capacity === 10 ? matchMetrics : playerMetrics.slice(0, capacity);
      return rows.map((metric) => sample(metric, query.includes("probe_icmp_duration_seconds") ? 0.012 : 1));
    }
    if (query.includes("ifHC")) return [sample({}, 16e6)];
    if (query.includes("sysUpTime")) return [sample({ instance: stage.instance, target_ip: stage.target_ip }, 3600)];
    if (query.includes("probe_icmp_duration_seconds")) return [sample(stage, 0.012)];
    if (query.includes("probe_success") || query === "up") return [sample(stage, 1)];
    return [];
  }
  return {
    config, inventoryBody, setStatus: (metric, value) => status.set(metric, value),
    discovery: { status: count ? "ok" : "disabled", last_success_at: fixedAt - 1, inventory_count: count,
      inventory_sha256: createHash("sha256").update(inventoryBody).digest("hex") },
    prometheus(url, routeName) {
      const query = url.searchParams.get("query") || "";
      const at = Number(url.searchParams.get("time") ?? url.searchParams.get("end") ?? fixedAt);
      const range = url.pathname.endsWith("/query_range");
      if (query.includes('"overview_field"')) return wanFailed ? [503, "matrix", []] : [200, "matrix", taggedWan(at)];
      if (query.startsWith("{__name__")) return [200, "matrix", rawSamples(query, at)];
      if (query.includes('"ping_field"')) return [200, range ? "matrix" : "vector",
        range || query.includes('job="custom-ping"') ? [] : taggedPing(query, at)];
      return [200, range ? "matrix" : "vector", legacy(query, url, at, routeName)];
    }
  };
}

const baseline = new Map();
if (process.env.INFRA_BASE_REF) {
  for (const asset of modifiedAssets) {
    baseline.set(asset, execFileSync("git", ["show", `${process.env.INFRA_BASE_REF}:librenms+grafana/bigscreen/${asset}`],
      { cwd: root }));
  }
}

async function open(browser, viewport, data, assets, routeName = "/infra") {
  const isMobile = viewport.width === 390;
  const context = await browser.newContext({ viewport, timezoneId: "UTC", hasTouch: isMobile, isMobile });
  await context.route("**/*", (route) => {
    const url = new URL(route.request().url());
    if (url.origin !== origin) return route.abort();
    const json = (data, status = 200) => route.fulfill({ json: data, status });
    if (url.pathname === "/config.js") return route.fulfill({
      contentType: "text/javascript", body: `window.BIGSCREEN_CONFIG = ${JSON.stringify(data.config)};`
    });
    if (url.pathname === "/topology/isp_targets.json") {
      return route.fulfill({ contentType: "application/json", body: data.inventoryBody });
    }
    if (url.pathname === "/topology/isp-discovery-state.json") return json(data.discovery);
    if (url.pathname === "/platform-api/network/traffic-bc") {
      const points = [fixedAt - 90, fixedAt - 60, fixedAt - 30, fixedAt]
        .map((t, i) => ({ t, bps: i === 1 ? null : 200000 + i * 150000 }));
      const mk = (name, iface, rate, kind) => ({
        name, interface: iface, interface_status: "UP",
        sa_status: kind === "remote_ob" ? "UNKNOWN" : null,
        egress_isp: kind === "remote_ob" ? "UNKNOWN" : null,
        configured_isp: kind === "remote_ob" ? "Configured only" : null,
        traffic_validity: "VALID", unit: kind === "remote_ob" ? "Tunnel 接口" : "SVI 三层网关接口",
        rx: { bps: rate, sample_at: fixedAt, points },
        tx: { bps: rate * 0.3, sample_at: fixedAt,
          points: points.map(({ t, bps }) => ({ t, bps: bps === null ? null : bps * 0.3 })) }
      });
      return json({ ok: true, generated_at: fixedAt, interval_seconds: 30,
        domains: {
          remote_ob: { state: "ready", items: [mk("Fixture OB", "tunnel1", 1020000, "remote_ob")] },
          business_vlan: { state: "ready", items: [
            mk("Fixture VLAN20", "Vlan20", 54660000, "business_vlan"),
            mk("Fixture VLAN21", "Vl21", 45970000, "business_vlan")
          ] }
        } });
    }
    if (/\/api\/v1\/query(?:_range)?$/.test(url.pathname)) {
      const [status, resultType, result] = data.prometheus(url, routeName);
      return json({ status: status === 200 ? "success" : "error", data: { resultType, result } }, status);
    }
    if (url.pathname === "/platform-api/auth/status") return json({ ok: true, authenticated: false, enabled: true });
    if (url.pathname === "/platform-api/ping/targets") return json({ ok: true, targets: [], revision: "fixture" });
    const asset = routes.has(url.pathname) ? "index.html" : url.pathname.slice(1);
    const file = path.resolve(root, asset);
    if (!file.startsWith(root + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
      return route.fulfill({ status: 404 });
    }
    const types = { ".js": "text/javascript", ".css": "text/css", ".html": "text/html", ".svg": "image/svg+xml" };
    return route.fulfill({ body: assets.get(asset) || fs.readFileSync(file), contentType: types[path.extname(file)] });
  });
  const page = await context.newPage();
  page.setDefaultTimeout(7000);
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const requests = [];
  const startup = [];
  const pending = new Set();
  let elapsed = 0;
  page.on("request", (request) => {
    const url = new URL(request.url());
    startup.push(`${request.method()} ${request.resourceType()} ${url.pathname}`);
    if (!["fetch", "xhr"].includes(request.resourceType())) return;
    requests.push({ elapsed, method: request.method(), path: url.pathname, search: url.search });
    pending.add(request);
  });
  for (const event of ["requestfinished", "requestfailed"]) page.on(event, (request) => pending.delete(request));
  await page.clock.install({ time: new Date((fixedAt - 10) * 1000) });
  await page.clock.pauseAt(new Date(fixedAt * 1000));
  await page.goto(origin + routeName);
  return { page, context, requests, errors, pending, startup, async tick(seconds) {
    elapsed += seconds;
    await page.clock.runFor(seconds * 1000);
  } };
}

async function until(predicate, message) {
  const deadline = performance.now() + 8000;
  while (!(await predicate())) {
    assert.ok(performance.now() < deadline, message);
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
}

async function ready(page) {
  await page.locator('.overview-table-wrap [data-gateway]').first().waitFor();
  await until(async () => !(await part(page, "wan-source").textContent()).includes("正在读取") &&
    await page.locator('.overview-table-wrap [data-gateway]').first().locator("td").nth(2).textContent() !== "N/A",
  "WAN and gateway STDDEV did not settle");
}

async function geometry(page) {
  return page.evaluate(() => {
    const overview = document.querySelector("#infraOverview");
    const parts = Object.fromEntries([...overview.querySelectorAll("[data-overview]")].map((el) => [el.dataset.overview, el]));
    const table = overview.querySelector(".overview-table-wrap");
    const candidates = {
      health: parts.health, infra: parts.infra, coverage: parts.coverage,
      faultTitle: overview.querySelector("#overviewFaultTitle"), faultSummary: parts["fault-summary"],
      firstDown: parts.faults.querySelector(".overview-faults a"), gateway: table.querySelector("[data-gateway] a"),
      wan: overview.querySelector("#overviewWanTitle")
    };
    const visibleText = (node) => {
      if (!node?.textContent.trim()) return false;
      const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
      const range = document.createRange();
      const rects = [];
      while (walker.nextNode()) {
        if (!walker.currentNode.textContent.trim()) continue;
        range.selectNodeContents(walker.currentNode);
        rects.push(...range.getClientRects());
      }
      return rects.length > 0 && rects.every((rect) => {
        if (rect.width <= 0 || rect.height <= 0 || rect.left < 0 || rect.right > innerWidth ||
            rect.top < 0 || rect.bottom > innerHeight) return false;
        const y = (rect.top + rect.bottom) / 2;
        return [rect.left + 0.5, (rect.left + rect.right) / 2, rect.right - 0.5]
          .every((x) => {
            const hit = document.elementFromPoint(x, y);
            return node.contains(hit) || (node.matches("a") && hit === node.parentElement);
          });
      });
    };
    const wanPlots = [...parts.bandwidth.querySelectorAll(".overview-wan")].map((card) => {
      const plot = card.querySelector("svg, .overview-no-trend");
      const baseline = plot.querySelector(".overview-baseline");
      return {
        row: card.getBoundingClientRect().top,
        top: plot.getBoundingClientRect().top,
        baseline: baseline ? baseline.getBoundingClientRect().top : null
      };
    });
    return {
      visible: Object.fromEntries(Object.entries(candidates).map(([key, node]) => [key, visibleText(node)])),
      top: Object.fromEntries(Object.entries(candidates).map(([key, node]) => [key,
        node ? Math.round(node.getBoundingClientRect().top) : null])),
      domOrder: [...overview.querySelectorAll("#overviewFaultTitle,#overviewGatewayTitle,#overviewWanTitle")].map(({ id }) => id),
      pageOverflowX: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      overviewOverflowX: overview.scrollWidth - overview.clientWidth,
      cardOverflowX: [...parts.bandwidth.querySelectorAll(".overview-wan,.overview-rate,h4,.overview-wan-note")]
        .map((node) => node.scrollWidth - node.clientWidth).filter((px) => px > 1),
      tableOverflowX: table.scrollWidth - table.clientWidth, tableOverflowY: table.scrollHeight - table.clientHeight,
      firstRowWans: wanPlots.filter((plot) => plot.row === wanPlots[0]?.row).length,
      wanPlots
    };
  });
}

function expectSummary(result, width, down) {
  const { visible, top } = result;
  for (const field of ["health", "infra", "faultTitle", "faultSummary", ...(down ? ["firstDown"] : [])]) {
    assert.equal(visible[field], true, `${width}px does not expose ${field}: ${JSON.stringify(result)}`);
  }
  assert.deepEqual(result.domOrder, ["overviewFaultTitle", "overviewGatewayTitle", "overviewWanTitle"]);
  assert.ok(top.health < top.faultTitle && top.faultTitle < top.wan && top.gateway < top.wan);
  assert.equal(result.pageOverflowX, 0);
  assert.equal(result.overviewOverflowX, 0);
  assert.deepEqual(result.cardOverflowX, []);
  if (width > 390) assert.equal(visible.gateway, true, `${width}px gateway text is clipped: ${JSON.stringify(result)}`);
}

function expectWanAlignment(plots, expectedCards, label) {
  assert.equal(plots.length, expectedCards);
  const rows = new Map();
  for (const plot of plots) {
    if (!rows.has(plot.row)) rows.set(plot.row, []);
    rows.get(plot.row).push(plot);
  }
  assert.ok(plots.length <= 1 || [...rows.values()].some((row) => row.length > 1), label);
  for (const row of rows.values()) {
    const tops = row.map((plot) => plot.top);
    const baselines = row.map((plot) => plot.baseline).filter((y) => y !== null);
    assert.ok(Math.max(...tops) - Math.min(...tops) <= 1,
      `${label}: WAN trend and placeholder top edges differ: ${tops.join(", ")}`);
    if (baselines.length > 1) {
      assert.ok(Math.max(...baselines) - Math.min(...baselines) <= 1,
        `${label}: WAN plot baselines differ: ${baselines.join(", ")}`);
    }
  }
}

async function expectFaultColors(page) {
  const colors = await part(page, "fault-summary").locator("[data-count]").evaluateAll((nodes) => nodes.map((node) => {
    const { color, backgroundColor } = getComputedStyle(node);
    return { status: node.dataset.status, count: node.dataset.count, color, backgroundColor };
  }));
  const active = {
    DOWN: ["rgb(255, 192, 205)", "rgb(72, 37, 50)"],
    DEGRADED: ["rgb(255, 219, 155)", "rgb(71, 53, 28)"],
    UNKNOWN: ["rgb(215, 227, 239)", "rgb(44, 57, 75)"]
  };
  for (const { status, count, color, backgroundColor } of colors) {
    const expected = count === "0" ? ["rgb(154, 175, 195)", "rgb(13, 23, 36)"] : active[status];
    assert.deepEqual([color, backgroundColor], expected, `${status} count ${count}`);
  }
}

function screen(page, name) {
  return page.screenshot({ path: path.join(output, name + ".png"), animations: "disabled", timeout: 15000 });
}

async function verifyScenarios(browser, report) {
  const scenarios = [
    ["normal", {}, "healthy", [0, 0, 0]],
    ["single-down", { down: true }, "fault", [1, 0, 0]],
    ["multiple-down", { down: true, many: true, count: 9 }, "fault", [10, 0, 0]],
    ["unknown", { unknown: true, count: 1 }, "partial", [0, 0, 1]],
    ["mixed", { down: true, unknown: true, degraded: true, wanFailed: true }, "fault", [1, 1, 1]],
    ["wan-failed", { wanFailed: true }, "partial", [0, 0, 0]],
    ["vip-missing", { vipMissing: true }, "partial", [0, 0, 0]],
    ["shared-gateway", { shared: true }, "partial", [0, 0, 0]],
    ["zero-wan", { count: 0 }, "partial", [0, 0, 0]],
    ["long-labels", { count: maxWans, long: true, longWarning: true, down: true }, "fault", [1, 0, 0]]
  ];
  for (const [width, height] of dims) {
    for (const [name, options, health, faultCounts] of scenarios) {
      const { page, context, errors } = await open(browser, { width, height }, fixture(options), new Map());
      try {
        await ready(page);
        assert.equal(await part(page, "health").getAttribute("data-health"), health,
          `${name}: ${await part(page, "coverage").textContent()}`);
        assert.deepEqual(await part(page, "fault-summary").locator("[data-count]").evaluateAll(
          (nodes) => nodes.map((node) => Number(node.dataset.count))), faultCounts, name);
        assert.equal(await page.locator(".overview-wan").count(), options.count ?? 6);
        assert.equal(await page.locator(".overview-vlan").count(), 0);
        assert.equal(await part(page, "bc-ob").locator(".overview-bc-item").count(), 1);
        assert.equal(await part(page, "bc-vlan").locator(".overview-bc-item").count(), 2);
        assert.match(await part(page, "bc-ob").textContent(), /SA: UNKNOWN/);
        assert.match(await part(page, "bc-vlan").textContent(), /不是 VLAN 互联网/);
        if (name === "normal") {
          await page.locator(".overview-bc-grid").screenshot({
            path: path.join(output, "bc-synthetic-" + width + "x" + height + ".png"),
            animations: "disabled", timeout: 15000
          });
        }
        assert.doesNotMatch(await part(page, "faults").textContent(), /AP noise|203\.0\.113\.1/);
        const coverage = await part(page, "coverage").textContent();
        const measured = await geometry(page);
        if (["normal", "single-down", "unknown", "mixed"].includes(name)) await screen(page, `after-${name}-${width}x${height}`);
        expectSummary(measured, width, options.down);
        expectWanAlignment(measured.wanPlots, options.count ?? 6, `${name}-${width}`);
        await expectFaultColors(page);
        if (coverage) assert.equal(measured.visible.coverage, true, name);
        if (name === "normal") {
          assert.equal(measured.firstRowWans, width === 390 ? 2 : 6);
          assert.deepEqual(await part(page, "gateways").locator("tr[data-gateway] a").allTextContents(),
            ["Core A", "Firewall VIP", "Firewall A", "Firewall B", ...Array.from({ length: 6 }, (_, i) => `ISP Gateway ${i + 1}`)]);
          assert.deepEqual(await part(page, "gateways").locator("thead th").allTextContents(),
            ["Name", "Status", "RTT ms", "STDDEV ms", "Loss %"]);
          assert.equal(await part(page, "gateways").locator("tbody").count(), 2);
          const wan = page.locator(".overview-wan");
          assert.match(await wan.first().locator('[data-direction="in"] strong').textContent(), /0\.0 Mbps/);
          assert.match(await wan.first().locator('[data-direction="out"] strong').textContent(), /<0\.1 Kbps/);
          assert.equal(await wan.first().locator(".overview-util").count(), 2);
          assert.equal(await wan.nth(1).locator(".overview-util").count(), 0);
          assert.match(await wan.nth(1).textContent(), /容量待核实/);
          assert.equal((await wan.first().locator("path.overview-rx").getAttribute("d")).match(/M/g).length, 2);
          assert.equal(await wan.first().locator("path.overview-tx").getAttribute("class"), "overview-tx");
          assert.ok(measured.wanPlots.every(({ baseline }) => Number.isFinite(baseline)));
        }
        if (name === "multiple-down") {
          assert.equal(await part(page, "faults").locator(".overview-faults li").count(), 8);
          assert.equal(await part(page, "faults").getByText("查看其余离线设备").getAttribute("href"), "/ping");
        }
        if (name === "mixed") {
          assert.match(await part(page, "health").textContent(), /检测到网络异常.*监控部分不可用/);
          assert.match(await part(page, "faults").textContent(), /质量异常.*探测未知/s);
          assert.match(coverage, /WAN 采集缺失/);
        }
        if (options.wanFailed) {
          assert.match(await part(page, "bandwidth").textContent(), /N\/A/);
          assert.doesNotMatch(await part(page, "bandwidth").textContent(), /0\.0 Mbps/);
        }
        if (name === "vip-missing") assert.match(coverage, /FW VIP 缺少探测覆盖/);
        if (name === "shared-gateway") {
          assert.equal(await page.locator('[data-probe-coverage="ambiguous"]').count(), 2);
          assert.match(coverage, /2 个 WAN 探测关联有歧义/);
        }
        if (name === "long-labels") {
          const cards = page.locator(".overview-wan");
          const longCard = cards.nth(2);
          assert.equal(await longCard.locator("h4").textContent(), longWan);
          assert.equal(await longCard.locator(".overview-util").count(), 1);
          assert.match(await longCard.locator('[data-direction="in"]').textContent(), /986\.0 Mbps.*1750 Mbps/s);
          assert.match(await longCard.locator('[data-direction="out"] strong').textContent(), /N\/A/);
          assert.match(await cards.nth(3).locator(".overview-no-trend").textContent(), /近 15 分钟暂无数据/);
          assert.ok(Number.isFinite(measured.wanPlots[2].baseline));
          assert.equal(measured.wanPlots[3].baseline, null);
          assert.equal(measured.wanPlots[2].row, measured.wanPlots[3].row);
          const first = cards.first();
          assert.match(await first.textContent(), /抓取失败、缺样或数据过期/);
          assert.match(await first.textContent(), /速率样本不足、重复或计数异常/);
          await cards.first().scrollIntoViewIfNeeded();
          await screen(page, `after-wan-alignment-${width}x${height}`);
        }
        assert.deepEqual(errors, []);
        report.scenarios[`${name}-${width}`] = measured;
        if (baseline.size && name === "mixed") {
          const before = await open(browser, { width, height }, fixture(options), baseline);
          try {
            await ready(before.page);
            report.before[width] = await geometry(before.page);
            await screen(before.page, `before-mixed-${width}x${height}`);
          } finally { await before.context.close(); }
        }
      } finally { await context.close(); }
    }
  }
}

function requestKind(request) {
  if (request.path.startsWith("/topology/")) return request.path.split("/").at(-1);
  const params = new URLSearchParams(request.search);
  const query = params.get("query") || "";
  if (query.includes('"overview_field"')) {
    assert.equal(Number(params.get("end")) - Number(params.get("start")), 900);
    assert.equal(params.get("step"), "15");
    return "WAN range";
  }
  if (query.includes('"target"')) return "Ping inventory + summary";
  if (query.includes('"ping_field"')) return "Ping summary";
  if (query.startsWith("{__name__")) return "Gateway STDDEV";
  return request.path;
}

async function verifyBudget(browser, assets) {
  const layout = await open(browser, { width: 1366, height: 768 }, fixture(), assets);
  const { page, context, requests, pending, startup, tick } = layout;
  try {
    await ready(page);
    const startupRequests = startup.slice().sort();
    const rounds = [];
    for (let at = 0; at <= 60; at += 5) {
      const expected = [at % 30 === 0 ? "Ping inventory + summary" : "Ping summary"];
      if (at % 15 === 0) expected.push("WAN range");
      if (at % 30 === 0) expected.push("Gateway STDDEV", "isp_targets.json", "isp-discovery-state.json");
      if (at) await tick(5);
      await until(() => requests.filter((request) => request.elapsed === at).length >= expected.length && !pending.size,
        `HTTP batch at ${at}s did not finish`);
      const batch = requests.filter((request) => request.elapsed === at);
      assert.deepEqual(batch.map(requestKind).sort(), expected.sort(), `HTTP budget at ${at}s`);
      assert.ok(batch.every(({ method }) => method === "GET"));
      rounds.push({ at, kinds: expected });
    }
    const infraRequests = requests.slice().sort((a, b) => a.elapsed - b.elapsed ||
      a.path.localeCompare(b.path) || a.search.localeCompare(b.search));
    await page.evaluate(() => {
      history.pushState({}, "", "/");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    await tick(30);
    assert.equal(requests.length, infraRequests.length, "polling after leaving /infra");
    return { rounds, requests: infraRequests, startupRequests };
  } finally { await context.close(); }
}

async function verifyOverviewNavigation(browser, report) {
  const data = fixture({ down: true });
  const view = await open(browser, { width: 390, height: 844 }, data, new Map());
  const { page, context, errors, tick } = view;
  const table = page.locator(".overview-table-wrap");
  const distHref = `/ping?target=${encodeURIComponent(identity(dist))}`;
  const coreHref = `/ping?target=${encodeURIComponent(identity(core))}`;
  try {
    await ready(page);
    assert.equal(await part(page, "faults").locator(".overview-faults a").first().getAttribute("href"), distHref);
    const keyboardOrder = [
      distHref, "/wireless", "/ping", "overview-table-wrap", coreHref
    ];
    await page.locator(".overview-primary-link").focus();
    for (const expected of keyboardOrder) {
      await page.keyboard.press("Tab");
      const active = await page.evaluate(() => document.activeElement.getAttribute("href") || document.activeElement.className);
      assert.equal(active, expected);
    }
    await page.keyboard.press("Shift+Tab");
    assert.equal(await table.evaluate((node) => document.activeElement === node), true);
    assert.equal(await table.evaluate((node) => getComputedStyle(node).outlineStyle), "solid");
    assert.equal(await page.locator(".overview-table-hint").isVisible(), true);
    await page.keyboard.press("ArrowRight");
    await until(() => table.evaluate((node) => node.scrollLeft > 0), "keyboard could not scroll the gateway table");
    const offset = await table.evaluate((node) => node.scrollLeft);
    data.setStatus(core, "DEGRADED");
    await tick(5);
    await table.locator('[data-status="DEGRADED"]').waitFor();
    assert.equal(await table.evaluate((node) => document.activeElement === node), true);
    assert.ok(await table.evaluate((node) => node.scrollLeft) >= offset, "refresh reset table scroll");
    await table.evaluate((node) => { node.scrollLeft = 0; });
    const cdp = await context.newCDPSession(page);
    const swipe = async (from, to) => {
      await cdp.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x: from.x, y: from.y }] });
      for (let i = 1; i <= 7; i++) {
        await cdp.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{
          x: from.x + (to.x - from.x) * i / 7, y: from.y + (to.y - from.y) * i / 7
        }] });
      }
      await cdp.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
    };
    const box = await table.boundingBox();
    const y = Math.round(Math.max(box.y, 0) + 35);
    await swipe({ x: 310, y }, { x: 93, y });
    await until(() => table.evaluate((node) => node.scrollLeft > 0), "touch gesture did not scroll the gateway table");
    await page.evaluate(() => window.scrollTo(0, 0));
    await swipe({ x: 180, y: 790 }, { x: 180, y: 305 });
    await until(() => page.evaluate(() => scrollY > 100 && scrollX === 0), "mobile page did not scroll vertically");
    await table.evaluate((node) => { node.scrollLeft = 0; });
    await table.locator(`a[href="${coreHref}"]`).tap();
    await page.waitForURL((url) => url.pathname === "/ping" && url.searchParams.get("target") === identity(core));
    await page.locator("#pingDetail").waitFor();
    await until(async () => (await page.locator("#pingSelected").textContent()).includes(core.instance),
      "Ping history did not restore the gateway identity");
    assert.deepEqual(errors, []);
    report.navigation = { deepLink: true, tabOrder: keyboardOrder, keyboardScroll: true, touchScroll: true,
      touchDeepLink: true, preservedScrollAndFocusOnRefresh: true };
  } finally { await context.close(); }
}

async function verifyOtherRoutes(browser, report) {
  for (const [mode, seats] of Object.entries(modes)) {
    const { page, context, errors } = await open(browser, { width: 1366, height: 768 }, fixture({ count: 5 }),
      new Map(), "/" + mode);
    try {
      const tiles = page.locator("#tournamentPanel a.seat-slot");
      await until(async () => await tiles.count() === seats, `${mode} did not render ${seats} occupied seats`);
      assert.equal(await page.locator("#infraOverview").isVisible(), false);
      const ping = page.locator("#pingGaugeGrid .gauge-item");
      const uptime = page.locator("#uptimeGaugeGrid .gauge-item");
      await ping.first().waitFor();
      await uptime.first().waitFor();
      await page.locator("#pingTrendChart svg").first().waitFor();
      const isps = page.locator("#ispGrid .isp-panel h2");
      await until(async () => await isps.count() === 2, `${mode} WAN carousel did not load`);
      const pages = [await isps.allTextContents()];
      for (let i = 0; i < 2; i++) {
        await page.locator(".isp-page-next").click();
        pages.push(await isps.allTextContents());
      }
      assert.deepEqual(pages, [["WAN 1", "WAN 2"], ["WAN 3", "WAN 4"], ["WAN 5"]]);
      assert.match(await tiles.first().getAttribute("href"), /^\/latency\?/);
      assert.deepEqual(errors, []);
      report.routes["/" + mode] = { seats, ping: await ping.count(), uptime: await uptime.count(), ispPages: pages };
    } finally { await context.close(); }
  }
  for (const [route, selector] of [["/ping", "#pingWorkspace"], ["/latency", "#evidenceTeam"], ["/control", "#controlLoginForm"]]) {
    const { page, context, errors } = await open(browser, { width: 1366, height: 768 }, fixture(), new Map(), route);
    try {
      await page.locator(selector).waitFor();
      assert.equal(await page.locator("#infraOverview").isVisible(), false);
      assert.deepEqual(errors, []);
      report.routes[route] = "PASS";
    } finally { await context.close(); }
  }
}

async function main() {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined });
  const report = { chromium: browser.version(), baseline: process.env.INFRA_BASE_REF || null,
    scenarios: {}, before: {}, routes: {} };
  try {
    await verifyScenarios(browser, report);
    console.log(`Geometry and status checked in ${Object.keys(report.scenarios).length} Chromium scenarios`);
    await verifyOverviewNavigation(browser, report);
    await verifyOtherRoutes(browser, report);
    const after = await verifyBudget(browser, new Map());
    if (baseline.size) {
      const before = await verifyBudget(browser, baseline);
      assert.deepEqual(after.startupRequests, before.startupRequests, "layout changed browser startup requests");
      assert.deepEqual(after.requests, before.requests, "layout changed HTTP requests or Prometheus expressions");
      report.http = { before: before.requests.length, after: after.requests.length, added: 0, rounds: after.rounds };
    } else {
      report.http = { before: null, after: after.requests.length, rounds: after.rounds };
    }
    console.log("HTTP, deep link, keyboard/touch and all route regressions passed", JSON.stringify(report.http));
  } finally {
    fs.writeFileSync(path.join(output, "infra-browser-results.json"), JSON.stringify(report, null, 2));
    await browser.close();
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
