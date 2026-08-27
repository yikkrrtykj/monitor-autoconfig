const assert = require('assert');
const dhcpPanelModule = require('../bigscreen/dhcp/dhcp-panel.js');
const dhcpModel = require('../bigscreen/dhcp/dhcp-model.js');

assert.deepStrictEqual(
  Object.keys(dhcpPanelModule),
  ['createDhcpPanel'],
  'the DHCP panel exposes only its dependency-injected controller factory'
);

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function groupAddressesByCBlock(addresses) {
  const blocks = new Map();
  addresses.forEach((ip) => {
    const prefix = String(ip).split('.').slice(0, 3).join('.');
    if (!blocks.has(prefix)) blocks.set(prefix, []);
    blocks.get(prefix).push(ip);
  });
  return [...blocks].map(([prefix, values]) => ({ prefix, addresses: values }));
}

class FakeElement {
  constructor(id = '') {
    this.id = id;
    this.dataset = {};
    this.listeners = new Map();
    this.disabled = false;
    this.hidden = false;
    this.value = '';
    this.textContent = '';
    this.className = '';
    this.scrollTop = 0;
    this._innerHTML = '';
    this.directory = null;
    this.detail = null;
  }

  set innerHTML(value) {
    this._innerHTML = String(value);
    this.directory = this._innerHTML.includes('class="dhcp-pool-directory"') ? new FakeElement() : null;
    this.detail = this._innerHTML.includes('class="dhcp-pool-detail"') ? new FakeElement() : null;
  }

  get innerHTML() {
    return this._innerHTML;
  }

  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(handler);
  }

  listenerCount(type) {
    return (this.listeners.get(type) || []).length;
  }

  async dispatch(type, values = {}) {
    const event = { type, target: values.target || this, ...values };
    const pending = [];
    (this.listeners.get(type) || []).forEach((handler) => {
      const result = handler(event);
      if (result && typeof result.then === 'function') pending.push(result);
    });
    await Promise.all(pending);
    return event;
  }

  querySelector(selector) {
    if (selector === '.dhcp-pool-directory') return this.directory;
    if (selector === '.dhcp-pool-detail') return this.detail;
    return null;
  }
}

class FakeDocument {
  constructor() {
    this.visibilityState = 'visible';
    this.listeners = new Map();
    this.elements = new Map();
    [
      'dhcpRefresh',
      'dhcpBindings',
      'dhcpPoolSearch',
      'dhcpPoolFilter',
      'dhcpPools',
      'dhcpPoolCount',
      'dhcpConnection',
      'dhcpStatus',
      'dhcpSummary',
      'dhcpFootnote',
      'dhcpBindingsStatus'
    ].forEach((id) => this.elements.set(id, new FakeElement(id)));
  }

  getElementById(id) {
    return this.elements.get(id) || null;
  }

  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(handler);
  }

  listenerCount(type) {
    return (this.listeners.get(type) || []).length;
  }

  async dispatch(type) {
    const pending = [];
    (this.listeners.get(type) || []).forEach((handler) => {
      const result = handler({ type, target: this });
      if (result && typeof result.then === 'function') pending.push(result);
    });
    await Promise.all(pending);
  }
}

class FakeWindow {
  constructor() {
    this.nextTimerId = 1;
    this.timers = new Map();
    this.cleared = [];
  }

  setTimeout(handler, delay) {
    const id = this.nextTimerId;
    this.nextTimerId += 1;
    this.timers.set(id, { handler, delay });
    return id;
  }

  clearTimeout(id) {
    this.cleared.push(id);
    this.timers.delete(id);
  }

  timersAt(delay) {
    return [...this.timers.entries()].filter(([, timer]) => timer.delay === delay);
  }

  async runTimer(id) {
    const timer = this.timers.get(id);
    assert.ok(timer, `timer ${id} exists`);
    this.timers.delete(id);
    await timer.handler();
    await flush();
  }

  async runFirstAt(delay) {
    const entry = this.timersAt(delay)[0];
    assert.ok(entry, `a ${delay}ms timer exists`);
    await this.runTimer(entry[0]);
  }
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolveValue, rejectValue) => {
    resolve = resolveValue;
    reject = rejectValue;
  });
  return { promise, resolve, reject };
}

function flush() {
  return new Promise((resolve) => setImmediate(resolve));
}

