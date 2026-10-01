const assert = require('assert');
const { appendApLeaves } = require('../bigscreen/ap-topology.js');
const { createTopologyPanel } = require('../bigscreen/charts/topology-panel.js');
const { createWirelessPanel } = require('../bigscreen/wireless/wireless-panel.js');
const wired = { width: 640, layout: { height: 420, nodes: [{ kind: 'dist', ip: '10.0.0.11', x: 40, y: 80, w: 144, h: 58 }], links: [] } };
const ap = { ip: '10.1.0.1', mac: '02:00:00:00:00:01', name: 'AP <one>', model: 'U6-Pro', online: true, clients: 0 };
const attachment = { ap_ip: ap.ip, ap_mac: ap.mac, switch_ip: '10.0.0.11', switch_ifindex: 1, switch_port: 'Gi1/0/1', evidence_age_seconds: 6000 };
const artifact = { source: 'librenms-fdb+snmp-exact', candidate_source: 'librenms-fdb', generated_at: 10000, max_age_seconds: 7200, attachments: [attachment] };
const overlay = (aps = [ap], evidence = artifact, base = wired, now = 10000) => appendApLeaves(base, { aps, artifact: evidence }, now);
const snapshot = JSON.stringify(wired);
let frame = overlay();
assert.deepStrictEqual(frame.apCount, { located: 1, total: 1 });
assert(frame.layout.nodes[1].y > wired.layout.nodes[0].y);
assert.strictEqual(frame.layout.nodes[1].clients, 0, 'valid zero clients retained');
assert.strictEqual(frame.layout.links[0].from, wired.layout.nodes[0]);
assert.strictEqual(JSON.stringify(wired), snapshot, 'AP overlay never mutates wired layout/cache');
const svg = require('../bigscreen/topology.js').renderTopologySvg(frame.layout, frame.width);
assert(svg.includes('AP &lt;one&gt;'));
assert(!svg.includes('U6-Pro') && !svg.includes('客户端') && !svg.includes('在线'));
assert.strictEqual(frame.layout.nodes[1].model, 'U6-Pro', 'model remains in data');
assert(!svg.includes('NaN'));

for (const evidence of [null, { ...artifact, attachments: [] }, { ...artifact, source: 'cache' }, { ...artifact, source: 'librenms-fdb' }, { ...artifact, candidate_source: 'cache' },
  { ...artifact, attachments: [attachment, attachment] },
  { ...artifact, attachments: [{ ...attachment, ap_mac: '02:00:00:00:00:02' }] },
  { ...artifact, attachments: [{ ...attachment, evidence_age_seconds: null }] }]) assert.strictEqual(overlay([ap], evidence).layout.links.length, 0);
assert.strictEqual(overlay([ap], artifact, wired, 11201).layout.links.length, 0, 'age advances after collector snapshot');
for (const kind of ['core', 'firewall', 'ap']) assert.strictEqual(overlay([ap], artifact, { ...wired, layout: { ...wired.layout, nodes: [{ ...wired.layout.nodes[0], kind }] } }).layout.links.length, 0);
for (const aps of [[ap, ap], [{ ...ap, mac: '' }], [{ ...ap, ip: '' }], []]) assert.strictEqual(overlay(aps).layout.links.length, 0);
const aps17 = Array.from({ length: 17 }, (_, i) => ({ ...ap, ip: `10.1.0.${i + 1}`, mac: `0200000000${(i + 1).toString(16).padStart(2, '0')}` }));
const rows7 = aps17.slice(0, 7).map((item, i) => ({ ...attachment, ap_ip: item.ip, ap_mac: item.mac, switch_ifindex: i + 1 }));
assert.deepStrictEqual(overlay(aps17, { ...artifact, attachments: rows7 }).apCount, { located: 7, total: 17 });

const elements = new Map();
const element = (id) => {
  if (!elements.has(id)) elements.set(id, { hidden: false, dataset: {}, clientWidth: 640, clientHeight: 420,
    setAttribute() {}, querySelector: (selector) => selector === ".topology-svg" ? null : ({ onclick: null }), addEventListener() {}, querySelectorAll: () => [] });
  return elements.get(id);
};
const document = { getElementById: element, querySelectorAll: () => [] };
const storage = new Map();
let calls = 0;
const dependencies = { document, location: {}, sessionStorage: { getItem: (key) => storage.get(key), setItem: (key, value) => storage.set(key, value) },
  onApVisibilityChange: () => calls++, appendApLeaves: (base, data) => appendApLeaves(base, data, 10000),
  buildTopologyLayers: () => ({ isps: [], firewalls: [], cores: [], dists: [{}], servers: [] }),
  topologyLayout: () => wired.layout, renderTopologySvg: (layout) => JSON.stringify(layout), escapeHtml: String };
