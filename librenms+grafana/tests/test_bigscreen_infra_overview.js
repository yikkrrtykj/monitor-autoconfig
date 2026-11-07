const assert = require("node:assert/strict");
const { test } = require("node:test");
const { createHash } = require("node:crypto");
const ping = require("../bigscreen/ping/model.js");
const { createPingData } = require("../bigscreen/ping/data.js");
const { createOverviewData } = require("../bigscreen/infra/overview-data.js");
const { createOverviewController } = require("../bigscreen/infra/overview-controller.js");
const { prometheusSamples } = require("../bigscreen/api.js");
const overview = require("../bigscreen/infra/overview-model.js");

const now = 1760000000;
const core = { job: "infra-core-ping", instance: "Same", target_ip: "192.0.2.10" };
const isp = { ...core, job: "infra-isp-ping" };
const ha = { ...core, job: "infra-fw-unit-ping" };
const ap = { job: "infra-ap-ping", instance: "AP", target_ip: "192.0.2.11" };
const signal = () => new AbortController().signal;
const vector = (metric, values) => Object.entries(values).map(([ping_field, value]) => ({
  metric: { ...metric, ping_field }, value: [now, String(value)]
}));
const healthy = { target: 1, source: 1, source_at: now - 1, probe: 1, probe_at: now - 1,
  rtt: 0.002, rtt_at: now - 1, count: 60, ok: 60 };
const targets = (...metrics) => ping.snapshot(metrics.flatMap((metric) =>
  vector(metric, healthy)), now, ping.policy());
const inventoryItem = (name, index, opts = {}) => ({
  targets: opts.gateway ? [opts.gateway] : [],
  labels: { display_name: name, metric_target: opts.instance || "192.0.2.1",
    metric_ifindex: index, metadata_conflict: opts.conflict }
});
const field = (overview_field, value, ifIndex, instance = "192.0.2.1") => ({
  metric: { overview_field, instance, ...(ifIndex ? { ifIndex } : {}) },
  values: [[now, String(value)]]
});
const source = (up = 1, at = now - 1) => [field("up", up), field("up_at", at)];
const interfaceFields = (index, changes = {}) => Object.entries({
  in: 12e6, out: 3e6, in_at: now - 1, out_at: now - 1, in_resets: 0, out_resets: 0, speed: 100,
  ...changes
}).map(([name, value]) => field(name, value, index));
const validCards = () => overview.wanCards(overview.inventoryRows([inventoryItem("WAN A", "3", { gateway: "192.0.2.10" })]),
  [...source(), ...interfaceFields("3")], now, 15, "WAN A:100/50");
const status = (points, cards = validCards()) => ({
  pingState: { state: "ready", data: { targets: points, agedOut: 0 } },
  wanState: { state: "ready", data: cards },
  statsState: { state: "ready", at: now, data: new Map([[ping.identity(core), 0]]) }
});

test("only fresh, complete key coverage may produce All Good", () => {
  const current = status(targets(core, isp, ha, ap));
  const ready = overview.overviewState(current, now);
  assert.equal(ready.health, "healthy");
  assert.equal(ready.infraCounts.UP, 2);
  assert.equal(ready.ispCounts.UP, 1);
  assert.equal(ready.clients.find((client) => client.category === "ap").UP, 1);
  assert.equal(ready.gatewayRows.find(({ job }) => job === core.job).stddev, 0);
  assert.equal(ready.gatewayRows.some(({ job }) => job === ha.job), false);
  assert.equal(overview.overviewState(current, now + 20).health, "unavailable");
  assert.equal(overview.overviewState(status([]), now).health, "unavailable");
  assert.equal(overview.overviewState(status(targets(isp)), now).health, "partial");
  assert.equal(overview.overviewState(status(targets(core, isp), []), now).health, "partial");
  const withoutGateway = validCards().map((card) => ({ ...card, gateway: "192.0.2.200" }));
  assert.equal(overview.overviewState(status(targets(core, isp), withoutGateway), now).uncovered, 1);
  current.pingState.data.agedOut = 1;
  assert.equal(overview.overviewState(current, now).health, "partial");
  current.pingState.state = "error";
  const failed = overview.overviewState(current, now);
  assert.equal(failed.health, "unavailable");
  assert.equal(failed.infraCounts.UNKNOWN, 2);
  assert.equal(failed.wans[0].in.bps, 12e6);
});

