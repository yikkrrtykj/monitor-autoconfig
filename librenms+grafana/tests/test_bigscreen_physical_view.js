const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

global.window = {
  BIGSCREEN_CONFIG: {},
  BIGSCREEN_QUERIES: {},
  BIGSCREEN_PAGES: []
};

const { projectPhysicalTopology } = require(path.resolve(
  __dirname,
  "../bigscreen/physical-topology.js"
));
const {
  buildTopologyLayers,
  topologyLayout,
  renderTopologySvg,
  physicalTopologyLayout,
  physicalTopologySignature,
  renderPhysicalTopologySvg
} = require(path.resolve(__dirname, "../bigscreen/topology.js"));

const physicalEdge = (overrides = {}) => ({
  edge_type: "physical",
  from_ip: "10.0.0.1",
  from_sysname: "Switch-A",
  from_port: "Gi1/0/1",
  from_ifindex: 101,
  to_ip: "10.0.0.2",
  to_sysname: "Switch-B",
  to_port: "Gi1/0/2",
  to_ifindex: 202,
  protocols: ["lldp"],
  stale: false,
  last_seen: 100,
  ...overrides
});

const reversePhysicalEdge = (edge) => {
  const reversed = {};
  Object.entries(edge).forEach(([key, value]) => {
    if (key.startsWith("from_")) reversed[`to_${key.slice(5)}`] = value;
    else if (key.startsWith("to_")) reversed[`from_${key.slice(3)}`] = value;
    else reversed[key] = value;
  });
  return reversed;
};

const target = (ip, displayName, job = "infra-dist-ping", overrides = {}) => ({
  targetIp: ip,
  instance: ip,
  displayName,
  job,
  success: true,
  latency: 0.002,
  ...overrides
});

const layoutFor = (edges, targets = []) => physicalTopologyLayout(
  projectPhysicalTopology(edges),
  targets,
  900,
  560
);

// Accepted projection devices are the only node/connectivity source. Targets decorate only.
const ordinaryEdges = [physicalEdge()];
const ordinaryProjection = projectPhysicalTopology(ordinaryEdges);
const ordinaryLayout = physicalTopologyLayout(ordinaryProjection, [
  target("10.0.0.1", "Target-A", "infra-core-ping"),
  target("10.0.0.2", "Target-B"),
  target("10.0.0.99", "Unconnected target")
], 900, 560);
assert.strictEqual(ordinaryLayout.links.length, 1);
assert.deepStrictEqual(ordinaryLayout.nodes.map((node) => node.ip).sort(), ["10.0.0.1", "10.0.0.2"]);
assert.strictEqual(ordinaryLayout.nodes.find((node) => node.ip === "10.0.0.1").name, "Target-A");
assert.strictEqual(ordinaryLayout.nodes.find((node) => node.ip === "10.0.0.1").kind, "core");
const ordinarySvg = renderPhysicalTopologySvg(ordinaryLayout, ordinaryLayout.width);
assert.strictEqual((ordinarySvg.match(/topology-link--physical/g) || []).length, 1);
assert.ok(!ordinarySvg.includes("topology-backbone"));
assert.ok(!ordinarySvg.includes("topology-ha-bond"));
assert.ok(!ordinarySvg.includes("data-protocols"), "protocol metadata is deferred to Stage 3C");

// ACCEPTED_ENDPOINT_WITHOUT_TARGET / ACCEPTED_ENDPOINT_SURVIVES_SEENUP_FILTER:
// accepted projection endpoints remain facts even when target decoration is missing.
const onlyATarget = [target("10.0.0.1", "Target-A", "infra-core-ping")];
const missingTargetLayout = physicalTopologyLayout(ordinaryProjection, onlyATarget, 900, 560);
assert.deepStrictEqual(
  missingTargetLayout.nodes.map((node) => node.ip).sort(),
  ["10.0.0.1", "10.0.0.2"]
);
assert.strictEqual(missingTargetLayout.links.length, 1);
assert.strictEqual(missingTargetLayout.nodes.find((node) => node.ip === "10.0.0.2").level, "none");
assert.strictEqual(
  (renderPhysicalTopologySvg(missingTargetLayout, missingTargetLayout.width)
    .match(/topology-link--physical/g) || []).length,
  1
);

