const test = require("node:test");
const assert = require("node:assert/strict");
const dns = require("../bigscreen/ping/custom-dns.js");
const custom = require("../bigscreen/ping/custom-model.js");
const model = require("../bigscreen/ping/model.js");
const history = require("../bigscreen/ping/history.js");
const segments = require("../bigscreen/ping/custom-history.js");
const api = require("../bigscreen/ping/custom-api.js");
const { createPingData } = require("../bigscreen/ping/data.js");
const { webcrypto } = require("node:crypto");

const id = (n = 1) => "abcd1234-1000-4000-8000-" + String(n).padStart(12, "0");
const at = 2000000000;
const settings = model.policy({ pingQualityRttMs: 50 });
function row(fields = {}) {
  return { id: id(), kind: "dns", address: "game.example.com", name: "Game Cloud", group: "Remote",
    enabled: true, revision: 7, endpointRevision: 7, privateCidrs: [], updatedAt: new Date((at - 20) * 1000).toISOString(),
    dnsStatus: "DNS_OK", resolvedIp: "1.1.1.1", aCount: 3, resolutionGeneration: 9,
    sampleNotBefore: at - 5, activatedAt: at - 6, resolvedAt: at - 6, expiresAt: at + 60, dnsError: "", ...fields };
}
function views(target = row(), observed = {}, options = {}) {
  const authority = { ok: true, revision: 7, manifestRevision: 7, publication: "published",
    publishedRevision: 7, dnsEnabled: true, targets: [target] };
  const discovery = { ok: true, revision: 7, publishedRevision: 7, discoveredRevision: 7,
    activeTargets: [{ id: target.id, ip: target.resolvedIp, revision: target.revision, resolutionGeneration: target.resolutionGeneration }],
    dnsTargets: [{ id: target.id, dnsStatus: target.dnsStatus, resolvedIp: target.resolvedIp,
      resolutionGeneration: target.resolutionGeneration, discoveredGeneration: target.resolutionGeneration,
      sampleNotBefore: target.sampleNotBefore, pingStatus: "UP", discoveryStatus: "DISCOVERED", withdrawalPending: false, ...observed }] };
  const key = dns.target(target).key;
  const source = new Map([[key, { sourceAt: at - 1, status: "UP", rtt: 12, loss: model.lossStats(30, 30, 60, 2), ...options }]]);
  const show = () => custom.present(authority, discovery, source, settings, undefined, "", at)[0];
  return { authority, discovery, source, show };
}

test("IPv4 compatibility, IDNA normalization, optional name, and per-host CIDR validation", () => {
  for (const input of ["1.1.1.1", "192.168.1.2"]) {
    assert.deepEqual(dns.editPayload("", dns.addressInput(input), "Remote"), { name: input, ip: input, group: "Remote" });
  }
  const parsed = dns.addressInput(" 例子.Example. ");
  assert.deepEqual(parsed, { kind: "dns", address: "xn--fsqu00a.example" });
  assert.equal(dns.addressInput("node.123").address, "node.123");
  for (const input of ["https://game.example", "game.example:443", ".a.example", "a.local", "a.localhost",
    "192.168.01.3", "1.1.1.999", "node", "a.example..", "name/example"]) assert.equal(dns.addressInput(input), null, input);
  assert.deepEqual(dns.privateCidrs("10.1.0.0/16, 192.168.1.2/32 10.1.0.0/16"), ["10.1.0.0/16", "192.168.1.2/32"]);
  for (const net of ["172.15.0.0/16", "10.1.0.1/24", "192.168.0.0/15", "10.0.0.0/8/1"])
    assert.equal(dns.privateCidrs(net), null, net);
  assert.deepEqual(dns.editPayload("", parsed, "Other", []),
    { kind: "dns", address: parsed.address, name: parsed.address, group: "Other", privateCidrs: [] });
  const long = dns.addressInput("a".repeat(60) + "." + "b".repeat(60) + ".example");
  assert.equal(dns.editPayload("", long, "Remote").name.length, 80);
});