test("true DOWN, quality loss and missing scrape remain separate; same IP links keep job identity", () => {
  const settings = ping.policy({ pingIntervals: { infra: "10s" } });
  const snapshot = (changes) => ping.snapshot([
    ...vector(core, { ...healthy, count: 6, ok: 5, ...changes }), ...vector(isp, healthy)
  ], now, settings);
  const quality = overview.overviewState(status(snapshot({})), now);
  assert.equal(quality.health, "fault");
  assert.equal(quality.gatewayRows[0].loss.loss.toFixed(1), "16.7");
  assert.equal(quality.gatewayRows[0].status, "DEGRADED");
  const down = overview.overviewState(status(snapshot({ probe: 0 })), now);
  assert.equal(down.faults[0].status, "DOWN");
  assert.equal(down.faults[0].rtt, null);
  const missing = overview.overviewState(status(snapshot({ source: 0, probe: 0 })), now);
  assert.equal(missing.health, "partial");
  assert.equal(missing.faults[0].status, "UNKNOWN");
  for (const target of missing.gatewayRows) {
    assert.equal(new URL(overview.pingHref(target), "http://localhost").searchParams.get("target"), ping.identity(target.metric));
  }
  assert.notEqual(overview.pingHref(missing.gatewayRows[0]), overview.pingHref(missing.gatewayRows[1]));
});

test("each WAN needs its own verified probe association, including a WAN without a published gateway", () => {
  const rows = overview.inventoryRows([
    inventoryItem("WAN A", "3", { gateway: "192.0.2.10" }), inventoryItem("WAN B", "4")
  ]);
  const cards = overview.wanCards(rows, [...source(), ...interfaceFields("3"), ...interfaceFields("4")], now, 15, "");
  const current = targets(core, isp);
  const mixed = overview.overviewState(status(current, cards), now);
  assert.equal(mixed.health, "partial");
  assert.deepEqual(mixed.wans.map((wan) => wan.probeCoverage.state), ["matched", "unconfigured"]);
  assert.equal(mixed.unconfigured, 1);
  assert.equal(mixed.total.DOWN, 0);
  assert.deepEqual(mixed.wans.map((wan) => [wan.in.bps, wan.out.bps]), [[12e6, 3e6], [12e6, 3e6]]);

  const noGateways = cards.map((wan) => ({ ...wan, gateway: "" }));
  const missing = overview.overviewState(status(current, noGateways), now);
  assert.equal(missing.health, "partial");
  assert.equal(missing.unconfigured, 2);
  const unboundManual = { ...isp, instance: "WAN B", target_ip: "192.0.2.100" };
  const namedOnly = overview.overviewState(status(targets(core, isp, unboundManual), noGateways), now);
  assert.equal(namedOnly.unconfigured, 2);
  assert.equal(namedOnly.total.DOWN, 0);

  const canonical = { ...unboundManual, metric_target: "192.0.2.1", metric_ifindex: "4" };
  const bound = overview.overviewState(status(targets(core, isp, canonical), cards), now);
  assert.equal(bound.health, "healthy");
  assert.equal(bound.wans[1].probeCoverage.target.key, ping.identity(canonical));
  const wrongInterface = { ...canonical, metric_ifindex: "5" };
  assert.equal(overview.overviewState(status(targets(core, isp, wrongInterface), cards), now).health, "partial");

  const restored = cards.map((wan, index) => ({ ...wan, gateway: index ? "192.0.2.100" : "192.0.2.10" }));
  assert.equal(overview.overviewState(status(targets(core, isp, unboundManual), restored), now).health, "healthy");
  const unmatched = cards.map((wan) => ({ ...wan, gateway: "192.0.2.200" }));
  const uncovered = overview.overviewState(status(current, unmatched), now);
  assert.equal(uncovered.uncovered, 2);
  assert.equal(uncovered.total.DOWN, 0);
  const duplicateProbe = { ...isp, instance: "Manual ISP", display_name: "WAN A" };
  const ambiguous = overview.overviewState(status(targets(core, isp, duplicateProbe)), now);
  assert.equal(ambiguous.health, "partial");
  assert.equal(ambiguous.wans[0].probeCoverage.state, "ambiguous");
  const conflictingProbe = { ...isp, metric_target: "192.0.2.1", metric_ifindex: "99" };
  const wrongBinding = overview.overviewState(status(targets(core, conflictingProbe)), now);
  assert.equal(wrongBinding.wans[0].probeCoverage.state, "missing");
  assert.equal(wrongBinding.total.DOWN, 0);
});

