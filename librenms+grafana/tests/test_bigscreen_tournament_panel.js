const assert = require('assert');
const tournamentPanelModule = require('../bigscreen/tournament/tournament-panel.js');
const playersModule = require('../bigscreen/players.js');
const utils = require('../bigscreen/utils.js');

assert.deepStrictEqual(
  Object.keys(tournamentPanelModule),
  ['createTournamentPanel'],
  'the Tournament panel exposes only its dependency-injected controller factory'
);

class FakeElement {
  constructor(id, ownerDocument) {
    this.id = id;
    this.ownerDocument = ownerDocument;
    this.dataset = {};
    this.listeners = new Map();
    this.className = '';
    this.clientWidth = 180;
    this.clientHeight = 72;
    this._innerHTML = '';
  }

  set innerHTML(value) {
    this._innerHTML = String(value);
    const pattern = /\sid="([^"]+)"/g;
    let match;
    while ((match = pattern.exec(this._innerHTML)) !== null) {
      if (!this.ownerDocument.elements.has(match[1])) {
        this.ownerDocument.elements.set(match[1], new FakeElement(match[1], this.ownerDocument));
      }
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

  async dispatch(type) {
    const event = { type, target: this };
    const pending = [];
    (this.listeners.get(type) || []).forEach((handler) => {
      const result = handler(event);
      if (result && typeof result.then === 'function') pending.push(result);
    });
    await Promise.all(pending);
  }

  getBoundingClientRect() {
    return { width: this.clientWidth, height: this.clientHeight };
  }
}

class FakeDocument {
  constructor() {
    this.elements = new Map();
    ['tournamentRefresh', 'tournamentSummary', 'tournamentBoard', 'tournamentTrendChart']
      .forEach((id) => this.elements.set(id, new FakeElement(id, this)));
  }

