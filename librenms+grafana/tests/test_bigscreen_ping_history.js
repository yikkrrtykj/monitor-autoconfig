const assert = require("node:assert/strict");
const { test } = require("node:test");
const { readFileSync } = require("node:fs");
const vm = require("node:vm");
const model = require("../bigscreen/ping/model.js");
const history = require("../bigscreen/ping/history.js");

const now = 1700000600;
const settings = model.policy({});
const core = model.targetFromMetric({ job: "infra-core-ping", instance: "edge + 测试#1", target_ip: "192.0.2.1" }, settings);

test("canonical single and compared routes survive URL encoding and legacy links", () => {
  const compare = model.targetFromMetric({ job: "infra-ap-ping", instance: "AP 1", target_ip: "192.0.2.2" }, settings);
  const range = { start: now - 900, end: now - 2 };
  const href = new URL(history.urlFor([core, compare], range, now - 300), "https://example.test");
  const route = history.parseRoute(href.searchParams, now, settings);
  assert.deepEqual(route.targets.map((target) => target.key), [core.key, compare.key]);
  assert.deepEqual(route.range, range);
  assert.equal(route.at, now - 300);
  const legacy = new URLSearchParams({ target: core.key, at: new Date((now - 300) * 1000).toISOString() });
  assert.equal(history.isHistory(legacy), true);
  assert.deepEqual(history.parseRoute(legacy, now, settings).range, { start: now - 600, end: now - 300 });
  assert.equal(history.parseTarget(model.identity({ job: "player-ping", instance: "seat" }), settings), null);
  assert.equal(history.parseTarget('[["job",2],["instance","x"]]', settings), null);
  assert.equal(history.parseTarget(" ".repeat(2049), settings), null);
  assert.equal(history.parseTarget('[["instance","a"],["instance","b"],["job","infra-core-ping"]]', settings), null);
  const overflow = new URLSearchParams({ target: core.key });
  for (let i = 0; i < 7; i++) overflow.append("compare", model.identity({job:"infra-ap-ping", instance:"AP-" + i}));
  assert.equal(history.parseRoute(overflow, now, settings).targets.length, 4);
});

test("nearest sample retains a real spike and states for 1/2/5-second sources", () => {
  for (const interval of [1, 2, 5]) {
    const points = [
      { t: 100, scrape: 1, success: 1, rtt: 2.1 },
      { t: 100 + interval, scrape: 1, success: 1, rtt: 128.237 },
      { t: 100 + interval * 2, scrape: 1, success: 0, rtt: null },
      { t: 100 + interval * 3, scrape: 0, success: null, rtt: null },
      { t: 100 + interval * 4, scrape: 1, success: null, rtt: null }
    ];
    const data = [{ target: {...core, interval}, points }];
    const spike = history.selection(data, 100 + interval * 1.1, false);
    assert.equal(spike.time, points[1].t);
    assert.equal(spike.rows[0].sample.rtt, 128.237);
    assert.equal(history.sampleStatus(points[1]), "成功（UP）");
    assert.equal(history.sampleStatus(points[2]), "ICMP 丢包（DOWN）");
    assert.equal(history.sampleStatus(points[3]), "抓取失败");
    assert.equal(history.sampleStatus(points[4]), "缺样（UNKNOWN）");
    assert.equal(history.sampleStatus({ scrape: 1, success: 1, rtt: null }), "探测成功 · RTT 缺样");
    assert.equal(history.selection(data, 100 + interval * 8, false).canPin, false);
  }
  assert.equal(history.nearest([], 100, 2), null);
});

test("long-range hover exposes its real bucket and pans keep span across bounds", () => {
  const bucket = { from: 100, to: 800, loss: 4.25, failed: 34, total: 800 };
  const pick = history.selection([{ target: core, points: [{ t: 700, rtt: 25 }], buckets: [bucket] }], 350, true);
  assert.equal(pick.rows[0].bucket, bucket);
  assert.equal(pick.time, 800);
  assert.equal(pick.rtt, null);
  assert.equal(history.validateRange(2, 604803, now), null);
  assert.equal(history.validateRange(now, now + 1, now), null);
  assert.deepEqual(history.shiftRange({ start: 10, end: 40 }, -20, now), { start: 1, end: 31 });
  assert.deepEqual(history.shiftRange({ start: now - 45, end: now - 15 }, 30, now), { start: now - 30, end: now });
});

function fakePlot() {
  const svg = new EventTarget();
  const attrs = new Map();
  const tooltip = { hidden: true, classList: { toggle() {} } };
  const visuals = new Map();
  const svgShape = (selector) => {
    if (!visuals.has(selector)) visuals.set(selector, {
      classList: { add(){}, remove(){}, toggle(){} },
      setAttribute(name, value) { attrs.set(selector + "/" + name, value); }
    });
    return visuals.get(selector);
  };
  svg.dataset = { chartStart: "100", chartEnd: "250", chartMax: "128.237" };
  svg.querySelector = svgShape;
  svg.setPointerCapture = () => {};
  const send = (name, fields) => svg.dispatchEvent(Object.assign(new Event(name, {cancelable:true}), fields));
  return {
    container: { querySelector: (selector) => selector === ".ping-tooltip" ? tooltip : svg },
    attrs, tooltip, send
  };
}