const pairedWanCards = (secondGateway = "192.0.2.10") => overview.wanCards(overview.inventoryRows([
  inventoryItem("WAN A", "3", { gateway: "192.0.2.10" }),
  inventoryItem("WAN B", "4", { gateway: secondGateway })
]), [...source(), ...interfaceFields("3"), ...interfaceFields("4")], now, 15, "");

test("one address-only probe cannot cover different WAN interfaces sharing a gateway", () => {
  const cards = pairedWanCards();
  const state = overview.overviewState(status(targets(core, isp), cards), now);
  assert.equal(state.health, "partial");
  assert.equal(state.availability, "partial");
  assert.equal(state.ambiguous, 2);
  assert.deepEqual(state.wans.map((wan) => wan.probeCoverage.state), ["ambiguous", "ambiguous"]);
  assert.deepEqual(state.wans.map((wan) => [wan.in.bps, wan.out.bps]), [[12e6, 3e6], [12e6, 3e6]]);
  assert.equal(state.total.DOWN, 0);
  assert.deepEqual(state.faults, []);
});

test("distinct canonical probes cover WANs sharing a gateway with their exact interface identities", () => {
  const canonical = ["3", "4"].map((metric_ifindex) => ({
    ...isp, metric_target: "192.0.2.1", metric_ifindex
  }));
  const state = overview.overviewState(status(targets(core, ...canonical), pairedWanCards()), now);
  assert.equal(state.health, "healthy");
  assert.equal(state.ambiguous, 0);
  assert.deepEqual(state.wans.map((wan) => wan.probeCoverage.target.key), canonical.map(ping.identity));
  assert.deepEqual(state.wans.map((wan) => wan.probeCoverage.state), ["matched", "matched"]);
});

test("unique address-only probes still cover WANs with different published gateways", () => {
  const other = { ...isp, instance: "WAN B", target_ip: "192.0.2.11" };
  const state = overview.overviewState(status(targets(core, isp, other), pairedWanCards(other.target_ip)), now);
  assert.equal(state.health, "healthy");
  assert.deepEqual(state.wans.map((wan) => wan.probeCoverage.target.key), [isp, other].map(ping.identity));
});

test("shared gateway ambiguity clears after independent probes arrive without changing WAN traffic", () => {
  const cards = pairedWanCards();
  const ambiguous = overview.overviewState(status(targets(core, isp), cards), now);
  const canonical = ["3", "4"].map((metric_ifindex) => ({
    ...isp, metric_target: "192.0.2.1", metric_ifindex
  }));
  const recovered = overview.overviewState(status(targets(core, ...canonical), cards), now);
  assert.equal(ambiguous.health, "partial");
  assert.equal(recovered.health, "healthy");
  assert.equal(recovered.total.DOWN, 0);
  assert.equal(recovered.ambiguous, 0);
  assert.deepEqual(recovered.wans.map((wan) => [wan.in.bps, wan.out.bps]), ambiguous.wans.map((wan) => [wan.in.bps, wan.out.bps]));
  assert.deepEqual(ambiguous.wans.map((wan) => wan.probeCoverage.state), ["ambiguous", "ambiguous"]);
});

test("confirmed faults remain visible alongside an unavailable data domain", () => {
  for (const changes of [{ probe: 0 }, { ok: 50 }]) {
    const points = ping.snapshot([...vector(core, { ...healthy, ...changes }), ...vector(isp, healthy)], now, ping.policy());
    const current = status(points);
    current.wanState.state = "error";
    const combined = overview.overviewState(current, now);
    assert.equal(combined.health, "fault");
    assert.equal(combined.availability, "partial");
    assert.equal(combined.faults[0].status, changes.probe === 0 ? "DOWN" : "DEGRADED");
    assert.equal(combined.total.UNKNOWN, 0);
  }
  const current = status(targets(core, isp));
  current.wanState.state = "error";
  assert.equal(overview.overviewState(current, now).health, "partial");
  current.pingState.state = "error";
  const unavailable = overview.overviewState(current, now);
  assert.equal(unavailable.health, "unavailable");
  assert.equal(unavailable.total.DOWN, 0);
});

