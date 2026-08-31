const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

global.window = {
  BIGSCREEN_CONFIG: {
    ispNames: 'ISP-A',
    ispIps: 'ISP-A:203.0.113.10',
    ispAutoDiscovery: 'false',
    serverTargets: 'Server-A:10.0.1.10'
  },
  BIGSCREEN_QUERIES: {},
  BIGSCREEN_PAGES: []
};

const {
  buildTopologyLayers,
  topologyLayout,
  renderTopologySvg
} = require('../bigscreen/topology.js');

const root = path.resolve(__dirname, '..');
const indexSource = fs.readFileSync(path.join(root, 'bigscreen/index.html'), 'utf8');
const appSource = fs.readFileSync(path.join(root, 'bigscreen/app.js'), 'utf8');
const panelSource = fs.readFileSync(path.join(root, 'bigscreen/charts/topology-panel.js'), 'utf8');
const cssSource = fs.readFileSync(path.join(root, 'bigscreen/style.css'), 'utf8');

// SINGLE_TOPOLOGY_UI: the correction removes only the 3B mode selector and
// leaves the ordinary Topology panel, legend and interaction help intact.
const topologySection = indexSource.match(
  /<section class="topology-panel"[\s\S]*?<\/section>/
)[0];
assert.ok(topologySection.includes('id="topologyCanvas"'));
assert.ok(topologySection.includes('id="topologyUpdated"'));
assert.ok(topologySection.includes('在线'));
assert.strictEqual((topologySection.match(/<button\b/g) || []).length, 0);
assert.ok(!topologySection.includes('>Operations<'));
assert.ok(!topologySection.includes('>Physical<'));
const topologyScripts = Array.from(indexSource.matchAll(/<script src="([^"]+)"/g), (match) => match[1]);
assert.ok(topologyScripts.every((src) => !/physical/i.test(src)), 'no retired projection script is loaded');
const prepareBody = panelSource.match(/function prepare\(targets, edges\) \{[\s\S]*?\n    \}/)[0];
const renderBody = panelSource.match(/function render\(frame\) \{[\s\S]*?\n    \}/)[0];
assert.ok(prepareBody.includes('buildTopologyLayers(targets)'));
assert.ok(prepareBody.includes('topologyLayout(layers, width, height, edges)'));
assert.ok(renderBody.includes('renderTopologySvg(frame.layout, frame.width)'));
assert.ok(!cssSource.includes('topology-view-switch'));
assert.ok(!cssSource.includes('data-topology-view="physical"'));

// There is still exactly one refresh loop and no metadata-specific fetch path.
assert.strictEqual(
  (appSource.match(/setInterval\(refreshTopology, 10000\)/g) || []).length,
  1,
  'Topology owns one 10-second polling interval'
);
const refreshBody = appSource.match(/async function refreshTopology\(\) \{[\s\S]*?\n  \}/)[0];
assert.strictEqual((refreshBody.match(/fetchTopologyTargets\(\)/g) || []).length, 1);
assert.strictEqual((refreshBody.match(/fetchTopologyEdges\(\)/g) || []).length, 1);

