;(function () {
  'use strict';
  const MAX_APS = 512;
  function mac(value) {
    const normalized = String(value || '').toLowerCase().replace(/[:.\-]/g, '');
    return /^[0-9a-f]{12}$/.test(normalized) && normalized !== '000000000000' && normalized !== 'ffffffffffff' ? normalized : '';
  }
  function ipv4(value) {
    return typeof value === 'string' && /^(\d{1,3}\.){3}\d{1,3}$/.test(value) && value.split('.').every((v) => Number(v) <= 255 && String(Number(v)) === v);
  }
  function identities(rows) {
    if (!Array.isArray(rows) || rows.length > MAX_APS) return [];
    const parsed = rows.map((ap) => ap && typeof ap === 'object' ? ({ ...ap, mac: mac(ap.mac) }) : {});
    return parsed.filter((ap) => ipv4(ap.ip) && ap.mac && parsed.filter((other) => other.ip === ap.ip).length === 1
      && parsed.filter((other) => other.mac === ap.mac).length === 1);
  }
  const intersects = (a, b) => a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;
  function compactPath(points) {
    const result = [];
    points.forEach((point) => {
      const last = result[result.length - 1];
      if (last && point[0] === last[0] && point[1] === last[1]) return;
      const previous = result[result.length - 2];
      if (previous && ((previous[0] === last[0] && last[0] === point[0] && (last[1] - previous[1]) * (point[1] - last[1]) >= 0)
        || (previous[1] === last[1] && last[1] === point[1] && (last[0] - previous[0]) * (point[0] - last[0]) >= 0))) result.pop();
      result.push(point);
    });
    return result;
  }
  // Rectilinear visibility grid: only adjacent coordinates are searched. All
  // obstacles include labels/metadata, not just the device body.
  function route(start, end, obstacles, width) {
    const source = start;
    // Leave the parent's bottom vertically before any general route. The
    // remaining route cannot backtrack above this egress, even in a maze.
    const minY = source[1] + 16;
    start = [source[0], minY];
    const boxes = obstacles.map((r) => ({ x: r.x - 8, y: r.y - 8, w: r.w + 16, h: r.h + 16 }));
    const xs = [...new Set([0, width, start[0], end[0], ...boxes.flatMap((r) => [r.x, r.x + r.w])])].filter((x) => x >= 0 && x <= width).sort((a, b) => a - b);
    const ys = [...new Set([start[1], end[1], ...boxes.flatMap((r) => [r.y, r.y + r.h])])].filter((y) => y >= minY).sort((a, b) => a - b);
    const clear = (a, b) => !boxes.some((r) => a[0] === b[0]
      ? a[0] > r.x && a[0] < r.x + r.w && Math.max(a[1], b[1]) > r.y && Math.min(a[1], b[1]) < r.y + r.h
      : a[1] > r.y && a[1] < r.y + r.h && Math.max(a[0], b[0]) > r.x && Math.min(a[0], b[0]) < r.x + r.w);
    if (end[1] < minY || !clear(source, start)) return null;
    // Most local branches need one bend; avoid a full grid for large AP sets.
    const simple = [[start, [start[0], end[1]], end], [start, [end[0], start[1]], end]];
    for (const points of simple) if (points.slice(1).every((point, i) => clear(points[i], point))) return [source, ...points];
    const key = (x, y) => y * xs.length + x;
    const first = key(xs.indexOf(start[0]), ys.indexOf(start[1]));
    const last = key(xs.indexOf(end[0]), ys.indexOf(end[1]));
    const previous = new Map([[first, null]]);
    const queue = [first];
    for (let cursor = 0; cursor < queue.length; cursor++) {
      const current = queue[cursor];
      if (current === last) break;
      const x = current % xs.length, y = Math.floor(current / xs.length);
      for (const [nx, ny] of [[x, y + 1], [x - 1, y], [x + 1, y], [x, y - 1]]) {
        if (nx < 0 || ny < 0 || nx >= xs.length || ny >= ys.length) continue;
        const next = key(nx, ny);
        if (!previous.has(next) && clear([xs[x], ys[y]], [xs[nx], ys[ny]])) {
          previous.set(next, current); queue.push(next);
        }
      }
    }
    if (!previous.has(last)) return null;
    const points = [];
    for (let k = last; k !== null; k = previous.get(k)) points.unshift([xs[k % xs.length], ys[Math.floor(k / xs.length)]]);
    return [source, ...points];
  }
  function appendApLeaves(frame, data, now = Date.now() / 1000) {
    const aps = identities(data && data.aps);
    const artifact = data && data.artifact;
    const result = { ...frame, layout: { ...frame.layout, wiredHeight: frame.layout.height, nodes: [...frame.layout.nodes], links: [...frame.layout.links], apBuses: [] }, apCount: { located: 0, total: aps.length } };
    if (!aps.length || !artifact || artifact.source !== 'librenms-fdb+snmp-exact' || artifact.candidate_source !== 'librenms-fdb' || !Array.isArray(artifact.attachments)
      || artifact.attachments.length > MAX_APS || !Number.isFinite(artifact.generated_at)
      || !Number.isFinite(artifact.max_age_seconds) || artifact.max_age_seconds < 0) return result;
    const elapsed = now - artifact.generated_at;
    // A stopped collector must not keep AP edges alive indefinitely.
    if (elapsed < 0 || elapsed > artifact.max_age_seconds) return result;
    const resolved = [];
    aps.forEach((ap) => {
      const matches = artifact.attachments.filter((row) => row && typeof row === 'object' && row.ap_ip === ap.ip && mac(row.ap_mac) === ap.mac);
      if (matches.length !== 1) return;
      const row = matches[0];
      if (!Number.isFinite(row.evidence_age_seconds) || row.evidence_age_seconds < 0
        || row.evidence_age_seconds + elapsed > artifact.max_age_seconds || !Number.isInteger(row.switch_ifindex) || row.switch_ifindex <= 0) return;
      const parents = frame.layout.nodes.filter((node) => node.ip === row.switch_ip && node.kind === 'dist');
      if (parents.length === 1) resolved.push({ ap, row, parent: parents[0] });
    });
    // Ordering affects positions only, never attachment authority.
    resolved.sort((a, b) => a.parent.x - b.parent.x || a.parent.ip.localeCompare(b.parent.ip) || a.ap.ip.localeCompare(b.ap.ip));
    const groups = new Map();
    resolved.forEach((item) => {
      if (!groups.has(item.parent.ip)) groups.set(item.parent.ip, []);
      groups.get(item.parent.ip).push(item);
    });
    const reserved = [...frame.layout.nodes];
    const orderedGroups = [...groups.values()];
    orderedGroups.forEach((items, groupIndex) => {
      const parent = items[0].parent;
      const center = parent.x + parent.w / 2;
      const previousParent = orderedGroups[groupIndex - 1]?.[0].parent;
      const nextParent = orderedGroups[groupIndex + 1]?.[0].parent;
      const left = previousParent ? (previousParent.x + previousParent.w / 2 + center) / 2 : 20;
      const right = nextParent ? (nextParent.x + nextParent.w / 2 + center) / 2 : frame.width - 20;
      const cols = Math.min(3, items.length, Math.max(1, Math.floor((right - left - 32) / 160)));
      const groupWidth = cols * 160;
      const x = Math.max(20, Math.min(frame.width - groupWidth - 20, center - groupWidth / 2));
      const box = { x, y: parent.y + parent.h + 64, w: groupWidth, h: Math.ceil(items.length / cols) * 144 };
      // A group's complete label/body/metadata area is reserved before routing.
      while (reserved.some((r) => intersects(box, { x: r.x - 16, y: r.y - 16, w: r.w + 32, h: r.h + 32 }))) box.y += 32;
      const nodes = items.map(({ ap, row }, idx) => {
        const node = { kind: 'ap', ip: ap.ip, name: ap.name || ap.ip, model: ap.model, clients: ap.clients,
          parentIp: row.switch_ip, parentIfindex: row.switch_ifindex, switchPort: row.switch_port,
          vlan: Number.isInteger(row.vlan) && row.vlan >= 1 && row.vlan <= 4094 ? row.vlan : null,
          latency: Number.isFinite(ap.latency) && ap.latency >= 0 ? ap.latency : null,
          success: ap.online, level: ap.online === true ? 'good' : ap.online === false ? 'bad' : 'none',
          x: x + (idx % cols) * 160 + 54, y: box.y + Math.floor(idx / cols) * 144 + 28, w: 52, h: 52 };
        return node;
      });
      // Each row fans out from a local bus. Route around all unrelated cards.
      const buses = [];
      const links = [];
      for (let i = 0; i < nodes.length; i += cols) {
        const children = nodes.slice(i, i + cols);
        const busY = children[0].y - 28;
        const childXs = children.map((node) => node.x + node.w / 2);
        const junctionX = Math.max(childXs[0], Math.min(childXs[childXs.length - 1], center));
        const obstacles = reserved.filter((r) => r !== parent);
        // The route must also avoid this group's own circles and metadata.
        nodes.forEach((n) => obstacles.push({ x: n.x - 46, y: n.y - 20, w: 144, h: 128 }));
        const points = route([center, parent.y + parent.h], [junctionX, busY], obstacles, frame.width);
        if (!points) return; // Never draw a guessed diagonal through a card.
        // Trunk ends inside the participating span, never at an empty slot.
        // A partial row has only its real children, and a single AP has no bus.
        const bus = { points: compactPath(points), severity: 'good', y: busY,
          childXs, junctionX, childRoutes: children.map((node) => ({ apIp: node.ip,
            points: compactPath([...points, [node.x + node.w / 2, busY], [node.x + node.w / 2, node.y]]) })) };
        buses.push(bus);
        children.forEach((node) => links.push({ from: parent, to: node, severity: node.level, apLink: true, apBusY: busY }));
      }
      result.layout.apBuses.push(...buses);
      result.layout.links.push(...links);
      result.layout.nodes.push(...nodes);
      reserved.push(box);
      buses.forEach((bus) => [...bus.points.slice(1).map((point, idx) => [bus.points[idx], point]),
        [[bus.childXs[0], bus.y], [bus.childXs[bus.childXs.length - 1], bus.y]]].forEach(([previous, point]) => {
        reserved.push({ x: Math.min(point[0], previous[0]), y: Math.min(point[1], previous[1]),
          w: Math.max(1, Math.abs(point[0] - previous[0])), h: Math.max(1, Math.abs(point[1] - previous[1])) });
      }));
      result.layout.height = Math.max(result.layout.height, box.y + box.h + 24);
      result.apCount.located += nodes.length;
    });
    return result;
  }
  const ns = { appendApLeaves };
  if (typeof module !== 'undefined' && module.exports) module.exports = ns;
  else window.BSApTopology = ns;
}());
