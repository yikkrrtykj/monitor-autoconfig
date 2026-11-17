const test = require("node:test");
const assert = require("node:assert/strict");
const { webcrypto } = require("node:crypto");
const { createClient } = require("../bigscreen/ping/custom-api.js");
const custom = require("../bigscreen/ping/custom-model.js");
const model = require("../bigscreen/ping/model.js");
const history = require("../bigscreen/ping/history.js");

const id = "65a8c647-c103-4e45-89bf-06b107bf531a";
const otherId = "4f1c8d1e-6f6b-4a0f-8a4d-9c4d2e1f7b21";
const row = { id, name: "转播", group: "Remote", ip: "192.0.2.17", enabled: true, revision: 1,
  createdAt: "2026-11-17T09:00:00Z", updatedAt: "2026-11-17T09:00:00Z" };
const manifest = (revision, targets) => ({ ok: true, revision, targets, publication: "published", publishedRevision: revision });
const discovery = (revision, targets) => ({ ok: true, revision, publishedRevision: revision, discoveredRevision: revision,
  activeTargets: targets.map(({ id, ip, revision }) => ({ id, ip, revision, scrapeHealth: "up" })) });
const settings = model.policy();
const settle = async () => {
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
};

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((ok, no) => { resolve = ok; reject = no; });
  return { promise, resolve, reject };
}

// Every request is controlled by the test so pause/resume timing and late
// responses are deterministic without touching the browser clock.
function harness({ clock = () => 100, visible = () => true } = {}) {
  const sent = [];
  const latches = { list: [], discovery: [], sample: [] };
  const client = {
    list(signal) { const latch = { ...deferred(), signal }; latches.list.push(latch); return latch.promise; },
    discovery(signal) { const latch = { ...deferred(), signal }; latches.discovery.push(latch); return latch.promise; },
  };
  const sample = (at, signal) => { const latch = { ...deferred(), at, signal }; latches.sample.push(latch); return latch.promise; };
  const monitor = custom.createMonitor({ client, sample, settings, changed: (view) => sent.push(view), visible, now: clock });
  return { monitor, sent, latches };
}

const reading = (item, values) => new Map([[item.key, { ...item, ...values }]]);
const fresh = (at, status = "UP") => ({ status, sourceAt: at - 1, rtt: 12, loss: model.lossStats(30, 30, 60, 2) });

test("accepted deletion is immediate even on 202 and older reads cannot restore it", () => {
  const store = custom.createState();
  assert.equal(store.read(manifest(0, [])), true);
  assert.equal(store.read(manifest(1, [row])), true);
  store.save({ id, savedRevision: 2, revision: 2, target: null, publication: "pending", publishedRevision: null });
  assert.equal(store.read(manifest(1, [row])), false);
  assert.deepEqual(store.current().targets, []);
  store.save({ id, savedRevision: 1, revision: 2, target: row, publication: "published", publishedRevision: 2 });
  assert.deepEqual(store.current().targets, []);
  assert.equal(store.read(manifest(2, [])), true);
});

test("identity is stable across metadata edits and deleted deep links survive", () => {
  const first = custom.target(row, settings);
  const rename = custom.target({ ...row, name: "节目", group: "Other", revision: 2 }, settings);
  const move = custom.target({ ...row, ip: "192.0.2.18", revision: 3 }, settings);
  assert.equal(first.key, rename.key);
  assert.notEqual(first.key, move.key);
  assert.equal(history.parseTarget(first.key, settings).ip, row.ip);
  assert.equal(history.parseTarget(move.key, settings).metric.custom_id, id);
});

test("discovery revision, exact endpoint, successful scrape and fresh samples gate status", () => {
  const at = Date.parse(row.updatedAt) / 1000 + 70;
  const base = custom.target(row, settings);
  const readings = (values) => new Map([[base.key, { ...base, ...values }]]);
  const atSnapshot = manifest(1, [row]);
  const output = (seen, data) => custom.present(atSnapshot, seen, data, settings)[0];
  const fresh = { status: "DOWN", sourceAt: at, loss: model.lossStats(30, 28, 60, 2) };
  assert.equal(output(discovery(1, [row]), readings(fresh)).status, "DOWN");
  assert.equal(output(discovery(1, [row]), readings({ ...fresh, sourceAt: at - 80 })).status, "UNKNOWN");
  assert.equal(output(discovery(1, [row]), readings({ ...fresh, status: "UNKNOWN", sourceAt: null })).status, "UNKNOWN");
  assert.equal(output(discovery(1, [{ ...row, revision: 2 }]), readings(fresh)).status, "UNKNOWN");
  assert.equal(output(discovery(2, [row]), readings(fresh)).status, "UNKNOWN");
  assert.equal(output(discovery(1, [row]), new Map()).sync, "待首次探测");
});

