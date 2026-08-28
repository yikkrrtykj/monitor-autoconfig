const assert = require('assert');
const { createInfraController } = require('../bigscreen/infra/infra-controller.js');
const { createIspCarousel } = require('../bigscreen/isp-carousel.js');

assert.deepStrictEqual(
  Object.keys(require('../bigscreen/infra/infra-controller.js')),
  ['createInfraController'],
  'the Infra module exposes only its dependency-injected controller factory'
);

class FakeClassList {
  constructor() {
    this.values = new Set();
  }

  contains(name) {
    return this.values.has(name);
  }

  toggle(name, force) {
    if (force === undefined ? !this.values.has(name) : force) this.values.add(name);
    else this.values.delete(name);
  }

  setFromClassName(value) {
    this.values = new Set(String(value || '').split(/\s+/).filter(Boolean));
  }
}

class FakeElement {
  constructor(tagName = 'div', id = '') {
    this.tagName = tagName;
    this.id = id;
    this.dataset = {};
    this.styleValues = new Map();
    this.style = { setProperty: (name, value) => this.styleValues.set(name, String(value)) };
    this.classList = new FakeClassList();
    this.children = [];
    this.listeners = new Map();
    this.attributes = new Map();
    this.hidden = false;
    this.title = '';
    this._className = '';
    this._innerHTML = '';
    this.queries = new Map();
  }

  set className(value) {
    this._className = String(value || '');
    this.classList.setFromClassName(this._className);
  }

  get className() {
    return this._className;
  }

  set innerHTML(value) {
    this._innerHTML = String(value);
    this.children = [];
    this.queries.clear();
    if (this._innerHTML.includes('isp-page-previous')) {
      this.queries.set('.isp-page-previous', new FakeElement('button'));
      this.queries.set('.isp-page-next', new FakeElement('button'));
    }
  }

  get innerHTML() {
    return this._innerHTML;
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  appendChild(child) {
    if (child.isFragment) this.children.push(...child.children);
    else this.children.push(child);
    return child;
  }

  querySelector(selector) {
    return this.queries.get(selector) || null;
  }

  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }

  async dispatch(type) {
    const pending = (this.listeners.get(type) || []).map((listener) => listener({ type, target: this }));
    await Promise.all(pending.filter((value) => value && typeof value.then === 'function'));
  }
}

class FakeDocument {
  constructor() {
    this.screen = new FakeElement('main');
    this.screen.className = 'screen infra-mode';
    this.elements = new Map();
    [
      'pingGaugeGrid',
      'pingServerGaugeGrid',
      'serverGaugesWrap',
      'uptimeGaugeGrid',
      'pingTrendChart',
      'lossHeatmap',
      'ispGrid'
    ].forEach((id) => this.elements.set(id, new FakeElement('div', id)));
  }

  getElementById(id) {
    return this.elements.get(id) || null;
  }

  querySelector(selector) {
    if (selector === '.screen') return this.screen;
    if (selector === '.screen.tournament-mode') {
      return this.screen.classList.contains('tournament-mode') ? this.screen : null;
    }
    return null;
  }

  createElement(tagName) {
    return new FakeElement(tagName);
  }

  createDocumentFragment() {
    const fragment = new FakeElement('fragment');
    fragment.isFragment = true;
    return fragment;
  }
}

class FakeWindow {
  constructor() {
    this.nextTimerId = 1;
    this.timers = new Map();
  }

  setInterval(callback, delay) {
    const id = this.nextTimerId++;
    this.timers.set(id, { callback, delay, active: true });
    return id;
  }

  clearInterval(id) {
    const timer = this.timers.get(id);
    if (timer) timer.active = false;
  }

