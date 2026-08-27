const assert = require('assert');
const wirelessPanelModule = require('../bigscreen/wireless/wireless-panel.js');
const playersModule = require('../bigscreen/players.js');
const utils = require('../bigscreen/utils.js');

assert.deepStrictEqual(
  Object.keys(wirelessPanelModule),
  ['createWirelessPanel'],
  'the Wireless panel exposes only its dependency-injected controller factory'
);

class FakeClassList {
  constructor() {
    this.values = new Set();
  }

  add(value) {
    this.values.add(value);
  }

  remove(value) {
    this.values.delete(value);
  }

  contains(value) {
    return this.values.has(value);
  }
}

class FakeElement {
  constructor(id) {
    this.id = id;
    this.dataset = {};
    this.listeners = new Map();
    this.innerHTML = '';
    this.hidden = false;
    this.disabled = false;
    this.classList = new FakeClassList();
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
    (this.listeners.get(type) || []).forEach((handler) => handler.call(this, event));
  }

  insertAdjacentHTML(position, html) {
    assert.strictEqual(position, 'afterbegin');
    this.innerHTML = `${html}${this.innerHTML}`;
  }
}

class FakeDocument {
  constructor() {
    this.elements = new Map();
    ['wirelessControls', 'wirelessSummary', 'wirelessBoard', 'wirelessRescan']
      .forEach((id) => this.elements.set(id, new FakeElement(id)));
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
  const latencyItems = [
    metricItem({ team: '2', seat: '1', instance: '10.0.2.11', network: 'wireless' }, 0.005),
    metricItem({ team: '1', seat: '2', instance: '10.0.1.12', network: 'wireless' }, 0.120),
    metricItem({ team: '1', seat: '1', instance: '10.0.1.11', network: 'wireless' }, 0.003),
    metricItem({ team: '1', seat: '9', instance: '10.0.1.254', network: 'wireless' }, 0.001)
  ];
  const successItems = [
    metricItem({ team: '2', seat: '1', instance: '10.0.2.11', network: 'wireless' }, 0),
    metricItem({ team: '1', seat: '2', instance: '10.0.1.12', network: 'wireless' }, 1),
    metricItem({ team: '1', seat: '1', instance: '10.0.1.11', network: 'wireless' }, 1),
    metricItem({ team: '1', seat: '9', instance: '10.0.1.254', network: 'wireless' }, 1)
  ];
  return {
    latencyItems,
    successItems,
    players: playersModule.buildPlayers(latencyItems, successItems)
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
  const snapshotCalls = [];
  const queryCalls = [];
  const noDataCalls = [];
  const errors = [];
  const rescanCalls = [];
  let dataSuccesses = 0;
  const snapshotSource = options.fetchPlayerSnapshot || (() => Promise.resolve(defaultSnapshot()));
  const querySource = options.prometheusQuery || ((query) => {
    if (query.startsWith('unpoller_device_info')) {
      return [
        metricItem({ name: 'AP-B', model: 'U6-Lite', status: 'down' }, 1),
        metricItem({ name: 'AP-A', model: 'U6-Pro', state: 'connected' }, 1),
        metricItem({ name: 'AP-C', model: 'UAP-AC-Pro' }, 1),
        metricItem({ model: 'ignored' }, 1)
      ];
    }
    if (query.startsWith('sum by (name)')) {
      return [
        metricItem({ name: 'AP-A' }, 8),
        metricItem({ name: 'AP-B' }, 7),
        metricItem({ name: 'AP-C' }, 2)
      ];
    }
    return [];
  });
  const panel = wirelessPanelModule.createWirelessPanel({
    document,
    window,
    console: { error: (error) => errors.push(error) },
    escapeHtml: utils.escapeHtml,
    seatLabel: utils.seatLabel,
    formatPingText: utils.formatPingText,
    isGatewayAddress: playersModule.isGatewayAddress,
    latencyLevel: playersModule.latencyLevel,
    playerStatusText: playersModule.playerStatusText,
    teamName: (page, team) => `第 ${Number(team)} 队`,
    latencyUrlForPlayer: (player) => `/latency?team=${player.team}&seat=${player.seat}&network=${player.network}&ip=${player.ip}`,
    renderNoData(element, message) {
      const value = message || '暂无数据';
      element.innerHTML = `<div class="no-data">${value}</div>`;
      noDataCalls.push({ id: element.id, message: value });
    },
    fetchPlayerSnapshot(selector) {
      snapshotCalls.push(selector);
      try {
        return Promise.resolve(snapshotSource(selector, snapshotCalls.length - 1));
      } catch (error) {
        return Promise.reject(error);
      }
    },
    prometheusQuery(query) {
      queryCalls.push(query);
      try {
        return Promise.resolve(querySource(query, queryCalls.length - 1));
      } catch (error) {
        return Promise.reject(error);
      }
    },
    triggerRescan(button) {
      rescanCalls.push(button);
      if (options.triggerRescan) return options.triggerRescan(button);
      return undefined;
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
    snapshotCalls,
    queryCalls,
    noDataCalls,
    errors,
    rescanCalls,
    get dataSuccesses() { return dataSuccesses; }
  };
}

async function main() {
  // start performs the immediate refresh, renders the existing control copy,
  // and schedules the unchanged five-second polling cadence.
  const normal = createHarness();
  normal.panel.start({ id: 'wireless' });
  assert.strictEqual(normal.panel.hasScheduledRefresh(), true);
  assert.strictEqual(normal.intervals.size, 1);
  assert.strictEqual([...normal.intervals.values()][0].delay, 5000);
  assert.deepStrictEqual(normal.snapshotCalls, ['role="player",network="wireless"']);
  assert.match(normal.document.getElementById('wirelessControls').innerHTML, /无线异常总览/);
  assert.strictEqual(
    normal.document.getElementById('wirelessSummary').innerHTML,
    '',
    'the current implementation has no separate loading placeholder while the first query is pending'
  );
  await settle();
  assert.strictEqual(normal.dataSuccesses, 1);
  const summary = normal.document.getElementById('wirelessSummary').innerHTML;
  assert.match(summary, /无线目标[\s\S]*<strong>3<\/strong>/);
  assert.match(summary, /在线[\s\S]*<strong>2<\/strong>/);
  assert.match(summary, /高延迟[\s\S]*<strong>1<\/strong>/);
  assert.match(summary, /疑似网关[\s\S]*<strong>1<\/strong>/);
  assert.match(summary, /最高延迟[\s\S]*<strong>120\.0 ms<\/strong>/);

  // Wireless player mapping remains delegated to players.js; presentation
  // order is offline first, then higher latency, then team/seat.
  const board = normal.document.getElementById('wirelessBoard').innerHTML;
  const offlineIndex = board.indexOf('10.0.2.11');
  const highIndex = board.indexOf('10.0.1.12');
  const normalIndex = board.indexOf('10.0.1.11');
  assert.ok(offlineIndex >= 0 && offlineIndex < highIndex && highIndex < normalIndex);
  assert.match(board, /wireless-table-row offline/);
  assert.match(board, /wireless-table-row bad/);
  assert.match(board, /data-label="IP">10\.0\.1\.11/);
  assert.match(board, /href="\/latency\?team=1&amp;seat=1&amp;network=wireless&amp;ip=10\.0\.1\.11"/);

  // AP compatibility keeps the original label fallbacks: explicit down is
  // offline, connected is online, and missing state labels fall back online.
  assert.match(board, /无线 AP：2 台在线 \/ 3 台 · 10 客户端/);
  assert.ok(board.indexOf('AP-A') < board.indexOf('AP-C'));
  assert.ok(board.indexOf('AP-C') < board.indexOf('AP-B'));
  assert.match(board, /ap-chip offline[\s\S]*AP-B[\s\S]*离线/);
  assert.ok(!board.includes('ignored'), 'AP rows without a name remain filtered out');
  assert.strictEqual(normal.queryCalls.length, 8, 'two AP base queries plus six compatible online-state probes');

  // Repeated start replaces the timer and keeps the one-time rescan binding.
  const firstTimer = [...normal.intervals.keys()][0];
  normal.panel.start({ id: 'wireless' });
  const secondTimer = [...normal.intervals.keys()][0];
  assert.notStrictEqual(secondTimer, firstTimer);
  assert.deepStrictEqual(normal.clearedIntervals, [firstTimer]);
  assert.strictEqual(normal.intervals.size, 1);
  assert.strictEqual(normal.document.getElementById('wirelessRescan').listenerCount('click'), 1);
  assert.strictEqual(normal.document.getElementById('wirelessRescan').hidden, true);
  normal.document.getElementById('wirelessRescan').dispatch('click');
  assert.deepStrictEqual(normal.rescanCalls, [normal.document.getElementById('wirelessRescan')]);
  normal.panel.start({ id: 'wireless' });
  normal.document.getElementById('wirelessRescan').dispatch('click');
  assert.strictEqual(normal.rescanCalls.length, 2, 'repeated start must not multiply rescan requests');
  await settle();

  // The scheduled callback performs another refresh and stop only cancels the
  // active interval, matching the pre-extraction controller lifecycle.
  const activeTimer = [...normal.intervals.values()][0];
  activeTimer.handler();
  await settle();
  assert.strictEqual(normal.snapshotCalls.length, 4);
  normal.panel.stop();
  assert.strictEqual(normal.panel.hasScheduledRefresh(), false);
  assert.strictEqual(normal.intervals.size, 0);

  // Empty players and APs preserve the existing no-data presentation while a
  // successful query still refreshes global freshness.
  const empty = createHarness({
    fetchPlayerSnapshot: () => ({ latencyItems: [], successItems: [], players: [] }),
    prometheusQuery: () => []
  });
  empty.panel.start({ id: 'wireless' });
  await settle();
  assert.match(empty.document.getElementById('wirelessSummary').innerHTML, /无线目标[\s\S]*<strong>0<\/strong>/);
  assert.strictEqual(empty.document.getElementById('wirelessBoard').innerHTML, '<div class="no-data">当前没有无线选手</div>');
  assert.strictEqual(empty.dataSuccesses, 1);

  // AP base-query failure is intentionally partial: it yields no AP strip but
  // leaves the successful player board visible.
  const apFailure = createHarness({
    prometheusQuery(query) {
      if (query.startsWith('unpoller_device_info')) throw new Error('unifi unavailable');
      return [];
    }
  });
  apFailure.panel.start({ id: 'wireless' });
  await settle();
  assert.match(apFailure.document.getElementById('wirelessBoard').innerHTML, /wireless-table/);
  assert.ok(!apFailure.document.getElementById('wirelessBoard').innerHTML.includes('ap-strip'));
  assert.strictEqual(apFailure.errors.length, 0, 'AP failures are swallowed by the compatibility fallback');
  assert.strictEqual(apFailure.dataSuccesses, 1);

  // Optional online-state metric failures also fall back to device_info labels.
  const optionalFailure = createHarness({
    prometheusQuery(query) {
      if (query.startsWith('unpoller_device_info')) {
        return [metricItem({ name: 'AP-Legacy', status: 'connected' }, 1)];
      }
      if (query.startsWith('sum by (name)')) return [metricItem({ name: 'AP-Legacy' }, 4)];
      throw new Error('metric not exported by this unpoller version');
    }
  });
  optionalFailure.panel.start({ id: 'wireless' });
  await settle();
  assert.match(optionalFailure.document.getElementById('wirelessBoard').innerHTML, /AP-Legacy/);
  assert.match(optionalFailure.document.getElementById('wirelessBoard').innerHTML, /<b>4<\/b> 人/);

  // A player/Prometheus snapshot failure keeps the original whole-page error
  // behavior even though the independent AP path may resolve.
  const playerFailure = createHarness({
    fetchPlayerSnapshot: () => Promise.reject(new Error('prometheus unavailable'))
  });
  playerFailure.panel.start({ id: 'wireless' });
  await settle();
  assert.strictEqual(playerFailure.document.getElementById('wirelessSummary').innerHTML, '<div class="no-data">查询失败</div>');
  assert.strictEqual(playerFailure.document.getElementById('wirelessBoard').innerHTML, '<div class="no-data">暂无数据</div>');
  assert.strictEqual(playerFailure.errors.length, 1);
  assert.strictEqual(playerFailure.dataSuccesses, 0);

  // No stale/concurrency guard existed before this extraction: interval ticks
  // may overlap, and stop cancels future ticks without cancelling an in-flight
  // request. Lock that behavior so this refactor does not silently add policy.
  const firstRequest = deferred();
  const secondRequest = deferred();
  const lifecycle = createHarness({
    fetchPlayerSnapshot(selector, index) {
      return index === 0 ? firstRequest.promise : secondRequest.promise;
    },
    prometheusQuery: () => []
  });
  lifecycle.panel.start({ id: 'wireless' });
  [...lifecycle.intervals.values()][0].handler();
  assert.strictEqual(lifecycle.snapshotCalls.length, 2, 'overlapping interval refreshes remain allowed');
  lifecycle.panel.stop();
  secondRequest.resolve({ latencyItems: [], successItems: [], players: [] });
  await settle();
  assert.strictEqual(lifecycle.dataSuccesses, 1, 'an in-flight response still renders after stop, as before extraction');
  firstRequest.resolve(defaultSnapshot());
  await settle();
  assert.strictEqual(lifecycle.dataSuccesses, 2);
  assert.match(lifecycle.document.getElementById('wirelessBoard').innerHTML, /10\.0\.1\.11/);

  console.log('bigscreen wireless panel tests passed');
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
