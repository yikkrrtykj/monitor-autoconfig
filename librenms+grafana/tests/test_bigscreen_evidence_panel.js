const assert = require('assert');
const evidencePanelModule = require('../bigscreen/evidence/evidence-panel.js');
const utils = require('../bigscreen/utils.js');

assert.deepStrictEqual(
  Object.keys(evidencePanelModule),
  ['createEvidencePanel'],
  'the Evidence panel exposes only its dependency-injected controller factory'
);

class FakeElement {
  constructor(id = '') {
    this.id = id;
    this.dataset = {};
    this.listeners = new Map();
    this.value = '';
    this.innerHTML = '';
    this.href = '';
    this.download = '';
    this.clicked = false;
    this.removed = false;
  }

  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(handler);
  }

  listenerCount(type) {
    return (this.listeners.get(type) || []).length;
  }

  dispatch(type, target = this) {
    const event = {
      type,
      target,
      defaultPrevented: false,
      preventDefault() {
        this.defaultPrevented = true;
      }
    };
    (this.listeners.get(type) || []).forEach((handler) => handler(event));
    return event;
  }

  click() {
    this.clicked = true;
  }

  remove() {
    this.removed = true;
  }
}

class FakeDocument {
  constructor() {
    this.elements = new Map();
    this.createdLinks = [];
    [
      'evidenceForm',
      'evidenceTeam',
      'evidenceSeat',
      'evidenceNetwork',
      'evidenceIp',
      'evidenceAt',
      'evidenceWindow',
      'evidenceExport',
      'evidenceSummary',
      'evidenceLatencyChart',
      'evidenceSuccessChart'
    ].forEach((id) => this.elements.set(id, new FakeElement(id)));
    this.getElementById('evidenceTeam').value = '1';
    this.getElementById('evidenceSeat').value = '1';
    this.getElementById('evidenceNetwork').value = 'wired';
    this.getElementById('evidenceWindow').value = '5';
    this.body = {
      appendChild: (element) => {
        this.createdLinks.push(element);
      }
    };
  }

  getElementById(id) {
    return this.elements.get(id) || null;
  }

  createElement(tagName) {
    assert.strictEqual(tagName, 'a');
    return new FakeElement('download-link');
  }
}

class FakeBlob {
  constructor(parts, options) {
    this.parts = parts;
    this.options = options;
  }
}

class FixedDate extends Date {
  constructor(value) {
    super(arguments.length ? value : '2024-01-01T00:00:00Z');
  }

