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
const modular = groupPorts([
  port('Te1/0/1', 1, 1), port('Te1/1/1', 1, 1), port('Te1/0/1', 1, 1),
  port('Gi1/0/3', 1, 3)
]);
assert.strictEqual(modular.members.length, 1, 'one stack member has one outer section');
assert.deepStrictEqual(modular.members[0].banks.map((bank) => `${bank.family} ${bank.member}/${bank.slot}`),
  ['Gi 1/0', 'Te 1/0', 'Te 1/1']);
assert.strictEqual(modular.members[0].banks[1].items.length, 1,
  'duplicate physical identities cannot appear twice in one bank');
assert.strictEqual(modular.other.length, 1, 'duplicate row remains accessible as an other interface');
assert.strictEqual(groupPorts(Array.from({ length: 12 }, (_, index) =>
  port(`Te1/1/${index + 1}`, 1, index + 1))).banks[0].twoRow, false,
'a twelve-port modular bank remains compact');

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
  fetchPortHistory: async () => ({ rx: [], tx: [], coverage: { rx: 'empty', tx: 'empty' } }),
  setTimeout: (handler, delay) => { assert.strictEqual(delay, 120); tasks.push(handler); return tasks.length; },
  clearTimeout: () => {} });
const payload = { ip: '192.0.2.7', name: '<Core>', model: 'C9300', degraded: true,
  warnings: ['当前 IF-MIB 无有效覆盖', '已省略过期邻接'], ports: [
    port('Gi1/0/1', 1, 1, { speedBps: 2_500_000_000, rxBps: null, txBps: null, rxUtilization: null,
      vlanEvidence: { authority: 'observed', memberships: [{ vlanId: 42, untagged: true }] },
      neighbors: null, inputErrorsTotal: 3 }),
    port('Gi1/0/2', 1, 2, { adminState: 'up', operState: 'down', neighbors: [] }),
    port('Port-channel1', null, null, { speedBps: 10_000_000_000 })
  ] };
panel.loading(payload.ip);
assert.strictEqual(panel.isOpen(), true);
panel.open(payload);
assert.ok(root.innerHTML.includes('&lt;Core&gt;'));
assert.ok(root.innerHTML.includes('当前 IF-MIB 无有效覆盖'));
assert.ok(root.innerHTML.includes('已省略过期邻接'));
assert.ok(root.innerHTML.includes('其他接口 (1)'));
assert.ok(root.innerHTML.includes('接口 3 · 物理可识别 2 · 其他 1'));
assert.ok(root.innerHTML.includes('<details class="port-other">'), 'other interfaces start collapsed');
assert.strictEqual((root.innerHTML.match(/<section class="port-member">/g) || []).length, 1);
assert.ok(root.innerHTML.includes('Gi 1/0'));
assert.ok(root.innerHTML.includes('累计 errors/discards 非 0（不等于当前故障）'));
for (const label of ['在线', '链路断开', '管理关闭', '未知']) {
  assert.ok(root.innerHTML.includes(label), `state legend includes ${label}`);
}
assert.ok(root.innerHTML.includes('Port-channel1'));
assert.ok(root.innerHTML.includes('port-state-up'));
assert.ok(root.innerHTML.includes('port-state-down'));
assert.ok(root.innerHTML.includes('aria-label="Gi1/0/1 在线"'));
assert.ok(root.innerHTML.includes('port-face-counter'));
assert.ok(root.innerHTML.includes('2.5G') && root.innerHTML.includes('10G'));
assert.ok(!root.innerHTML.includes('3G'), '2.5G must not round to 3G');

