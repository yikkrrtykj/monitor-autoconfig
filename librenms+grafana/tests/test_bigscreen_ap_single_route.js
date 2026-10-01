const assert = require('assert');
const { appendApLeaves } = require('../bigscreen/ap-topology.js');
const { renderTopologySvg } = require('../bigscreen/topology.js');
const parent = { kind: 'dist', ip: '10.0.0.1', x: 300, y: 150, w: 144, h: 58 };
const ap = { ip: '10.1.0.1', mac: '020000000001', name: 'AP-one', online: true };
const artifact = { source: 'librenms-fdb+snmp-exact', candidate_source: 'librenms-fdb', generated_at: 10000,
  max_age_seconds: 7200, attachments: [{ ap_ip: ap.ip, ap_mac: ap.mac, switch_ip: parent.ip,
    switch_ifindex: 1, switch_port: 'Gi0/8', vlan: 200, evidence_age_seconds: 1 }] };
const build = (others = [], source = parent) => appendApLeaves({ width: 800,
  layout: { height: 680, nodes: [source, ...others], links: [] } }, { aps: [ap], artifact }, 10000);
const hits = (a, b, r) => a[0] === b[0]
  ? a[0] > r.x && a[0] < r.x + r.w && Math.max(a[1], b[1]) > r.y && Math.min(a[1], b[1]) < r.y + r.h
  : a[1] > r.y && a[1] < r.y + r.h && Math.max(a[0], b[0]) > r.x && Math.min(a[0], b[0]) < r.x + r.w;
const validate = (frame) => {
  assert.deepStrictEqual(frame.apCount, { located: 1, total: 1 });
  assert.strictEqual(frame.layout.apBuses.length, 0, 'one parent/one AP has no bus or junction');
  const link = frame.layout.links[0], points = link.apPoints;
  assert(!Object.hasOwn(link, 'apBusY'));
  assert.deepStrictEqual(points[0], [link.from.x + 72, link.from.y + 58]);
  assert.deepStrictEqual(points.at(-1), [link.to.x + 26, link.to.y]);
  assert.strictEqual(link.to.x + 26, link.from.x + 72, 'AP centered under validated parent');
  assert.strictEqual(points[1][0], points[0][0]);
  assert(points[1][1] >= points[0][1] + 16, 'downward egress at least 16px');
  points.slice(1).forEach((b, i) => {
    const a = points[i];
    assert(a[0] === b[0] || a[1] === b[1]);
    assert.notDeepStrictEqual(a, b);
    assert(b[1] >= points[0][1] + 16, 'no return above egress');
    frame.layout.nodes.filter((n) => n !== link.to).forEach((n) => assert(!hits(a, b, n), 'no re-entry or crossing unrelated bound'));
    if (i > 0) {
      const previous = points[i - 1];
      assert(!(previous[0] === a[0] && a[0] === b[0]) && !(previous[1] === a[1] && a[1] === b[1]), 'no redundant collinear vertices');
    }
  });
  const svg = renderTopologySvg(frame.layout, 800);
  assert(!svg.includes('topology-ap-bus'));
  assert(svg.includes(`d="${points.map(([x, y], i) => `${i ? 'L' : 'M'} ${x} ${y}`).join(' ')}"`), 'actual renderer draws complete direct child route');
  return points;
};
const clear = build();
assert.strictEqual(validate(clear).length, 2, 'unobstructed route is one vertical segment');
// Near viewport edge is also centered; old group-slot clamp produced a dogleg.
assert.strictEqual(validate(build([], { ...parent, x: 0 })).length, 2);
const obstacle = { kind: 'server', ip: '10.0.0.2', x: 300, y: 250, w: 144, h: 58 };
const blocked = build([obstacle]);
const points = validate(blocked);
assert.strictEqual(points.length, 5, 'one real obstacle produces only the necessary turns');
const length = points.slice(1).reduce((sum, b, i) => sum + Math.abs(b[0] - points[i][0]) + Math.abs(b[1] - points[i][1]), 0);
assert.strictEqual(length, blocked.layout.links[0].to.y - 208 + 160, 'minimum safe Manhattan route around inflated obstacle');
points.slice(1).forEach((b, i) => assert(!hits(points[i], b, { x: 292, y: 242, w: 160, h: 74 }), '8px obstacle clearance retained'));
const sealed = build([{ ...obstacle, y: 220 }]);
assert.deepStrictEqual(sealed.apCount, { located: 0, total: 1 });
assert.strictEqual(sealed.layout.links.length, 0, 'blocked initial egress fails closed');
assert.strictEqual(sealed.layout.apBuses.length, 0);
console.log('bigscreen single AP direct/minimal safe route: PASS');
