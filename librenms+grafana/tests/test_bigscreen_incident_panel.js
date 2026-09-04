const assert = require('assert');
const incidentPanelModule = require('../bigscreen/incident/incident-panel.js');
const utils = require('../bigscreen/utils.js');

assert.deepStrictEqual(
  Object.keys(incidentPanelModule),
  ['createIncidentPanel'],
  'the Incident panel exposes only its dependency-injected controller factory'
);

class FakeElement {
  constructor(id = '') {
    this.id = id;
    this.dataset = {};
    this.listeners = new Map();
    this.value = '';
    this.options = [];
    this.innerHTML = '';
    this.className = '';
  }

  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(handler);
  }

  listenerCount(type) {
    return (this.listeners.get(type) || []).length;
  }

  dispatch(type) {
    const event = {
      type,
      target: this,
      defaultPrevented: false,
      preventDefault() {
        this.defaultPrevented = true;
      }
    };
    (this.listeners.get(type) || []).forEach((handler) => handler(event));
    return event;
  }
}

class FakeDocument {
  constructor() {
    this.elements = new Map();
    [
      'incidentForm',
      'incidentAt',
      'incidentWindow',
      'incidentThreshold',
      'incidentVerdict',
      'incidentPlayers',
      'incidentInfra',
      'incidentIsp',
      'incidentStage'
    ].forEach((id) => this.elements.set(id, new FakeElement(id)));
    const windowSelect = this.getElementById('incidentWindow');
    windowSelect.options = ['2', '5', '10', '15'].map((value) => ({ value }));
    windowSelect.value = '5';
    const thresholdSelect = this.getElementById('incidentThreshold');
    thresholdSelect.options = ['0.02', '0.03', '0.05', '0.08'].map((value) => ({ value }));
    thresholdSelect.value = '0.05';
  }