let panel = createTopologyPanel(dependencies);
assert.strictEqual(panel.isApVisible(), false);
assert.strictEqual(panel.prepare([], [], { aps: [ap], artifact }).layout.nodes.length, 1);
element('topologyApToggle').onclick();
assert.strictEqual(panel.isApVisible(), true);
assert.strictEqual(panel.prepare([], [], { aps: [ap], artifact }).layout.nodes.length, 2);
assert.strictEqual(element('topologyApCount').textContent, 'AP 1/1 已定位');
panel = createTopologyPanel(dependencies);
assert.strictEqual(panel.isApVisible(), true, 'session ON survives controller recreation');
element('topologyApToggle').onclick();
assert.strictEqual(panel.isApVisible(), false);
assert.strictEqual(panel.prepare([], [], { aps: [ap], artifact }).layout.nodes.length, 1, 'old AP response cannot restore OFF');
panel.clearDetail(); panel.resetView();
for (let i = 0; i < 3; i++) assert.strictEqual(panel.prepare([], [], { aps: [ap], artifact }).layout.nodes.length, 1, 'OFF survives refresh/navigation');
assert.strictEqual(createTopologyPanel(dependencies).isApVisible(), false, 'session OFF survives recreation');
assert.strictEqual(calls, 2);
element('topologyApToggle').onclick();
panel.prepare([], [], { aps: [ap], artifact });
panel.showError('fixture auth expiry');
const errorMarkup = element('topologyCanvas').innerHTML;
element('topologyApToggle').onclick();
assert.strictEqual(element('topologyCanvas').innerHTML, errorMarkup, 'OFF cannot restore an old protected wired frame after failure');
assert.strictEqual(createTopologyPanel({ ...dependencies, getSessionStorage: () => { throw new Error('blocked'); } }).isApVisible(), false);


async function run() {
  const queries = [];
  const wireless = createWirelessPanel({ document, prometheusQuery: async (query) => {
    queries.push(query);
    if (query.startsWith('unpoller_device_info')) return [{ metric: { ...ap, type: 'uap' }, value: 1 }];
    if (query.startsWith('sum by')) return [{ metric: { name: ap.name }, value: 0 }];
    if (query.startsWith('max by (name) (unpoller_device_up')) return [{ metric: { name: ap.name }, value: 1 }];
    return [];
  } });
  const current = await wireless.fetchApStatus({ topology: true });
  assert.strictEqual(current[0].online, true);
  assert.strictEqual(current[0].clients, 0);
  assert.strictEqual(current[0].mac, ap.mac);
  assert(queries.includes('unpoller_device_info{type="uap"}'));
  assert(queries.includes('sum by (name) (unpoller_device_stations{type="uap"})'));
  const api = require('../bigscreen/api.js');
  const previousFetch = global.fetch;
  const urls = [];
  try {
    global.fetch = async (url, options) => {
      urls.push(url);
      assert.strictEqual(options.cache, 'no-store');
      assert(options.signal instanceof AbortSignal);
      return new Response(JSON.stringify(artifact));
    };
    assert.deepStrictEqual(await api.fetchApAttachments(), artifact);
    assert.deepStrictEqual(urls, ['/topology/ap-attachments.json']);
    let cancelled = false;
    global.fetch = async () => new Response(new ReadableStream({
      start(controller) { controller.enqueue(new Uint8Array(4 * 1024 * 1024 + 1)); },
      cancel() { cancelled = true; }
    }));
    assert.strictEqual(await api.fetchApAttachments(), null);
    assert.strictEqual(cancelled, true, 'over-limit AP artifact stream cancelled before parsing');
    global.fetch = async () => { throw new Error('fixture'); };
    assert.strictEqual(await api.fetchApAttachments(), null, 'AP request failure stays isolated');
  } finally { global.fetch = previousFetch; }
  console.log('bigscreen AP topology: PASS');
}
run().catch((error) => { console.error(error); process.exitCode = 1; });