test("API preserves UUID, body and If-Match on retry and reports B1 error codes", async () => {
  const seen = [];
  const client = createClient({ crypto: webcrypto, fetch: async (path, options) => {
    seen.push({ path, ...options });
    if (seen.length === 1) throw new TypeError("offline");
    if (seen.length === 2) return new Response(JSON.stringify({ ok: true, id, target: row, savedRevision: 1,
      ...manifest(1, [row]), replayed: true }), { status: 202 });
    return new Response('{"ok":false,"error":"custom_ping_revision_conflict","currentRevision":2}', { status: 412 });
  } });
  const op = client.operation("PATCH", row, { enabled: false });
  await assert.rejects(client.write(op, "token"), { code: "network" });
  const receipt = await client.write(op, "other-token");
  assert.equal(receipt.replayed, true);
  assert.match(op.key, /^[a-f0-9-]{14}4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/);
  assert.equal(seen[0].headers["Idempotency-Key"], seen[1].headers["Idempotency-Key"]);
  assert.equal(seen[1].headers["If-Match"], '"1"');
  assert.equal(seen[0].body, seen[1].body);
  assert.equal(seen[0].headers.Origin, undefined);
  assert.equal(seen[0].credentials, "same-origin");
  await assert.rejects(client.write(op, "token"), { status: 412, code: "custom_ping_revision_conflict" });
});


test("timeout is classified, with the logical request reusable after transport abort", async () => {
  const client = createClient({
    crypto: { getRandomValues: webcrypto.getRandomValues.bind(webcrypto) },
    timeout: 15,
    fetch: (_path, { signal }) => new Promise((_resolve, reject) => {
      signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
    })
  });
  const operation = client.operation("DELETE", row);
  assert.match(operation.key, /^[a-f0-9-]{14}4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/);
  await assert.rejects(client.write(operation, "csrf"), { code: "timeout" });
  assert.equal(operation.body, undefined);
  assert.equal(operation.version, '"1"');
});

test("monitor drops a late pre-deletion read and keeps the last good list on failure", async () => {
  const sent = [];
  const requested = [];
  let found = { ...discovery(2, [row]), discoveredRevision: null };
  const monitor = custom.createMonitor({
    client: {
      list: () => new Promise((resolve, reject) => requested.push({ resolve, reject })),
      discovery: async () => found,
    },
    sample: async () => new Map(), changed: (view) => sent.push(view),
    settings, visible: () => true, now: () => 100,
  });
  try {
    monitor.start();
    monitor.saved({ id, target: null, savedRevision: 2, revision: 2, publication: "pending", publishedRevision: null });
    assert.equal(sent.at(-1).sync, "待发布");
    requested[0].resolve(manifest(1, [row]));
    requested[1].resolve(manifest(2, []));
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(sent.at(-1).catalog.revision, 2);
    assert.deepEqual(sent.at(-1).items, []);
    assert.equal(sent.at(-1).sync, "待发现");
    found = discovery(2, []);
    const converged = monitor.refresh();
    requested[2].resolve(manifest(2, []));
    assert.equal(await converged, true);
    assert.equal(sent.at(-1).sync, "已发现");
    const failure = monitor.refresh();
    requested[3].reject(new Error("503"));
    assert.equal(await failure, false);
    assert.match(sent.at(-1).notice, /清单暂不可用/);
    assert.equal(sent.at(-1).catalog.revision, 2);
  } finally { monitor.stop(); }
});

