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
  const physicalTargets = [
    { job: "infra-core-ping", targetIp: "10.0.0.1", instance: "10.0.0.1", displayName: "core", success: true, latency: 0.002 },
    { job: "infra-dist-ping", targetIp: "10.0.0.2", instance: "10.0.0.2", displayName: "dist", success: true, latency: 0.003 },
    { job: "infra-dist-ping", targetIp: "10.0.0.3", instance: "10.0.0.3", displayName: "never seen", success: true },
    { job: "infra-isp-ping", targetIp: "10.0.1.1", instance: "10.0.1.1", displayName: "ISP-A", success: true, latency: 0.004 }
  ];
  const fullInventory = [
    { ip: "10.0.0.1", status: "down", name: "core renamed" },
    { ip: "10.0.0.2", status: "unknown", name: "dist renamed" },
    { ip: "10.0.0.3", status: "up" },
    ...Array.from({ length: 20 }, (_, index) => ({ ip: `10.9.0.${index}`, status: "up" }))
  ];
  const topologyDomains = {
    devices: { source: "network-api", data: { devices: fullInventory } },
    isp: { source: "network-api", data: { isps: [{ name: "ISP-A", target: "10.0.1.1", status: "up" }] } }
  };
  const seenUp = new Set(["10.0.0.1", "10.0.0.2"]);
  const apiTopologyTargets = network.topologyNetworkTargets(topologyDomains, physicalTargets, seenUp);
  const legacyTopologyTargets = network.topologyNetworkTargets({
    devices: { source: "legacy", data: physicalTargets },
    isp: { source: "legacy", data: [{ name: "ISP-A", gateway: "10.0.1.1" }] }
  }, physicalTargets, seenUp);
  assert.deepStrictEqual(apiTopologyTargets.map((item) => item.targetIp), legacyTopologyTargets.map((item) => item.targetIp),
    "full LibreNMS inventory cannot expand the existing topology target set");
  assert.deepStrictEqual(apiTopologyTargets.map((item) => item.success), [false, null, true],
    "only matching topology targets receive authoritative API status; unknown remains neutral");
  assert.strictEqual(apiTopologyTargets[0].displayName, "core", "API inventory does not replace topology labels");
  const unavailableTargets = network.topologyNetworkTargets({
    authenticated: true,
    devices: { source: "none", data: null }, isp: { source: "none", data: null }
  }, physicalTargets, seenUp);
  assert.deepStrictEqual(unavailableTargets.map((item) => item.targetIp), apiTopologyTargets.map((item) => item.targetIp));
  assert(unavailableTargets.every((item) => item.success === null && item.status === "unknown"),
    "API outage keeps probe structure but never promotes probe success to authoritative status");
  const partialDevices = network.topologyNetworkTargets({ ...topologyDomains, authenticated: true,
    devices: { source: "network-api", data: { devices: [{ ip: "10.0.0.1", status: "down" }] } }
  }, physicalTargets, seenUp);
  assert.deepStrictEqual(partialDevices.map((item) => item.success), [false, null, true],
    "unmatched devices remain unknown while ISP API status remains authoritative");
  const apiLayout = topologyView.topologyLayout(topologyView.buildTopologyLayers(apiTopologyTargets), 1200, 700, []);
  const legacyLayout = topologyView.topologyLayout(topologyView.buildTopologyLayers(legacyTopologyTargets), 1200, 700, []);
  assert.strictEqual(apiLayout.nodes.length, legacyLayout.nodes.length, "authenticated layout keeps the legacy business node count");
  assert.strictEqual(apiLayout.nodes.find((node) => node.ip === "10.0.0.2").level, "none");
  assert.strictEqual(network.topologyNetworkTargets(topologyDomains, [], seenUp).filter((item) => item.job !== "infra-isp-ping").length, 0,
    "missing probes cannot be replaced by full API inventory");
  const targetCache = network.createTopologyTargetCache();
  assert.throws(() => targetCache.recover(), /拓扑数据暂不可用/, "first probe failure cannot render an empty success");
  assert.strictEqual(targetCache.remember(physicalTargets)[0].latency, 0.002, "fresh probes retain current latency");
  const recoveredTargets = targetCache.recover();
  assert.deepStrictEqual(recoveredTargets.map((item) => item.targetIp), physicalTargets.map((item) => item.targetIp));
  assert(recoveredTargets.every((item) => item.success === null && item.status === "unknown" && item.latency === null),
    "cached structure cannot claim old probe status or latency is current");
  const recoveredApiTargets = network.topologyNetworkTargets(topologyDomains, recoveredTargets, seenUp);
  assert.deepStrictEqual(recoveredApiTargets.map((item) => item.targetIp), apiTopologyTargets.map((item) => item.targetIp),
    "probe failure preserves nodes without adding unrelated LibreNMS devices");
  assert.deepStrictEqual(recoveredApiTargets.map((item) => item.success), [false, null, true],
    "current API status may still cover matching cached devices");
  assert(recoveredApiTargets.every((item) => item.latency === null), "old latency is absent from rendered targets");
  const recoveredLegacyTargets = network.topologyNetworkTargets({
    devices: { source: "none", data: null },
    isp: { source: "legacy", data: [{ name: "ISP-A", gateway: "10.0.1.1" }] }
  }, recoveredTargets, seenUp);
  assert.strictEqual(recoveredLegacyTargets.length, 3, "cached structure survives simultaneous API and probe failure");
  assert(recoveredLegacyTargets.every((item) => item.success === null), "legacy fallback does not reuse old probe states");
  const normalSnapshot = { domains: { devices: { state: "normal" }, topology: { state: "normal" }, isp: { state: "normal" } } };
  assert.strictEqual(network.networkIssueNotice(normalSnapshot.domains), null);
  assert.deepStrictEqual(network.networkIssueNotice(network.topologyReadStatus(normalSnapshot, true).domains), {
    state: "degraded", text: "拓扑数据降级"
  }, "cached structure is visibly degraded rather than reported as a fresh success");
  assert.strictEqual(network.topologyReadStatus(normalSnapshot, false), normalSnapshot);
  assert.strictEqual(network.topologyReadStatus({ domains: { topology: { state: "unavailable" } } }, true).domains.topology.state,
    "unavailable", "probe failure cannot downgrade an existing unavailable warning");
  assert.strictEqual(network.networkIssueNotice({ devices: { state: "normal" }, isp: { state: "legacy-fallback" } }), null);
  assert.strictEqual(network.networkIssueNotice({ isp: { state: "unauthenticated" } }), null);
  assert.deepStrictEqual(network.networkIssueNotice({ devices: { state: "degraded" }, topology: { state: "stale" }, isp: { state: "unavailable" } }), {
    state: "unavailable", text: "设备数据降级 · 拓扑快照陈旧 · ISP不可用"
  });
  assert(!network.networkIssueNotice({ isp: { state: "normal" } }), "healthy ISP does not render a status banner");
  const noticeElement = { dataset: {}, textContent: "", hidden: true };
  network.renderNetworkIssue(noticeElement, { isp: { state: "degraded" } });
  assert.deepStrictEqual(noticeElement, { dataset: { state: "degraded" }, textContent: "ISP数据降级", hidden: false });
  network.renderNetworkIssue(noticeElement, { isp: { state: "legacy-fallback" } });
  assert.deepStrictEqual(noticeElement, { dataset: { state: "" }, textContent: "", hidden: true },
    "normal or legacy recovery removes the notice and its layout space");
  network.renderNetworkIssue(noticeElement, { isp: { state: "unauthenticated" } });
  assert.strictEqual(noticeElement.hidden, true, "anonymous view does not expose source state");
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
  assert.strictEqual(network.apiControlTargets({
    devices: { source: "none", data: null }, isp: { source: "none", data: null }
  }).length, 0, "control never supplements unavailable API domains with legacy inventory");
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
  const beforeApiOnly = { ...calls };
  const noLegacy = network.resolveApiSnapshot({ devices, topology: null, isp });
  assert.strictEqual(noLegacy.domains.topology.state, "unavailable");
  assert.strictEqual(noLegacy.domains.topology.source, "none");
  assert.strictEqual(network.resolveApiSnapshot(null).overall, "unavailable", "503 overview leaves all domains unavailable");
  assert.strictEqual(network.resolveApiDomain({ ok: true }, "topology").source, "none", "malformed success is unavailable");
  assert.deepStrictEqual(calls, beforeApiOnly, "API-only resolution does not touch legacy readers");
  const failedOverview = await network.readApiOverview(async () => { throw Object.assign(new Error("offline"), { status: 503 }); });
  assert.deepStrictEqual(Object.values(failedOverview.domains).map((domain) => domain.state),
    ["unavailable", "unavailable", "unavailable"], "overview 503 does not abort the other control reads");
  assert.deepStrictEqual(calls, beforeApiOnly, "overview 503 cannot invoke legacy readers");
  await assert.rejects(network.readApiOverview(async () => { throw Object.assign(new Error("auth"), { status: 401 }); }),
    (error) => error.status === 401, "control auth expiry still fails closed");
  let trafficCalls = 0;
  let expiredCalls = 0;
  const unavailableTraffic = await network.readApiIspTraffic(
    async () => { throw Object.assign(new Error("offline"), { status: 503 }); },
    async () => { trafficCalls++; return []; }, () => { expiredCalls++; });
  assert.strictEqual(unavailableTraffic.domain.state, "unavailable");
  assert.deepStrictEqual(unavailableTraffic.traffic, []);
  assert.strictEqual(trafficCalls, 0, "infra ISP 503 skips traffic inventory and legacy read");
  assert.deepStrictEqual(calls, beforeApiOnly);
  const degradedTraffic = await network.readApiIspTraffic(async () => isp,
    async (inventory) => { trafficCalls++; assert.strictEqual(inventory.length, 5); return ["series"]; }, () => { expiredCalls++; });
  assert.strictEqual(degradedTraffic.domain.state, "stale");
  assert.deepStrictEqual(degradedTraffic.traffic, ["series"], "degraded/stale API inventory still drives Prometheus traffic");
  await network.readApiIspTraffic(async () => { throw Object.assign(new Error("auth"), { status: 403 }); },
    async () => { trafficCalls++; return []; }, () => { expiredCalls++; });
  assert.strictEqual(expiredCalls, 1, "infra auth expiry invalidates the session");

  const edgeCache = network.createApiEdgeCache();
  assert.strictEqual(edgeCache.read({ source: "none", data: null }), null, "first API edge failure has no invented zero edges");
  assert.deepStrictEqual(edgeCache.read({ source: "network-api", data: { edges: topology.edges } }), topology.edges);
  assert.deepStrictEqual(edgeCache.read({ source: "none", data: null }), topology.edges, "API outage retains last API edge structure");
  edgeCache.clear();
  assert.strictEqual(edgeCache.read({ source: "none", data: null }), null, "anonymous transition cannot reuse authenticated edges");
  const apiEdges = { source: "network-api", data: { edges: topology.edges } };
  const unavailableEdges = { source: "none", data: null };
  const legacyEdges = { source: "legacy", data: [{ from_ip: "legacy", to_ip: "b" }] };
  const edgeLifecycle = network.createTopologyLifecycle();
  const oldPoll = edgeLifecycle.begin();
  edgeLifecycle.readEdges(apiEdges);
  edgeLifecycle.invalidate(); // stopTopologyRefresh / route exit
  assert.strictEqual(edgeLifecycle.isCurrent(oldPoll), false, "route exit invalidates in-flight topology reads");
  assert.strictEqual(edgeLifecycle.readEdges(unavailableEdges), null, "route re-entry 503 cannot reuse prior route edges");

  edgeLifecycle.readEdges(apiEdges);
  edgeLifecycle.invalidate(); // onLoggedOut
  edgeLifecycle.invalidate(); // onAuthenticated
  assert.strictEqual(edgeLifecycle.readEdges(unavailableEdges), null, "logout/login 503 cannot reuse prior session edges");

  edgeLifecycle.readEdges(apiEdges);
  edgeLifecycle.invalidate(); // onApplyStart
  assert.strictEqual(edgeLifecycle.readEdges(unavailableEdges), null, "Apply 503 cannot reuse pre-Apply edges");

  edgeLifecycle.readEdges(apiEdges);
  edgeLifecycle.invalidate(); // current-round authExpired
  assert.strictEqual(edgeLifecycle.readEdges(unavailableEdges), null, "401/403 current round cannot reuse authenticated edges");
  assert.strictEqual(edgeLifecycle.readEdges(unavailableEdges), null, "subsequent polls cannot reuse expired edges");

  edgeLifecycle.readEdges(legacyEdges);
  assert.strictEqual(edgeLifecycle.readEdges(unavailableEdges), null, "anonymous legacy edges never populate API edge cache");
  edgeLifecycle.readEdges(apiEdges);
  assert.deepStrictEqual(edgeLifecycle.readEdges(unavailableEdges), topology.edges,
    "ordinary same-lifecycle 503 retains the last successful API edges");

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
  assert.strictEqual(result.authenticated, true);
  assert.deepStrictEqual(clientCalls, { devices: 1, topology: 1, isp: 1 });
  const beforeStaleFallback = calls.topology;
  assert.strictEqual(calls.topology, beforeStaleFallback);

  clients.topology = async () => { clientCalls.topology++; throw Object.assign(new Error("unavailable"), { status: 503 }); };
  result = await network.loadNetworkDomains(gated, clients, fallback);
  assert.strictEqual(result.domains.topology.source, "none");
  assert.strictEqual(result.domains.topology.apiError.status, 503);
  assert.strictEqual(calls.topology, beforeStaleFallback);
  clients.topology = async () => { clientCalls.topology++; return topology; };
  result = await network.loadNetworkDomains(gated, clients, fallback);
  assert.strictEqual(result.domains.topology.source, "network-api", "API recovers on next poll");

  clients.devices = async () => { clientCalls.devices++; throw Object.assign(new Error("unavailable"), { status: 503 }); };
  clients.isp = async () => { clientCalls.isp++; throw Object.assign(new Error("unavailable"), { status: 503 }); };
  const beforeDomainFailure = { ...calls };
  result = await network.loadNetworkDomains(gated, clients, fallback);
  assert.strictEqual(result.domains.devices.state, "unavailable");
  assert.strictEqual(result.domains.isp.state, "unavailable");
  assert.deepStrictEqual(calls, beforeDomainFailure, "authenticated domain 503 never reads legacy inventory or edges");
  clients.isp = async () => { clientCalls.isp++; return isp; };

  clients.devices = async () => { clientCalls.devices++; throw Object.assign(new Error("auth"), { status: 401 }); };
  result = await network.loadNetworkDomains(gated, clients, fallback);
  assert.strictEqual(result.authExpired, true, "current API round exposes session expiry to topology rendering");
  const beforeExpiredPoll = { ...clientCalls };
  result = await network.loadNetworkDomains(gated, clients, fallback);
  assert.strictEqual(result.authenticated, false, "subsequent poll uses anonymous legacy reads");
  assert.deepStrictEqual(clientCalls, beforeExpiredPoll, "expired session stops protected polls");
  assert.strictEqual(calls.topology, beforeStaleFallback + 1, "anonymous fallback still reads legacy edges after expiry");
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
