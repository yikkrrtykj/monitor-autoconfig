const assert = require("node:assert/strict");
const { test } = require("node:test");
const { readFileSync } = require("node:fs");
const { runInNewContext } = require("node:vm");
const model = require("../bigscreen/ping/model.js");
const { createPingData } = require("../bigscreen/ping/data.js");
const { createTrendLoader } = require("../bigscreen/ping/trends.js");

const settings = model.policy();
const at = 1760000000;
const core = { job: "infra-core-ping", instance: "Core-A", target_ip: "192.0.2.3" };
const player = { job: "player-ping", instance: "192.0.2.3", team: "2", seat: "3", network: "wired" };
const ap = { job: "infra-ap-ping", instance: "AP-A", target_ip: "192.0.2.4" };
const target = (metric, interval = 2) => ({ metric, interval, job: metric.job, key: model.identity(metric) });
const vector = (metric, fields) => Object.entries(fields).map(([ping_field, value]) => ({
  metric: { ...metric, ping_field }, value: [at, String(value)]
}));
const matrix = (metric, name, values) => ({ metric: { ...metric, __name__: name }, values });
const healthy = { target: 1, source: 1, source_at: at - 2, probe: 1, probe_at: at - 2,
  rtt: 0.003, rtt_at: at - 2, count: 30, ok: 30 };

test("loss counts real binary outcomes, requires coverage, and uses tenths of a percent", () => {
  for (const [ok, expected] of [[6, "0.0"], [5, "16.7"], [4, "33.3"]]) {
    assert.equal(model.lossStats(6, ok, 30, 5).loss.toFixed(1), expected);
  }
  assert.deepEqual(model.lossStats(5, 4, 30, 5), { total: 5, success: 4, failed: 1, coverage: 5 / 6, loss: 20 });
  assert.equal(model.lossStats(4, 4, 30, 5).loss, null);
  assert.equal(model.lossStats(0, 0, 30, 5).loss, null);
  assert.equal(model.lossStats(5, 5.5, 30, 5).loss, null);
});

test("current failure, scrape failure, missing and stale evidence are distinct", () => {
  const cases = [
    [{}, "UP", 3, 0],
    [{ probe: 0, ok: 29, rtt: 0 }, "DOWN", null, 100 / 30],
    [{ ok: 28 }, "DEGRADED", 3, 200 / 30],
    [{ rtt: 0.051 }, "DEGRADED", 51, 0],
    [{ source: 0, probe: 0 }, "UNKNOWN", null, null],
    [{ probe: "NaN" }, "UNKNOWN", null, null],
    [{ probe_at: at - 4 }, "UNKNOWN", null, null],
    [{ source_at: at - 30 }, "UNKNOWN", null, null],
    [{ rtt_at: at - 4 }, "UP", null, 0],
    [{ count: 10, ok: 10 }, "UP", 3, null]
  ];
  for (const [changes, status, rtt, loss] of cases) {
    const [result] = model.snapshot(vector(core, { ...healthy, ...changes }), at, settings);
    assert.deepEqual([result.status, result.rtt, result.loss.loss], [status, rtt, loss]);
  }
  const duplicateProbe = [...vector(core, healthy), ...vector(core, { probe: 0 })];
  assert.equal(model.snapshot(duplicateProbe, at, settings)[0].status, "UNKNOWN");
  assert.equal(model.snapshot([], at, settings).length, 0);
  assert.equal(model.snapshot(vector(core, healthy), at, model.policy({ pingQualityRttMs: 2 }))[0].status, "DEGRADED");
  assert.equal(model.policy({ pingIntervals: { isp: "1500ms", infra: "1m2s" } }).intervals.infra, 62);
});

