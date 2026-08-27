const assert = require('assert');
const iperfControllerModule = require('../bigscreen/control/iperf-controller.js');
const iperf = require('../bigscreen/iperf.js');
const utils = require('../bigscreen/utils.js');

assert.deepStrictEqual(
  Object.keys(iperfControllerModule),
  ['createIperfController'],
  'the iPerf controller exposes only its dependency-injected factory'
);

class FakeElement {
  constructor(id = '') {
    this.id = id;
    this.dataset = {};
    this.listeners = new Map();
    this.value = '';
    this.innerHTML = '';
    this.textContent = '';
    this.className = '';
    this.hidden = false;
    this.disabled = false;
    this.readOnly = false;
    this.placeholder = '';
    this.style = {};
    this.focusCount = 0;
    this.attributes = new Map();
    this.insertions = [];
    this.progressTrack = null;
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

  insertAdjacentHTML(position, html) {
    this.insertions.push({ position, html });
    this.innerHTML += html;
  }

  querySelector(selector) {
    return selector === '[role=progressbar]' ? this.progressTrack : null;
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  focus() {
    this.focusCount += 1;
  }
}

class FakeDocument {
  constructor() {
    this.elements = new Map();
    [
      'iperfPreset',
      'iperfPublicServer',
      'iperfServer',
      'iperfPorts',
      'iperfDuration',
      'iperfParallel',
      'iperfDirection',
      'iperfPresetHint',
      'iperfRunBtn',
      'iperfStopBtn',
      'iperfConfirm',
      'iperfConfirmSummary',
      'iperfConfirmBtn',
      'iperfCancelBtn',
      'iperfProgress',
      'iperfProgressPhase',
      'iperfProgressElapsed',
      'iperfProgressFill',
      'iperfProgressDetail',
      'iperfResult',
      'iperfHistory'
    ].forEach((id) => this.elements.set(id, new FakeElement(id)));
    this.getElementById('iperfPreset').value = 'hongkong';
    this.getElementById('iperfPublicServer').value = '0';
    this.getElementById('iperfDuration').value = '10';
    this.getElementById('iperfParallel').value = '10';
    this.getElementById('iperfDirection').value = 'both';
    this.getElementById('iperfStopBtn').hidden = true;
    this.getElementById('iperfConfirm').hidden = true;
    this.getElementById('iperfProgress').hidden = true;
    this.getElementById('iperfResult').hidden = true;
    this.getElementById('iperfProgress').progressTrack = new FakeElement('iperfProgressTrack');
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

function runningStatus(overrides = {}) {
  return {
    state: 'running',
    phase: 'upload',
    elapsedSeconds: 2,
    maxSeconds: 60,
    percent: 3,
    message: '测速进行中',
    ...overrides
  };
}

function completeStatus(state = 'complete') {
  if (state !== 'complete') {
    return { state, phase: state, message: `${state} fixture` };
  }
  return {
    state: 'complete',
    phase: 'complete',
    ok: true,
    taskId: 'task-complete',
    server: 'hk.example',
    duration: 10,
    parallel: 10,
    results: []
  };
}

function defaultPresets() {
  return {
    hongkong: {
      note: '香港 fixture',
      servers: [{ label: 'HK-A', server: 'hk.example', ports: '5201-5202' }]
    },
    custom: iperf.DEFAULT_CUSTOM_PRESET
  };
}

function createHarness(options = {}) {
  const document = new FakeDocument();
  const container = new FakeElement('controlDelivery');
  const intervals = new Map();
  const clearedIntervals = [];
  let nextInterval = 1;
  const storage = new Map(Object.entries(options.storage || {}));
  const window = {
    sessionStorage: {
      getItem(key) { return storage.has(key) ? storage.get(key) : null; },
      setItem(key, value) { storage.set(key, String(value)); },
      removeItem(key) { storage.delete(key); }
    },
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
  const postCalls = [];
  const statusCalls = [];
  const historyCalls = [];
  const serverConfigCalls = [];
  const startQueue = [...(options.startQueue || [{ taskId: 'task-1' }])];
  const stopQueue = [...(options.stopQueue || [{ ok: true }])];
  const statusQueue = [...(options.statusQueue || [runningStatus()])];
  const historyQueue = [...(options.historyQueue || [{ history: [] }])];

  function take(queue, fallback, ...args) {
    const item = queue.length ? queue.shift() : fallback;
    if (typeof item === 'function') return item(...args);
    if (item instanceof Error) return Promise.reject(item);
    return Promise.resolve(item);
  }

  const controller = iperfControllerModule.createIperfController({
    document,
    window,
    fetch: () => Promise.resolve({ ok: true }),
    escapeHtml: utils.escapeHtml,
    postPlatform(path, payload, requestOptions) {
      postCalls.push({ path, payload, options: requestOptions });
      return path.endsWith('/stop')
        ? take(stopQueue, { ok: true }, path, payload)
        : take(startQueue, { taskId: 'task-1' }, path, payload);
    },
    fetchIperfStatus(taskId) {
      statusCalls.push(taskId);
      return take(statusQueue, runningStatus(), taskId);
    },
    fetchIperfHistory() {
      historyCalls.push(true);
      return take(historyQueue, { history: [] });
    },
    defaultCustomPreset: iperf.DEFAULT_CUSTOM_PRESET,
    resultView: iperf.resultView,
    historyHtml: iperf.historyHtml,
    loadServerConfig() {
      serverConfigCalls.push(true);
      return Promise.resolve({ presets: defaultPresets(), verifiedAt: '2026-08-28' });
    },
    presetView: iperf.presetView
  });

  return {
    controller,
    container,
    document,
    window,
    intervals,
    clearedIntervals,
    storage,
    postCalls,
    statusCalls,
    historyCalls,
    serverConfigCalls
  };
}

async function mount(harness) {
  harness.controller.ensureMounted(harness.container);
  await settle();
}

async function openConfirmation(harness) {
  await harness.document.getElementById('iperfRunBtn').dispatch('click');
  assert.strictEqual(harness.document.getElementById('iperfConfirm').hidden, false);
}

async function confirmRun(harness) {
  await openConfirmation(harness);
  await harness.document.getElementById('iperfConfirmBtn').dispatch('click');
  await settle();
}

async function main() {
  // First mount owns all iPerf markup and binding. Repeated Delivery renders
  // reuse the same controller object without rebuilding DOM or state.
  const mounted = createHarness();
  await mount(mounted);
  assert.strictEqual(mounted.container.insertions.length, 1);
  assert.strictEqual(mounted.container.insertions[0].position, 'beforeend');
  assert.match(mounted.container.insertions[0].html, /id="iperfPreset"/);
  assert.match(mounted.container.insertions[0].html, /id="iperfProgress"/);
  assert.strictEqual(mounted.serverConfigCalls.length, 1);
  assert.strictEqual(mounted.historyCalls.length, 1);
  assert.strictEqual(mounted.document.getElementById('iperfRunBtn').listenerCount('click'), 1);
  assert.strictEqual(mounted.document.getElementById('iperfStopBtn').listenerCount('click'), 1);
  assert.strictEqual(mounted.document.getElementById('iperfHistory').listenerCount('click'), 1);
  mounted.controller.ensureMounted(mounted.container);
  await settle();
  assert.strictEqual(mounted.container.insertions.length, 1);
  assert.strictEqual(mounted.serverConfigCalls.length, 1);
  assert.strictEqual(mounted.historyCalls.length, 1);
  assert.strictEqual(mounted.intervals.size, 0);
  assert.strictEqual(mounted.document.getElementById('iperfServer').value, 'hk.example');
  assert.strictEqual(mounted.document.getElementById('iperfPorts').value, '5201-5202');

  // Run opens the existing confirmation. Cancel only discards the pending
  // start and never sends a request or touches active task tracking.
  await openConfirmation(mounted);
  assert.match(mounted.document.getElementById('iperfConfirmSummary').textContent, /正常约 20 秒/);
  await mounted.document.getElementById('iperfCancelBtn').dispatch('click');
  assert.strictEqual(mounted.document.getElementById('iperfConfirm').hidden, true);
  await mounted.document.getElementById('iperfConfirmBtn').dispatch('click');
  await settle();
  assert.strictEqual(mounted.postCalls.length, 0);

  // A successful start preserves payload, storage, the immediate status read,
  // and the exact 500 ms polling cadence.
  const success = createHarness({ statusQueue: [runningStatus()] });
  await mount(success);
  await confirmRun(success);
  assert.deepStrictEqual(success.postCalls[0], {
    path: '/network/iperf3',
    payload: {
      server: 'hk.example',
      ports: '5201-5202',
      duration: '10',
      parallel: '10',
      direction: 'both'
    },
    options: { timeoutMs: 10000 }
  });
  assert.strictEqual(success.storage.get('bigscreen.iperfTaskId'), 'task-1');
  assert.deepStrictEqual(success.statusCalls, ['task-1']);
  assert.strictEqual(success.intervals.size, 1);
  assert.strictEqual([...success.intervals.values()][0].delay, 500);
  assert.strictEqual(success.document.getElementById('iperfRunBtn').disabled, true);
  assert.strictEqual(success.document.getElementById('iperfStopBtn').hidden, false);
  const activeTimer = [...success.intervals.keys()][0];
  success.controller.ensureMounted(success.container);
  assert.strictEqual([...success.intervals.keys()][0], activeTimer, 'SPA re-entry must keep the active timer');
  assert.strictEqual(success.document.getElementById('iperfRunBtn').listenerCount('click'), 1);

  // A pending status call blocks the next interval tick, then releases the
  // concurrency guard after the request settles.
  const pendingStatus = deferred();
  const concurrency = createHarness({
    statusQueue: [() => pendingStatus.promise, runningStatus()]
  });
  await mount(concurrency);
  await concurrency.document.getElementById('iperfRunBtn').dispatch('click');
  await concurrency.document.getElementById('iperfConfirmBtn').dispatch('click');
  await settle();
  assert.strictEqual(concurrency.statusCalls.length, 1);
  const concurrencyTimer = [...concurrency.intervals.values()][0];
  await concurrencyTimer.handler();
  assert.strictEqual(concurrency.statusCalls.length, 1, 'overlapping status reads remain suppressed');
  pendingStatus.resolve(runningStatus());
  await settle();
  await concurrencyTimer.handler();
  assert.strictEqual(concurrency.statusCalls.length, 2);

  // HTTP 409 with an existing task reconnects; an invalid 409 remains the
  // existing ordinary error presentation and does not start polling.
  const conflict = new Error('already running');
  conflict.status = 409;
  conflict.payload = { taskId: 'task-existing' };
  const reconnect = createHarness({ startQueue: [conflict], statusQueue: [runningStatus()] });
  await mount(reconnect);
  await confirmRun(reconnect);
  assert.strictEqual(reconnect.storage.get('bigscreen.iperfTaskId'), 'task-existing');
  assert.deepStrictEqual(reconnect.statusCalls, ['task-existing']);
  assert.strictEqual(reconnect.intervals.size, 1);
  assert.match(reconnect.document.getElementById('iperfResult').textContent, /已连接到该任务/);

  const invalidConflict = new Error('conflict without task');
  invalidConflict.status = 409;
  invalidConflict.payload = {};
  const rejectedConflict = createHarness({ startQueue: [invalidConflict] });
  await mount(rejectedConflict);
  await confirmRun(rejectedConflict);
  assert.strictEqual(rejectedConflict.intervals.size, 0);
  assert.strictEqual(rejectedConflict.storage.has('bigscreen.iperfTaskId'), false);
  assert.strictEqual(rejectedConflict.document.getElementById('iperfResult').textContent, '测速失败：conflict without task');

  // The backend queued state is non-terminal just like running: active task,
  // storage, and the polling interval all remain in place.
  const queued = createHarness({
    statusQueue: [runningStatus({ state: 'queued', phase: 'preparing', message: '等待执行' })]
  });
  await mount(queued);
  await confirmRun(queued);
  assert.strictEqual(queued.storage.get('bigscreen.iperfTaskId'), 'task-1');
  assert.strictEqual(queued.intervals.size, 1);
  assert.strictEqual(queued.document.getElementById('iperfProgress').className, 'iperf-progress queued');

  // All backend terminal states clear task/storage/timer only after status says
  // the task is terminal, and render the original result semantics.
  for (const state of ['complete', 'failed', 'cancelled']) {
    const terminal = createHarness({ statusQueue: [completeStatus(state)] });
    await mount(terminal);
    await confirmRun(terminal);
    assert.strictEqual(terminal.intervals.size, 0, `${state} must stop polling`);
    assert.strictEqual(terminal.storage.has('bigscreen.iperfTaskId'), false, `${state} must clear storage`);
    assert.strictEqual(terminal.document.getElementById('iperfRunBtn').disabled, false);
    assert.strictEqual(terminal.document.getElementById('iperfStopBtn').hidden, true);
    assert.strictEqual(terminal.document.getElementById('iperfResult').hidden, false);
  }

  // Transient unavailable status deliberately retains tracking and polling;
  // explicit missing/expired status performs the existing failed cleanup.
  const transient = createHarness({
    statusQueue: [{ state: 'unavailable', error: '服务暂时不可用' }]
  });
  await mount(transient);
  await confirmRun(transient);
  assert.strictEqual(transient.storage.get('bigscreen.iperfTaskId'), 'task-1');
  assert.strictEqual(transient.intervals.size, 1);
  assert.strictEqual(transient.document.getElementById('iperfRunBtn').disabled, true);

  for (const error of ['任务不存在', '任务已过期']) {
    const expired = createHarness({ statusQueue: [{ state: 'unavailable', error }] });
    await mount(expired);
    await confirmRun(expired);
    assert.strictEqual(expired.storage.has('bigscreen.iperfTaskId'), false);
    assert.strictEqual(expired.intervals.size, 0);
    assert.strictEqual(expired.document.getElementById('iperfResult').textContent, `测速失败：${error}`);
  }

  // Stop only posts the request. Polling and task storage stay active until a
  // later cancelled status performs cleanup.
  const stopping = createHarness({
    statusQueue: [runningStatus(), completeStatus('cancelled')]
  });
  await mount(stopping);
  await confirmRun(stopping);
  const stoppingTimer = [...stopping.intervals.values()][0];
  await stopping.document.getElementById('iperfStopBtn').dispatch('click');
  assert.deepStrictEqual(stopping.postCalls[1], {
    path: '/network/iperf3/stop',
    payload: { taskId: 'task-1' },
    options: { timeoutMs: 5000 }
  });
  assert.strictEqual(stopping.intervals.size, 1);
  assert.strictEqual(stopping.storage.get('bigscreen.iperfTaskId'), 'task-1');
  assert.strictEqual(stopping.document.getElementById('iperfProgressDetail').textContent, '正在停止测速进程……');
  await stoppingTimer.handler();
  await settle();
  assert.strictEqual(stopping.intervals.size, 0);
  assert.strictEqual(stopping.storage.has('bigscreen.iperfTaskId'), false);

  // Session restore occurs only when the controller is first mounted by the
  // authenticated Delivery render, and repeated mount does not restore twice.
  const restored = createHarness({
    storage: { 'bigscreen.iperfTaskId': 'task-restored' },
    statusQueue: [runningStatus()]
  });
  assert.strictEqual(restored.statusCalls.length, 0);
  assert.strictEqual(restored.intervals.size, 0);
  await mount(restored);
  assert.deepStrictEqual(restored.statusCalls, ['task-restored']);
  assert.strictEqual(restored.intervals.size, 1);
  const restoredTimer = [...restored.intervals.keys()][0];
  restored.controller.ensureMounted(restored.container);
  await settle();
  assert.deepStrictEqual(restored.statusCalls, ['task-restored']);
  assert.strictEqual([...restored.intervals.keys()][0], restoredTimer);

  // Viewing history never takes ownership of the active task or its timer.
  const historyView = createHarness({
    statusQueue: [runningStatus(), completeStatus()],
    historyQueue: [{ history: [{ taskId: 'task-old', server: 'old.example', state: 'complete' }] }]
  });
  await mount(historyView);
  await confirmRun(historyView);
  const historyTimer = [...historyView.intervals.keys()][0];
  const historyButton = { dataset: { taskId: 'task-old' } };
  await historyView.document.getElementById('iperfHistory').dispatch('click', {
    target: { closest: () => historyButton }
  });
  assert.deepStrictEqual(historyView.statusCalls, ['task-1', 'task-old']);
  assert.strictEqual(historyView.storage.get('bigscreen.iperfTaskId'), 'task-1');
  assert.strictEqual([...historyView.intervals.keys()][0], historyTimer);

  // Existing error presentations remain unchanged for start, status, stop,
  // and history detail failures; none silently terminates an active task.
  const startFailure = createHarness({ startQueue: [new Error('start broke')] });
  await mount(startFailure);
  await confirmRun(startFailure);
  assert.strictEqual(startFailure.document.getElementById('iperfResult').textContent, '测速失败：start broke');
  assert.strictEqual(startFailure.intervals.size, 0);

  const statusFailure = createHarness({ statusQueue: [runningStatus(), new Error('status broke'), runningStatus()] });
  await mount(statusFailure);
  await confirmRun(statusFailure);
  const statusTimer = [...statusFailure.intervals.values()][0];
  await assert.rejects(statusTimer.handler(), /status broke/);
  assert.strictEqual(statusFailure.storage.get('bigscreen.iperfTaskId'), 'task-1');
  assert.strictEqual(statusFailure.intervals.size, 1);
  await statusTimer.handler();
  assert.strictEqual(statusFailure.statusCalls.length, 3, 'status failure must release the concurrency guard');

  const stopFailure = createHarness({ statusQueue: [runningStatus()], stopQueue: [new Error('stop broke')] });
  await mount(stopFailure);
  await confirmRun(stopFailure);
  await stopFailure.document.getElementById('iperfStopBtn').dispatch('click');
  assert.strictEqual(stopFailure.document.getElementById('iperfProgressDetail').textContent, '停止失败：stop broke');
  assert.strictEqual(stopFailure.intervals.size, 1);
  assert.strictEqual(stopFailure.storage.get('bigscreen.iperfTaskId'), 'task-1');

  const historyFailure = createHarness({ statusQueue: [runningStatus(), new Error('history broke')] });
  await mount(historyFailure);
  await confirmRun(historyFailure);
  await historyFailure.document.getElementById('iperfHistory').dispatch('click', {
    target: { closest: () => ({ dataset: { taskId: 'task-old' } }) }
  });
  assert.strictEqual(historyFailure.document.getElementById('iperfResult').textContent, '测速失败：history broke');
  assert.strictEqual(historyFailure.storage.get('bigscreen.iperfTaskId'), 'task-1');
  assert.strictEqual(historyFailure.intervals.size, 1);

  console.log('bigscreen iperf controller tests passed');
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
