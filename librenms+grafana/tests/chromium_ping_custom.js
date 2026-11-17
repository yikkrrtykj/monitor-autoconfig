/* Run with PLAYWRIGHT_MODULE and CHROMIUM_PATH, or installed Playwright Chromium. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { performance } = require("node:perf_hooks");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");

const root = path.join(__dirname, "..", "bigscreen");
const baseline = process.env.PING_BASELINE_BIGSCREEN;
const out = process.env.PING_BROWSER_OUTPUT || path.join(require("node:os").tmpdir(), "ping-b2-browser");
const host = "http://ping-fixture.invalid";
const api = "/platform-api/ping/targets";
const token = "a".repeat(64);
const uuid = (n) => "abcd1234-1000-4000-8000-" + n.toString(16).padStart(12, "0");
const address = (n, prefix) => prefix + "." + Math.floor(n / 250) + "." + (n % 250 + 1);
const fixtureTime = () => Math.floor(Date.now() / 1000);
const iso = (seconds) => new Date(seconds * 1000).toISOString();
const contentTypes = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml" };

function fixture(size, customCount, options = {}) {
  const infra = Array.from({ length: size - customCount }, (_, i) => ({
    job: i === 0 ? "infra-fw-ping" : "infra-dist-ping", instance: "设备 " + i,
    target_ip: address(i, "192.0")
  }));
  let revision = customCount;
  let rows = Array.from({ length: customCount }, (_, i) => ({
    id: uuid(i + 1), name: "自定义 " + (i + 1), ip: address(i, "198.51"), group: "Remote",
    enabled: true, createdAt: iso(fixtureTime() - 180), updatedAt: iso(fixtureTime() - 180), revision: i + 1
  }));
  const metrics = rows.map((row) => ({ ...metricFor(row), row }));
  const failure = { status: 0, code: "" };
  const requestKeys = [];
  const stats = { summary: 0, custom: 0, manifest: 0, discovery: 0, raw: 0, rawPeak: 0, rawSize: 0, held: 0 };
  let activeRaw = 0;
  let auth = false;
  let authEnabled = true;
  let missingDiscovery = false;
  let missingList = false;
  let generation = 1000;
  let allUp = options.allUp === true;
  let barrier = null;
  let releaseBarrier = null;
  function metricFor(row) { return { job: "custom-ping", instance: row.id, custom_id: row.id, target_ip: row.ip }; }
  const state = () => ({ ok: true, revision, publication: "published", publishedRevision: revision });

  function payload(q, at) {
    const custom = q.includes('job="custom-ping"');
    const raw = q.startsWith("{__name__");
    const instances = /instance=(~?)("(?:[^"\\]|\\.)*")/.exec(q);
    const selection = instances ? new RegExp("^(?:" + (instances[1] ? JSON.parse(instances[2]) :
      JSON.parse(instances[2]).replace(/[.*+?^${}()|[\]\\]/g, "\\$&")) + ")$") : null;
    const chosen = (raw ? [...infra, ...metrics] : custom ? metrics.filter((m) => m.row.enabled) : infra)
      .map(({ row: _row, ...metric }) => metric)
      .filter((metric) => !raw || selection?.test(metric.instance));
    if (raw) {
      stats.rawSize = Math.max(stats.rawSize, chosen.length);
      return chosen.flatMap((metric) => ["up", "probe_success", "probe_icmp_duration_seconds"].map((field) => ({
        metric: { ...metric, __name__: field, ...(field === "probe_icmp_duration_seconds" ? { phase: "rtt" } : {}) },
        values: Array.from({ length: 44 }, (_, i) => [at - i * 2, field === "probe_icmp_duration_seconds" ? "0.012" : "1"]).reverse()
      })));
    }
    const live = custom
      ? chosen.map((metric) => ({ metric, values: allUp ? [1, 1, 12] : metric.instance === uuid(2) ? [0, 1, 55] :
        metric.instance === uuid(3) ? [1, 0, 0] : [1, 1, metric.instance === uuid(4) ? 90 : 12] }))
      : chosen.map((metric) => ({ metric, values: [1, 1, 12] }));
    return live.flatMap(({ metric, values: [up, ping, rtt] }) => Object.entries({
      ...(!custom ? { target: 1 } : {}), source: up, source_at: at - 1, probe: ping,
      probe_at: at - 1, rtt: rtt / 1000, rtt_at: at - 1, count: 30, ok: ping ? 30 : 0
    }).map(([field, value]) => ({ metric: { ...metric, ping_field: field }, value: [at, String(value)] })));
  }

  async function intercept(route, directory) {
    const req = route.request();
    const url = new URL(req.url());
    const respond = (json, status = 200) => route.fulfill({ status, json });
    if (url.origin !== host) return route.abort();
    if (url.pathname === "/platform-api/auth/status") return respond({ ok: true, enabled: authEnabled, authenticated: auth || !authEnabled });
    if (url.pathname === "/platform-api/auth/login") { auth = true; return respond({ ok: true, authenticated: true }); }
    if (url.pathname === "/platform-api/auth/logout") { auth = false; return respond({ ok: true, authenticated: false }); }
    if (url.pathname === api + "/csrf") return auth ? respond({ ok: true, csrfToken: token }) : respond({ ok: false }, 401);
    if (url.pathname === api + "/discovery") {
      stats.discovery++;
      if (missingDiscovery) return respond({ ok: false, error: "custom_ping_inventory_unavailable" }, 503);
      if (barrier) { stats.held++; await barrier; }
      return respond({ ...state(), discoveredRevision: revision, activeTargets: rows.filter((row) => row.enabled)
        .map(({ id, ip, revision }) => ({ id, ip, revision, scrapeHealth: "up" })) });
    }
    if (url.pathname === api && req.method() === "GET") {
      stats.manifest++;
      if (missingList) return respond({ ok: false }, 503);
      if (barrier) { stats.held++; await barrier; }
      return respond({ ...state(), targets: rows });
    }
    if (url.pathname === api || url.pathname.startsWith(api + "/")) {
      const method = req.method();
      const headers = req.headers();
      requestKeys.push([headers["idempotency-key"], req.postData(), headers["if-match"]]);
      assert.ok(auth);
      assert.equal(headers["x-csrf-token"], token);
      assert.match(headers["idempotency-key"], /^[a-f0-9-]{14}4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/);
      if (failure.status) {
        const { status, code } = failure;
        failure.status = 0;
        if (status === 401) auth = false;
        return status === -1 ? route.abort() : respond({ ok: false, error: code }, status);
      }
      const previous = rows.find((row) => row.id === url.pathname.split("/").pop());
      if (method !== "POST") assert.equal(headers["if-match"], JSON.stringify(String(previous.revision)));
      revision++;
      const target = method === "DELETE" ? null : {
        ...(previous || { id: uuid(generation++), createdAt: iso(fixtureTime() - 2), enabled: true }),
        ...req.postDataJSON(), updatedAt: iso(fixtureTime() - 2), revision
      };
      rows = rows.filter((row) => row !== previous);
      if (target) rows.push(target);
      if (target?.enabled) {
        const reading = metrics.find((item) => item.instance === target.id && item.target_ip === target.ip);
        if (reading) reading.row = target;
        else metrics.push({ ...metricFor(target), row: target });
      }
      // Retain stale samples after removal to exercise authority filtering.
      return respond({ ...state(), target, id: (target || previous).id, savedRevision: revision, replayed: false,
        ...(method === "DELETE" ? { publication: "pending", publishedRevision: null } : {}) },
      method === "DELETE" ? 202 : method === "POST" ? 201 : 200);
    }
    if (url.pathname.includes("/api/v1/query")) {
      const q = url.searchParams.get("query") || "";
      const at = Number(url.searchParams.get("time")) || fixtureTime();
      if (q.startsWith("{__name__")) { stats.raw++; stats.rawPeak = Math.max(stats.rawPeak, ++activeRaw); }
      else if (q.includes('job="custom-ping"')) stats.custom++;
      else if (q.includes("last_over_time(up")) stats.summary++;
      const result = payload(q, at);
      await new Promise((resolve) => setTimeout(resolve, 24));
      if (q.startsWith("{__name__")) activeRaw--;
      return respond({ status: "success", data: { resultType: q.startsWith("{__name__") ? "matrix" : "vector", result } });
    }
    if (url.pathname === "/config.js") return route.fulfill({ body:
      'window.BIGSCREEN_CONFIG={firewallPing:"192.0.0.1",pingIntervals:{infra:"2s"}};', contentType: "text/javascript" });
    if (url.pathname.startsWith("/platform-api")) return respond({ ok: false }, 404);
    const file = path.join(directory, url.pathname === "/ping" ? "index.html" : url.pathname.slice(1));
    if (!file.startsWith(directory + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
      return route.fulfill({ status: 404 });
    }
    return route.fulfill({ path: file, contentType: contentTypes[path.extname(file)] || "application/octet-stream" });
  }

  return { intercept, stats, failure, requestKeys, authenticate(value) { auth = value; },
    disableAuth() { authEnabled = false; }, enableAuth() { authEnabled = true; }, unavailable(list, discovery) { missingList = list; missingDiscovery = discovery; },
    // Hold authority reads to model a stalled Platform API while the wall is
    // resumed; release resolves every request that queued on the barrier.
    hold() { barrier = new Promise((resolve) => { releaseBarrier = resolve; }); },
    release() { const resolve = releaseBarrier; releaseBarrier = null; barrier = null; resolve?.(); } };
}

async function cardCount(page, expected) {
  await page.waitForFunction((number) => document.querySelectorAll("#pingWall .ping-card").length === number, expected);
}

async function start(browser, directory, size, customCount, viewport, touch = false, locked = false, options = {}) {
  const context = await browser.newContext({ viewport, hasTouch: touch, acceptDownloads: true });
  const mock = fixture(size, customCount, options);
  if (locked) mock.disableAuth();
  await context.route("**/*", (route) => mock.intercept(route, directory));
  const page = await context.newPage();
  page.setDefaultTimeout(9000);
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(host + "/ping", { waitUntil: "domcontentloaded" });
  await cardCount(page, size);
  assert.deepEqual(errors, []);
  return { context, page, mock, errors };
}