test("same IP, duplicate display label, readdress and rename preserve separate histories", () => {
  const renamed = { ...core, instance: "Core-B" };
  const readdressed = { ...core, target_ip: "192.0.2.4" };
  const secondLabel = { ...core, display_name: "Core-A", source: "other" };
  const input = [core, player, renamed, readdressed, secondLabel];
  const metrics = input.flatMap((item) => vector(item, healthy));
  const output = model.snapshot(metrics, at, settings);
  assert.equal(new Set(output.map(({ key }) => key)).size, 5);
  assert.equal(output.filter(({ ip }) => ip === "192.0.2.3").length, 4);
  assert.equal(output.find(({ job }) => job === "player-ping").name, "队 2 · 座 3 · wired");
  const original = target(core);
  const data = input.flatMap((item, index) => [
    matrix(item, "up", [[at, "1"]]),
    matrix(item, "probe_success", [[at, "1"]]),
    matrix({ ...item, phase: "rtt" }, "probe_icmp_duration_seconds", [[at, String((index + 1) / 1000)]])
  ]);
  assert.deepEqual(model.rawRows(data, [original], at - 5, at).get(original.key), [{ t: at, scrape: 1, success: 1, rtt: 1 }]);
});

test("raw RTT requires a successful scrape and probe at the same sample timestamp", () => {
  const original = target(core);
  const points = (values) => values.map((value, index) => [at + index, String(value)]);
  const result = model.rawRows([
    matrix(core, "up", points([1, 1, 0, 1, 1, 1, 1, 1])),
    matrix(core, "probe_success", points([1, 0, 1, 1, 1, 1, 1])),
    matrix({ ...core, phase: "rtt" }, "probe_icmp_duration_seconds", points([0.001, 0, 0.02, 0, "+Inf", -1, 0.5, 0.2])),
    matrix({ ...core, phase: "rtt" }, "probe_icmp_duration_seconds", [[at + 6, "0.001"]])
  ], [original], at - 1, at + 7).get(original.key);
  assert.deepEqual(result.map(({ success }) => success), [1, 0, null, 1, 1, 1, 1, null]);
  assert.deepEqual(result.map(({ rtt }) => rtt), [1, null, null, null, null, null, null, null]);
  assert.equal(model.rawStats(result, 8, 1).max, 1);
  assert.equal(model.rawStats(result, 8, 1).loss, null);
});

test("P95, population STDDEV, max and coverage use raw samples before display buckets", () => {
  const points = Array.from({ length: 40 }, (_, index) => ({
    t: at + index + 1, success: 1, rtt: index + 1
  }));
  const stats = model.rawStats(points, 40, 1);
  assert.equal(stats.p50, 20.5);
  assert.equal(stats.p95, 38.05);
  assert.equal(stats.stddev, Math.sqrt(133.25));
  assert.equal(stats.max, 40);
  assert.equal(model.rawStats(points.map((p) => ({ ...p, rtt: 2 })), 40, 1).stddev, 0);
  assert.equal(model.rawStats(points.slice(0, 29), 40, 1).p95, null);
  assert.equal(model.rawStats(points, 60, 1).stddev, null);
  assert.deepEqual(model.bucketRows(points, at, at + 40, 10, 1).map(({ total }) => total), [10, 10, 10, 10]);
});

function observedInventory(readings) {
  const api = createPingData({ settings, query: async (expr, { time }) => {
    // Simulate Prometheus 2.53 range boundaries with fixture scrape times.
    // Derive the ranges from the request so a shortened lookback fails here.
    const ranges = [...expr.matchAll(/last_over_time\(up\{[^}]+\}\[(\d+)s\](?: offset (\d+)s)?\), "ping_field", "([^"]+)"/g)];
    return readings.flatMap(({ metric, scrapes, success = 1 }) => {
      const fields = {};
      for (const [, seconds, offset, field] of ranges) {
        const end = time - Number(offset || 0);
        if (scrapes.some((timestamp) => timestamp >= end - Number(seconds) && timestamp <= end)) fields[field] = 1;
      }
      const latest = scrapes.findLast((timestamp) => timestamp <= time);
      if (latest !== undefined && time - latest <= 300) {
        Object.assign(fields, { source: 1, source_at: latest, probe: success, probe_at: latest,
          rtt: success ? 0.003 : 0, rtt_at: latest, count: 30, ok: success ? 30 : 29 });
      }
      return vector(metric, fields);
    });
  } });
  return (time) => api.summary(time, new AbortController().signal);
}