// The existing debounced resize path remains a single Operations refresh.
const resizeRegistration = appSource.match(
  /window\.addEventListener\("resize", \(\) => \{[\s\S]*?\n  \}\);/
);
assert.ok(resizeRegistration);
let resizeHandler = null;
let resizeDebounce = null;
let resizeRefreshes = 0;
const resizeContext = {
  window: {
    addEventListener: (type, handler) => {
      if (type === 'resize') resizeHandler = handler;
    },
    clearTimeout: () => {},
    setTimeout: (handler, delay) => {
      assert.strictEqual(delay, 200);
      resizeDebounce = handler;
      return 1;
    }
  },
  renderSignatures: new Map([['topology', 'old']]),
  infraController: { refreshForResize: () => {} },
  tournamentPanel: { hasScheduledRefresh: () => false, refresh: () => {} },
  activePage: () => null,
  topologyTimer: 1,
  refreshTopology: () => { resizeRefreshes += 1; }
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
assert.strictEqual(resizeRefreshes, 1, 'resize schedules exactly one Topology refresh');

const target = (job, displayName, targetIp) => ({
  job,
  displayName,
  instance: displayName,
  targetIp,
  success: true,
  latency: 0.002
});

const targets = [
  target('infra-isp-ping', 'ISP-A', '203.0.113.10'),
  target('infra-fw-unit-snmp', 'FW-A', '10.0.0.254'),
  target('infra-fw-unit-snmp', 'FW-B', '10.0.0.253'),
  target('infra-core-ping', 'Core-A', '10.0.0.1'),
  target('infra-dist-ping', 'Access-A', '10.0.0.2'),
  target('infra-dist-ping', 'Access-B', '10.0.0.3'),
  target('infra-srv-ping', 'Server-A', '10.0.1.10')
];
const layers = buildTopologyLayers(targets);

const baseEdges = [
  {
    from_ip: '10.0.0.1', from_port: 'Te1/0/2',
    from_member_ports: ['Te1/0/2', 'Te2/0/2'],
    to_ip: '10.0.0.2', to_port: 'Te1/0/1',
    to_member_ports: ['Te1/0/1', 'Te2/0/1'],
    stale: false
  },
  {
    from_ip: '10.0.0.2', from_port: 'Gi1/0/20',
    to_ip: '10.0.1.10', to_port: '',
    source: 'fdb', stale: false
  }
];

const metadataOnlyEdges = baseEdges.map((edge, index) => ({
  ...edge,
  edge_type: index === 1 ? 'server_attachment' : 'physical',
  protocols: index === 1 ? undefined : ['cdp', 'lldp']
}));

const baseLayout = topologyLayout(layers, 1200, 680, baseEdges);
const metadataLayout = topologyLayout(layers, 1200, 680, metadataOnlyEdges);
const nodeGeometry = (layout) => layout.nodes.map((node) => ({
  kind: node.kind,
  ip: node.ip,
  x: node.x,
  y: node.y,
  w: node.w,
  h: node.h,
  unlocated: node.unlocated === true
}));
const connectivity = (layout) => layout.links.map((link) => ({
  pair: [link.from.ip, link.to.ip].sort(),
  fallback: link.fallback === true,
  logical: link.logical === true,
  severity: link.severity
}));

// PHYSICAL_METADATA_DOES_NOT_CHANGE_OPERATIONS_LAYOUT.
assert.deepStrictEqual(nodeGeometry(metadataLayout), nodeGeometry(baseLayout));
assert.deepStrictEqual(connectivity(metadataLayout), connectivity(baseLayout));
assert.deepStrictEqual(
  metadataLayout.nodes.filter((node) => node.kind === 'isp').map((node) => node.y),
  [22]
);
const firewallY = new Set(metadataLayout.nodes.filter((node) => node.kind === 'firewall').map((node) => node.y));
const coreY = new Set(metadataLayout.nodes.filter((node) => node.kind === 'core').map((node) => node.y));
const accessY = new Set(metadataLayout.nodes.filter((node) => node.kind === 'dist').map((node) => node.y));
assert.strictEqual(firewallY.size, 1, 'firewalls stay in one row');
assert.strictEqual(coreY.size, 1, 'core stays in one row');
assert.ok(Math.min(...firewallY) < Math.min(...coreY), 'firewall row remains above core');
assert.ok(Math.min(...coreY) < Math.min(...accessY), 'core remains above the access row');
assert.ok(metadataLayout.coreBus, 'the Operations core/access bus remains present');
assert.strictEqual(metadataLayout.haBonds.length, 1, 'the synthetic firewall HA relationship remains');
assert.ok(
  metadataLayout.links.some((link) => link.fallback && link.from.kind === 'isp' && link.to.kind === 'firewall'),
  'synthetic ISP-to-firewall relationships remain'
);
assert.ok(
  metadataLayout.links.some((link) => link.fallback && link.from.kind === 'firewall' && link.to.kind === 'core'),
  'synthetic firewall-to-core relationships remain'
);

const server = metadataLayout.nodes.find((node) => node.kind === 'server');
const accessA = metadataLayout.nodes.find((node) => node.ip === '10.0.0.2');
assert.ok(server.y > accessA.y, 'the FDB server remains below its real access switch');
assert.strictEqual(server.unlocated, false);
const serverLinks = metadataLayout.links.filter((link) => (
  link.from.kind === 'server' || link.to.kind === 'server'
));
assert.strictEqual(serverLinks.length, 1, 'server_attachment does not create a second path');
assert.strictEqual(serverLinks[0].serverAttachment, true);

const logicalLinkFor = (layout, leftIp, rightIp) => layout.links.find((link) => (
  [link.from.ip, link.to.ip].sort().join('|') === [leftIp, rightIp].sort().join('|') &&
  link.logical
));

// Ordinary physical links retain their endpoint port labels.
const normalLink = logicalLinkFor(topologyLayout(layers, 1200, 680, [{
  from_ip: '10.0.0.1', from_port: 'Te1/0/7',
  to_ip: '10.0.0.3', to_port: 'Gi27',
  stale: false
}]), '10.0.0.1', '10.0.0.3');
assert.deepStrictEqual(normalLink.labelLines, ['Te1/0/7', 'Gi27']);

// Aggregate metadata keeps the device pair as one visual relationship, while
// each endpoint independently prefers its confirmed physical member set.
const lagRows = [
  {
    edge_type: 'physical', protocols: ['lldp'], stale: true,
    from_ip: '10.0.0.1', from_port: 'Te1/0/1', from_aggregate_port: 'Po11',
    from_member_ports: ['Te1/0/1', 'Te2/0/1'],
    to_ip: '10.0.0.2', to_port: 'Gi1/0/47', to_aggregate_port: 'Po1',
    to_member_ports: ['Gi1/0/47', 'Gi1/0/48']
  },
  {
    edge_type: 'physical', protocols: ['cdp'], stale: false,
    from_ip: '10.0.0.1', from_port: 'Te2/0/1', from_aggregate_port: 'Po11',
    from_member_ports: ['Te1/0/1', 'Te2/0/1'],
    to_ip: '10.0.0.2', to_port: 'Gi1/0/48', to_aggregate_port: 'Po1',
    to_member_ports: ['Gi1/0/47', 'Gi1/0/48']
  }
];
const lagLayout = topologyLayout(layers, 1200, 680, lagRows);
const lagLinks = lagLayout.links.filter((link) => (
  [link.from.ip, link.to.ip].sort().join('|') === '10.0.0.1|10.0.0.2'
));
assert.strictEqual(lagLinks.length, 1, 'LAG rows stay one logical Operations link');
assert.strictEqual(lagLinks[0].aggregated, true);
assert.deepStrictEqual(lagLinks[0].labelLines, [
  'Te1/0/1, Te2/0/1',
  'Gi1/0/47, Gi1/0/48'
]);
assert.strictEqual(lagLinks[0].severity, 'warn', 'existing partial-stale warning behavior remains');
const lagSvg = renderTopologySvg(lagLayout, 1200);
assert.strictEqual((lagSvg.match(/link-aggregated/g) || []).length, 1);
for (const memberSet of ['Te1/0/1, Te2/0/1', 'Gi1/0/47, Gi1/0/48']) {
  assert.ok(lagSvg.includes(`>${memberSet}<`), `member set ${memberSet} is the canvas label`);
}
assert.ok(!lagSvg.includes('>Po11<'));
assert.ok(!lagSvg.includes('>Po1<'));
assert.ok(!lagSvg.includes('memberPairs'));
assert.ok(!lagSvg.includes('cablePairs'));
assert.ok(!lagSvg.includes('CDP'));
assert.ok(!lagSvg.includes('LLDP'));

// Aggregate interface names remain a last-resort label when neither endpoint
// has a trustworthy physical member or ordinary physical port.
const aggregateOnlyLink = logicalLinkFor(topologyLayout(layers, 1200, 680, [{
  from_ip: '10.0.0.1', from_port: null, from_aggregate_port: 'Po11',
  to_ip: '10.0.0.2', to_port: null, to_aggregate_port: 'Po1',
  stale: false
}]), '10.0.0.1', '10.0.0.2');
assert.deepStrictEqual(aggregateOnlyLink.labelLines, ['Po11', 'Po1']);
assert.strictEqual(aggregateOnlyLink.aggregated, true);

// Missing physical evidence on one endpoint cannot downgrade confirmed
// members on the other endpoint to an aggregate label.
const asymmetricLink = logicalLinkFor(topologyLayout(layers, 1200, 680, [{
  from_ip: '10.0.0.1', from_port: 'Te1/0/1', from_aggregate_port: 'Po11',
  from_member_ports: ['Te1/0/1', 'Te2/0/1'],
  to_ip: '10.0.0.2', to_port: null, to_aggregate_port: 'Po1',
  stale: false
}]), '10.0.0.1', '10.0.0.2');
assert.deepStrictEqual(asymmetricLink.labelLines, ['Te1/0/1, Te2/0/1', 'Po1']);

// Aggregate-like names accidentally present in a member list are normalized
// but never treated as physical member labels.
const filteredMemberLink = logicalLinkFor(topologyLayout(layers, 1200, 680, [{
  from_ip: '10.0.0.1', from_port: 'Po11', from_aggregate_port: 'Po11',
  from_member_ports: ['Po11', 'Te1/0/1', 'Te2/0/1'],
  to_ip: '10.0.0.2', to_port: 'Po1', to_aggregate_port: 'Po1',
  to_member_ports: ['Po1', 'Gi1/0/47', 'Gi1/0/48'],
  stale: false
}]), '10.0.0.1', '10.0.0.2');
assert.deepStrictEqual(filteredMemberLink.labelLines, [
  'Te1/0/1, Te2/0/1',
  'Gi1/0/47, Gi1/0/48'
]);

// The original screenshot-style LAG remains available without explicit
// aggregate metadata and still renders each endpoint's confirmed member set.
const screenshotLagLink = logicalLinkFor(baseLayout, '10.0.0.1', '10.0.0.2');
assert.deepStrictEqual(screenshotLagLink.labelLines, [
  'Te1/0/2, Te2/0/2',
  'Te1/0/1, Te2/0/1'
]);

const enrichedSvg = renderTopologySvg(metadataLayout, 1200);
assert.strictEqual((enrichedSvg.match(/link-attachment/g) || []).length, 1);
assert.strictEqual((enrichedSvg.match(/<svg class="topology-svg"/g) || []).length, 1);

console.log('bigscreen single Topology integration correction tests passed');
