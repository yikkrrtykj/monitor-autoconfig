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
    assert.match(graph.tooltip.innerHTML, /128\.237 ms/);
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