const first = new FakeNode(); first.dataset.portIndex = '0';
const second = new FakeNode(); second.dataset.portIndex = '1';
const numberChild = { closest: () => first };
const speedChild = { closest: () => first };
root.faces = [first, second];
root.onmouseover({ target: first });
assert.strictEqual(root.card.innerHTML, '', 'pointer hover waits before showing the card');
root.onmouseout({ target: numberChild, relatedTarget: speedChild });
root.onmouseover({ target: speedChild, relatedTarget: numberChild });
assert.strictEqual(tasks.length, 1, 'internal movement does not re-arm the pending hover timer');
tasks.shift()();
assert.ok(root.card.innerHTML.includes('RX</dt><dd>—'));
assert.ok(root.card.innerHTML.includes('VLAN 观测数据 / 非配置权威'));
assert.ok(root.card.innerHTML.includes('邻接资料暂不可用'));
assert.ok(root.card.innerHTML.includes('输入错误（累计）'));
assert.ok(root.card.innerHTML.includes('<dt>速率</dt><dd>2.5G</dd>'));
const scheduledBeforeInternalMove = tasks.length;
root.onmouseout({ target: numberChild, relatedTarget: speedChild });
root.onmouseover({ target: speedChild, relatedTarget: numberChild });
assert.strictEqual(root.card.hidden, false, 'moving within one port keeps the hover card visible');
assert.strictEqual(tasks.length, scheduledBeforeInternalMove, 'moving within one port does not restart the delay');
root.onfocusin({ target: second });
assert.ok(root.card.innerHTML.includes('当前快照未发现邻接'), 'empty neighbor list differs from unavailable');
root.onclick({ target: first });
assert.strictEqual(root.pinned.hidden, false);
assert.ok(root.pinned.innerHTML.includes('VLAN 观测数据 / 非配置权威'));
assert.strictEqual(first.attributes['aria-pressed'], 'true');
assert.strictEqual(networkReads, 0, 'hover, focus and pin use loaded data only');

panel.open({ ...payload, ports: [
  port('Te1/0/1', 1, 1), port('Te1/1/1', 1, 1), port('Gi1/0/3', 1, 3)
] });
assert.strictEqual((root.innerHTML.match(/<section class="port-member">/g) || []).length, 1);
assert.ok(root.innerHTML.includes('Te 1/0') && root.innerHTML.includes('Te 1/1'));
assert.strictEqual((root.innerHTML.match(/class="port-face-number">1</g) || []).length, 2,
  'equal slot numbers are separated by distinct sub-bank labels');

const hillstone = { ...payload, kind: 'hillstone', name: 'FW A', model: 'SG-6000',
  warnings: ['当前防火墙 IF-MIB 无有效覆盖', '已省略过期邻接'], ports: [
    port('eth1', null, null, { ifAlias: 'WAN-A', rxBps: null, txBps: null,
      wanEvidence: { authority: 'isp-inventory', name: 'ISP A', wanIp: '198.51.100.2', gateway: '198.51.100.1' },
      inputErrorsTotal: 4, neighbors: null }),
    port('eth2', null, null, { adminState: 'down', operState: 'down', speedBps: null,
      rxBps: null, txBps: null, wanEvidence: null, neighbors: [] })
  ] };
panel.open(hillstone);
assert.ok(root.innerHTML.includes('接口 2 · 在线 1 · 离线 1 · 未知 0 · 管理关闭 1'));
assert.ok(root.innerHTML.includes('WAN / ISP') && root.innerHTML.includes('其他接口'));
assert.ok(root.innerHTML.includes('当前防火墙 IF-MIB 无有效覆盖'));
assert.ok(root.innerHTML.includes('port-interface-tile'));
assert.ok(root.innerHTML.includes('历史累计证据，不等于当前故障'));
assert.ok(!root.innerHTML.includes('port-member') && !root.innerHTML.includes('port-bank'),
  'Hillstone must not reuse Cisco physical or StackWise layout');
first.dataset.portIndex = '0';
root.onfocusin({ target: first });
assert.ok(root.card.innerHTML.includes('ISP A'));
assert.ok(root.card.innerHTML.includes('RX</dt><dd>—</dd>'));
assert.ok(root.card.innerHTML.includes('TX</dt><dd>—</dd>'));
assert.ok(root.card.innerHTML.includes('邻接资料暂不可用'));
root.onclick({ target: first });
assert.ok(root.pinned.innerHTML.includes('WAN IP 198.51.100.2'));
second.dataset.portIndex = '1';
root.onfocusin({ target: second });
assert.ok(root.card.innerHTML.includes('当前快照未发现邻接'));
assert.strictEqual(networkReads, 0, 'Hillstone hover, focus and pin use only loaded payload');
panel.open({ ...hillstone, ports: [{ ...hillstone.ports[1] }], warnings: [] });
assert.ok(!root.innerHTML.includes('WAN / ISP'), 'no exact WAN evidence means no WAN section');

panel.open({ ...hillstone, kind: 'generic-switch', model: null, ports: [
  port('Gi1/0/1', 1, 1, { metricSource: 'LibreNMS poller', metricAgeSeconds: 30, rxBps: 80, txBps: null })
] });
assert.ok(!root.innerHTML.includes('LibreNMS poller · 30 秒前'));
assert.ok(root.innerHTML.includes('型号 —'));
assert.ok(!root.innerHTML.includes('port-member') && !root.innerHTML.includes('port-bank'));
root.onfocusin({ target: first });
root.onclick({ target: first });
assert.ok(root.pinned.innerHTML.includes('指标来源'));
assert.ok(root.pinned.innerHTML.includes('LibreNMS poller · 30 秒前'));
assert.ok(root.pinned.innerHTML.includes('TX</dt><dd>—</dd>'));
assert.strictEqual(networkReads, 0);