  getElementById(id) {
    return this.elements.get(id) || null;
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

function metricItem(metric, value) {
  return { metric, value };
}

function defaultSnapshot() {
  return {
    latencyItems: [
      metricItem({ team: '2', seat: '2', instance: '10.0.2.12', network: 'wired' }, 0.09),
      metricItem({ team: '2', seat: '1', instance: '10.0.2.11', network: 'wired' }, 0.005),
      metricItem({ team: '1', seat: '1', instance: '10.0.1.11', network: 'wired' }, 0.002)
    ],
    successItems: [
      metricItem({ team: '2', seat: '2', instance: '10.0.2.12', network: 'wired' }, 1),
      metricItem({ team: '1', seat: '2', instance: '10.0.1.12', network: 'wired' }, 1),
      metricItem({ team: '2', seat: '1', instance: '10.0.2.11', network: 'wired' }, 0),
      metricItem({ team: '1', seat: '1', instance: '10.0.1.11', network: 'wired' }, 1)
    ]
  };
}

function defaultTrend() {
  return [
    { metric: { team: '2', seat: '2' }, values: [{ t: 100, v: 0.07 }, { t: 104, v: 0.09 }] },
    { metric: { team: '1', seat: '2' }, values: [] },
    { metric: { team: '1', seat: '1' }, values: [{ t: 100, v: 0.002 }, { t: 103, v: null }, { t: 104, v: 0.003 }] },
    { metric: { team: '2', seat: '1' }, values: [{ t: 100, v: 0.004 }, { t: 104, v: 0.005 }] }
  ];
}

function matchPage() {
  return {
    id: 'match-5v5',
    kind: 'match',
    teams: [1, 2],
    teamSize: 2,
    trendMode: 'per-seat'
  };
}

function groupPage() {
  return {
    id: 'tournament-groups',
    kind: 'tournament',
    teams: [1, 2],
    teamSize: 2,
    groups: [[1], [2]],
    trendMode: 'groups'
  };
}

function flatPage() {
  return {
    id: 'tournament-flat',
    kind: 'tournament',
    teams: [1, 2],
    teamSize: 2
  };
}

function createHarness(options = {}) {
  const document = new FakeDocument();
  const intervals = new Map();
  const clearedIntervals = [];
  let nextInterval = 1;
  const window = {
    setInterval(handler, delay) {
      const id = nextInterval++;
      intervals.set(id, { handler, delay });
      return id;
    },
    clearInterval(id) {
      clearedIntervals.push(id);
      intervals.delete(id);
    }
  };
  const selectorCalls = [];
  const snapshotCalls = [];
  const rangeCalls = [];
  const buildCalls = [];
  const noDataCalls = [];
  const errors = [];
  const teamOrderCalls = [];
  let renderSignatureClears = 0;
  let rangeInvalidations = 0;
  let dataSuccesses = 0;
  const signatures = new Map();
  const snapshotSource = options.fetchPlayerSnapshot || (() => Promise.resolve(defaultSnapshot()));
  const rangeSource = options.prometheusRangeCached || (() => Promise.resolve(defaultTrend()));
  const teamLayouts = options.teamLayouts || {
    applyTeamOrder(page, rawOrders) {
      teamOrderCalls.push({ page, rawOrders });
      return options.configuredPage ? options.configuredPage(page) : page;
    }
  };

  function teamName(page, team) {
    const teamNumber = Number(team);
    if (page.id === 'match-5v5') {
      if (teamNumber === 1) return '舞台左';
      if (teamNumber === 2) return '舞台右';
    }
    return `第 ${teamNumber} 队`;
  }

  function latencyUrlForPlayer(player) {
    const params = new URLSearchParams({
      team: String(player.team),
      seat: String(player.seat),
      network: player.network || 'wired'
    });
    if (player.ip) params.set('ip', player.ip);
    return `/latency?${params.toString()}`;
  }

  function playerLabel(team, seat, network) {
    return `${teamName({ id: '' }, team)} ${utils.seatLabel(seat)} ${network}`;
  }

  const panel = tournamentPanelModule.createTournamentPanel({
    document,
    window,
    console: { error: (error) => errors.push(error) },
    teamLayouts,
    getTeamOrders: () => options.teamOrders || '{"fixture":true}',
    seriesColors: ['#one', '#two', '#three'],
    escapeHtml: utils.escapeHtml,
    seatLabel: utils.seatLabel,
    formatPingText: utils.formatPingText,
    niceMax: utils.niceMax,
    average: utils.average,
    linePathFromPoints: utils.linePathFromPoints,
    teamName,
    latencyUrlForPlayer,
    playerLabel,
    buildPlayers(latencyItems, successItems) {
      buildCalls.push({ latencyItems, successItems });
      return playersModule.buildPlayers(latencyItems, successItems);
    },
    latencyLevel: playersModule.latencyLevel,
    tournamentSelector(page, network = 'wired') {
      selectorCalls.push({ page, network });
      return `role="player",network="${network}",teams="${(page.teams || []).join('|')}"`;
    },
    fetchPlayerSnapshot(selector) {
      snapshotCalls.push(selector);
      try {
        return Promise.resolve(snapshotSource(selector, snapshotCalls.length - 1));
      } catch (error) {
        return Promise.reject(error);
      }
    },
    prometheusRangeCached(query, labeler) {
      rangeCalls.push({ query, labeler });
      try {
        return Promise.resolve(rangeSource(query, labeler, rangeCalls.length - 1));
      } catch (error) {
        return Promise.reject(error);
      }
    },
    renderNoData(element, message) {
      const value = message || '暂无数据';
      element.innerHTML = `<div class="no-data">${value}</div>`;
      noDataCalls.push({ id: element.id, message: value });
    },
    shouldRender(key, signature) {
      if (signatures.get(key) === signature) return false;
      signatures.set(key, signature);
      return true;
    },
    seriesSignature: utils.seriesSignature,
    deleteRenderSignature(key) {
      signatures.delete(key);
    },
    clearRenderSignatures() {
      renderSignatureClears += 1;
      signatures.clear();
    },
    invalidateRangeCache() {
      rangeInvalidations += 1;
    },
    onDataSuccess() {
      dataSuccesses += 1;
    }
  });

  return {
    panel,
    document,
    intervals,
    clearedIntervals,
    selectorCalls,
    snapshotCalls,
    rangeCalls,
    buildCalls,
    noDataCalls,
    errors,
    teamOrderCalls,
    get renderSignatureClears() { return renderSignatureClears; },
    get rangeInvalidations() { return rangeInvalidations; },
    get dataSuccesses() { return dataSuccesses; }
  };
}

async function main() {
  // start performs the immediate refresh and keeps the original five-second
  // cadence. Shared selector/snapshot helpers are injected rather than copied.
  const lifecycle = createHarness();
  lifecycle.panel.start(matchPage());
  assert.strictEqual(lifecycle.panel.hasScheduledRefresh(), true);
  assert.strictEqual(lifecycle.intervals.size, 1);
  assert.strictEqual([...lifecycle.intervals.values()][0].delay, 5000);
  assert.strictEqual(lifecycle.renderSignatureClears, 1);
  assert.strictEqual(lifecycle.rangeInvalidations, 1);
  assert.deepStrictEqual(lifecycle.snapshotCalls, ['role="player",network="wired",teams="1|2"']);
  await settle();
  assert.strictEqual(lifecycle.dataSuccesses, 1);
  assert.strictEqual(lifecycle.buildCalls.length, 1);
  assert.deepStrictEqual(lifecycle.buildCalls[0].latencyItems, defaultSnapshot().latencyItems);
  assert.strictEqual(lifecycle.rangeCalls.length, 1);
  assert.strictEqual(
    lifecycle.rangeCalls[0].query,
    'avg by (team,seat) (probe_icmp_duration_seconds{role="player",network="wired",teams="1|2",phase="rtt"})'
  );
  assert.strictEqual(lifecycle.rangeCalls[0].labeler({ team: '1', seat: '2' }), '舞台左 S2');

  // Summary and seat presentation preserve online/offline/unknown/high RTT,
  // names, IP suffixes, links, and ascending seat order.
  const summary = lifecycle.document.getElementById('tournamentSummary').innerHTML;
  assert.match(summary, /在线[\s\S]*<strong>3<\/strong>/);
  assert.match(summary, /离线[\s\S]*<strong>1<\/strong>/);
  assert.match(summary, /高延迟[\s\S]*<strong>1<\/strong>/);
  assert.match(summary, /总计[\s\S]*<strong>4<\/strong>/);
  const board = lifecycle.document.getElementById('tournamentBoard');
  assert.strictEqual(board.className, 'tournament-board match-board');
  assert.match(board.innerHTML, /舞台左/);
  assert.match(board.innerHTML, /舞台右/);
  assert.match(board.innerHTML, /seat-slot good/);
  assert.match(board.innerHTML, /seat-slot unknown/);
  assert.match(board.innerHTML, /seat-slot offline/);
  assert.match(board.innerHTML, /seat-slot bad/);
  assert.match(board.innerHTML, /\.11/);
  assert.match(board.innerHTML, /team=1&amp;seat=1&amp;network=wired&amp;ip=10\.0\.1\.11/);
  assert.ok(board.innerHTML.indexOf('.11') < board.innerHTML.indexOf('.12'), 'seat order remains ascending');

  // Per-seat layout and sparkline output preserve dimensions, SVG, legend,
  // irregular time ranges, null samples, and empty-series presentation.
  const perSeatTrend = lifecycle.document.getElementById('tournamentTrendChart').innerHTML;
  assert.match(perSeatTrend, /team-trend-stack-horizontal/);
  assert.match(perSeatTrend, /seatTrend_1_1/);
  assert.match(perSeatTrend, /seatTrend_2_2/);
  const normalSparkline = lifecycle.document.getElementById('seatTrend_1_1').innerHTML;
  assert.match(normalSparkline, /class="sparkline-chart" width="180" height="72" viewBox="0 0 180 72"/);
  assert.match(normalSparkline, /class="sparkline-path"/);
  assert.match(normalSparkline, /class="sparkline-grid"/);
  assert.ok(!normalSparkline.includes('NaN'), 'current null handling must continue producing drawable coordinates');
  assert.ok(lifecycle.noDataCalls.some((item) => item.id === 'seatTrend_1_2' && item.message === '暂无趋势'));

  // The group and flat branches remain distinct and keep configured team order.
  const grouped = createHarness({
    configuredPage: (page) => ({ ...page, groups: [[2], [1]] })
  });
  grouped.panel.start(groupPage());
  await settle();
  const groupedTrend = grouped.document.getElementById('tournamentTrendChart').innerHTML;
  assert.match(groupedTrend, /team-trend-stack/);
  assert.ok(groupedTrend.indexOf('第 2 队') < groupedTrend.indexOf('第 1 队'));
  const groupedBoard = grouped.document.getElementById('tournamentBoard').innerHTML;
  assert.ok(groupedBoard.indexOf('第 2 队') < groupedBoard.indexOf('第 1 队'));
  assert.strictEqual(grouped.teamOrderCalls[0].rawOrders, '{"fixture":true}');

  const flat = createHarness();
  flat.panel.start(flatPage());
  await settle();
  const flatTrend = flat.document.getElementById('tournamentTrendChart').innerHTML;
  assert.match(flatTrend, /team-trend-grid/);
  assert.ok(!flatTrend.includes('team-trend-stack'));
  assert.match(flatTrend, /id="teamTrend1"/);
  assert.match(flat.document.getElementById('teamTrend1').innerHTML, /sparkline-legend/);

  // Repeated start replaces the timer and keeps one manual listener. Manual
  // refresh is immediate and stop removes only the Tournament timer.
  const firstTimer = [...lifecycle.intervals.keys()][0];
  lifecycle.panel.start(matchPage());
  const secondTimer = [...lifecycle.intervals.keys()][0];
  assert.notStrictEqual(secondTimer, firstTimer);
  assert.deepStrictEqual(lifecycle.clearedIntervals, [firstTimer]);
  assert.strictEqual(lifecycle.intervals.size, 1);
  assert.strictEqual(lifecycle.document.getElementById('tournamentRefresh').listenerCount('click'), 1);
  await settle();
  const callsBeforeManual = lifecycle.snapshotCalls.length;
  await lifecycle.document.getElementById('tournamentRefresh').dispatch('click');
  await settle();
  assert.strictEqual(lifecycle.snapshotCalls.length, callsBeforeManual + 1);
  lifecycle.panel.stop();
  assert.strictEqual(lifecycle.panel.hasScheduledRefresh(), false);
  assert.strictEqual(lifecycle.intervals.size, 0);
  await lifecycle.document.getElementById('tournamentRefresh').dispatch('click');
  await settle();
  assert.strictEqual(lifecycle.snapshotCalls.length, callsBeforeManual + 1, 'hidden manual refresh remains inactive after stop');

  // A newer refresh wins over an older pending snapshot, preserving the
  // existing sequence guard without changing route-stop semantics.
  const oldSnapshot = deferred();
  const newSnapshot = {
    latencyItems: [metricItem({ team: '1', seat: '1', instance: '10.9.9.9', network: 'wired' }, 0.003)],
    successItems: [metricItem({ team: '1', seat: '1', instance: '10.9.9.9', network: 'wired' }, 1)]
  };
  const sequence = createHarness({
    fetchPlayerSnapshot: (_selector, index) => index === 0 ? oldSnapshot.promise : newSnapshot
  });
  sequence.panel.start(matchPage());
  const newerRefresh = sequence.panel.refresh(matchPage());
  await newerRefresh;
  assert.match(sequence.document.getElementById('tournamentBoard').innerHTML, /\.9/);
  const stableBoard = sequence.document.getElementById('tournamentBoard').innerHTML;
  oldSnapshot.resolve(defaultSnapshot());
  await settle();
  assert.strictEqual(sequence.document.getElementById('tournamentBoard').innerHTML, stableBoard);
  assert.strictEqual(sequence.dataSuccesses, 1);

  // Empty players keep the existing team/seat skeleton; missing trend series
  // produces the same per-card no-data output.
  const empty = createHarness({
    fetchPlayerSnapshot: () => ({ latencyItems: [], successItems: [] }),
    prometheusRangeCached: () => []
  });
  empty.panel.start(groupPage());
  await settle();
  assert.match(empty.document.getElementById('tournamentSummary').innerHTML, /总计[\s\S]*<strong>0<\/strong>/);
  assert.match(empty.document.getElementById('tournamentBoard').innerHTML, /seat-slot empty/);
  assert.ok(empty.noDataCalls.some((item) => item.id === 'teamTrend1' && item.message === '暂无趋势'));
  assert.ok(empty.noDataCalls.some((item) => item.id === 'teamTrend2' && item.message === '暂无趋势'));

  // Query failures keep the exact board/trend empty-state split and error log.
  const failed = createHarness({
    fetchPlayerSnapshot: () => { throw new Error('Prometheus unavailable'); }
  });
  failed.panel.start(matchPage());
  await settle();
  assert.deepStrictEqual(failed.noDataCalls.map((item) => item.id), ['tournamentBoard', 'tournamentTrendChart']);
  assert.strictEqual(failed.noDataCalls[0].message, '暂无选手数据');
  assert.strictEqual(failed.errors[0].message, 'Prometheus unavailable');
  assert.strictEqual(failed.dataSuccesses, 0);

  console.log('bigscreen Tournament panel tests passed');
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