test("partial discovery loss after five minutes keeps all 20 rows and distinguishes an ICMP failure", async () => {
  const beforeLoss = at - 360;
  const readings = Array.from({ length: 20 }, (_, index) => ({
    metric: { ...core, instance: `192.0.2.${index + 1}`, target_ip: `192.0.2.${index + 1}` },
    scrapes: index < 5 ? [beforeLoss] : [beforeLoss, at - 2],
    success: index === 5 ? 0 : 1
  }));
  const { targets, agedOut } = await observedInventory(readings)(at);
  assert.equal(targets.length, 20);
  assert.equal(agedOut, 0);
  const missing = targets.filter(({ status }) => status === "UNKNOWN");
  assert.equal(missing.length, 5);
  assert.ok(missing.every(({ rtt, loss }) => rtt === null && loss.loss === null));
  assert.equal(targets.filter(({ status }) => status === "UP").length, 14);
  assert.equal(targets.filter(({ status }) => status === "DOWN").length, 1);
});

test("complete discovery loss stays unknown and rediscovery restores the same identity", async () => {
  const read = observedInventory([
    { metric: core, scrapes: [at - 360, at + 9] },
    { metric: ap, scrapes: [at - 360, at + 9] }
  ]);
  const missing = await read(at);
  assert.deepEqual(missing.targets.map(({ status }) => status), ["UNKNOWN", "UNKNOWN"]);
  const recovered = await read(at + 10);
  assert.deepEqual(recovered.targets.map(({ key }) => key), missing.targets.map(({ key }) => key));
  assert.deepEqual(recovered.targets.map(({ status }) => status), ["UP", "UP"]);
  assert.equal(recovered.agedOut, 0);
});

test("a removed target ages out with a notice, then disappears from the bounded inventory", async () => {
  const read = observedInventory([
    { metric: core, scrapes: [at] },
    { metric: ap, scrapes: [at, at + 1798, at + 1800, at + 2100] }
  ]);
  const retained = await read(at + 1799);
  assert.deepEqual(retained.targets.map(({ status }) => status), ["UNKNOWN", "UP"]);
  const expired = await read(at + 1801);
  assert.deepEqual(expired.targets.map(({ key }) => key), [model.identity(ap)]);
  assert.equal(expired.agedOut, 1);
  const later = await read(at + 2101);
  assert.deepEqual(later.targets.map(({ key }) => key), [model.identity(ap)]);
  assert.equal(later.agedOut, 0);
  assert.deepEqual(await observedInventory([{ metric: core, scrapes: [at] }])(at + 2101), { targets: [], agedOut: 0 });
});

test("device summaries scope current and aged-out targets to the page", async () => {
  const signal = new AbortController().signal;
  for (const includeAp of [true, false]) {
    const api = createPingData({ settings, includeAp, inventoryMaxAge: 30, query: async (expr) => {
      assert.doesNotMatch(expr, /player-ping/);
      assert.equal(expr.includes("infra-ap-ping"), includeAp);
      const rows = [...vector(core, healthy), ...vector(ap, healthy),
        ...vector(player, healthy), ...vector({ ...player, instance: "Old seat" }, { aged_out: 1 }),
        ...vector({ ...ap, instance: "Old AP" }, { aged_out: 1 })];
      return expr.includes("last_over_time(up") ? rows :
        rows.filter(({ metric }) => !["target", "aged_out"].includes(metric.ping_field));
    } });
    for (const time of [at, at + 1]) {
      const state = await api.summary(time, signal);
      assert.deepEqual(state.targets.map(({ job }) => job), includeAp ? [core.job, ap.job] : [core.job]);
      assert.equal(state.agedOut, includeAp ? 1 : 0);
    }
  }
});