panel.open({ ...payload, kind: 'cisco', warnings: ['Prometheus IF-MIB 无有效覆盖，已使用 LibreNMS poller'], ports: [
  port('Gi1/0/1', 1, 1, { metricSource: 'LibreNMS poller', metricAgeSeconds: 30,
    rxBps: 80, txBps: 160, inputErrorsTotal: 0, outputErrorsTotal: 0,
    inputDiscardsTotal: null, outputDiscardsTotal: null })
] });
assert.ok(root.innerHTML.includes('port-member') && root.innerHTML.includes('Gi 1/0'));
assert.ok(!root.innerHTML.includes('class="port-face-counter"'), 'zero counters never create a warning marker');
root.onfocusin({ target: first });
assert.ok(root.card.innerHTML.includes('LibreNMS poller · 30 秒前'));
assert.ok(root.card.innerHTML.includes('输入错误（累计）</dt><dd>0</dd>'));
assert.ok(root.card.innerHTML.includes('输入丢弃（累计）</dt><dd>—</dd>'));
assert.strictEqual(networkReads, 0);

for (const kind of ['cisco', 'hillstone', 'generic-switch']) {
  const warnings = ['Prometheus IF-MIB 无有效覆盖，已使用 LibreNMS poller', '已省略过期邻接', 'WAN / ISP 资料暂不可用'];
  panel.open({ ...payload, kind, degraded: true, warnings, ports: [
    port('Gi1/0/1', 1, 1, { metricSource: 'LibreNMS poller', metricAgeSeconds: 30, rxBps: 80 }),
    port('Gi1/0/2', 1, 2, { txBps: null })
  ] });
  first.dataset.portIndex = '0';
  second.dataset.portIndex = '1';
  assert.strictEqual(root.dataset.panelMode, kind === 'cisco' ? 'physical' : 'interfaces');
  assert.ok(!root.innerHTML.includes('资料部分降级'));
  warnings.forEach((warning) => assert.ok(root.innerHTML.includes(warning)));
  root.onclick({ target: first });
  assert.strictEqual(root.pinned.hidden, false);
  assert.ok(root.pinned.innerHTML.includes('关闭详情'));
  assert.strictEqual(first.attributes['aria-pressed'], 'true');
  root.onclick({ target: second });
  assert.ok(root.pinned.innerHTML.includes('Gi1/0/2'));
  assert.strictEqual(first.attributes['aria-pressed'], 'false');
  assert.strictEqual(second.attributes['aria-pressed'], 'true');
  root.onclick({ target: second });
  assert.strictEqual(root.pinned.hidden, true);
  assert.strictEqual(root.pinned.innerHTML, '');
  assert.ok(root.faces.every((face) => face.attributes['aria-pressed'] === 'false'));
  root.onclick({ target: second });
  assert.strictEqual(root.pinned.hidden, false, 'same port can reopen after deselection');
  root.onclick({ target: { closest: (selector) => selector === '.port-pinned-close' ? {} : null } });
  assert.strictEqual(root.pinned.hidden, true);
  assert.strictEqual(root.pinned.innerHTML, '');
  assert.ok(root.faces.every((face) => face.attributes['aria-pressed'] === 'false'));
  root.onclick({ target: second });
  assert.strictEqual(root.pinned.hidden, false, 'explicit close resets selected index');
  let prevented = 0;
  let stopped = 0;
  const escape = { key: 'Escape', preventDefault: () => prevented++, stopPropagation: () => stopped++ };
  root.onkeydown(escape);
  assert.strictEqual(root.pinned.hidden, true);
  assert.strictEqual(root.pinned.innerHTML, '');
  assert.ok(root.faces.every((face) => face.attributes['aria-pressed'] === 'false'));
  assert.strictEqual(panel.isOpen(), true, 'first Escape closes detail only');
  root.onkeydown(escape);
  assert.strictEqual(prevented, 1);
  assert.strictEqual(stopped, 1, 'subsequent Escape is left to existing outer behavior');
  root.onclick({ target: second });
  assert.strictEqual(root.pinned.hidden, false, 'Escape resets selected index');
  panel.close();
  assert.strictEqual(root.onkeydown, null);
  assert.strictEqual(root.onclick, null);
  assert.strictEqual(root.pinned.hidden, true);
  assert.ok(root.faces.every((face) => face.attributes['aria-pressed'] === 'false'));
  assert.strictEqual(root.dataset.panelMode, undefined);
}
assert.strictEqual(networkReads, 0, 'all pin lifecycle actions stay payload-only');

