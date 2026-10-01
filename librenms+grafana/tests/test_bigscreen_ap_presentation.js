const assert = require('assert');
const { appendApLeaves } = require('../bigscreen/ap-topology.js');
const { renderTopologySvg } = require('../bigscreen/topology.js');
const { createWirelessPanel } = require('../bigscreen/wireless/wireless-panel.js');
const { createTopologyPanel } = require('../bigscreen/charts/topology-panel.js');

const wired = { width: 1100, layout: { height: 680, nodes: [
  { kind: 'dist', ip: '10.0.0.11', x: 180, y: 100, w: 144, h: 58 },
  { kind: 'dist', ip: '10.0.0.12', x: 600, y: 100, w: 144, h: 58 },
  { kind: 'dist', ip: '10.0.0.13', x: 180, y: 290, w: 144, h: 58 },
  { kind: 'server', ip: '10.0.0.14', x: 420, y: 300, w: 144, h: 58 },
  { kind: 'server', ip: '10.0.0.15', x: 750, y: 360, w: 144, h: 58 }
], links: [] } };
const aps = Array.from({ length: 8 }, (_, i) => ({ ip: `10.1.0.${i + 1}`, mac: `02000000000${i + 1}`,
  name: i ? `AP-${i}` : '很长的 AP 名称 '.repeat(8), model: 'UAL6', clients: 4, online: true, latency: 0.0012 }));
const artifact = { source: 'librenms-fdb+snmp-exact', candidate_source: 'librenms-fdb', generated_at: 10000,
  max_age_seconds: 7200, attachments: aps.map((ap, i) => ({ ap_ip: ap.ip, ap_mac: ap.mac,
    switch_ip: i % 2 ? '10.0.0.12' : '10.0.0.11', switch_ifindex: i + 1,
    switch_port: `Gi6/0/${i + 1}`, vlan: 200, librenms_vlan_id: 142, evidence_age_seconds: 1 })) };
