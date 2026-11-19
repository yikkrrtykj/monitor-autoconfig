const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const { createTrendLoader } = require("../bigscreen/ping/trends.js");
const { prometheusSamples } = require("../bigscreen/api.js");

const panel = fs.readFileSync(path.join(__dirname, "../bigscreen/ping/panel.js"), "utf8");

function fixture(size, dns = (i) => i % 2 === 1) {
  const requests = [];
  const painted = new Map();
  const active = () => requests.filter(({ settled }) => !settled);
  let peak = 0;

  function hold(kind) {
    return (targets, start, end, signal, ...options) => new Promise((resolve, reject) => {
      const request = {
        kind, targets, size: targets.length, span: end - start, end, signal, settled: false, options,
        finish(rows = [{ t: end - 1 }]) {
          assert.equal(request.settled, false);
          request.settled = true;
          resolve(new Map(targets.map(({ key }) => [key, rows])));
        },
        fail(error = new Error("fixture transport failure")) {
          assert.equal(request.settled, false);
          request.settled = true;
          reject(error);
        }
      };
      requests.push(request);
      peak = Math.max(peak, active().length);
    });
  }

  // Capture the actual panel-to-loader wiring before the remaining DOM setup.
  const wiringCaptured = Symbol("trend wiring captured");
  let loader;
  const window = {
    BSPingDetail: {},
    BSPingData: {},
    BSOverviewModel: { firewallExpectations: () => ({}) },
    BSPingWall: { createPingWall: () => ({ paint: (key, trend) => painted.set(key, trend) }) },
    BSPingTrends: {
      createTrendLoader(options) {
        loader = createTrendLoader(options);
        throw wiringCaptured;
      }
    }
  };
  vm.runInNewContext(panel, { window });
  const data = { raw: hold("ipv4"), dnsRaw: hold("dns") };
  assert.throws(() => window.BSPingPanel.createPingPanel({ querySelector: () => null }, data),
    (error) => error === wiringCaptured);
  const targets = Array.from({ length: size }, (_, i) => ({
    key: String(i), dns: dns(i), ip: "10.0.0." + (i + 1), resolutionGeneration: 1, sampleNotBefore: 0
  }));
  loader.update(targets, 300);
  return { active, loader, painted, requests, targets, peak: () => peak };
}

const flush = () => new Promise((resolve) => setImmediate(resolve));
const summarize = (requests) => requests.map(({ kind, size, span }) => [kind, size, span]);

async function finishAll(run) {
  for (let i = 0; i < 10 && run.active().length; i++) {
    for (const request of run.active().reverse()) request.finish();
    await flush();
  }
  assert.equal(run.active().length, 0);
  assert.ok(run.peak() <= 2);
  assert.ok(run.requests.every(({ size, span }) => size >= 1 && size <= 12 && span === 300));
  assert.ok(run.requests.every(({ options }) => options.at(-1)?.waitForErrorBody));
}

function report(scenario, run) {
  console.log("PING_TREND_HTTP_CONTRACT " + JSON.stringify({
    scenario, targets: run.targets.length, started: run.requests.length,
    peakObserved: run.peak(), inFlight: run.active().length, limit: 2, verdict: "PASS", kind: "controlled-promises"
  }));
}

test("A: 24 alternating targets share two physical requests", async () => {
  const run = fixture(24);
  assert.ok(run.peak() <= 2, `24 targets exceeded the two-request budget: ${run.peak()}`);
  assert.deepEqual(summarize(run.active()), [["ipv4", 12, 300], ["dns", 12, 300]]);
  run.requests[1].finish();
  await flush();
  assert.equal(run.active().length, 1);
  assert.equal(run.painted.size, 12);
  await finishAll(run);
  assert.equal(run.painted.size, 24);
  assert.equal(run.requests.length, 2);
  report("A", run);
});

test("B: a fixed failure cannot free the slow DNS slot or clear DNS results", async () => {
  const run = fixture(36);
  const fixed = run.requests.find((request) => request.kind === "ipv4");
  const dns = run.requests.find((request) => request.kind === "dns");
  fixed.fail();
  await flush();
  assert.ok(run.peak() <= 2, `early fixed failure exceeded the two-request budget: ${run.peak()}`);
  assert.equal(dns.signal.aborted, false);
  assert.ok(run.active().includes(dns));
  assert.deepEqual(summarize(run.active()), [["dns", 12, 300], ["ipv4", 6, 300]]);
  assert.equal(run.painted.size, 12);
  assert.equal(run.painted.get(fixed.targets[0].key).error, "查询失败");
  run.requests.at(-1).finish();
  await flush();
  assert.deepEqual(summarize(run.active()), [["dns", 12, 300], ["dns", 6, 300]]);
  await finishAll(run);
  assert.equal(run.painted.size, 36);
  assert.equal(run.requests.length, 4);
  assert.ok(run.targets.filter((target) => target.dns)
    .every(({ key }) => run.loader.get(key).rows.length === 1));
  report("B", run);
});

