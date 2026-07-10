;(function () {
  'use strict';

  // Topology presentation layer: pure data->layout->SVG functions. No DOM
  // access, no fetching -- app.js feeds in targets/edges (from BSApi) and
  // injects the returned SVG markup. Kept side-effect free so the whole
  // pipeline is unit-testable in Node.
  const isNode = (typeof module !== 'undefined' && module.exports);
  const utils = isNode ? require('./utils.js') : window.BSUtils;
  const api = isNode ? require('./api.js') : window.BSApi;
  const {
    escapeHtml, formatPingText, uniqueNames, parseIspIps, parseConfiguredTargetIps,
    compactPortLabel, isPortLikeLabel, isAggPortName
  } = utils;
  const { getConfiguredIspNames, getIspNames, isIspAutoDiscoveryEnabled } = api;

  const config = (typeof window !== 'undefined' && window.BIGSCREEN_CONFIG) || {};

  function topologyNodeLevel(node) {
    if (!node) return "none";
    if (!node.success) return "bad";
    if (Number.isFinite(node.latency) && node.latency >= 0.03) return "warn";
    return "good";
  }

  function buildTopologyLayers(targets) {
    // 自动发现时只用显式配置的名字（通常为空），不要回退 ISP1,ISP2 默认，
    // 否则拓扑会多出两个永远连不通的 ISP1/ISP2 占位节点。
    const ispNames = isIspAutoDiscoveryEnabled() ? getConfiguredIspNames() : getIspNames();
    const ispIpMap = parseIspIps(config.ispIps);
    const ispTargets = targets.filter((t) => t.job === "infra-isp-ping");
    const usedIspTargets = new Set();
    const configuredIspNames = new Set(ispNames.map((name) => String(name || "").toLowerCase()));
    const targetKey = (target) => `${target.job}|${target.targetIp || target.instance || target.displayName}`;
    const findIspTarget = (name, ip) => {
      const lowerName = String(name || "").toLowerCase();
      if (ip) {
        return ispTargets.find((target) => {
          if (usedIspTargets.has(targetKey(target))) return false;
          return target.targetIp === ip;
        });
      }
      return ispTargets.find((target) => {
        if (usedIspTargets.has(targetKey(target))) return false;
        return String(target.displayName || "").toLowerCase() === lowerName ||
          String(target.instance || "").toLowerCase() === lowerName;
      });
    };
    const isps = ispNames.map((name) => {
      const configuredIp = ispIpMap[name] || "";
      const target = findIspTarget(name, configuredIp);
      if (target) {
        usedIspTargets.add(targetKey(target));
        return {
          kind: "isp",
          name,
          ip: target.targetIp || configuredIp,
          level: topologyNodeLevel(target),
          latency: target.latency,
          success: target.success
        };
      }
      return {
        kind: "isp",
        name,
        ip: configuredIp,
        level: "none"
      };
    });
    ispTargets.forEach((target) => {
      if (!isIspAutoDiscoveryEnabled()) return;
      if (usedIspTargets.has(targetKey(target))) return;
      if (configuredIspNames.has(String(target.displayName || target.instance || "").toLowerCase())) return;
      usedIspTargets.add(targetKey(target));
      isps.push({
        kind: "isp",
        name: target.displayName,
        ip: target.targetIp,
        level: topologyNodeLevel(target),
        latency: target.latency,
        success: target.success
      });
    });

    const infrastructureIps = new Set();
    const firewalls = targets.filter((t) => t.job === "infra-fw-ping").map((t) => ({
      kind: "firewall",
      name: t.displayName,
      ip: t.targetIp,
      level: topologyNodeLevel(t),
      latency: t.latency,
      success: t.success
    }));
    firewalls.forEach((node) => { if (node.ip) infrastructureIps.add(node.ip); });

    const cores = targets.filter((t) => t.job === "infra-core-ping").map((t) => ({
      kind: "core",
      name: t.displayName,
      ip: t.targetIp,
      level: topologyNodeLevel(t),
      latency: t.latency,
      success: t.success
    }));
    cores.forEach((node) => { if (node.ip) infrastructureIps.add(node.ip); });

    const dists = targets.filter((t) => t.job === "infra-dist-ping").map((t) => ({
      kind: "dist",
      name: t.displayName,
      ip: t.targetIp,
      level: topologyNodeLevel(t),
      latency: t.latency,
      success: t.success
    }));
    dists.forEach((node) => { if (node.ip) infrastructureIps.add(node.ip); });

    const configuredServerIps = parseConfiguredTargetIps(config.serverTargets);
    const serversByName = new Map();
    targets
      .filter((t) => t.job === "infra-srv-ping")
      .filter((t) => !configuredServerIps.size || configuredServerIps.has(t.targetIp))
      .filter((t) => !infrastructureIps.has(t.targetIp))
      .forEach((t) => {
        const key = String(t.displayName || t.targetIp || "").toLowerCase();
        if (!serversByName.has(key)) serversByName.set(key, t);
      });
    const servers = Array.from(serversByName.values()).map((t) => ({
        kind: "server",
        name: t.displayName,
        ip: t.targetIp,
        level: topologyNodeLevel(t),
        latency: t.latency,
        success: t.success
      }));

    return { isps, firewalls, cores, dists, servers };
  }

  function topologyLayout(layers, canvasWidth, canvasHeight, lldpEdges) {
    const NODE_W = 128;
    const NODE_H = 58;
    const topPad = 22;
    const bottomPad = 22;
    const rowCount = 4;
    const DIST_LINK_GAP = 42;
    const hasServers = !!(layers.servers && layers.servers.length);
    const usableHeight = Math.max(420, canvasHeight || 680) + (hasServers ? 96 : 0);
    const layerGap = Math.max(36, (usableHeight - topPad - bottomPad - NODE_H * rowCount) / (rowCount - 1));
    const rowY = (idx) => topPad + idx * (NODE_H + layerGap);
    // Servers sit on their own evenly-spaced row in the gap between the core and the
    // access-switch (dist) row, so they never clamp/stack like the old flanking layout.
    const serverRowY = rowY(2) + NODE_H + Math.max(20, (layerGap - NODE_H) / 2);

    const placeRow = (items, y) => {
      const total = items.length;
      if (!total) return [];
      const totalWidth = total * NODE_W + (total - 1) * 24;
      const startX = Math.max(20, (canvasWidth - totalWidth) / 2);
      return items.map((item, idx) => ({
        ...item,
        x: startX + idx * (NODE_W + 24),
        y,
        w: NODE_W,
        h: NODE_H
      }));
    };

    const ispRow = placeRow(layers.isps, rowY(0));
    const fwRow = placeRow(layers.firewalls, rowY(1));
    const coreRow = placeRow(layers.cores, rowY(2));
    const placeServerRow = (items, y) => {
      if (!items.length || !coreRow.length) return [];
      const primaryCore = coreRow[Math.floor(coreRow.length / 2)];
      const leftCount = Math.floor(items.length / 2);
      const leftItems = items.slice(0, leftCount);
      const rightItems = items.slice(leftCount);
      const gapFromCore = 34;
      const itemGap = 24;
      const nodes = [];
      const leftStart = primaryCore.x - gapFromCore - leftItems.length * NODE_W - Math.max(0, leftItems.length - 1) * itemGap;
      leftItems.forEach((item, idx) => {
        nodes.push({ ...item, x: leftStart + idx * (NODE_W + itemGap), y, w: NODE_W, h: NODE_H });
      });
      const rightStart = primaryCore.x + primaryCore.w + gapFromCore;
      rightItems.forEach((item, idx) => {
        nodes.push({ ...item, x: rightStart + idx * (NODE_W + itemGap), y, w: NODE_W, h: NODE_H });
      });
      const minX = Math.min(...nodes.map((n) => n.x));
      const maxX = Math.max(...nodes.map((n) => n.x + n.w));
      const shift = minX < 20 ? 20 - minX : (maxX > canvasWidth - 20 ? canvasWidth - 20 - maxX : 0);
      return nodes.map((node) => ({ ...node, x: node.x + shift }));
    };
    const serverRow = (hasServers && coreRow.length)
      ? placeServerRow(layers.servers, serverRowY)
      : [];
    // Build the access-switch (dist) layer as a tree from the discovered edges:
    // switches that uplink to the core sit in the main row; a switch whose uplink
    // lands on ANOTHER access switch is drawn in a layer below its parent
    // (e.g. core -> FOH -> JIESHOU-RIGHT -> JIESHOU-LEFT).
    const placeDistTree = () => {
      const dists = layers.dists;
      if (!dists.length) return { nodes: [], depthByIp: new Map() };
      const distByIp = new Map();
      dists.forEach((d) => { if (d.ip) distByIp.set(d.ip, d); });
      const coreIps = new Set(coreRow.map((c) => c.ip).filter(Boolean));
      const adj = new Map();
      const addAdj = (a, b) => {
        if (!adj.has(a)) adj.set(a, new Set());
        adj.get(a).add(b);
      };
      const inGraph = (ip) => coreIps.has(ip) || distByIp.has(ip);
      (Array.isArray(lldpEdges) ? lldpEdges : []).forEach((edge) => {
        if (edge.from_ip && edge.to_ip && inGraph(edge.from_ip) && inGraph(edge.to_ip)) {
          addAdj(edge.from_ip, edge.to_ip);
          addAdj(edge.to_ip, edge.from_ip);
        }
      });
      // BFS from the core: depth + parent for every reachable switch.
      const depthByIp = new Map();
      const parentByIp = new Map();
      const queue = [];
      coreIps.forEach((ip) => { depthByIp.set(ip, 0); queue.push(ip); });
      while (queue.length) {
        const ip = queue.shift();
        (adj.get(ip) || []).forEach((nb) => {
          if (!depthByIp.has(nb)) {
            depthByIp.set(nb, depthByIp.get(ip) + 1);
            parentByIp.set(nb, ip);
            queue.push(nb);
          }
        });
      }
      const childrenOf = new Map();
      dists.forEach((d) => {
        const p = parentByIp.get(d.ip);
        if (p && distByIp.has(p)) {
          if (!childrenOf.has(p)) childrenOf.set(p, []);
          childrenOf.get(p).push(d.ip);
        }
      });
      // Top of the tree = directly under the core (depth 1) or never discovered.
      const topLevel = dists.filter((d) => !depthByIp.has(d.ip) || depthByIp.get(d.ip) <= 1);
      const placed = new Map();
      const baseRow = placeRow(topLevel, rowY(3));
      baseRow.forEach((n) => { if (n.ip) placed.set(n.ip, n); });
      const childRowH = NODE_H + DIST_LINK_GAP;
      const placeChildren = (parentNode) => {
        const kids = (childrenOf.get(parentNode.ip) || [])
          .map((ip) => distByIp.get(ip))
          .filter((kid) => kid && kid.ip && !placed.has(kid.ip));
        const count = kids.length;
        kids.forEach((kid, idx) => {
          const x = Math.max(20, parentNode.x + (idx - (count - 1) / 2) * (NODE_W + 16));
          const node = { ...kid, x, y: parentNode.y + childRowH, w: NODE_W, h: NODE_H };
          placed.set(kid.ip, node);
          placeChildren(node);
        });
      };
      baseRow.forEach((n) => placeChildren(n));
      // Safety net: anything not reached above still gets a slot in the main row.
      dists.forEach((d, idx) => {
        if (d.ip && !placed.has(d.ip)) {
          placed.set(d.ip, { ...d, x: Math.max(20, 20 + idx * (NODE_W + 16)), y: rowY(3), w: NODE_W, h: NODE_H });
        }
      });
      return {
        nodes: dists.map((d) => (d.ip ? placed.get(d.ip) : null)).filter(Boolean),
        depthByIp,
      };
    };
    const distTree = placeDistTree();
    const distRow = distTree.nodes;
    const distDepthByIp = distTree.depthByIp;

    const allNodes = [...ispRow, ...fwRow, ...coreRow, ...distRow, ...serverRow];
    const nodeByIp = new Map();
    const nodePriority = { isp: 1, firewall: 2, server: 3, dist: 4, core: 5 };
    allNodes.forEach((n) => {
      if (!n.ip) return;
      const existing = nodeByIp.get(n.ip);
      if (!existing || (nodePriority[n.kind] || 0) > (nodePriority[existing.kind] || 0)) {
        nodeByIp.set(n.ip, n);
      }
    });

    const pairKeyFor = (a, b) => [a.ip || a.name, b.ip || b.name].sort().join("|");
    const cleanPortNames = (ports) => uniqueNames(ports.map(compactPortLabel)).filter((port) => port && isPortLikeLabel(port));
    const selectDisplayPorts = (ports, maxPhysical = Infinity) => {
      const unique = cleanPortNames(ports);
      const physical = unique.filter((port) => !isAggPortName(port));
      const aggregate = unique.filter(isAggPortName);
      const selected = physical.length ? physical.slice(0, maxPhysical) : aggregate.slice(0, Math.max(1, maxPhysical));
      return selected.length ? selected : unique.slice(0, Math.max(1, maxPhysical));
    };
    const portDetail = (fromPorts, toPorts, maxPhysical = Infinity) => {
      const fromSelected = selectDisplayPorts(fromPorts, maxPhysical);
      const toSelected = selectDisplayPorts(toPorts, maxPhysical);
      const lines = [];
      if (fromSelected.length) lines.push(fromSelected.join(", "));
      if (toSelected.length) lines.push(toSelected.join(", "));
      const aggregated = Math.max(fromSelected.length, toSelected.length) > 1 ||
        [...fromPorts, ...toPorts].map(compactPortLabel).some(isAggPortName);
      return { lines, aggregated };
    };

    const lldpLinks = [];
    const lldpCoveredPairs = new Set();
    if (Array.isArray(lldpEdges) && lldpEdges.length) {
      const groupedEdges = new Map();
      lldpEdges.forEach((edge) => {
        const fromNode = nodeByIp.get(edge.from_ip);
        const toNode = nodeByIp.get(edge.to_ip);
        if (!fromNode || !toNode) return;
        const orientFrom = fromNode.y <= toNode.y ? fromNode : toNode;
        const orientTo = fromNode.y <= toNode.y ? toNode : fromNode;
        const orientFromPort = orientFrom === fromNode ? edge.from_port : edge.to_port;
        const orientToPort = orientFrom === fromNode ? edge.to_port : edge.from_port;
        const pairKey = pairKeyFor(orientFrom, orientTo);
        const group = groupedEdges.get(pairKey) || {
          from: orientFrom,
          to: orientTo,
          fromPorts: [],
          toPorts: [],
          count: 0
        };
        group.fromPorts.push(orientFromPort);
        group.toPorts.push(orientToPort);
        group.count += 1;
        groupedEdges.set(pairKey, group);
      });
      groupedEdges.forEach((group, pairKey) => {
        const isCoreDist = (
          (group.from.kind === "core" && group.to.kind === "dist") ||
          (group.from.kind === "dist" && group.to.kind === "core")
        );
        const detail = portDetail(group.fromPorts, group.toPorts, isCoreDist ? 2 : Infinity);
        lldpLinks.push({
          from: group.from,
          to: group.to,
          labelLines: detail.lines,
          severity: group.to.level || "good",
          logical: true,
          aggregated: detail.aggregated
        });
        lldpCoveredPairs.add(pairKey);
      });
    }

    const links = [];
    const pushCrossLink = (from, to, severity) => {
      const pairKey = pairKeyFor(from, to);
      if (lldpCoveredPairs.has(pairKey)) return;
      links.push({ from, to, severity, fallback: true });
    };
    fwRow.forEach((fw) => ispRow.forEach((isp) => pushCrossLink(isp, fw, fw.level)));
    coreRow.forEach((core) => fwRow.forEach((fw) => pushCrossLink(fw, core, core.level)));
    distRow.forEach((d) => {
      // Skip the synthetic core->switch link when a real uplink was discovered
      // (it's drawn via the core bus or via its parent switch instead).
      if (distDepthByIp.has(d.ip)) return;
      coreRow.forEach((core) => pushCrossLink(core, d, d.level));
    });
    serverRow.forEach((s) => coreRow.forEach((core) => pushCrossLink(core, s, s.level)));
    links.push(...lldpLinks);

    const isCoreDistLink = (link) => (
      (link.from.kind === "core" && link.to.kind === "dist") ||
      (link.from.kind === "dist" && link.to.kind === "core")
    );
    const coreDistLinks = links.filter(isCoreDistLink);
    let coreBus = null;
    if (coreDistLinks.length && coreRow.length && distRow.length) {
      const primaryCore = coreRow[Math.floor(coreRow.length / 2)];
      const coreX = primaryCore.x + primaryCore.w / 2;
      const coreY = primaryCore.y + primaryCore.h;
      // Size the backbone from switches connected directly to the core only.
      // Child switches live in lower rows and must not make the horizontal bus
      // protrude beyond its first/last real downlink.
      const distCenters = coreDistLinks.map((link) => {
        const node = link.from.kind === "dist" ? link.from : link.to;
        return node.x + node.w / 2;
      });
      const busY = rowY(3) - DIST_LINK_GAP;
      coreBus = {
        x1: Math.min(coreX, ...distCenters),
        x2: Math.max(coreX, ...distCenters),
        y: busY,
        coreX,
        coreY,
        severity: primaryCore.level || "good"
      };
      coreDistLinks.forEach((link) => {
        link.busLink = true;
      });
    }

    const nodeKey = (node) => node.ip || `${node.kind}|${node.name}`;
    const assignSlots = (side) => {
      const groups = new Map();
      links.forEach((link) => {
        const key = nodeKey(link[side]);
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(link);
      });
      groups.forEach((group) => {
        group.forEach((link, idx) => {
          link[`${side}Slot`] = idx;
          link[`${side}SlotCount`] = group.length;
        });
      });
    };
    assignSlots("from");
    assignSlots("to");

    const haBonds = [];
    if (fwRow.length === 2) {
      haBonds.push({ from: fwRow[0], to: fwRow[1] });
    }

    return {
      nodes: allNodes,
      links,
      haBonds,
      coreBus,
      height: Math.max(usableHeight, allNodes.reduce((m, n) => Math.max(m, n.y + (n.h || 0)), 0) + bottomPad)
    };
  }

  function topologyNodeIcon(kind) {
    return { isp: "🌐", firewall: "🛡", core: "★", dist: "▦", server: "⚙" }[kind] || "?";
  }

  function topologyNodeKindLabel(kind) {
    return { isp: "ISP", firewall: "防火墙", core: "核心", dist: "接入", server: "服务器" }[kind] || kind;
  }

  function renderTopologySvg(layout, canvasWidth) {
    const nodeCenterX = (node) => node.x + node.w / 2;
    const nodeCenterY = (node) => node.y + node.h / 2;
    const anchorX = (node, slot, count) => {
      if (!Number.isFinite(slot) || !Number.isFinite(count) || count <= 1) {
        return node.x + node.w / 2;
      }
      const pad = 18;
      return node.x + pad + ((node.w - pad * 2) * slot) / (count - 1);
    };
    // Estimate rendered title width (CJK glyphs are ~2x a Latin char) so an
    // over-long switch name gets squeezed to fit the box instead of spilling out.
    const estTextWidth = (text) => {
      let w = 0;
      for (const ch of String(text || "")) {
        w += /[　-鿿＀-￯]/.test(ch) ? 13 : 7.3;
      }
      return w;
    };

    const coreBus = layout.coreBus
      ? `<path class="topology-link topology-backbone link-${layout.coreBus.severity}" d="M ${layout.coreBus.coreX} ${layout.coreBus.coreY} L ${layout.coreBus.coreX} ${layout.coreBus.y} M ${layout.coreBus.x1} ${layout.coreBus.y} L ${layout.coreBus.x2} ${layout.coreBus.y}" />`
      : "";

    const linkPaths = layout.links.map((link) => {
      let labelX;
      let labelY;
      let labelAnchor = "middle";
      let labelPositions = null;
      let d;
      if (link.busLink && layout.coreBus) {
        const distNode = link.from.kind === "dist" ? link.from : link.to;
        const x = nodeCenterX(distNode);
        d = `M ${x} ${layout.coreBus.y} L ${x} ${distNode.y}`;
        labelX = x + 14;
        labelY = Math.max(layout.coreBus.y + 12, distNode.y - 34);
        labelAnchor = "start";
        if (Array.isArray(link.labelLines) && link.labelLines.length > 1) {
          labelPositions = [
            { text: link.labelLines[0], x: x + 14, y: layout.coreBus.y - 8, anchor: "start" },
            { text: link.labelLines[1], x: x + 14, y: distNode.y - 5, anchor: "start" }
          ];
        }
      } else if (Math.abs(link.from.y - link.to.y) < 4) {
        const left = link.from.x <= link.to.x ? link.from : link.to;
        const right = left === link.from ? link.to : link.from;
        const x1 = left.x + left.w;
        const x2 = right.x;
        const y = nodeCenterY(left);
        d = `M ${x1} ${y} L ${x2} ${y}`;
        labelX = (x1 + x2) / 2;
        labelY = y - 7;
      } else if (
        (link.from.kind === "core" && link.to.kind === "server") ||
        (link.from.kind === "server" && link.to.kind === "core")
      ) {
        const coreNode = link.from.kind === "core" ? link.from : link.to;
        const serverNode = link.from.kind === "server" ? link.from : link.to;
        const side = nodeCenterX(serverNode) < nodeCenterX(coreNode) ? -1 : 1;
        const x1 = nodeCenterX(coreNode) + side * Math.min(42, coreNode.w * 0.34);
        const y1 = coreNode.y + coreNode.h;
        const x2 = nodeCenterX(serverNode);
        const y2 = serverNode.y;
        const bendY = y1 + Math.max(18, (y2 - y1) * 0.42);
        d = `M ${x1} ${y1} C ${x1} ${bendY} ${x2} ${bendY} ${x2} ${y2}`;
        labelX = (x1 + x2) / 2;
        labelY = bendY - 5;
      } else if (Math.abs(nodeCenterX(link.from) - nodeCenterX(link.to)) < 14) {
        const x = nodeCenterX(link.from);
        const y1 = link.from.y + link.from.h;
        const y2 = link.to.y;
        d = `M ${x} ${y1} L ${x} ${y2}`;
        labelX = x + 14;
        labelY = (y1 + y2) / 2;
        labelAnchor = "start";
        if (Array.isArray(link.labelLines) && link.labelLines.length > 1) {
          labelPositions = [
            { text: link.labelLines[0], x: x + 14, y: y1 + 13, anchor: "start" },
            { text: link.labelLines[1], x: x + 14, y: y2 - 5, anchor: "start" }
          ];
        }
      } else {
        const x1 = anchorX(link.from, link.fromSlot, link.fromSlotCount);
        const y1 = link.from.y + link.from.h;
        const x2 = anchorX(link.to, link.toSlot, link.toSlotCount);
        const y2 = link.to.y;
        const midY = (y1 + y2) / 2;
        d = `M ${x1} ${y1} C ${x1} ${midY} ${x2} ${midY} ${x2} ${y2}`;
        labelX = (x1 + x2) / 2;
        labelY = midY - 5;
        if (Array.isArray(link.labelLines) && link.labelLines.length > 1) {
          // Keep the two endpoint ports visually attached to their own boxes.
          // A shared two-line label in the middle makes parent/child switch
          // ports look concatenated, especially when the link is diagonal.
          labelPositions = [
            { text: link.labelLines[0], x: x1, y: y1 + 13, anchor: "middle" },
            { text: link.labelLines[1], x: x2, y: y2 - 5, anchor: "middle" }
          ];
        }
      }
      const labelLines = Array.isArray(link.labelLines) && link.labelLines.length
        ? link.labelLines
        : (link.label ? [link.label] : []);
      const positionedLabels = labelPositions
        ? labelPositions.filter((item) => item.text).map((item) => `<text class="topology-link-label topology-link-label-stack" x="${item.x}" y="${item.y}" text-anchor="${item.anchor}">${escapeHtml(item.text)}</text>`).join("")
        : "";
      const linkLabel = positionedLabels || (labelLines.length
        ? `<text class="topology-link-label${labelLines.length > 1 ? " topology-link-label-stack" : ""}" x="${labelX}" y="${labelY}" text-anchor="${labelAnchor}">${labelLines.map((line, idx) => `<tspan x="${labelX}" dy="${idx ? 12 : 0}">${escapeHtml(line)}</tspan>`).join("")}</text>`
        : "");
      const linkClass = `topology-link link-${link.severity} ${link.logical ? "link-logical" : "link-fallback"}${link.aggregated ? " link-aggregated" : ""}`;
      return `
        <g class="topology-link-group">
          <path class="${linkClass}" d="${d}" />
          ${linkLabel}
        </g>
      `;
    }).join("");

    const nodes = layout.nodes.map((node, idx) => {
      const latencyText = Number.isFinite(node.latency)
        ? formatPingText(node.latency)
        : (node.kind === "isp" && node.success === true ? "在线" : "");
      const dataAttrs = `data-idx="${idx}" data-kind="${escapeHtml(node.kind)}" data-name="${escapeHtml(node.name)}" data-ip="${escapeHtml(node.ip || "")}" data-level="${escapeHtml(node.level)}"`;
      const subline = node.ip
        ? `<text class="topology-node-ip" x="14" y="${node.h - 8}">${escapeHtml(node.ip)}</text>`
        : "";
      const nodeName = String(node.name || "?");
      const nameMaxW = node.w - 42; // title starts at x=34, leave ~8px right padding
      const nameFitAttr = estTextWidth(nodeName) > nameMaxW
        ? ` textLength="${nameMaxW}" lengthAdjust="spacingAndGlyphs"`
        : "";
      return `
        <g class="topology-node node-${node.level}" transform="translate(${node.x},${node.y})" ${dataAttrs} role="button" tabindex="0">
          <rect width="${node.w}" height="${node.h}" rx="10" />
          <text class="topology-node-icon" x="14" y="22">${topologyNodeIcon(node.kind)}</text>
          <text class="topology-node-name" x="34" y="22"${nameFitAttr}>${escapeHtml(nodeName)}</text>
          <text class="topology-node-kind" x="34" y="38">${escapeHtml(topologyNodeKindLabel(node.kind))}</text>
          <text class="topology-node-latency" x="${node.w - 10}" y="38" text-anchor="end">${escapeHtml(latencyText)}</text>
          ${subline}
        </g>
      `;
    }).join("");

    const haBonds = (layout.haBonds || []).map((bond) => {
      const x1 = bond.from.x + bond.from.w;
      const x2 = bond.to.x;
      const y = bond.from.y + bond.from.h / 2;
      const midX = (x1 + x2) / 2;
      return `
        <g class="topology-ha-bond">
          <line x1="${x1}" y1="${y}" x2="${x2}" y2="${y}" />
          <rect x="${midX - 14}" y="${y - 8}" width="28" height="16" rx="4" />
          <text x="${midX}" y="${y + 4}" text-anchor="middle">HA</text>
        </g>
      `;
    }).join("");

    return `
      <svg class="topology-svg" viewBox="0 0 ${canvasWidth} ${layout.height}" data-base-width="${canvasWidth}" data-base-height="${layout.height}" preserveAspectRatio="xMidYMid meet" focusable="false">
        <defs>
          <filter id="topology-glow" x="-20%" y="-20%" width="140%" height="140%">
            <feGaussianBlur stdDeviation="2.5" result="blur" />
            <feMerge><feMergeNode in="blur" /><feMergeNode in="SourceGraphic" /></feMerge>
          </filter>
        </defs>
        ${coreBus}
        ${linkPaths}
        ${haBonds}
        ${nodes}
      </svg>
    `;
  }

  const ns = {
    topologyNodeLevel,
    buildTopologyLayers,
    topologyLayout,
    topologyNodeIcon,
    topologyNodeKindLabel,
    renderTopologySvg
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = ns;
  } else {
    window.BSTopology = ns;
  }
}());