const data = { aps, artifact };
const snapshot = JSON.stringify(wired);
const frame = appendApLeaves(wired, data, 10000);
assert.deepStrictEqual(frame.apCount, { located: 8, total: 8 });
assert.strictEqual(JSON.stringify(wired), snapshot);
assert.strictEqual(frame.width, wired.width, 'APs never re-fit the wired width');
assert.strictEqual(frame.layout.wiredHeight, wired.layout.height);
assert.deepStrictEqual(frame.layout.nodes.slice(0, wired.layout.nodes.length), wired.layout.nodes);
const apNodes = frame.layout.nodes.filter((n) => n.kind === 'ap');
assert.deepStrictEqual(apNodes.map((n) => n.parentIp), Array(4).fill('10.0.0.11').concat(Array(4).fill('10.0.0.12')), 'groups do not interleave');
assert.deepStrictEqual(appendApLeaves(wired, { aps: [...aps].reverse(), artifact: { ...artifact, attachments: [...artifact.attachments].reverse() } }, 10000), frame, 'geometry independent of input order');
const bounds = (n) => n.kind === 'ap' ? { x: n.x - 46, y: n.y - 20, w: 144, h: 130 } : n;
const overlaps = (a, b) => a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;
frame.layout.nodes.forEach((n, i) => frame.layout.nodes.slice(i + 1).forEach((other) => {
  if (n.kind === 'ap' || other.kind === 'ap') assert(!overlaps(bounds(n), bounds(other)), 'body, name, metadata and port-label bounds do not overlap');
}));
function segmentHits(a, b, r) {
  assert(a[0] === b[0] || a[1] === b[1], 'all AP routes are orthogonal');
  return a[0] === b[0] ? a[0] > r.x && a[0] < r.x + r.w && Math.max(a[1], b[1]) > r.y && Math.min(a[1], b[1]) < r.y + r.h
    : a[1] > r.y && a[1] < r.y + r.h && Math.max(a[0], b[0]) > r.x && Math.min(a[0], b[0]) < r.x + r.w;
}
frame.layout.apBuses.forEach((bus) => bus.points.slice(1).forEach((point, i) => {
  frame.layout.nodes.forEach((n) => assert(!segmentHits(bus.points[i], point, bounds(n)), 'bus never crosses a node or label'));
}));
frame.layout.links.forEach((link) => {
  const x = link.to.x + 26;
  frame.layout.nodes.filter((n) => n !== link.to && n !== link.from).forEach((n) => assert(!segmentHits([x, link.apBusY], [x, link.to.y], bounds(n))));
  assert(!link.label, 'no duplicate midpoint label');
});
assert(frame.layout.apBuses.length >= 2, 'one parent with multiple APs uses shared local branch buses');
const svg = renderTopologySvg(frame.layout, frame.width);
assert(svg.includes('<circle cx="26" cy="26" r="26" />'));
assert(apNodes.every((n) => n.w === 52 && n.w < wired.layout.nodes[0].w));
assert(!svg.includes('1.2 ms') && !svg.includes('VLAN') && !svg.includes('UAL6') && !svg.includes('客户端'));
apNodes.forEach((n) => {
  const card = renderTopologySvg({ height: 680, nodes: [n], links: [] }, 1100);
  assert.strictEqual((card.match(/<text /g) || []).length, 2, 'only AP port and name text');
  assert.strictEqual(n.latency, 0.0012); assert.strictEqual(n.vlan, 200);
  assert.strictEqual(n.model, 'UAL6'); assert.strictEqual(n.clients, 4);
});
assert(!svg.includes('142'), 'internal VLAN FK is not rendered');
assert(svg.includes('textLength="144"'), 'long names stay in their reserved label width');
assert(svg.includes('class="topology-link-label" x="26" y="-8"'), 'shared wired label renderer moves with AP above its circle');
assert(!svg.includes('topology-ap-port'), 'no separate AP label typography');
frame.layout.apBuses.forEach((bus) => {
  const children = frame.layout.links.filter((link) => link.apBusY === bus.y).map((link) => link.to);
  assert.deepStrictEqual(bus.childXs, children.map((n) => n.x + n.w / 2), 'bus extent contains only participating child anchors');
  assert.deepStrictEqual(bus.childRoutes.map((r) => r.apIp), children.map((n) => n.ip));
  const cuts = [...new Set([...bus.childXs, bus.junctionX])].sort((a, b) => a - b);
  const segments = [...bus.points.slice(1).map((point, i) => [bus.points[i], point]),
    ...cuts.slice(1).map((x, i) => [[cuts[i], bus.y], [x, bus.y]])];
  const covers = (a, b, c, d) => a[0] === b[0] && c[0] === d[0] && a[0] === c[0]
    ? Math.min(c[1], d[1]) <= Math.min(a[1], b[1]) && Math.max(c[1], d[1]) >= Math.max(a[1], b[1])
    : a[1] === b[1] && c[1] === d[1] && a[1] === c[1]
      && Math.min(c[0], d[0]) <= Math.min(a[0], b[0]) && Math.max(c[0], d[0]) >= Math.max(a[0], b[0]);
  segments.forEach(([a, b]) => {
    assert.notDeepStrictEqual(a, b, 'no empty terminal segment');
    assert(bus.childRoutes.some((r) => r.points.slice(1).some((point, i) => covers(a, b, r.points[i], point))), 'every rendered segment belongs to a validated child route');
    frame.layout.nodes.forEach((n) => assert(!segmentHits(a, b, bounds(n)), 'trunk and exact participating bus span avoid all card/label bounds'));
  });
  bus.childRoutes.forEach((r, i) => assert.deepStrictEqual(r.points[r.points.length - 1], [children[i].x + 26, children[i].y], 'route terminates at its own real AP'));
});
assert(frame.layout.apBuses.some((bus) => bus.childXs.length === 1), 'fixture includes a partial last row with one AP and no horizontal bus');
for (const kind of ['core', 'dist', 'device']) {
  const minimalNode = { ...wired.layout.nodes[0], kind, name: 'Minimal switch', latency: 0.002, success: true, level: 'good' };
  const card = renderTopologySvg({ height: 680, nodes: [minimalNode], links: [] }, 1100);
  assert(card.includes('Minimal switch') && card.includes('topology-node-icon'));
  for (const cls of ['topology-node-kind', 'topology-node-ip', 'topology-node-latency']) assert(!card.includes(`class="${cls}"`), 'wired switch/device card has no secondary summary');
  assert.strictEqual(minimalNode.latency, 0.002, 'Inspector data is retained');
}

