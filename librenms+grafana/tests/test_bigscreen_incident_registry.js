const assert = require('assert');
const fs = require('fs');
const path = require('path');
const incidentRegistryModule = require('../bigscreen/control/incident-registry.js');

assert.deepStrictEqual(
  Object.keys(incidentRegistryModule),
  ['createIncidentRegistry'],
  'the incident registry exposes only its dependency-injected factory'
);

function escapeHtml(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function decodeAttribute(value) {
  return String(value)
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&gt;/g, '>')
    .replace(/&lt;/g, '<')
    .replace(/&amp;/g, '&');
}

class FakeElement {
  constructor(id) {
    this.id = id;
    this.dataset = {};
    this.listeners = new Map();
    this.value = '';
    this._innerHTML = '';
    this.resolveButtons = [];
  }

  set innerHTML(value) {
    this._innerHTML = String(value);
    this.resolveButtons = [];
    const pattern = /data-resolve-incident="([^"]*)"/g;
    let match;
    while ((match = pattern.exec(this._innerHTML)) !== null) {
      const button = new FakeElement('resolve');
      button.dataset.resolveIncident = decodeAttribute(match[1]);
      this.resolveButtons.push(button);
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

  dispatch(type) {
    const event = { type, target: this };
    const pending = (this.listeners.get(type) || []).map((handler) => handler(event));
    return Promise.all(pending.filter((result) => result && typeof result.then === 'function'));
  }

  querySelectorAll(selector) {
    return selector === '[data-resolve-incident]' ? this.resolveButtons : [];
  }
}

class FakeDocument {
  constructor() {
    this.elements = new Map([
      ['controlIncidentList', new FakeElement('controlIncidentList')],
      ['controlIncidentTitle', new FakeElement('controlIncidentTitle')],
      ['controlIncidentCreate', new FakeElement('controlIncidentCreate')]
    ]);
  }

  getElementById(id) {
    return this.elements.get(id) || null;
  }
}

function sourceValue(source, index, ...args) {
  const value = Array.isArray(source)
    ? source[Math.min(index, source.length - 1)]
    : source;
  if (typeof value === 'function') return value(...args);
  if (value instanceof Error) throw value;
  return value;
}

function createHarness(options = {}) {
  const document = options.document || new FakeDocument();
  const postCalls = [];
  const patchCalls = [];
  const fetchCalls = [];
  const postSource = options.postPlatform === undefined ? { ok: true } : options.postPlatform;
  const patchSource = options.patchPlatform === undefined ? { ok: true } : options.patchPlatform;
  const fetchSource = options.fetchIncidents === undefined ? { incidents: [] } : options.fetchIncidents;
  const controller = incidentRegistryModule.createIncidentRegistry({
    document,
    escapeHtml,
    formatTimestampFull: (timestamp) => `TS:${timestamp}`,
    fetchIncidents() {
      const index = fetchCalls.length;
      fetchCalls.push(index);
      try {
        return Promise.resolve(sourceValue(fetchSource, index, index));
      } catch (error) {
        return Promise.reject(error);
      }
    },
    postPlatform(url, payload) {
      const index = postCalls.length;
      postCalls.push({ url, payload });
      try {
        return Promise.resolve(sourceValue(postSource, index, url, payload));
      } catch (error) {
        return Promise.reject(error);
      }
    },
    patchPlatform(url, payload) {
      const index = patchCalls.length;
      patchCalls.push({ url, payload });
      try {
        return Promise.resolve(sourceValue(patchSource, index, url, payload));
      } catch (error) {
        return Promise.reject(error);
      }
    },
    getControlReport: () => options.controlReport || null,
    now: () => options.now === undefined ? 1700000123456 : options.now
  });
  return { controller, document, postCalls, patchCalls, fetchCalls };
}

function incident(overrides = {}) {
  return {
    id: 1,
    status: 'open',
    title: '链路异常',
    owner: '值班员',
    severity: 'warn',
    startedAt: 1000,
    ...overrides
  };
}