test("main and original RTT SVG pin a loaded sample; drag only commits on pointer-up", () => {
  const window = {
    BSPingHistory: history,
    BSPingCharts: { chartTime: (_, x) => x, plot: {left:53,right:882,top:16,bottom:153} },
    BSUtils: { escapeHtml: (s) => s }
  };
  let frame;
  vm.runInNewContext(readFileSync(require.resolve("../bigscreen/ping/timeline.js"), "utf8"), {
    window, AbortController, requestAnimationFrame: (fn) => { frame = fn; return 1; }, cancelAnimationFrame: () => {}
  });
  const series = [{ target: core, points: [{ t: 140, scrape: 1, success: 1, rtt: 128.237 }] }];
  const options = { button: 0, isPrimary: true, pointerId: 1, pointerType: "mouse" };
  const pins = [];
  const zooms = [];
  const bindings = ["OverviewChart", "Distribution"].map((id) => {
    const graph = fakePlot();
    const binding = window.BSPingTimeline.bind(graph.container, series, {
      aggregated: false, onPin: (pick) => pins.push([id, pick.time]), onZoom: (...args) => zooms.push(args), onClear: () => {}
    });
    graph.send("pointermove", { ...options, clientX: 140.3 });
    frame();
    assert.match(graph.tooltip.innerHTML, /128\.24 ms/);
    assert.equal(pins.length, id === "OverviewChart" ? 0 : 1);
    graph.send("pointerdown", { ...options, clientX: 140.3 });
    graph.send("pointerup", { ...options, clientX: 140.3 });
    graph.send("pointerdown", { ...options, clientX: 130 });
    graph.send("pointermove", { ...options, clientX: 180 });
    frame();
    assert.equal(zooms.length, id === "OverviewChart" ? 0 : 1);
    graph.send("pointerup", { ...options, clientX: 180 });
    binding.marker(140);
    assert.match(graph.attrs.get(".ping-pin/d"), /^M/);
    return binding;
  });
  assert.deepEqual(pins, [["OverviewChart", 140], ["Distribution", 140]]);
  assert.deepEqual(zooms, [[130, 180], [130, 180]]);
  bindings.forEach((binding) => binding.dispose());
});

test("RTT display rounds msec by magnitude and leaves the sample untouched", () => {
  const window = { BSPingHistory: history, BSUtils: { escapeHtml: (s) => s } };
  vm.runInNewContext(readFileSync(require.resolve("../bigscreen/ping/timeline.js"), "utf8"), { window });
  for (const [value, expected] of [[0.351237, "0.351 ms"], [8.625678, "8.63 ms"], [128.237, "128.24 ms"],
    [0, "0 ms"], [1, "1 ms"], [null, "N/A"], [NaN, "N/A"]]) {
    const sample = Object.freeze({ t: 140, scrape: 1, success: 1, rtt: value });
    const html = window.BSPingTimeline.describe({ rows: [{ target: core, sample }] });
    assert.ok(html.includes("RTT " + expected + "</span>"));
  }
});

test("history discards the slider markup, controller and styles", () => {
  const removed = /pingPan|ping-navigation|byId\("Pan|panTrack|panGesture|panCommit|navStart|navEnd|keepDomain/;
  for (const path of ["../bigscreen/index.html", "../bigscreen/ping/detail.js", "../bigscreen/ping/ping.css"]) {
    assert.doesNotMatch(readFileSync(require.resolve(path), "utf8"), removed, path);
  }
});

test("time buttons display and apply the actual shift, including the remaining time at both bounds", () => {
  const window = {
    BSPingModel: model, BSPingHistory: history, BSPingCharts: { colors: ["#fff"] },
    BSPingTimeline: { time: String }, BSPingData: {}, BSUtils: { escapeHtml: (s) => s }
  };
  const elements = new Map();
  const byId = (name) => {
    if (!elements.has(name)) {
      const element = new EventTarget();
      Object.assign(element, { querySelectorAll: () => [], focus() {} });
      elements.set(name, element);
    }
    return elements.get(name);
  };
  const click = (name) => byId(name).dispatchEvent(new Event("click"));
  const ranges = [];
  const queries = [];
  class Clock extends Date { static now() { return now * 1000; } }
  vm.runInNewContext(readFileSync(require.resolve("../bigscreen/ping/detail.js"), "utf8"), { window, Date: Clock });
  const panel = window.BSPingDetail.createHistoryPanel({
    byId, data: { raw: (_, start, end) => queries.push([start, end]) }, run: (_, load) => load(), cancel() {},
    replaceUrl: (href) => ranges.push(new URL(href, "https://example.test")), back() {}
  });
  const start = (from, to) => panel.start({ targets: [core], range: { start: from, end: to }, at: null }, []);
  start(now - 4200, now - 600);
  assert.match(byId("HistoryCaption").textContent, /当前查询（1 小时）/);
  assert.ok(byId("HistoryCaption").textContent.includes(`${now - 4200} 至 ${now - 600}`));
  assert.equal(byId("GoEarlier").textContent, "← 前 1 小时");
  assert.equal(byId("GoLater").textContent, "后 10 分钟 →");
  const commits = [
    ["GoEarlier", now - 7800, now - 4200],
    ["PrevRange", now - 4200, now - 600],
    ["GoLater", now - 3600, now]
  ];
  for (const [name, from, to] of commits) {
    const count = queries.length;
    click(name);
    assert.equal(queries.length, count + 1);
    assert.deepEqual(queries.at(-1), [from, to]);
    assert.deepEqual(["from", "to"].map((key) => Number(ranges.at(-1).searchParams.get(key))), [from, to]);
  }
  assert.equal(byId("GoLater").disabled, true);
  click("GoLater");
  assert.equal(queries.length, 4);
  click("ResetRange");
  assert.deepEqual(queries.at(-1), [now - 4200, now - 600]);
  assert.equal(byId("PrevRange").disabled, true);

  start(61, 361);
  assert.equal(byId("GoEarlier").textContent, "← 前 1 分钟");
  click("GoEarlier");
  assert.deepEqual(queries.at(-1), [1, 301]);
  assert.equal(byId("GoEarlier").disabled, true);
});
