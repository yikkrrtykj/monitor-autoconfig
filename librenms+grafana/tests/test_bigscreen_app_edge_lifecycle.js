const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const network = require("../bigscreen/network-read.js");

const appSource = fs.readFileSync(path.resolve(__dirname, "../bigscreen/app.js"), "utf8");
const apiEdges = [{ from_ip: "10.0.0.1", to_ip: "10.0.0.2" }];
const unavailable = { source: "none", data: null };

function harness(options = {}) {
  const elements = new Map();
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, {
      id, dataset: {}, hidden: false, textContent: "", innerHTML: "", className: "",
      querySelectorAll: () => [], addEventListener: () => {}
    });
    return elements.get(id);
  };
  const document = {
    title: "", getElementById: element,
    querySelector: (selector) => selector === ".screen" ? element("screen") : null
  };
  const callbacks = {};
  const timers = new Map();
  let nextTimer = 1;
  let lifecycle;
  let topologyFailure = null;
  let deviceFailure = null;
  const renderedEdges = [];
  const renderedAp = [];
  let apVisible = Boolean(options.apVisible);
  let apRequestCount = 0;
  let renderCount = 0;
  const errors = [];
  const protectedPanel = { open: false, generation: 0 };
  const noop = () => {};
  const noOpPanel = new Proxy({}, { get: () => noop });
  const ns = (factory) => new Proxy({}, { get: (_, key) => key.startsWith("create") ? factory : noop });
  const window = {
    BIGSCREEN_PAGES: [
      { id: "home", path: "/", title: "Home", label: "Home" },
      { id: "topology", path: "/topology", title: "Topology", label: "Topology" }
    ],
    BIGSCREEN_CONFIG: {}, BIGSCREEN_QUERIES: {}, BIGSCREEN_TEAM_LAYOUTS: {},
    location: { pathname: "/", search: "" }, HTMLInputElement: function () {},
    setInterval: (fn, delay) => { const id = nextTimer++; timers.set(id, { fn, delay }); return id; },
    clearInterval: (id) => timers.delete(id),
    setTimeout: noop, clearTimeout: noop, scrollTo: noop,
    addEventListener: (name, fn) => { callbacks[name] = fn; }
  };
  window.BSUtils = new Proxy({}, { get: (_, key) => key === "escapeHtml" ? String : noop });
  window.BSNetworkRead = { ...network, createTopologyLifecycle: () => {
    lifecycle = network.createTopologyLifecycle();
    return lifecycle;
  } };
  window.BSApi = new Proxy({}, { get: (_, key) => {
    if (key === "fetchPlatformAuthStatus") return async () => ({ authenticated: true });
    if (key === "fetchTopologyTargets") return async () => [];
    if (key === "fetchApAttachments") return async () => { apRequestCount++; return { source: 'librenms-fdb', attachments: [] }; };
    if (key === "prometheusInstant") return async () => [];
    if (key === "activeInfraPingQuery") return () => "probe_success";
    if (key === "activeSeriesNames") return () => new Set();
    if (key === "fetchNetworkDevices") return async () => {
      if (deviceFailure) throw Object.assign(new Error("devices failed"), { status: deviceFailure });
      return { ok: true, devices: [] };
    };
    if (key === "fetchNetworkTopology") return async () => {
      if (topologyFailure) throw Object.assign(new Error("topology failed"), { status: topologyFailure });
      return { ok: true, edges: apiEdges };
    };
    if (key === "fetchNetworkIsp") return async () => ({ ok: true, isps: [] });
    return noop;
  } });
  window.BSTopologyPanel = { createTopologyPanel: (dependencies) => {
    callbacks.apToggle = dependencies.onApVisibilityChange;
    return ({
    isAvailable: () => true,
    isApVisible: () => apVisible,
    clearDetail: () => { protectedPanel.open = false; protectedPanel.generation += 1; },
    resetView: noop,
    prepare: (_, edges, apData) => { renderedEdges.push(edges); renderedAp.push(apVisible ? apData : null); return { layout: { nodes: options.nodes || [] }, width: 100 }; },
    render: () => renderCount++, updateLatency: noop, updateStatus: noop,
    showError: (message) => errors.push(message)
  }); } };
  window.BSPortPanel = { createPortPanel: () => noOpPanel };
  window.BSAuthController = {
    createAuthController: (options) => { callbacks.auth = options; return { invalidate: noop, ensureAuthenticated: async () => false }; },
    createControlRefreshLifecycle: () => ({ stop: noop, invalidate: noop, execute: async () => {} })
  };
  window.BSConfigEditor = { createConfigEditor: (options) => { callbacks.config = options; return noOpPanel; } };
  const factories = ["BSLineChart", "BSPingChart", "BSLossHeatmap", "BSIspChart", "BSEvidenceChart",
    "BSEvidencePanel", "BSIncidentPanel", "BSWirelessPanel", "BSTournamentPanel", "BSIperfController",
    "BSDeliveryPanel", "BSIncidentRegistry", "BSInfraController", "BSDhcpPanel", "BSIspCarousel"];
  for (const name of factories) window[name] = ns(() => noOpPanel);
  for (const name of ["BSApTopology", "BSTopology", "BSPlayers", "BSPlatform", "BSIncident", "BSIperf", "BSDhcpModel", "BSConfigModel", "BSPingTransform"]) {
    window[name] = new Proxy({}, { get: () => noop });
  }
  window.BSWirelessPanel = { createWirelessPanel: () => ({ start: noop, stop: noop, clearInspector: noop,
    fetchApStatus: () => options.apPromise || Promise.resolve([]) }) };
  vm.runInNewContext(appSource, { window, document, console: { warn: noop, error: noop },
    Blob, URL, URLSearchParams, Date, fetch: noop }, { filename: "app.js" });
  const navigate = (pathname) => { window.location.pathname = pathname; callbacks.popstate(); };
  const poll = () => {
    const timer = [...timers.values()].find((item) => item.delay === 10000);
    assert(timer, "topology polling timer is active");
    timer.fn();
  };
  return {
    callbacks, lifecycle, navigate, poll, renderedEdges, renderedAp, errors, element, protectedPanel,
    setApVisible: (value) => { apVisible = value; callbacks.apToggle(); },
    apRequestCount: () => apRequestCount,
    renderCount: () => renderCount,
    openProtectedPortPanel: () => { protectedPanel.open = true; return protectedPanel.generation; },
    failTopology: (status) => { topologyFailure = status; },
    failDevices: (status) => { deviceFailure = status; }
  };
}