test("expired cached UP becomes UNKNOWN on resume before the stalled read completes", async () => {
  let clock = Date.parse(row.updatedAt) / 1000 + 70;
  let shown = true;
  const h = harness({ clock: () => clock, visible: () => shown });
  const item = custom.target(row, settings);
  try {
    h.monitor.start();
    h.latches.list[0].resolve(manifest(1, [row]));
    h.latches.discovery[0].resolve(discovery(1, [row]));
    await settle();
    h.latches.sample[0].resolve(reading(item, fresh(clock)));
    await settle();
    assert.equal(h.sent.at(-1).items[0].status, "UP");
    assert.equal(h.sent.at(-1).sync, "已发现");

    // The tab was hidden or /ping was left long enough for the last sample to expire.
    clock += 31;
    shown = false;
    h.monitor.pause();
    shown = true;
    const resuming = h.monitor.refresh();
    // The retired status is rendered synchronously, before any new request settles.
    const expired = h.sent.at(-1);
    assert.equal(expired.items[0].status, "UNKNOWN");
    assert.equal(expired.items[0].reason, "数据过期");
    assert.equal(expired.items[0].sourceAt, null);
    assert.equal(expired.catalog.targets.length, 1);
    assert.equal(expired.sync, "已发现");
    assert.equal(h.latches.list.length, 2);
    assert.equal(h.latches.sample.length, 1);

    // A delayed or failing authority read cannot restore the cached UP.
    h.latches.list[1].reject(new Error("503"));
    h.latches.discovery[1].resolve(discovery(1, [row]));
    assert.equal(await resuming, false);
    assert.equal(h.sent.at(-1).items[0].status, "UNKNOWN");
    assert.match(h.sent.at(-1).notice, /清单暂不可用/);
    assert.equal(h.sent.at(-1).catalog.targets.length, 1);

    // Only a fresh successful ICMP sample may become UP again.
    h.monitor.refresh();
    h.latches.list[2].resolve(manifest(1, [row]));
    h.latches.discovery[2].resolve(discovery(1, [row]));
    await settle();
    h.latches.sample.at(-1).resolve(reading(item, fresh(clock)));
    await settle();
    assert.equal(h.sent.at(-1).items[0].status, "UP");
    assert.equal(h.sent.at(-1).notice, "");
  } finally { h.monitor.stop(); }
});

test("stale samples held across a hidden/unhidden cycle never flash UP while pending", async () => {
  let clock = Date.parse(row.updatedAt) / 1000 + 70;
  let shown = true;
  const h = harness({ clock: () => clock, visible: () => shown });
  const item = custom.target(row, settings);
  try {
    h.monitor.start();
    h.latches.list[0].resolve(manifest(1, [row]));
    h.latches.discovery[0].resolve(discovery(1, [row]));
    await settle();
    h.latches.sample[0].resolve(reading(item, fresh(clock)));
    await settle();
    assert.equal(h.sent.at(-1).items[0].status, "UP");

    clock += 30;
    const pending = h.monitor.refresh();
    assert.equal(h.sent.at(-1).items[0].status, "UNKNOWN");
    shown = false;
    h.monitor.pause();
    assert.equal(h.latches.list[1].signal.aborted, true);
    const before = h.sent.length;
    shown = true;
    h.monitor.start();
    // A late response from the hidden period must be discarded, not re-rendered.
    h.latches.list[1].resolve(manifest(1, [row]));
    h.latches.discovery[1].resolve(discovery(1, [row]));
    assert.equal(await pending, false);
    await settle();
    assert.equal(h.sent.length, before);
    assert.equal(h.sent.at(-1).items[0].status, "UNKNOWN");

    h.latches.list[2].resolve(manifest(1, [row]));
    h.latches.discovery[2].resolve(discovery(1, [row]));
    await settle();
    h.latches.sample.at(-1).resolve(reading(item, fresh(clock)));
    await settle();
    assert.equal(h.sent.at(-1).items[0].status, "UP");
  } finally { h.monitor.stop(); }
});

test("a stalled authority read cannot paint samples that expire before display", async () => {
  let clock = Date.parse(row.updatedAt) / 1000 + 70;
  const h = harness({ clock: () => clock });
  const item = custom.target(row, settings);
  try {
    const started = clock;
    h.monitor.start();
    // The list/discovery request hangs until the original sample time is expired.
    clock = started + 11;
    h.latches.list[0].resolve(manifest(1, [row]));
    h.latches.discovery[0].resolve(discovery(1, [row]));
    await settle();
    assert.equal(h.latches.sample[0].at, started);
    h.latches.sample[0].resolve(reading(item, fresh(started)));
    await settle();
    assert.equal(h.sent.at(-1).items[0].status, "UNKNOWN");
    assert.equal(h.sent.at(-1).items[0].reason, "数据过期");
  } finally { h.monitor.stop(); }
});

test("rapid hide/show cycles abort the previous read and keep one in-flight pair", async () => {
  let clock = Date.parse(row.updatedAt) / 1000 + 70;
  let shown = true;
  const h = harness({ clock: () => clock, visible: () => shown });
  const item = custom.target(row, settings);
  try {
    h.monitor.start();
    h.latches.list[0].resolve(manifest(1, [row]));
    h.latches.discovery[0].resolve(discovery(1, [row]));
    await settle();
    h.latches.sample[0].resolve(reading(item, fresh(clock)));
    await settle();
    clock += 20;
    for (let index = 0; index < 3; index += 1) {
      shown = false;
      h.monitor.pause();
      shown = true;
      void h.monitor.refresh();
    }
    assert.equal(h.latches.list.length, 4);
    assert.equal(h.latches.discovery.length, 4);
    assert.equal(h.latches.sample.length, 1);
    assert.equal(h.latches.list[1].signal.aborted, true);
    assert.equal(h.latches.list[2].signal.aborted, true);
    assert.equal(h.latches.list[3].signal.aborted, false);
    assert.equal(h.sent.at(-1).items[0].status, "UNKNOWN");
  } finally { h.monitor.stop(); }
});

