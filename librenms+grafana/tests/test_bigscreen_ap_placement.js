const assert = require('assert');
const apModule = require('../bigscreen/ap-placement.js');
const { escapeHtml } = require('../bigscreen/utils.js');

assert.deepStrictEqual(
  Object.keys(apModule).sort(),
  ['createApPlacementPanel', 'formatAge', 'isPlacementEnabled', 'normalizeMac'],
  'the module exposes its factory and pure helpers'
);

assert.strictEqual(apModule.isPlacementEnabled('true'), true);
assert.strictEqual(apModule.isPlacementEnabled('1'), true);
assert.strictEqual(apModule.isPlacementEnabled('yes'), true);
assert.strictEqual(apModule.isPlacementEnabled('ON'), true);
assert.strictEqual(apModule.isPlacementEnabled('false'), false);
assert.strictEqual(apModule.isPlacementEnabled(''), false);
assert.strictEqual(apModule.normalizeMac('02-00-00-00-01-0A'), '02:00:00:00:01:0a');
assert.strictEqual(apModule.normalizeMac('02000000010a'), '02:00:00:00:01:0a');
assert.strictEqual(apModule.normalizeMac('not-a-mac'), '');
assert.strictEqual(apModule.formatAge(30), '30 秒');
assert.strictEqual(apModule.formatAge(10800), '3 小时');
assert.strictEqual(apModule.formatAge(null), '未知');

class FakeElement {
  constructor(id) {
    this.id = id;
    this.dataset = {};
    this.listeners = new Map();
    this.innerHTML = '';
    this.textContent = '';
    this.hidden = false;
    this.value = '';
  }

  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(handler);
  }

  dispatch(type) {
    (this.listeners.get(type) || []).forEach((handler) => handler.call(this, { type, target: this }));
  }
}

class FakeDocument {
  constructor() {
    this.hidden = false;
    this.elements = new Map();
    this.listeners = new Map();
    [
      'apPlacementSection', 'apPlacementStatus', 'apPlacementSummary',
      'apPlacementList', 'apPlacementSearch', 'apPlacementLevelFilter',
      'apPlacementRefresh'
    ].forEach((id) => this.elements.set(id, new FakeElement(id)));
    this.elements.get('apPlacementSection').hidden = true;
  }

  getElementById(id) {
    return this.elements.get(id) || null;
  }

  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(handler);
  }

  removeEventListener(type, handler) {
    const handlers = this.listeners.get(type) || [];
    this.listeners.set(type, handlers.filter((item) => item !== handler));
  }

  dispatch(type) {
    (this.listeners.get(type) || []).forEach((handler) => handler({ type }));
  }

  hasListener(type) {
    return (this.listeners.get(type) || []).length > 0;
  }
}

class FakeWindow {
  constructor() {
    this.intervalCount = 0;
    this.cleared = [];
    this.nextTimer = 1;
    this.timers = new Map();
  }

  setInterval(handler, delay) {
    const id = this.nextTimer++;
    this.intervalCount += 1;
    this.timers.set(id, { handler, delay });
    return id;
  }

  clearInterval(id) {
    this.cleared.push(id);
    this.timers.delete(id);
  }
}