const rawSeenUpTargets = [
  target("10.0.0.1", "Target-A", "infra-core-ping"),
  target("10.0.0.2", "Target-B")
];
const seenUpFilteredTargets = rawSeenUpTargets.filter((item) => item.instance !== "10.0.0.2");
const seenUpLayout = physicalTopologyLayout(ordinaryProjection, seenUpFilteredTargets, 900, 560);
assert.ok(seenUpLayout.nodes.some((node) => node.ip === "10.0.0.2"));
assert.strictEqual(seenUpLayout.links.length, 1);

// Parallel physical identities use stable, visibly distinct geometry.
const parallelEdges = [
  physicalEdge(),
  physicalEdge({
    from_port: "Gi1/0/3",
    from_ifindex: 103,
    to_port: "Gi1/0/4",
    to_ifindex: 204
  }),
  physicalEdge({
    from_port: "Gi1/0/5",
    from_ifindex: 105,
    to_port: "Gi1/0/6",
    to_ifindex: 206
  })
];
const parallelLayout = layoutFor(parallelEdges);
assert.strictEqual(parallelLayout.links.length, 3);
assert.deepStrictEqual(parallelLayout.links.map((link) => link.parallelOffset), [-12, 0, 12]);
const parallelSvg = renderPhysicalTopologySvg(parallelLayout, parallelLayout.width);
const parallelPaths = Array.from(parallelSvg.matchAll(
  /<path class="topology-link topology-link--physical[^"]*" d="([^"]+)"/g
)).map((match) => match[1]);
assert.strictEqual(parallelPaths.length, 3);
assert.strictEqual(new Set(parallelPaths).size, 3, "three parallel links never overlap as one path");

const pathById = (svg) => Object.fromEntries(Array.from(svg.matchAll(
  /data-link-id="([^"]+)"[^>]*>\s*<path class="[^"]+" d="([^"]+)"/g
)).map((match) => [match[1], match[2]]));
const reversedParallelLayout = layoutFor(parallelEdges.slice().reverse());
const reversedParallelSvg = renderPhysicalTopologySvg(
  reversedParallelLayout,
  reversedParallelLayout.width
);
assert.deepStrictEqual(
  pathById(reversedParallelSvg),
  pathById(parallelSvg),
  "stable projected link IDs retain the same parallel path geometry after source reorder"
);
assert.deepStrictEqual(
  Object.fromEntries(reversedParallelLayout.links.map((link) => [link.id, link.parallelOffset])),
  Object.fromEntries(parallelLayout.links.map((link) => [link.id, link.parallelOffset]))
);

// A two-sided LAG renders once and never invents cross-end member pairing.
const lagRows = [
  physicalEdge({
    from_port: "Te1/0/2",
    from_ifindex: 102,
    from_aggregate_port: "Po11",
    from_member_ports: ["Te1/0/2", "Te2/0/2"],
    to_port: "Te1/0/1",
    to_ifindex: 101,
    to_aggregate_port: "Po11",
    to_member_ports: ["Te1/0/1", "Te2/0/1"],
    stale: true
  }),
  physicalEdge({
    from_port: "Te2/0/2",
    from_ifindex: 202,
    from_aggregate_port: "Po11",
    from_member_ports: ["Te1/0/2", "Te2/0/2"],
    to_port: "Te2/0/1",
    to_ifindex: 201,
    to_aggregate_port: "Po11",
    to_member_ports: ["Te1/0/1", "Te2/0/1"],
    stale: false
  })
];
const lagProjection = projectPhysicalTopology(lagRows);
const lagLayout = physicalTopologyLayout(lagProjection, [], 900, 560);
const lagSvg = renderPhysicalTopologySvg(lagLayout, lagLayout.width);
assert.strictEqual(lagProjection.bundles.length, 1);
assert.strictEqual(lagLayout.links.length, 1);
assert.strictEqual((lagSvg.match(/topology-link--bundle/g) || []).length, 1);
assert.ok(lagSvg.includes("topology-link--partial-stale"));
const partialBundleClass = lagSvg.match(
  /<path class="([^"]*topology-link--bundle[^"]*)"/
)[1];
assert.ok(!partialBundleClass.split(" ").includes("topology-link--stale"));
for (const member of ["Te1/0/2", "Te2/0/2", "Te1/0/1", "Te2/0/1"]) {
  assert.ok(!lagSvg.includes(`>${member}<`), "bundle member arrays are not rendered as cable pairs");
}
assert.ok(lagSvg.includes(">Po11<"));