for (const [label, dns] of [["IPv4", () => false], ["DNS", () => true], ["mixed", (i) => i >= 24]]) {
  test(`${label}: only 48 viewport targets, bounded batches, same-window revisit and rollover`, async () => {
    const run = fixture(60, dns);
    const visible = run.targets.slice(0, 48);
    assert.deepEqual(run.requests[0].targets, visible.slice(0, 12));
    if (label === "mixed") assert.deepEqual(run.requests[1].targets, visible.slice(24, 36));
    await finishAll(run);
    assert.equal(run.requests.length, 4);
    assert.deepEqual(new Set(run.requests.flatMap(({ targets }) => targets)), new Set(visible));
    assert.equal(run.painted.size, 48);
    run.loader.update(visible.slice(12), 300);
    run.loader.update(visible, 300);
    assert.equal(run.requests.length, 4);
    run.loader.update(visible, 330);
    await finishAll(run);
    assert.equal(run.requests.length, 8);
    assert.ok(run.requests.slice(4).every(({ end }) => end === 330));
  });
}

test("updates replace queued work and abort keeps both slots until late resolve or reject", async () => {
  const run = fixture(48);
  const [oldFixed, oldDns] = run.requests;
  run.loader.update(run.targets.slice(24), 300);
  run.loader.update(run.targets.slice(36), 330);
  run.loader.stop();
  const latest = run.targets.slice(40, 44);
  run.loader.update(latest, 330);
  assert.ok(oldFixed.signal.aborted && oldDns.signal.aborted);
  assert.equal(run.requests.length, 2);
  oldDns.fail();
  await flush();
  assert.deepEqual(run.active().map(({ kind }) => kind), ["ipv4", "ipv4"]);
  assert.equal(run.painted.size, 0);
  assert.deepEqual(run.requests[2].targets, latest.filter((target) => !target.dns));
  oldFixed.finish();
  await flush();
  assert.deepEqual(run.requests[3].targets, latest.filter((target) => target.dns));
  assert.equal(run.painted.size, 0);
  await finishAll(run);
  assert.deepEqual(new Set(run.painted.keys()), new Set(latest.map(({ key }) => key)));
  assert.equal(run.requests.length, 4);
});

test("DNS A→B→A and changes to IP or sample gate cannot reuse stale mini-trends", async () => {
  const run = fixture(1, () => true);
  const [a] = run.targets;
  const b = { ...a, ip: "10.0.0.200", resolutionGeneration: 2, sampleNotBefore: 100 };
  const aAgain = { ...a, resolutionGeneration: 3, sampleNotBefore: 200 };
  run.requests[0].finish([{ t: 50 }]);
  await flush();
  assert.deepEqual(run.loader.get(a.key).rows, [{ t: 50 }]);
  run.loader.update([b], 300);
  run.loader.update([aAgain], 300);
  assert.equal(run.loader.get(a.key), undefined);
  assert.equal(run.requests[1].signal.aborted, true);
  run.requests[1].finish([{ t: 150 }]);
  await flush();
  assert.equal(run.loader.get(a.key), undefined);
  run.requests[2].finish([{ t: 200 }, { t: 201 }]);
  await flush();
  assert.deepEqual(run.loader.get(a.key).rows, [{ t: 201 }]);
  for (const next of [{ ...aAgain, sampleNotBefore: 220 },
    { ...aAgain, sampleNotBefore: 220, ip: "10.0.0.201" }]) {
    run.loader.update([next], 300);
    assert.equal(run.loader.get(a.key), undefined);
    run.requests.at(-1).finish([{ t: 221 }]);
    await flush();
    run.loader.update([], 300);
    run.loader.update([next], 300);
    assert.deepEqual(run.loader.get(a.key).rows, [{ t: 221 }]);
  }
  assert.equal(run.requests.length, 5);
  await finishAll(run);
});

test("mini-trend HTTP errors settle after the body ends, while other queries keep their error lifecycle", async (t) => {
  const streams = [];
  t.mock.method(globalThis, "fetch", async () => new Response(new ReadableStream({
    start(controller) { streams.push(controller); }
  }), { status: 502 }));
  const signal = new AbortController().signal;
  let settled = false;
  const miniTrend = prometheusSamples("fixture", { time: 300 }, signal, { waitForErrorBody: true })
    .catch((error) => { settled = true; return error; });
  await flush();
  streams[0].enqueue(new Uint8Array([1]));
  await flush();
  assert.equal(settled, false);
  await assert.rejects(prometheusSamples("fixture", { time: 300 }, signal), /监控数据查询失败/);
  streams[1].close();
  streams[0].close();
  assert.match((await miniTrend).message, /监控数据查询失败/);
  assert.equal(settled, true);
});