function pool(name, start, overrides = {}) {
  return {
    name,
    range: `${start}-${start.slice(0, start.lastIndexOf('.') + 1)}3`,
    total: 3,
    leased: 1,
    available: 1,
    excluded: 1,
    utilization: 50,
    level: 'good',
    excludedAddresses: [],
    ...overrides
  };
}

function dashboardPayload(overrides = {}) {
  const pools = overrides.pools === undefined ? [
    pool('Alpha', '192.168.40.1', { excludedAddresses: ['192.168.40.2'] }),
    pool('Beta', '192.168.41.1', { leased: 0, excluded: 0, excludedAddresses: [] })
  ] : overrides.pools;
  return {
    host: '192.168.10.254',
    capturedAt: 1700000000,
    refreshSeconds: 75,
    collectionSeconds: 1.25,
    cached: false,
    refreshing: false,
    warnings: [],
    conflicts: ['192.168.40.3'],
    summary: {
      poolCount: pools.length,
      total: pools.reduce((sum, item) => sum + item.total, 0),
      leased: pools.reduce((sum, item) => sum + item.leased, 0),
      available: pools.reduce((sum, item) => sum + item.available, 0),
      excluded: pools.reduce((sum, item) => sum + item.excluded, 0),
      utilization: 50,
      conflictCount: 1
    },
    pools,
    ...overrides
  };
}

function bindingPayload(overrides = {}) {
  return {
    capturedAt: 1700000001,
    bindings: [{ ip: '192.168.40.1', detail: 'alpha-client' }],
    arpEntries: [{ ip: '192.168.40.2', detail: 'alpha-arp' }],
    usedAddresses: ['192.168.40.1'],
    observedAddresses: ['192.168.40.2'],
    ...overrides
  };
}

function createHarness(options = {}) {
  const document = new FakeDocument();
  const window = new FakeWindow();
  const state = { active: options.active !== false };
  const dashboardCalls = [];
  const bindingCalls = [];
  const dataSuccess = [];
  const dashboardQueue = [...(options.dashboardQueue || [])];
  const bindingQueue = [...(options.bindingQueue || [])];
  const fetchDhcpDashboard = (force) => {
    dashboardCalls.push(force);
    if (dashboardQueue.length) {
      const value = dashboardQueue.shift();
      return typeof value === 'function' ? value(force) : value;
    }
    return Promise.resolve(options.dashboard || dashboardPayload());
  };
  const fetchDhcpBindings = () => {
    bindingCalls.push(true);
    if (bindingQueue.length) {
      const value = bindingQueue.shift();
      return typeof value === 'function' ? value() : value;
    }
    return Promise.resolve(options.bindings || bindingPayload());
  };
  const panel = dhcpPanelModule.createDhcpPanel({
    document,
    window,
    model: dhcpModel,
    escapeHtml,
    groupAddressesByCBlock,
    setText(id, value) {
      const element = document.getElementById(id);
      if (element) element.textContent = value;
    },
    fetchDhcpDashboard,
    fetchDhcpBindings,
    isPageActive: () => state.active,
    onDataSuccess: () => dataSuccess.push(true)
  });
  return { panel, document, window, state, dashboardCalls, bindingCalls, dataSuccess };
}

async function startAndFlush(harness) {
  harness.panel.start();
  await flush();
  await flush();
}