// Dense three-level chamber: adjacent access/server cards close both sides
// and the downward lane. The old router escaped upward through its parent.
const denseParent = { kind: 'dist', ip: '10.0.0.11', x: 300, y: 150, w: 144, h: 58 };
const denseWired = { width: 800, layout: { height: 680, links: [], nodes: [denseParent,
  ...[170, 240, 310].flatMap((y, i) => [
    { kind: 'dist', ip: `10.0.1.${i + 1}`, x: 151, y, w: 144, h: 58 },
    { kind: 'server', ip: `10.0.2.${i + 1}`, x: 448, y, w: 144, h: 58 }
  ]),
  { kind: 'dist', ip: '10.0.3.1', x: 300, y: 250, w: 144, h: 58 },
  { kind: 'server', ip: '10.0.3.2', x: 300, y: 390, w: 144, h: 58 }
] } };
const denseSnapshot = JSON.stringify(denseWired);
const denseData = { aps: aps.slice(0, 2), artifact: { ...artifact, attachments: artifact.attachments.slice(0, 2).map((row) => ({ ...row, switch_ip: denseParent.ip })) } };
const denseFrame = appendApLeaves(denseWired, denseData, 10000);
assert.strictEqual(denseFrame.layout.apBuses.length, 0, 'closed downward chamber cannot escape upward through parent');
assert.strictEqual(denseFrame.layout.links.length, 0, 'no unsafe partial AP route');
assert.deepStrictEqual(denseFrame.apCount, { located: 0, total: 2 }, 'no safe path fails closed');
assert.strictEqual(JSON.stringify(denseWired), denseSnapshot);
const blockedEgress = { ...denseWired, layout: { ...denseWired.layout,
  nodes: denseWired.layout.nodes.map((n) => n.ip === '10.0.3.1' ? { ...n, y: 220 } : n) } };
const blockedFrame = appendApLeaves(blockedEgress, denseData, 10000);
assert.deepStrictEqual(blockedFrame.apCount, { located: 0, total: 2 }, 'blocked initial downward egress fails closed');
assert.strictEqual(blockedFrame.layout.apBuses.length, 0);
assert.strictEqual(blockedFrame.layout.links.length, 0);

// Open one side while retaining the downward obstacle and other levels.
// Routing must now succeed outward and retain every remaining obstacle.
const openWired = { ...denseWired, layout: { ...denseWired.layout,
  nodes: denseWired.layout.nodes.filter((n) => !n.ip.startsWith('10.0.1.')) } };
const openFrame = appendApLeaves(openWired, denseData, 10000);
assert.deepStrictEqual(openFrame.apCount, { located: 2, total: 2 });
openFrame.layout.apBuses.forEach((bus) => {
  const [source, egress] = bus.points;
  assert.deepStrictEqual(source, [denseParent.x + denseParent.w / 2, denseParent.y + denseParent.h]);
  assert.strictEqual(egress[0], source[0], 'first egress can only move down');
  assert(egress[1] > source[1]);
  assert(bus.points.every((point) => point[1] >= source[1]), 'no waypoint returns above parent bottom');
  bus.points.slice(1).forEach((point, i) => openFrame.layout.nodes.forEach((n) => {
    assert(!segmentHits(bus.points[i], point, bounds(n)), 'dense route avoids parent, unrelated cards and AP label/body reservations');
  }));
});