const customStates = (page) => page.evaluate(() => [...document.querySelectorAll("#pingWall .ping-card")]
  .filter((card) => card.querySelector(".ping-card-group").textContent.startsWith("Custom"))
  .map((card) => card.dataset.state));
const sourceState = (page) => page.evaluate(() => {
  const source = document.getElementById("pingSource");
  return { state: source.dataset.state, text: source.textContent };
});
const waitAllUp = (page) => page.waitForFunction(() => [...document.querySelectorAll("#pingWall .ping-card")]
  .filter((card) => card.querySelector(".ping-card-group").textContent.startsWith("Custom"))
  .every((card) => card.dataset.state === "UP"));

// P1 regression: an expired cached UP must be retired on the synchronous
// resume/re-entry render, before the stalled authority read settles, and the
// surrounding wall must not keep advertising a healthy source.
async function staleScenario(browser, directory, viewport, mode) {
  const { context, page, mock, errors } = await start(browser, directory, 14, 4, viewport, false, false, { allUp: true });
  try {
    await waitAllUp(page);
    const healthy = await sourceState(page);
    assert.equal(healthy.state, "UP");
    assert.equal(healthy.text, "实时监控");
    if (mode === "route") {
      await page.evaluate(() => { history.pushState({}, "", "/"); dispatchEvent(new PopStateEvent("popstate")); });
      await page.waitForTimeout(12000);
      mock.hold();
      await page.evaluate(() => { history.pushState({}, "", "/ping"); dispatchEvent(new PopStateEvent("popstate")); });
    } else {
      await page.evaluate(() => {
        Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
        document.dispatchEvent(new Event("visibilitychange"));
      });
      await page.waitForTimeout(12000);
      mock.hold();
      await page.evaluate(() => {
        Object.defineProperty(document, "hidden", { configurable: true, get: () => false });
        document.dispatchEvent(new Event("visibilitychange"));
      });
    }
    const stale = { states: await customStates(page), source: await sourceState(page) };
    assert.equal(stale.states.length, 4);
    assert.deepEqual([...new Set(stale.states)], ["UNKNOWN"]);
    assert.notEqual(stale.source.state, "UP");
    assert.match(stale.source.text, /部分不可用/);
    await page.screenshot({ path: path.join(out, "stale-" + mode + "-" + viewport.width + ".png") });
    mock.release();
    await waitAllUp(page);
    const recovered = await sourceState(page);
    assert.equal(recovered.state, "UP");
    assert.equal(recovered.text, "实时监控");
    await page.screenshot({ path: path.join(out, "resumed-" + mode + "-" + viewport.width + ".png") });
    assert.deepEqual(errors, []);
    return { mode, width: viewport.width, stale, recovered, held: mock.stats.held };
  } finally { await context.close(); }
}

