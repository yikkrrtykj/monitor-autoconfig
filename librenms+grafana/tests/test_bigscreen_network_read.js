const assert = require("assert");
const path = require("path");
const network = require(path.resolve(__dirname, "../bigscreen/network-read.js"));
const api = require(path.resolve(__dirname, "../bigscreen/api.js"));
const topologyView = require(path.resolve(__dirname, "../bigscreen/topology.js"));
const platform = require(path.resolve(__dirname, "../bigscreen/platform.js"));

const devices = { ok: true, degraded: true, devices: [{ ip: "10.0.0.11", name: "stage-sw", status: "unknown" }] };
const topology = { ok: true, stale: true, edges: [{ from_ip: "a", to_ip: "b" }] };
const isp = { ok: true, degraded: true, stale: true, isps: Array.from({ length: 5 }, (_, index) => ({
  name: `ISP${index}`, target: `10.0.1.${index}`, status: "unknown", metricTarget: "192.168.9.1", metricIfindex: String(index + 1)
})) };

async function run() {
  const requests = [];
  global.fetch = async (url, options) => {
    requests.push({ url, options });
    if (url.endsWith("/network/devices")) return { ok: false, status: 503, json: async () => ({ ok: false, code: "librenms_unavailable", error: "inventory unavailable" }) };
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  };
  await Promise.all([api.fetchNetworkOverview(), api.fetchNetworkTopology(), api.fetchNetworkIsp()]);
  assert.deepStrictEqual(requests.map((item) => item.url).sort(), [
    "/platform-api/network/isp", "/platform-api/network/overview", "/platform-api/network/topology"
  ]);
  assert(requests.every((item) => item.options.credentials === "same-origin"));
  await assert.rejects(api.fetchNetworkDevices(), (error) => error.status === 503 &&
    error.payload.code === "librenms_unavailable" && error.payload.error === "inventory unavailable");
  global.fetch = async () => ({ ok: false, status: 401, json: async () => ({ ok: false, code: "authentication_required", error: "login required" }) });
  await assert.rejects(api.fetchNetworkOverview(), (error) => error.status === 401 &&
    error.payload.code === "authentication_required" && error.payload.error === "login required");

  let calls = { devices: 0, topology: 0, isp: 0 };
  const fallback = {
    devices: async () => { calls.devices++; return []; },
    topology: async () => { calls.topology++; return [{ from_ip: "legacy", to_ip: "b" }]; },
    isp: async () => { calls.isp++; return []; }
  };
  let result = await network.resolveNetworkSnapshot({ devices: { ...devices, degraded: false }, topology: { ...topology, stale: false, degraded: false }, isp: { ...isp, stale: false, degraded: false } }, fallback);
  assert.strictEqual(result.overall, "normal");
  assert.deepStrictEqual(calls, { devices: 0, topology: 0, isp: 0 });

  result = await network.resolveNetworkSnapshot({ devices, topology, isp }, fallback);
  assert.strictEqual(result.domains.devices.state, "degraded");
  assert.strictEqual(result.domains.topology.state, "stale");
  assert.strictEqual(result.domains.isp.state, "stale");
  assert.strictEqual(result.domains.isp.data.isps.length, 5);
  assert.deepStrictEqual(calls, { devices: 0, topology: 0, isp: 0 });

  const enriched = network.mergeNetworkDevices(devices.devices, [{ targetIp: "10.0.0.11", job: "infra-dist-ping", displayName: "stage-sw", latency: 0.0015, success: true }]);
  assert.strictEqual(enriched[0].success, null);
  assert.strictEqual(enriched[0].latency, 0.0015);
  assert.strictEqual(enriched[0].job, "infra-dist-ping");
  assert.strictEqual(platform.summarizeTargets(enriched).offline.length, 0, "unknown is not offline in control readiness");
  const withoutMetadata = network.mergeNetworkDevices(devices.devices);
  assert.strictEqual(platform.summarizeTargets(withoutMetadata).byKind.other, 1, "control does not invent a device kind");
  const topologyTargets = network.mergeNetworkDevices(devices.devices, [{ targetIp: "10.0.0.11", latency: 0.0015 }]);
  const unknownNode = topologyView.buildTopologyLayers(topologyTargets).dists[0];
  assert.strictEqual(unknownNode.kind, "device", "missing metric metadata does not invent a device type");
  assert.strictEqual(unknownNode.level, "none", "unknown topology node remains neutral");
  assert.strictEqual(unknownNode.success, null);
  assert(topologyView.renderTopologySvg(topologyView.topologyLayout(topologyView.buildTopologyLayers(topologyTargets), 1200, 700, []), 1200).includes("状态未知"));
  assert.strictEqual(network.mergeNetworkDevices([{ ip: "10.0.0.11", status: "down" }], [{ targetIp: "10.0.0.11", success: true }])[0].success, false);
  assert.strictEqual(network.mergeNetworkDevices([{ ip: "10.0.0.11", status: "up" }], [{ targetIp: "10.0.0.11", success: false }])[0].success, true);
  assert.strictEqual(network.mergeIspInventory(isp.isps, []).length, 5);
  assert.strictEqual(network.mergeLegacyIspInventory([], [{ job: "infra-isp-ping", targetIp: "10.0.1.9", displayName: "legacy", success: true }]).length, 1,
    "anonymous topology keeps legacy probe-only ISP nodes");
  const manualInventory = [{ name: "ISP-A", gateway: "" }, { name: "ISP-B", target: "10.0.1.2" }];
  const ispProbes = [
    { job: "infra-isp-ping", targetIp: "10.0.1.1", displayName: "ISP-A", success: false, latency: 0.021 },
    { job: "infra-isp-ping", targetIp: "10.0.1.2", displayName: "different label", success: true, latency: 0.012 }
  ];
  const matchedIsp = network.mergeLegacyIspInventory(manualInventory, ispProbes);
  assert.strictEqual(matchedIsp.length, 2, "matched probes are not duplicated");
  assert.deepStrictEqual(matchedIsp.map((item) => item.success), [false, true]);
  assert.deepStrictEqual(matchedIsp.map((item) => item.latency), [0.021, 0.012]);
  assert.strictEqual(platform.summarizeTargets(matchedIsp).offline.length, 1, "manual ISP outage reaches control readiness");
  const ambiguous = network.mergeLegacyIspInventory([{ name: "ISP-A", gateway: "" }], [
    ispProbes[0], { ...ispProbes[0], targetIp: "10.0.1.3" }
  ]);
  assert.strictEqual(ambiguous[0].success, null, "ambiguous names do not invent a status");
  let enrichmentCalls = 0;
  const controlDomains = {
    devices: { source: "network-api", data: devices },
    isp: { source: "legacy", data: manualInventory }
  };
  const controlTargets = await network.controlNetworkTargets(controlDomains, async () => { enrichmentCalls++; return ispProbes; });
  assert.strictEqual(enrichmentCalls, 1, "ISP fallback fetches probes even when devices API succeeds");
  assert.strictEqual(platform.summarizeTargets(controlTargets).offline.length, 1);
  const missingProbes = await network.controlNetworkTargets(controlDomains, async () => { throw new Error("Prometheus unavailable"); });
  assert.strictEqual(missingProbes[1].success, null, "failed enrichment keeps inventory with unknown status");
  await network.controlNetworkTargets({ ...controlDomains, isp: { source: "network-api", data: isp } }, async () => { enrichmentCalls++; return ispProbes; });
  assert.strictEqual(enrichmentCalls, 1, "healthy ISP API does not fetch fallback probes");
  const reusedTargets = await network.controlNetworkTargets({
    devices: { source: "legacy", data: ispProbes }, isp: { source: "legacy", data: manualInventory }
  }, async () => { throw new Error("must reuse device fallback"); });
  assert.strictEqual(platform.summarizeTargets(reusedTargets).offline.length, 1);
  const trafficQueries = [];
  global.fetch = async (url) => {
    trafficQueries.push(String(url));
    return { ok: true, status: 200, json: async () => ({ status: "success", data: { result: [{ metric: {}, values: [[2_000_000_000, "123"]] }] } }) };
  };
  const traffic = await api.fetchIspTraffic(isp.isps.map((item) => ({
    name: item.name, metricTarget: item.metricTarget, metricIfindex: item.metricIfindex
  })));
  assert.strictEqual(traffic.length, 5);
  assert.strictEqual(trafficQueries.length, 10, "degraded ISP inventory still drives both traffic queries");
  assert(trafficQueries.every((url) => decodeURIComponent(url).includes('instance="192.168.9.1"')));

  result = await network.resolveNetworkSnapshot({ devices, topology: null, isp }, fallback);
  assert.strictEqual(result.overall, "partial-unavailable");
  assert.deepStrictEqual(calls, { devices: 0, topology: 1, isp: 0 });
  assert.strictEqual(result.domains.topology.source, "legacy");
  assert.strictEqual(result.domains.devices.source, "network-api");
  assert.strictEqual(result.domains.isp.source, "network-api");
  assert.strictEqual((await network.resolveNetworkDomain({ ok: true }, fallback.topology, "topology")).source, "legacy", "malformed 200 is not an empty topology");
  result = await network.resolveNetworkSnapshot({ devices, topology: null, isp,
    warnings: [{ domain: "topology", code: "topology_unavailable", error: "snapshot unavailable" }] }, fallback);
  assert.strictEqual(result.domains.topology.apiError.code, "topology_unavailable");

  result = await network.resolveNetworkSnapshot(null, fallback);
  assert.strictEqual(result.overall, "legacy-fallback");
  assert.deepStrictEqual(calls, { devices: 1, topology: 4, isp: 1 });
  result = await network.resolveNetworkSnapshot(null, { ...fallback, topology: async () => { throw new Error("offline"); } });
  assert.strictEqual(result.overall, "partial-unavailable");
  assert.strictEqual(result.domains.topology.state, "unavailable");

  let now = 0;
  let authenticated = false;
  let authCalls = 0;
  let apiCalls = 0;
  const session = network.createNetworkSession(async () => { authCalls++; return { authenticated }; }, () => now, 45000);
  for (let poll = 0; poll < 5; poll++) {
    if (await session.canRead()) apiCalls++;
    now += 10000;
  }
  assert.strictEqual(apiCalls, 0);
  assert.strictEqual(authCalls, 1);
  authenticated = true;
  assert.strictEqual(await session.canRead(), true);
  apiCalls++;
  session.expired();
  for (let poll = 0; poll < 4; poll++) {
    if (await session.canRead()) apiCalls++;
    now += 10000;
  }
  assert.strictEqual(apiCalls, 1);
  now += 10000;
  assert.strictEqual(await session.canRead(), true);
  assert.strictEqual(authCalls, 3);
  assert.strictEqual(network.isAuthError({ status: 401 }), true);
  assert.strictEqual(network.isAuthError({ status: 503 }), false);
  let authState = { authenticated: true };
  let transientCalls = 0;
  let transientNow = 0;
  const transientSession = network.createNetworkSession(async () => { transientCalls++; return authState; }, () => transientNow, 45000);
  assert.strictEqual(await transientSession.canRead(), true);
  transientNow = 45000;
  authState = { authenticated: false, transient: true };
  assert.strictEqual(await transientSession.canRead(), true, "temporary auth transport failure preserves known login");
  transientNow += 4999;
  assert.strictEqual(await transientSession.canRead(), true);
  assert.strictEqual(transientCalls, 2);
  transientNow++;
  authState = { authenticated: true };
  assert.strictEqual(await transientSession.canRead(), true, "transient status is retried after five seconds");
  assert.strictEqual(transientCalls, 3);
  transientSession.invalidate();
  authState = { authenticated: false, transient: true };
  assert.strictEqual(await transientSession.canRead(), false, "unknown login is not granted by transient failure");
  transientNow += 5000;
  authState = { authenticated: false };
  assert.strictEqual(await transientSession.canRead(), false, "confirmed logout remains logged out");
  transientNow += 45000;
  authState = { authenticated: false, transient: true };
  assert.strictEqual(await transientSession.canRead(), false);
  transientNow += 5000;
  authState = { authenticated: true };
  assert.strictEqual(await transientSession.canRead(), true, "transient failure while logged out retries promptly");
  let releaseAuth;
  const delayed = network.createNetworkSession(() => new Promise((resolve) => { releaseAuth = resolve; }), () => now, 45000);
  const pendingAuth = delayed.canRead();
  await Promise.resolve();
  delayed.expired();
  releaseAuth({ authenticated: true });
  assert.strictEqual(await pendingAuth, false, "old auth response cannot revive an expired session");

  let clientCalls = { devices: 0, topology: 0, isp: 0 };
  let authenticatedMode = false;
  const gated = network.createNetworkSession(async () => ({ authenticated: authenticatedMode }), () => now, 45000);
  const clients = {
    devices: async () => { clientCalls.devices++; return devices; },
    topology: async () => { clientCalls.topology++; return topology; },
    isp: async () => { clientCalls.isp++; return isp; }
  };
  result = await network.loadNetworkDomains(gated, clients, fallback);
  assert.strictEqual(result.overall, "unauthenticated");
  await network.loadNetworkDomains(gated, clients, fallback);
  assert.deepStrictEqual(clientCalls, { devices: 0, topology: 0, isp: 0 }, "anonymous polls never call protected API");
  authenticatedMode = true;
  gated.invalidate();
  result = await network.loadNetworkDomains(gated, clients, fallback);
  assert.strictEqual(result.domains.topology.state, "stale");
  assert.deepStrictEqual(clientCalls, { devices: 1, topology: 1, isp: 1 });
  const beforeStaleFallback = calls.topology;
  assert.strictEqual(calls.topology, beforeStaleFallback);

  clients.topology = async () => { clientCalls.topology++; throw Object.assign(new Error("unavailable"), { status: 503 }); };
  result = await network.loadNetworkDomains(gated, clients, fallback);
  assert.strictEqual(result.domains.topology.source, "legacy");
  assert.strictEqual(result.domains.topology.apiError.status, 503);
  assert.strictEqual(calls.topology, beforeStaleFallback + 1);
  clients.topology = async () => { clientCalls.topology++; return topology; };
  result = await network.loadNetworkDomains(gated, clients, fallback);
  assert.strictEqual(result.domains.topology.source, "network-api", "API recovers on next poll");

  clients.devices = async () => { clientCalls.devices++; throw Object.assign(new Error("auth"), { status: 401 }); };
  await network.loadNetworkDomains(gated, clients, fallback);
  const beforeExpiredPoll = { ...clientCalls };
  await network.loadNetworkDomains(gated, clients, fallback);
  assert.deepStrictEqual(clientCalls, beforeExpiredPoll, "expired session stops protected polls");
  clients.devices = async () => { clientCalls.devices++; return devices; };
  now += 45000;
  result = await network.loadNetworkDomains(gated, clients, fallback);
  assert.strictEqual(result.domains.devices.source, "network-api", "session recovery restores primary");

  let apiAvailable = false;
  async function cycle() {
    return network.resolveNetworkDomain(apiAvailable ? topology : null, fallback.topology);
  }
  assert.strictEqual((await cycle()).source, "legacy");
  const afterFallback = calls.topology;
  apiAvailable = true;
  assert.strictEqual((await cycle()).source, "network-api");
  assert.strictEqual(calls.topology, afterFallback, "stale API must not invoke fallback after recovery");

  console.log("network read adapter: PASS");
}

run().catch((error) => { console.error(error); process.exitCode = 1; });