test("an idempotent replay behind a newer revision requires authoritative reread", () => {
  const store = custom.createState();
  assert.equal(store.read(manifest(1, [row])), true);
  // The original write landed at savedRevision 2 but its response was lost;
  // another administrator then committed revision 3.
  const replay = { id, savedRevision: 2, revision: 3, target: { ...row, name: "旧操作", revision: 2 },
    publication: "published", publishedRevision: 3 };
  assert.equal(store.save(replay), "reread");
  const held = store.current();
  assert.equal(held.complete, false);
  assert.equal(held.revision, 1);
  assert.deepEqual(held.targets.map(({ id: value }) => value), [id]);
  assert.equal(held.targets[0].name, row.name);
  // A read that is older than the confirmed global revision cannot reset the floor.
  assert.equal(store.read(manifest(2, [{ ...row, name: "旧操作", revision: 2 }])), false);
  const concurrent = { ...row, id: otherId, name: "并发目标", ip: "192.0.2.28", revision: 3 };
  assert.equal(store.read(manifest(3, [{ ...row, name: "旧操作", revision: 2 }, concurrent])), true);
  assert.equal(store.current().complete, true);
  assert.deepEqual(store.current().targets.map(({ id: value }) => value).sort(), [id, otherId].sort());
  // If the full authoritative list at or beyond the receipt revision is already
  // held, the replay cannot invalidate it.
  assert.equal(store.save({ ...replay, target: { ...row, revision: 2 } }), "current");
  assert.equal(store.current().complete, true);
  assert.equal(store.current().revision, 3);
});

test("replayed receipt cannot masquerade as the concurrent newer manifest", async () => {
  let clock = Date.parse(row.updatedAt) / 1000 + 70;
  const h = harness({ clock: () => clock });
  const item = custom.target(row, settings);
  try {
    h.monitor.start();
    h.latches.list[0].resolve(manifest(1, [row]));
    h.latches.discovery[0].resolve(discovery(1, [row]));
    await settle();
    h.latches.sample[0].resolve(reading(item, fresh(clock)));
    await settle();
    assert.equal(h.sent.at(-1).items[0].status, "UP");

    h.monitor.saved({ id, savedRevision: 2, revision: 3, target: { ...row, group: "Server", revision: 2 },
      publication: "published", publishedRevision: 3 });
    const held = h.sent.at(-1);
    assert.equal(held.catalog.complete, false);
    assert.equal(held.catalog.revision, 1);
    assert.match(held.notice, /重新读取/);
    assert.equal(held.items[0].status, "UNKNOWN");
    assert.equal(h.latches.list.length, 2);

    // The refresh is not allowed to accept a list older than the confirmed revision.
    h.latches.list[1].resolve(manifest(2, [{ ...row, group: "Server", revision: 2 }]));
    h.latches.discovery[1].resolve(discovery(2, [{ ...row, group: "Server", revision: 2 }]));
    await settle();
    assert.match(h.sent.at(-1).notice, /清单暂不可用/);
    assert.equal(h.sent.at(-1).items[0].status, "UNKNOWN");

    const updated = { ...row, group: "Server", revision: 2 };
    const concurrent = { ...row, id: otherId, name: "并发目标", ip: "192.0.2.28", revision: 3 };
    h.monitor.refresh();
    h.latches.list[2].resolve(manifest(3, [updated, concurrent]));
    h.latches.discovery[2].resolve(discovery(3, [updated, concurrent]));
    await settle();
    h.latches.sample.at(-1).resolve(new Map([
      [custom.target(updated, settings).key, { ...custom.target(updated, settings), ...fresh(clock) }],
      [custom.target(concurrent, settings).key, { ...custom.target(concurrent, settings), ...fresh(clock) }],
    ]));
    await settle();
    const done = h.sent.at(-1);
    assert.equal(done.catalog.complete, true);
    assert.equal(done.catalog.revision, 3);
    assert.equal(done.notice, "");
    assert.deepEqual(done.items.map(({ name, status }) => [name, status]).sort(),
      [["并发目标", "UP"], ["转播", "UP"]].sort());
  } finally { h.monitor.stop(); }
});
