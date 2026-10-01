const assert = require('assert');
const topologyPanelModule = require('../bigscreen/charts/topology-panel.js');

assert.deepStrictEqual(
  Object.keys(topologyPanelModule),
  ['createTopologyPanel'],
  'the Topology panel module exposes only its dependency-injected controller factory'
);

class FakeClassList {
  constructor() {
    this.values = new Set();
  }

  add(value) {
    this.values.add(value);
  }

  remove(value) {
    this.values.delete(value);
  }

  contains(value) {
    return this.values.has(value);
  }
}

class FakeElement {
  constructor(tagName = 'DIV') {
    this.tagName = tagName;
    this.dataset = {};
    this.hidden = false;
    this.textContent = '';
    this.classList = new FakeClassList();
    this.attributes = new Map();
    this.listeners = new Map();
    this.onclick = null;
  }

  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(handler);
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  getAttribute(name) {
    return this.attributes.get(name);
  }

  listenerCount(type) {
    return (this.listeners.get(type) || []).length;
  }

  dispatch(type, values = {}) {
    const event = {
      type,
      target: this,
      button: 0,
      clientX: 0,
      clientY: 0,
      pointerId: 1,
      deltaY: 0,
      key: '',
      defaultPrevented: false,
      propagationStopped: false,
      preventDefault() { this.defaultPrevented = true; },
      stopPropagation() { this.propagationStopped = true; },
      ...values
    };
    (this.listeners.get(type) || []).forEach((handler) => handler(event));
    // Model the browser's native keyboard activation for real <button>
    // controls; production intentionally has no custom keydown handler.
    if (
      type === 'keydown' &&
      this.tagName === 'BUTTON' &&
      !event.defaultPrevented &&
      (event.key === 'Enter' || event.key === ' ')
    ) {
      this.dispatch('click');
    }
    if (type === 'click' && !event.propagationStopped && typeof this.onclick === 'function') {
      this.onclick(event);
    }
    return event;
  }

  closest() {
    return null;
  }

  querySelector(selector) {
    if (!this.children) this.children = new Map();
    if (!this.children.has(selector)) this.children.set(selector, new FakeElement('BUTTON'));
    return this.children.get(selector);
  }
}

class FakeSvg extends FakeElement {
  constructor(baseWidth, baseHeight) {
    super();
    this.dataset.baseWidth = String(baseWidth);
    this.dataset.baseHeight = String(baseHeight);
    this.attributes = new Map();
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }

  getAttribute(name) {
    return this.attributes.get(name);
  }
}

class FakeTopologyNode extends FakeElement {
  constructor(index, latencyText) {
    super();
    this.dataset.idx = String(index);
    this.latencyText = new FakeElement();
    this.latencyText.textContent = latencyText;
  }

  closest(selector) {
    return selector === '.topology-node' ? this : null;
  }

  querySelector(selector) {
    return selector === '.topology-node-latency' ? this.latencyText : null;
  }
}

class FakeCanvas extends FakeElement {
  constructor() {
    super();
    this.clientWidth = 800;
    this.clientHeight = 500;
    this.rect = { left: 0, top: 0, width: 800, height: 500 };
    this.svg = null;
    this.nodes = [];
    this.innerHTMLWrites = 0;
    this.capturedPointers = [];
    this.releasedPointers = [];
    this._innerHTML = '';
  }

  set innerHTML(value) {
    this._innerHTML = String(value);
    this.innerHTMLWrites += 1;
    this.nodes = [];
    this.svg = null;
    const svg = this._innerHTML.match(/<svg[^>]*class="topology-svg"[^>]*data-base-width="([^"]+)"[^>]*data-base-height="([^"]+)"/);
    if (svg) this.svg = new FakeSvg(Number(svg[1]), Number(svg[2]));
    const nodePattern = /<g class="topology-node" data-idx="(\d+)"><text class="topology-node-latency">([^<]*)<\/text><\/g>/g;
    let match;
    while ((match = nodePattern.exec(this._innerHTML)) !== null) {
      this.nodes.push(new FakeTopologyNode(Number(match[1]), match[2]));
    }
  }

  get innerHTML() {
    return this._innerHTML;
  }

  querySelector(selector) {
    return selector === '.topology-svg' ? this.svg : null;
  }

  querySelectorAll(selector) {
    return selector === '.topology-node' ? this.nodes : [];
  }

  getBoundingClientRect() {
    return this.rect;
  }

  setPointerCapture(pointerId) {
    this.capturedPointers.push(pointerId);
  }