// BUNDLE_AND_ORDINARY_SAME_DEVICE_PAIR: a bundle and an independent cable
// remain two distinct visual facts even though their device pair is identical.
const independentSamePair = physicalEdge({
  from_port: "Gi1/0/10",
  from_ifindex: 110,
  to_port: "Gi1/0/10",
  to_ifindex: 210
});
const bundleAndOrdinaryProjection = projectPhysicalTopology([
  ...lagRows,
  independentSamePair
]);
const bundleAndOrdinaryLayout = physicalTopologyLayout(
  bundleAndOrdinaryProjection,
  [],
  900,
  560
);
const bundleAndOrdinarySvg = renderPhysicalTopologySvg(
  bundleAndOrdinaryLayout,
  bundleAndOrdinaryLayout.width
);
assert.strictEqual(bundleAndOrdinaryProjection.bundles.length, 1);
assert.strictEqual(bundleAndOrdinaryProjection.physicalLinks.length, 1);
assert.strictEqual(bundleAndOrdinaryLayout.links.length, 2);
assert.strictEqual((bundleAndOrdinarySvg.match(/topology-link--bundle/g) || []).length, 1);
assert.strictEqual((bundleAndOrdinarySvg.match(/topology-link--physical/g) || []).length, 1);

// One-sided aggregate evidence remains an ordinary link with honest endpoint labels.
const oneSidedProjection = projectPhysicalTopology([physicalEdge({
  from_port: "Gi1/0/7",
  from_ifindex: 107,
  from_aggregate_port: "Po21",
  from_member_ports: ["Gi1/0/7"],
  to_port: "Gi27",
  to_ifindex: 27
})]);
const oneSidedLayout = physicalTopologyLayout(oneSidedProjection, [], 900, 560);
const oneSidedSvg = renderPhysicalTopologySvg(oneSidedLayout, oneSidedLayout.width);
assert.strictEqual(oneSidedProjection.physicalLinks.length, 1);
assert.strictEqual(oneSidedProjection.bundles.length, 0);
assert.strictEqual(oneSidedLayout.links[0].kind, "physical");
assert.ok(oneSidedSvg.includes("Po21 / Gi1/0/7"));
assert.ok(oneSidedSvg.includes(">Gi27<"));
assert.ok(!oneSidedSvg.includes("topology-link--bundle"));

// Server attachment has its own class and never fabricates a server NIC.
const attachmentEdge = {
  edge_type: "server_attachment",
  from_ip: "10.0.0.1",
  from_sysname: "Switch-A",
  from_port: "Gi1/0/48",
  from_ifindex: 148,
  to_ip: "192.0.2.20",
  to_sysname: "Server-20",
  to_port: null,
  to_ifindex: null,
  source: "fdb",
  server_mac: "00:11:22:33:44:55",
  server_vlan: 20
};
const attachmentLayout = layoutFor([attachmentEdge], [
  target("10.0.0.1", "Switch-A"),
  target("192.0.2.20", "Server-20", "infra-srv-ping")
]);
const attachmentSvg = renderPhysicalTopologySvg(attachmentLayout, attachmentLayout.width);
assert.strictEqual(attachmentLayout.links[0].kind, "attachment");
assert.ok(attachmentSvg.includes("topology-link--attachment"));
assert.ok(attachmentSvg.includes(">Gi1/0/48<"));
assert.ok(!attachmentSvg.includes("eth0"));
assert.ok(!attachmentSvg.includes("nic0"));

// ACCEPTED_SERVER_WITHOUT_TARGET: an accepted attachment authorizes both
// endpoints even when Prometheus has no server target to decorate the server.
const missingServerTargetLayout = layoutFor([attachmentEdge], [
  target("10.0.0.1", "Switch-A")
]);
const missingServerTargetSvg = renderPhysicalTopologySvg(
  missingServerTargetLayout,
  missingServerTargetLayout.width
);
assert.deepStrictEqual(
  missingServerTargetLayout.nodes.map((node) => node.ip).sort(),
  ["10.0.0.1", "192.0.2.20"]
);
assert.strictEqual(missingServerTargetLayout.links.length, 1);
assert.strictEqual((missingServerTargetSvg.match(/topology-link--attachment/g) || []).length, 1);

