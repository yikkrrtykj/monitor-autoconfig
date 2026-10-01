// Runs in a real browser against the production CSS, DOM section, layout and
// controller. Requests are local fixture functions; no production probes.
(async function () {
  const check = (ok, message) => { if (!ok) throw new Error(message); };
  const settle = () => new Promise((resolve) => setTimeout(resolve, 25));
  const count = Number(document.body.dataset.wiredCount);
  const targets = Array.from({ length: count }, (_, i) => ({ job: i ? 'infra-dist-ping' : 'infra-core-ping',
    targetIp: `10.0.0.${i + 1}`, displayName: `switch-${i}`, success: true, latency: 0.002 }));
  const edges = targets.slice(1).map((t) => ({ from_ip: '10.0.0.1', to_ip: t.targetIp, from_port: 'Gi1/0/1', to_port: 'Gi0/1' }));
  const aps = Array.from({ length: 4 }, (_, i) => ({ ip: `10.1.0.${i + 1}`, mac: `02000000000${i + 1}`,
    name: i ? `AP-${i}` : '长名称 AP 机房设备', online: true, model: 'UAL6', clients: 4, latency: 0.0012 }));
  const artifact = { source: 'librenms-fdb+snmp-exact', candidate_source: 'librenms-fdb', generated_at: Date.now() / 1000,
    max_age_seconds: 7200, attachments: aps.map((ap, i) => ({ ap_ip: ap.ip, ap_mac: ap.mac, switch_ip: '10.0.0.2',
      switch_ifindex: i + 1, switch_port: `Gi6/0/${i + 1}`, vlan: 200, librenms_vlan_id: 142, evidence_age_seconds: 1 })) };
  const data = { aps, artifact };
  const canvas = document.getElementById('topologyCanvas');
  let panel;
  const refresh = () => panel.render(panel.prepare(targets, edges, data));
  panel = BSTopologyPanel.createTopologyPanel({ document, location, ...BSTopology,
    escapeHtml: BSUtils.escapeHtml, formatPingText: BSUtils.formatPingText,
    appendApLeaves: BSApTopology.appendApLeaves, onApVisibilityChange: refresh,
    fetchNodeInspector: async (ip) => ({ ip, kind: 'unifi-ap', name: 'Fixture AP', model: 'UAL6', online: 'up', clients: 4 }) });
  const snapshot = () => {
    const svg = canvas.querySelector('svg'), matrix = svg.getScreenCTM();
    return { viewport: svg.getAttribute('viewBox'), transform: [matrix.a, matrix.b, matrix.c, matrix.d, matrix.e, matrix.f],
      nodes: [...canvas.querySelectorAll('.topology-node:not(.topology-ap-node)')].map((n) => {
        const r = n.getBoundingClientRect(); return [n.dataset.ip, r.x, r.y, r.width, r.height];
      }), routes: [...canvas.querySelectorAll('path.topology-link')].filter((p) => !p.classList.contains('topology-ap-bus') && !p.closest('.topology-ap-link-group')).map((p) => p.getAttribute('d')) };
  };
  const same = (a, b, step) => check(JSON.stringify(a) === JSON.stringify(b), `${step}: wired projected positions/scale/anchor/routes changed ${JSON.stringify({ a, b })}`);
  const toggle = document.getElementById('topologyApToggle');
  const result = document.createElement('pre'); result.id = 'browserResult'; result.hidden = true; document.body.append(result);
  try {
    refresh(); await settle();
    const before = snapshot();
    toggle.click(); await settle(); refresh(); await settle();
    check(canvas.querySelectorAll('.topology-ap-node').length === 4, 'validated APs must be rendered');
    same(before, snapshot(), 'OFF->ON');
    canvas.querySelector('.topology-ap-node').dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    await settle();
    const detail = document.getElementById('topologyDetail');
    check(!detail.hidden && detail.textContent.includes('VLAN200') && detail.textContent.includes('上联端口Gi6/0/1'), 'Inspector retains validated VLAN/port');
    same(before, snapshot(), 'Inspector open');
    for (const n of canvas.querySelectorAll('.topology-node[data-kind="core"],.topology-node[data-kind="dist"],.topology-node[data-kind="device"]')) {
      check(!n.querySelector('.topology-node-ip,.topology-node-kind,.topology-node-latency'), 'wired card has no secondary summary');
    }
    const reference = document.createElement('div'); reference.style.cssText = 'position:absolute;opacity:0;pointer-events:none';
    const from = { kind: 'core', x: 0, y: 0, w: 144, h: 58 }, to = { kind: 'server', x: 170, y: 150, w: 144, h: 58 };
    reference.innerHTML = BSTopology.renderTopologySvg({ height: 240, nodes: [], links: [{ from, to, label: 'Gi6/0/1', severity: 'good' }] }, 400);
    document.body.append(reference);
    const apLabel = canvas.querySelector('.topology-ap-node .topology-link-label');
    const wiredLabel = reference.querySelector('.topology-link-label');
    for (const property of ['fontFamily', 'fontSize', 'fontWeight', 'fill', 'lineHeight', 'textAnchor']) {
      check(getComputedStyle(apLabel)[property] === getComputedStyle(wiredLabel)[property], `AP/wired label style differs: ${property}`);
    }
    check(apLabel.getAttribute('y') === '-8', 'AP port stays immediately above its node');
    toggle.click(); await settle(); refresh(); await settle();
    same(before, snapshot(), 'ON->OFF');
    // Existing pan/zoom must also remain anchored across the additive layer.
    canvas.dispatchEvent(new WheelEvent('wheel', { deltaY: -120, clientX: 200, clientY: 220, bubbles: true, cancelable: true }));
    const zoomed = snapshot();
    toggle.click(); await settle(); refresh(); await settle(); same(zoomed, snapshot(), 'zoomed ON');
    toggle.click(); await settle(); refresh(); await settle(); same(zoomed, snapshot(), 'zoomed OFF');
    result.textContent = JSON.stringify({ pass: true, wiredNodes: before.nodes.length, apNodes: 4, scale: before.transform[0] });
  } catch (error) {
    result.textContent = JSON.stringify({ pass: false, error: error.message });
  }
}());
