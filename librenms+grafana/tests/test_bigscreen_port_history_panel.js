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
  constructor() { super(); this.status = { textContent: '' }; this.chart = { hidden: true }; }
  querySelector(selector) { return selector === '.port-history-status' ? this.status : selector === '.port-history-chart' ? this.chart : null; }
}
class Root extends Node {
  constructor() { super(); this.detail = new Detail(); this.card = new Node(); this.faces = [new Node(), new Node()]; this.faces.forEach((face, i) => face.dataset.portIndex = String(i)); }
  querySelector(selector) { return selector === '.port-pinned-detail' ? this.detail : selector === '.port-hover-card' ? this.card : null; }
  querySelectorAll() { return this.faces; }
}
const root = new Root();
const pending = [];
const charts = [];
const timers = [];
const formatBits = (value) => `${value} b/s`;
const panel = createPortPanel({ document: { getElementById: () => root }, window: { innerWidth: 1200, innerHeight: 900 }, escapeHtml,
  fetchPortHistory: (device, port) => new Promise((resolve, reject) => pending.push({ device, port, resolve, reject })),
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
(async () => {
  panel.loading(payload.ip, payload.kind);
  panel.open(payload);
  assert.strictEqual(pending.length, 0);
  root.onfocusin({ target: root.faces[0] });
  root.onmouseover({ target: root.faces[0] });
  timers.pop()();
  assert.ok(root.card.innerHTML.includes('current-A'));
  assert.ok(!root.card.innerHTML.includes('port-history'));
  assert.strictEqual(pending.length, 0, 'open/hover/focus never reads history');
  click(0);
  assert.strictEqual(pending.length, 1);
  assert.ok(root.detail.innerHTML.includes('正在读取历史'));
  click(1);
  assert.strictEqual(pending.length, 2);
  assert.ok(root.detail.innerHTML.includes('current-B'));
  pending[0].resolve(history);
  await flush();
  assert.strictEqual(charts.length, 0, 'late A cannot replace B');
  pending[1].resolve(history);
  await flush();
  assert.strictEqual(charts.length, 1);
  assert.deepStrictEqual(charts[0].series.map((item) => item.name), ['RX', 'TX']);
  assert.strictEqual(charts[0].series[0].values[0].v, 0);
  assert.deepStrictEqual(charts[0].series[1].values, []);
  assert.strictEqual(charts[0].options.axisFormatter, formatBits);
  assert.strictEqual(charts[0].options.valueFormatter, formatBits);
  assert.ok(root.detail.status.textContent.includes('TX 历史暂无覆盖'));
  click(0);
  pending[2].resolve(empty);
  await flush();
  assert.ok(root.detail.status.textContent.includes('当前值来自 LibreNMS poller；Prometheus 暂无'));
  assert.ok(root.detail.innerHTML.includes('current-A') && !root.detail.hidden);
  click(1);
  pending[3].reject(new Error('private upstream diagnostic'));
  await flush();
  assert.ok(root.detail.status.textContent.includes('历史暂不可用'));
  assert.ok(root.detail.innerHTML.includes('current-B') && !root.detail.hidden);
  assert.strictEqual(root.faces[1].attributes['aria-pressed'], 'true');
  click(0);
  pending[4].resolve({ ...empty, coverage: { rx: 'ambiguous', tx: 'empty' } });
  await flush();
  assert.ok(root.detail.status.textContent.includes('重复序列'));
  const closers = [
    () => root.onclick({ target: { closest: (selector) => selector === '.port-pinned-close' ? {} : null } }),
    () => click(0),
    () => root.onkeydown({ key: 'Escape', preventDefault() {}, stopPropagation() {} }),
    () => panel.close(),
    () => panel.loading('192.0.2.99', 'cisco')
  ];
  for (const close of closers) {
    panel.open(payload);
    click(0);
    const read = pending[pending.length - 1];
    const count = charts.length;
    close();
    read.resolve(history);
    await flush();
    assert.strictEqual(charts.length, count, 'close invalidates history');
    assert.strictEqual(root.detail.hidden, true);
    assert.ok(root.faces.every((face) => face.attributes['aria-pressed'] === 'false'));
  }
  // End-to-end request counting with the actual helper: one ports GET and two ranges only on pin.
  global.window = { BIGSCREEN_CONFIG: {}, BIGSCREEN_QUERIES: {} };
  const api = require('../bigscreen/api.js');
  const requests = [];
  global.fetch = async (url) => { requests.push(url); return { ok: true, json: async () => url.includes('/ports') ? payload : ({ status: 'success', data: { result: [] } }) }; };
  const integrated = createPortPanel({ document: { getElementById: () => root }, window: { innerWidth: 1200, innerHeight: 900 }, escapeHtml,
    fetchPortHistory: api.fetchPortHistory, renderLineChart() {}, formatBits });
  integrated.open(await api.fetchNodePorts(payload.ip));
  assert.strictEqual(requests.length, 1);
  root.onfocusin({ target: root.faces[0] });
  assert.strictEqual(requests.length, 1);
  click(0);
  assert.strictEqual(requests.length, 3);
  await flush(); await flush();
  integrated.close();
  console.log('bigscreen port history lifecycle tests passed');
})().catch((error) => { console.error(error); process.exitCode = 1; });