// STALE_RENDER_MATRIX: only explicit stale facts receive the full-stale class.
const freshStaleMatrixEdge = physicalEdge({
  from_ip: "10.0.0.5",
  from_sysname: "Switch-E",
  from_port: "Gi5",
  from_ifindex: 5,
  to_ip: "10.0.0.6",
  to_sysname: "Switch-F",
  to_port: "Gi6",
  to_ifindex: 6,
  stale: false
});
const unknownStale = physicalEdge({
  from_ip: "10.0.0.3",
  from_sysname: "Switch-C",
  from_port: "Gi3",
  from_ifindex: 3,
  to_ip: "10.0.0.4",
  to_sysname: "Switch-D",
  to_port: "Gi4",
  to_ifindex: 4
});
delete unknownStale.stale;
const staleLayout = layoutFor([
  physicalEdge({ stale: true }),
  freshStaleMatrixEdge,
  unknownStale
]);
const staleSvg = renderPhysicalTopologySvg(staleLayout, staleLayout.width);
assert.strictEqual((staleSvg.match(/topology-link--physical/g) || []).length, 3);
assert.strictEqual((staleSvg.match(/topology-link--stale/g) || []).length, 1);

const allStaleLagProjection = projectPhysicalTopology(
  lagRows.map((edge) => ({ ...edge, stale: true }))
);
const allStaleLagLayout = physicalTopologyLayout(allStaleLagProjection, [], 900, 560);
const allStaleLagSvg = renderPhysicalTopologySvg(allStaleLagLayout, allStaleLagLayout.width);
const allStaleBundleClass = allStaleLagSvg.match(
  /<path class="([^"]*topology-link--bundle[^"]*)"/
)[1];
assert.ok(allStaleBundleClass.split(" ").includes("topology-link--stale"));
assert.ok(!allStaleBundleClass.split(" ").includes("topology-link--partial-stale"));

// Multiple protocols remain one projected and visual link.
const protocolLayout = layoutFor([physicalEdge({ protocols: ["cdp", "lldp", "xdp"] })]);
const protocolSvg = renderPhysicalTopologySvg(protocolLayout, protocolLayout.width);
assert.strictEqual(protocolLayout.links.length, 1);
assert.strictEqual((protocolSvg.match(/topology-link--physical/g) || []).length, 1);

// Empty accepted edges have an explicit state and zero synthetic SVG links.
const emptyLayout = physicalTopologyLayout(projectPhysicalTopology([]), [
  target("10.0.0.99", "Target-only")
], 900, 560);
const emptySvg = renderPhysicalTopologySvg(emptyLayout, emptyLayout.width);
assert.deepStrictEqual(emptyLayout.nodes, []);
assert.deepStrictEqual(emptyLayout.links, []);
assert.ok(emptySvg.includes("No accepted physical topology"));
assert.ok(!emptySvg.includes("<path"));

// PHYSICAL_EMPTY_VS_OPERATIONS_SYNTHETIC: the same Core/Dist targets retain
// Operations fallback connectivity but create no Physical facts without edges.
const emptyComparisonTargets = [
  target("10.0.0.1", "Core", "infra-core-ping"),
  target("10.0.0.2", "Dist", "infra-dist-ping")
];
const emptyOperationsLayers = buildTopologyLayers(emptyComparisonTargets);
const emptyOperationsLayout = topologyLayout(emptyOperationsLayers, 900, 560, []);
const emptyOperationsSvg = renderTopologySvg(emptyOperationsLayout, 900);
const emptyPhysicalComparisonLayout = physicalTopologyLayout(
  projectPhysicalTopology([]),
  emptyComparisonTargets,
  900,
  560
);
const emptyPhysicalComparisonSvg = renderPhysicalTopologySvg(
  emptyPhysicalComparisonLayout,
  emptyPhysicalComparisonLayout.width
);
assert.ok(emptyOperationsLayout.links.length > 0, "Operations keeps its synthetic fallback");
assert.ok(emptyOperationsSvg.includes("<path"));
assert.strictEqual(emptyPhysicalComparisonLayout.links.length, 0);
assert.ok(emptyPhysicalComparisonSvg.includes("No accepted physical topology"));
assert.ok(!emptyPhysicalComparisonSvg.includes("<path"));

