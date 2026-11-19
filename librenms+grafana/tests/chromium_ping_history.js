const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const model = require("../bigscreen/ping/model.js");
const { plot } = (() => {
  const window = { BSUtils: { escapeHtml: String } };
  require("node:vm").runInNewContext(fs.readFileSync(require.resolve("../bigscreen/ping/charts.js"), "utf8"), { window });
  return window.BSPingCharts;
})();

const staticDir = path.join(__dirname, "..", "bigscreen");
const host = "http://ping-history-fixture.invalid";
const end = 1700000400;
const overviewStart = end - 4800;
const core = { job: "infra-core-ping", instance: "Fixed point fixture", target_ip: "192.0.2.10" };
const el = (page, name) => page.locator("#ping" + name);

function matrix(metric, points) {
  return Object.entries({
    up: points.map(({ t, scrape }) => [t, String(scrape ?? 1)]),
    probe_success: points.filter(({ success }) => success != null).map(({ t, success }) => [t, String(success)]),
    probe_icmp_duration_seconds: points.filter(({ success }) => success === 1).map(({ t }) => [t, "0.012"])
  }).map(([name, values]) => ({
    metric: { ...metric, __name__: name, ...(name === "probe_icmp_duration_seconds" ? { phase: "rtt" } : {}) },
    values
  }));
}

async function openHistory(browser, viewport, { start, keys, samples, segments = [], delays, config = {} }) {
  const context = await browser.newContext({ viewport, hasTouch: viewport.width === 390, timezoneId: "UTC" });
  const page = await context.newPage();
  page.setDefaultTimeout(12000);
  const errors = [];
  const requests = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await context.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    const json = (body) => route.fulfill({ json: body });
    if (url.origin !== host) return route.abort();
    if (url.pathname.endsWith("/resolutions")) return json({
      ok: true, segments, more: false, nextCursor: segments.at(-1)?.generation ?? 0
    });
    if (url.pathname.startsWith("/platform-api/")) return json({ ok: true, targets: [] });
    if (url.pathname.startsWith("/prometheus/api/v1/")) {
      const query = url.searchParams.get("query") || "";
      const raw = query.startsWith("{__name__");
      const step = Number(url.searchParams.get("step"));
      const time = Number(url.searchParams.get("time"));
      let rows = [];
      if (raw) {
        const span = Number(query.match(/\[(\d+)s\]$/)?.[1]);
        requests.push({ type: "raw", time, span, dns: query.includes('job="custom-ping"') });
        if (delays) await delays(time);
        rows = samples(time, span).flatMap(({ metric, points }) => matrix(metric,
          points.filter(({ t }) => t > time - span && t <= time)));
      } else if (step) {
        requests.push({ type: "range", step });
        if (query.includes("count_over_time")) {
          const first = Number(url.searchParams.get("start"));
          const last = Number(url.searchParams.get("end"));
          const slots = Array.from({ length: Math.round((last - first) / step) + 1 }, (_, i) => first + i * step);
          rows = ["count", "ok"].map((ping_field) => ({
            metric: { ...core, ping_field }, values: slots.map((t) => [t, String(step)])
          }));
        }
      } else {
        requests.push({ type: "instant" });
      }
      return json({ status: "success", data: { resultType: raw || step ? "matrix" : "vector", result: rows } });
    }
    if (url.pathname === "/config.js") return route.fulfill({
      body: "window.BIGSCREEN_CONFIG=" + JSON.stringify(config), contentType: "text/javascript"
    });
    const file = path.resolve(staticDir, url.pathname === "/ping" ? "index.html" : url.pathname.slice(1));
    if (!file.startsWith(staticDir + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
      return route.fulfill({ status: 404 });
    }
    const types = { ".js": "text/javascript", ".css": "text/css", ".html": "text/html" };
    return route.fulfill({ path: file, contentType: types[path.extname(file)] || "application/octet-stream" });
  });
  const params = new URLSearchParams({ view: "history", from: start, to: end });
  keys.forEach((key, index) => params.append(index ? "compare" : "target", key));
  try {
    await page.goto(host + "/ping?" + params, { waitUntil: "domcontentloaded" });
    await el(page, "OverviewChart").locator("svg").waitFor();
    return { context, page, requests, errors };
  } catch (error) {
    await context.close();
    throw error;
  }
}