async function settle() {
  for (let i = 0; i < 16; i++) await Promise.resolve();
}

async function run() {
  const route = harness();
  route.navigate("/topology");
  await settle();
  assert.deepStrictEqual(route.lifecycle.readEdges(unavailable), apiEdges);
  route.navigate("/");
  assert.strictEqual(route.lifecycle.readEdges(unavailable), null, "leaving topology clears API edges");
  route.failTopology(503);
  route.navigate("/topology");
  await settle();
  assert.strictEqual(route.lifecycle.readEdges(unavailable), null, "re-entry 503 does not restore old edges");
  assert(route.errors.length > 0, "first API edge failure renders unavailable");

  const auth = harness();
  auth.lifecycle.readEdges({ source: "network-api", data: { edges: apiEdges } });
  auth.callbacks.auth.onLoggedOut();
  auth.callbacks.auth.onAuthenticated();
  assert.strictEqual(auth.lifecycle.readEdges(unavailable), null, "logout/login clears the old session edges");

  const apply = harness();
  apply.lifecycle.readEdges({ source: "network-api", data: { edges: apiEdges } });
  apply.callbacks.config.onApplyStart();
  assert.strictEqual(apply.lifecycle.readEdges(unavailable), null, "Apply start clears pre-Apply edges");

  for (const status of [401, 403]) {
    const expiry = harness();
    expiry.navigate("/topology");
    await settle();
    assert.deepStrictEqual(expiry.lifecycle.readEdges(unavailable), apiEdges);
    const pendingPortGeneration = expiry.openProtectedPortPanel();
    assert.strictEqual(expiry.protectedPanel.open, true, "the protected Port Panel is open before expiry");
    expiry.failDevices(status);
    const renderedBeforeExpiry = expiry.renderedEdges.length;
    expiry.poll();
    await settle();
    assert.strictEqual(expiry.lifecycle.readEdges(unavailable), null, `${status} current round clears authenticated edges`);
    assert.strictEqual(expiry.protectedPanel.open, false, `${status} closes loaded protected ports`);
    assert.ok(expiry.protectedPanel.generation > pendingPortGeneration,
      `${status} invalidates pending Port Panel responses`);
    assert(expiry.errors.length > 0, `${status} current round renders unavailable`);
    assert.strictEqual(expiry.renderedEdges.length, renderedBeforeExpiry, `${status} cannot repaint old API edges`);
    expiry.failDevices(null);
    expiry.failTopology(503);
    expiry.poll();
    await settle();
    assert.strictEqual(expiry.lifecycle.readEdges(unavailable), null, "post-expiry anonymous round cannot fill API cache");
  }

  const transient = harness();
  transient.navigate("/topology");
  await settle();
  transient.failTopology(503);
  transient.poll();
  await settle();
  assert.deepStrictEqual(transient.renderedEdges.at(-1), apiEdges,
    "ordinary 503 within one lifecycle renders previously successful API edges");
  assert.strictEqual(transient.element("topologyNetworkStatus").textContent, "拓扑不可用",
    "retained API edges remain visibly unavailable");
  assert.strictEqual(transient.lifecycle.readEdges(unavailable)[0].from_ip, apiEdges[0].from_ip);
  const enabled = harness({ apVisible: true });
  enabled.navigate('/topology'); await settle();
  assert.strictEqual(enabled.apRequestCount(), 1, 'ON fetches the separate AP artifact');
  assert.strictEqual(enabled.renderedAp.at(-1).artifact.source, 'librenms-fdb');
  enabled.setApVisible(false); await settle();
  enabled.poll(); await settle();
  assert.strictEqual(enabled.apRequestCount(), 1, 'OFF survives periodic refresh without AP fetch');
  assert.strictEqual(enabled.renderedAp.at(-1), null);
  let resolveAp;
  const pending = harness({ apVisible: true, apPromise: new Promise((resolve) => { resolveAp = resolve; }) });
  pending.navigate('/topology'); await settle();
  pending.setApVisible(false); await settle();
  const writesBefore = pending.renderedAp.length;
  resolveAp([{ ip: '10.1.0.1', mac: '02:00:00:00:00:01' }]); await settle();
  assert.strictEqual(pending.renderedAp.length, writesBefore, 'old pending AP response cannot overwrite the OFF refresh');
  pending.navigate('/'); pending.navigate('/topology'); await settle();
  assert.strictEqual(pending.renderedAp.at(-1), null, 'OFF survives SPA re-entry');
  const apNode = { kind: 'ap', ip: '10.1.0.1', name: 'AP-1', level: 'good', clients: 0,
    parentIp: '10.0.0.11', parentIfindex: 1, switchPort: 'Gi1/0/1' };
  const ownership = harness({ apVisible: true, nodes: [apNode] });
  ownership.navigate('/topology'); await settle();
  const initialRenderCount = ownership.renderCount();
  apNode.parentIp = '10.0.0.12';
  ownership.poll(); await settle();
  assert.strictEqual(ownership.renderCount(), initialRenderCount + 1, 'new authoritative parent redraws AP even when wired edges and AP status are unchanged');
  apNode.parentIfindex = 2; apNode.switchPort = 'Gi1/0/2';
  ownership.poll(); await settle();
  assert.strictEqual(ownership.renderCount(), initialRenderCount + 2, 'new proven access port redraws AP edge labels');
  console.log("bigscreen app edge lifecycle: PASS");
}

run().catch((error) => { console.error(error); process.exitCode = 1; });
