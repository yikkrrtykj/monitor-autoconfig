const assert = require('assert');
const deliveryPanelModule = require('../bigscreen/control/delivery-panel.js');
const { escapeHtml } = require('../bigscreen/utils.js');

assert.deepStrictEqual(
  Object.keys(deliveryPanelModule),
  ['createDeliveryPanel'],
  'the Delivery panel exposes only its dependency-injected factory'
);

class FakeElement {
  constructor(id, ownerDocument) {
    this.id = id;
    this.ownerDocument = ownerDocument;
    this.dataset = {};
    this.listeners = new Map();
    this.className = '';
    this.textContent = '';
    this.hidden = false;
    this.disabled = false;
    this._innerHTML = '';
  }

  set innerHTML(value) {
    this._innerHTML = String(value);
    this.registerIds(this._innerHTML);
  }

  get innerHTML() {
    return this._innerHTML;
  }

  registerIds(markup) {
    const pattern = /\sid="([^"]+)"/g;
    let match;
    while ((match = pattern.exec(markup)) !== null) {
      if (!this.ownerDocument.elements.has(match[1])) {
        this.ownerDocument.elements.set(match[1], new FakeElement(match[1], this.ownerDocument));
      }
    }
  }

  insertAdjacentHTML(_position, markup) {
    this._innerHTML += String(markup);
    this.registerIds(String(markup));
  }

  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(handler);
  }

  listenerCount(type) {
    return (this.listeners.get(type) || []).length;
  }

  dispatch(type, event = {}) {
    const pending = (this.listeners.get(type) || []).map((handler) => handler({
      type,
      target: event.target || this,
      ...event
    }));
    return Promise.all(pending.filter((result) => result && typeof result.then === 'function'));
  }
}

class FakeDocument {
  constructor(withContainer = true) {
    this.elements = new Map();
    if (withContainer) {
      this.elements.set('controlDelivery', new FakeElement('controlDelivery', this));
    }
  }

  getElementById(id) {
    return this.elements.get(id) || null;
  }
}

class FakeRetireRow {
  constructor(key, token) {
    this.dataset = { key, token };
  }
}

class FakeRetireButton {
  constructor(action, row) {
    this.dataset = { retireAction: action };
    this.row = row;
    this.disabled = false;
    this.textContent = action === 'delete' ? '确认删除' : '保留设备';
  }