// Complete Physical output is deterministic under raw edge and target reorder.
const mixedEdges = [...parallelEdges, ...lagRows, attachmentEdge];
const mixedTargets = [
  target("10.0.0.1", "Switch-A", "infra-core-ping"),
  target("10.0.0.2", "Switch-B"),
  target("192.0.2.20", "Server-20", "infra-srv-ping")
];
const mixedForward = physicalTopologyLayout(
  projectPhysicalTopology(mixedEdges),
  mixedTargets,
  900,
  560
);
const mixedReverse = physicalTopologyLayout(
  projectPhysicalTopology(mixedEdges.slice().reverse()),
  mixedTargets.slice().reverse(),
  900,
  560
);
assert.deepStrictEqual(mixedReverse, mixedForward);
assert.strictEqual(
  renderPhysicalTopologySvg(mixedReverse, mixedReverse.width),
  renderPhysicalTopologySvg(mixedForward, mixedForward.width)
);
assert.strictEqual(
  physicalTopologySignature(mixedReverse, mixedReverse.width),
  physicalTopologySignature(mixedForward, mixedForward.width),
  "Physical signature is invariant under complete raw input reorder"
);

// Physical signature tracks visual identity/metadata but ignores 3C-only fields.
const signatureFor = (edges, targets = []) => {
  const layout = layoutFor(edges, targets);
  return physicalTopologySignature(layout, layout.width);
};
const baseSignature = signatureFor([physicalEdge()]);
assert.notStrictEqual(
  signatureFor([physicalEdge({ from_port: "Gi1/0/9", from_ifindex: 109 })]),
  baseSignature,
  "physical identity changes the signature"
);
assert.notStrictEqual(
  signatureFor([physicalEdge({
    from_aggregate_port: "Po11",
    from_member_ports: ["Gi1/0/1", "Gi1/0/9"]
  })]),
  baseSignature,
  "aggregate/member presentation changes the signature"
);
assert.notStrictEqual(signatureFor([physicalEdge({ stale: true })]), baseSignature);
assert.strictEqual(signatureFor([physicalEdge({ last_seen: 999 })]), baseSignature);
assert.strictEqual(signatureFor([physicalEdge({ protocols: ["cdp", "lldp"] })]), baseSignature);

// PHYSICAL_NODE_* and latency jitter: render-relevant node state changes the
// structural signature, while raw latency within the same level does not.
const goodNodeTargets = [
  target("10.0.0.1", "Target-A", "infra-core-ping", { success: true, latency: 0.002 }),
  target("10.0.0.2", "Target-B")
];
const badNodeTargets = [
  target("10.0.0.1", "Target-A", "infra-core-ping", { success: false, latency: null }),
  target("10.0.0.2", "Target-B")
];
const roleChangedTargets = [
  target("10.0.0.1", "Target-A", "infra-dist-ping", { success: true, latency: 0.002 }),
  target("10.0.0.2", "Target-B")
];
const renamedTargets = [
  target("10.0.0.1", "Target-A-Renamed", "infra-core-ping", { success: true, latency: 0.002 }),
  target("10.0.0.2", "Target-B")
];
const jitteredTargets = [
  target("10.0.0.1", "Target-A", "infra-core-ping", { success: true, latency: 0.003 }),
  target("10.0.0.2", "Target-B")
];
const goodNodeSignature = signatureFor(ordinaryEdges, goodNodeTargets);
assert.notStrictEqual(signatureFor(ordinaryEdges, badNodeTargets), goodNodeSignature);
assert.notStrictEqual(signatureFor(ordinaryEdges, roleChangedTargets), goodNodeSignature);
assert.notStrictEqual(signatureFor(ordinaryEdges, renamedTargets), goodNodeSignature);
assert.strictEqual(signatureFor(ordinaryEdges, jitteredTargets), goodNodeSignature);
const goodNodeSvg = renderPhysicalTopologySvg(
  layoutFor(ordinaryEdges, goodNodeTargets),
  900
);
const badNodeSvg = renderPhysicalTopologySvg(
  layoutFor(ordinaryEdges, badNodeTargets),
  900
);
assert.ok(goodNodeSvg.includes("node-good"));
assert.ok(badNodeSvg.includes("node-bad"));
assert.ok(!goodNodeSvg.includes("Target-A-Renamed"));
assert.ok(renderPhysicalTopologySvg(layoutFor(ordinaryEdges, renamedTargets), 900)
  .includes("Target-A-Renamed"));

// PHYSICAL_SIGNATURE_DUPLICATE_EQUIVALENCE locks the Stage 3B signature and
// rendered count to the projected model rather than noisy raw rows.
const duplicateEquivalentEdges = [
  physicalEdge(),
  physicalEdge(),
  reversePhysicalEdge(physicalEdge())
];
const duplicateEquivalentLayout = layoutFor(duplicateEquivalentEdges);
assert.strictEqual(signatureFor(duplicateEquivalentEdges), baseSignature);
assert.strictEqual(duplicateEquivalentLayout.links.length, 1);
assert.strictEqual(
  (renderPhysicalTopologySvg(duplicateEquivalentLayout, duplicateEquivalentLayout.width)
    .match(/topology-link--physical/g) || []).length,
  1
);

