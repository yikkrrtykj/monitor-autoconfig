const assert = require("assert");
const path = require("path");

global.window = { BIGSCREEN_CONFIG: {}, BIGSCREEN_QUERIES: {} };

const api = require(path.resolve(__dirname, "../bigscreen/api.js"));

// Frozen, manually-advanced clock so rangeWindow() is deterministic.
let nowSec = 1000000;
const realDateNow = Date.now;
Date.now = () => nowSec * 1000;

// Fake Prometheus: serves whatever samples exist inside the requested
// range and records every requested window so the tests can assert that
// the cache only re-fetches the increment.
const samples = new Map();
const calls = [];

global.fetch = async (url) => {
  const parsed = new URL(String(url), "http://localhost");
  const start = Number(parsed.searchParams.get("start"));
  const end = Number(parsed.searchParams.get("end"));
  const step = Number(parsed.searchParams.get("step"));
  calls.push({ start, end, step });
  const values = [];
  for (let t = Math.ceil(start / step) * step; t <= end; t += step) {
    if (samples.has(t)) values.push([t, String(samples.get(t))]);
  }
  return {
    ok: true,
    json: async () => ({
      status: "success",
      data: { result: values.length ? [{ metric: { instance: "sw1" }, values }] : [] }
    })
  };
};

(async () => {
  // Seed 15 minutes of 10s samples ending at "now".
  for (let t = nowSec - 900; t <= nowSec; t += 10) samples.set(t, 0.001);

  // 1. First call fetches the entire window.
  let series = await api.prometheusRangeCached("q");
  assert.strictEqual(calls.length, 1);
  assert.strictEqual(calls[0].start, nowSec - 900);
  assert.strictEqual(calls[0].end, nowSec);
  assert.strictEqual(series.length, 1);
  assert.strictEqual(series[0].name, "sw1");
  assert.strictEqual(series[0].values[series[0].values.length - 1].t, 1000000);
  const fullCount = series[0].values.length;

  // 2. One new sample appears: only the increment is requested, the new
  //    point is appended and the stale head is trimmed off.
  nowSec += 10;
  samples.set(1000010, 0.002);
  series = await api.prometheusRangeCached("q");
  assert.strictEqual(calls.length, 2);
  assert.strictEqual(calls[1].start, 1000010, "incremental fetch starts right after the watermark");
  const last = series[0].values[series[0].values.length - 1];
  assert.strictEqual(last.t, 1000010);
  assert.strictEqual(last.v, 0.002);
  assert.ok(series[0].values[0].t >= nowSec - 900, "head trimmed to the sliding window");
  assert.strictEqual(series[0].values.length, fullCount, "window slides: one in, one out");

  // 3. No new sample due yet: served from cache, no HTTP request at all.
  nowSec += 3;
  series = await api.prometheusRangeCached("q");
  assert.strictEqual(calls.length, 2, "no fetch when no new sample is due");
  assert.ok(series[0].values[0].t >= nowSec - 900, "early-return path still trims the head");

  // 4. Cache fully aged out of the window: falls back to a full-window
  //    fetch, and a series with no remaining samples is dropped.
  nowSec += 2000;
  series = await api.prometheusRangeCached("q");
  assert.strictEqual(calls.length, 3);
  assert.strictEqual(calls[2].start, nowSec - 900, "stale cache triggers a full-window fetch");
  assert.deepStrictEqual(series, [], "aged-out series is removed");

  // 5. invalidateRangeCache forces the next call back to a full fetch.
  // (Seed on step-aligned timestamps -- nowSec is no longer a multiple of 10.)
  for (let t = Math.ceil((nowSec - 900) / 10) * 10; t <= nowSec; t += 10) samples.set(t, 0.003);
  api.invalidateRangeCache();
  series = await api.prometheusRangeCached("q");
  assert.strictEqual(calls[calls.length - 1].start, nowSec - 900);
  assert.strictEqual(series.length, 1);
  assert.strictEqual(series[0].values[series[0].values.length - 1].v, 0.003);

  Date.now = realDateNow;
  console.log("bigscreen api cache tests passed");
})().catch((error) => {
  Date.now = realDateNow;
  console.error(error);
  process.exit(1);
});
