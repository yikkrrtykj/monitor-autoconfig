const assert = require("node:assert/strict");
const { test } = require("node:test");
const model = require("../bigscreen/ping/model.js");
const { createPingData } = require("../bigscreen/ping/data.js");

const settings = model.policy();
const at = 1760000000;
const core = { job: "infra-core-ping", instance: "Core-A", target_ip: "192.0.2.3" };
const player = { job: "player-ping", instance: "192.0.2.3", team: "2", seat: "3", network: "wired" };
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
