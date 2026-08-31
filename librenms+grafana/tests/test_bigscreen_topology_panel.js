const assert = require('assert');
const topologyPanelModule = require('../bigscreen/charts/topology-panel.js');

global.window = {
  BIGSCREEN_CONFIG: {},
  BIGSCREEN_QUERIES: {},
  BIGSCREEN_PAGES: []
};

const { projectPhysicalTopology: projectPhysicalTopologyReal } = require(
  '../bigscreen/physical-topology.js'
);
const {
  physicalTopologyLayout: physicalTopologyLayoutReal,
  renderPhysicalTopologySvg: renderPhysicalTopologySvgReal
} = require('../bigscreen/topology.js');

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
const operationsButton = new FakeElement('BUTTON');
const physicalButton = new FakeElement('BUTTON');
const elements = {
  topologyCanvas: canvas,
  topologyDetail: detail,
  topologyUpdated: updated,
  topologyViewOperations: operationsButton,
  topologyViewPhysical: physicalButton
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
const physicalProjectionCalls = [];
const physicalLayoutCalls = [];
const physicalRenderCalls = [];
const modeChanges = [];
const projectPhysicalTopology = (edges) => {
  physicalProjectionCalls.push(edges);
  return {
    devices: [],
    physicalLinks: edges.map((edge, index) => ({ id: `physical-${index}`, edge })),
    bundles: [],
    serverAttachments: [],
    compatibilityWarnings: []
  };
};
const physicalTopologyLayout = (projection, targets, width, height) => {
  physicalLayoutCalls.push({ projection, targets, width, height });
  return { nodes: targets, links: projection.physicalLinks, width, height };
};
const renderPhysicalTopologySvg = (layout, width) => {
  physicalRenderCalls.push({ layout, width });
  return `
    <svg class="topology-svg" data-base-width="${width}" data-base-height="${layout.height}">
      ${layout.nodes.map((node, index) => `<g class="topology-node" data-idx="${index}"><text class="topology-node-latency">${Number.isFinite(node.latency) ? formatPingText(node.latency) : ''}</text></g>`).join('')}
    </svg>
  `;
};

const panel = topologyPanelModule.createTopologyPanel({
  document,
  location: { protocol: 'http:', hostname: 'bigscreen.local' },
  buildTopologyLayers,
  topologyLayout,
  renderTopologySvg,
  projectPhysicalTopology,
  physicalTopologyLayout,
  renderPhysicalTopologySvg,
  topologyNodeKindLabel: (kind) => ({ core: '核心', isp: 'ISP' }[kind] || kind),
  topologyLatencyIp: (node) => node.kind === 'isp' ? (node.probeIp || node.ip || '') : (node.ip || ''),
  escapeHtml,
  formatPingText,
  onModeChange: (mode) => modeChanges.push(mode)
});

assert.deepStrictEqual(
  Object.keys(panel),
  ['isAvailable', 'prepare', 'render', 'updateLatency', 'updateStatus', 'showError', 'clearDetail', 'resetView', 'getMode', 'setMode'],
  'the controller API is explicit and contains no fetch, timer or cache methods'
);
assert.strictEqual(panel.isAvailable(), true, 'the panel owns the Topology canvas availability check');
assert.strictEqual(panel.getMode(), 'operations', 'Operations is the explicit default mode');
assert.strictEqual(operationsButton.getAttribute('aria-pressed'), 'true');
assert.strictEqual(physicalButton.getAttribute('aria-pressed'), 'false');
assert.strictEqual(operationsButton.listenerCount('click'), 1);
assert.strictEqual(physicalButton.listenerCount('click'), 1);

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
assert.ok(updated.textContent.endsWith('LLDP 1 条边'));
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

// Explicit mode navigation reuses the latest caller-owned data and resets view.
physicalButton.dispatch('click');
assert.strictEqual(panel.getMode(), 'physical');
assert.deepStrictEqual(modeChanges, ['physical']);
assert.strictEqual(operationsButton.getAttribute('aria-pressed'), 'false');
assert.strictEqual(physicalButton.getAttribute('aria-pressed'), 'true');
assert.strictEqual(canvas.svg.getAttribute('viewBox'), '0 0 800 500', 'mode change resets pan/zoom');

const physicalEdges = [{ from_ip: '10.0.0.1', to_ip: '203.0.113.10' }];
const physicalFrame = panel.prepare(nodes, physicalEdges);
assert.strictEqual(physicalProjectionCalls.length, 1);
assert.strictEqual(physicalProjectionCalls[0], physicalEdges, 'Physical mode consumes the shared edge snapshot');
assert.strictEqual(physicalLayoutCalls.length, 1);
assert.strictEqual(physicalLayoutCalls[0].targets, nodes, 'Physical nodes receive the shared target snapshot');
panel.render(physicalFrame);
assert.strictEqual(physicalRenderCalls.length, 1);
assert.strictEqual(canvas.dataset.topologyView, 'physical');
panel.updateStatus(physicalEdges);
assert.ok(updated.textContent.endsWith('Physical 1 条链路'));

operationsButton.dispatch('click');
assert.strictEqual(panel.getMode(), 'operations');
assert.deepStrictEqual(modeChanges, ['physical', 'operations']);
assert.strictEqual(operationsButton.getAttribute('aria-pressed'), 'true');
assert.strictEqual(physicalButton.getAttribute('aria-pressed'), 'false');
assert.strictEqual(panel.setMode('unsupported'), false, 'unknown mode is rejected without changing state');
const operationsFrameAgain = panel.prepare(nodes, edges);
panel.render(operationsFrameAgain);
assert.strictEqual(canvas.dataset.topologyView, 'operations');
assert.ok(canvas.innerHTML.includes('class="topology-svg"'));

// MULTI_MODE_SWITCHING / MODE_BUTTON_KEYBOARD: native Enter/Space activation
// drives several complete cycles without duplicate listeners or render roots.
let projectionCountBeforeKeyboard = physicalProjectionCalls.length;
physicalButton.dispatch('keydown', { key: 'Enter' });
assert.strictEqual(panel.getMode(), 'physical');
assert.strictEqual(physicalButton.getAttribute('aria-pressed'), 'true');
assert.strictEqual(operationsButton.getAttribute('aria-pressed'), 'false');
assert.strictEqual(
  physicalProjectionCalls.length,
  projectionCountBeforeKeyboard,
  'keyboard mode navigation itself does not fetch or prepare new data'
);
operationsButton.dispatch('keydown', { key: ' ' });
assert.strictEqual(panel.getMode(), 'operations');
assert.strictEqual(operationsButton.getAttribute('aria-pressed'), 'true');
physicalButton.dispatch('keydown', { key: ' ' });
assert.strictEqual(panel.getMode(), 'physical');
assert.strictEqual(physicalButton.getAttribute('aria-pressed'), 'true');
operationsButton.dispatch('keydown', { key: 'Enter' });
assert.strictEqual(panel.getMode(), 'operations');
assert.strictEqual(operationsButton.getAttribute('aria-pressed'), 'true');
assert.strictEqual(physicalButton.getAttribute('aria-pressed'), 'false');
assert.strictEqual(operationsButton.listenerCount('click'), 1);
assert.strictEqual(physicalButton.listenerCount('click'), 1);
assert.strictEqual(operationsButton.listenerCount('keydown'), 0, 'native button activation needs no custom key handler');
assert.strictEqual(physicalButton.listenerCount('keydown'), 0, 'native button activation needs no custom key handler');
assert.ok(canvas.svg, 'each mode owns exactly one current SVG root');

// PHYSICAL_PAN_ZOOM_RESET and incremental raw latency update exercise the
// shared interaction implementation while Physical is the active renderer.
physicalButton.dispatch('click');
const interactivePhysicalFrame = panel.prepare(nodes, physicalEdges);
panel.render(interactivePhysicalFrame);
assert.strictEqual(canvas.svg.getAttribute('viewBox'), '0 0 800 500');
const physicalHtmlWrites = canvas.innerHTMLWrites;
panel.updateLatency(interactivePhysicalFrame.layout.nodes.map((node) => ({
  ...node,
  latency: node.kind === 'core' ? 0.003 : node.latency
})));
assert.strictEqual(canvas.innerHTMLWrites, physicalHtmlWrites, 'Physical latency jitter is incremental');
assert.strictEqual(canvas.nodes[0].latencyText.textContent, '3ms');
canvas.dispatch('pointerdown', { clientX: 20, clientY: 20, pointerId: 12 });
canvas.dispatch('pointermove', { clientX: 32, clientY: 20, pointerId: 12 });
assert.notStrictEqual(canvas.svg.getAttribute('viewBox'), '0 0 800 500', 'Physical pan changes the view');
canvas.dispatch('pointerup', { pointerId: 12 });
panel.resetView();
assert.strictEqual(canvas.svg.getAttribute('viewBox'), '0 0 800 500');
canvas.dispatch('wheel', { clientX: 400, clientY: 250, deltaY: -1 });
assert.notStrictEqual(canvas.svg.getAttribute('viewBox'), '0 0 800 500', 'Physical zoom changes the view');
canvas.dispatch('dblclick');
assert.strictEqual(canvas.svg.getAttribute('viewBox'), '0 0 800 500', 'Physical double-click reset restores the view');
operationsButton.dispatch('click');
const postPhysicalOperationsFrame = panel.prepare(nodes, edges);
panel.render(postPhysicalOperationsFrame);
assert.strictEqual(canvas.svg.getAttribute('viewBox'), '0 0 800 500', 'mode switch leaves no stale Physical transform');
assert.strictEqual(canvas.listenerCount('pointerdown'), 1);
assert.strictEqual(canvas.listenerCount('wheel'), 1);

// BUNDLE_FULL_PANEL_INTEGRATION: raw Phase 2 rows traverse the real projector,
// Physical layout and renderer through the panel controller without pairing
// endpoint member arrays as cables.
const bundleCanvas = new FakeCanvas();
const bundleDetail = new FakeElement();
const bundleUpdated = new FakeElement();
const bundleOperationsButton = new FakeElement('BUTTON');
const bundlePhysicalButton = new FakeElement('BUTTON');
const bundleElements = {
  topologyCanvas: bundleCanvas,
  topologyDetail: bundleDetail,
  topologyUpdated: bundleUpdated,
  topologyViewOperations: bundleOperationsButton,
  topologyViewPhysical: bundlePhysicalButton
};
const bundleDocument = {
  getElementById(id) {
    return bundleElements[id] || null;
  },
  querySelectorAll(selector) {
    return bundleCanvas.querySelectorAll(selector);
  }
};
const bundlePanel = topologyPanelModule.createTopologyPanel({
  document: bundleDocument,
  location: { protocol: 'http:', hostname: 'bigscreen.local' },
  buildTopologyLayers,
  topologyLayout,
  renderTopologySvg,
  projectPhysicalTopology: projectPhysicalTopologyReal,
  physicalTopologyLayout: physicalTopologyLayoutReal,
  renderPhysicalTopologySvg: renderPhysicalTopologySvgReal,
  topologyNodeKindLabel: (kind) => kind,
  topologyLatencyIp: (node) => node.ip || '',
  escapeHtml,
  formatPingText,
  onModeChange: () => {}
});
const bundleRows = [
  {
    edge_type: 'physical',
    from_ip: '10.0.0.1', from_sysname: 'A', from_port: 'Te1/0/2', from_ifindex: 102,
    from_aggregate_port: 'Po11', from_member_ports: ['Te1/0/2', 'Te2/0/2'],
    to_ip: '10.0.0.2', to_sysname: 'B', to_port: 'Te1/0/1', to_ifindex: 101,
    to_aggregate_port: 'Po11', to_member_ports: ['Te1/0/1', 'Te2/0/1'],
    protocols: ['lldp'], stale: true
  },
  {
    edge_type: 'physical',
    from_ip: '10.0.0.1', from_sysname: 'A', from_port: 'Te2/0/2', from_ifindex: 202,
    from_aggregate_port: 'Po11', from_member_ports: ['Te1/0/2', 'Te2/0/2'],
    to_ip: '10.0.0.2', to_sysname: 'B', to_port: 'Te2/0/1', to_ifindex: 201,
    to_aggregate_port: 'Po11', to_member_ports: ['Te1/0/1', 'Te2/0/1'],
    protocols: ['cdp'], stale: false
  }
];
bundlePanel.setMode('physical', false);
const bundleProjection = projectPhysicalTopologyReal(bundleRows);
const bundleFrame = bundlePanel.prepare([], bundleRows);
bundlePanel.render(bundleFrame);
assert.strictEqual(bundleProjection.bundles.length, 1);
assert.strictEqual(bundleFrame.layout.links.filter((link) => link.kind === 'bundle').length, 1);
assert.strictEqual((bundleCanvas.innerHTML.match(/topology-link--bundle/g) || []).length, 1);
for (const member of ['Te1/0/2', 'Te2/0/2', 'Te1/0/1', 'Te2/0/1']) {
  assert.ok(!bundleCanvas.innerHTML.includes(`>${member}<`));
}
assert.ok(!bundleCanvas.innerHTML.includes('memberPairs'));
assert.ok(!bundleCanvas.innerHTML.includes('cablePairs'));

console.log('bigscreen Topology panel tests passed');