  closest(selector) {
    if (selector === 'button[data-retire-action]') return this;
    if (selector === '.retire-pending-row') return this.row;
    return null;
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

async function settle() {
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
}

function valueFrom(source, index, ...args) {
  const entry = Array.isArray(source)
    ? source[Math.min(index, source.length - 1)]
    : source;
  if (typeof entry === 'function') return entry(...args);
  return entry;
}

function createHarness(options = {}) {
  const document = options.document || new FakeDocument(options.withContainer !== false);
  const postCalls = [];
  const retireFetchCalls = [];
  const timeoutCalls = [];
  let iperfMountCalls = 0;
  const postSource = options.postPlatform || ((path) => {
    if (path === '/pre-check') return { ok: true, verdict: 'good', pass: 5, warn: 0, fail: 0, output: 'ready' };
    if (path === '/test-alert') return { ok: true, channel: 'app' };
    return { ok: true };
  });
  const retireSource = options.fetchRetirePending || [{ ok: true, pending: [] }];
  const iperfController = options.iperfController || {
    ensureMounted(container) {
      iperfMountCalls += 1;
      container.insertAdjacentHTML('beforeend', '<section id="iperfFixture">iPerf active</section>');
    }
  };

  const panel = deliveryPanelModule.createDeliveryPanel({
    document,
    setTimeout(handler, delay) {
      timeoutCalls.push({ handler, delay });
      return timeoutCalls.length;
    },
    escapeHtml,
    postPlatform(path, payload) {
      const index = postCalls.length;
      postCalls.push({ path, payload });
      try {
        return Promise.resolve(valueFrom(postSource, index, path, payload, index));
      } catch (error) {
        return Promise.reject(error);
      }
    },
    fetchRetirePending() {
      const index = retireFetchCalls.length;
      retireFetchCalls.push(index);
      try {
        return Promise.resolve(valueFrom(retireSource, index, index));
      } catch (error) {
        return Promise.reject(error);
      }
    },
    iperfController
  });

  return {
    panel,
    document,
    postCalls,
    retireFetchCalls,
    timeoutCalls,
    get iperfMountCalls() { return iperfMountCalls; }
  };
}

async function main() {
  // Initial render keeps the exact Delivery shell, mounts the existing iPerf
  // controller, binds actions once, and loads the dynamic pending-device list.
  const initial = createHarness({
    fetchRetirePending: [{
      ok: true,
      pending: [{
        key: 'core-<1>',
        token: 'tok-"1"',
        name: '<Core & One>',
        ip: '10.0.0.1',
        downSince: 1724800000
      }]
    }]
  });
  initial.panel.render();
  await settle();
  const shell = initial.document.getElementById('controlDelivery');
  assert.strictEqual(shell.dataset.built, '1');
  assert.match(shell.innerHTML, /class="delivery-actions"/);
  assert.match(shell.innerHTML, /id="preCheckBtn"/);
  assert.match(shell.innerHTML, /id="testAlertBtn"/);
  assert.match(shell.innerHTML, /id="retirePendingRefreshBtn"/);
  assert.match(shell.innerHTML, /id="iperfFixture"/);
  assert.strictEqual(initial.iperfMountCalls, 1);
  assert.strictEqual(initial.retireFetchCalls.length, 1);
  assert.strictEqual(initial.document.getElementById('preCheckBtn').listenerCount('click'), 1);
  assert.strictEqual(initial.document.getElementById('testAlertBtn').listenerCount('click'), 1);
  assert.strictEqual(initial.document.getElementById('retirePendingRefreshBtn').listenerCount('click'), 1);
  assert.strictEqual(initial.document.getElementById('retirePendingList').listenerCount('click'), 1);
  const pendingMarkup = initial.document.getElementById('retirePendingList').innerHTML;
  assert.match(pendingMarkup, /&lt;Core &amp; One&gt;/);
  assert.match(pendingMarkup, /data-key="core-&lt;1&gt;"/);
  assert.match(pendingMarkup, /data-token="tok-&quot;1&quot;"/);
  assert.ok(!pendingMarkup.includes('<Core & One>'));

  // A ten-second Control snapshot render is a no-op for an already built
  // Delivery panel: listeners, operator output, and active iPerf DOM survive.
  const preResult = initial.document.getElementById('preCheckResult');
  const iperfFixture = initial.document.getElementById('iperfFixture');
  preResult.textContent = 'operator is reading this';
  iperfFixture.textContent = 'running task 17';
  const shellMarkup = shell.innerHTML;
  initial.panel.render({ ignoredSnapshot: true });
  assert.strictEqual(shell.innerHTML, shellMarkup);
  assert.strictEqual(preResult.textContent, 'operator is reading this');
  assert.strictEqual(iperfFixture.textContent, 'running task 17');
  assert.strictEqual(initial.iperfMountCalls, 1);
  assert.strictEqual(initial.retireFetchCalls.length, 1);
  assert.strictEqual(initial.document.getElementById('preCheckBtn').listenerCount('click'), 1);

  // Pre-check retains busy timing, API payload, verdict markup, and escaping.
  const precheckPending = deferred();
  const precheck = createHarness({
    postPlatform: (path) => path === '/pre-check' ? precheckPending.promise : { ok: true },
  });
  precheck.panel.render();
  await settle();
  const preBtn = precheck.document.getElementById('preCheckBtn');
  const preBox = precheck.document.getElementById('preCheckResult');
  const precheckClick = preBtn.dispatch('click');
  assert.strictEqual(preBtn.disabled, true);
  assert.strictEqual(preBox.hidden, false);
  assert.strictEqual(preBox.textContent, '体检中…（最长约 2 分钟）');
  precheckPending.resolve({ ok: true, verdict: 'warn', pass: 8, warn: 2, fail: 0, output: '<unsafe>' });
  await precheckClick;
  assert.strictEqual(preBtn.disabled, false);
  assert.strictEqual(preBox.className, 'precheck-result warn');
  assert.match(preBox.innerHTML, /⚠ 有警告，请确认　通过 8 · 警告 2 · 失败 0/);
  assert.match(preBox.innerHTML, /&lt;unsafe&gt;/);
  assert.deepStrictEqual(precheck.postCalls[0], { path: '/pre-check', payload: {} });

  const precheckBad = createHarness({
    postPlatform: (path) => path === '/pre-check' ? { ok: false, error: 'bridge unavailable' } : { ok: true }
  });
  precheckBad.panel.render();
  await settle();
  await precheckBad.document.getElementById('preCheckBtn').dispatch('click');
  assert.strictEqual(precheckBad.document.getElementById('preCheckResult').textContent, '体检失败：bridge unavailable');

  const precheckError = createHarness({
    postPlatform: (path) => {
      if (path === '/pre-check') throw new Error('request timeout');
      return { ok: true };
    }
  });
  precheckError.panel.render();
  await settle();
  await precheckError.document.getElementById('preCheckBtn').dispatch('click');
  assert.strictEqual(precheckError.document.getElementById('preCheckResult').textContent, '体检失败：request timeout');

  // Test-alert keeps the existing channel/fallback wording, busy state, and
  // error presentation without affecting the other Delivery actions.
  const alertPending = deferred();
  const alert = createHarness({
    postPlatform: (path) => path === '/test-alert' ? alertPending.promise : { ok: true }
  });
  alert.panel.render();
  await settle();
  const alertBtn = alert.document.getElementById('testAlertBtn');
  const alertResult = alert.document.getElementById('testAlertResult');
  const alertClick = alertBtn.dispatch('click');
  assert.strictEqual(alertBtn.disabled, true);
  assert.strictEqual(alertResult.textContent, '发送中…');
  alertPending.resolve({ ok: true, channel: 'webhook', appError: 'app credential invalid' });
  await alertClick;
  assert.strictEqual(alertBtn.disabled, false);
  assert.strictEqual(alertResult.className, 'test-alert-result warn');
  assert.strictEqual(alertResult.textContent, '已通过 Webhook 回退发送；自建应用失败：app credential invalid');
  assert.deepStrictEqual(alert.postCalls[0], { path: '/test-alert', payload: {} });

  const alertError = createHarness({
    postPlatform: (path) => {
      if (path === '/test-alert') throw new Error('send timeout');
      return { ok: true };
    }
  });
  alertError.panel.render();
  await settle();
  await alertError.document.getElementById('testAlertBtn').dispatch('click');
  assert.strictEqual(alertError.document.getElementById('testAlertResult').textContent, '失败：send timeout');
  assert.strictEqual(alertError.document.getElementById('testAlertResult').className, 'test-alert-result bad');

  // Pending-device refresh supports dynamic payloads without rebuilding the
  // panel. A successful keep action retains the exact request and refreshes.
  const retire = createHarness({
    fetchRetirePending: [
      { ok: true, pending: [{ key: 'dist-1', token: 'tok-1', name: 'Stage 1', ip: '10.0.1.1' }] },
      { ok: true, pending: [{ key: 'dist-2', token: 'tok-2', name: 'Stage 2', ip: '10.0.1.2' }] },
      { ok: true, pending: [] }
    ],
    postPlatform: { ok: true }
  });
  retire.panel.render();
  await settle();
  const retireList = retire.document.getElementById('retirePendingList');
  assert.match(retireList.innerHTML, /Stage 1/);
  await retire.document.getElementById('retirePendingRefreshBtn').dispatch('click');
  assert.match(retireList.innerHTML, /Stage 2/);
  const keepRow = new FakeRetireRow('dist-2', 'tok-2');
  const keepButton = new FakeRetireButton('keep', keepRow);
  await retireList.dispatch('click', { target: keepButton });
  assert.deepStrictEqual(retire.postCalls[0], {
    path: '/network/retire/resolve',
    payload: { key: 'dist-2', token: 'tok-2', action: 'keep' }
  });
  assert.strictEqual(keepButton.disabled, false);
  assert.strictEqual(retireList.className, 'network-tool-result good');
  assert.strictEqual(retireList.textContent, '没有待删除设备。');

  // Delete keeps the current two-click arm, five-second disarm timer, failure
  // message, and delayed 1.5-second list recovery.
  const retireFailure = createHarness({
    fetchRetirePending: [{ ok: true, pending: [{ key: 'core-1', token: 'tok-x', name: 'Core' }] }],
    postPlatform: { ok: false, error: 'token expired' }
  });
  retireFailure.panel.render();
  await settle();
  const deleteRow = new FakeRetireRow('core-1', 'tok-x');
  const deleteButton = new FakeRetireButton('delete', deleteRow);
  const failureList = retireFailure.document.getElementById('retirePendingList');
  await failureList.dispatch('click', { target: deleteButton });
  assert.strictEqual(retireFailure.postCalls.length, 0);
  assert.strictEqual(deleteButton.dataset.armed, '1');
  assert.strictEqual(deleteButton.textContent, '再点一次确认删除');
  assert.strictEqual(retireFailure.timeoutCalls[0].delay, 5000);
  await failureList.dispatch('click', { target: deleteButton });
  assert.deepStrictEqual(retireFailure.postCalls[0], {
    path: '/network/retire/resolve',
    payload: { key: 'core-1', token: 'tok-x', action: 'delete' }
  });
  assert.strictEqual(failureList.className, 'network-tool-result bad');
  assert.strictEqual(failureList.textContent, 'token expired');
  assert.strictEqual(retireFailure.timeoutCalls[1].delay, 1500);
  assert.strictEqual(deleteButton.disabled, false);

  const retireError = createHarness({
    fetchRetirePending: [{ ok: true, pending: [{ key: 'fw-1', token: 'tok-f', name: 'Firewall' }] }],
    postPlatform: () => { throw new Error('resolve timeout'); }
  });
  retireError.panel.render();
  await settle();
  const keepErrorButton = new FakeRetireButton('keep', new FakeRetireRow('fw-1', 'tok-f'));
  const retireErrorList = retireError.document.getElementById('retirePendingList');
  await retireErrorList.dispatch('click', { target: keepErrorButton });
  assert.strictEqual(retireErrorList.textContent, '操作失败：resolve timeout');
  assert.strictEqual(keepErrorButton.disabled, false);

  // A missing Control Delivery host remains a harmless no-op.
  const missing = createHarness({ withContainer: false });
  missing.panel.render();
  assert.strictEqual(missing.iperfMountCalls, 0);
  assert.strictEqual(missing.retireFetchCalls.length, 0);

  console.log('bigscreen Delivery panel tests passed');
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