// PHYSICAL_ESCAPING: projection-derived names, ports, aggregate labels and
// data-link-id attributes remain inert in the complete Physical renderer.
const maliciousName = '<img src=x onerror=alert(1)>&"\'';
const maliciousPort = '\"/><script>alert(1)</script><>&\'';
const maliciousAggregate = '<Po&"\'>';
const maliciousLayout = layoutFor([physicalEdge({
  from_sysname: maliciousName,
  from_port: maliciousPort,
  from_aggregate_port: maliciousAggregate,
  from_member_ports: [maliciousPort]
})]);
const maliciousSvg = renderPhysicalTopologySvg(maliciousLayout, maliciousLayout.width);
assert.ok(!maliciousSvg.includes("<img"));
assert.ok(!maliciousSvg.includes("<script"));
assert.ok(!maliciousSvg.includes('\"/><script>'));
assert.ok(maliciousSvg.includes("&lt;img src=x onerror=alert(1)&gt;&amp;&quot;&#39;"));
assert.ok(maliciousSvg.includes("&lt;script&gt;alert(1)&lt;/script&gt;"));
assert.ok(maliciousSvg.includes("&lt;Po&amp;&quot;&#39;&gt;"));
assert.ok(!maliciousSvg.includes('data-link-id="physical|'));
assert.ok(!maliciousSvg.includes('role="button" data-link-id='));
assert.ok(!maliciousSvg.includes('aria-selected='));

// Browser wiring: accessible controls, dependency order, cached mode switch, one poll loop.
const indexSource = fs.readFileSync(path.resolve(__dirname, "../bigscreen/index.html"), "utf8");
const appSource = fs.readFileSync(path.resolve(__dirname, "../bigscreen/app.js"), "utf8");
const cssSource = fs.readFileSync(path.resolve(__dirname, "../bigscreen/style.css"), "utf8");
assert.ok(indexSource.includes('<button type="button" id="topologyViewOperations" aria-pressed="true">'));
assert.ok(indexSource.includes('<button type="button" id="topologyViewPhysical" aria-pressed="false">'));
assert.ok(
  indexSource.indexOf("physical-topology.js?v=20260831a") <
  indexSource.indexOf("charts/topology-panel.js?v=20260826a")
);
assert.ok(
  indexSource.indexOf("charts/topology-panel.js?v=20260826a") <
  indexSource.indexOf("app.js?v=20260828f")
);
assert.strictEqual(
  (appSource.match(/window\.setInterval\(refreshTopology, 10000\)/g) || []).length,
  1,
  "Operations and Physical share one polling interval"
);
const modeHandler = appSource.match(/function handleTopologyModeChange\(\) \{[\s\S]*?\n  \}/);
assert.ok(modeHandler);
assert.ok(modeHandler[0].includes("renderTopologySnapshot(lastTopologySnapshot, true)"));
assert.ok(!modeHandler[0].includes("fetchTopologyTargets("), "mode switch never refetches targets");
assert.ok(!modeHandler[0].includes("fetchTopologyEdges("), "mode switch never refetches edges");
assert.ok(!modeHandler[0].includes("prometheusInstant("), "mode switch never refetches status");
assert.ok(appSource.includes('topologyPanel.setMode("operations", false)'));
assert.ok(appSource.includes('if (topologyTimer) refreshTopology()'), "resize keeps the existing refresh path");
assert.ok(cssSource.includes('.topology-canvas[data-topology-view="physical"] .topology-link--physical'));
assert.ok(cssSource.includes('.topology-canvas[data-topology-view="physical"] .topology-link--bundle'));
assert.ok(cssSource.includes('.topology-canvas[data-topology-view="physical"] .topology-link--attachment'));

function extractAppFunction(name) {
  const match = appSource.match(new RegExp(
    `(?:async )?function ${name}\\([^\\n]*\\) \\{[\\s\\S]*?\\n  \\}`
  ));
  assert.ok(match, `${name} remains directly testable without a production export`);
  return match[0];
}