test("one stable card and strict per-row, generation, TTL, sample, and withdrawal gates", () => {
  const positive = views().show();
  assert.equal(positive.name, "game.example.com");
  assert.equal(positive.friendlyName, "Game Cloud");
  assert.equal(positive.ip, "1.1.1.1");
  assert.equal(positive.status, "UP");
  assert.match(positive.dnsLabel, /3 个 A 记录，当前监控 1 个/);
  assert.equal(views(row(), {}, { rtt: 142.36 }).show().status, "DEGRADED");
  assert.equal(views(row(), { pingStatus: "DOWN" }, { status: "DOWN", rtt: null }).show().status, "DOWN");
  for (const state of ["DNS_PENDING", "DNS_ERROR", "DNS_STALE", "DNS_POLICY_BLOCKED"]) {
    assert.equal(views(row({ dnsStatus: state }), { pingStatus: "DOWN" }, { status: "DOWN" }).show().status, "UNKNOWN");
  }
  assert.match(views(row({ dnsStatus: "DNS_ERROR", dnsError: "timeout" })).show().reason, /超时/);
  for (const found of [{ withdrawalPending: true }, { discoveredGeneration: 8 }, { resolvedIp: "1.1.1.2" },
    { discoveryStatus: "DISCOVERY_PENDING" }, { sampleNotBefore: at - 4 }, { pingStatus: "UNKNOWN" }]) {
    assert.equal(views(row(), found).show().status, "UNKNOWN");
  }
  const invalid = views();
  invalid.discovery.activeTargets[0].revision = 5;
  assert.equal(invalid.show().status, "UNKNOWN");
  const wrongManifest = views();
  wrongManifest.authority.manifestRevision = 6;
  assert.equal(wrongManifest.show().status, "UNKNOWN");
  assert.equal(views(row({ expiresAt: at })).show().dnsStatus, "DNS_STALE");
  const newB = views(row({ resolvedIp: "1.1.1.2", resolutionGeneration: 10, sampleNotBefore: at - 1 }), {}, { sourceAt: at - 1 });
  assert.equal(newB.show().key, positive.key);
  assert.equal(newB.show().status, "UNKNOWN");
  assert.equal(views(row({ resolutionGeneration: 11, sampleNotBefore: at - .5 }), {}, { sourceAt: at - 1 }).show().status, "UNKNOWN");
  assert.equal(views(row({ resolutionGeneration: 11, sampleNotBefore: at - .5 }), {}, { sourceAt: at - .1 }).show().status, "UP");
  const held = views(row({ dnsStatus: "DNS_ERROR" }), { withdrawalPending: true }).show();
  assert.equal(held.status, "UNKNOWN");
  assert.match(held.sync, /停止旧探测/);
});

