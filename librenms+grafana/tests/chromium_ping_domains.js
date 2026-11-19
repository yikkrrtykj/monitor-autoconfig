const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");

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

function matrix(metric, at, count = 31) {
  return ["up", "probe_success", "probe_icmp_duration_seconds"].map((field) => ({
    metric: { ...metric, __name__: field, ...(field === "probe_icmp_duration_seconds" ? { phase: "rtt" } : {}) },
    values: Array.from({ length: count }, (_, i) => [at - (count - i) * 2, field === "probe_icmp_duration_seconds" ? "0.012" : "1"])
  }));
}

function fixture(size) {
  const instant = () => Math.floor(Date.now() / 1000);
  const first = instant() - 120;
  const enabled = Math.min(size, 100);
  const list = Array.from({ length: size }, (_, i) => {
    const id = "abcd1234-1000-4000-8000-" + String(i + 1).padStart(12, "0");
    const ip = "10.0." + Math.floor(i / 250) + "." + (i % 250 + 1);
    const common = { id, name: "Game " + i, group: "Remote", enabled: i < enabled, revision: i + 1,
      updatedAt: new Date(first * 1000).toISOString() };
    return i % 2 ? { ...common, ip } : { ...common, address: i === 0 ? longHost : "game" + i + ".example.test",
      kind: "dns", endpointRevision: i + 1, privateCidrs: [], resolvedIp: ip, aCount: 3, dnsStatus: "DNS_OK",
      resolutionGeneration: i + 1, sampleNotBefore: first, expiresAt: first + 300, dnsError: "" };
  });
  const active = list.filter((row) => row.enabled);
  const metric = (row) => ({ job: "custom-ping", instance: row.id, custom_id: row.id, target_ip: row.ip || row.resolvedIp });
  const state = { ok: true, revision: size, manifestRevision: size, publication: "published", publishedRevision: size, dnsEnabled: true };
  let opened = 0;
  let queryCount = 0;
  let activeQueries = 0;
  let queryPeak = 0;
  const ops = [];
  const initial = () => ({ catalog: size, enabled, openCalls: opened, queryCount, queryPeak });
  async function intercept(route) {
    const req = route.request();
    const url = new URL(req.url());
    const json = (body, status = 200) => route.fulfill({ status, json: body });
    if (url.origin !== fixtureHost) return route.abort();
    if (url.pathname === "/platform-api/auth/status") return json({ ok: true, enabled: true, authenticated: true });
    if (url.pathname === api + "/csrf") return json({ ok: true, csrfToken: "a".repeat(64) });
    if (url.pathname === api && req.method() === "GET") { opened++; return json({ ...state, targets: list }); }
    if (url.pathname === api + "/discovery") return json({ ...state, discoveredRevision: size,
      activeTargets: active.map((row) => ({
        id: row.id, ip: row.ip || row.resolvedIp, revision: row.revision, resolutionGeneration: row.resolutionGeneration
      })), dnsTargets: active.filter((row) => row.kind === "dns").map((row) => ({
        id: row.id, dnsStatus: row.dnsStatus, resolvedIp: row.resolvedIp, resolutionGeneration: row.resolutionGeneration,
        discoveredGeneration: row.resolutionGeneration, sampleNotBefore: first, pingStatus: "UP",
        discoveryStatus: "DISCOVERED", withdrawalPending: false
      })) });
    if (req.method() === "PATCH" && url.pathname.startsWith(api + "/")) {
      const data = req.postDataJSON();
      assert.ok(req.headers()["idempotency-key"]);
      assert.ok(req.headers()["if-match"]);
      assert.equal(req.headers()["x-csrf-token"], "a".repeat(64));
      ops.push(data);
      return json({ ok: false, error: "custom_ping_revision_conflict" }, 412);
    }
    if (url.pathname.startsWith("/prometheus/api/v1/")) {
      queryCount++;
      activeQueries++;
      queryPeak = Math.max(queryPeak, activeQueries);
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
      activeQueries--;
      return json({ status: "success", data: { resultType: raw ? "matrix" : "vector", result: rows } });
    }
    if (url.pathname === "/config.js") return route.fulfill({ body: "window.BIGSCREEN_CONFIG={};", contentType: "text/javascript" });
    const file = path.resolve(staticDir, url.pathname === "/ping" ? "index.html" : url.pathname.slice(1));
    if (!file.startsWith(staticDir + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) return route.fulfill({ status: 404 });
    const types = { ".js": "text/javascript", ".css": "text/css", ".html": "text/html", ".svg": "image/svg+xml" };
    return route.fulfill({ path: file, contentType: types[path.extname(file)] || "application/octet-stream" });
  }
  return { intercept, initial, ops };
}

async function layout(browser) {
  const evidence = [];
  for (const [width, height, columns] of dims) {
    const context = await browser.newContext({ viewport: { width, height }, hasTouch: width === 390 });
    const data = fixture(width === 1920 ? 300 : width === 1366 ? 100 : 50);
    await context.route("**/*", (route) => data.intercept(route));
    const page = await context.newPage();
    page.setDefaultTimeout(11000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    try {
      const started = Date.now();
      await page.goto(fixtureHost + "/ping", { waitUntil: "domcontentloaded" });
      const wall = el(page, "Wall");
      const count = data.initial().enabled;
      await page.waitForFunction((count) => document.querySelectorAll("#pingWall .ping-card").length === count, count);
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
      await el(page, "CustomManage").click();
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
      assert.deepEqual(errors, []);
      evidence.push({ width, height, ...metrics, elapsedMs: Date.now() - started, ...data.initial() });
    } finally {
      await context.close();
    }
  }
  record("layouts", evidence);
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
      requestLog.push({ method: req.method(), path: new URL(response.url()).pathname, status: response.status() });
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
    await el(page, "CustomLoginSubmit").click();
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
    assert.deepEqual(errors, []);
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
    else await layout(browser);
    console.log(JSON.stringify({ chromium: browser.version(), mode: process.env.PING_D3_LIVE ? "isolated-services" : "layouts" }));
  } finally { await browser.close(); }
})().catch((error) => { console.error(error); process.exitCode = 1; });