const renderTopologySnapshotSource = extractAppFunction("renderTopologySnapshot");
const handleTopologyModeChangeSource = extractAppFunction("handleTopologyModeChange");
const refreshTopologySource = extractAppFunction("refreshTopology");

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolveValue, rejectValue) => {
    resolve = resolveValue;
    reject = rejectValue;
  });
  return { promise, resolve, reject };
}

function createTopologyRefreshHarness({ targetResults, edgeResults, seenResults }) {
  let mode = "operations";
  let targetCalls = 0;
  let edgeCalls = 0;
  let seenCalls = 0;
  const prepares = [];
  const renders = [];
  const latencyUpdates = [];
  const statuses = [];

  const valueAt = (items, index) => {
    const value = items[Math.min(index, items.length - 1)];
    return typeof value === "function" ? value(index) : value;
  };
  const topologyPanel = {
    isAvailable: () => true,
    getMode: () => mode,
    setMode: (nextMode) => { mode = nextMode; },
    prepare: (targets, edges) => {
      const layout = mode === "physical"
        ? physicalTopologyLayout(projectPhysicalTopology(edges), targets, 900, 560)
        : { nodes: targets, links: edges, width: 900, height: 560 };
      prepares.push({ mode, targets, edges, layout });
      return { layout, width: 900 };
    },
    render: (frame) => renders.push({ mode, frame }),
    updateLatency: (nodes) => latencyUpdates.push({ mode, nodes }),
    updateStatus: (edges) => statuses.push({ mode, edges }),
    showError: (message) => { throw new Error(`unexpected topology error: ${message}`); }
  };
  const context = {
    topologyPanel,
    physicalTopologySignature,
    topologySignature: (layout, width, edges) => `${width}:${layout.nodes.length}:${edges.length}`,
    shouldRender: () => true,
    fetchTopologyTargets: () => Promise.resolve(valueAt(targetResults, targetCalls++)),
    fetchTopologyEdges: () => Promise.resolve(valueAt(edgeResults, edgeCalls++)),
    prometheusInstant: () => Promise.resolve(valueAt(seenResults, seenCalls++)),
    activeInfraPingQuery: () => "active-infra",
    activeSeriesNames: (items) => new Set(items),
    console: { error: () => {} }
  };
  const api = vm.runInNewContext(`
    (function () {
      let topologySeq = 0;
      let lastTopologySnapshot = null;
      let lastDataSuccessAt = 0;
      const renderSignatures = new Map();
      ${renderTopologySnapshotSource}
      ${handleTopologyModeChangeSource}
      ${refreshTopologySource}
      return {
        refreshTopology,
        handleTopologyModeChange,
        setMode: (nextMode) => topologyPanel.setMode(nextMode),
        getMode: () => topologyPanel.getMode(),
        getSnapshot: () => lastTopologySnapshot,
        getSeq: () => topologySeq
      };
    }())
  `, context);
  return {
    ...api,
    prepares,
    renders,
    latencyUpdates,
    statuses,
    calls: () => ({ targets: targetCalls, edges: edgeCalls, seen: seenCalls })
  };
}