(async () => {
  const basic = createHarness();
  assert.deepStrictEqual(
    Object.keys(basic.panel),
    ['start', 'stop', 'hasScheduledRefresh'],
    'the facade exposes only the lifecycle needed by app.js'
  );
  assert.strictEqual(basic.document.listenerCount('visibilitychange'), 1, 'visibility recovery binds once at construction');
  await startAndFlush(basic);
  assert.deepStrictEqual(basic.dashboardCalls, [false], 'start performs the existing non-forced refresh');
  assert.strictEqual(basic.dataSuccess.length, 1);
  assert.strictEqual(basic.document.getElementById('dhcpStatus').className, 'dhcp-status good');
  assert.ok(basic.document.getElementById('dhcpStatus').textContent.includes('已从核心交换机刷新'));
  assert.ok(basic.document.getElementById('dhcpSummary').innerHTML.includes('总体使用率'));
  assert.ok(basic.document.getElementById('dhcpPools').innerHTML.includes('Alpha'));
  assert.ok(basic.document.getElementById('dhcpPools').innerHTML.includes('Beta'));
  assert.strictEqual(basic.document.getElementById('dhcpPoolCount').textContent, '显示 2 / 2 个网段');
  assert.strictEqual(basic.panel.hasScheduledRefresh(), true);
  assert.strictEqual(basic.window.timersAt(75000).length, 1, 'payload refreshSeconds keeps the adaptive timeout');
  assert.strictEqual(basic.window.timersAt(0).length, 1, 'successful dashboard render schedules the initial bindings read');

  await basic.window.runFirstAt(0);
  assert.strictEqual(basic.bindingCalls.length, 1);
  assert.ok(basic.document.getElementById('dhcpBindingsStatus').textContent.includes('DHCP 租约（绿色）1 个'));
  const boundHtml = basic.document.getElementById('dhcpPools').innerHTML;
  assert.ok(boundHtml.includes('dhcp-address-cell used'));
  assert.ok(boundHtml.includes('dhcp-address-cell reserved-used'));
  assert.ok(boundHtml.includes('dhcp-address-cell conflict'));
  assert.ok(boundHtml.includes('alpha-client'));
  assert.ok(boundHtml.includes('alpha-arp'));

  // Repeated start clears the prior polling timer and never duplicates DOM events.
  await startAndFlush(basic);
  assert.deepStrictEqual(basic.dashboardCalls, [false, false]);
  assert.strictEqual(basic.window.timersAt(75000).length, 1, 'repeated start leaves one polling timer');
  assert.strictEqual(basic.document.getElementById('dhcpRefresh').listenerCount('click'), 1);
  assert.strictEqual(basic.document.getElementById('dhcpBindings').listenerCount('click'), 1);
  assert.strictEqual(basic.document.getElementById('dhcpPoolSearch').listenerCount('input'), 1);
  assert.strictEqual(basic.document.getElementById('dhcpPoolFilter').listenerCount('change'), 1);
  assert.strictEqual(basic.document.getElementById('dhcpPools').listenerCount('click'), 1);
  assert.strictEqual(basic.document.listenerCount('visibilitychange'), 1);
  basic.panel.stop();
  assert.strictEqual(basic.panel.hasScheduledRefresh(), false, 'stop clears the DHCP polling timer');

  const floorCadence = createHarness({ dashboard: dashboardPayload({ refreshSeconds: 5 }) });
  await startAndFlush(floorCadence);
  assert.strictEqual(floorCadence.window.timersAt(30000).length, 1, 'adaptive refresh retains the 30 second floor');

  const failed = createHarness({
    dashboardQueue: [() => Promise.reject(new Error('telnet unavailable'))]
  });
  await startAndFlush(failed);
  assert.strictEqual(failed.document.getElementById('dhcpStatus').className, 'dhcp-status bad');
  assert.strictEqual(failed.document.getElementById('dhcpStatus').textContent, '读取失败：telnet unavailable');
  assert.strictEqual(failed.document.getElementById('dhcpSummary').innerHTML, '');
  assert.ok(failed.document.getElementById('dhcpPools').innerHTML.includes('/control#core-telnet'));
  assert.strictEqual(failed.window.timersAt(60000).length, 1, 'a failed refresh keeps the existing 60 second retry');

  const empty = createHarness({ dashboard: dashboardPayload({ pools: [], conflicts: [], summary: {} }) });
  await startAndFlush(empty);
  assert.strictEqual(empty.document.getElementById('dhcpPoolCount').textContent, '0 个网段');
  assert.ok(empty.document.getElementById('dhcpPools').innerHTML.includes('当前没有返回 DHCP 地址池'));

  // Search, filter and selection stay inside the panel and preserve the selected pool across refreshes.
  const browser = createHarness();
  await startAndFlush(browser);
  const search = browser.document.getElementById('dhcpPoolSearch');
  search.value = 'beta';
  await search.dispatch('input');
  assert.strictEqual(browser.document.getElementById('dhcpPoolCount').textContent, '显示 1 / 2 个网段');
  assert.ok(browser.document.getElementById('dhcpPools').innerHTML.includes('Beta'));
  assert.ok(!browser.document.getElementById('dhcpPools').innerHTML.includes('Alpha'));
  search.value = '';
  await search.dispatch('input');
  const filter = browser.document.getElementById('dhcpPoolFilter');
  filter.value = 'active';
  await filter.dispatch('change');
  assert.strictEqual(browser.document.getElementById('dhcpPoolCount').textContent, '显示 1 / 2 个网段');
  assert.ok(browser.document.getElementById('dhcpPools').innerHTML.includes('Alpha'));
  assert.ok(!browser.document.getElementById('dhcpPools').innerHTML.includes('Beta'));
  filter.value = 'all';
  await filter.dispatch('change');
  const betaKey = dhcpModel.dhcpPoolKey(dashboardPayload().pools[1]);
  await browser.document.getElementById('dhcpPools').dispatch('click', {
    target: { closest: () => ({ dataset: { dhcpPool: betaKey } }) }
  });
  let html = browser.document.getElementById('dhcpPools').innerHTML;
  assert.ok(html.includes(`selected" data-dhcp-pool="${betaKey}"`), 'pool click updates selection');
  await browser.document.getElementById('dhcpRefresh').dispatch('click');
  html = browser.document.getElementById('dhcpPools').innerHTML;
  assert.ok(html.includes(`selected" data-dhcp-pool="${betaKey}"`), 'selection survives a dashboard refresh');
  assert.strictEqual(browser.dashboardCalls.at(-1), true, 'manual refresh remains forced');

  // A dashboard request already in flight blocks another refresh request.
  const pendingRefresh = deferred();
  const concurrent = createHarness({ dashboardQueue: [pendingRefresh.promise] });
  concurrent.panel.start();
  await flush();
  const concurrentClick = concurrent.document.getElementById('dhcpRefresh').dispatch('click');
  await flush();
  assert.strictEqual(concurrent.dashboardCalls.length, 1, 'refresh concurrency guard prevents a second fetch');
  pendingRefresh.resolve(dashboardPayload());
  await concurrentClick;
  await flush();

  // Sequence invalidation prevents an older request from replacing the newer start result.
  const oldRequest = deferred();
  const newRequest = deferred();
  const stale = createHarness({ dashboardQueue: [oldRequest.promise, newRequest.promise] });
  stale.panel.start();
  await flush();
  stale.panel.start();
  await flush();
  newRequest.resolve(dashboardPayload({ host: 'new-host' }));
  await flush();
  await flush();
  oldRequest.resolve(dashboardPayload({ host: 'old-host' }));
  await flush();
  await flush();
  assert.ok(stale.document.getElementById('dhcpConnection').textContent.startsWith('new-host'));
  assert.ok(!stale.document.getElementById('dhcpConnection').textContent.includes('old-host'));
  assert.strictEqual(stale.dataSuccess.length, 1, 'stale response does not report a second data success');
  assert.strictEqual(stale.window.timersAt(75000).length, 1, 'stale response does not create another polling timer');

  // Hidden pages stop polling; becoming visible restarts with the original behavior.
  const visibility = createHarness();
  await startAndFlush(visibility);
  visibility.document.visibilityState = 'hidden';
  await visibility.document.dispatch('visibilitychange');
  assert.strictEqual(visibility.panel.hasScheduledRefresh(), false);
  visibility.document.visibilityState = 'visible';
  await visibility.document.dispatch('visibilitychange');
  await flush();
  await flush();
  assert.deepStrictEqual(visibility.dashboardCalls, [false, false]);
  assert.strictEqual(visibility.panel.hasScheduledRefresh(), true);
  visibility.state.active = false;
  visibility.document.visibilityState = 'hidden';
  await visibility.document.dispatch('visibilitychange');
  assert.strictEqual(visibility.panel.hasScheduledRefresh(), true, 'visibility changes are ignored off the DHCP route');

  const bindingFailure = createHarness({
    bindingQueue: [() => Promise.reject(new Error('ARP timeout'))]
  });
  await startAndFlush(bindingFailure);
  await bindingFailure.window.runFirstAt(0);
  assert.strictEqual(bindingFailure.document.getElementById('dhcpBindingsStatus').textContent, '已用 IP 查询失败：ARP timeout');
  assert.strictEqual(bindingFailure.document.getElementById('dhcpBindings').disabled, false);

  console.log('bigscreen DHCP panel tests passed');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