test("WAN direction and named capacity are exact, and name or identity ambiguity cannot guess utilization", () => {
  const metrics = [...source(), ...interfaceFields("3"), ...interfaceFields("4", { in: 25e6, out: 2e6 })];
  const rows = overview.inventoryRows([inventoryItem("WAN A", "3"), inventoryItem("WAN A", "4")]);
  const cards = overview.wanCards(rows, metrics, now, 15, "WAN A:100/50");
  assert.deepEqual(cards.map((card) => [card.in.bps, card.out.bps]), [[12e6, 3e6], [25e6, 2e6]]);
  assert.ok(cards.every((card) => card.in.capacity === null && card.out.capacity === null));
  const [valid] = validCards();
  assert.deepEqual([valid.in.capacity, valid.out.capacity], [100, 50]);
  assert.equal(overview.wanCards(rows.slice(0, 1), metrics, now, 15, "1000")[0].in.capacity, null);
  const [unique] = overview.inventoryRows([inventoryItem("WAN A", "3")]);
  assert.equal(overview.wanCards([unique], metrics, now, 15, "WAN A:100,WAN A:200")[0].in.capacity, null);
  const conflicting = overview.inventoryRows([inventoryItem("WAN A", "3"), inventoryItem("WAN B", "3")]);
  assert.ok(overview.wanCards(conflicting, metrics, now, 15, "WAN A:100").every(({ in: input }) => input.bps === null));
});

test("WAN freshness, resets, impossible speeds and duplicate metrics are unavailable without losing the other direction", () => {
  const rows = overview.inventoryRows([inventoryItem("WAN A", "3")]);
  for (const series of [
    [...source(0), ...interfaceFields("3")],
    [...source(1, now - 40), ...interfaceFields("3", { in_at: now - 40 })],
    [...source(), ...interfaceFields("3", { in_resets: 1 })],
    [...source(), ...interfaceFields("3", { in: 200e6 })],
    [...source(), ...interfaceFields("3"), field("in", 20e6, "3")]
  ]) {
    const [card] = overview.wanCards(rows, series, now, 15, "WAN A:100");
    assert.equal(card.in.bps, null);
    assert.notEqual(card.in.reason, "");
    assert.equal(card.in.points.at(-1).v, null);
  }
  const [reset] = overview.wanCards(rows, [...source(), ...interfaceFields("3", { in_resets: 1 })], now, 15, "WAN A:100");
  assert.equal(reset.out.bps, 3e6);
  const withError = status(targets(core, isp));
  withError.wanState.state = "error";
  const partial = overview.overviewState(withError, now);
  assert.equal(partial.health, "partial");
  assert.equal(partial.gatewayRows[0].status, "UP");
  assert.equal(partial.wans[0].in.bps, null);
});

test("overview reuses the 30-minute inventory only within its 30-second budget", async () => {
  const scans = [];
  const api = createPingData({ settings: ping.policy(), inventoryMaxAge: 30, query: async (expr, { time }) => {
    const full = expr.includes("last_over_time");
    scans.push(full);
    if (full) return vector(core, { ...healthy, source_at: time - 1, probe_at: time - 1, rtt_at: time - 1 });
    if (time === now + 10) return [];
    const { target, ...fields } = healthy;
    return vector(isp, { ...fields, source_at: time - 1, probe_at: time - 1, rtt_at: time - 1 });
  } });
  assert.equal((await api.summary(now, signal())).targets.length, 1);
  const partial = await api.summary(now + 5, signal());
  assert.deepEqual(partial.targets.map(({ status }) => status), ["UNKNOWN", "UP"]);
  const disappeared = await api.summary(now + 10, signal());
  assert.deepEqual(disappeared.targets.map(({ status }) => status), ["UNKNOWN", "UNKNOWN"]);
  await api.summary(now + 29, signal());
  await api.summary(now + 30, signal());
  assert.deepEqual(scans, [true, false, false, false, true]);
});

test("STDDEV reads only timestamp-matched positive successful samples", async () => {
  const [target] = targets(core);
  const points = Array.from({ length: 150 }, (_, i) => [now - 2 * i, "1"]);
  const success = points.map(([t], index) => [t, index === 0 ? "0" : "1"]);
  const rtt = points.map(([t], index) => [t, index === 0 ? "0" : "0.002"]);
  const api = createOverviewData({ pingData: { raw: async (rows, start, end) =>
    ping.rawRows([
      { metric: { ...core, __name__: "up" }, values: points },
      { metric: { ...core, __name__: "probe_success" }, values: success },
      { metric: { ...core, __name__: "probe_icmp_duration_seconds", phase: "rtt" }, values: rtt }
    ], rows, start, end)
  } });
  const stats = await api.gatewayStats([target], now, signal());
  assert.equal(stats.get(target.key), 0);
});