  static now() {
    return new Date('2024-01-01T00:00:00Z').getTime();
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

function latencySeries(name = 'latency-a', values = [{ t: 100, v: 0.002 }]) {
  return [{ name, values }];
}

function successSeries(name = 'success-a', values = [{ t: 100, v: 1 }]) {
  return [{ name, values }];
}

function createHarness(options = {}) {
  const document = new FakeDocument();
  const location = { pathname: '/latency', search: options.search || '' };
  const replacements = [];
  const timers = [];
  const window = {
    location,
    history: {
      replaceState(state, title, value) {
        replacements.push({ state, title, value });
        const parsed = new URL(value, 'http://bigscreen.local');
        location.pathname = parsed.pathname;
        location.search = parsed.search;
      }
    },
    setTimeout(handler, delay) {
      timers.push({ handler, delay });
      return timers.length;
    }
  };
  const instantCalls = [];
  const rangeCalls = [];
  const renderCalls = [];
  const noDataCalls = [];
  const errors = [];
  const blobs = [];
  const revokedUrls = [];
  const instantQueue = [...(options.instantQueue || [])];
  const rangeFor = options.rangeFor || ((query) => (
    query.startsWith('probe_icmp_duration_seconds')
      ? latencySeries()
      : successSeries()
  ));
  const urlApi = {
    createObjectURL(blob) {
      blobs.push(blob);
      return 'blob:evidence-test';
    },
    revokeObjectURL(url) {
      revokedUrls.push(url);
    }
  };
  const panel = evidencePanelModule.createEvidencePanel({
    document,
    window,
    Blob: FakeBlob,
    URL: urlApi,
    URLSearchParams,
    Date: FixedDate,
    console: { error: (error) => errors.push(error) },
    escapeLabel: utils.escapeLabel,
    escapeRegex: utils.escapeRegex,
    networkLabel: utils.networkLabel,
    formatTime: utils.formatTime,
    formatTimestampFull: utils.formatTimestampFull,
    buildCsv: utils.buildCsv,
    dateTimeInputValue: () => '2024-01-01T08:00',
    playerLabel: (team, seat, network) => `Team ${team} S${seat} ${network}`,
    renderNoData(element, message) {
      const value = message || '暂无数据';
      element.innerHTML = `<div class="no-data">${value}</div>`;
      noDataCalls.push({ id: element.id, message: value });
    },
    prometheusInstant(query) {
      instantCalls.push(query);
      if (instantQueue.length) {
        const value = instantQueue.shift();
        return typeof value === 'function' ? value(query) : value;
      }
      return Promise.resolve(options.instantItems || [
        { metric: { instance: '10.0.0.2' } }
      ]);
    },
    prometheusRangeFor(query, queryWindow, nameFormatter) {
      rangeCalls.push({ query, queryWindow: { ...queryWindow }, nameFormatter });
      try {
        return Promise.resolve(rangeFor(query, queryWindow, nameFormatter, rangeCalls.length - 1));
      } catch (error) {
        return Promise.reject(error);
      }
    },
    renderEvidenceCharts(input) {
      renderCalls.push(input);
    }
  });
  return {
    panel,
    document,
    window,
    replacements,
    timers,
    instantCalls,
    rangeCalls,
    renderCalls,
    noDataCalls,
    errors,
    blobs,
    revokedUrls
  };
}

(async () => {
  const normalLatency = latencySeries('latency-current', [{ t: 100, v: 0.002 }, { t: 101, v: 0.003 }]);
  const normalSuccess = successSeries('success-current', [{ t: 100, v: 1 }, { t: 101, v: 1 }]);
  const initial = createHarness({
    search: '?team=2&seat=3&network=wireless&range=10&at=2023-12-31T16%3A00',
    instantItems: [
      { metric: { target_ip: '10.0.0.10' } },
      { metric: { instance: '10.0.0.2' } },
      { metric: { instance: '10.0.0.2' } }
    ],
    rangeFor: (query) => query.startsWith('probe_icmp_duration_seconds') ? normalLatency : normalSuccess
  });

  await initial.panel.start();

  assert.strictEqual(initial.document.getElementById('evidenceTeam').value, '2');
  assert.strictEqual(initial.document.getElementById('evidenceSeat').value, '3');
  assert.strictEqual(initial.document.getElementById('evidenceNetwork').value, 'wireless');
  assert.strictEqual(initial.document.getElementById('evidenceWindow').value, '10');
  assert.strictEqual(initial.document.getElementById('evidenceAt').value, '2023-12-31T16:00');
  assert.strictEqual(initial.document.getElementById('evidenceIp').value, '');
  assert.strictEqual(initial.instantCalls.length, 1, 'seat-based queries resolve the current IP exactly once');
  assert.strictEqual(
    initial.instantCalls[0],
    'probe_success{role="player",team="2",seat="3",network="wireless"}'
  );
  assert.strictEqual(initial.rangeCalls.length, 2, 'latency and success range queries run together');
  assert.ok(initial.rangeCalls[0].query.includes('probe_icmp_duration_seconds'));
  assert.ok(initial.rangeCalls[0].query.includes('instance=~"^(?:10\\\\.0\\\\.0\\\\.2|10\\\\.0\\\\.0\\\\.10)$"'));
  assert.ok(initial.rangeCalls[0].query.includes('target_ip=~"^(?:10\\\\.0\\\\.0\\\\.2|10\\\\.0\\\\.0\\\\.10)$"'));
  assert.ok(initial.rangeCalls[1].query.includes('probe_success'));
  assert.strictEqual(initial.rangeCalls[0].queryWindow.step, 1);
  assert.strictEqual(initial.rangeCalls[0].queryWindow.end - initial.rangeCalls[0].queryWindow.start, 600);
  assert.strictEqual(
    initial.rangeCalls[0].nameFormatter({ seat: '3', instance: '10.0.0.2', network: 'wireless' }),
    'S3 10.0.0.2 无线'
  );
  assert.strictEqual(initial.renderCalls.length, 1);
  assert.strictEqual(initial.renderCalls[0].latencySeries, normalLatency);
  assert.strictEqual(initial.renderCalls[0].successSeries, normalSuccess);
  assert.deepStrictEqual(
    initial.renderCalls[0],
    {
      summaryContainerId: 'evidenceSummary',
      latencyContainerId: 'evidenceLatencyChart',
      successContainerId: 'evidenceSuccessChart',
      context: { label: initial.renderCalls[0].context.label },
      latencySeries: normalLatency,
      successSeries: normalSuccess
    },
    'the panel preserves the complete chart facade call shape'
  );
  assert.ok(initial.renderCalls[0].context.label.includes('Team 2 S3 wireless · 当前 IP 10.0.0.2、10.0.0.10'));
  assert.deepStrictEqual(
    initial.noDataCalls.slice(0, 2),
    [
      { id: 'evidenceLatencyChart', message: '加载中' },
      { id: 'evidenceSuccessChart', message: '加载中' }
    ],
    'both charts enter the existing loading state before querying'
  );
  assert.ok(initial.replacements[0].value.startsWith('/latency?team=2&seat=3&network=wireless&range=10'));

  const form = initial.document.getElementById('evidenceForm');
  const exportButton = initial.document.getElementById('evidenceExport');
  await initial.panel.start();
  assert.strictEqual(form.listenerCount('submit'), 1, 'repeated start does not duplicate submit listeners');
  assert.strictEqual(form.listenerCount('change'), 1, 'repeated start does not duplicate change listeners');
  assert.strictEqual(exportButton.listenerCount('click'), 1, 'repeated start does not duplicate export listeners');

  initial.document.getElementById('evidenceTeam').value = '4';
  initial.document.getElementById('evidenceSeat').value = '5';
  initial.document.getElementById('evidenceNetwork').value = 'all';
  initial.document.getElementById('evidenceWindow').value = '5';
  initial.document.getElementById('evidenceIp').value = ' 203.0.113.8 ';
  const submitEvent = form.dispatch('submit');
  await settle();
  assert.strictEqual(submitEvent.defaultPrevented, true);
  assert.ok(initial.window.location.search.includes('team=4'));
  assert.ok(initial.window.location.search.includes('seat=5'));
  assert.ok(initial.window.location.search.includes('network=all'));
  assert.ok(initial.window.location.search.includes('ip=203.0.113.8'));
  assert.ok(initial.rangeCalls.at(-2).query.includes('instance="203.0.113.8"'));
  assert.ok(initial.rangeCalls.at(-2).query.includes('target_ip="203.0.113.8"'));

  initial.document.getElementById('evidenceIp').value = '198.51.100.9';
  form.dispatch('change', initial.document.getElementById('evidenceTeam'));
  await settle();
  assert.strictEqual(initial.document.getElementById('evidenceIp').value, '', 'seat identity changes clear a manual IP');

  const missing = createHarness({
    search: '?team=&seat=&network=bogus&window=15&at=&ip=',
    instantItems: []
  });
  await missing.panel.start();
  assert.strictEqual(missing.document.getElementById('evidenceTeam').value, '1');
  assert.strictEqual(missing.document.getElementById('evidenceSeat').value, '1');
  assert.strictEqual(missing.document.getElementById('evidenceNetwork').value, 'wired');
  assert.strictEqual(missing.document.getElementById('evidenceWindow').value, '15', 'legacy window URL input is retained');
  assert.strictEqual(missing.document.getElementById('evidenceAt').value, '2024-01-01T08:00');
  assert.strictEqual(missing.renderCalls.length, 0);
  assert.ok(missing.document.getElementById('evidenceSummary').innerHTML.includes('当前没有可查询的 IP'));
  assert.ok(missing.document.getElementById('evidenceLatencyChart').innerHTML.includes('当前座位未生成监控目标'));
  assert.ok(missing.document.getElementById('evidenceSuccessChart').innerHTML.includes('当前座位未生成监控目标'));

  const emptyRange = createHarness({
    search: '?ip=192.0.2.1',
    rangeFor: () => []
  });
  await emptyRange.panel.start();
  assert.strictEqual(emptyRange.renderCalls.length, 1, 'empty range results still reach the chart facade');
  assert.deepStrictEqual(emptyRange.renderCalls[0].latencySeries, []);
  assert.deepStrictEqual(emptyRange.renderCalls[0].successSeries, []);

  const queryError = new Error('Prometheus unavailable');
  const failed = createHarness({
    search: '?ip=192.0.2.2',
    rangeFor: () => { throw queryError; }
  });
  await failed.panel.start();
  assert.strictEqual(failed.renderCalls.length, 0);
  assert.ok(failed.document.getElementById('evidenceSummary').innerHTML.includes('查询失败'));
  assert.ok(failed.document.getElementById('evidenceLatencyChart').innerHTML.includes('暂无数据'));
  assert.ok(failed.document.getElementById('evidenceSuccessChart').innerHTML.includes('暂无数据'));
  assert.deepStrictEqual(failed.errors, [queryError]);

  const oldInstant = deferred();
  const stale = createHarness({ instantQueue: [oldInstant.promise] });
  const oldQuery = stale.panel.start();
  stale.window.location.search = '?team=8&seat=2&network=wired&range=5&ip=198.51.100.20';
  await stale.panel.start();
  assert.strictEqual(stale.renderCalls.length, 1);
  oldInstant.resolve([{ metric: { instance: '198.51.100.10' } }]);
  await oldQuery;
  assert.strictEqual(stale.renderCalls.length, 1, 'an older IP resolution cannot overwrite a newer query');
  assert.ok(stale.renderCalls[0].context.label.includes('198.51.100.20'));

  const stoppedRange = deferred();
  const stopped = createHarness({
    search: '?ip=192.0.2.3',
    rangeFor: () => stoppedRange.promise
  });
  const stoppedQuery = stopped.panel.start();
  stopped.panel.stop();
  stoppedRange.resolve(latencySeries());
  await stoppedQuery;
  assert.strictEqual(stopped.renderCalls.length, 0, 'stop invalidates in-flight Evidence work');

  const exportLatency = latencySeries('seat,"A"', [
    { t: 100, v: 0.002 },
    { t: 101, v: null }
  ]);
  const exportSuccess = successSeries('seat,"A"', [
    { t: 100, v: 1 },
    { t: 101, v: null }
  ]);
  const csv = createHarness({
    search: '?ip=10.0.0.8&at=2023-12-31T16%3A00&range=5',
    rangeFor: (query) => query.startsWith('probe_icmp_duration_seconds') ? exportLatency : exportSuccess
  });
  await csv.panel.start();
  csv.document.getElementById('evidenceExport').dispatch('click');
  assert.strictEqual(csv.document.createdLinks.length, 1);
  const link = csv.document.createdLinks[0];
  assert.strictEqual(link.clicked, true);
  assert.strictEqual(link.removed, true);
  assert.ok(link.download.startsWith('latency_10.0.0.8_'));
  assert.ok(link.download.endsWith('.csv'));
  assert.strictEqual(link.href, 'blob:evidence-test');
  assert.strictEqual(csv.blobs.length, 1);
  assert.strictEqual(csv.blobs[0].options.type, 'text/csv;charset=utf-8');
  const csvText = csv.blobs[0].parts[0];
  assert.ok(csvText.startsWith('\uFEFFtime,series,metric,value\r\n'));
  assert.ok(csvText.includes('"seat,""A""",latency_ms,2.00'));
  assert.ok(csvText.includes('"seat,""A""",latency_ms,0.00'), 'existing null latency CSV behavior is preserved');
  assert.ok(csvText.includes('"seat,""A""",online,null'), 'existing null success CSV behavior is preserved');
  assert.strictEqual(csv.timers.length, 1);
  assert.strictEqual(csv.timers[0].delay, 1000);
  csv.timers[0].handler();
  assert.deepStrictEqual(csv.revokedUrls, ['blob:evidence-test']);

  console.log('bigscreen Evidence panel tests passed');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