async function runControllerRegressions() {
  const sharedTargets = [
    target("10.0.0.1", "Target-A", "infra-core-ping"),
    target("10.0.0.2", "Target-B")
  ];

  // REFRESH_WHILE_PHYSICAL and runtime seenUp filtering use the current mode
  // and never remove an accepted endpoint from the projected Physical model.
  const refreshHarness = createTopologyRefreshHarness({
    targetResults: [sharedTargets],
    edgeResults: [ordinaryEdges],
    seenResults: [["10.0.0.1"]]
  });
  refreshHarness.setMode("physical");
  await refreshHarness.refreshTopology();
  assert.strictEqual(refreshHarness.getMode(), "physical");
  assert.strictEqual(refreshHarness.prepares[0].targets.length, 1, "seenUp removed target B");
  assert.deepStrictEqual(
    refreshHarness.renders[0].frame.layout.nodes.map((node) => node.ip).sort(),
    ["10.0.0.1", "10.0.0.2"],
    "accepted endpoint B survives the app-level seenUp filter"
  );
  assert.strictEqual(refreshHarness.renders[0].frame.layout.links.length, 1);

  const callsBeforeModeSwitch = refreshHarness.calls();
  refreshHarness.setMode("operations");
  refreshHarness.handleTopologyModeChange();
  refreshHarness.setMode("physical");
  refreshHarness.handleTopologyModeChange();
  assert.deepStrictEqual(refreshHarness.calls(), callsBeforeModeSwitch, "cached mode switches never refetch");
  assert.strictEqual(refreshHarness.renders.at(-1).mode, "physical");

  // SWITCH_DURING_INFLIGHT_FETCH: response completion consults the requested
  // current mode instead of restoring the mode active at request start.
  const inflightTargets = deferred();
  const inflightEdges = deferred();
  const inflightSeen = deferred();
  const inflightHarness = createTopologyRefreshHarness({
    targetResults: [inflightTargets.promise],
    edgeResults: [inflightEdges.promise],
    seenResults: [inflightSeen.promise]
  });
  const inflightRefresh = inflightHarness.refreshTopology();
  inflightHarness.setMode("physical");
  inflightTargets.resolve(sharedTargets);
  inflightEdges.resolve(ordinaryEdges);
  inflightSeen.resolve([]);
  await inflightRefresh;
  assert.strictEqual(inflightHarness.getMode(), "physical");
  assert.strictEqual(inflightHarness.renders.at(-1).mode, "physical");
  assert.strictEqual(inflightHarness.renders.at(-1).frame.layout.links.length, 1);

  // PHYSICAL_SEQUENCE_GUARD: the late first response cannot overwrite the
  // newer completed request and no second Physical-specific sequence exists.
  const oldTargets = deferred();
  const newTargets = deferred();
  const oldEdges = deferred();
  const newEdges = deferred();
  const oldSeen = deferred();
  const newSeen = deferred();
  const sequenceHarness = createTopologyRefreshHarness({
    targetResults: [oldTargets.promise, newTargets.promise],
    edgeResults: [oldEdges.promise, newEdges.promise],
    seenResults: [oldSeen.promise, newSeen.promise]
  });
  sequenceHarness.setMode("physical");
  const firstRefresh = sequenceHarness.refreshTopology();
  const secondRefresh = sequenceHarness.refreshTopology();
  const newestEdge = physicalEdge({
    from_port: "Gi1/0/9",
    from_ifindex: 109,
    to_port: "Gi1/0/10",
    to_ifindex: 210
  });
  newTargets.resolve(sharedTargets);
  newEdges.resolve([newestEdge]);
  newSeen.resolve([]);
  await secondRefresh;
  assert.strictEqual(sequenceHarness.getSeq(), 2);
  assert.strictEqual(sequenceHarness.renders.length, 1);
  assert.strictEqual(sequenceHarness.getSnapshot().edges[0].from_port, "Gi1/0/9");
  oldTargets.resolve(sharedTargets);
  oldEdges.resolve(ordinaryEdges);
  oldSeen.resolve([]);
  await firstRefresh;
  assert.strictEqual(sequenceHarness.renders.length, 1, "stale response is ignored");
  assert.strictEqual(sequenceHarness.getSnapshot().edges[0].from_port, "Gi1/0/9");

  // PHYSICAL_RESIZE executes the real registered resize handler. The existing
  // architecture deliberately refreshes once after debounce and retains mode.
  const resizeRegistration = appSource.match(
    /window\.addEventListener\("resize", \(\) => \{[\s\S]*?\n  \}\);/
  );
  assert.ok(resizeRegistration);
  let resizeHandler = null;
  let resizeDebounce = null;
  let resizeMode = "physical";
  let resizeRefreshes = 0;
  const resizeContext = {
    window: {
      addEventListener: (type, handler) => {
        if (type === "resize") resizeHandler = handler;
      },
      clearTimeout: () => {},
      setTimeout: (handler, delay) => {
        assert.strictEqual(delay, 200);
        resizeDebounce = handler;
        return 1;
      }
    },
    renderSignatures: new Map([["topology:physical", "old"]]),
    infraController: { refreshForResize: () => {} },
    tournamentPanel: { hasScheduledRefresh: () => false, refresh: () => {} },
    activePage: () => null,
    topologyTimer: 1,
    refreshTopology: () => {
      resizeRefreshes += 1;
      assert.strictEqual(resizeMode, "physical");
    }
  };
  vm.runInNewContext(`
    (function () {
      let resizeRepaintTimer = null;
      ${resizeRegistration[0]}
    }())
  `, resizeContext);
  resizeHandler();
  assert.strictEqual(resizeContext.renderSignatures.size, 0);
  resizeDebounce();
  assert.strictEqual(resizeRefreshes, 1);
  assert.strictEqual(resizeMode, "physical");
}

runControllerRegressions()
  .then(() => console.log("bigscreen Physical View presentation tests passed"))
  .catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
