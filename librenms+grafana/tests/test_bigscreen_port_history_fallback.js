const assert = require('assert');
const { createPortPanel } = require('../bigscreen/charts/port-panel.js');
const escapeHtml = (v) => String(v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
class Node {
  constructor() { this.hidden = true; this.innerHTML = ''; this.style = {}; this.dataset = {}; this.attributes = {}; }
  closest(selector) { return selector === '.port-face' ? this : null; }
  setAttribute(k, v) { this.attributes[k] = v; }
  getBoundingClientRect() { return { left: 10, right: 30, top: 10 }; }
}
class Detail extends Node {
  constructor() { super(); this.status = { textContent: '' }; this.chart = { hidden: true }; this.title = { textContent: '' }; }
  querySelector(selector) { return selector === '.port-history-status' ? this.status : selector === '.port-history-chart' ? this.chart : selector === '.port-history h4' ? this.title : null; }
}
class Root extends Node {
  constructor() { super(); this.detail = new Detail(); this.card = new Node(); this.faces = [new Node(), new Node()]; this.faces.forEach((face, i) => face.dataset.portIndex = String(i)); }
  querySelector(selector) { return selector === '.port-pinned-detail' ? this.detail : selector === '.port-hover-card' ? this.card : null; }
  querySelectorAll() { return this.faces; }
}
const root = new Root();
const pending = [];
const charts = [];
const fallbacks = [];
const timers = [];
const formatBits = (value) => `${value} b/s`;
const panel = createPortPanel({ document: { getElementById: () => root }, window: { innerWidth: 1200, innerHeight: 900 }, escapeHtml,
  fetchPortHistory: (device, port) => new Promise((resolve, reject) => pending.push({ device, port, resolve, reject })),
  fetchPortHistoryFallback: (ip, ifIndex) => new Promise((resolve, reject) => fallbacks.push({ ip, ifIndex, resolve, reject })),
  renderLineChart: (id, series, options) => charts.push({ id, series, options }), formatBits,
  setTimeout: (fn) => { timers.push(fn); return timers.length; }, clearTimeout: () => {} });
const payload = { kind: 'hillstone', ip: '192.0.2.7', ports: [
  { ifIndex: 1, ifName: 'A', ifAlias: 'current-A', rxBps: 80, metricSource: 'LibreNMS poller' },
  { ifIndex: 2, ifName: 'B', ifAlias: 'current-B', rxBps: 160, metricSource: 'Prometheus' }
] };
const history = { source: 'Prometheus', rx: [{ t: 1, v: 0 }, { t: 31, v: 80 }], tx: [], coverage: { rx: 'available', tx: 'empty' } };
const empty = { rx: [], tx: [], coverage: { rx: 'empty', tx: 'empty' } };
const flush = async () => { await Promise.resolve(); await Promise.resolve(); };
const click = (i) => root.onclick({ target: root.faces[i] });
const rrd = { ...history, source: 'LibreNMS RRD', step: 300 };
const closeExplicit = () => root.onclick({ target: { closest: (s) => s === '.port-pinned-close' ? {} : null } });
const pin = (device, index = 0) => { panel.open(device); click(index); return pending[pending.length - 1]; };
(async () => {
  // 3850 high-resolution coverage always wins, even with only RX.
  const cisco = { ...payload, kind: 'cisco', model: 'WS-C3850-12XS', ports: [{ ...payload.ports[0], ifName: 'Gi1/0/1' }, { ...payload.ports[1], ifName: 'Te1/1/1' }] };
  pin(cisco).resolve(history);
  await flush();
  assert.strictEqual(fallbacks.length, 0);
  assert.ok(root.detail.title.textContent.includes('Prometheus'));
  assert.strictEqual(root.dataset.panelMode, 'physical');
  for (const device of [{ ...cisco, model: 'WS-C2960X' }, { ...cisco, model: 'C9300-48P' }, payload, { ...payload, kind: 'generic-switch' }]) {
    const read = pin(device);
    const count = fallbacks.length;
    read.resolve(empty);
    await flush();
    assert.strictEqual(fallbacks.length, count + 1);
    const fallback = fallbacks.at(-1);
    assert.strictEqual(fallback.ip, device.ip);
    assert.strictEqual(fallback.ifIndex, device.ports[0].ifIndex);
    if (device.kind === 'cisco') {
      assert.strictEqual(root.dataset.panelMode, 'physical');
      assert.ok(root.innerHTML.includes('Gi1/0/1') && root.innerHTML.includes('Te1/1/1'));
    }
    fallback.resolve(rrd);
    await flush();
    assert.ok(root.detail.title.textContent.includes('LibreNMS RRD'));
    assert.strictEqual(charts.at(-1).series[0].values, rrd.rx);
    assert.ok(root.detail.innerHTML.includes('current-A'));
  }
  const failure = pin(payload);
  failure.reject(new Error('Prometheus transport failed'));
  await flush();
  fallbacks.at(-1).resolve(rrd);
  await flush();
  assert.ok(root.detail.title.textContent.includes('LibreNMS RRD'));
  pin(payload).resolve({ ...empty, coverage: { rx: 'ambiguous', tx: 'ambiguous' } });
  await flush();
  fallbacks.at(-1).resolve({ ...empty, source: 'LibreNMS RRD' });
  await flush();
  assert.ok(root.detail.status.textContent.includes('Prometheus 与 LibreNMS RRD 均暂无'));
  pin(payload).resolve(empty);
  await flush();
  fallbacks.at(-1).reject(new Error('RRD unavailable'));
  await flush();
  assert.ok(root.detail.status.textContent.includes('暂不可用'));
  assert.ok(root.detail.innerHTML.includes('current-A') && !root.detail.hidden);
  assert.strictEqual(root.faces[0].attributes['aria-pressed'], 'true');
  // Old Prometheus completion must not even start fallback after close/selection switch.
  const closers = [closeExplicit, () => click(0), () => root.onkeydown({ key: 'Escape', preventDefault() {}, stopPropagation() {} }),
    () => panel.close(), () => panel.loading('192.0.2.99', 'cisco')];
  for (const close of closers) {
    const read = pin(payload);
    const count = fallbacks.length;
    close(); read.resolve(empty); await flush();
    assert.strictEqual(fallbacks.length, count);
    // Old fallback completion also cannot render after each close path.
    pin(payload).resolve(empty); await flush();
    const fallback = fallbacks.at(-1);
    const chartCount = charts.length;
    close(); fallback.resolve(rrd); await flush();
    assert.strictEqual(charts.length, chartCount);
    assert.ok(root.detail.hidden);
    assert.ok(root.faces.every((f) => f.attributes['aria-pressed'] === 'false'));
  }
  pin(payload).resolve(empty); await flush();
  const lateA = fallbacks.at(-1);
  click(1);
  pending.at(-1).resolve(history); await flush();
  const chartCount = charts.length;
  lateA.resolve(rrd); await flush();
  assert.strictEqual(charts.length, chartCount);
  assert.ok(root.detail.innerHTML.includes('current-B'));
  assert.ok(root.detail.title.textContent.includes('Prometheus'));
  panel.close();

  // Production helpers: opening once, hover/focus none; pin 2 ranges then exactly 1 fallback.
  global.window = { BIGSCREEN_CONFIG: {}, BIGSCREEN_QUERIES: {} };
  const api = require('../bigscreen/api.js');
  const requests = [];
  global.fetch = async (url) => {
    requests.push(url);
    return { ok: true, json: async () => url.endsWith('/history') ? { ...rrd, ok: true, ip: payload.ip, ifIndex: 1 }
      : url.endsWith('/ports') ? payload : { status: 'success', data: { result: [] } } };
  };
  const integrated = createPortPanel({ document: { getElementById: () => root }, window: { innerWidth: 1200, innerHeight: 900 }, escapeHtml,
    fetchPortHistory: api.fetchPortHistory, fetchPortHistoryFallback: api.fetchPortHistoryFallback, renderLineChart() {}, formatBits });
  integrated.open(await api.fetchNodePorts(payload.ip));
  root.onfocusin({ target: root.faces[0] });
  assert.strictEqual(requests.length, 1);
  click(0);
  for (let i = 0; i < 20; i++) await flush();
  assert.strictEqual(requests.length, 4);
  assert.strictEqual(requests.filter((u) => u.includes('/query_range?')).length, 2);
  assert.strictEqual(requests.at(-1), `/platform-api/network/nodes/${payload.ip}/ports/1/history`);
  assert.ok(root.detail.title.textContent.includes('LibreNMS RRD'));
  integrated.close();
  console.log('bigscreen port history RRD fallback tests passed');
})().catch((error) => { console.error(error); process.exitCode = 1; });