test("long queries use bounded count/sum windows and keep display samples out of exact stats", async () => {
  const original = target(core);
  const calls = [];
  const signal = new AbortController().signal;
  const api = createPingData({ settings, query: async (expr, window, requestSignal) => {
    calls.push({ expr, window });
    assert.equal(requestSignal, signal);
    if (!window.step || !expr.includes("count_over_time")) return [];
    const to = window.start;
    return [
      { metric: { ...core, ping_field: "count" }, values: [[to, "30"]] },
      { metric: { ...core, ping_field: "ok" }, values: [[to, "25"]] }
    ];
  } });
  const span = 6 * 3600;
  const result = await api.overview([original], at - span, at, signal);
  const [first, second] = result.get(original.key).buckets;
  assert.equal(result.get(original.key).buckets.length, 240);
  assert.equal(first.total, 30);
  assert.equal(first.loss, null);
  assert.equal(second.total, 0);
  assert.equal(calls.length, 2);
  assert.equal(calls[0].window.step, 90);
  await assert.rejects(api.overview([original], at - model.maxHistory - 1, at, signal));
  await assert.rejects(api.overview(Array(5).fill(original), at - span, at, signal));
  await assert.rejects(api.raw([original], at - 3601, at, signal));
  assert.equal(calls.length, 2);
});

test("the wall reports a missing configured VIP even when other targets and HA members are healthy", () => {
  const vip = { job: "infra-fw-ping", metric: { target_ip: "192.0.2.10" } };
  const member = { ...vip, job: "infra-fw-unit-ping" };
  const expected = new Set(["192.0.2.10"]);
  assert.deepEqual(model.missingFirewall([target(core), member], expected), ["192.0.2.10"]);
  assert.deepEqual(model.missingFirewall([vip, member], expected), []);
  assert.deepEqual(model.missingFirewall([], new Set()), []);
});

test("raw keeps the target, time and sample budgets before any upstream call", async () => {
  const api = createPingData({ settings, query: async () => assert.fail("raw limit bypassed") });
  await assert.rejects(api.raw(Array(13).fill(target(core)), at - 300, at), /超出范围/);
  await assert.rejects(api.raw(Array(12).fill(target(core, 1)), at - 3600, at), /样本过多/);
  await assert.rejects(api.raw([target(core)], at - 3601, at), /超出范围/);
});

function trendFixture() {
  const calls = [];
  const updates = [];
  const loader = createTrendLoader({
    raw(targets, start, end, signal) {
      return new Promise((resolve, reject) => calls.push({ targets, start, end, signal, resolve, reject }));
    },
    changed: (key, trend) => updates.push({ key, trend })
  });
  const finish = ({ targets, resolve }) => resolve(new Map(targets.map(({ key }) => [key, []])));
  const devices = Array.from({ length: 300 }, (_, index) => target({ ...core, label: String(index) }));
  return { calls, updates, loader, finish, devices };
}

const settle = () => new Promise(setImmediate);

test("300-target inventory feeds only viewport and bounded trend batches; same-window requests deduplicate", async () => {
  const { calls, updates, loader, finish, devices } = trendFixture();
  const visible = devices.slice(25, 57);
  loader.update(visible, at);
  loader.update(visible, at);
  assert.deepEqual(calls.map(({ targets }) => targets.length), [12, 12]);
  finish(calls[0]);
  finish(calls[1]);
  await settle();
  assert.deepEqual(calls.map(({ targets }) => targets.length), [12, 12, 8]);
  finish(calls[2]);
  await settle();
  assert.equal(updates.length, 32);
  assert.ok(updates.every(({ key }) => visible.some((device) => device.key === key)));
  assert.equal(loader.get(visible[0].key).end, at);
  loader.update(visible.slice(6), at);
  loader.update(visible, at);
  assert.equal(calls.length, 3);
  loader.update(visible, at + 30);
  assert.equal(calls.length, 5);
  assert.equal(calls[3].start, at + 30 - 300);
  loader.stop();
});

