const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const { verifyFixedPoint, verifyLossBuckets } = require("./chromium_ping_history.js");

const staticDir = path.join(__dirname, "..", "bigscreen");
const output = process.env.PING_BROWSER_OUTPUT || path.join(require("node:os").tmpdir(), "ping-d3-evidence");
const fixtureHost = "http://ping-domain-fixture.invalid";
const api = "/platform-api/ping/targets";
const dims = [[1920, 1080, 5], [1366, 768, 4], [390, 844, 1]];
const record = (name, value) => fs.writeFileSync(path.join(output, name + ".json"), JSON.stringify(value, null, 2));
const screenshot = (page, name) => page.screenshot({ path: path.join(output, name + ".png") });
const domainHost = "game.example.test";
const longHost = "remote-" + "a".repeat(50) + "." + "cloud-" + "b".repeat(54) + ".game.example.test";
const domainCard = (page, domain = domainHost) => page.locator(".ping-card-domain").filter({
  has: page.getByText(domain, { exact: true })
});
const el = (page, name) => page.locator("#ping" + name);

function matrix(metric, at, count = 31, interval = 2) {
  return ["up", "probe_success", "probe_icmp_duration_seconds"].map((field) => ({
    metric: { ...metric, __name__: field, ...(field === "probe_icmp_duration_seconds" ? { phase: "rtt" } : {}) },
    values: Array.from({ length: count }, (_, i) => [at - (count - i) * interval, field === "probe_icmp_duration_seconds" ? "0.012" : "1"])
  }));
}

function rawQueryKind(expression) {
  if (!expression.startsWith("{__name__")) return null;
  if (expression.endsWith("[300s]")) return "miniTrend";
  if (expression.endsWith("[60s]") && /,custom_id=/.test(expression)) return "dnsStatus";
  return null;
}

function miniTrendBudget(queries) {
  assert.ok(queries.miniTrend.started > 0);
  assert.ok(queries.miniTrend.peak <= 2, JSON.stringify(queries.miniTrend));
  assert.equal(queries.miniTrend.inFlight, 0);
  return { limit: 2, verdict: "PASS" };
}