async function pickChart(page, name, t, touch = false) {
  const svg = el(page, name).locator("svg");
  await svg.scrollIntoViewIfNeeded();
  const { x, y } = await svg.evaluate((svg, { t, left, right }) => {
    const start = Number(svg.dataset.chartStart);
    const end = Number(svg.dataset.chartEnd);
    const point = svg.createSVGPoint();
    point.x = left + (t - start) * (right - left) / (end - start);
    point.y = 85;
    const screen = point.matrixTransform(svg.getScreenCTM());
    return { x: screen.x, y: screen.y };
  }, { t, left: plot.left, right: plot.right });
  await (touch ? page.touchscreen.tap(x, y) : page.mouse.click(x, y));
}

async function downloadCsv(page) {
  const received = page.waitForEvent("download");
  await el(page, "Export").click();
  return fs.readFileSync(await (await received).path(), "utf8");
}

async function observeFixedPoint(browser, viewport, options = {}) {
  const samples = Array.from({ length: 300 }, (_, i) => ({ t: end - 299 + i, success: 1 }));
  const { page, context, requests, errors } = await openHistory(browser, viewport, {
    start: overviewStart, keys: [model.identity(core)], samples: () => [{ metric: core, points: samples }],
    config: { pingIntervals: { infra: "1s" } }, ...options
  });
  const read = async () => ({
    title: await el(page, "DrillStatistics").locator("h4").first().textContent(),
    metrics: await el(page, "DrillMetrics").innerText(),
    pinned: await el(page, "Pinned").innerText(),
    caption: await el(page, "DrillCaption").textContent(),
    window: await el(page, "StatsWindow").inputValue()
  });
  try {
    await pickChart(page, "OverviewChart", end, viewport.width === 390);
    await el(page, "Distribution").locator("svg").waitFor();
    const full = await read();
    const csvBefore = await downloadCsv(page);
    const before = requests.length;
    const originalSvg = await el(page, "Distribution").locator("svg").elementHandle();
    await pickChart(page, "Distribution", end - 290, viewport.width === 390);
    const clipped = await read();
    const widthOk = await el(page, "Workspace").evaluate((root) => root.scrollWidth <= root.clientWidth);
    if (options.onClipped) await options.onClipped(page);
    const csvAfter = await downloadCsv(page);
    return {
      full, clipped, widthOk, errors, requests, extra: requests.length - before,
      sameChart: await originalSvg.evaluate((svg) => svg.isConnected),
      sameCsv: csvBefore === csvAfter, csvSamples: csvBefore.trim().split("\n").length - 1
    };
  } finally {
    await context.close();
  }
}