test("manual WAN binding requires one interface and uses only the proven canonical pair", async () => {
  const query = async (_expr, params) => !params.step
    ? [{ metric: { ifAlias: "Single", instance: "192.0.2.1", ifIndex: "3" } },
      { metric: { ifAlias: "Duplicate", instance: "192.0.2.1", ifIndex: "3" } },
      { metric: { ifAlias: "Duplicate", instance: "192.0.2.1", ifIndex: "4" } }]
    : [...source(), ...interfaceFields("3")];
  const api = createOverviewData({ pingData: {}, query, autoDiscovery: false,
    configuredNames: ["Single", "Duplicate"], bandwidthConfig: "Single:100,Duplicate:100" });
  const { cards } = await api.bandwidth(now, signal());
  assert.equal(cards[0].in.bps, 12e6);
  assert.equal(cards[1].in.bps, null);
  assert.equal(cards[1].in.capacity, null);
});

test("discovered WAN rejects stale or inconsistent publication and surfaces metric transport failure", async () => {
  const payload = [inventoryItem("WAN A", "3")];
  const bytes = new TextEncoder().encode(JSON.stringify(payload));
  const metadata = { status: "ok", inventory_count: 1, last_success_at: now,
    inventory_sha256: createHash("sha256").update(bytes).digest("hex") };
  const readJson = async (path) => path.endsWith("isp_targets.json") ? { bytes, data: payload } : { data: metadata };
  const make = () => createOverviewData({ autoDiscovery: true, configuredNames: [], readJson,
    pingData: {}, query: async () => { throw new Error("metrics 503"); } });
  const result = await make().bandwidth(now, signal());
  assert.equal(result.failed, true);
  assert.equal(result.cards.length, 1);
  assert.equal(result.cards[0].in.bps, null);
  metadata.last_success_at = now - 301;
  await assert.rejects(make().bandwidth(now, signal()));
  metadata.last_success_at = now;
  metadata.status = "failed";
  await assert.rejects(make().bandwidth(now, signal()));
  metadata.status = "ok";
  metadata.inventory_count = 2;
  await assert.rejects(make().bandwidth(now, signal()));
});

test("controller isolates failed domains and drops a response after stopping or re-entering", async () => {
  const tasks = [];
  const rendered = [];
  let scheduled;
  let clock = now;
  const controller = createOverviewController({
    data: {
      summary: (at, currentSignal) => new Promise((resolve) => tasks.push({ at, currentSignal, resolve })),
      bandwidth: async () => { throw new Error("WAN 503"); },
      gatewayStats: async () => new Map()
    },
    render: (state) => rendered.push(state), clock: () => clock,
    schedule: (tick, interval) => { assert.equal(interval, 5000); scheduled = tick; return 1; },
    cancel: () => { scheduled = null; }
  });
  controller.start();
  clock += 5;
  scheduled();
  assert.equal(tasks.length, 1);
  controller.stop();
  assert.equal(tasks[0].currentSignal.aborted, true);
  assert.equal(scheduled, null);
  controller.start();
  tasks[1].resolve({ targets: targets(core, isp), agedOut: 0 });
  await new Promise(setImmediate);
  assert.equal(rendered.at(-1).health, "partial");
  assert.equal(rendered.at(-1).gatewayRows.length, 2);
  const previous = rendered.length;
  tasks[0].resolve({ targets: [], agedOut: 99 });
  await new Promise(setImmediate);
  assert.equal(rendered.length, previous);
  controller.stop();
});

test("WAN accepts only the two known IF-MIB naming infos; Ping and genuine query warnings stay strict", async () => {
  const previousFetch = global.fetch;
  let warnings = ['PromQL info: metric might not be a counter, name does not end in _total/_sum/_count/_bucket: "ifHCInOctets" (1:20)'];
  global.fetch = async () => ({
    ok: true, json: async () => ({ status: "success", warnings, data: { resultType: "matrix", result: [] } })
  });
  try {
    await assert.rejects(prometheusSamples("rate(ifHCInOctets[1m])", { start: now - 900, end: now, step: 15 }, signal()));
    const read = () => prometheusSamples("rate(ifHCInOctets[1m])", { start: now - 900, end: now, step: 15 },
      signal(), { ifMibCounterNames: true });
    assert.deepEqual(await read(), []);
    warnings = ["query truncated"];
    await assert.rejects(read());
    warnings = ['PromQL info: metric might not be a counter, name does not end in _total/_sum/_count/_bucket: "otherMetric" (1:20)'];
    await assert.rejects(read());
  } finally {
    global.fetch = previousFetch;
  }
});