function observePrometheusQueries(page, isWall = () => true) {
  const counters = Object.fromEntries(["all", "miniTrend", "dnsStatus"].map((name) => [
    name, { started: 0, finished: 0, failed: 0, peak: 0 }
  ]));
  const inFlight = ({ started, finished, failed }) => started - finished - failed;
  const pending = new Map();

  function queryUrl(request) {
    const url = new URL(request.url());
    return url.origin === fixtureHost && /^\/prometheus\/api\/v1\/query(?:_range)?$/.test(url.pathname) ? url : null;
  }

  page.on("request", (request) => {
    const url = queryUrl(request);
    if (!url) return;
    const kind = url.pathname.endsWith("/query") && isWall() && rawQueryKind(url.searchParams.get("query") || "");
    const groups = kind ? [counters.all, counters[kind]] : [counters.all];
    assert.ok(!pending.has(request), "duplicate query start event");
    pending.set(request, groups);
    for (const group of groups) {
      group.started++;
      group.peak = Math.max(group.peak, inFlight(group));
    }
  });

  function settle(request, terminal) {
    if (!queryUrl(request)) return;
    const groups = pending.get(request);
    assert.ok(pending.delete(request), "query ended without exactly one pending request");
    for (const group of groups) group[terminal]++;
  }

  page.on("requestfinished", (request) => settle(request, "finished"));
  page.on("requestfailed", (request) => settle(request, "failed"));

  function snapshot() {
    return Object.fromEntries(Object.entries(counters).map(([name, group]) => [
      name, { ...group, inFlight: inFlight(group) }
    ]));
  }

  async function drain() {
    const until = performance.now() + 5000;
    while (pending.size && performance.now() < until) {
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    const report = snapshot();
    assert.equal(pending.size, 0, JSON.stringify(report));
    for (const group of Object.values(report)) assert.equal(group.inFlight, 0);
    return report;
  }

  return { snapshot, drain };
}

async function verifyQueryLifecycle(browser) {
  const context = await browser.newContext();
  try {
    const page = await context.newPage();
    const queries = observePrometheusQueries(page);
    await page.route(fixtureHost + "/lifecycle", (route) => route.fulfill({ contentType: "text/html", body: "<html></html>" }));
    await page.goto(fixtureHost + "/lifecycle");
    await page.evaluate(() => { window.queryControllers = {}; });

    async function start(id, expression) {
      const url = new URL("/prometheus/api/v1/query", fixtureHost);
      url.searchParams.set("query", expression);
      url.searchParams.set("case", id);
      let accept;
      const arrived = new Promise((resolve) => { accept = resolve; });
      await page.route((requestUrl) => requestUrl.href === url.href, accept, { times: 1 });
      const request = page.waitForRequest((event) => event.url() === url.href, { timeout: 5000 });
      const result = page.evaluate(async ({ id, href }) => {
        const controller = new AbortController();
        window.queryControllers[id] = controller;
        try {
          const response = await fetch(href, { signal: controller.signal });
          await response.text();
          return response.status;
        } catch (error) {
          return error.name;
        }
      }, { id, href: url.href });
      await request;
      let timer;
      try {
        const deadline = new Promise((_, reject) => {
          timer = setTimeout(() => reject(new Error("route unavailable for " + id)), 5000);
        });
        return { route: await Promise.race([arrived, deadline]), result };
      } finally {
        clearTimeout(timer);
      }
    }

    for (const status of [200, 503, 201]) {
      const call = await start(String(status), "vector(1)");
      await call.route.fulfill({ status, body: "ok" });
      assert.equal(await call.result, status);
      await queries.drain();
    }
    const sequential = (await queries.drain()).all;
    assert.deepEqual(sequential, { started: 3, finished: 3, failed: 0, peak: 1, inFlight: 0 });

    const fixed = '{__name__="up",instance="fixed"}[300s]';
    const dnsTrend = '{__name__="up",custom_id="domain"}[300s]';
    const dnsStatus = '{__name__="up",custom_id="domain"}[60s]';
    const parallel = await Promise.all([start("fixed", fixed), start("dns-trend", dnsTrend), start("dns-status", dnsStatus)]);
    const held = queries.snapshot();
    assert.deepEqual(Object.values(held).map(({ inFlight }) => inFlight), [3, 2, 1]);
    await Promise.all(parallel.map(({ route }) => route.fulfill({ body: "ok" })));
    assert.deepEqual(await Promise.all(parallel.map(({ result }) => result)), [200, 200, 200]);
    const concurrent = await queries.drain();
    assert.deepEqual(Object.values(concurrent).map(({ peak }) => peak), [3, 2, 1]);

    const failed = await start("failed", fixed);
    await failed.route.abort("failed");
    assert.equal(await failed.result, "TypeError");
    await queries.drain();
    const cancelled = await start("cancelled", dnsTrend);
    await page.evaluate(() => window.queryControllers.cancelled.abort());
    assert.equal(await cancelled.result, "AbortError");
    await queries.drain();
    const timedOut = await start("timeout", dnsStatus);
    await timedOut.route.abort("timedout");
    assert.equal(await timedOut.result, "TypeError");
    const final = await queries.drain();
    assert.deepEqual(final, {
      all: { started: 9, finished: 6, failed: 3, peak: 3, inFlight: 0 },
      miniTrend: { started: 4, finished: 2, failed: 2, peak: 2, inFlight: 0 },
      dnsStatus: { started: 2, finished: 1, failed: 1, peak: 1, inFlight: 0 }
    });
    record("query-lifecycle", { sequential, concurrent, final });
    console.log("PING_QUERY_LIFECYCLE " + JSON.stringify({ sequential, concurrent, final }));
  } finally {
    await context.close();
  }
}

function fixture(size, { builtIn = false, infrastructure = 0, miniTrendSeconds = 60, beforeMiniTrend,
  alternateNames = false } = {}) {
  const instant = () => Math.floor(Date.now() / 1000);
  const first = instant() - 120;
  const enabled = Math.min(size, 100);
  const list = Array.from({ length: size }, (_, i) => {
    const id = "abcd1234-1000-4000-8000-" + String(i + 1).padStart(12, "0");
    const ip = "10.0." + Math.floor(i / 250) + "." + (i % 250 + 1);
    const alternate = "batch" + i + ".example.test";
    const common = { id, name: alternateNames ? alternate : "Game " + i, group: "Remote", enabled: i < enabled, revision: i + 1,
      updatedAt: new Date(first * 1000).toISOString() };
    const address = alternateNames ? alternate : i === 0 ? longHost : "game" + i + ".example.test";
    return i % 2 ? { ...common, ip } : { ...common, address,
      kind: "dns", endpointRevision: i + 1, privateCidrs: [], resolvedIp: ip, aCount: 3, dnsStatus: "DNS_OK",
      resolutionGeneration: i + 1, sampleNotBefore: first, expiresAt: first + 300, dnsError: "" };
  });
  const active = list.filter((row) => row.enabled);
  const metric = (row) => ({ job: "custom-ping", instance: row.id, custom_id: row.id, target_ip: row.ip || row.resolvedIp });
  const state = { ok: true, revision: size, manifestRevision: size, publication: "published", publishedRevision: size, dnsEnabled: true };
  let catalog = { ...state, targets: list };
  let discovery = { ...state, discoveredRevision: size,
    activeTargets: active.map((row) => ({
      id: row.id, ip: row.ip || row.resolvedIp, revision: row.revision, resolutionGeneration: row.resolutionGeneration
    })), dnsTargets: active.filter((row) => row.kind === "dns").map((row) => ({
      id: row.id, dnsStatus: row.dnsStatus, resolvedIp: row.resolvedIp, resolutionGeneration: row.resolutionGeneration,
      discoveredGeneration: row.resolutionGeneration, sampleNotBefore: first, pingStatus: "UP",
      discoveryStatus: "DISCOVERED", withdrawalPending: false
    })) };
  let opened = 0;
  const infrastructureRows = Array.from({ length: infrastructure }, (_, i) => ({
    job: ["infra-isp-ping", "infra-ap-ping", "infra-dist-ping"][Math.min(i % 5, 2)],
    instance: "fixture-" + i,
    target_ip: "10.1." + Math.floor(i / 250) + "." + (i % 250 + 1)
  }));
  const samples = { rawSize: 0, duplicateTargets: 0, dnsRawSize: 0, miniTrendSamplesPerSeries: {} };
  const requested = new Set();
  const intervalFor = (row) => row.job === "infra-isp-ping" ? 1 : row.job === "infra-ap-ping" ? 5 : 2;
  const miniTrendCount = (interval) => miniTrendSeconds === 60 ? 60 / interval + 1 : miniTrendSeconds / interval;
  const ops = [];
  const initial = () => ({ catalog: size, enabled, openCalls: opened });
  async function intercept(route) {
    const req = route.request();
    const url = new URL(req.url());
    const json = (body, status = 200) => route.fulfill({ status, json: body });
    if (url.origin !== fixtureHost) return route.abort();
    if (url.pathname === "/platform-api/auth/status") return json({ ok: true, enabled: true, authenticated: true });
    if (url.pathname === api + "/csrf") return json({ ok: true, csrfToken: "a".repeat(64) });
    if (url.pathname === api && req.method() === "GET") { opened++; return json(catalog); }
    if (url.pathname === api + "/discovery") return json(discovery);
    if (req.method() === "GET" && url.pathname.endsWith("/resolutions")) {
      const id = url.pathname.split("/").at(-2);
      const row = list.find((item) => item.id === id && item.kind === "dns");
      if (!row) return json({ ok: true, segments: [], more: false, nextCursor: 0 });
      return json({ ok: true, segments: [{ id, address: row.address, ip: row.resolvedIp,
        generation: row.resolutionGeneration, start: first - 1, end: null, sampleNotBefore: first }],
        more: false, nextCursor: row.resolutionGeneration });
    }
    if (req.method() === "DELETE" && builtIn && url.pathname === api + "/" + list[0].id) {
      const deleted = list[0];
      const revision = catalog.revision + 1;
      assert.equal(req.headers()["if-match"], JSON.stringify(String(deleted.revision)));
      assert.equal(req.headers()["x-csrf-token"], "a".repeat(64));
      catalog = { ...catalog, revision, manifestRevision: revision, publishedRevision: revision, targets: [] };
      discovery = { ...discovery, revision, publishedRevision: revision, discoveredRevision: null, activeTargets: [], dnsTargets: [] };
      return json({ ok: true, target: null, id: deleted.id, savedRevision: revision, revision, replayed: false,
        publication: "published", publishedRevision: revision });
    }
    if (req.method() === "PATCH" && url.pathname.startsWith(api + "/")) {
      const data = req.postDataJSON();
      assert.ok(req.headers()["idempotency-key"]);
      assert.ok(req.headers()["if-match"]);
      assert.equal(req.headers()["x-csrf-token"], "a".repeat(64));
      ops.push(data);
      return json({ ok: false, error: "custom_ping_revision_conflict" }, 412);
    }
    if (url.pathname.startsWith("/prometheus/api/v1/")) {
      const expression = url.searchParams.get("query") || "";
      const at = Number(url.searchParams.get("time")) || instant();
      let rows = [];
      const raw = expression.startsWith("{__name__");
      if (expression.includes('job="custom-ping"')) {
        rows = raw ? active.flatMap((row) => matrix(metric(row), at)) :
          active.flatMap((row) => Object.entries({
            source: 1, source_at: at - 2, probe: 1, probe_at: at - 2, rtt: .012, rtt_at: at - 2, count: 30, ok: 30
          }).map(([field, value]) => ({ metric: { ...metric(row), ping_field: field }, value: [at, String(value)] })));
      }
      if (builtIn && !raw && expression.includes("infra-core-ping")) {
        const labels = { job: "infra-core-ping", instance: "Sync fixture core", target_ip: "192.0.2.254" };
        rows = Object.entries({ target: 1, source: 1, source_at: at - 2, probe: 1, probe_at: at - 2,
          rtt: .005, rtt_at: at - 2, count: 30, ok: 30 })
          .map(([field, value]) => ({ metric: { ...labels, ping_field: field }, value: [at, String(value)] }));
      }
      const dnsRead = raw && /,custom_id=/.test(expression);
      const kind = rawQueryKind(expression);
      if (raw && (infrastructure || kind === "miniTrend" && !builtIn)) {
        const field = dnsRead ? "custom_id" : "instance";
        const matcher = dnsRead ? /custom_id=(~?)("(?:[^"\\]|\\.)*")/g : /instance=(~?)("(?:[^"\\]|\\.)*")/g;
        const selectors = [...expression.matchAll(matcher)].map((match) => [match[1], JSON.parse(match[2])]);
        const population = dnsRead ? active.map(metric) : [...infrastructureRows, ...active.map(metric)];
        const matched = population.filter((row) => selectors.some(([regex, value]) =>
          regex ? new RegExp("^(?:" + value + ")$").test(row[field]) : value === row[field]));
        assert.ok(matched.length, expression);
        if (kind === "miniTrend") {
          samples.rawSize = Math.max(samples.rawSize, matched.length);
          assert.ok(matched.length <= 12, "one mini-trend HTTP query targets more than 12 endpoints");
          for (const row of matched) {
            const key = at + ":" + row.job + ":" + row.instance;
            if (requested.has(key)) samples.duplicateTargets++;
            requested.add(key);
          }
          for (const row of matched) {
            const interval = intervalFor(row);
            samples.miniTrendSamplesPerSeries[interval] = miniTrendCount(interval);
          }
          if (beforeMiniTrend && await beforeMiniTrend({ dns: dnsRead, targets: matched.map((row) => row.instance) }) === "fail") {
            return route.abort("failed");
          }
        } else if (kind === "dnsStatus") {
          samples.dnsRawSize = Math.max(samples.dnsRawSize, matched.length);
        }
        rows = matched.flatMap((row) => {
          const interval = intervalFor(row);
          const fullWindow = kind === "miniTrend" && miniTrendSeconds === 300;
          return matrix(row, fullWindow ? at + interval : at,
            kind === "miniTrend" ? miniTrendCount(interval) : 60 / interval + 1, interval);
        });
      } else if (infrastructure && !expression.includes('job="custom-ping"')) {
        rows = infrastructureRows.flatMap((row) => Object.entries({
          target: 1, source: 1, source_at: at - 1, probe: 1, probe_at: at - 1,
          rtt: .012, rtt_at: at - 1, count: 60 / intervalFor(row), ok: 60 / intervalFor(row)
        }).map(([field, value]) => ({ metric: { ...row, ping_field: field }, value: [at, String(value)] })));
      }
      await new Promise((resolve) => setTimeout(resolve, 15));
      return json({ status: "success", data: { resultType: raw ? "matrix" : "vector", result: rows } });
    }
    if (url.pathname === "/config.js") return route.fulfill({ body: "window.BIGSCREEN_CONFIG={};", contentType: "text/javascript" });
    const file = path.resolve(staticDir, url.pathname === "/ping" ? "index.html" : url.pathname.slice(1));
    if (!file.startsWith(staticDir + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) return route.fulfill({ status: 404 });
    const types = { ".js": "text/javascript", ".css": "text/css", ".html": "text/html", ".svg": "image/svg+xml" };
    return route.fulfill({ path: file, contentType: types[path.extname(file)] || "application/octet-stream" });
  }
  return { intercept, initial, ops, samples, snapshot: () => ({ catalog, discovery }),
    setSnapshot(next) { ({ catalog, discovery } = next); } };
}

async function layout(browser) {
  const evidence = [];
  for (const [width, height, columns] of dims) {
    const context = await browser.newContext({ viewport: { width, height }, hasTouch: width === 390 });
    const data = fixture(width === 1920 ? 300 : width === 1366 ? 100 : 50);
    await context.route("**/*", (route) => data.intercept(route));
    const page = await context.newPage();
    page.setDefaultTimeout(11000);
    let onWall = true;
    const monitor = observePrometheusQueries(page, () => onWall);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    try {
      const started = Date.now();
      await page.goto(fixtureHost + "/ping", { waitUntil: "domcontentloaded" });
      const wall = el(page, "Wall");
      const count = data.initial().enabled;
      await page.waitForFunction((count) => document.querySelectorAll("#pingWall .ping-card").length === count, count);
      const cardsMs = Date.now() - started;
      await wall.locator(".ping-card-chart svg").first().waitFor();
      const firstTrendMs = Date.now() - started;
      await domainCard(page, longHost).locator(".ping-card-dns").getByText(/3 个 A 记录/).waitFor();
      const metrics = await wall.evaluate((wall) => ({
        columns: getComputedStyle(wall).gridTemplateColumns.split(" ").length,
        wallWidth: wall.clientWidth, wallScroll: wall.scrollWidth, documentWidth: document.documentElement.clientWidth,
        documentScroll: document.documentElement.scrollWidth, parentWidth: wall.closest("#pingWorkspace").clientWidth,
        parentScroll: wall.closest("#pingWorkspace").scrollWidth
      }));
      assert.equal(metrics.columns, columns);
      assert.ok(metrics.documentScroll <= metrics.documentWidth && metrics.wallScroll <= metrics.wallWidth &&
        metrics.parentScroll <= metrics.parentWidth, JSON.stringify(metrics));
      assert.equal(await domainCard(page, longHost).locator("strong").getAttribute("title"), longHost);
      assert.match(await domainCard(page, longHost).locator(".ping-card-ip").textContent(), /当前解析：10\.0\.0\.1/);
      await screenshot(page, "wall-" + width + "x" + height);
      if (width === 390) await el(page, "CustomManage").tap();
      else await el(page, "CustomManage").click();
      await el(page, "CustomRows").getByRole("button", { name: "编辑 Game 0", exact: true }).click();
      await el(page, "CustomPrivate").locator("summary").click();
      await el(page, "CustomCIDRs").fill("10.0.0.0/24");
      await el(page, "CustomIP").fill("new.example.test");
      assert.equal(await el(page, "CustomCIDRs").inputValue(), "");
      assert.equal(await el(page, "CustomForm").evaluate((el) => el.scrollWidth <= el.clientWidth), true);
      await screenshot(page, "manager-" + width + "x" + height);
      await el(page, "CustomSave").click();
      await el(page, "CustomReview").waitFor();
      assert.deepEqual(data.ops.at(-1).privateCidrs, []);
      await el(page, "CustomReview").click();
      assert.equal(await el(page, "CustomIP").inputValue(), "new.example.test");
      await el(page, "CustomClose").click();
      onWall = false;
      const history = domainCard(page, longHost).locator(".ping-card-open");
      if (width === 390) await history.tap();
      else await history.click();
      await el(page, "HistoryTitle").getByText(longHost, { exact: true }).waitFor();
      await el(page, "DnsHistory").locator("summary").click();
      await el(page, "DnsSegments").getByText(/严格晚于/).first().waitFor();
      assert.equal(await el(page, "Workspace").evaluate((root) => root.scrollWidth <= root.clientWidth), true);
      await screenshot(page, "history-" + width + "x" + height);
      assert.deepEqual(errors, []);
      const queries = await monitor.drain();
      assert.ok(queries.all.started > 0 && queries.all.peak > 0);
      const miniTrendHttpConcurrency = miniTrendBudget(queries);
      assert.ok(data.samples.rawSize > 0);
      const samples = { ...data.samples };
      onWall = true;
      await el(page, "BackToWall").click();
      await wall.locator(".ping-card-chart svg").first().waitFor();
      await page.waitForTimeout(250);
      const throughReturn = await monitor.drain();
      miniTrendBudget(throughReturn);
      evidence.push({ width, height, ...metrics, elapsedMs: Date.now() - started, ...data.initial(),
        queryCount: queries.all.started, queryPeak: queries.all.peak, queries, throughReturn,
        cardsMs, firstTrendMs, miniTrendHttpConcurrency, ...samples });
    } finally {
      await context.close();
    }
  }
  record("layouts", evidence);
  console.log("PING_DOMAIN_LAYOUT_QUERIES " + JSON.stringify(evidence));
}

async function mixedPerformance(browser, sampleSeconds, workloads) {
  const results = [];
  for (const [total, custom] of workloads) {
    const infrastructure = total - custom;
    const data = fixture(custom, { infrastructure, miniTrendSeconds: sampleSeconds });
    const context = await browser.newContext({ viewport: { width: 1920, height: 1080 } });
    await context.route("**/*", (route) => data.intercept(route));
    const page = await context.newPage();
    const monitor = observePrometheusQueries(page);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    try {
      // Pin the wall's clock to one cache window while measuring scroll and revisit.
      const now = Math.floor(Date.now() / 30000) * 30000 + 15000;
      await page.clock.setFixedTime(now);
      const start = performance.now();
      await page.goto(fixtureHost + "/ping", { waitUntil: "domcontentloaded" });
      await page.waitForFunction((count) => document.querySelectorAll("#pingWall .ping-card").length === count, total);
      const cardsMs = Math.round(performance.now() - start);
      await page.locator("#pingWall .ping-card-chart svg").first().waitFor();
      const firstTrendMs = Math.round(performance.now() - start);
      await page.waitForTimeout(250);
      const initialRaw = monitor.snapshot().miniTrend.started;

      const rawByScroll = [];
      const renderedFullWindowPoints = {};
      for (const ratio of [0.45, 0.9, ...(sampleSeconds === 300 ? ["AP"] : []), 0]) {
        const before = monitor.snapshot().miniTrend.started;
        if (ratio === "AP") {
          const ap = el(page, "Wall").locator(".ping-card").filter({
            has: page.getByText("AP ICMP", { exact: true })
          }).first();
          await ap.scrollIntoViewIfNeeded();
          const trace = await ap.locator(".ping-trace").getAttribute("d");
          renderedFullWindowPoints.ap = (trace.match(/[ML]/g) || []).length;
          assert.equal(renderedFullWindowPoints.ap, 60);
        } else {
          const offset = await el(page, "Workspace").evaluate((root, ratio) => {
            root.scrollTop = (root.scrollHeight - root.clientHeight) * ratio;
            return root.scrollTop;
          }, ratio);
          if (ratio) assert.ok(offset > 0, "scroll must reach a different viewport");
        }
        await page.waitForTimeout(400);
        rawByScroll.push(monitor.snapshot().miniTrend.started - before);
      }
      assert.ok(rawByScroll[0] + rawByScroll[1] > 0);
      assert.equal(rawByScroll.at(-1), 0, "same-window revisit must reuse the cached first view");
      assert.equal(data.samples.duplicateTargets, 0);
      assert.equal(data.samples.dnsRawSize, Math.ceil(custom / 2));
      assert.ok(data.samples.rawSize > 0 && data.samples.rawSize <= 12);
      if (sampleSeconds === 300) assert.deepEqual(data.samples.miniTrendSamplesPerSeries, { 1: 300, 2: 150, 5: 60 });
      else for (const [interval, count] of Object.entries(data.samples.miniTrendSamplesPerSeries)) {
        assert.equal(count, 60 / Number(interval) + 1);
      }
      const queries = await monitor.drain();
      assert.equal(queries.miniTrend.started, initialRaw + rawByScroll.reduce((sum, count) => sum + count, 0));
      assert.ok(queries.dnsStatus.started > 0);
      assert.deepEqual(errors, []);
      if (sampleSeconds === 300) {
        const trace = await el(page, "Wall").locator(".ping-card").filter({
          has: page.getByText("fixture-0", { exact: true })
        }).locator(".ping-trace").getAttribute("d");
        renderedFullWindowPoints.isp = (trace.match(/[ML]/g) || []).length;
        assert.equal(renderedFullWindowPoints.isp, 300);
      }
      results.push({ total, infrastructure, fixed: Math.floor(custom / 2), dns: Math.ceil(custom / 2),
        cardsMs, firstTrendMs, initialRaw, rawByScroll, cacheRevisitAvoided: initialRaw,
        approxFixtureSampleSeconds: sampleSeconds, queries,
        miniTrendHttpConcurrency: miniTrendBudget(queries),
        ...(sampleSeconds === 300 ? { renderedFullWindowPoints } : {}),
        ...data.samples });
    } finally {
      await context.close();
    }
  }
  record(sampleSeconds === 60 ? "mixed-browser-performance" : "mixed-browser-performance-300s", results);
  console.log((sampleSeconds === 60 ? "PING_DOMAIN_MIXED_BROWSER " : "PING_DOMAIN_MIXED_BROWSER_300S ") + JSON.stringify(results));
}

function holdMiniTrends() {
  const held = [];
  let release = false;
  return {
    held,
    beforeMiniTrend(info) {
      if (release) return;
      return new Promise((finish) => held.push({ ...info, finish }));
    },
    async waitFor(count) {
      const until = performance.now() + 5000;
      while (held.length < count && performance.now() < until) {
        await new Promise((resolve) => setTimeout(resolve, 10));
      }
      assert.ok(held.length >= count, `expected ${count} gated requests, saw ${held.length}`);
    },
    releaseAll() {
      release = true;
      for (const entry of held) entry.finish();
    }
  };
}

async function controlledMiniTrendBudget(browser) {
  const results = [];
  for (const total of [24, 36]) {
    const gate = holdMiniTrends();
    const data = fixture(total, { beforeMiniTrend: gate.beforeMiniTrend, alternateNames: true });
    const context = await browser.newContext({ viewport: { width: 1920, height: 1900 } });
    await context.route("**/*", (route) => data.intercept(route));
    const page = await context.newPage();
    const monitor = observePrometheusQueries(page);
    const fixedTime = Math.floor(Date.now() / 30000) * 30000 + 15000;
    await page.clock.setFixedTime(fixedTime);
    try {
      await page.goto(fixtureHost + "/ping", { waitUntil: "domcontentloaded" });
      const cards = el(page, "Wall").locator(".ping-card");
      await page.waitForFunction((total) => document.querySelectorAll("#pingWall .ping-card").length === total, total);
      assert.deepEqual(await cards.evaluateAll((nodes) => nodes.map((node) => node.classList.contains("ping-card-domain"))),
        Array.from({ length: total }, (_, i) => i % 2 === 0));
      await gate.waitFor(2);
      // Keep both responses gated long enough for an erroneous extra request to arrive.
      await page.waitForTimeout(150);
      assert.equal(monitor.snapshot().miniTrend.inFlight, 2);
      const dns = gate.held.find((request) => request.dns);
      const fixed = gate.held.find((request) => !request.dns);
      assert.equal(dns?.targets.length, 12);
      assert.equal(fixed?.targets.length, 12);
      if (total === 36) {
        fixed.finish("fail");
        await gate.waitFor(3);
        assert.equal(monitor.snapshot().miniTrend.failed, 1);
        assert.equal(monitor.snapshot().miniTrend.inFlight, 2);
        assert.deepEqual([gate.held[2].dns, gate.held[2].targets.length], [false, 6]);
        gate.held[2].finish();
        await gate.waitFor(4);
        assert.deepEqual([gate.held[3].dns, gate.held[3].targets.length], [true, 6]);
        gate.held[3].finish();
      }
      dns.finish();
      fixed.finish();
      await domainCard(page, "batch0.example.test").locator(".ping-card-chart svg").waitFor();
      await page.waitForTimeout(200);
      const queries = await monitor.drain();
      assert.equal(queries.miniTrend.started, total === 24 ? 2 : 4);
      assert.equal(queries.miniTrend.failed, total === 24 ? 0 : 1);
      assert.equal(new Set(gate.held.flatMap(({ targets }) => targets)).size, total);
      assert.equal(data.samples.duplicateTargets, 0);
      if (total === 36) await cards.nth(1).locator(".ping-card-note").getByText("查询失败").waitFor();
      results.push({ total, queries, miniTrendHttpConcurrency: miniTrendBudget(queries), ...data.samples,
        batches: gate.held.map(({ dns, targets }) => ({ dns, size: targets.length })) });
    } finally {
      gate.releaseAll();
      await context.close();
    }
  }
  record("controlled-mini-trend-browser", results);
  console.log("PING_DOMAIN_CONTROLLED_BROWSER " + JSON.stringify(results));
}

async function miniTrendNavigation(browser) {
  const gate = holdMiniTrends();
  const data = fixture(25, { infrastructure: 75, beforeMiniTrend: gate.beforeMiniTrend });
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true });
  await context.route("**/*", (route) => data.intercept(route));
  const page = await context.newPage();
  let onWall = true;
  const monitor = observePrometheusQueries(page, () => onWall);
  try {
    const fixedTime = Math.floor(Date.now() / 30000) * 30000 + 15000;
    await page.clock.setFixedTime(fixedTime);
    await page.goto(fixtureHost + "/ping", { waitUntil: "domcontentloaded" });
    const workspace = el(page, "Workspace");
    const wall = el(page, "Wall");
    await page.waitForFunction(() => document.querySelectorAll("#pingWall .ping-card").length === 100);
    await gate.waitFor(1);
    await workspace.evaluate(async (root) => {
      for (const ratio of [1, 0, 0.8, 0.1]) {
        root.scrollTop = (root.scrollHeight - root.clientHeight) * ratio;
        await new Promise(requestAnimationFrame);
      }
    });
    await el(page, "Search").fill("game12.example.test");
    await el(page, "Category").selectOption("custom-Remote");
    await el(page, "Category").selectOption("custom-Other");
    await el(page, "Empty").getByText("没有符合条件的目标").waitFor();
    await page.waitForTimeout(250);
    const afterCancel = await monitor.drain();
    assert.ok(afterCancel.miniTrend.failed > 0, "filtering cancels an actual browser request");
    gate.releaseAll();
    await el(page, "Category").selectOption("custom-Remote");
    await wall.locator(".ping-card-chart svg").waitFor();
    await page.waitForTimeout(200);
    const beforeHistory = await monitor.drain();
    const history = wall.locator(".ping-card-open");
    onWall = false;
    await history.tap();
    await el(page, "HistoryTitle").getByText("game12.example.test", { exact: true }).waitFor();
    await monitor.drain();
    onWall = true;
    await el(page, "BackToWall").tap();
    await wall.locator(".ping-card-chart svg").waitFor();
    await page.waitForTimeout(200);
    const queries = await monitor.drain();
    const result = { queries, afterCancel, beforeHistory, miniTrendHttpConcurrency: miniTrendBudget(queries),
      ...data.samples };
    record("mini-trend-navigation", result);
    console.log("PING_DOMAIN_NAVIGATION_BROWSER " + JSON.stringify(result));
  } finally {
    gate.releaseAll();
    await context.close();
  }
}