// These fixtures model the P1 -> #142 -> B1 contract: confirmed-direct always
// carries online + operator-attested manual/direct evidence + matched FDB/exact
// + verified port; formal ap-attachments are a separate authoritative source and
// are intentionally not joined by B1.
function placement(index, level, online) {
  const confirmed = level === 'confirmed-direct';
  const observed = level === 'observed-attachment';
  const hasCurrent = confirmed || observed;
  const ambiguous = level === 'ambiguous';
  return {
    ap_name: `AP-${String(index).padStart(2, '0')}`,
    ap_ip: `10.20.0.${index}`,
    ap_mac: `02:00:00:00:01:${String(index).padStart(2, '0')}`,
    site: 'fixture',
    level,
    online,
    switch_ip: hasCurrent ? '10.0.0.11' : null,
    switch_ifindex: hasCurrent ? 10101 : null,
    switch_port: hasCurrent ? 'Gi1/0/1' : null,
    vlan: hasCurrent ? 40 : null,
    evidence_age_seconds: hasCurrent ? 30 : null,
    last_observed: hasCurrent ? null : {
      switch_ip: '10.0.0.9', switch_port: 'Gi1/0/9', vlan: 44,
      evidence_age_seconds: 600, validated_at: 1300
    },
    coverage_status: 'partial',
    reason: confirmed ? 'operator_confirmed_core_port'
      : observed ? 'verified_fdb_egress'
        : ambiguous ? 'ambiguous_fdb_candidates' : 'offline_no_current_location',
    evidence: {
      fdb: {
        status: hasCurrent ? 'matched' : ambiguous ? 'ambiguous' : 'no_candidate',
        raw_candidates: hasCurrent ? 1 : ambiguous ? 2 : 0,
        candidate_age_seconds: hasCurrent ? 10800 : ambiguous ? 10800 : null
      },
      exact_snmp: {
        status: hasCurrent ? 'matched' : ambiguous ? 'ambiguous' : 'not_attempted',
        gets: hasCurrent ? 1 : 0,
        validated_at: hasCurrent ? 1870 : null,
        validation_age_seconds: hasCurrent ? 30 : null
      },
      manual_confirmation: { status: confirmed ? 'confirmed' : 'none' },
      direct_neighbor: confirmed
        ? { status: 'operator-attested', source: 'manual-allowlist' }
        : { status: 'unknown', source: 'none' },
      online: { status: online, source: 'fixture', reason: 'fixture' },
      coverage: { status: 'partial', reasons: ['fdb_missing_switch'] },
      port: { verified: hasCurrent }
    }
  };
}

function fixturePayload(overrides = {}) {
  const placements = [];
  let index = 1;
  for (let count = 0; count < 8; count += 1) {
    placements.push(placement(index, 'confirmed-direct', 'online'));
    index += 1;
  }
  for (let count = 0; count < 2; count += 1) {
    placements.push(placement(index, 'observed-attachment', 'unknown'));
    index += 1;
  }
  placements.push(placement(index, 'unresolved', 'offline'));
  return {
    ok: true,
    schema_version: 1,
    generated_at: 1900.0,
    max_age_seconds: 7200,
    valid: true,
    file_age_seconds: 5,
    counts: {
      confirmed_direct: 8, observed_attachment: 2, ambiguous: 0, unresolved: 1,
      online: 8, offline: 1, unknown: 2, total: 11
    },
    coverage: {
      status: 'partial', expected_switches: 2, polled_switches: 2,
      missing_switches: 0, poll_failed_switches: 1,
      fdb_switches_usable: 1, fdb_switches_failed: 1,
      reasons: ['unresolved_switch']
    },
    placements,
    warnings: [],
    degraded: false,
    ...overrides
  };
}

function allUnknownPayload() {
  const placements = [];
  let index = 1;
  for (let count = 0; count < 10; count += 1) {
    placements.push(placement(index, 'observed-attachment', 'unknown'));
    index += 1;
  }
  placements.push(placement(index, 'ambiguous', 'unknown'));
  return fixturePayload({
    placements,
    counts: {
      confirmed_direct: 0, observed_attachment: 10, ambiguous: 1, unresolved: 0,
      online: 0, offline: 0, unknown: 11, total: 11
    }
  });
}

