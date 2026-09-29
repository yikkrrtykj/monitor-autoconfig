const assert = require('assert');
const fs = require('fs');
const path = require('path');
const { createPortPanel, groupPorts, portState } = require('../bigscreen/charts/port-panel.js');

const escapeHtml = (value) => String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;')
  .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
const port = (name, member, number, more = {}) => ({ ifName: name, stackMember: member,
  portNumber: number, adminState: 'up', operState: 'up', speedBps: 1e9, ...more });

assert.deepStrictEqual(['down', 'up', 'up', 'unknown'].map((adminState, index) =>
  portState({ adminState, operState: ['up', 'down', 'up', 'down'][index] }).key),
['disabled', 'down', 'up', 'unknown']);
assert.strictEqual(portState(port('Gi1/0/1', 1, 1, { inputErrorsTotal: 12 })).key, 'up',
  'cumulative errors never change the base link state');

const physical = Array.from({ length: 48 }, (_, index) => port(`Gi1/0/${index + 1}`, 1, index + 1));
const uplinks = Array.from({ length: 4 }, (_, index) => port(`Te1/0/${index + 1}`, 1, index + 1));
const secondMember = [port('Gi2/0/1', 2, 1)];
const logical = [port('Port-channel1', null, null), port('Vlan42', null, null),
  port('Loopback0', null, null), port('unknown', 1, 3)];
const grouped = groupPorts([...physical, ...uplinks, ...secondMember, ...logical]);
assert.strictEqual(grouped.banks.length, 3, 'member and interface family create separate banks');
assert.deepStrictEqual(grouped.banks[0].top.map((item) => item.port.portNumber),
  Array.from({ length: 24 }, (_, index) => 2 * index + 1));
assert.deepStrictEqual(grouped.banks[0].bottom.map((item) => item.port.portNumber),
  Array.from({ length: 24 }, (_, index) => 2 * index + 2));
assert.strictEqual(grouped.banks[1].twoRow, false, 'four uplinks stay a compact bank');
assert.deepStrictEqual(grouped.banks[1].items.map((item) => item.port.portNumber), [1, 2, 3, 4]);
assert.strictEqual(grouped.banks[2].member, 2);
assert.strictEqual(grouped.other.length, 4, 'unparsed and logical interfaces remain accessible');

class FakeNode {
  constructor() { this.hidden = false; this.innerHTML = ''; this.style = {}; this.dataset = {};
    this.attributes = {}; this.offsetHeight = 250; }
  setAttribute(name, value) { this.attributes[name] = value; }
  getBoundingClientRect() { return { left: 100, right: 130, top: 100 }; }
  closest(selector) { return selector === '.port-face' ? this : null; }
}
class FakeRoot extends FakeNode {
  constructor() { super(); this.card = new FakeNode(); this.pinned = new FakeNode(); }
  querySelector(selector) {
    if (selector === '.port-hover-card') return this.innerHTML.includes('port-hover-card') ? this.card : null;
    if (selector === '.port-pinned-detail') return this.pinned;
    return null;
  }
  querySelectorAll(selector) { return selector === '.port-face' ? this.faces || [] : []; }
}
const root = new FakeRoot();
const tasks = [];
let networkReads = 0;
const originalFetch = global.fetch;
global.fetch = () => { networkReads += 1; throw new Error('Port Panel must use loaded data'); };
const panel = createPortPanel({ document: { getElementById: () => root },
  window: { innerWidth: 1200, innerHeight: 800 }, escapeHtml,
  setTimeout: (handler, delay) => { assert.strictEqual(delay, 120); tasks.push(handler); return tasks.length; },
  clearTimeout: () => {} });
const payload = { ip: '192.0.2.7', name: '<Core>', model: 'C9300', degraded: true,
  warnings: ['当前 IF-MIB 无有效覆盖', '已省略过期邻接'], ports: [
    port('Gi1/0/1', 1, 1, { rxBps: null, txBps: null, rxUtilization: null,
      vlanEvidence: { authority: 'observed', memberships: [{ vlanId: 42, untagged: true }] },
      neighbors: null, inputErrorsTotal: 3 }),
    port('Gi1/0/2', 1, 2, { adminState: 'up', operState: 'down', neighbors: [] }),
    port('Port-channel1', null, null)
  ] };
panel.loading(payload.ip);
assert.strictEqual(panel.isOpen(), true);
panel.open(payload);
assert.ok(root.innerHTML.includes('&lt;Core&gt;'));
assert.ok(root.innerHTML.includes('当前 IF-MIB 无有效覆盖'));
assert.ok(root.innerHTML.includes('已省略过期邻接'));
assert.ok(root.innerHTML.includes('其他接口 (1)'));
assert.ok(root.innerHTML.includes('Port-channel1'));
assert.ok(root.innerHTML.includes('port-state-up'));
assert.ok(root.innerHTML.includes('port-state-down'));
assert.ok(root.innerHTML.includes('aria-label="Gi1/0/1 在线"'));
assert.ok(root.innerHTML.includes('port-face-counter'));

const first = new FakeNode(); first.dataset.portIndex = '0';
const second = new FakeNode(); second.dataset.portIndex = '1';
root.faces = [first, second];
root.onmouseover({ target: first });
assert.strictEqual(root.card.innerHTML, '', 'pointer hover waits before showing the card');
tasks.shift()();
assert.ok(root.card.innerHTML.includes('RX</dt><dd>—'));
assert.ok(root.card.innerHTML.includes('VLAN 观测数据 / 非配置权威'));
assert.ok(root.card.innerHTML.includes('邻接资料暂不可用'));
assert.ok(root.card.innerHTML.includes('输入错误（累计）'));
root.onfocusin({ target: second });
assert.ok(root.card.innerHTML.includes('当前快照未发现邻接'), 'empty neighbor list differs from unavailable');
root.onclick({ target: first });
assert.strictEqual(root.pinned.hidden, false);
assert.ok(root.pinned.innerHTML.includes('VLAN 观测数据 / 非配置权威'));
assert.strictEqual(first.attributes['aria-pressed'], 'true');
assert.strictEqual(networkReads, 0, 'hover, focus and pin use loaded data only');

const dense = { ...payload, ports: Array.from({ length: 338 }, (_, index) =>
  port(`Port-channel${index + 1}`, null, null, { speedBps: null })) };
panel.open(dense);
assert.strictEqual((root.innerHTML.match(/data-port-index=/g) || []).length, 338);
assert.ok(root.innerHTML.includes('其他接口 (338)'));
const css = fs.readFileSync(path.join(__dirname, '../bigscreen/style.css'), 'utf8');
assert.ok(css.includes('width: min(900px, calc(100% - 36px))'));
assert.ok(css.includes('.port-other-grid {') && css.includes('max-height: 180px'));
assert.ok(css.includes('.port-bank-two-row .port-bank-row { min-width: 760px'));
panel.close();
assert.strictEqual(panel.isOpen(), false);
global.fetch = originalFetch;
console.log('bigscreen Port Panel tests passed');