test("loss and current samples from Prometheus exclude all samples at/before the gate", async () => {
  const series = (ip, field, rows) => ({ metric: { ...dns.endpoint(id(), ip), __name__: field }, values: rows });
  const live = row();
  const endpoint = dns.verifiedEndpoint(views().authority, views().discovery, live, at);
  const samples = [
    series("1.1.1.1", "up", [[at - 9, "1"], [at - 5, "1"], [at - 1, "1"]]),
    series("1.1.1.1", "probe_success", [[at - 9, "0"], [at - 5, "0"], [at - 1, "1"]]),
    series("1.1.1.1", "probe_icmp_duration_seconds", [[at - 1, "0.012"]]),
    series("1.1.1.2", "up", [[at, "1"]]), series("1.1.1.2", "probe_success", [[at, "0"]])
  ];
  const data = createPingData({ settings, query: async (expression) => {
    assert.match(expression, /custom_id="abcd1234/);
    return samples;
  } });
  const selected = await data.custom(at, undefined, [endpoint], false);
  const reading = selected.get(endpoint.key);
  assert.equal(reading.sourceAt, at - 1);
  assert.equal(reading.rtt, 12);
  assert.equal(reading.loss.total, 1);
  assert.equal(reading.loss.failed, 0);
});

test("DNS route survives readdressing and deleted targets resolve from paginated history", async () => {
  const target = dns.target(row());
  const url = history.urlFor([target], { start: at - 120, end: at }, null);
  const parsed = history.parseRoute(new URL(url, "http://fixture.invalid").searchParams, at, settings);
  assert.equal(parsed.targets[0].key, target.key);
  const segment = { id: id(), address: "game.example.com", ip: "1.1.1.1", generation: 9, start: at - 100, end: at - 2,
    sampleNotBefore: at - 90 };
  const fake = { list: async () => ({ targets: [] }), resolutions: async (identity) => {
    assert.equal(identity, id()); return [segment];
  } };
  const client = api.createClient({ crypto: webcrypto, fetch: async (pathname) => {
    if (pathname.endsWith("cursor=0")) return { ok: true, json: async () => ({ ok: true, segments: [segment], nextCursor: 9, more: true }) };
    assert.ok(pathname.endsWith("cursor=9"));
    return { ok: true, json: async () => ({ ok: true, segments: [{ ...segment, generation: 10 }], nextCursor: 10, more: false }) };
  } });
  assert.deepEqual((await client.resolutions(id())).map((row) => row.generation), [9, 10]);
  assert.equal((await segments.createHistoryData(fake, {}).hydrate(parsed.targets))[0].name, "game.example.com");
});

test("A→B→A, unconfirmed B, partial aggregate windows and historical statistics never mix", () => {
  const first = { id: id(), address: "game.example.com", ip: "1.1.1.1", generation: 1, start: 10, end: 20, sampleNotBefore: 11 };
  const missed = { ...first, ip: "1.1.1.2", generation: 2, start: 20, end: 30, sampleNotBefore: null };
  const last = { ...first, address: "renamed.example.com", generation: 3, start: 30, end: null, sampleNotBefore: 34 };
  const target = { ...dns.target(row()), segments: segments.validateSegments([first, missed, last]) };
  const points = segments.annotate([[11, first.ip], [12, first.ip], [25, missed.ip], [31, first.ip], [34, first.ip], [36, last.ip]]
    .map(([t, ip]) => ({ t, ip, rtt: t, success: 1 })), target.segments);
  assert.deepEqual(points.map((point) => point.verified), [false, true, false, false, false, true]);
  const plotted = segments.rawSeries(target, points, 10, 44);
  assert.deepEqual(plotted.points.map((point) => point.rtt), [12, null, 36]);
  assert.deepEqual(segments.splitStatistics(plotted, 10, 44, false)
    .map((item) => [item.target.generation, item.target.name, item.points.length]), [[1, "game.example.com", 1], [3, "renamed.example.com", 1]]);
  assert.ok(plotted.buckets.every((bucket) => bucket.total <= 1));
  const long = { ...target, segments: [{ ...last, start: 100, sampleNotBefore: 122 }] };
  const step = 20;
  const buckets = Array.from({ length: 240 }, (_, i) => ({ from: 100 + i * step, to: 120 + i * step, total: 10, success: 10 }));
  const aggregated = segments.aggregate(long, new Map([["actual", { target: { ...target, ip: last.ip },
    buckets, points: buckets.map((bucket) => ({ t: bucket.to, rtt: 1 })) }]]), 100, 4900);
  assert.deepEqual(aggregated.buckets.slice(0, 3).map((bucket) => bucket.total), [0, 0, 10]);
});

test("a delayed sample response cannot revive a different active generation or a paused route", async () => {
  let state = views();
  let release;
  let called;
  const wait = () => new Promise((resolve) => { called = resolve; });
  const updates = [];
  const monitor = custom.createMonitor({
    client: { list: async () => state.authority, discovery: async () => state.discovery },
    sample: () => new Promise((resolve) => { release = resolve; called(); }),
    settings, visible: () => true, now: () => at, changed: (view) => updates.push(view.items[0]?.status)
  });
  try {
    const requested = wait();
    monitor.start();
    await requested;
    const old = state;
    state = views(row({ resolvedIp: "1.1.1.2", resolutionGeneration: 10 }));
    release(old.source);
    await new Promise(setImmediate);
    assert.equal(updates.at(-1), "UNKNOWN");
    const next = wait();
    const pending = monitor.refresh();
    await next;
    monitor.pause();
    release(state.source);
    assert.equal(await pending, false);
    assert.ok(updates.every((status) => status !== "UP"));
  } finally { monitor.stop(); }
});

test("mixed sizes keep UUID identity, 100 enabled, and 300-row catalog limit", () => {
  for (const size of [50, 100, 300]) {
    const state = custom.createState();
    const targets = Array.from({ length: size }, (_, i) => {
      const item = row({ id: id(i + 1), address: "host" + i + ".example", enabled: i < 100, dnsStatus: "DNS_PENDING" });
      if (i % 2 === 0) return item;
      return { id: item.id, ip: "192.0.2." + (i % 250 + 1), name: item.name, updatedAt: item.updatedAt,
        enabled: item.enabled, revision: 7, group: "Remote" };
    });
    const list = { ...views().authority, targets };
    assert.ok(state.read(list));
    assert.equal(custom.present(state.current(), null, null, settings).length, Math.min(size, 100));
    if (size > 100) assert.throws(() => state.read({ ...list, targets: targets.map((item) => ({ ...item, enabled: true })) }));
  }
});


test("full 100-domain batch stays below the Prometheus URL limit", async () => {
  const endpoints = Array.from({ length: 100 }, (_, i) => ({
    ...dns.target(row({ id: id(i + 1), resolvedIp: "192.168.100." + (i + 100) })),
    sampleNotBefore: at - 20
  }));
  const data = createPingData({ settings, query: async (expression) => {
    const params = new URLSearchParams({ query: expression, time: at, timeout: "7s" });
    assert.ok(("/prometheus/api/v1/query?" + params).length < 7600);
    assert.equal((expression.match(/abcd1234/g) || []).length, 100);
    return [];
  } });
  assert.equal((await data.custom(at, undefined, endpoints, false)).size, 0);
});

test("DNS sample request failure preserves validated IPv4 readings", async () => {
  const fixed = dns.endpoint(id(2), "192.0.2.2");
  const labels = { source: 1, source_at: at - 1, probe: 1, probe_at: at - 1, rtt: .012, rtt_at: at - 1, count: 30, ok: 30 };
  const data = createPingData({ settings, query: async (expression) => {
    if (expression.startsWith("{__name__")) throw new Error("isolated domain sample source unavailable");
    return Object.entries(labels).map(([field, value]) => ({ metric: { ...fixed, ping_field: field }, value: [at, String(value)] }));
  } });
  const endpoint = { ...dns.target(row()), sampleNotBefore: at - 20 };
  const samples = await data.custom(at, undefined, [endpoint], true);
  assert.equal(samples.get(model.identity(fixed)).status, "UP");
  assert.equal(samples.has(endpoint.key), false);
});

test("monitor separates catalog discovery from valid fixed and DNS probe evidence", async () => {
  const pending = row({ dnsStatus: "DNS_PENDING", resolvedIp: null, resolutionGeneration: null, sampleNotBefore: null });
  const unresolved = views(pending, { discoveryStatus: "DISCOVERY_PENDING", pingStatus: "UNKNOWN" });
  const resolved = views();
  const fixed = { id: id(2), name: "Fixed", group: "Remote", ip: "192.0.2.2", enabled: true,
    revision: 7, updatedAt: new Date((at - 75) * 1000).toISOString() };
  const physical = { id: fixed.id, ip: fixed.ip, revision: fixed.revision };
  const fixedKey = custom.target(fixed, settings).key;
  const cases = [];
  const add = (name, authority, discovery, expected, fixedStatus = null, dnsStatus = null, source = new Map()) =>
    cases.push({ name, authority, discovery, expected, fixedStatus, dnsStatus, source });

  for (const schema of [1, 2]) {
    const { manifestRevision, dnsEnabled, ...base } = resolved.authority;
    const authority = { ...base, targets: [], ...(schema === 2 ? { manifestRevision, dnsEnabled } : {}) };
    for (const loaded of [false, true]) {
      const discovery = { ...resolved.discovery, activeTargets: [], dnsTargets: [], discoveredRevision: loaded ? 7 : null };
      add("Schema " + schema + " empty, pool loaded=" + loaded, authority, discovery, loaded ? "已发现" : "待发现");
      if (schema === 2) {
        add("all disabled, pool loaded=" + loaded,
          { ...authority, targets: [{ ...fixed, enabled: false }, { ...resolved.authority.targets[0], enabled: false }] },
          discovery, loaded ? "已发现" : "待发现");
      }
    }
  }

  add("DNS pending despite empty pool acknowledgement", unresolved.authority,
    { ...unresolved.discovery, activeTargets: [] }, "待发现", null, "UNKNOWN");
  add("DNS publication pending", { ...resolved.authority, publication: "pending", publishedRevision: null },
    { ...resolved.discovery, publishedRevision: null, discoveredRevision: null, activeTargets: [] }, "待发布", null, "UNKNOWN");
  add("DNS address pending discovery", resolved.authority,
    { ...resolved.discovery, discoveredRevision: null, activeTargets: [] }, "待发现", null, "UNKNOWN");
  const withdrawn = views(row({ dnsStatus: "DNS_ERROR" }), {
    discoveryStatus: "DISCOVERY_PENDING", withdrawalPending: true, pingStatus: "UNKNOWN"
  });
  add("DNS withdrawal pending", withdrawn.authority,
    { ...withdrawn.discovery, discoveredRevision: null }, "待发现", null, "UNKNOWN");
  add("DNS confirmed without a probe sample", resolved.authority, resolved.discovery, "已发现", null, "UNKNOWN");

  for (const [status, rtt] of [["UP", 12], ["DOWN", null], ["DEGRADED", 80]]) {
    const source = new Map([[fixedKey, { sourceAt: at - 1, status: status === "DOWN" ? "DOWN" : "UP", rtt,
      loss: model.lossStats(30, 30, 60, 2) }]]);
    const mixed = { ...unresolved.authority, targets: [fixed, pending] };
    const discovery = { ...unresolved.discovery, activeTargets: [physical] };
    add("healthy evidence independently available: " + status, mixed, discovery, "待发现", status, "UNKNOWN", source);
    add("fixed evidence during DNS publication pending: " + status, { ...mixed, publication: "pending", publishedRevision: null },
      { ...discovery, publishedRevision: null, discoveredRevision: null }, "待发布", status, "UNKNOWN", source);
    add("both discovered: " + status, { ...resolved.authority, targets: [fixed, ...resolved.authority.targets] },
      { ...resolved.discovery, activeTargets: [physical, ...resolved.discovery.activeTargets] },
      "已发现", status, "UP", new Map([...source, ...resolved.source]));
  }

  const actual = [];
  for (const scenario of cases) {
    let last;
    const monitor = custom.createMonitor({
      client: { list: async () => scenario.authority, discovery: async () => scenario.discovery },
      sample: async () => scenario.source, settings, visible: () => true, now: () => at,
      changed: (view) => { last = view; }
    });
    try {
      monitor.start();
      await monitor.refresh();
      actual.push([scenario.name, last.sync, last.items.find((item) => !item.dns)?.status ?? null,
        last.items.find((item) => item.dns)?.status ?? null, last.notice]);
    } finally { monitor.stop(); }
  }
  assert.deepEqual(actual, cases.map(({ name, expected, fixedStatus, dnsStatus }) => [name, expected, fixedStatus, dnsStatus, ""]));
});

test("deleting the last DNS target waits for the Schema 2 empty pool to be confirmed", async () => {
  const { authority, discovery, source } = views();
  let list = authority;
  let found = discovery;
  const events = [];
  const monitor = custom.createMonitor({
    client: { list: async () => list, discovery: async () => found },
    sample: async () => source, settings, visible: () => true, now: () => at, changed: (view) => events.push(view)
  });
  try {
    monitor.start();
    await monitor.refresh();
    assert.equal(events.at(-1).sync, "已发现");
    list = { ...list, manifestRevision: 8, revision: 8, publishedRevision: 8, targets: [] };
    found = { ok: true, revision: 8, publishedRevision: 8, discoveredRevision: null, activeTargets: [] };
    const before = events.length;
    monitor.saved({ id: id(), target: null, savedRevision: 8, revision: 8, publication: "published", publishedRevision: 8 });
    await monitor.refresh();
    assert.deepEqual(events.slice(before).map(({ items, sync }) => [items.length, sync]), [[0, "待发现"], [0, "待发现"]]);
    found = { ...found, discoveredRevision: 8 };
    await monitor.refresh();
    assert.equal(events.at(-1).sync, "已发现");
  } finally { monitor.stop(); }
});

test("the overall DNS discovery lease expires even when its first probe is still pending", async () => {
  const { authority, discovery } = views(row({ expiresAt: at + 1 }));
  let clock = at;
  let events = [];
  const monitor = custom.createMonitor({
    client: { list: async () => authority, discovery: async () => discovery },
    sample: async () => new Map(), settings, visible: () => true, now: () => clock,
    changed: (view) => events.push(view)
  });
  try {
    monitor.start();
    await monitor.refresh();
    assert.equal(events.at(-1).sync, "已发现");
    assert.equal(events.at(-1).items[0].status, "UNKNOWN");
    clock++;
    events = [];
    const refresh = monitor.refresh();
    assert.deepEqual(events.map(({ sync }) => sync), ["待发现"]);
    assert.equal(events[0].items[0].dnsStatus, "DNS_STALE");
    await refresh;
    assert.ok(events.every(({ sync }) => sync === "待发现"));
  } finally { monitor.stop(); }
});
