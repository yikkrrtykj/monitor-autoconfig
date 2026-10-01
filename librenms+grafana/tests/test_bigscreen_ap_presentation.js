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
assert(svg.includes('1.2 ms · VLAN 200') && svg.includes('UAL6 · 4 客户端'));
assert(!svg.includes('142'), 'internal VLAN FK is not rendered');
assert(svg.includes('textLength="144"'), 'long names stay in their reserved label width');
assert(svg.includes('class="topology-ap-port" x="26" y="-8"'), 'port label moves with AP above its circle');

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