test("scroll, filter, time change and stop ignore superseded batches even if transport resolves after abort", async () => {
  const { calls, updates, loader, finish, devices } = trendFixture();
  loader.update(devices.slice(0, 30), at);
  loader.update(devices.slice(240, 270), at);
  assert.ok(calls.every(({ signal }) => signal.aborted));
  assert.equal(calls.length, 2);
  finish(calls[0]);
  finish(calls[1]);
  await settle();
  assert.equal(updates.length, 0);
  assert.equal(calls.length, 4);
  loader.update(devices.slice(242, 243), at);
  finish(calls[2]);
  finish(calls[3]);
  await settle();
  assert.deepEqual(updates.map(({ key }) => key), [devices[242].key]);
  loader.update([devices[242]], at - 30);
  loader.stop();
  assert.equal(calls[4].signal.aborted, true);
  finish(calls[4]);
  await settle();
  assert.equal(updates.length, 1);
  assert.equal(loader.get(devices[242].key), undefined);
});

test("partial viewport overlap reuses the in-flight batch and a timeout is distinct from no data", async () => {
  const { calls, updates, loader, finish, devices } = trendFixture();
  loader.update(devices.slice(0, 2), at);
  loader.update(devices.slice(1, 3), at);
  assert.equal(calls[0].signal.aborted, false);
  assert.deepEqual(calls[1].targets, devices.slice(2, 3));
  calls[0].reject(new DOMException("", "TimeoutError"));
  finish(calls[1]);
  await settle();
  assert.deepEqual(updates.map(({ key, trend }) => [key, trend.error, trend.rows]), [
    [devices[1].key, "查询超时", undefined], [devices[2].key, undefined, []]
  ]);
  loader.update(devices.slice(1, 3), at);
  assert.equal(calls.length, 2);
  loader.stop();
});

test("wall order keeps gateways ahead of access switches and does not rank by live status", () => {
  const devices = [
    { group: "Dist", status: "DOWN" }, { group: "FW Unit", status: "UP" },
    { group: "AP", status: "UNKNOWN" }, { group: "FW", status: "UP" },
    { group: "Core", status: "UP" }, { group: "Stage", status: "DEGRADED" }
  ];
  assert.deepEqual(devices.sort(model.wallOrder).map(model.groupLabel),
    ["Core", "FW VIP", "FW Unit", "Stage", "Dist", "AP ICMP"]);
});

test("wall charts share RTT coordinates and break on missing, lost and failed scrapes", () => {
  const window = { BSUtils: require("../bigscreen/utils.js") };
  runInNewContext(readFileSync(require.resolve("../bigscreen/ping/charts.js"), "utf8"), { window });
  const { sparkline } = window.BSPingCharts;
  const points = [
    { t: 1, rtt: 10, scrape: 1, success: 1 }, { t: 2, rtt: 10, scrape: 1, success: 1 },
    { t: 4, rtt: 30, scrape: 1, success: 1 }, { t: 5, rtt: null, scrape: 0, success: null },
    { t: 6, rtt: null, scrape: 1, success: 0 }, { t: 7, rtt: 3000, scrape: 1, success: 1 }
  ];
  const graph = sparkline(points, 0, 8, 1);
  const trace = (svg) => svg.match(/class="ping-trace" d="([^"]+)"/)[1];
  assert.equal(trace(graph).split("M")[1], trace(sparkline(points.slice(0, 2), 0, 8, 1)).split("M")[1]);
  assert.equal(trace(graph).match(/M/g).length, 3);
  for (const reason of ["loss", "scrape", "missing", "clipped"]) assert.match(graph, new RegExp("ping-mark-" + reason));
  assert.match(sparkline([], 0, 8, 1), /最近 5 分钟无样本/);
});