async function syncStates(browser) {
  const evidence = [];
  const scenarios = [
    ["schema1-empty-loaded", 1, "empty", true],
    ["schema1-empty-missing", 1, "empty", false],
    ["schema2-empty-loaded", 2, "empty", true],
    ["schema2-empty-missing", 2, "empty", false],
    ["all-disabled-loaded", 2, "disabled", true],
    ["all-disabled-missing", 2, "disabled", false],
    ["last-dns-deleted", 2, "delete", true],
    ["dns-awaiting-resolution", 2, "resolving", true],
    ["dns-awaiting-publication", 2, "publishing", false],
    ["dns-awaiting-discovery", 2, "discovering", false],
    ["dns-withdrawal-pending", 2, "withdrawing", false],
    ["mixed-awaiting-publication", 2, "mixed-publishing", false],
    ["mixed-awaiting-resolution", 2, "mixed-resolving", true],
    ["mixed-discovered", 2, "mixed", true]
  ];
  try {
    // Collect the actual DOM of each scenario before asserting it, including
    // the old-head failure. Built-in evidence stays UP in every scenario.
    for (const [name, schema, phase, acknowledged] of scenarios) {
      const context = await browser.newContext({ viewport: { width: 1366, height: 768 } });
      const data = fixture(2, { builtIn: true });
      const { catalog, discovery } = data.snapshot();
      const [domain, fixed] = catalog.targets;
      const [dynamicEndpoint, fixedEndpoint] = discovery.activeTargets;
      const [found] = discovery.dnsTargets;
      const mixed = phase.startsWith("mixed");
      const hasFixed = mixed;
      const hasDns = !["empty", "disabled"].includes(phase);
      const resolving = phase.endsWith("resolving");
      const publishing = phase.endsWith("publishing");
      const withdrawing = phase === "withdrawing";
      const pending = publishing || phase === "discovering" || withdrawing;
      catalog.targets = phase === "disabled" ? [domain, fixed].map((row) => ({ ...row, enabled: false })) :
        !hasDns ? [] : [resolving ? { ...domain, dnsStatus: "DNS_PENDING", resolvedIp: null,
          resolutionGeneration: null, sampleNotBefore: null } : withdrawing ? { ...domain, dnsStatus: "DNS_ERROR" } : domain,
        ...(hasFixed ? [fixed] : [])];
      if (schema === 1) {
        delete catalog.manifestRevision;
        delete catalog.dnsEnabled;
      }
      if (publishing) {
        catalog.publication = "pending";
        catalog.publishedRevision = null;
      }
      discovery.publishedRevision = catalog.publishedRevision;
      discovery.discoveredRevision = acknowledged ? catalog.revision : null;
      discovery.activeTargets = [
        ...(hasDns && !resolving && (!pending || withdrawing) ? [dynamicEndpoint] : []),
        ...(hasFixed ? [fixedEndpoint] : [])
      ];
      discovery.dnsTargets = !hasDns ? [] : resolving || pending
        ? [{ ...found, ...Object.fromEntries(["dnsStatus", "resolvedIp", "resolutionGeneration"].map((key) =>
          [key, catalog.targets[0][key]])), discoveredGeneration: null, sampleNotBefore: null,
          pingStatus: "UNKNOWN", discoveryStatus: "DISCOVERY_PENDING", withdrawalPending: withdrawing }] : [found];

      await context.route("**/*", (route) => data.intercept(route));
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      page.setDefaultTimeout(11000);
      try {
        await page.goto(fixtureHost + "/ping", { waitUntil: "domcontentloaded" });
        const core = el(page, "Wall").locator(".ping-card").filter({ hasText: "Sync fixture core" });
        await core.waitFor();
        assert.equal(await core.getAttribute("data-state"), "UP");
        await el(page, "CustomManage").click();
        const inventory = el(page, "CustomInventory");
        await inventory.getByText(new RegExp(catalog.targets.length + " / 300 总数")).waitFor();
        if (hasFixed) await el(page, "CustomRows").getByText(/Game 1.*UP/).waitFor();
        if (phase === "delete") {
          await el(page, "CustomRows").getByRole("button", { name: /^删除/ }).click();
          await el(page, "CustomDelete").click();
          await inventory.getByText(/0 \/ 300 总数/).waitFor();
          // Wait for the post-delete discovery read beyond the write receipt.
          const empty = data.snapshot().discovery;
          assert.equal(empty.discoveredRevision, null);
          const response = page.waitForResponse((reply) => new URL(reply.url()).pathname === api + "/discovery");
          await el(page, "CustomClose").click();
          await el(page, "Refresh").click();
          await response;
          await el(page, "CustomManage").click();
        }

        const fixedCard = el(page, "Wall").locator(".ping-card").filter({ hasText: "Game 1" });
        const dynamicCard = el(page, "Wall").locator(".ping-card-domain");
        const actual = {
          inventory: await inventory.textContent(),
          notice: await el(page, "CustomNotice").textContent(),
          source: await el(page, "Source").getAttribute("data-state"),
          fixed: await fixedCard.count() ? await fixedCard.getAttribute("data-state") : null,
          dns: await dynamicCard.count() ? await dynamicCard.getAttribute("data-state") : null,
          errors
        };
        const synchronized = acknowledged && !resolving && phase !== "delete";
        const sync = synchronized ? "已发现" : publishing ? "待发布" : "待发现";
        evidence.push({ name, schema, phase, acknowledged, actual });
        await screenshot(page, "sync-" + name + "-manager");
        await el(page, "CustomClose").click();
        await screenshot(page, "sync-" + name + "-wall");
        assert.ok(actual.inventory.endsWith(sync), name + ": " + actual.inventory);
        assert.equal(actual.notice, synchronized ? "" : "自定义目标 · " + sync, name);
        assert.equal(actual.source, synchronized ? "UP" : "DEGRADED", name);
        assert.equal(actual.fixed, hasFixed ? "UP" : null, name);
        assert.equal(actual.dns, hasDns && phase !== "delete" ? synchronized ? "UP" : "UNKNOWN" : null, name);
        assert.deepEqual(errors, []);
        if (phase === "delete") {
          const empty = data.snapshot();
          data.setSnapshot({ ...empty, discovery: { ...empty.discovery, discoveredRevision: empty.catalog.revision } });
          await el(page, "Refresh").click();
          await el(page, "CustomManage").click();
          await inventory.getByText(/已发现/).waitFor();
          await el(page, "CustomClose").click();
          const recovered = { notice: await el(page, "CustomNotice").textContent(),
            source: await el(page, "Source").getAttribute("data-state") };
          assert.deepEqual(recovered, { notice: "", source: "UP" });
          evidence.at(-1).recovered = recovered;
          await screenshot(page, "sync-last-dns-deleted-job-confirmed");
        }
      } finally { await context.close(); }
    }
  } finally { record("sync-states", evidence); }
}