  activeTimers(delay) {
    return Array.from(this.timers.entries())
      .filter(([, timer]) => timer.active && (delay === undefined || timer.delay === delay));
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

async function settle(turns = 8) {
  for (let index = 0; index < turns; index += 1) await Promise.resolve();
}

function series(name, job, value) {
  return { name, metric: { instance: name, job }, values: [{ t: 100, v: value }] };
}

function createHarness(overrides = {}) {
  const document = new FakeDocument();
  const window = new FakeWindow();
  const renderCache = new Map();
  const calls = {
    instant: [],
    query: [],
    range: [],
    ping: [],
    loss: [],
    isp: [],
    presentation: [],
    errors: [],
    noData: [],
    visible: [],
    dataSuccess: 0,
    invalidate: 0,
    clearSignatures: 0,
    deletedSignatures: []
  };
  const state = {
    stageFilter: Boolean(overrides.stageFilter),
    failSeen: false,
    failGauge: false,
    failCharts: false,
    seriesVersion: 0,
    nameMapQueue: [],
    seenItems: [
      { name: 'stage-switch' },
      { name: 'core-switch' },
      { name: 'server-A' },
      { name: 'stage-display-alias' }
    ],
    targets: [
      { instance: ' stage-switch ' },
      { targetIp: 'core-switch' },
      { instance: 'server-A' },
      { displayName: ' stage-display-alias ' }
    ],
    pingGauge: [
      { name: 'stage-switch', value: 0.002, metric: { job: 'infra-dist-ping' } },
      { name: 'stage-switch', value: 0.006, metric: { job: 'infra-dist-ping' } },
      { name: 'core-switch', value: 0.003, metric: { job: 'infra-core-ping' } },
      { name: 'server-A', value: 0.001, metric: { job: 'infra-srv-ping' } },
      { name: 'stage-display-alias', value: 0.004, metric: { job: 'infra-dist-ping' } },
      { name: 'retired-switch', value: 0.200, metric: { job: 'infra-dist-ping' } }
    ],
    uptime: [
      { name: 'stage-switch', value: 100, metric: {} },
      { name: 'stage-switch', value: 200, metric: {} },
      { name: 'core-switch', value: 300, metric: {} },
      { name: 'retired-switch', value: 400, metric: {} }
    ],
    ispTraffic: [
      { name: 'ISP A', download: series('A download', '', 1), upload: series('A upload', '', 2) },
      { name: 'ISP B', download: series('B download', '', 3), upload: series('B upload', '', 4) },
      { name: 'ISP C', download: series('C download', '', 5), upload: series('C upload', '', 6) }
    ]
  };

  function chartSeries(query) {
    const high = 0.010 + state.seriesVersion * 0.001;
    if (query === 'PING') {
      return [
        series('stage-switch', 'infra-dist-ping', 0.002),
        series('stage-switch', 'infra-dist-ping', high),
        series('core-switch', 'infra-core-ping', 0.003),
        series('retired-switch', 'infra-dist-ping', 0.200)
      ];
    }
    if (query === 'SUCCESS') {
      return [
        series('stage-switch', 'infra-dist-ping', 1),
        series('core-switch', 'infra-core-ping', 1),
        series('retired-switch', 'infra-dist-ping', 1)
      ];
    }
    return [
      series('stage-switch', 'infra-dist-ping', 0),
      series('core-switch', 'infra-core-ping', 0.2),
      series('retired-switch', 'infra-dist-ping', 1)
    ];
  }

  const controller = createInfraController({
    document,
    window,
    console: { error: (...args) => calls.errors.push(args) },
    queries: { pingTrend: 'PING', pingSuccessTrend: 'SUCCESS', pingGauge: 'GAUGE', uptime: 'UPTIME', loss: 'LOSS' },
    stageDeviceFilter: 'stage,wutai,舞台',
    escapeHtml: (value) => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;'),
    escapeRegex: (value) => String(value).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'),
    metricName: (metric) => metric.instance || '',
    formatPing: (value) => ({ value: String(value), unit: 'ms' }),
    formatUptime: (value) => ({ value: String(value), unit: 's' }),
    gaugeColor: (_kind, value) => `color-${value}`,
    gaugePercent: (_kind, value) => Number(value),
    seriesSignature: (items) => JSON.stringify(items),
    activeInfraPingQuery: () => 'SEEN',
    activeSeriesNames: (items) => new Set(items.map((item) => item.name)),
    prometheusQuery: async (query) => {
      calls.query.push(query);
      if (state.failGauge) throw new Error('gauge failed');
      return query === 'GAUGE' ? state.pingGauge : state.uptime;
    },
    prometheusInstant: async (query) => {
      calls.instant.push(query);
      if (query === 'SEEN') {
        if (state.failSeen) throw new Error('seen failed');
        return state.seenItems;
      }
      return [];
    },
    prometheusRangeCached: async (query, _name, step) => {
      calls.range.push({ query, step });
      if (state.failCharts) throw new Error('chart failed');
      return chartSeries(query);
    },
    invalidateRangeCache: () => { calls.invalidate += 1; },
    fetchInfraDeviceNames: () => {
      if (state.nameMapQueue.length) return state.nameMapQueue.shift().promise;
      return Promise.resolve({});
    },
    renameListWithInfraMap: (items) => items.map((item) => ({ ...item })),
    partitionInfraPingItems: (items) => ({
      network: items.filter((item) => item.metric.job !== 'infra-srv-ping'),
      servers: items.filter((item) => item.metric.job === 'infra-srv-ping')
    }),
    fetchTopologyTargets: async () => {
      if (state.failSeen) throw new Error('targets failed');
      return state.targets;
    },
    fetchIspTraffic: async () => {
      if (state.failCharts) throw new Error('isp failed');
      return state.ispTraffic;
    },
    buildInfrastructurePingPresentation: (input) => {
      calls.presentation.push(input);
      return { displayLatencySeries: input.latencySeries.map((item) => ({ ...item })) };
    },
    renderPingChart: (input) => calls.ping.push(input),
    renderLossHeatmap: (...args) => calls.loss.push(args),
    renderIspChart: (input) => calls.isp.push(input),
    renderNoData: (element, message) => {
      calls.noData.push({ id: element.id, message });
      element.innerHTML = `<div>${message || '暂无数据'}</div>`;
    },
    setVisible: (id, visible) => {
      calls.visible.push({ id, visible });
      document.getElementById(id).hidden = !visible;
    },
    shouldRender: (key, signature) => {
      if (renderCache.get(key) === signature) return false;
      renderCache.set(key, signature);
      return true;
    },
    deleteRenderSignature: (key) => {
      calls.deletedSignatures.push(key);
      renderCache.delete(key);
    },
    clearRenderSignatures: () => {
      calls.clearSignatures += 1;
      renderCache.clear();
    },
    isStageFilterActive: () => state.stageFilter,
    onDataSuccess: () => { calls.dataSuccess += 1; },
    createIspCarousel
  });

  return { controller, document, window, calls, state };
}

function gaugeNames(element) {
  return element.children.map((child) => child.title);
}

async function testLifecycleTransformsAndModes() {
  const harness = createHarness({ stageFilter: true });
  const { controller, document, window, calls, state } = harness;

  controller.enterInfraMode();
  controller.start();
  assert.strictEqual(controller.hasScheduledRefresh(), true);
  assert.deepStrictEqual(window.activeTimers().map(([, timer]) => timer.delay), [5000, 5000, 30000]);
  assert.strictEqual(calls.clearSignatures, 1);
  assert.strictEqual(calls.invalidate, 1);
  assert.deepStrictEqual(calls.instant.slice(0, 1), ['SEEN']);
  assert.strictEqual(calls.dataSuccess, 0, 'seen-up success does not mark chart data fresh');

  await settle();

  assert.deepStrictEqual(calls.query.slice(0, 2), ['GAUGE', 'UPTIME']);
  assert(calls.instant.includes('last_over_time(up{job="infra-switch-snmp"}[25m])'));
  assert.deepStrictEqual(calls.range.slice(0, 3), [
    { query: 'PING', step: 2 },
    { query: 'SUCCESS', step: 2 },
    { query: 'LOSS', step: undefined }
  ]);
  assert.strictEqual(calls.dataSuccess, 2, 'only successful gauge and chart refreshes update freshness');

  assert.deepStrictEqual(gaugeNames(document.getElementById('pingGaugeGrid')), ['stage-display-alias', 'stage-switch']);
  assert.deepStrictEqual(gaugeNames(document.getElementById('pingServerGaugeGrid')), ['server-A']);
  assert.deepStrictEqual(gaugeNames(document.getElementById('uptimeGaugeGrid')), ['stage-switch']);
  assert.strictEqual(document.getElementById('serverGaugesWrap').hidden, false);
  assert.strictEqual(document.getElementById('pingGaugeGrid').styleValues.get('--gauge-columns'), '1');
  assert.strictEqual(document.getElementById('pingServerGaugeGrid').styleValues.get('--gauge-columns'), '1');
  assert(document.getElementById('pingGaugeGrid').children[1].innerHTML.includes('0.006'));
  assert(!gaugeNames(document.getElementById('pingGaugeGrid')).includes('retired-switch'));

  const presentation = calls.presentation.at(-1);
  assert.deepStrictEqual(presentation.latencySeries.map((item) => item.name), ['stage-switch']);
  assert.strictEqual(presentation.latencySeries[0].values[0].v, 0.010, 'duplicate series merge keeps the maximum RTT');
  assert.deepStrictEqual(presentation.successSeries.map((item) => item.name), ['stage-switch']);
  assert.strictEqual(calls.ping.at(-1).tournamentMode, false);
  assert.deepStrictEqual(calls.loss.at(-1)[1].map((item) => item.name), ['stage-switch']);
  assert.deepStrictEqual(calls.isp.slice(-3).map((item) => item.result.name), ['ISP A', 'ISP B', 'ISP C']);

  const refreshTimerIds = window.activeTimers().map(([id]) => id);
  controller.start();
  assert.deepStrictEqual(window.activeTimers().map(([id]) => id), refreshTimerIds, 'repeated start is a no-op');
  assert.strictEqual(calls.clearSignatures, 1);
  assert.strictEqual(calls.invalidate, 1);

  document.screen.className = 'screen tournament-mode match-mode';
  const ispCallCount = calls.isp.length;
  controller.enterTournamentMode();
  assert.deepStrictEqual(calls.isp.slice(ispCallCount).map((item) => item.result.name), ['ISP A', 'ISP B']);
  assert(calls.isp.slice(ispCallCount).every((item) => item.compactTournamentChart));
  assert.strictEqual(window.activeTimers(10000).length, 1, 'three ISP results activate one carousel timer');
  assert.deepStrictEqual(
    window.activeTimers().filter(([, timer]) => timer.delay !== 10000).map(([id]) => id),
    refreshTimerIds,
    'mode changes do not restart Infra refresh timers'
  );

  const pager = document.getElementById('ispGrid').children.find((child) => child.className === 'isp-pager');
  await pager.querySelector('.isp-page-next').dispatch('click');
  assert.strictEqual(calls.isp.at(-1).result.name, 'ISP C');
  assert.strictEqual(calls.isp.at(-1).resultIndex, 2);

  document.screen.className = 'screen infra-mode';
  const beforeInfraMode = calls.isp.length;
  controller.enterInfraMode();
  assert.deepStrictEqual(calls.isp.slice(beforeInfraMode).map((item) => item.result.name), ['ISP A', 'ISP B', 'ISP C']);
  assert(calls.isp.slice(beforeInfraMode).every((item) => !item.compactTournamentChart));
  assert.strictEqual(window.activeTimers(10000).length, 0);

  state.stageFilter = false;
  const [gaugeRefresh, chartRefresh] = window.activeTimers(5000).map(([, timer]) => timer.callback);
  await gaugeRefresh();
  await chartRefresh();
  assert(gaugeNames(document.getElementById('pingGaugeGrid')).includes('core-switch'));
  assert(calls.ping.at(-1).series.some((item) => item.name === 'core-switch'));

  const beforeResize = calls.range.length;
  controller.refreshForResize();
  await settle();
  assert.strictEqual(calls.range.length, beforeResize + 3);

  controller.stop();
  assert.strictEqual(controller.hasScheduledRefresh(), false);
  assert.strictEqual(window.activeTimers().length, 0);
  const afterStop = calls.range.length;
  controller.refreshForResize();
  await settle();
  assert.strictEqual(calls.range.length, afterStop, 'resize does nothing while Infra polling is stopped');

  controller.start();
  await settle();
  assert.strictEqual(calls.clearSignatures, 2);
  assert.strictEqual(calls.invalidate, 2);
  controller.stop();
}

async function testSeenFailureRetainsDeployedSets() {
  const harness = createHarness();
  const { controller, document, window, state } = harness;
  controller.start();
  await settle();

  state.failSeen = true;
  state.pingGauge = [{ name: 'retired-switch', value: 0.2, metric: { job: 'infra-dist-ping' } }];
  await window.activeTimers(30000)[0][1].callback();
  await window.activeTimers(5000)[0][1].callback();
  assert.deepStrictEqual(gaugeNames(document.getElementById('pingGaugeGrid')), []);
  assert(document.getElementById('pingGaugeGrid').innerHTML.includes('暂无数据'));
  controller.stop();
}

async function testFailuresAndEmptyFallbacks() {
  const harness = createHarness();
  const { controller, document, calls, state } = harness;
  state.failGauge = true;
  state.failCharts = true;
  controller.start();
  await settle();

  assert.strictEqual(calls.dataSuccess, 0);
  assert(document.getElementById('pingGaugeGrid').innerHTML.includes('暂无数据'));
  assert(document.getElementById('pingServerGaugeGrid').innerHTML.includes('暂无数据'));
  assert.strictEqual(document.getElementById('serverGaugesWrap').hidden, true);
  assert(document.getElementById('uptimeGaugeGrid').innerHTML.includes('暂无数据'));
  assert.deepStrictEqual(calls.deletedSignatures.sort(), ['ispGrid', 'lossHeatmap', 'pingTrendChart']);
  assert(calls.noData.some((item) => item.id === 'pingTrendChart'));
  assert(calls.noData.some((item) => item.id === 'ispGrid'));
  assert.deepStrictEqual(calls.loss.at(-1), ['lossHeatmap', []]);
  assert.strictEqual(calls.errors.length, 2);
  controller.stop();
}

async function testLatestChartRequestWins() {
  const harness = createHarness();
  const { controller, document, window, calls, state } = harness;
  controller.start();
  await settle();
  state.stageFilter = false;

  const gaugeRefresh = window.activeTimers(5000)[0][1].callback;
  const chartRefresh = window.activeTimers(5000)[1][1].callback;
  const firstNames = deferred();
  const secondNames = deferred();
  state.nameMapQueue.push(firstNames, secondNames);

  state.seriesVersion = 1;
  chartRefresh();
  await settle(2);
  state.seriesVersion = 2;
  chartRefresh();
  await settle(2);

  secondNames.resolve({});
  await settle();
  const latestValue = calls.ping.at(-1).series.find((item) => item.name === 'stage-switch').values[0].v;
  assert.strictEqual(latestValue, 0.012);
  const renderCount = calls.ping.length;

  firstNames.resolve({});
  await settle();
  assert.strictEqual(calls.ping.length, renderCount, 'older chart response is discarded by chartSeq');
  assert.strictEqual(calls.ping.at(-1).series.find((item) => item.name === 'stage-switch').values[0].v, 0.012);

  const firstGaugeNames = deferred();
  const secondGaugeNames = deferred();
  state.nameMapQueue.push(firstGaugeNames, secondGaugeNames);
  state.pingGauge = [{ name: 'stage-switch', value: 0.021, metric: { job: 'infra-dist-ping' } }];
  gaugeRefresh();
  await settle(2);
  state.pingGauge = [{ name: 'stage-switch', value: 0.022, metric: { job: 'infra-dist-ping' } }];
  gaugeRefresh();
  await settle(2);

  secondGaugeNames.resolve({});
  await settle();
  assert(document.getElementById('pingGaugeGrid').children[0].innerHTML.includes('0.022'));
  firstGaugeNames.resolve({});
  await settle();
  assert(document.getElementById('pingGaugeGrid').children[0].innerHTML.includes('0.022'), 'older gauge response is discarded by gaugeSeq');
  controller.stop();
}

(async () => {
  await testLifecycleTransformsAndModes();
  await testSeenFailureRetainsDeployedSets();
  await testFailuresAndEmptyFallbacks();
  await testLatestChartRequestWins();
  console.log('bigscreen infra controller tests: ok');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