async function run() {
  fs.mkdirSync(out, { recursive: true });
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH });
  const dimensions = [{ width: 1920, height: 1080 }, { width: 1366, height: 768 }, { width: 390, height: 844 }];
  try {
    for (const viewport of dimensions) {
      const { context, page, mock, errors } = await start(browser, root, 14, 4, viewport, viewport.width < 400,
        viewport.width === 1366);
      const id = (name) => page.locator("#pingCustom" + name);
      await page.waitForFunction(() => [...document.querySelectorAll(".ping-card")]
        .filter((card) => card.querySelector(".ping-card-group").textContent.startsWith("Custom"))
        .map((card) => card.dataset.state).sort().join() === "DEGRADED,DOWN,UNKNOWN,UP");
      assert.equal(await page.locator("#pingWall").evaluate((el) => getComputedStyle(el).gridTemplateColumns.split(" ").length),
        viewport.width === 390 ? 1 : viewport.width === 1366 ? 4 : 5);
      await page.screenshot({ path: path.join(out, "wall-" + viewport.width + ".png") });
      if (viewport.width === 390) await id("Manage").tap();
      else await id("Manage").click();
      if (viewport.width === 1366) {
        await id("Message").getByText("平台鉴权已关闭", { exact: false }).waitFor();
        assert.equal(await id("Login").isVisible(), false);
        assert.equal(await id("Rows").locator("button").count(), 0);
        await page.screenshot({ path: path.join(out, "readonly-1366.png") });
        await page.keyboard.press("Escape");
        mock.enableAuth();
        await id("Manage").click();
      }
      await id("Login").waitFor();
      assert.equal(await id("Rows").locator("button").count(), 0);
      await id("User").fill("admin");
      await id("Pass").fill("fixture-password");
      await id("LoginSubmit").click();
      await id("New").click();
      await id("Name").fill("联调目标");
      await id("IP").fill("not-ip");
      await id("Save").click();
      await id("Message").getByText("请输入合法", { exact: false }).waitFor();
      assert.equal(await id("IP").evaluate((el) => el === document.activeElement), true);
      await id("IP").fill("203.0.113.7");
      if (viewport.width === 1366) {
        mock.failure.status = 401;
        mock.failure.code = "custom_ping_auth_required";
        await id("Save").click();
        await id("Login").waitFor();
        await id("Pass").fill("fixture-password");
        await id("LoginSubmit").click();
        await id("Dismiss").click();
      }
      if (viewport.width === 1920) {
        mock.failure.status = 403;
        mock.failure.code = "custom_ping_csrf_rejected";
        await id("Save").click();
        await id("Message").getByText("安全令牌已失效", { exact: false }).waitFor();
        await id("Dismiss").click();
      }
      mock.failure.status = 403;
      mock.failure.code = "custom_ping_ip_outside_allowed_cidrs";
      await id("Save").click();
      await id("Message").getByText("不在批准", { exact: false }).waitFor();
      assert.equal(await id("Name").inputValue(), "联调目标");
      mock.failure.status = 409;
      mock.failure.code = "custom_ping_duplicate_ip";
      await id("Save").click();
      await id("Message").getByText("已被监控", { exact: false }).waitFor();
      assert.equal(await id("IP").inputValue(), "203.0.113.7");
      mock.failure.status = viewport.width === 390 ? -1 : viewport.width === 1366 ? 503 : 500;
      mock.failure.code = "custom_ping_commit_failed";
      await id("Save").click();
      await id("Retry").click();
      await id("Rows").getByText("联调目标", { exact: false }).waitFor();
      assert.deepEqual(mock.requestKeys.at(-1), mock.requestKeys.at(-2));
      let item = id("Rows").locator("li").filter({ hasText: "联调目标" });
      await item.getByRole("button", { name: "编辑 联调目标" }).click();
      await id("Name").fill("联调改名");
      mock.failure.status = viewport.width === 390 ? 428 : 412;
      mock.failure.code = viewport.width === 390 ? "custom_ping_revision_required" : "custom_ping_revision_conflict";
      await id("Save").click();
      await id("Review").click();
      assert.equal(await id("Name").inputValue(), "联调改名");
      await id("Save").click();
      item = id("Rows").locator("li").filter({ hasText: "联调改名" });
      await item.getByRole("button", { name: "停用 联调改名" }).click();
      await item.getByRole("button", { name: "恢复 联调改名" }).waitFor();
      await cardCount(page, 14);
      await item.getByRole("button", { name: "恢复 联调改名" }).click();
      await cardCount(page, 15);
      await page.screenshot({ path: path.join(out, "manager-" + viewport.width + ".png") });
      await item.getByRole("button", { name: "删除 联调改名" }).click();
      await id("Delete").click();
      await cardCount(page, 14);
      await page.keyboard.press("Escape");
      assert.equal(await id("Manage").evaluate((el) => el === document.activeElement), true);

      await page.locator("#pingCategory").selectOption("custom");
      await cardCount(page, 4);
      await page.locator("#pingSearch").fill("自定义 3");
      await cardCount(page, 1);
      await page.locator("#pingStatus").selectOption("DOWN");
      await cardCount(page, 1);
      await page.locator(".ping-card-open").click();
      const deep = page.url();
      assert.match(deep, /view=history/);
      await page.locator("#pingExport").waitFor();
      await page.waitForFunction(() => !document.getElementById("pingExport").disabled);
      const downloadPromise = page.waitForEvent("download");
      await page.locator("#pingExport").click();
      const download = await downloadPromise;
      assert.match(download.suggestedFilename(), /\.csv$/);
      const csv = fs.readFileSync(await download.path(), "utf8");
      assert.match(csv, /UTC|time_utc/i);
      assert.match(csv, /\d{4}-\d\d-\d\dT.*Z/);
      await page.screenshot({ path: path.join(out, "history-" + viewport.width + ".png") });
      await page.reload();
      await page.locator("#pingSelected").getByText("198.51.0.3", { exact: true }).waitFor();

      await page.goto(host + "/ping");
      await page.locator("#pingCategory").selectOption("custom");
      await cardCount(page, 4);
      for (const button of await page.locator(".ping-card-compare").all()) await button.click();
      await page.locator("#pingComparison").click();
      assert.equal(await page.locator("#pingSelected .ping-history-target").count(), 4);
      mock.unavailable(true, true);
      await page.getByRole("button", { name: "返回监控墙" }).click();
      await page.locator("#pingCustomNotice").getByText("自定义清单暂不可用", { exact: false }).waitFor();
      await cardCount(page, 4);
      assert.deepEqual(errors, []);
      await context.close();
    }

    const staleRows = [];
    for (const viewport of dimensions) {
      for (const mode of ["route", "visibility"]) staleRows.push(await staleScenario(browser, root, viewport, mode));
    }
    fs.writeFileSync(path.join(out, "stale.json"), JSON.stringify(staleRows, null, 2));

    const performanceRows = [];
    for (const size of [50, 100, 300]) {
      for (const [variant, directory] of [["baseline", baseline], ["b2", root]]) {
        if (!directory) continue;
        const startAt = performance.now();
        const customCount = variant === "b2" ? Math.ceil(size / 4) : 0;
        const { context, page, mock } = await start(browser, directory, size, customCount, dimensions[0]);
        const readyMs = Math.round(performance.now() - startAt);
        for (const ratio of [0, 0.45, 0.9]) {
          await page.locator("#pingWorkspace").evaluate((root, point) => { root.scrollTop = root.scrollHeight * point; }, ratio);
          await page.waitForTimeout(450);
        }
        assert.ok(mock.stats.rawPeak <= 2 && mock.stats.rawSize <= 12);
        performanceRows.push({ variant, size, customCount, readyMs, ...mock.stats });
        await context.close();
      }
    }
    fs.writeFileSync(path.join(out, "performance.json"), JSON.stringify(performanceRows, null, 2));
    console.log(JSON.stringify({ chromium: browser.version(), screenshots: out, stale: staleRows, performance: performanceRows }));
  } finally { await browser.close(); }
}

run().catch((error) => { console.error(error); process.exitCode = 1; });
