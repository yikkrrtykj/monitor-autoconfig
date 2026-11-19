const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const { createTrendLoader } = require("../bigscreen/ping/trends.js");

const panel = fs.readFileSync(path.join(__dirname, "../bigscreen/ping/panel.js"), "utf8");

function fixture(size) {
  const requests = [];
  const active = () => requests.filter(({ settled }) => !settled);
  let peak = 0;

  function hold(kind) {
    return (targets, start, end, signal) => new Promise((resolve, reject) => {
      const request = {
        kind, size: targets.length, span: end - start, signal, settled: false,
        finish() {
          request.settled = true;
          resolve(new Map(targets.map(({ key }) => [key, []])));
        },
        fail() {
          request.settled = true;
          reject(new Error("fixture transport failure"));
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
    BSPingWall: { createPingWall: () => ({ paint() {} }) },
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
    key: String(i), dns: i % 2 === 1, ip: "10.0.0." + (i + 1), resolutionGeneration: 1, sampleNotBefore: 0
  }));
  loader.update(targets, 300);
  return { active, requests, peak: () => peak };
}

const flush = () => new Promise((resolve) => setImmediate(resolve));
const types = ["ipv4", "dns", "ipv4", "dns"];
const summarize = (requests) => requests.map(({ kind, size, span }) => [kind, size, span]);

test("production characterization A: two mixed batches exceed the two-HTTP mini-trend contract", async () => {
  const run = fixture(24);
  assert.deepEqual(summarize(run.active()), types.map((kind) => [kind, 6, 300]));
  assert.equal(run.peak(), 4);
  for (const request of run.active()) request.finish();
  await flush();
  assert.equal(run.active().length, 0);
  console.log("PING_TREND_CONTRACT_COUNTEREXAMPLE " + JSON.stringify({
    scenario: "A", node: process.version, targets: 24, started: run.requests.length,
    peakObserved: run.peak(), limit: 2, verdict: "FAIL", kind: "controlled-promises"
  }));
});

test("production characterization B: failure frees a logical batch with its DNS request still in flight", async () => {
  const run = fixture(36);
  assert.deepEqual(summarize(run.active()), types.map((kind) => [kind, 6, 300]));
  const [fixed, dns] = run.requests;
  assert.equal(fixed.signal, dns.signal);
  fixed.fail();
  await flush();
  assert.equal(dns.signal.aborted, false);
  assert.ok(run.active().includes(dns));
  assert.deepEqual(summarize(run.active()), ["dns", ...types].map((kind) => [kind, 6, 300]));
  assert.equal(run.active().length, 5);
  assert.equal(run.requests.length, 6);
  assert.equal(run.peak(), 5);
  for (const request of run.active()) request.finish();
  await flush();
  assert.equal(run.active().length, 0);
  console.log("PING_TREND_CONTRACT_COUNTEREXAMPLE " + JSON.stringify({
    scenario: "B", node: process.version, targets: 36, started: run.requests.length,
    peakObserved: run.peak(), limit: 2, verdict: "FAIL", siblingAborted: dns.signal.aborted,
    kind: "controlled-promises"
  }));
});