async function verifyStatisticsEdges(browser, output) {
  const viewport = { width: 1366, height: 768 };
  const start = end - 300;
  const points = [0.25, 0.75, 2, 10].map((offset) => ({ t: start + offset, success: 1 }));
  const fixed = await openHistory(browser, viewport, {
    start, keys: [model.identity(core)], samples: () => [{ metric: core, points }],
    config: { pingIntervals: { infra: "1s" } }
  });
  try {
    const { page } = fixed;
    const title = el(page, "DrillStatistics").locator("h4").first();
    const metrics = el(page, "DrillMetrics");
    await pickChart(page, "OverviewChart", start + 0.25);
    assert.match(await title.innerText(), /实际 250 毫秒（受限于历史查询边界）.*22:15:00\.250/);
    assert.match(await metrics.innerText(), /1 个有效 RTT 样本/);
    await pickChart(page, "OverviewChart", start + 10);
    assert.match(await title.innerText(), /所选 5 分钟 · 实际 10 秒（受限于历史查询边界）/);
    assert.match(await metrics.innerText(), /采样覆盖 40\.0%（按 10 秒）.*4 个有效 RTT/s);
    assert.equal(fixed.requests.filter(({ type }) => type === "raw").length, 1);
    assert.deepEqual(fixed.errors, []);
  } finally {
    await fixed.context.close();
  }

  const id = "abcd1234-1000-4000-8000-000000000118";
  const ipA = "192.0.2.31";
  const ipB = "192.0.2.32";
  const metric = (target_ip) => ({ job: "custom-ping", instance: id, custom_id: id, target_ip });
  const segment = (generation, ip, start, end, sampleNotBefore) =>
    ({ id, address: "stats.example.test", generation, ip, start, end, sampleNotBefore });
  const dns = await openHistory(browser, viewport, {
    start, keys: ["custom-dns:" + id],
    segments: [
      segment(1, ipA, start - 1, start + 100, start),
      segment(2, ipB, start + 100, start + 200, start + 101),
      segment(3, ipA, start + 200, null, start + 201)
    ],
    samples: () => [
      { metric: metric(ipA), points: [2, 98, 150, 202, 300].map((offset) => ({ t: start + offset, success: 1 })) },
      { metric: metric(ipB), points: [101, 102, 198].map((offset) => ({ t: start + offset, success: 1 })) }
    ]
  });
  try {
    const { page } = dns;
    await pickChart(page, "OverviewChart", end);
    const cards = el(page, "DrillMetrics").locator("article");
    const text = await cards.allInnerTexts();
    assert.equal(text.length, 3);
    for (const [index, ip, duration, from, to] of [
      [0, ipA, "1 分钟 40 秒", "22:15:00", "22:16:40"],
      [1, ipB, "1 分钟 39 秒", "22:16:41", "22:18:20"],
      [2, ipA, "1 分钟 39 秒", "22:18:21", "22:20:00"]
    ]) {
      assert.ok([ip, "代次 " + (index + 1), "（按 " + duration + "）", from, to, "2 个有效 RTT"].every(
        (label) => text[index].includes(label)), text[index]);
    }
    const csv = await downloadCsv(page);
    assert.equal(csv.trim().split(/\r?\n/).filter((line) => line.endsWith(",false")).length, 2);
    assert.equal(dns.requests.filter(({ type }) => type === "raw").length, 1);
    assert.deepEqual(dns.errors, []);
    await el(page, "DrillStatistics").screenshot({ path: path.join(output, "fixed-window-dns-generations.png") });
  } finally {
    await dns.context.close();
  }
}

async function verifyFixedPoint(browser, dims, output) {
  const evidence = [];
  for (const [width, height] of dims) {
    const data = await observeFixedPoint(browser, { width, height }, {
      onClipped: (page) => el(page, "DrillStatistics").screenshot({ path: path.join(output, "fixed-window-" + width + ".png") })
    });
    assert.match(data.full.title, /所选 5 分钟 · 实际 5 分钟/);
    assert.match(data.full.metrics, /采样覆盖 100\.0%（按 5 分钟）[\s\S]*300 个有效 RTT/);
    assert.match(data.clipped.title, /所选 5 分钟 · 实际 10 秒（受限于下钻加载范围不足）/);
    assert.match(data.clipped.metrics, /采样覆盖 100\.0%（按 10 秒）[\s\S]*10 个有效 RTT/);
    assert.match(data.clipped.pinned, /22:15:10/);
    assert.equal(data.clipped.caption, data.full.caption);
    assert.equal(data.clipped.window, "300");
    assert.equal(data.sameChart && data.sameCsv && data.widthOk, true);
    assert.equal(data.csvSamples, 300);
    assert.equal(data.extra, 0);
    assert.deepEqual(data.requests.filter(({ type }) => type === "raw"), [
      { type: "raw", time: end, span: 300, dns: false }
    ]);
    assert.equal(data.requests.filter(({ type }) => type === "range").length, 2);
    assert.deepEqual(data.errors, []);
    evidence.push({ width, height, ...data });
  }
  await verifyStatisticsEdges(browser, output);
  fs.writeFileSync(path.join(output, "history-fixed-point.json"), JSON.stringify(evidence, null, 2));
}

module.exports = { end, el, openHistory, observeFixedPoint, pickChart, verifyFixedPoint };