async function settle(rounds = 6) {
  for (let index = 0; index < rounds; index += 1) {
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
}

function createHarness(options = {}) {
  const doc = new FakeDocument();
  const win = new FakeWindow();
  const state = {
    authFailures: 0,
    fetches: 0,
    clock: 2000,
    auth: options.authenticated !== false,
    body: options.payload || fixturePayload(),
    error: options.error || null
  };
  const panel = apModule.createApPlacementPanel({
    document: doc,
    window: win,
    escapeHtml,
    fetchApPlacement: async () => {
      state.fetches += 1;
      if (typeof options.fetch === 'function') return options.fetch(state);
      if (state.error) throw state.error;
      return state.body;
    },
    canRead: async () => state.auth,
    enabled: options.enabled !== false,
    intervalMs: 30000,
    staleAfterMs: 75000,
    now: () => state.clock,
    onAuthFailure: () => { state.authFailures += 1; }
  });
  return { panel, doc, win, state };
}

function rowCount(doc) {
  const html = doc.getElementById('apPlacementList').innerHTML;
  return (html.match(/<article class="ap-placement-row/g) || []).length;
}

function rowHtml(doc, index) {
  const html = doc.getElementById('apPlacementList').innerHTML;
  return html.split('<article class="ap-placement-row')[index + 1] || '';
}

(async () => {
  {
    // Fixture contract: confirmed-direct is a P1 evidence level, not a formal
    // attachment; every confirmed row carries online + operator-attested
    // manual/direct + matched FDB/exact + verified port.
    const primary = fixturePayload();
    const confirmed = primary.placements.filter((item) => item.level === 'confirmed-direct');
    assert.strictEqual(confirmed.length, 8);
    confirmed.forEach((item) => {
      assert.strictEqual(item.online, 'online');
      assert.strictEqual(item.evidence.manual_confirmation.status, 'confirmed');
      assert.strictEqual(item.evidence.direct_neighbor.status, 'operator-attested');
      assert.strictEqual(item.evidence.direct_neighbor.source, 'manual-allowlist');
      assert.strictEqual(item.evidence.fdb.status, 'matched');
      assert.strictEqual(item.evidence.exact_snmp.status, 'matched');
      assert.strictEqual(item.evidence.port.verified, true);
    });
    const unknown = primary.placements.filter((item) => item.online === 'unknown');
    assert.ok(unknown.every((item) => item.level !== 'confirmed-direct'));
    const offline = primary.placements.find((item) => item.online === 'offline');
    assert.ok(offline && offline.level === 'unresolved'
      && offline.switch_ip === null && offline.last_observed);
  }

  {
    const { panel, doc, win } = createHarness({ enabled: false });
    panel.start();
    await settle(2);
    assert.strictEqual(doc.getElementById('apPlacementSection').hidden, true);
    assert.strictEqual(win.intervalCount, 0);
  }

  {
    const { panel, doc, win, state } = createHarness({ authenticated: false });
    panel.start();
    await settle();
    assert.strictEqual(state.fetches, 0, 'anonymous sessions must not request placement data');
    assert.strictEqual(doc.getElementById('apPlacementSection').hidden, true);
    assert.strictEqual(win.intervalCount, 1, 'authenticated check still owns one timer');
    panel.stop();
  }

  {
    const { panel, doc, win, state } = createHarness();
    panel.start();
    await settle();
    assert.strictEqual(state.fetches, 1);
    assert.strictEqual(doc.getElementById('apPlacementSection').hidden, false);
    assert.strictEqual(rowCount(doc), 11);
    const list = doc.getElementById('apPlacementList').innerHTML;
    const summary = doc.getElementById('apPlacementSummary').innerHTML;
    assert.ok(list.includes('已确认直连'));
    assert.ok(list.includes('已验证转发出口，物理直连未确认'));
    assert.ok(!list.includes('已确认物理直连'));
    assert.ok(list.includes('未知（无可靠心跳）'));
    assert.ok(list.includes('30 秒'));
    assert.ok(list.includes('3 小时'));
    assert.ok(summary.includes('不完整'));
    assert.ok(summary.includes('11'));
    assert.ok(list.includes('历史位置（非当前）'));
    assert.ok(rowHtml(doc, 10).split('<details')[0].includes('Gi1/0/1') === false);
    assert.ok(rowHtml(doc, 10).includes('10.0.0.9'));
    // confirmed rows must follow the P1/#142 contract: all online and carrying
    // operator-attested manual/direct evidence; unresolved/offline has no current port.
    for (let index = 0; index < 8; index += 1) {
      const confirmed = rowHtml(doc, index);
      assert.ok(confirmed.includes('在线'), 'confirmed-direct must be online');
      assert.ok(!confirmed.includes('>离线<'), 'confirmed-direct cannot be offline');
      assert.ok(confirmed.includes('operator-attested · manual-allowlist'));
      assert.ok(confirmed.includes('>confirmed<'));
    }
    assert.ok(rowHtml(doc, 10).includes('>离线<'));
    assert.ok(rowHtml(doc, 10).split('<details')[0].includes('>—<'));

    const search = doc.getElementById('apPlacementSearch');
    search.value = 'AP-03';
    search.dispatch('input');
    assert.strictEqual(rowCount(doc), 1);
    search.value = '';
    search.dispatch('input');
    const level = doc.getElementById('apPlacementLevelFilter');
    level.value = 'observed-attachment';
    level.dispatch('change');
    assert.strictEqual(rowCount(doc), 2);
    level.value = '';
    level.dispatch('change');
    assert.strictEqual(rowCount(doc), 11);

    panel.stop();
    assert.strictEqual(doc.getElementById('apPlacementSection').hidden, true);
    assert.strictEqual(panel.hasScheduledRefresh(), false);
  }

  {
    const { panel, doc, win } = createHarness();
    panel.start();
    panel.start();
    await settle();
    assert.strictEqual(win.intervalCount, 1, 'repeated start must not create a second interval');
    panel.stop();
    panel.start();
    await settle();
    assert.strictEqual(win.intervalCount, 2, 'a fresh start after stop gets exactly one new interval');
    panel.stop();
  }

  {
    const error = new Error('fixture unauthorized');
    error.status = 401;
    error.payload = { code: 'authentication_required' };
    const { panel, doc, state } = createHarness({ error });
    panel.start();
    await settle();
    assert.strictEqual(rowCount(doc), 0);
    assert.ok(doc.getElementById('apPlacementStatus').textContent.includes('登录状态已失效'));
    assert.strictEqual(state.authFailures, 1);
    panel.stop();
  }

  {
    const error = new Error('fixture unavailable');
    error.status = 503;
    error.payload = { code: 'ap_placement_stale' };
    const { panel, doc } = createHarness({ error });
    panel.start();
    await settle();
    assert.strictEqual(rowCount(doc), 0);
    assert.ok(doc.getElementById('apPlacementStatus').textContent.includes('ap_placement_stale'));
    panel.stop();
  }

  {
    const error = new Error('fixture timeout');
    const { panel, doc } = createHarness({ error });
    panel.start();
    await settle();
    assert.strictEqual(rowCount(doc), 0);
    assert.ok(doc.getElementById('apPlacementStatus').textContent.includes('请求失败'));
    panel.stop();
  }

  {
    let fail = true;
    const { panel, doc } = createHarness({
      fetch: async (state) => {
        if (fail) {
          fail = false;
          const error = new Error('fixture temporary');
          error.status = 503;
          error.payload = { code: 'ap_placement_unavailable' };
          throw error;
        }
        return fixturePayload();
      }
    });
    panel.start();
    await settle();
    assert.strictEqual(rowCount(doc), 0);
    await panel.refresh();
    await settle();
    assert.strictEqual(rowCount(doc), 11);
    assert.strictEqual(doc.getElementById('apPlacementSection').hidden, false);
    panel.stop();
  }

  {
    const duplicated = fixturePayload();
    duplicated.placements = duplicated.placements.concat([{
      ...duplicated.placements[0],
      ap_mac: '02-00-00-00-01-01',
      ap_ip: '10.20.0.99'
    }]);
    duplicated.counts = { ...duplicated.counts, total: 12 };
    const { panel, doc } = createHarness({ payload: duplicated });
    panel.start();
    await settle();
    assert.strictEqual(rowCount(doc), 11, 'duplicate normalized MACs must not create duplicate rows');
    assert.ok(doc.getElementById('apPlacementStatus').textContent.includes('重复'));
    panel.stop();
  }

  {
    const error = new Error('fixture forbidden');
    error.status = 403;
    error.payload = { code: 'authentication_required' };
    const { panel, doc, state } = createHarness({ error });
    panel.start();
    await settle();
    assert.strictEqual(rowCount(doc), 0);
    assert.ok(doc.getElementById('apPlacementStatus').textContent.includes('登录状态已失效'));
    assert.strictEqual(state.authFailures, 1);
    panel.stop();
  }

  {
    const { panel, doc } = createHarness({
      payload: fixturePayload({
        placements: [...fixturePayload().placements,
          placement(12, 'ambiguous', 'unknown')],
        counts: {
          confirmed_direct: 8, observed_attachment: 2, ambiguous: 1, unresolved: 1,
          online: 8, offline: 1, unknown: 3, total: 12
        }
      })
    });
    panel.start();
    await settle();
    assert.ok(doc.getElementById('apPlacementList').innerHTML.includes('存在多个候选或证据冲突'));
    panel.stop();
  }

  {
    const { panel, doc, state } = createHarness();
    panel.start();
    await settle();
    assert.strictEqual(rowCount(doc), 11);
    state.body = fixturePayload({
      placements: fixturePayload().placements.slice(0, 10),
      counts: {
        confirmed_direct: 8, observed_attachment: 2, ambiguous: 0, unresolved: 0,
        online: 8, offline: 0, unknown: 2, total: 10
      }
    });
    await panel.refresh();
    await settle();
    assert.strictEqual(rowCount(doc), 10, 'an AP removed from the snapshot disappears on refresh');
    panel.stop();
  }

  {
    const { panel, doc, state } = createHarness();
    panel.start();
    await settle();
    assert.strictEqual(rowCount(doc), 11);
    state.auth = false;
    await panel.refresh();
    await settle();
    assert.strictEqual(doc.getElementById('apPlacementSection').hidden, true);
    assert.strictEqual(rowCount(doc), 0, 'logout clears current positions immediately');
    panel.stop();
  }

  {
    let resolveFetch;
    const pending = new Promise((resolve) => { resolveFetch = resolve; });
    const { panel, doc, state } = createHarness({
      fetch: () => pending
    });
    panel.start();
    await settle();
    assert.strictEqual(state.fetches, 1);
    panel.refresh();
    await settle();
    assert.strictEqual(state.fetches, 1, 'only one placement request may be in flight');
    panel.stop();
    resolveFetch(fixturePayload());
    await settle();
    assert.strictEqual(doc.getElementById('apPlacementSection').hidden, true);
    assert.strictEqual(rowCount(doc), 0, 'late responses after stop are ignored');
  }

  {
    const { panel, doc, win, state } = createHarness();
    panel.start();
    await settle();
    assert.strictEqual(rowCount(doc), 11);
    doc.hidden = true;
    doc.dispatch('visibilitychange');
    assert.strictEqual(win.timers.size, 0, 'hidden tab pauses the refresh timer');
    doc.hidden = false;
    state.clock += 100000;
    doc.dispatch('visibilitychange');
    assert.ok(doc.getElementById('apPlacementStatus').textContent.includes('过期'));
    await settle();
    assert.strictEqual(rowCount(doc), 11);
    panel.stop();
  }

  {
    const { panel, doc } = createHarness();
    panel.start();
    await settle();
    assert.strictEqual(rowCount(doc), 11);
    panel.clear();
    assert.strictEqual(doc.getElementById('apPlacementSection').hidden, true);
    assert.strictEqual(rowCount(doc), 0);
    panel.stop();
  }

  {
    const invalidCases = {
      'ok=false': { ok: false },
      'valid=false': { valid: false },
      'schema=2': { schema_version: 2 },
      'schema="1"': { schema_version: '1' },
      'file age over max': { file_age_seconds: 7201 },
      'file age string': { file_age_seconds: '5' },
      'generated string': { generated_at: '1900' },
      'max age zero': { max_age_seconds: 0 },
      'counts null': { counts: null },
      'placements object': { placements: {} }
    };
    for (const [label, overrides] of Object.entries(invalidCases)) {
      const { panel, doc } = createHarness({
        payload: fixturePayload(overrides)
      });
      panel.start();
      await settle();
      assert.strictEqual(rowCount(doc), 0, `${label} must not render current positions`);
      assert.ok(doc.getElementById('apPlacementStatus').textContent.includes('不可用'),
        `${label} must show an unavailable state`);
      panel.stop();
    }
    // A normal valid envelope still renders.
    const { panel, doc } = createHarness();
    panel.start();
    await settle();
    assert.strictEqual(rowCount(doc), 11);
    panel.stop();
  }

  {
    // Local display expiry: a payload already close to max age must not stay
    // on screen after the UI's own elapsed-time guard fires.
    const nearExpiry = fixturePayload({
      file_age_seconds: 7199, max_age_seconds: 7200
    });
    const { panel, doc, state } = createHarness({ payload: nearExpiry });
    panel.start();
    await settle();
    assert.strictEqual(rowCount(doc), 11);
    state.clock += 5000;
    let release;
    state.body = new Promise((resolve) => { release = resolve; });
    panel.refresh();
    await settle(2);
    assert.strictEqual(rowCount(doc), 0, 'locally expired data must be cleared');
    panel.stop();
    release(fixturePayload());
    await settle(1);
  }

  {
    // All online=unknown is the typical current P1 state and must never show a
    // confirmed-direct row.
    const { panel, doc } = createHarness({ payload: allUnknownPayload() });
    panel.start();
    await settle();
    assert.strictEqual(rowCount(doc), 11);
    const list = doc.getElementById('apPlacementList').innerHTML;
    assert.ok(!list.includes('已确认直连'));
    assert.ok(list.includes('已验证转发出口，物理直连未确认'));
    assert.ok(list.includes('未知（无可靠心跳）'));
    panel.stop();
  }

  {
    // P1-1: a pending request from a previous page life must not block the new
    // life's first read, and its late result must not overwrite the new result.
    let calls = 0;
    const pending = [];
    const { panel, doc, state } = createHarness({
      fetch: () => new Promise((resolve, reject) => {
        calls += 1;
        pending.push({ resolve, reject });
      })
    });
    panel.start();
    await settle();
    assert.strictEqual(state.fetches, 1);
    panel.stop();
    panel.start();
    await settle();
    assert.strictEqual(state.fetches, 2, 'new page life must request immediately');
    pending[1].resolve(fixturePayload({
      placements: fixturePayload().placements.slice(0, 10),
      counts: {
        confirmed_direct: 8, observed_attachment: 2, ambiguous: 0, unresolved: 0,
        online: 8, offline: 0, unknown: 2, total: 10
      }
    }));
    await settle();
    assert.strictEqual(rowCount(doc), 10);
    pending[0].resolve(fixturePayload());
    await settle();
    assert.strictEqual(rowCount(doc), 10, 'old late success must not overwrite the new result');
    panel.stop();
  }

  {
    // P1-1: a late failure from the old life must not clear the new result.
    const pending = [];
    const { panel, doc } = createHarness({
      fetch: () => new Promise((resolve, reject) => {
        pending.push({ resolve, reject });
      })
    });
    panel.start();
    await settle();
    panel.stop();
    panel.start();
    await settle();
    pending[1].resolve(fixturePayload());
    await settle();
    assert.strictEqual(rowCount(doc), 11);
    const oldError = new Error('old request failed');
    oldError.status = 503;
    oldError.payload = { code: 'ap_placement_unavailable' };
    pending[0].reject(oldError);
    await settle();
    assert.strictEqual(rowCount(doc), 11, 'old late failure must not clear the new result');
    panel.stop();
  }

  {
    // P1-1: the old request's finally must not release the new life's lock.
    const pending = [];
    const { panel, doc, state } = createHarness({
      fetch: () => new Promise((resolve, reject) => {
        pending.push({ resolve, reject });
      })
    });
    panel.start();
    await settle();
    panel.stop();
    panel.start();
    await settle();
    assert.strictEqual(state.fetches, 2);
    pending[0].resolve(fixturePayload());
    await settle();
    panel.refresh();
    await settle();
    assert.strictEqual(state.fetches, 2, 'new life keeps its single-flight lock');
    pending[1].resolve(fixturePayload());
    await settle();
    assert.strictEqual(rowCount(doc), 11);
    panel.stop();
  }

  {
    // P1-1: logout->login (clear->refresh) can read again immediately.
    const pending = [];
    const { panel, doc, state } = createHarness({
      fetch: () => new Promise((resolve, reject) => {
        pending.push({ resolve, reject });
      })
    });
    panel.start();
    await settle();
    assert.strictEqual(state.fetches, 1);
    panel.clear();
    const loginRefresh = panel.refresh();
    await settle();
    assert.strictEqual(state.fetches, 2, 'login after logout must read immediately');
    pending[1].resolve(fixturePayload());
    await loginRefresh;
    await settle();
    assert.strictEqual(rowCount(doc), 11);
    pending[0].resolve(fixturePayload());
    await settle();
    assert.strictEqual(rowCount(doc), 11);
    panel.stop();
  }

  console.log('test_bigscreen_ap_placement: ok');
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
