const assert = require('assert');
const configEditorModule = require('../bigscreen/config/config-editor.js');
const configModel = require('../bigscreen/config/config-model.js');

assert.deepStrictEqual(
  Object.keys(configEditorModule),
  ['createConfigEditor'],
  'the Config Editor module exposes only its dependency-injected controller factory'
);

function decodeHtml(value) {
  return String(value || '')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&amp;/g, '&');
}

function escapeHtml(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function dataKey(name) {
  return name.slice(5).replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
}

function parseAttributes(text) {
  const attributes = {};
  const pattern = /([^\s=]+)(?:="([^"]*)")?/g;
  let match;
  while ((match = pattern.exec(text)) !== null) {
    attributes[match[1]] = match[2] === undefined ? true : decodeHtml(match[2]);
  }
  return attributes;
}

function matchesSelector(element, selector) {
  if (!element) return false;
  if (selector.startsWith('#')) return element.id === selector.slice(1);
  if (selector.startsWith('.')) return element.classes.has(selector.slice(1));
  const data = selector.match(/^\[data-([a-z0-9-]+)(?:="([^"]*)")?\]$/);
  if (data) {
    const key = dataKey(`data-${data[1]}`);
    if (!Object.prototype.hasOwnProperty.call(element.dataset, key)) return false;
    return data[2] === undefined || String(element.dataset[key]) === data[2];
  }
  return element.tagName.toLowerCase() === selector.toLowerCase();
}

class FakeElement {
  constructor(tagName = 'div', ownerDocument = null) {
    this.tagName = tagName.toUpperCase();
    this.ownerDocument = ownerDocument;
    this.parentElement = null;
    this.children = [];
    this.dataset = {};
    this.classes = new Set();
    this.listeners = new Map();
    this.id = '';
    this.type = '';
    this.value = '';
    this.checked = false;
    this.disabled = false;
    this.hidden = false;
    this.title = '';
    this.placeholder = '';
    this.textContent = '';
    this.className = '';
    this.files = [];
    this.clickCount = 0;
    this.blurCount = 0;
    this._innerHTML = '';
    this.parseInnerHtml = false;
  }

  appendChild(child) {
    child.parentElement = this;
    this.children.push(child);
  }

  set innerHTML(value) {
    this._innerHTML = String(value);
    if (this.parseInnerHtml && this.ownerDocument) {
      this.ownerDocument.parseInto(this, this._innerHTML);
    }
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
    const event = {
      type,
      target: values.target || this,
      defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; },
      ...values
    };
    const pending = [];
    (this.listeners.get(type) || []).forEach((handler) => {
      const result = handler(event);
      if (result && typeof result.then === 'function') pending.push(result);
    });
    await Promise.all(pending);
    return event;
  }

  click() {
    this.clickCount += 1;
  }

  blur() {
    this.blurCount += 1;
    if (this.ownerDocument && this.ownerDocument.activeElement === this) {
      this.ownerDocument.activeElement = null;
    }
  }

  scrollIntoView() {}

  closest(selector) {
    let current = this;
    while (current) {
      if (matchesSelector(current, selector)) return current;
      current = current.parentElement;
    }
    return null;
  }

  querySelectorAll(selector) {
    const matches = [];
    const visit = (node) => {
      node.children.forEach((child) => {
        if (matchesSelector(child, selector)) matches.push(child);
        visit(child);
      });
    };
    visit(this);
    return matches;
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
}

class FakeDocument {
  constructor() {
    this.elements = new Map();
    this.activeElement = null;
  }

  createStatic(id, tagName = 'div', parseInnerHtml = false) {
    const element = new FakeElement(tagName, this);
    element.id = id;
    element.parseInnerHtml = parseInnerHtml;
    this.elements.set(id, element);
    return element;
  }

  getElementById(id) {
    return this.elements.get(id) || null;
  }

  unregisterTree(root) {
    const visit = (node) => {
      node.children.forEach(visit);
      if (node.id && this.elements.get(node.id) === node) this.elements.delete(node.id);
    };
    root.children.forEach(visit);
  }

  parseInto(root, html) {
    this.unregisterTree(root);
    root.children = [];
    const stack = [root];
    const tokens = String(html).match(/<[^>]+>|[^<]+/g) || [];
    const voidTags = new Set(['input', 'br', 'hr', 'img', 'meta', 'link']);
    tokens.forEach((token) => {
      if (!token.startsWith('<')) {
        const current = stack[stack.length - 1];
        if (current && current.tagName === 'TEXTAREA') current.value += decodeHtml(token);
        else if (current && current.tagName === 'OPTION') current.textContent += decodeHtml(token).trim();
        return;
      }
      if (/^<\//.test(token)) {
        const closed = stack.pop();
        if (closed && closed.tagName === 'SELECT') {
          const options = closed.children.filter((child) => child.tagName === 'OPTION');
          const selected = options.find((option) => option.selected) || options[0];
          closed.value = selected ? selected.value : '';
        }
        return;
      }
      if (/^<!/.test(token)) return;
      const opening = token.match(/^<([a-zA-Z0-9]+)\s*([^>]*)>$/);
      if (!opening) return;
      const tagName = opening[1].toLowerCase();
      const rawAttributes = opening[2].replace(/\/$/, '').trim();
      const attributes = parseAttributes(rawAttributes);
      const element = new FakeElement(tagName, this);
      Object.entries(attributes).forEach(([name, value]) => {
        if (name === 'id') element.id = String(value);
        else if (name === 'class') {
          element.className = String(value);
          String(value).split(/\s+/).filter(Boolean).forEach((item) => element.classes.add(item));
        } else if (name.startsWith('data-')) element.dataset[dataKey(name)] = String(value);
        else if (name === 'type') element.type = String(value);
        else if (name === 'value') element.value = String(value);
        else if (name === 'checked') element.checked = true;
        else if (name === 'selected') element.selected = true;
        else if (name === 'placeholder') element.placeholder = String(value);
        else if (name === 'hidden') element.hidden = true;
      });
      stack[stack.length - 1].appendChild(element);
      if (element.id) this.elements.set(element.id, element);
      if (!voidTags.has(tagName) && !/\/$/.test(opening[2])) stack.push(element);
    });
  }
}

function controlItemHtml(item) {
  return `<div class="control-item ${escapeHtml(item.level || '')}"><span>${escapeHtml(item.section || '')}</span><strong>${escapeHtml(item.label || '')}</strong></div>`;
}

const pages = [{
  id: 'layout-a',
  label: 'Layout A',
  kind: 'match',
  teams: [1, 2],
  teamSize: 1,
  groups: [[1, 2]]
}];

const teamLayouts = {
  configurableLayoutIds: ['layout-a'],
  teamOrderForPage(_page, orders) {
    return (orders && orders['layout-a']) || [1, 2];
  },
  defaultTeamOrder() {
    return [1, 2];
  }
};

function configFixture(overrides = {}) {
  return {
    event: { name: 'Finals', default_layout: 'layout-a', team_orders: { 'layout-a': [2, 1] } },
    networks: { player_vlan: 40, wireless_vlan: 41 },
    devices: {
      core: { ip: '192.168.10.254' },
      firewall: { ip: '192.168.9.1' },
      stage_switches: [{ name: 'stage-a', ip: '192.168.10.11' }],
      access_switches: [{ name: 'access-a', ip: '192.168.10.21' }],
      servers: [{ name: 'server-a', ip: '192.168.41.10', hidden: 'drop' }]
    },
    isp: {
      auto_discovery: true,
      links: [{ name: 'WAN1', bandwidth_mbps: '1000/100', ping: '1.1.1.1', hidden: 'drop' }]
    },
    ...overrides
  };
}

function createHarness(options = {}) {
  const document = new FakeDocument();
  const form = document.createStatic('controlConfigForm', 'div', true);
  const result = document.createStatic('controlConfigResult');
  const actionIds = [
    'controlConfigValidate',
    'controlConfigSave',
    'controlConfigApply',
    'controlConfigRollback',
    'controlConfigImport'
  ];
  actionIds.forEach((id) => document.createStatic(id, 'button'));
  document.createStatic('controlConfigImportFile', 'input');
  const calls = [];
  const refreshCalls = [];
  const confirmCalls = [];
  const browserWindow = {
    location: { hash: '' },
    requestAnimationFrame(callback) { callback(); },
    confirm() { confirmCalls.push(true); return true; }
  };
  const postPlatform = async (path, payload, requestOptions) => {
    calls.push({ type: 'post', path, payload, options: requestOptions });
    if (options.postPlatform) return options.postPlatform(path, payload, requestOptions);
    const config = payload && payload.text ? JSON.parse(payload.text) : configFixture();
    return { ok: true, config, issues: [], applied: path === '/config/apply' };
  };
  const saveDhcpSettings = async (payload) => {
    calls.push({ type: 'dhcp-save', payload });
    if (options.saveDhcpSettings) return options.saveDhcpSettings(payload);
    return { ok: true, username: payload.username, port: Number(payload.port), passwordConfigured: true, enablePasswordConfigured: true };
  };
  const testDhcpConnection = async () => {
    calls.push({ type: 'dhcp-test' });
    if (options.testDhcpConnection) return options.testDhcpConnection();
    return { ok: true, privileged: true, message: '连接成功', host: '192.168.10.254', port: 23 };
  };
  const fetchPlatformConfig = options.fetchPlatformConfig || (async () => ({ ok: true, config: configFixture() }));
  const fetchApplyStatus = options.fetchApplyStatus || (async () => ({ ok: true, state: 'succeeded' }));
  const waitForApplyRecovery = options.waitForApplyRecovery || (async () => ({ outcome: 'succeeded', config: { ok: true, config: configFixture() }, status: { ok: true, applied: true } }));
  const applyRecoveryRenderPayload = options.applyRecoveryRenderPayload || ((recovery, action) => ({
    ...(recovery.status || {}),
    ok: recovery.outcome !== 'failed',
    action,
    applied: recovery.outcome === 'succeeded'
  }));
  const editor = configEditorModule.createConfigEditor({
    document,
    window: browserWindow,
    HTMLInputElement: FakeElement,
    pages,
    teamLayouts,
    escapeHtml,
    controlItemHtml,
    model: configModel,
    fetchPlatformConfig,
    fetchApplyStatus,
    postPlatform,
    saveDhcpSettings,
    testDhcpConnection,
    waitForApplyRecovery,
    applyRecoveryRenderPayload,
    applyRequestTimeoutMs: 180000,
    onRefresh: () => refreshCalls.push(true)
  });
  return {
    document,
    form,
    result,
    editor,
    calls,
    refreshCalls,
    confirmCalls,
    browserWindow
  };
}

function byPath(harness, path) {
  return harness.form.querySelector(`[data-config-path="${path}"]`);
}

function listRows(harness, name) {
  const list = harness.form.querySelector(`[data-config-list="${name}"]`);
  return list ? list.querySelectorAll('.config-list-row') : [];
}

function configPosts(harness, path) {
  return harness.calls.filter((call) => call.type === 'post' && (!path || call.path === path));
}

function parsedPayload(call) {
  return JSON.parse(call.payload.text);
}

async function flushAsync() {
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function platformPayload(config = configFixture(), overrides = {}) {
  return { ok: true, config, issues: [], ...overrides };
}

function dhcpPayload(overrides = {}) {
  return {
    ok: true,
    username: 'admin',
    port: 23,
    passwordConfigured: true,
    enablePasswordConfigured: false,
    ...overrides
  };
}

async function clickAction(harness, id) {
  await harness.document.getElementById(id).dispatch('click');
}

async function testRenderAndBinding() {
  const harness = createHarness();
  assert.deepStrictEqual(
    Object.keys(harness.editor),
    ['bind', 'render', 'isApplyInProgress'],
    'the controller facade stays small and does not expose draft or DOM internals'
  );
  harness.editor.bind();
  harness.editor.bind();
  assert.strictEqual(harness.form.listenerCount('input'), 1, 'bind is idempotent for form input');
  assert.strictEqual(harness.form.listenerCount('change'), 1, 'bind is idempotent for form change');
  assert.strictEqual(harness.form.listenerCount('wheel'), 1, 'bind is idempotent for wheel protection');
  assert.strictEqual(harness.form.listenerCount('click'), 1, 'bind is idempotent for delegated actions');
  ['controlConfigValidate', 'controlConfigSave', 'controlConfigApply', 'controlConfigRollback', 'controlConfigImport']
    .forEach((id) => assert.strictEqual(harness.document.getElementById(id).listenerCount('click'), 1));
  assert.strictEqual(harness.document.getElementById('controlConfigImportFile').listenerCount('change'), 1);

  harness.editor.render(platformPayload(), dhcpPayload());
  harness.editor.render(platformPayload(), dhcpPayload());
  assert.strictEqual(harness.form.listenerCount('input'), 1, 'repeated render does not rebind form listeners');
  assert.strictEqual(harness.document.getElementById('controlConfigSave').listenerCount('click'), 1, 'repeated render does not rebind action listeners');
  assert.strictEqual(byPath(harness, 'event.name').value, 'Finals');
  assert.strictEqual(byPath(harness, 'networks.player_vlan').value, '40');
  assert.strictEqual(listRows(harness, 'stage_switches').length, 1);
  assert.strictEqual(listRows(harness, 'servers').length, 1);
  assert.strictEqual(listRows(harness, 'isp').length, 1);
  assert.deepStrictEqual(
    harness.form.querySelectorAll('[data-team-order-slot]').map((item) => Number(item.value)),
    [2, 1],
    'team-order form reflects the configured layout ordering'
  );
  assert.strictEqual(harness.document.getElementById('controlDhcpUsername').value, 'admin');
  assert.strictEqual(harness.document.getElementById('controlDhcpPort').value, '23');
  assert.match(harness.document.getElementById('controlDhcpPassword').placeholder, /已保存/);
  assert.match(harness.document.getElementById('controlDhcpSavedState').textContent, /登录密码已保存/);

  const empty = createHarness();
  empty.editor.render(platformPayload({}), dhcpPayload({ username: '' }));
  assert.ok(byPath(empty, 'event.name'), 'missing config still renders normalized scalar controls');
  assert.ok(byPath(empty, 'event.default_layout'), 'missing config receives model defaults');
  assert.ok(empty.document.getElementById('controlDhcpSettingsForm'), 'DHCP/Telnet editor remains in the form');

  const escaped = createHarness();
  escaped.editor.render(platformPayload(configFixture({
    event: { name: '<Finals & "Guests">', default_layout: 'layout-a', team_orders: { 'layout-a': [1, 2] } }
  })), dhcpPayload());
  assert.ok(!escaped.form.innerHTML.includes('value="<Finals'), 'scalar values are HTML escaped');
  assert.ok(escaped.form.innerHTML.includes('&lt;Finals &amp; &quot;Guests&quot;&gt;'));
}

async function testDirtyLifecycle() {
  const harness = createHarness();
  harness.editor.bind();
  harness.editor.render(platformPayload(), dhcpPayload());
  const eventName = byPath(harness, 'event.name');
  eventName.value = 'Unsaved Draft';
  await harness.form.dispatch('input', { target: eventName });
  assert.strictEqual(harness.form.dataset.dirty, '1');
  harness.editor.render(platformPayload(configFixture({
    event: { name: 'Server Refresh', default_layout: 'layout-a', team_orders: { 'layout-a': [1, 2] } }
  })), dhcpPayload());
  assert.strictEqual(byPath(harness, 'event.name').value, 'Unsaved Draft', 'periodic render does not overwrite an ordinary dirty draft');

  const telnet = createHarness();
  telnet.editor.bind();
  telnet.editor.render(platformPayload(), dhcpPayload());
  const password = telnet.document.getElementById('controlDhcpPassword');
  password.value = 'private-draft';
  await telnet.form.dispatch('input', { target: password });
  assert.strictEqual(telnet.form.dataset.telnetDirty, '1');
  assert.strictEqual(telnet.form.dataset.dirty, undefined, 'Telnet fields use an independent dirty flag');
  telnet.editor.render(platformPayload(configFixture({
    event: { name: 'New Snapshot', default_layout: 'layout-a', team_orders: { 'layout-a': [1, 2] } }
  })), dhcpPayload({ username: 'new-admin' }));
  assert.strictEqual(telnet.document.getElementById('controlDhcpPassword').value, 'private-draft');
  assert.strictEqual(telnet.document.getElementById('controlDhcpUsername').value, 'admin', 'Telnet dirty refresh preserves the existing private form draft');

  const numberInput = byPath(harness, 'networks.player_vlan');
  harness.document.activeElement = numberInput;
  await harness.form.dispatch('wheel', { target: numberInput });
  assert.strictEqual(numberInput.blurCount, 1, 'mouse wheel blurs focused number inputs');
}

async function testConfigActionsAndStickyResult() {
  const validate = createHarness();
  validate.editor.bind();
  validate.editor.render(platformPayload(), dhcpPayload());
  const input = byPath(validate, 'event.name');
  input.value = 'Validated Draft';
  await validate.form.dispatch('input', { target: input });
  await clickAction(validate, 'controlConfigValidate');
  assert.strictEqual(configPosts(validate, '/config/validate').length, 1);
  assert.strictEqual(validate.form.dataset.dirty, '1', 'validate preserves the draft dirty flag');
  assert.match(validate.result.innerHTML, /验证通过/);
  const sticky = validate.result.innerHTML;
  validate.editor.render(platformPayload(configFixture({
    event: { name: 'Background Value', default_layout: 'layout-a', team_orders: { 'layout-a': [1, 2] } }
  })), dhcpPayload());
  assert.strictEqual(validate.result.innerHTML, sticky, 'configResultSticky is not reset by periodic rendering');

  const save = createHarness();
  save.editor.bind();
  save.editor.render(platformPayload(), dhcpPayload());
  byPath(save, 'event.name').value = 'Saved Value';
  save.form.dataset.dirty = '1';
  await clickAction(save, 'controlConfigSave');
  assert.strictEqual(save.form.dataset.dirty, undefined, 'successful save clears ordinary dirty');
  assert.strictEqual(save.refreshCalls.length, 1);
  assert.match(save.result.innerHTML, /已保存/);

  const failed = createHarness({ postPlatform: async () => { throw new Error('save unavailable'); } });
  failed.editor.bind();
  failed.editor.render(platformPayload(), dhcpPayload());
  byPath(failed, 'event.name').value = 'Failed Save Draft';
  failed.form.dataset.dirty = '1';
  await clickAction(failed, 'controlConfigSave');
  assert.strictEqual(failed.form.dataset.dirty, '1', 'failed save keeps the draft dirty');
  assert.strictEqual(byPath(failed, 'event.name').value, 'Failed Save Draft', 'failed save keeps the DOM draft');
  assert.match(failed.result.innerHTML, /save unavailable/);

  const schema = createHarness();
  schema.editor.bind();
  schema.editor.render(platformPayload(configFixture(), { configTooNew: true }), dhcpPayload());
  assert.strictEqual(schema.document.getElementById('controlConfigValidate').disabled, false);
  ['controlConfigSave', 'controlConfigApply', 'controlConfigRollback', 'controlConfigImport']
    .forEach((id) => assert.strictEqual(schema.document.getElementById(id).disabled, true, `${id} is blocked for a newer schema`));
}

async function testApplyRollbackAndRecovery() {
  const pendingApply = deferred();
  const apply = createHarness({ postPlatform: (_path) => pendingApply.promise });
  apply.editor.bind();
  apply.editor.render(platformPayload(), dhcpPayload());
  const applyClick = apply.document.getElementById('controlConfigApply').dispatch('click');
  await flushAsync();
  assert.strictEqual(apply.editor.isApplyInProgress(), true, 'apply protects the page refresh while its request is pending');
  assert.strictEqual(apply.document.getElementById('controlConfigValidate').disabled, true);
  pendingApply.resolve(platformPayload(configFixture(), { applied: true }));
  await applyClick;
  assert.strictEqual(apply.editor.isApplyInProgress(), false);
  assert.strictEqual(apply.refreshCalls.length, 1);
  assert.strictEqual(configPosts(apply, '/config/apply')[0].options.timeoutMs, 180000);
  assert.ok(configPosts(apply, '/config/apply')[0].payload.operationId);

  const pendingRollback = deferred();
  const rollback = createHarness({ postPlatform: () => pendingRollback.promise });
  rollback.editor.bind();
  rollback.editor.render(platformPayload(), dhcpPayload());
  const rollbackClick = rollback.document.getElementById('controlConfigRollback').dispatch('click');
  await flushAsync();
  assert.strictEqual(rollback.editor.isApplyInProgress(), false, 'rollback preserves its existing applyInProgress timing difference');
  pendingRollback.resolve(platformPayload(configFixture(), { applied: true }));
  await rollbackClick;
  assert.strictEqual(rollback.refreshCalls.length, 1);
  assert.ok(configPosts(rollback, '/config/rollback')[0].payload.operationId);
  assert.strictEqual(rollback.confirmCalls.length, 0, 'apply and rollback do not introduce confirmation prompts');

  for (const outcome of ['succeeded', 'pending', 'failed', 'running', 'unknown']) {
    const waits = [];
    const recoveredConfig = platformPayload(configFixture({
      event: { name: `Recovered ${outcome}`, default_layout: 'layout-a', team_orders: { 'layout-a': [1, 2] } }
    }));
    const recovery = createHarness({
      postPlatform: async () => { throw new Error('service restarting'); },
      waitForApplyRecovery: async (operationId, dependencies) => {
        waits.push({ operationId, dependencies });
        return { outcome, config: recoveredConfig, status: { ok: outcome !== 'failed', applied: outcome === 'succeeded' } };
      },
      applyRecoveryRenderPayload: (value, action) => ({
        ok: value.outcome !== 'failed' && value.outcome !== 'unknown',
        error: value.outcome === 'failed' || value.outcome === 'unknown' ? `recovery ${value.outcome}` : '',
        pending: value.outcome === 'running' || value.outcome === 'pending',
        pendingLabel: `recovery ${value.outcome}`,
        action,
        applied: value.outcome === 'succeeded'
      })
    });
    recovery.editor.bind();
    recovery.editor.render(platformPayload(), dhcpPayload());
    recovery.form.dataset.dirty = '1';
    await clickAction(recovery, 'controlConfigApply');
    assert.strictEqual(waits.length, 1, `${outcome} polls recovery exactly once through the injected helper`);
    assert.ok(waits[0].operationId.startsWith('web-apply-'));
    assert.strictEqual(typeof waits[0].dependencies.fetchConfig, 'function');
    assert.strictEqual(typeof waits[0].dependencies.fetchStatus, 'function');
    const terminal = ['succeeded', 'pending', 'failed'].includes(outcome);
    assert.strictEqual(recovery.refreshCalls.length, terminal ? 1 : 0, `${outcome} preserves refresh semantics`);
    assert.strictEqual(recovery.form.dataset.dirty, terminal ? undefined : '1', `${outcome} preserves draft recovery semantics`);
    assert.strictEqual(recovery.editor.isApplyInProgress(), false);
  }
}

async function testImport() {
  const zip = createHarness();
  zip.editor.bind();
  zip.editor.render(platformPayload(), dhcpPayload());
  const zipInput = zip.document.getElementById('controlConfigImportFile');
  zipInput.files = [{ name: 'event-config.zip', text: async () => 'PKbinary' }];
  await zipInput.dispatch('change');
  assert.strictEqual(configPosts(zip).length, 0);
  assert.match(zip.result.innerHTML, /不支持导入压缩包/);

  const importedConfig = configFixture({
    event: { name: 'Imported', default_layout: 'layout-a', team_orders: { 'layout-a': [1, 2] } }
  });
  const imported = createHarness({ postPlatform: async (path) => {
    assert.strictEqual(path, '/config/validate');
    return platformPayload(importedConfig);
  } });
  imported.editor.bind();
  imported.editor.render(platformPayload(), dhcpPayload());
  const importedInput = imported.document.getElementById('controlConfigImportFile');
  importedInput.files = [{ name: 'event-config.yml', text: async () => 'event: imported' }];
  await importedInput.dispatch('change');
  assert.strictEqual(byPath(imported, 'event.name').value, 'Imported');
  assert.strictEqual(imported.form.dataset.dirty, '1');
  assert.deepStrictEqual(configPosts(imported).map((call) => call.path), ['/config/validate'], 'import validates but never saves or applies');

  const invalid = createHarness({ postPlatform: async () => ({ ok: false, error: 'invalid yaml' }) });
  invalid.editor.bind();
  invalid.editor.render(platformPayload(), dhcpPayload());
  const invalidInput = invalid.document.getElementById('controlConfigImportFile');
  invalidInput.files = [{ name: 'event-config.yml', text: async () => 'invalid' }];
  await invalidInput.dispatch('change');
  assert.strictEqual(invalid.form.dataset.dirty, undefined);
  assert.match(invalid.result.innerHTML, /invalid yaml/);

  const chooser = createHarness();
  chooser.editor.bind();
  await chooser.document.getElementById('controlConfigImport').dispatch('click');
  assert.strictEqual(chooser.document.getElementById('controlConfigImportFile').clickCount, 1);
}

async function testDynamicListsAndTeamOrder() {
  const harness = createHarness();
  harness.editor.bind();
  harness.editor.render(platformPayload(), dhcpPayload());

  const serverRow = listRows(harness, 'servers')[0];
  serverRow.querySelector('[data-config-key="name"]').value = '  server-trimmed  ';
  serverRow.querySelector('[data-config-key="ip"]').value = '  192.168.41.20  ';
  const ispRow = listRows(harness, 'isp')[0];
  ispRow.querySelector('[data-config-key="name"]').value = '  WAN Trimmed  ';
  ispRow.querySelector('[data-config-key="bandwidth_mbps"]').value = '  500/50  ';
  await clickAction(harness, 'controlConfigSave');
  const payload = parsedPayload(configPosts(harness, '/config/save')[0]);
  assert.deepStrictEqual(payload.devices.servers, [{ name: 'server-trimmed', ip: '192.168.41.20' }], 'server list trims and crops hidden fields');
  assert.deepStrictEqual(
    Object.keys(payload.isp.links[0]).sort(),
    ['bandwidth_mbps', 'name'],
    'ISP list payload retains only UI-exposed fields'
  );
  assert.strictEqual(payload.isp.links[0].name, 'WAN Trimmed');
  assert.strictEqual(payload.isp.links[0].bandwidth_mbps, '500/50');

  const lists = createHarness();
  lists.editor.bind();
  lists.editor.render(platformPayload(), dhcpPayload());
  await lists.form.dispatch('click', { target: lists.form.querySelector('[data-config-add="servers"]') });
  assert.strictEqual(listRows(lists, 'servers').length, 2, 'add preserves input order and appends a row');
  await lists.form.dispatch('click', { target: listRows(lists, 'servers')[0].querySelector('[data-config-remove="servers"]') });
  assert.strictEqual(listRows(lists, 'servers').length, 0, 'empty appended rows are cropped while removing the populated row');

  const range = createHarness();
  range.editor.bind();
  range.editor.render(platformPayload(), dhcpPayload());
  const rangeInput = range.form.querySelector('[data-config-range-input="stage_switches"]');
  rangeInput.value = '192.168.10.11-13';
  await range.form.dispatch('click', { target: range.form.querySelector('[data-config-add-range="stage_switches"]') });
  assert.deepStrictEqual(
    listRows(range, 'stage_switches').map((row) => row.querySelector('[data-config-key="ip"]').value),
    ['192.168.10.11', '192.168.10.12', '192.168.10.13'],
    'range expansion preserves existing entries, deduplicates, and appends in order'
  );

  const teams = createHarness();
  teams.editor.bind();
  teams.editor.render(platformPayload(), dhcpPayload());
  let selectors = teams.form.querySelectorAll('[data-team-order-slot]');
  selectors[0].value = '1';
  await teams.form.dispatch('change', { target: selectors[0] });
  selectors = teams.form.querySelectorAll('[data-team-order-slot]');
  assert.deepStrictEqual(selectors.map((item) => Number(item.value)), [1, 2], 'duplicate team selection swaps with the previous slot');
  await teams.form.dispatch('click', { target: teams.form.querySelector('[data-team-order-reset="layout-a"]') });
  assert.deepStrictEqual(
    teams.form.querySelectorAll('[data-team-order-slot]').map((item) => Number(item.value)),
    [1, 2],
    'team-order reset restores the injected layout default'
  );
}

async function testDhcpPartialSuccess() {
  const success = createHarness();
  success.editor.bind();
  success.editor.render(platformPayload(), dhcpPayload());
  success.form.dataset.dirty = '1';
  success.form.dataset.telnetDirty = '1';
  success.document.getElementById('controlDhcpUsername').value = '  operator  ';
  success.document.getElementById('controlDhcpPassword').value = 'login-secret';
  success.document.getElementById('controlDhcpEnablePassword').value = 'enable-secret';
  success.document.getElementById('controlDhcpPort').value = '2323';
  await success.form.dispatch('click', { target: success.document.getElementById('controlDhcpSaveTest') });
  await flushAsync();
  assert.deepStrictEqual(success.calls.map((call) => call.type), ['post', 'dhcp-save', 'dhcp-test']);
  assert.deepStrictEqual(success.calls[1].payload, {
    username: 'operator', password: 'login-secret', enablePassword: 'enable-secret', port: '2323'
  });
  assert.strictEqual(success.form.dataset.dirty, undefined, 'successful base-config save clears ordinary dirty');
  assert.strictEqual(success.form.dataset.telnetDirty, undefined, 'successful Telnet credential save clears telnetDirty');
  assert.match(success.document.getElementById('controlDhcpSettingsResult').textContent, /核心 IP 和 Telnet 信息已保存/);

  const testFailure = createHarness({ testDhcpConnection: async () => { throw new Error('connection refused'); } });
  testFailure.editor.bind();
  testFailure.editor.render(platformPayload(), dhcpPayload());
  await testFailure.form.dispatch('click', { target: testFailure.document.getElementById('controlDhcpSaveTest') });
  await flushAsync();
  assert.match(testFailure.document.getElementById('controlDhcpSettingsResult').textContent, /配置已保存，但连接测试失败/);

  const configFailure = createHarness({ postPlatform: async () => ({ ok: false, error: 'base invalid' }) });
  configFailure.editor.bind();
  configFailure.editor.render(platformPayload(), dhcpPayload());
  await configFailure.form.dispatch('click', { target: configFailure.document.getElementById('controlDhcpSaveTest') });
  await flushAsync();
  assert.deepStrictEqual(configFailure.calls.map((call) => call.type), ['post']);
  assert.match(configFailure.document.getElementById('controlDhcpSettingsResult').textContent, /保存失败/);
}

(async () => {
  await testRenderAndBinding();
  await testDirtyLifecycle();
  await testConfigActionsAndStickyResult();
  await testApplyRollbackAndRecovery();
  await testImport();
  await testDynamicListsAndTeamOrder();
  await testDhcpPartialSuccess();
  console.log('Bigscreen config editor tests: PASS');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