  getElementById(id) {
    return this.elements.get(id) || null;
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

function emptyResult(overrides = {}) {
  return {
    verdict: { level: 'good', text: '未检测到异常', detail: '没有超过阈值。' },
    affectedPlayers: [],
    offlinePlayers: [],
    infraEvents: [],
    ispEvents: [],
    stageGroups: {},
    ...overrides
  };
}

function populatedResult() {
  const largeStagePlayers = Array.from({ length: 9 }, (_, index) => ({
    team: index === 0 ? '<team>' : String(index + 1),
    seat: String(index + 1)
  }));
  return emptyResult({
    verdict: {
      level: 'warn',
      text: '怀疑 <核心>',
      detail: '详情 & 建议'
    },
    affectedPlayers: [{
      team: '<1>',
      seat: '2',
      network: 'wired',
      maxLatency: 0.06,
      instance: '<10.0.0.2>'
    }],
    offlinePlayers: [{
      team: '2',
      seat: '3',
      network: 'wireless',
      recoveryCount: 2,
      instance: '10.0.0.3'
    }],
    infraEvents: [
      { instance: '<core>', job: 'infra-core-ping', maxLatency: 0.1 },
      { targetIp: '10.0.0.1', job: 'infra-fw-ping', offline: true, recoveryCount: 1 }
    ],
    ispEvents: [
      { ifAlias: 'low', direction: 'out', maxBps: 40, capacityBps: 100, utilization: 0.4 },
      { ifAlias: '<high>', direction: 'in', maxBps: 80, capacityBps: 100, utilization: 0.8 }
    ],
    stageGroups: {
      small: { switch: 'small', players: [{ team: '8', seat: '1' }] },
      large: { switch: '<large>', players: largeStagePlayers }
    }
  });
}

function createHarness(options = {}) {
  const document = new FakeDocument();
  const location = { pathname: '/incident', search: options.search || '' };
  const replacements = [];
  const window = {
    location,
    history: {
      replaceState(state, title, value) {
        replacements.push({ state, title, value });
        const parsed = new URL(value, 'http://bigscreen.local');
        location.pathname = parsed.pathname;
        location.search = parsed.search;
      }
    }
  };
  const rangeCalls = [];
  const analyzeCalls = [];
  const errors = [];
  const ispQueue = [...(options.ispQueue || [])];
  const defaultSeries = {
    playerLatency: [{ metric: { kind: 'player-latency' }, values: [{ t: 1, v: 0.01 }] }],
    playerSuccess: [{ metric: { kind: 'player-success' }, values: [{ t: 1, v: 1 }] }],
    infraLatency: [{ metric: { kind: 'infra-latency' }, values: [{ t: 1, v: 0.002 }] }],
    infraSuccess: [{ metric: { kind: 'infra-success' }, values: [{ t: 1, v: 1 }] }]
  };
  const rangeFor = options.rangeFor || ((query) => {
    if (query.startsWith('probe_icmp_duration_seconds{role="player"')) return defaultSeries.playerLatency;
    if (query.startsWith('probe_success{role="player"')) return defaultSeries.playerSuccess;
    if (query.startsWith('probe_icmp_duration_seconds{job=~')) return defaultSeries.infraLatency;
    if (query.startsWith('probe_success{job=~')) return defaultSeries.infraSuccess;
    return [{ metric: { query }, values: [{ t: 1, v: query.includes('InOctets') ? 80 : 40 }] }];
  });
  const panel = incidentPanelModule.createIncidentPanel({
    document,
    window,
    URLSearchParams,
    Date: FixedDate,
    console: { error: (...args) => errors.push(args) },
    escapeHtml: utils.escapeHtml,
    networkLabel: utils.networkLabel,
    formatPingText: utils.formatPingText,
    formatBits: (value) => `${value}bps`,
    dateTimeInputValue: () => '2024-01-01T08:00',
    fetchIspNames() {
      if (ispQueue.length) {
        const value = ispQueue.shift();
        return typeof value === 'function' ? value() : value;
      }
      return Promise.resolve(options.ispNames || ['ISP-A', 'ISP-B']);
    },
    ispTrafficQuery(metric, name) {
      return `isp:${metric}:${name}`;
    },
    prometheusRangeFor(query, queryWindow) {
      rangeCalls.push({ query, queryWindow: { ...queryWindow } });
      try {
        return Promise.resolve(rangeFor(query, queryWindow, rangeCalls.length - 1));
      } catch (error) {
        return Promise.reject(error);
      }
    },
    analyzeIncident(data, threshold) {
      analyzeCalls.push({ data, threshold });
      return typeof options.result === 'function'
        ? options.result(data, threshold)
        : (options.result || emptyResult());
    }
  });
  return {
    panel,
    document,
    window,
    replacements,
    rangeCalls,
    analyzeCalls,
    errors,
    defaultSeries
  };
}

(async () => {
  const normal = createHarness({
    search: '?at=2023-12-31T16%3A00&window=10&threshold=0.03',
    result: populatedResult()
  });
  const initialQuery = normal.panel.start();

  ['incidentVerdict', 'incidentPlayers', 'incidentInfra', 'incidentIsp', 'incidentStage'].forEach((id) => {
    assert.ok(normal.document.getElementById(id).innerHTML.includes('加载中...'), `${id} enters loading state`);
  });
  await initialQuery;

  assert.strictEqual(normal.document.getElementById('incidentAt').value, '2023-12-31T16:00');
  assert.strictEqual(normal.document.getElementById('incidentWindow').value, '10');
  assert.strictEqual(normal.document.getElementById('incidentThreshold').value, '0.03');
  assert.strictEqual(normal.rangeCalls.length, 8, 'four base queries and two queries for each ISP are preserved');
  const queryTexts = normal.rangeCalls.map((call) => call.query);
  assert.ok(queryTexts.includes('probe_icmp_duration_seconds{role="player",network="wired",phase="rtt"}'));
  assert.ok(queryTexts.includes('probe_success{role="player",network="wired"}'));
  assert.ok(queryTexts.includes('probe_icmp_duration_seconds{job=~"infra-isp-ping|infra-core-ping|infra-dist-ping|infra-fw-ping|infra-srv-ping",phase="rtt"}'));
  assert.ok(queryTexts.includes('probe_success{job=~"infra-isp-ping|infra-core-ping|infra-dist-ping|infra-fw-ping|infra-srv-ping"}'));
  assert.deepStrictEqual(
    queryTexts.filter((query) => query.startsWith('isp:')),
    [
      'isp:ifHCInOctets:ISP-A',
      'isp:ifHCOutOctets:ISP-A',
      'isp:ifHCInOctets:ISP-B',
      'isp:ifHCOutOctets:ISP-B'
    ]
  );
  normal.rangeCalls.forEach((call) => {
    assert.strictEqual(call.queryWindow.step, 5);
    assert.strictEqual(call.queryWindow.minutes, 10);
    assert.strictEqual(call.queryWindow.end - call.queryWindow.start, 1200);
  });
  assert.strictEqual(normal.analyzeCalls.length, 1);
  assert.strictEqual(normal.analyzeCalls[0].threshold, 0.03);
  assert.strictEqual(normal.analyzeCalls[0].data.playerLatency, normal.defaultSeries.playerLatency);
  assert.strictEqual(normal.analyzeCalls[0].data.playerSuccess, normal.defaultSeries.playerSuccess);
  assert.strictEqual(normal.analyzeCalls[0].data.infraLatency, normal.defaultSeries.infraLatency);
  assert.strictEqual(normal.analyzeCalls[0].data.infraSuccess, normal.defaultSeries.infraSuccess);
  assert.strictEqual(normal.analyzeCalls[0].data.isp.length, 4);
  assert.deepStrictEqual(
    normal.analyzeCalls[0].data.isp.map((series) => [series._ispName, series._direction]),
    [
      ['ISP-A', 'in'],
      ['ISP-A', 'out'],
      ['ISP-B', 'in'],
      ['ISP-B', 'out']
    ]
  );
  assert.ok(normal.replacements[0].value.startsWith('/incident?at=2023-12-31T16%3A00&window=10&threshold=0.03'));

  const verdict = normal.document.getElementById('incidentVerdict');
  assert.strictEqual(verdict.className, 'incident-verdict warn');
  assert.ok(verdict.innerHTML.includes('怀疑 &lt;核心&gt;'));
  assert.ok(verdict.innerHTML.includes('详情 &amp; 建议'));
  const players = normal.document.getElementById('incidentPlayers').innerHTML;
  assert.ok(players.includes('Team &lt;1&gt; S2 (有线)'));
  assert.ok(players.includes('&lt;10.0.0.2&gt;'));
  assert.ok(players.indexOf('incident-item warn') < players.indexOf('incident-item bad'));
  const infra = normal.document.getElementById('incidentInfra').innerHTML;
  assert.ok(infra.includes('&lt;core&gt;'));
  assert.ok(infra.includes('1 次断线后恢复'));
  const isp = normal.document.getElementById('incidentIsp').innerHTML;
  assert.ok(isp.includes('&lt;high&gt;'));
  assert.ok(isp.indexOf('&lt;high&gt;') < isp.indexOf('<strong>low</strong>'), 'ISP rows remain sorted by utilization');
  const stages = normal.document.getElementById('incidentStage').innerHTML;
  assert.ok(stages.includes('&lt;large&gt;'));
  assert.ok(stages.includes('9 个选手'));
  assert.ok(stages.includes('T&lt;team&gt;S1'));
  assert.ok(stages.includes('…'));
  assert.ok(stages.indexOf('&lt;large&gt;') < stages.indexOf('<strong>small</strong>'), 'stage rows remain sorted by player count');

  const form = normal.document.getElementById('incidentForm');
  await normal.panel.start();
  assert.strictEqual(form.listenerCount('submit'), 1, 'repeated start does not duplicate form listeners');
  normal.document.getElementById('incidentAt').value = '2023-12-31T15:00';
  normal.document.getElementById('incidentWindow').value = '2';
  normal.document.getElementById('incidentThreshold').value = '0.08';
  const submitEvent = form.dispatch('submit');
  await settle();
  assert.strictEqual(submitEvent.defaultPrevented, true);
  assert.strictEqual(normal.window.location.search, '?at=2023-12-31T15%3A00&window=2&threshold=0.08');
  assert.strictEqual(normal.analyzeCalls.at(-1).threshold, 0.08);

  const missing = createHarness({
    search: '?window=999&threshold=invalid&at=',
    ispNames: [],
    result: emptyResult()
  });
  await missing.panel.start();
  assert.strictEqual(missing.document.getElementById('incidentAt').value, '2024-01-01T08:00');
  assert.strictEqual(missing.document.getElementById('incidentWindow').value, '5', 'unsupported URL window is ignored');
  assert.strictEqual(missing.document.getElementById('incidentThreshold').value, '0.05', 'unsupported URL threshold is ignored');
  assert.ok(missing.window.location.search.includes('window=5'));
  assert.ok(missing.window.location.search.includes('threshold=0.05'));
  assert.ok(missing.document.getElementById('incidentVerdict').innerHTML.includes('未检测到异常'));
  assert.ok(missing.document.getElementById('incidentPlayers').innerHTML.includes('没有选手超过阈值'));
  assert.ok(missing.document.getElementById('incidentInfra').innerHTML.includes('基础设施正常'));
  assert.ok(missing.document.getElementById('incidentIsp').innerHTML.includes('ISP 流量数据不可用'));
  assert.ok(missing.document.getElementById('incidentStage').innerHTML.includes('没有 stage 受影响'));

  const failureError = new Error('<Prometheus failed>');
  const failed = createHarness({
    ispQueue: [Promise.reject(failureError)]
  });
  await failed.panel.start();
  assert.strictEqual(failed.document.getElementById('incidentVerdict').className, 'incident-verdict bad');
  assert.ok(failed.document.getElementById('incidentVerdict').innerHTML.includes('&lt;Prometheus failed&gt;'));
  ['incidentPlayers', 'incidentInfra', 'incidentIsp', 'incidentStage'].forEach((id) => {
    assert.ok(failed.document.getElementById(id).innerHTML.includes('加载中...'), 'existing non-verdict failure state is preserved');
  });
  assert.strictEqual(failed.errors.length, 1);
  assert.strictEqual(failed.errors[0][0], 'Incident analysis failed:');
  assert.strictEqual(failed.errors[0][1], failureError);

  const oldIsp = deferred();
  const stale = createHarness({
    ispQueue: [oldIsp.promise, Promise.resolve([])],
    result: (data, threshold) => emptyResult({
      verdict: { level: 'good', text: `threshold ${threshold}`, detail: 'latest' }
    })
  });
  const oldQuery = stale.panel.start();
  stale.panel.stop();
  stale.window.location.search = '?window=2&threshold=0.08';
  await stale.panel.start();
  assert.strictEqual(stale.analyzeCalls.length, 1);
  assert.ok(stale.document.getElementById('incidentVerdict').innerHTML.includes('threshold 0.08'));
  oldIsp.resolve([]);
  await oldQuery;
  assert.strictEqual(stale.analyzeCalls.length, 1, 'an older page lifecycle cannot overwrite the restarted panel');

  const stoppedIsp = deferred();
  const stopped = createHarness({ ispQueue: [stoppedIsp.promise] });
  const stoppedQuery = stopped.panel.start();
  stopped.panel.stop();
  stoppedIsp.resolve([]);
  await stoppedQuery;
  assert.strictEqual(stopped.analyzeCalls.length, 0, 'stop invalidates an in-flight Incident query');
  assert.ok(stopped.document.getElementById('incidentVerdict').innerHTML.includes('加载中...'));

  console.log('bigscreen Incident panel tests passed');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