// Exercise the real controller and SVG renderer. Project the actual wired
// viewBox into the fixed viewport, including meet letterboxing and pan/zoom.
const elements = new Map();
let currentSvg, holder;
const element = (id) => {
  if (!elements.has(id)) elements.set(id, { hidden: false, dataset: {}, style: {}, clientWidth: 800, clientHeight: 500,
    setAttribute() {}, addEventListener() {}, querySelectorAll: () => [],
    querySelector: (selector) => selector === '.topology-svg' ? currentSvg : selector === '.topology-additive-frame' ? holder : { onclick: null } });
  return elements.get(id);
};
const canvas = element('topologyCanvas');
Object.defineProperty(canvas, 'innerHTML', { set(value) {
  const match = value.match(/data-base-width="([^"]+)" data-base-height="([^"]+)"/);
  currentSvg = { dataset: { baseWidth: match[1], baseHeight: match[2] }, style: {}, setAttribute(k, v) { this[k] = v; } };
  holder = { style: {} };
} });
const panel = createTopologyPanel({ document: { getElementById: element, querySelectorAll: () => [] }, location: {},
  appendApLeaves: (base, values) => appendApLeaves(base, values, 10000), buildTopologyLayers: () => ({ isps: [], cores: [], firewalls: [], dists: [], servers: [] }),
  topologyLayout: () => wired.layout, renderTopologySvg, escapeHtml: String });
const projection = () => {
  const [vx, vy, vw, vh] = currentSvg.viewBox.split(' ').map(Number);
  const height = parseFloat(currentSvg.style.height);
  const scale = Math.min(canvas.clientWidth / vw, height / vh);
  return wired.layout.nodes.map((n) => [scale, (n.x - vx) * scale + (canvas.clientWidth - vw * scale) / 2, (n.y - vy) * scale + (height - vh * scale) / 2]);
};
panel.render(wired);
const before = projection(), initialHeight = holder.style.height;
element('topologyApToggle').onclick();
panel.render(frame);
assert.deepStrictEqual(projection(), before, 'projected wired positions and scale identical OFF->ON');
assert(parseFloat(holder.style.height) >= parseFloat(initialHeight), 'only physical additive height can grow');
element('topologyApToggle').onclick();
panel.render(wired);
assert.deepStrictEqual(projection(), before, 'projected wired positions and scale identical ON->OFF');
assert.strictEqual(holder.style.height, initialHeight);

async function run() {
  const info = [{ metric: { ...aps[0], type: 'uap', state: 'online' }, value: 1 }];
  const sample = (ip, value) => ({ metric: { target_ip: ip, name: aps[0].name }, value });
  let success = [sample(aps[0].ip, 1)], rtt = [sample(aps[0].ip, 0.0012)], infos = info;
  const wireless = createWirelessPanel({ prometheusQuery: async (query) => {
    if (query.startsWith('unpoller_device_info')) return infos;
    if (query.startsWith('probe_success')) return success;
    if (query.startsWith('probe_icmp')) return rtt;
    return [];
  } });
  assert.strictEqual((await wireless.fetchApStatus({ topology: true }))[0].latency, 0.0012);
  for (const samples of [[], [sample('10.9.9.9', 0.1)], [sample(aps[0].ip, 0.1), sample(aps[0].ip, 0.2)], [{ metric: { name: aps[0].name }, value: 0.1 }], [sample(aps[0].ip, -1)]]) {
    rtt = samples; assert.strictEqual((await wireless.fetchApStatus({ topology: true }))[0].latency, null);
  }
  rtt = [sample(aps[0].ip, 0)];
  assert.strictEqual((await wireless.fetchApStatus({ topology: true }))[0].latency, 0, 'valid zero RTT retained');
  success = [sample(aps[0].ip, 0)];
  const failedPing = (await wireless.fetchApStatus({ topology: true }))[0];
  assert.strictEqual(failedPing.online, true, 'ping failure never replaces controller status');
  assert.strictEqual(failedPing.latency, null);
  success = [sample(aps[0].ip, 1), sample(aps[0].ip, 1)];
  assert.strictEqual((await wireless.fetchApStatus({ topology: true }))[0].latency, null);
  success = [sample(aps[0].ip, 1)]; infos = [info[0], { metric: { ...info[0].metric, name: 'same IP' }, value: 1 }];
  assert((await wireless.fetchApStatus({ topology: true })).every((n) => n.latency === null), 'duplicate management IP fails closed');
  console.log('bigscreen AP presentation and projected geometry: PASS');
}
run().catch((error) => { console.error(error); process.exitCode = 1; });