  releasePointerCapture(pointerId) {
    this.releasedPointers.push(pointerId);
  }
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

const canvas = new FakeCanvas();
const detail = new FakeElement();
const updated = new FakeElement();
const elements = {
  topologyCanvas: canvas,
  topologyDetail: detail,
  topologyUpdated: updated
};
const document = {
  getElementById(id) {
    return elements[id] || null;
  },
  querySelectorAll(selector) {
    return canvas.querySelectorAll(selector);
  }
};

const layoutCalls = [];
const buildTopologyLayers = (targets) => ({
  isps: targets.filter((node) => node.kind === 'isp'),
  firewalls: targets.filter((node) => node.kind === 'firewall'),
  cores: targets.filter((node) => node.kind === 'core'),
  dists: targets.filter((node) => node.kind === 'dist'),
  servers: targets.filter((node) => node.kind === 'server')
});
const topologyLayout = (layers, width, height, edges) => {
  const nodes = [
    ...layers.isps,
    ...layers.firewalls,
    ...layers.cores,
    ...layers.dists,
    ...layers.servers
  ];
  layoutCalls.push({ layers, width, height, edges });
  return { nodes, links: edges, height };
};
const formatPingText = (value) => `${Math.round(value * 1000)}ms`;
const renderTopologySvg = (layout, width) => `
  <svg class="topology-svg" data-base-width="${width}" data-base-height="${layout.height}">
    ${layout.nodes.map((node, index) => `<g class="topology-node" data-idx="${index}"><text class="topology-node-latency">${Number.isFinite(node.latency) ? formatPingText(node.latency) : ''}</text></g>`).join('')}
  </svg>
`;
const panel = topologyPanelModule.createTopologyPanel({
  document,
  location: { protocol: 'http:', hostname: 'bigscreen.local' },
  buildTopologyLayers,
  topologyLayout,
  renderTopologySvg,
  topologyNodeKindLabel: (kind) => ({ core: '核心', isp: 'ISP' }[kind] || kind),
  topologyLatencyIp: (node) => node.kind === 'isp' ? (node.probeIp || node.ip || '') : (node.ip || ''),
  escapeHtml,
  formatPingText
});

assert.deepStrictEqual(
  Object.keys(panel),
  ['isAvailable', 'prepare', 'render', 'updateLatency', 'updateStatus', 'showError', 'clearDetail', 'resetView'],
  'the controller API is explicit and contains no fetch, timer or cache methods'
);
assert.strictEqual(panel.isAvailable(), true, 'the panel owns the Topology canvas availability check');

const nodes = [
  {
    kind: 'core',
    name: 'Core <one>',
    ip: '10.0.0.1',
    level: 'good',
    success: true,
    latency: 0.002
  },
  {
    kind: 'isp',
    name: 'Carrier',
    ip: '203.0.113.10',
    probeIp: '203.0.113.1',
    level: 'good',
    success: true,
    latency: null
  }
];
const edges = [{ from_ip: '10.0.0.1', to_ip: '203.0.113.10' }];
const frame = panel.prepare(nodes, edges);
assert.strictEqual(frame.width, 800, 'the existing container width remains the minimum natural width');
assert.strictEqual(layoutCalls[0].height, 500, 'the existing canvas height is passed into layout');
assert.strictEqual(layoutCalls[0].edges, edges, 'edge input reaches the existing layout unchanged');

panel.render(frame);
assert.ok(canvas.innerHTML.includes('class="topology-svg"'), 'full render injects the SVG result');
assert.strictEqual(canvas.nodes.length, 2, 'rendered data-idx nodes remain one-to-one with layout nodes');
assert.strictEqual(canvas.nodes[0].dataset.idx, '0');
assert.strictEqual(canvas.nodes[1].dataset.idx, '1');
assert.strictEqual(canvas.svg.getAttribute('viewBox'), '0 0 800 500', 'full render applies the current view');

panel.updateStatus(edges);
assert.ok(updated.textContent.startsWith('刷新于 '), 'updated timestamp keeps its existing prefix');
assert.ok(updated.textContent.includes('拖动平移·滚轮缩放·双击复位'));
assert.ok(updated.textContent.endsWith('LLDP 1 条链路'));
panel.updateStatus([]);
assert.ok(updated.textContent.endsWith('LLDP 未发现邻居'));

// Click and keyboard activation render the same escaped, current detail model.
let event = canvas.nodes[1].dispatch('click');
assert.strictEqual(event.propagationStopped, true);
assert.strictEqual(detail.hidden, false);
assert.ok(detail.innerHTML.includes('Core &lt;one&gt;'), 'detail names are escaped');
assert.ok(!detail.innerHTML.includes('Core <one>'));
assert.ok(detail.innerHTML.includes('<dt>类型</dt><dd>核心</dd>'));
assert.ok(detail.innerHTML.includes('<dt>状态</dt><dd>在线</dd>'));
assert.ok(detail.innerHTML.includes('<dt>延迟</dt><dd>2ms</dd>'));
assert.ok(detail.innerHTML.includes('href="/latency?ip=10.0.0.1"'));
assert.ok(detail.innerHTML.includes('http://bigscreen.local:3000/d/device-syslog?var-host=10.0.0.1'));

detail.hidden = true;
event = canvas.nodes[1].dispatch('keydown', { key: 'Enter' });
assert.strictEqual(event.defaultPrevented, true);
assert.strictEqual(detail.hidden, false, 'Enter opens the node detail');
detail.hidden = true;
event = canvas.nodes[1].dispatch('keydown', { key: ' ' });
assert.strictEqual(event.defaultPrevented, true);
assert.strictEqual(detail.hidden, false, 'Space opens the node detail');
canvas.dispatch('click', { target: canvas });
assert.strictEqual(detail.hidden, true, 'a blank canvas click closes the detail');

// Incremental refresh updates text and the current node model without rebuilding SVG.
const htmlWritesBeforeIncremental = canvas.innerHTMLWrites;
const freshNodes = frame.layout.nodes.map((node) => (
  node.kind === 'core' ? { ...node, latency: 0.009 } : { ...node, latency: null }
));
panel.updateLatency(freshNodes);
assert.strictEqual(canvas.innerHTMLWrites, htmlWritesBeforeIncremental, 'latency-only update does not replace SVG');
assert.strictEqual(canvas.nodes[1].latencyText.textContent, '9ms');
assert.strictEqual(canvas.nodes[0].latencyText.textContent, '在线', 'online ISP without latency keeps its existing text');
canvas.nodes[1].dispatch('click');
assert.ok(detail.innerHTML.includes('<dt>延迟</dt><dd>9ms</dd>'), 'existing handlers read the latest node after incremental update');

// Pan binds once. Exactly four pixels is not a drag; more than four is.
assert.strictEqual(canvas.listenerCount('pointerdown'), 1);
assert.strictEqual(canvas.listenerCount('wheel'), 1);
canvas.dispatch('pointerdown', { clientX: 10, clientY: 10, pointerId: 7 });
canvas.dispatch('pointermove', { clientX: 14, clientY: 14, pointerId: 7 });
assert.strictEqual(canvas.classList.contains('topology-grabbing'), false, 'four pixels remains a click');
canvas.dispatch('pointerup', { pointerId: 7 });

detail.hidden = false;
canvas.dispatch('pointerdown', { clientX: 10, clientY: 10, pointerId: 8 });
canvas.dispatch('pointermove', { clientX: 16, clientY: 10, pointerId: 8 });
assert.strictEqual(canvas.classList.contains('topology-grabbing'), true, 'movement above four pixels starts drag');
assert.deepStrictEqual(canvas.capturedPointers, [8]);
assert.notStrictEqual(canvas.svg.getAttribute('viewBox'), '0 0 800 500', 'drag updates the viewBox');
canvas.dispatch('pointerup', { pointerId: 8 });
assert.strictEqual(canvas.classList.contains('topology-grabbing'), false);
assert.deepStrictEqual(canvas.releasedPointers, [8]);
event = canvas.dispatch('click', { target: canvas });
assert.strictEqual(event.propagationStopped, true, 'the click following a drag is swallowed');
assert.strictEqual(detail.hidden, false, 'the swallowed click does not close detail');
canvas.dispatch('click', { target: canvas });
assert.strictEqual(detail.hidden, true, 'the next ordinary click behaves normally');

panel.resetView();
for (let index = 0; index < 40; index += 1) {
  event = canvas.dispatch('wheel', { clientX: 400, clientY: 250, deltaY: -1 });
}
assert.strictEqual(event.defaultPrevented, true);
let viewBox = canvas.svg.getAttribute('viewBox').split(' ').map(Number);
assert.ok(Math.abs(viewBox[2] - 200) < 1e-9, 'wheel zoom is capped at scale 4');
assert.ok(Math.abs(viewBox[3] - 125) < 1e-9);

for (let index = 0; index < 80; index += 1) {
  canvas.dispatch('wheel', { clientX: 400, clientY: 250, deltaY: 1 });
}
viewBox = canvas.svg.getAttribute('viewBox').split(' ').map(Number);
assert.ok(Math.abs(viewBox[2] - (800 / 0.3)) < 1e-9, 'wheel zoom is capped at scale 0.3');
assert.ok(Math.abs(viewBox[3] - (500 / 0.3)) < 1e-9);

canvas.dispatch('dblclick');
assert.strictEqual(canvas.svg.getAttribute('viewBox'), '0 0 800 500', 'double click resets the view');

// A full redraw retains the current view and does not duplicate persistent events.
canvas.dispatch('wheel', { clientX: 400, clientY: 250, deltaY: -1 });
const zoomedView = canvas.svg.getAttribute('viewBox');
panel.render(frame);
assert.strictEqual(canvas.svg.getAttribute('viewBox'), zoomedView, 'full redraw reapplies the existing pan/zoom view');
assert.strictEqual(canvas.listenerCount('pointerdown'), 1, 'pan events are bound only once');
assert.strictEqual(canvas.listenerCount('wheel'), 1, 'wheel events are bound only once');

panel.clearDetail();
assert.strictEqual(detail.hidden, true);
assert.strictEqual(detail.innerHTML, '<div class="topology-empty">点击任意节点查看详情</div>');

panel.showError('<boom & retry>');
assert.strictEqual(
  canvas.innerHTML,
  '<div class="topology-error">拓扑数据拉取失败: &lt;boom &amp; retry&gt;</div>',
  'error output keeps the existing structure and escapes the message'
);

// Empty topology remains an empty SVG rather than changing product semantics.
const emptyFrame = panel.prepare([], []);
panel.render(emptyFrame);
assert.ok(canvas.innerHTML.includes('class="topology-svg"'));
assert.strictEqual(canvas.nodes.length, 0);

// Natural width still reserves one 168px slot per dist/server population.
const wideTargets = Array.from({ length: 6 }, (_, index) => ({
  kind: index === 5 ? 'server' : 'dist',
  name: `node-${index}`,
  ip: `10.0.0.${index + 1}`,
  level: 'good',
  success: true,
  latency: 0.002
}));
const wideFrame = panel.prepare(wideTargets, []);
assert.strictEqual(wideFrame.width, 6 * 168 + 48, 'dist and server nodes retain the existing natural-width calculation');

console.log('bigscreen Topology panel tests passed');

// Inspector responses are used only for the most recently selected, still-open node.
(async () => {
  const pending = [];
  const inspectorPanel = topologyPanelModule.createTopologyPanel({
    document, location: { protocol: 'http:', hostname: 'bigscreen.local' },
    buildTopologyLayers, topologyLayout, renderTopologySvg,
    topologyNodeKindLabel: (kind) => kind,
    topologyLatencyIp: (node) => node.ip,
    escapeHtml, formatPingText,
    fetchNodeInspector: (ip) => new Promise((resolve) => pending.push({ ip, resolve }))
  });
  inspectorPanel.render(inspectorPanel.prepare([
    { kind: 'core', ip: '10.0.0.1', name: 'A', level: 'good' },
    { kind: 'dist', ip: '10.0.0.2', name: 'B', level: 'none' }
  ], []));
  canvas.nodes[0].dispatch('click');
  assert.ok(detail.innerHTML.includes('节点详情读取中'));
  assert.ok(!detail.innerHTML.includes('<dt>类型</dt>'), 'authenticated Inspector has no legacy-card flash');
  canvas.nodes[1].dispatch('click');
  assert.deepStrictEqual(pending.map((item) => item.ip), ['10.0.0.1', '10.0.0.2']);
  pending[0].resolve({ name: 'stale', ports: { up: 9, down: 0, unknown: 0 } });
  await Promise.resolve();
  assert.ok(!detail.innerHTML.includes('stale'), 'older selection cannot replace newer selection');
  pending[1].resolve({ ip: '10.0.0.2', name: '<B>', model: 'switch', online: 'unknown',
    latencySeconds: null, ports: { up: 2, down: 1, unknown: 1 }, neighbors: [
      { localPort: 'Gi1', peerIp: '10.0.0.3', aggregatePort: 'Po1', members: ['Gi1'] },
      { localPort: 'Gi2', peerIp: '10.0.0.3', aggregatePort: 'Po1', members: ['Gi2'] },
      { localPort: 'Gi3', peerIp: '10.0.0.4', aggregatePort: 'Po2', members: ['Gi3'] }
    ], connections: { peers: 2, aggregates: 2 },
    warnings: ['partial <source>', '已省略过期邻接', '拓扑快照已过期'] });
  await Promise.resolve();
  assert.ok(detail.innerHTML.includes('&lt;B&gt;'));
  assert.ok(detail.innerHTML.includes('未知'));
  assert.ok(detail.innerHTML.includes('在线 2 / 离线 1 / 未知 1'));
  assert.ok(detail.innerHTML.includes('连接摘要'));
  assert.ok(detail.innerHTML.includes('邻接 2 台 · 聚合链路 2 组'));
  assert.ok(!detail.innerHTML.includes('Gi1'));
  assert.ok(!detail.innerHTML.includes('10.0.0.3'));
  assert.ok(!detail.innerHTML.includes('已发现上联与邻居'));
  assert.ok(detail.innerHTML.includes('已省略过期邻接'));
  assert.ok(detail.innerHTML.includes('拓扑快照已过期'));
  assert.ok(detail.innerHTML.includes('partial &lt;source&gt;'));
  assert.ok(!detail.innerHTML.includes('partial <source>'));
  assert.ok(detail.innerHTML.includes('href="/latency?ip=10.0.0.2"'));
  assert.ok(detail.innerHTML.includes('var-host=10.0.0.2'), 'final Inspector retains Syslog');
  inspectorPanel.clearDetail();
  canvas.nodes[0].dispatch('click');
  pending[2].resolve({ ip: '10.0.0.1', neighbors: [{ peerIp: '10.0.0.2', aggregatePort: 'Po1' }],
    connections: { peers: 16, aggregates: 3 }, warnings: [] });
  await Promise.resolve();
  assert.ok(detail.innerHTML.includes('邻接 16 台 · 聚合链路 3 组'));
  inspectorPanel.clearDetail();
  canvas.nodes[0].dispatch('click');
  pending[3].resolve({ ip: '10.0.0.1', neighbors: [],
    connections: { peers: 0, aggregates: 0 }, warnings: [] });
  await Promise.resolve();
  assert.ok(detail.innerHTML.includes('邻接 0 台 · 聚合链路 0 组'));
  inspectorPanel.clearDetail();
  canvas.nodes[0].dispatch('click');
  pending[4].resolve({ ip: '10.0.0.1', neighbors: [],
    connections: { peers: 0, aggregates: 0 }, warnings: ['邻接资料暂不可用'] });
  await Promise.resolve();
  assert.ok(detail.innerHTML.includes('<dt>连接摘要</dt><dd>—</dd>'));
  inspectorPanel.clearDetail();
  canvas.nodes[0].dispatch('click');
  pending[5].resolve({ ip: '10.0.0.1',
    neighbors: Array.from({ length: 12 }, (_, index) => ({ peerIp: `10.0.0.${index + 20}`, aggregatePort: 'Po1' })),
    warnings: ['已省略过期邻接'] });
  await Promise.resolve();
  assert.ok(detail.innerHTML.includes('<dt>连接摘要</dt><dd>—</dd>'));
  assert.ok(!detail.innerHTML.includes('邻接 12 台'));
  assert.ok(detail.innerHTML.includes('已省略过期邻接'));
  inspectorPanel.clearDetail();
  canvas.nodes[0].dispatch('click');
  inspectorPanel.clearDetail();
  pending[6].resolve({ name: 'late' });
  await Promise.resolve();
  assert.strictEqual(detail.hidden, true);
  assert.ok(!detail.innerHTML.includes('late'));

  const inspectorReads = [];
  const portReads = [];
  const portEvents = [];
  let portOpen = false;
  let closeHandler;
  const portPanel = {
    setCloseHandler(handler) { closeHandler = handler; },
    close() { portOpen = false; portEvents.push('close'); },
    loading(ip) { portOpen = true; portEvents.push(`loading:${ip}`); },
    open(data) { portEvents.push(`open:${data.ip}`); },
    failure() { portEvents.push('failure'); },
    isOpen() { return portOpen; }
  };
  const portsPanel = topologyPanelModule.createTopologyPanel({
    document, location: { protocol: 'http:', hostname: 'bigscreen.local' },
    buildTopologyLayers, topologyLayout, renderTopologySvg,
    topologyNodeKindLabel: (kind) => kind,
    topologyLatencyIp: (node) => node.ip,
    escapeHtml, formatPingText, portPanel,
    fetchNodeInspector: (ip) => new Promise((resolve) => inspectorReads.push({ ip, resolve })),
    fetchNodePorts: (ip) => new Promise((resolve) => portReads.push({ ip, resolve }))
  });
  portsPanel.render(portsPanel.prepare([
    { kind: 'core', ip: '10.0.0.1', name: 'Cisco A' },
    { kind: 'dist', ip: '10.0.0.2', name: 'Cisco B' },
    { kind: 'firewall', ip: '10.0.0.3', name: 'Hillstone' }
  ], []));
  canvas.nodes[0].dispatch('click');
  assert.ok(detail.innerHTML.includes('节点详情读取中'));
  assert.ok(!detail.innerHTML.includes('<dt>类型</dt>'));
  inspectorReads[0].resolve({ kind: 'hillstone', ip: '10.0.0.3' });
  await Promise.resolve();
  assert.ok(detail.innerHTML.includes('查看接口'), 'Hillstone offers the Interface Panel');
  assert.ok(!detail.innerHTML.includes('查看端口'), 'Hillstone does not offer Cisco port layout');
  assert.ok(detail.innerHTML.includes('href="/latency?ip=10.0.0.3"'));
  assert.ok(detail.innerHTML.includes('var-host=10.0.0.3'), 'firewall final Inspector retains Syslog');
  canvas.nodes[1].dispatch('click');
  inspectorReads[1].resolve({ kind: 'cisco', ip: '10.0.0.1' });
  await Promise.resolve();
  assert.ok(detail.innerHTML.includes('topology-view-ports'));
  detail.querySelector('.topology-view-ports').onclick();
  assert.deepStrictEqual(portReads.map((read) => read.ip), ['10.0.0.1'], 'one click makes one node read');
  assert.ok(portEvents.includes('loading:10.0.0.1'));
  canvas.nodes[2].dispatch('click');
  inspectorReads[2].resolve({ kind: 'cisco', ip: '10.0.0.2' });
  await Promise.resolve();
  detail.querySelector('.topology-view-ports').onclick();
  portReads[0].resolve({ ip: '10.0.0.1', ports: [] });
  await Promise.resolve();
  assert.ok(!portEvents.includes('open:10.0.0.1'), 'old node response cannot replace newer panel');
  portReads[1].resolve({ ip: '10.0.0.2', ports: [] });
  await Promise.resolve();
  assert.ok(portEvents.includes('open:10.0.0.2'));
  detail.querySelector('.topology-view-ports').onclick();
  closeHandler(); portPanel.close();
  portReads[2].resolve({ ip: '10.0.0.2', ports: [] });
  await Promise.resolve();
  assert.strictEqual(portEvents.filter((item) => item === 'open:10.0.0.2').length, 1,
    'closing the drawer invalidates its pending response');
  detail.querySelector('.topology-view-ports').onclick();
  portsPanel.clearDetail();
  portReads[3].resolve({ ip: '10.0.0.2', ports: [] });
  await Promise.resolve();
  assert.strictEqual(portEvents.filter((item) => item === 'open:10.0.0.2').length, 1,
    'close/clear invalidates late requests');

  const hillstoneReads = [];
  const hillstonePanel = topologyPanelModule.createTopologyPanel({
    document, location: { protocol: 'http:', hostname: 'bigscreen.local' },
    buildTopologyLayers, topologyLayout, renderTopologySvg,
    topologyNodeKindLabel: (kind) => kind, topologyLatencyIp: (node) => node.ip,
    escapeHtml, formatPingText, portPanel,
    fetchNodeInspector: () => Promise.resolve({ kind: 'hillstone', ip: '10.0.0.3', haRole: 'unavailable' }),
    fetchNodePorts: (ip) => new Promise((resolve) => hillstoneReads.push({ ip, resolve }))
  });
  hillstonePanel.render(hillstonePanel.prepare([
    { kind: 'firewall', ip: '10.0.0.3', name: 'Hillstone' },
    { kind: 'core', ip: '10.0.0.1', name: 'Cisco' }
  ], []));
  canvas.nodes[0].dispatch('click');
  await Promise.resolve();
  assert.ok(detail.innerHTML.includes('HA 角色</dt><dd class="topology-ha-value">未知（暂无可信数据源）'));
  detail.querySelector('.topology-view-ports').onclick();
  assert.deepStrictEqual(hillstoneReads.map((read) => read.ip), ['10.0.0.3']);
  canvas.nodes[1].dispatch('click');
  hillstoneReads[0].resolve({ ip: '10.0.0.3', kind: 'hillstone', ports: [] });
  await Promise.resolve();
  assert.ok(!portEvents.includes('open:10.0.0.3'), 'late Hillstone response cannot replace another node');
  canvas.nodes[0].dispatch('click');
  await Promise.resolve();
  detail.querySelector('.topology-view-ports').onclick();
  detail.querySelector('.topology-detail-close').onclick();
  hillstoneReads[1].resolve({ ip: '10.0.0.3', kind: 'hillstone', ports: [] });
  await Promise.resolve();
  assert.strictEqual(portEvents.filter((event) => event === 'open:10.0.0.3').length, 0,
    'closing Hillstone Inspector invalidates its pending Interface Panel');

  const haCases = [
    [{ source: 'Hillstone sysHAStatus', fresh: true, units: [
      { ip: '10.0.0.11', name: 'fw-a', state: 'master', fresh: true },
      { ip: '10.0.0.12', name: 'fw-b', state: 'backup', fresh: true }
    ] }, 'fw-a Master · fw-b Backup'],
    [{ source: 'Hillstone sysHAStatus', fresh: false, units: [
      { name: 'fw-a', state: 'master', fresh: true }, { name: 'fw-b', state: 'backup', fresh: false }
    ] }, 'fw-a Master · fw-b 未知'],
    [{ source: 'Hillstone sysHAStatus', fresh: true, units: [
      { name: 'fw-a', state: 'AA-mode', fresh: true }
    ] }, 'fw-a AA-mode'],
    [{ source: 'Hillstone sysHAStatus', fresh: true, units: [
      { name: '<fw-a>', state: 'vendor-unknown', code: 5, fresh: true }
    ] }, '&lt;fw-a&gt; 未知（厂商 slase / 5）'],
    [{ source: 'Hillstone sysHAStatus', fresh: false, units: [] }, '未知（暂无可信数据源）'],
    [{ source: 'traffic guess', fresh: true, units: [{ name: 'fw-a', state: 'master', fresh: true }] }, '未知（暂无可信数据源）']
  ];
  for (const [ha, expected] of haCases) {
    let portReads = 0;
    const haPanel = topologyPanelModule.createTopologyPanel({
      document, location: { protocol: 'http:', hostname: 'bigscreen.local' },
      buildTopologyLayers, topologyLayout, renderTopologySvg,
      topologyNodeKindLabel: (kind) => kind, topologyLatencyIp: (node) => node.ip,
      escapeHtml, formatPingText, portPanel,
      fetchNodeInspector: () => Promise.resolve({ kind: 'hillstone', ip: '10.0.0.3', ha }),
      fetchNodePorts: () => { portReads += 1; return Promise.resolve({ kind: 'hillstone', ports: [] }); }
    });
    haPanel.render(haPanel.prepare([{ kind: 'firewall', ip: '10.0.0.3', name: 'VIP' }], []));
    canvas.nodes[0].dispatch('click');
    await Promise.resolve();
    assert.ok(detail.innerHTML.includes(expected), expected);
    if (ha.source === 'Hillstone sysHAStatus' && ha.units.length) {
      assert.ok(detail.innerHTML.includes('HA 集群</dt><dd class="topology-ha-value">'));
      assert.ok(!detail.innerHTML.includes('HA 角色</dt>'), 'VIP never receives a physical role');
    }
    assert.ok(detail.innerHTML.includes('查看接口'));
    assert.strictEqual(portReads, 0, 'lightweight HA Inspector does not eagerly read full interfaces/history');
    detail.querySelector('.topology-view-ports').onclick();
    await Promise.resolve();
    assert.strictEqual(portReads, 1, 'existing on-demand Interface Panel entry is reused');
  }

  const physicalPair = [
    { ip: '10.0.0.11', state: 'master', fresh: true },
    { ip: '10.0.0.12', state: 'backup', fresh: true }
  ];
  const selectedCases = [
    ['10.0.0.11', physicalPair, 'Master', '10.0.0.12 Backup'],
    ['10.0.0.12', physicalPair, 'Backup', '10.0.0.11 Master'],
    ['10.0.0.11', [physicalPair[0], { ...physicalPair[1], fresh: false }], 'Master', '10.0.0.12 未知'],
    ['10.0.0.11', [physicalPair[0], { ip: '10.0.0.12' }], 'Master', '10.0.0.12 未知'],
    ['10.0.0.11', [{ ...physicalPair[0], state: 'unknown', fresh: false }, physicalPair[1]], '未知', '10.0.0.12 Backup'],
    ['10.0.0.11', [{ ...physicalPair[0], state: 'vendor-unknown', code: 5 }, physicalPair[1]], '未知（厂商 slase / 5）', '10.0.0.12 Backup'],
    ['10.0.0.11', [{ ...physicalPair[0], state: 'AA-mode' }, physicalPair[1]], 'AA-mode', '10.0.0.12 Backup'],
    ['10.0.0.11', [physicalPair[0], { ...physicalPair[1], name: '<img src=x onerror=alert(1)>' }], 'Master', '&lt;img src=x onerror=alert(1)&gt; Backup'],
    ['10.0.0.11', [physicalPair[0], { ...physicalPair[1], ip: '<svg onload=alert(1)>' }], 'Master', '&lt;svg onload=alert(1)&gt; Backup'],
    ['10.0.0.11', [physicalPair[0], { ...physicalPair[1], name: 'long-peer-name-'.repeat(10) }], 'Master', `${'long-peer-name-'.repeat(10)} Backup`]
  ];
  for (const [ip, units, own, peer] of selectedCases) {
    const selectedPanel = topologyPanelModule.createTopologyPanel({
      document, location: { protocol: 'http:', hostname: 'bigscreen.local' },
      buildTopologyLayers, topologyLayout, renderTopologySvg,
      topologyNodeKindLabel: (kind) => kind, topologyLatencyIp: (node) => node.ip,
      escapeHtml, formatPingText, portPanel,
      fetchNodeInspector: () => Promise.resolve({ kind: 'hillstone', ip, hostname: 'compact-host',
        ha: { source: 'Hillstone sysHAStatus', units } })
    });
    selectedPanel.render(selectedPanel.prepare([{ kind: 'firewall', ip, name: 'Physical unit' }], []));
    canvas.nodes[0].dispatch('click');
    await Promise.resolve();
    assert.ok(detail.innerHTML.includes(`HA 角色</dt><dd class="topology-ha-value">${own}</dd>`));
    assert.ok(detail.innerHTML.includes(`HA 对端</dt><dd class="topology-ha-value">${peer}</dd>`));
    assert.ok(!detail.innerHTML.includes('HA 集群'), 'a physical unit retains its own identity');
    assert.ok(!detail.innerHTML.includes('<img'), 'unit labels remain escaped');
    assert.ok(detail.innerHTML.includes('Hostname</dt><dd>compact-host</dd>'), 'ordinary rows keep compact styling');
  }

  const css = require('fs').readFileSync(require('path').join(__dirname, '../bigscreen/style.css'), 'utf8');
  const genericStyle = css.match(/\.topology-detail dd\s*\{([^}]+)\}/)[1];
  const haStyle = css.match(/\.topology-detail dd\.topology-ha-value\s*\{([^}]+)\}/)[1];
  assert.ok(/white-space:\s*nowrap/.test(genericStyle) && /text-overflow:\s*ellipsis/.test(genericStyle));
  assert.ok(/white-space:\s*normal/.test(haStyle) && /overflow:\s*visible/.test(haStyle));
  assert.ok(/text-overflow:\s*clip/.test(haStyle) && /overflow-wrap:\s*anywhere/.test(haStyle), 'long HA values wrap instead of ellipsis');