(async () => {
  const empty = createHarness();
  empty.controller.render({ incidents: [] });
  assert.ok(empty.document.getElementById('controlIncidentList').innerHTML.includes('暂无事故记录'));

  const normal = createHarness();
  normal.controller.render({ incidents: [incident()] });
  const normalHtml = normal.document.getElementById('controlIncidentList').innerHTML;
  assert.ok(normalHtml.includes('incident-record warn'));
  assert.ok(normalHtml.includes('#1 · open'));
  assert.ok(normalHtml.includes('链路异常'));
  assert.ok(normalHtml.includes('TS:1000 · 进行中 · 值班员'));
  assert.strictEqual(normal.document.getElementById('controlIncidentList').resolveButtons.length, 1);

  const ordered = createHarness();
  ordered.controller.render({ incidents: Array.from({ length: 13 }, (_, index) => incident({ id: index + 1, title: `事故-${index + 1}` })) });
  const orderedHtml = ordered.document.getElementById('controlIncidentList').innerHTML;
  assert.ok(orderedHtml.indexOf('事故-1') < orderedHtml.indexOf('事故-2'), 'payload order is preserved');
  assert.ok(orderedHtml.includes('事故-12'));
  assert.ok(!orderedHtml.includes('事故-13'), 'only the first 12 incidents render');

  const escaped = createHarness();
  escaped.controller.render({ incidents: [incident({ id: '<7>', status: '<open>', title: '<script>', owner: 'A&B' })] });
  const escapedHtml = escaped.document.getElementById('controlIncidentList').innerHTML;
  assert.ok(escapedHtml.includes('#&lt;7&gt; · &lt;open&gt;'));
  assert.ok(escapedHtml.includes('&lt;script&gt;'));
  assert.ok(escapedHtml.includes('A&amp;B'));
  assert.ok(!escapedHtml.includes('<script>'));

  const resolved = createHarness();
  resolved.controller.render({ incidents: [
    incident({ id: 1, status: 'resolved', recoveredAt: 1121 }),
    incident({ id: 2, status: 'open' }),
    incident({ id: 3, status: 'resolved', startedAt: 1200, recoveredAt: 1000 })
  ] });
  const resolvedHost = resolved.document.getElementById('controlIncidentList');
  assert.strictEqual(resolvedHost.resolveButtons.length, 1, 'resolved records have no action button');
  assert.ok(resolvedHost.innerHTML.includes('2 分钟'));
  assert.ok(resolvedHost.innerHTML.includes('0 分钟'), 'negative duration clamps to zero');

  const payloadError = createHarness();
  payloadError.controller.render({ incidents: [], error: '<registry failed>' });
  assert.strictEqual(
    payloadError.document.getElementById('controlIncidentList').innerHTML,
    '<div class="control-empty bad">&lt;registry failed&gt;</div>'
  );

  const checks = Array.from({ length: 12 }, (_, index) => ({
    id: index,
    level: index === 1 ? 'good' : (index % 2 ? 'warn' : 'bad')
  }));
  const createBad = createHarness({
    controlReport: { readiness: { level: 'bad', score: 20 }, checks },
    fetchIncidents: { incidents: [incident({ id: 20, title: '新事故' })] }
  });
  createBad.document.getElementById('controlIncidentTitle').value = '  核心故障  ';
  createBad.controller.bind();
  await createBad.document.getElementById('controlIncidentCreate').dispatch('click');
  assert.deepStrictEqual(createBad.postCalls, [{
    url: '/incidents',
    payload: {
      title: '核心故障',
      severity: 'bad',
      related: {
        readiness: { level: 'bad', score: 20 },
        checks: checks.filter((item) => item.level === 'bad' || item.level === 'warn').slice(0, 8)
      }
    }
  }]);
  assert.strictEqual(createBad.postCalls[0].payload.related.checks.length, 8);
  assert.strictEqual(createBad.document.getElementById('controlIncidentTitle').value, '');
  assert.strictEqual(createBad.fetchCalls.length, 1);
  assert.ok(createBad.document.getElementById('controlIncidentList').innerHTML.includes('新事故'));

  const createWarn = createHarness({ controlReport: { readiness: { level: 'good' }, checks: [] } });
  createWarn.controller.bind();
  await createWarn.document.getElementById('controlIncidentCreate').dispatch('click');
  assert.strictEqual(createWarn.postCalls[0].payload.title, '现场事故');
  assert.strictEqual(createWarn.postCalls[0].payload.severity, 'warn');

  const noReport = createHarness();
  noReport.controller.bind();
  await noReport.document.getElementById('controlIncidentCreate').dispatch('click');
  assert.deepStrictEqual(noReport.postCalls[0].payload.related, {});
  assert.strictEqual(noReport.postCalls[0].payload.severity, 'warn');

  const createFailure = createHarness({ postPlatform: new Error('create <failed>') });
  createFailure.controller.render({ incidents: [incident({ id: 31 })] });
  createFailure.document.getElementById('controlIncidentTitle').value = '保留标题';
  createFailure.controller.bind();
  await createFailure.document.getElementById('controlIncidentCreate').dispatch('click');
  assert.ok(createFailure.document.getElementById('controlIncidentList').innerHTML.includes('create &lt;failed&gt;'));
  assert.strictEqual(createFailure.document.getElementById('controlIncidentTitle').value, '保留标题');
  assert.strictEqual(createFailure.fetchCalls.length, 0);

  const resolveSuccess = createHarness({
    now: 1700000123456,
    fetchIncidents: { incidents: [incident({ id: 41, status: 'resolved', recoveredAt: 1700000123 })] }
  });
  resolveSuccess.controller.render({ incidents: [incident({ id: 41 })] });
  await resolveSuccess.document.getElementById('controlIncidentList').resolveButtons[0].dispatch('click');
  assert.deepStrictEqual(resolveSuccess.patchCalls, [{
    url: '/incidents/41',
    payload: {
      status: 'resolved',
      recoveredAt: 1700000123,
      event: '标记恢复',
      eventType: 'recovery'
    }
  }]);
  assert.strictEqual(resolveSuccess.fetchCalls.length, 1);
  assert.strictEqual(resolveSuccess.document.getElementById('controlIncidentList').resolveButtons.length, 0);

  const resolveFailure = createHarness({ patchPlatform: new Error('resolve failed') });
  resolveFailure.controller.render({ incidents: [incident({ id: 51 })] });
  const failedResolveButton = resolveFailure.document.getElementById('controlIncidentList').resolveButtons[0];
  await failedResolveButton.dispatch('click');
  assert.ok(resolveFailure.document.getElementById('controlIncidentList').innerHTML.includes('resolve failed'));
  assert.strictEqual(resolveFailure.fetchCalls.length, 0);

  const repeated = createHarness();
  repeated.controller.bind();
  repeated.controller.bind();
  repeated.controller.bind();
  assert.strictEqual(repeated.document.getElementById('controlIncidentCreate').listenerCount('click'), 1);

  const source = fs.readFileSync(path.resolve(__dirname, '../bigscreen/control/incident-registry.js'), 'utf8');
  assert.ok(source.includes('render({ incidents: lastIncidents, error: error.message || "创建事故失败" })'));
  assert.ok(source.includes('render({ incidents: lastIncidents, error: error.message || "更新事故失败" })'));

  console.log('bigscreen incident registry tests passed');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