const longName = 'ethernet-' + 'x'.repeat(100);
const longAlias = 'full alias ' + 'y'.repeat(100);
for (const kind of ['hillstone', 'generic-switch']) {
  const ports = Array.from({ length: 30 }, (_, index) => port(index === 0 ? longName : `eth${index}`, null, null, {
    ifAlias: longAlias, ifDescr: 'full description', metricSource: 'LibreNMS poller', metricAgeSeconds: 113,
    rxBps: 800, txBps: 1600, inputErrorsTotal: index === 0 ? 4 : 0,
    outputErrorsTotal: 0, inputDiscardsTotal: null, outputDiscardsTotal: null,
    wanEvidence: index < 2 ? { authority: 'isp-inventory', name: 'ISP A' } : null,
    vlanEvidence: { authority: 'observed', memberships: [{ vlanId: 42, untagged: true }] }, neighbors: []
  }));
  panel.open({ ...payload, kind, ports });
  assert.strictEqual((root.innerHTML.match(/class="port-face port-interface-tile /g) || []).length, 30);
  const sections = [...root.innerHTML.matchAll(/<div class="port-interface-list">([\s\S]*?)<\/div>/g)];
  assert.strictEqual(sections.length, 2, 'WAN and remaining interfaces share the same grid container');
  assert.strictEqual((sections[0][1].match(/port-interface-tile/g) || []).length, 2);
  assert.strictEqual((sections[1][1].match(/port-interface-tile/g) || []).length, 28);
  assert.ok(!root.innerHTML.includes('port-interface-row') && !root.innerHTML.includes('port-bank'));
  assert.ok(root.innerHTML.includes('port-interface-summary') && root.innerHTML.includes('在线 · 1G'));
  assert.ok(!root.innerHTML.includes(longAlias));
  assert.ok(!root.innerHTML.includes('113 秒前'));
  assert.strictEqual((root.innerHTML.match(/class="port-face-counter"/g) || []).length, 1);
  first.dataset.portIndex = '0';
  root.onfocusin({ target: first });
  assert.ok(root.card.innerHTML.includes(longName) && root.card.innerHTML.includes(longAlias));
  assert.ok(root.card.innerHTML.includes('LibreNMS poller · 113 秒前'));
  assert.ok(root.card.innerHTML.includes('RX</dt><dd>800 b/s</dd>'));
  root.onclick({ target: first });
  assert.ok(root.pinned.innerHTML.includes(longAlias) && root.pinned.innerHTML.includes('full description'));
  assert.ok(root.pinned.innerHTML.includes('VLAN 观测数据 / 非配置权威'));
  root.onclick({ target: first });
  assert.strictEqual(root.pinned.hidden, true);
  assert.strictEqual(networkReads, 0);
}

const dense = { ...payload, ports: Array.from({ length: 338 }, (_, index) =>
  port(`Port-channel${index + 1}`, null, null, { speedBps: null })) };
panel.open(dense);
assert.strictEqual((root.innerHTML.match(/data-port-index=/g) || []).length, 338);
assert.ok(root.innerHTML.includes('其他接口 (338)'));
const css = fs.readFileSync(path.join(__dirname, '../bigscreen/style.css'), 'utf8');
assert.ok(css.includes('width: min(900px, calc(100% - 36px))'));
assert.ok(css.includes('.topology-port-panel[data-panel-mode="interfaces"] { width: min(740px, calc(100% - 36px)); }'));
assert.ok(css.includes('.topology-port-panel[data-panel-mode="interfaces"] { width: auto; }'));
assert.ok(css.includes('grid-template-columns: repeat(auto-fill, minmax(min(120px, 100%), 1fr))'));
assert.ok(css.includes('.port-interface-tile { width: 100%; height: 54px; min-width: 0;'));
assert.ok(!css.includes('.port-interface-row'));
assert.ok(css.includes('text-overflow: ellipsis; white-space: nowrap;'));
assert.ok(css.includes('text-overflow: ellipsis'));
assert.ok(css.includes('.port-other-grid {') && css.includes('max-height: 180px'));
assert.ok(css.includes('.port-bank-two-row .port-bank-row { min-width: 760px'));
panel.close();
assert.strictEqual(panel.isOpen(), false);
global.fetch = originalFetch;
console.log('bigscreen Port Panel tests passed');
