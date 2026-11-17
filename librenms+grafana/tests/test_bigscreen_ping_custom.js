const test = require("node:test");
const assert = require("node:assert/strict");
const { webcrypto } = require("node:crypto");
const { createClient } = require("../bigscreen/ping/custom-api.js");
const custom = require("../bigscreen/ping/custom-model.js");
const model = require("../bigscreen/ping/model.js");
const history = require("../bigscreen/ping/history.js");

const id = "65a8c647-c103-4e45-89bf-06b107bf531a";
const row = { id, name: "转播", group: "Remote", ip: "192.0.2.17", enabled: true, revision: 1,
  createdAt: "2026-11-17T09:00:00Z", updatedAt: "2026-11-17T09:00:00Z" };
const manifest = (revision, targets) => ({ ok: true, revision, targets, publication: "published", publishedRevision: revision });
const discovery = (revision, targets) => ({ ok: true, revision, publishedRevision: revision, discoveredRevision: revision,
  activeTargets: targets.map(({ id, ip, revision }) => ({ id, ip, revision, scrapeHealth: "up" })) });
const settings = model.policy();

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