async function isolated(browser, host) {
  const context = await browser.newContext({ viewport: { width: 1366, height: 768 }, acceptDownloads: true });
  const page = await context.newPage();
  page.setDefaultTimeout(16000);
  const errors = [], operations = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(host + "/ping", { waitUntil: "domcontentloaded" });
  const requestLog = [];
  page.on("response", (response) => {
    const req = response.request();
    if (new URL(response.url()).pathname.startsWith(api)) {
      const entry = { method: req.method(), path: new URL(response.url()).pathname, status: response.status() };
      requestLog.push(entry);
      if (entry.status >= 400 && entry.path.endsWith("/resolutions")) {
        response.json().then(({ error }) => { entry.error = error; }, () => {});
      }
    }
  });
  const openAdd = () => el(page, "CustomAdd").click();
  const save = () => el(page, "CustomSave").click();
  const close = () => el(page, "CustomClose").click();
  const wallRow = domainCard(page);
  const requery = () => el(page, "Refresh").click();
  async function phase(name, state, ip) {
    const response = await context.request.post(host + "/fixture/dns/" + name);
    assert.ok(response.ok(), name + " " + response.status());
    const catalog = await response.json();
    await requery();
    await page.waitForFunction(([status, ip]) => {
      const card = document.querySelector(".ping-card-domain");
      return card?.dataset.state === status && (!ip || card.querySelector(".ping-card-ip").textContent.includes(ip));
    }, [state, ip || ""]);
    operations.push({ phase: name, catalog, state: await wallRow.getAttribute("data-state"),
      label: await wallRow.locator(".ping-card-dns").textContent() });
    assert.equal(await wallRow.count(), 1);
  }

  try {
    await openAdd();
    await el(page, "CustomUser").fill("d3-admin");
    await el(page, "CustomPass").fill("only-isolated-d3-fixture");
    const loginComplete = page.waitForResponse((response) => new URL(response.url()).pathname === "/platform-api/auth/login");
    await el(page, "CustomLoginSubmit").click();
    const loginResult = await loginComplete;
    assert.equal(loginResult.status(), 200, loginResult.ok() ? "" : await loginResult.text());
    await el(page, "CustomIP").fill("172.31.248.23");
    await save();
    await el(page, "CustomRows").getByText(/172\.31\.248\.23/).waitFor();
    const fixed = el(page, "CustomRows").locator("li").filter({ hasText: "172.31.248.23" });
    await fixed.getByRole("button", { name: /^编辑/ }).click();
    await el(page, "CustomIP").fill("172.31.248.24");
    await save();
    await el(page, "CustomRows").getByText(/172\.31\.248\.24/).waitFor();
    await el(page, "CustomNew").click();
    await el(page, "CustomName").fill("Game Cloud");
    await el(page, "CustomIP").fill("GAME.EXAMPLE.TEST.");
    await el(page, "CustomPrivate").locator("summary").click();
    await el(page, "CustomCIDRs").fill("172.31.248.0/24");
    await save();
    await el(page, "CustomRows").getByText(/game\.example\.test/).waitFor();
    await screenshot(page, "isolated-manager");
    await close();
    await wallRow.getByText(/等待解析/).first().waitFor();
    await phase("multi", "UP", "172.31.248.21");
    const key = await wallRow.getAttribute("data-key");
    assert.equal(await wallRow.locator("strong").textContent(), domainHost);
    assert.match(await wallRow.locator(".ping-card-group").textContent(), /Game Cloud/);
    assert.match(operations.at(-1).label, /3 个 A 记录，当前监控 1 个/);
    await screenshot(page, "isolated-live");
    await phase("b-pending", "UNKNOWN", "172.31.248.22");
    assert.match(await wallRow.locator(".ping-card-note").textContent(), /等待新地址发现/);
    await phase("discover", "UP", "172.31.248.22");
    await phase("a-pending", "UNKNOWN", "172.31.248.21");
    await phase("b", "UP", "172.31.248.22");
    assert.equal(await wallRow.getAttribute("data-key"), key);
    await phase("error-hold", "UNKNOWN", "172.31.248.22");
    await wallRow.getByText(/等待停止旧探测/).waitFor();
    await wallRow.getByText(/最近成功解析/).waitFor();
    await screenshot(page, "isolated-withdrawal");
    await phase("policy", "UNKNOWN");
    await wallRow.getByText(/解析地址未获授权/).first().waitFor();
    await phase("timeout", "UNKNOWN");
    await wallRow.getByText(/DNS 查询超时/).waitFor();
    await phase("b", "UP", "172.31.248.22");

    await wallRow.locator(".ping-card-open").click();
    const link = page.url();
    assert.match(link, /custom-dns/);
    await el(page, "HistoryTitle").getByText(domainHost, { exact: true }).waitFor();
    await el(page, "DnsHistory").locator("summary").click();
    await el(page, "DnsSegments").getByText(/未确认采样/).first().waitFor();
    const report = await el(page, "DnsSegments").innerText();
    assert.match(report, /172\.31\.248\.21/);
    assert.match(report, /172\.31\.248\.22/);
    await page.waitForFunction(() => !document.getElementById("pingExport").disabled);
    const promise = page.waitForEvent("download");
    await el(page, "Export").click();
    const downloaded = await promise;
    const csv = fs.readFileSync(await downloaded.path(), "utf8");
    assert.match(csv, /timestamp_utc/);
    assert.match(csv, /resolution_generation,sample_verified/);
    assert.match(csv, /172\.31\.248\.21/);
    assert.match(csv, /172\.31\.248\.22/);
    assert.match(csv, /true/);
    fs.writeFileSync(path.join(output, "isolated-history.csv"), csv);
    await screenshot(page, "isolated-history");
    await el(page, "BackToWall").click();
    await el(page, "CustomManage").click();
    const domain = el(page, "CustomRows").locator("li").filter({ hasText: domainHost });
    await domain.getByRole("button", { name: /^删除/ }).click();
    await el(page, "CustomDelete").click();
    await domain.waitFor({ state: "hidden" });
    await page.goto(link, { waitUntil: "domcontentloaded" });
    await el(page, "HistoryTitle").getByText(domainHost, { exact: true }).waitFor();
    await el(page, "DnsHistory").locator("summary").click();
    await el(page, "DnsSegments").getByText(/172\.31\.248\.22/).first().waitFor();
    await page.waitForFunction(() => !document.getElementById("pingExport").disabled);
    const [deletedDownload] = await Promise.all([page.waitForEvent("download"), el(page, "Export").click()]);
    const deletedCsv = fs.readFileSync(await deletedDownload.path(), "utf8");
    assert.match(deletedCsv, /timestamp_utc/);
    assert.match(deletedCsv, /172\.31\.248\.21/);
    assert.match(deletedCsv, /172\.31\.248\.22/);
    fs.writeFileSync(path.join(output, "isolated-history-after-delete.csv"), deletedCsv);
    assert.deepEqual(errors, []);
  } catch (error) {
    const resolutionErrors = requestLog.filter(({ path, status }) => path.endsWith("/resolutions") && status >= 400);
    console.error("DNS history diagnostics", JSON.stringify({
      resolutionErrors, title: await el(page, "HistoryTitle").allTextContents(), errors
    }));
    throw error;
  } finally {
    record("isolated-operations", { operations, requests: requestLog, errors });
    await context.close();
  }
}

(async () => {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined, args: ["--no-sandbox"] });
  try {
    if (process.env.PING_D3_LIVE) await isolated(browser, process.env.PING_D3_LIVE);
    else {
      await verifyQueryLifecycle(browser);
      await syncStates(browser);
      await layout(browser);
      await mixedPerformance(browser, 60, [[50, 13], [100, 25], [300, 100]]);
      await mixedPerformance(browser, 300, [[300, 100]]);
      await controlledMiniTrendBudget(browser);
      await miniTrendNavigation(browser);
      await verifyFixedPoint(browser, dims, output);
      await verifyLossBuckets(browser, dims, output);
    }
    console.log(JSON.stringify({ chromium: browser.version(), mode: process.env.PING_D3_LIVE ? "isolated-services" : "layouts" }));
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