  for (const kind of ['generic-switch', 'unknown']) {
    const switchPanel = topologyPanelModule.createTopologyPanel({
      document, location: { protocol: 'http:', hostname: 'bigscreen.local' },
      buildTopologyLayers, topologyLayout, renderTopologySvg,
      topologyNodeKindLabel: (value) => value, topologyLatencyIp: (node) => node.ip,
      escapeHtml, formatPingText, portPanel,
      fetchNodeInspector: () => Promise.resolve({ kind, ip: '10.0.0.4' }),
      fetchNodePorts: () => Promise.resolve({ kind, ports: [] })
    });
    switchPanel.render(switchPanel.prepare([{ kind: 'core', ip: '10.0.0.4', name: 'SG220' }], []));
    canvas.nodes[0].dispatch('click');
    await Promise.resolve();
    assert.strictEqual(detail.innerHTML.includes('查看接口'), kind === 'generic-switch');
    assert.ok(!detail.innerHTML.includes('查看端口'));
  }

  const anonymousPanel = topologyPanelModule.createTopologyPanel({
    document, location: { protocol: 'http:', hostname: 'bigscreen.local' },
    buildTopologyLayers, topologyLayout, renderTopologySvg,
    topologyNodeKindLabel: (kind) => kind, topologyLatencyIp: (node) => node.ip,
    escapeHtml, formatPingText,
    fetchNodeInspector: () => Promise.reject({ status: 401 })
  });
  anonymousPanel.render(anonymousPanel.prepare([{ kind: 'core', ip: '10.0.0.1', name: 'A' }], []));
  canvas.nodes[0].dispatch('click');
  assert.ok(detail.innerHTML.includes('节点详情读取中'));
  await Promise.resolve();
  await Promise.resolve();
  assert.ok(detail.innerHTML.includes('<dt>类型</dt><dd>core</dd>'), 'anonymous topology keeps legacy detail after auth denial');
  assert.ok(detail.innerHTML.includes('href="/latency?ip=10.0.0.1"'));
  for (const failure of [{ status: 503 }, new Error('transport failure')]) {
    const failedPanel = topologyPanelModule.createTopologyPanel({
      document, location: { protocol: 'http:', hostname: 'bigscreen.local' },
      buildTopologyLayers, topologyLayout, renderTopologySvg,
      topologyNodeKindLabel: (kind) => kind, topologyLatencyIp: (node) => node.ip,
      escapeHtml, formatPingText,
      fetchNodeInspector: () => Promise.reject(failure)
    });
    failedPanel.render(failedPanel.prepare([{ kind: 'core', ip: '10.0.0.1', name: 'A' }], []));
    canvas.nodes[0].dispatch('click');
    assert.ok(detail.innerHTML.includes('节点详情读取中'));
    await Promise.resolve();
    await Promise.resolve();
    assert.ok(!detail.innerHTML.includes('节点详情读取中'), 'failed Inspector must leave loading state');
    assert.ok(detail.innerHTML.includes('节点详情暂不可用'));
    assert.ok(detail.innerHTML.includes('href="/latency?ip=10.0.0.1"'));
    assert.ok(detail.innerHTML.includes('var-host=10.0.0.1'));
    detail.querySelector('.topology-detail-close').onclick();
    assert.strictEqual(detail.hidden, true, 'failure detail remains closable');
  }
  console.log('bigscreen Node Inspector lifecycle tests passed');
})().catch((error) => { console.error(error); process.exitCode = 1; });
