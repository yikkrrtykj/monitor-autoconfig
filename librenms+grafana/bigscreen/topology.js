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

  function stableTopologyCompare(left, right) {
    const a = String(left || "");
    const b = String(right || "");
    return a < b ? -1 : (a > b ? 1 : 0);
  }

  function topologyNodeLevel(node) {
    if (!node) return "none";
    if (!node.success) return "bad";
    if (Number.isFinite(node.latency) && node.latency >= 0.03) return "warn";
    return "good";
  }

  function topologyLatencyIp(node) {
    if (!node) return "";
    return node.kind === "isp" ? (node.probeIp || node.ip || "") : (node.ip || "");
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
          // Ping probes the carrier gateway, while operators need the actual
          // firewall WAN/public address on the topology card.
          ip: target.wanIp || configuredIp || target.targetIp,
          probeIp: target.targetIp,
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
        ip: target.wanIp || target.targetIp,
        probeIp: target.targetIp,
        level: topologyNodeLevel(target),
        latency: target.latency,
        success: target.success
      });
    });

    const infrastructureIps = new Set();
    const firewallUnitTargets = targets.filter((t) => t.job === "infra-fw-unit-snmp");
    // FireCluster 有物理成员 SNMP 数据时，拓扑只画物理成员；共享逻辑地址仍留在
    // 延迟总览中。没有配置成员时回退旧的共享地址，兼容非 HA 防火墙。
    const firewallTargets = firewallUnitTargets.length
      ? firewallUnitTargets
      : targets.filter((t) => t.job === "infra-fw-ping");
    const firewalls = firewallTargets.map((t) => ({
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
    const NODE_W = 144;
    const NODE_H = 58;
    const topPad = 22;
    const bottomPad = 22;
    const rowCount = 4;
    const DIST_LINK_GAP = 66;
    const DIST_NODE_GAP = 44;
    const hasServers = !!(layers.servers && layers.servers.length);
    // A short/wide viewport used to squeeze unresolved servers onto the same
    // y-coordinate as the core distribution bus.  The bus then ran behind an
    // otherwise unlinked server card and looked like a real cable.  Reserve a
    // minimum vertical lane whenever server cards are present.
    const usableHeight = Math.max(hasServers ? 620 : 420, canvasHeight || 680) +
      (hasServers ? 96 : 0);
    const layerGap = Math.max(36, (usableHeight - topPad - bottomPad - NODE_H * rowCount) / (rowCount - 1));
    const rowY = (idx) => topPad + idx * (NODE_H + layerGap);
    // Unresolved servers sit immediately below (and horizontally beside) the
    // core, safely above the core-to-access bus. Located servers are remapped
    // later to a branch below their confirmed access switch.
    const serverRowY = rowY(2) + NODE_H + 12;

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
    const serverIps = new Set((layers.servers || []).map((server) => server.ip).filter(Boolean));
    const infrastructureIps = new Set(
      [...(layers.cores || []), ...(layers.dists || [])].map((node) => node.ip).filter(Boolean)
    );
    const serverParentByIp = new Map();
    const serverParentScoreByIp = new Map();
    (Array.isArray(lldpEdges) ? lldpEdges : []).forEach((edge) => {
      let serverIp = "";
      let parentIp = "";
      if (serverIps.has(edge.from_ip) && infrastructureIps.has(edge.to_ip)) {
        serverIp = edge.from_ip;
        parentIp = edge.to_ip;
      } else if (serverIps.has(edge.to_ip) && infrastructureIps.has(edge.from_ip)) {
        serverIp = edge.to_ip;
        parentIp = edge.from_ip;
      }
      if (!serverIp || !parentIp) return;
      // Exact FDB ownership is authoritative.  Do not let a later weak
      // LLDP/CDP observation overwrite it merely because JSON array order
      // changed between topology refreshes.
      const score = edge.source === "fdb" ? 100 : 10;
      if (score <= (serverParentScoreByIp.get(serverIp) || 0)) return;
      serverParentByIp.set(serverIp, parentIp);
      serverParentScoreByIp.set(serverIp, score);
    });
    const serverChildrenByParent = new Map();
    serverParentByIp.forEach((parentIp, serverIp) => {
      if (!serverChildrenByParent.has(parentIp)) serverChildrenByParent.set(parentIp, []);
      serverChildrenByParent.get(parentIp).push(serverIp);
    });
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
    // Build the access-switch (dist) layer as a tree from the discovered edges:
    // switches that uplink to the core sit in the main row; a switch whose uplink
    // lands on ANOTHER access switch is drawn in a layer below its parent
    // (e.g. core -> FOH -> JIESHOU-RIGHT -> JIESHOU-LEFT).
    const placeDistTree = () => {
      const dists = layers.dists;
      if (!dists.length) return { nodes: [], depthByIp: new Map(), serverSlotsByIp: new Map() };
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
      // Keep switches with a direct peer link next to each other. Without this,
      // target discovery order can put unrelated switches between a pair and the
      // short inter-switch cable is rendered across half of the access row.
      const topLevelByIp = new Map(topLevel.filter((d) => d.ip).map((d) => [d.ip, d]));
      const topLevelOrder = new Map(topLevel.map((d, idx) => [d.ip, idx]));
      const orderedTopLevel = [];
      const groupedTopLevelIps = new Set();
      topLevel.forEach((seed) => {
        if (!seed.ip || groupedTopLevelIps.has(seed.ip)) return;
        const queue = [seed.ip];
        groupedTopLevelIps.add(seed.ip);
        while (queue.length) {
          const ip = queue.shift();
          const item = topLevelByIp.get(ip);
          if (item) orderedTopLevel.push(item);
          Array.from(adj.get(ip) || [])
            .filter((peerIp) => topLevelByIp.has(peerIp) && !groupedTopLevelIps.has(peerIp))
            .sort((a, b) => (topLevelOrder.get(a) || 0) - (topLevelOrder.get(b) || 0))
            .forEach((peerIp) => {
              groupedTopLevelIps.add(peerIp);
              queue.push(peerIp);
            });
        }
      });
      topLevel.forEach((item) => {
        if (!item.ip) orderedTopLevel.push(item);
      });
      const placed = new Map();
      const serverSlotsByIp = new Map();
      const childRowH = NODE_H + DIST_LINK_GAP;
      const subtreeWidthByIp = new Map();
      const subtreeWidth = (ip, visiting = new Set()) => {
        if (!ip || visiting.has(ip)) return NODE_W;
        if (subtreeWidthByIp.has(ip)) return subtreeWidthByIp.get(ip);
        const nextVisiting = new Set(visiting);
        nextVisiting.add(ip);
        const kids = (childrenOf.get(ip) || []).filter((childIp) => distByIp.has(childIp));
        const attachedServers = serverChildrenByParent.get(ip) || [];
        const branchWidths = [
          ...kids.map((childIp) => subtreeWidth(childIp, nextVisiting)),
          ...attachedServers.map(() => NODE_W)
        ];
        const childrenWidth = branchWidths.length
          ? branchWidths.reduce((sum, width) => sum + width, 0) +
            (branchWidths.length - 1) * DIST_NODE_GAP
          : 0;
        const width = Math.max(NODE_W, childrenWidth);
        subtreeWidthByIp.set(ip, width);
        return width;
      };

      // Allocate each top-level switch the width of its complete descendant tree.
      // This avoids the old behaviour where every child was centred independently
      // on its parent and sibling trees eventually occupied the same coordinates.
      const roots = orderedTopLevel.filter((node) => node && node.ip);
      const rootsWidth = roots.reduce((sum, root) => sum + subtreeWidth(root.ip), 0) +
        Math.max(0, roots.length - 1) * DIST_NODE_GAP;
      let rootCursor = Math.max(20, (canvasWidth - rootsWidth) / 2);
      const placeSubtree = (item, spanX, spanWidth, y, visiting = new Set()) => {
        if (!item || !item.ip || visiting.has(item.ip) || placed.has(item.ip)) return;
        const node = {
          ...item,
          x: spanX + (spanWidth - NODE_W) / 2,
          y,
          w: NODE_W,
          h: NODE_H
        };
        placed.set(item.ip, node);
        const nextVisiting = new Set(visiting);
        nextVisiting.add(item.ip);
        const kids = (childrenOf.get(item.ip) || [])
          .map((ip) => distByIp.get(ip))
          .filter((kid) => kid && kid.ip && !nextVisiting.has(kid.ip));
        const branches = [
          ...kids.map((kid) => ({ kind: "dist", kid, width: subtreeWidth(kid.ip) })),
          ...(serverChildrenByParent.get(item.ip) || [])
            .map((serverIp) => ({ kind: "server", serverIp, width: NODE_W }))
        ];
        const branchesWidth = branches.reduce((sum, branch) => sum + branch.width, 0) +
          Math.max(0, branches.length - 1) * DIST_NODE_GAP;
        let childCursor = spanX + (spanWidth - branchesWidth) / 2;
        branches.forEach((branch) => {
          if (branch.kind === "dist") {
            placeSubtree(branch.kid, childCursor, branch.width, y + childRowH, nextVisiting);
          } else {
            serverSlotsByIp.set(branch.serverIp, {
              x: childCursor + (branch.width - NODE_W) / 2,
              y: y + childRowH
            });
          }
          childCursor += branch.width + DIST_NODE_GAP;
        });
      };
      roots.forEach((root) => {
        const width = subtreeWidth(root.ip);
        placeSubtree(root, rootCursor, width, rowY(3));
        rootCursor += width + DIST_NODE_GAP;
      });
      // Safety net: anything not reached above still gets a slot in the main row.
      dists.forEach((d, idx) => {
        if (d.ip && !placed.has(d.ip)) {
          placed.set(d.ip, { ...d, x: Math.max(20, 20 + idx * (NODE_W + DIST_NODE_GAP)), y: rowY(3), w: NODE_W, h: NODE_H });
        }
      });
      return {
        nodes: dists.map((d) => (d.ip ? placed.get(d.ip) : null)).filter(Boolean),
        depthByIp,
        serverSlotsByIp,
      };
    };
    const distTree = placeDistTree();
    const distRow = distTree.nodes;
    const distDepthByIp = distTree.depthByIp;
    const baseServerRow = (hasServers && coreRow.length)
      ? placeServerRow(layers.servers, serverRowY)
      : [];
    const infrastructureByIp = new Map(
      [...coreRow, ...distRow].filter((node) => node.ip).map((node) => [node.ip, node])
    );
    const siblingsByParent = new Map();
    baseServerRow.forEach((server) => {
      const parentIp = serverParentByIp.get(server.ip);
      if (!parentIp) return;
      if (!siblingsByParent.has(parentIp)) siblingsByParent.set(parentIp, []);
      siblingsByParent.get(parentIp).push(server.ip);
    });
    const serverRow = baseServerRow.map((server) => {
      const parentIp = serverParentByIp.get(server.ip);
      const parent = infrastructureByIp.get(parentIp);
      if (!parent) return { ...server, unlocated: true };
      const reservedSlot = distTree.serverSlotsByIp.get(server.ip);
      if (reservedSlot) {
        return { ...server, x: reservedSlot.x, y: reservedSlot.y, unlocated: false };
      }
      const siblings = siblingsByParent.get(parentIp) || [server.ip];
      const siblingIndex = siblings.indexOf(server.ip);
      const siblingOffset = (siblingIndex - (siblings.length - 1) / 2) * (NODE_W + 16);
      return {
        ...server,
        x: Math.min(
          canvasWidth - NODE_W - 20,
          Math.max(20, parent.x + parent.w / 2 - NODE_W / 2 + siblingOffset)
        ),
        // A located server is a downstream leaf. When its access switch has
        // switch children, the reserved slots above keep every leaf on one row.
        y: parent.y + NODE_H + DIST_LINK_GAP,
        unlocated: false,
      };
    });

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
        const orientFromMembers = orientFrom === fromNode ? edge.from_member_ports : edge.to_member_ports;
        const orientToMembers = orientFrom === fromNode ? edge.to_member_ports : edge.from_member_ports;
        const pairKey = pairKeyFor(orientFrom, orientTo);
        const group = groupedEdges.get(pairKey) || {
          from: orientFrom,
          to: orientTo,
          fromPorts: [],
          toPorts: [],
          count: 0,
          liveCount: 0,
          staleCount: 0,
          strongStale: false
        };
        group.fromPorts.push(...(
          Array.isArray(orientFromMembers) && orientFromMembers.length
            ? orientFromMembers
            : [orientFromPort]
        ));
        group.toPorts.push(...(
          Array.isArray(orientToMembers) && orientToMembers.length
            ? orientToMembers
            : [orientToPort]
        ));
        group.count += 1;
        if (edge.stale === true) {
          group.staleCount += 1;
          const staleFromPorts = cleanPortNames(
            Array.isArray(orientFromMembers) && orientFromMembers.length
              ? orientFromMembers
              : [orientFromPort]
          );
          const staleToPorts = cleanPortNames(
            Array.isArray(orientToMembers) && orientToMembers.length
              ? orientToMembers
              : [orientToPort]
          );
          // A cached row with no usable port on one endpoint is only a weak
          // reverse LLDP/CDP shadow. If this device pair also has a live edge,
          // that weak row must not make the healthy link yellow. Fully
          // identified stale members still warn so a degraded LAG remains
          // visible for the configured retention window.
          group.strongStale = group.strongStale || (
            staleFromPorts.length > 0 && staleToPorts.length > 0
          );
        } else {
          group.liveCount += 1;
        }
        groupedEdges.set(pairKey, group);
      });
      groupedEdges.forEach((group, pairKey) => {
        const isCoreDist = (
          (group.from.kind === "core" && group.to.kind === "dist") ||
          (group.from.kind === "dist" && group.to.kind === "core")
        );
        const detail = portDetail(group.fromPorts, group.toPorts, isCoreDist ? 2 : Infinity);
        const endpointLevels = [group.from.level || "good", group.to.level || "good"];
        const staleWarning = group.strongStale || (
          group.staleCount > 0 && group.liveCount === 0
        );
        const severity = endpointLevels.includes("bad")
          ? "bad"
          : (staleWarning || endpointLevels.includes("warn")
            ? "warn"
            : (endpointLevels.includes("none") ? "none" : "good"));
        lldpLinks.push({
          from: group.from,
          to: group.to,
          labelLines: detail.lines,
          severity,
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
    // A server without a discovered LLDP/CDP/FDB edge stays unlinked. Drawing a
    // synthetic core attachment here creates a false topology whenever a single
    // collector cycle temporarily misses the server's ARP/FDB entry.
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
    const branchBuses = [];
    const branchGroups = new Map();
    links.forEach((link) => {
      const upper = link.from.y < link.to.y ? link.from : link.to;
      const lower = upper === link.from ? link.to : link.from;
      if (
        upper.kind !== "dist" ||
        !["dist", "server"].includes(lower.kind) ||
        lower.y <= upper.y
      ) return;
      const key = nodeKey(upper);
      if (!branchGroups.has(key)) branchGroups.set(key, { parent: upper, links: [] });
      branchGroups.get(key).links.push({ link, child: lower });
    });
    branchGroups.forEach((group) => {
      const parentX = group.parent.x + group.parent.w / 2;
      const parentY = group.parent.y + group.parent.h;
      const childY = Math.min(...group.links.map((item) => item.child.y));
      const childXs = group.links.map((item) => item.child.x + item.child.w / 2);
      const bus = {
        x1: Math.min(parentX, ...childXs),
        x2: Math.max(parentX, ...childXs),
        y: parentY + (childY - parentY) / 2,
        parentX,
        parentY,
        severity: group.parent.level || "good"
      };
      branchBuses.push(bus);
      group.links.forEach((item) => {
        item.link.branchBus = bus;
      });
    });

    const assignSlots = (side) => {
      const groups = new Map();
      links.forEach((link) => {
        const key = nodeKey(link[side]);
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(link);
      });
      groups.forEach((group) => {
        const otherSide = side === "from" ? "to" : "from";
        // Allocate attachment slots from left to right according to the other
        // endpoint. LLDP rows can arrive in any order; using that raw order can
        // swap two child links and draw an unnecessary X below their parent.
        group.sort((a, b) => {
          const aOther = a[otherSide];
          const bOther = b[otherSide];
          const aX = aOther.x + aOther.w / 2;
          const bX = bOther.x + bOther.w / 2;
          return aX - bX || nodeKey(aOther).localeCompare(nodeKey(bOther));
        });
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
      branchBuses,
      height: Math.max(usableHeight, allNodes.reduce((m, n) => Math.max(m, n.y + (n.h || 0)), 0) + bottomPad)
    };
  }

  function topologyNodeIcon(kind) {
    return { isp: "🌐", firewall: "🛡", core: "★", dist: "▦", infrastructure: "▣", server: "⚙" }[kind] || "?";
  }

  function topologyNodeKindLabel(kind) {
    return { isp: "ISP", firewall: "防火墙", core: "核心", dist: "接入", infrastructure: "网络设备", server: "服务器" }[kind] || kind;
  }

  function physicalTargetIp(target) {
    return String(target && (target.targetIp || target.ip || target.instance) || "").trim();
  }

  function physicalTargetKind(target, fallbackKind) {
    if (fallbackKind === "server") return "server";
    const job = String(target && target.job || "");
    if (job === "infra-isp-ping") return "isp";
    if (job === "infra-fw-unit-snmp" || job === "infra-fw-ping") return "firewall";
    if (job === "infra-core-ping") return "core";
    if (job === "infra-dist-ping") return "dist";
    return "infrastructure";
  }

  function buildPhysicalTopologyNodes(projection, targets) {
    const targetByIp = new Map();
    (Array.isArray(targets) ? targets : []).slice().sort((a, b) => stableTopologyCompare(
      `${physicalTargetIp(a)}|${a.job || ""}|${a.displayName || ""}|${a.instance || ""}`,
      `${physicalTargetIp(b)}|${b.job || ""}|${b.displayName || ""}|${b.instance || ""}`
    )).forEach((target) => {
      const ip = physicalTargetIp(target);
      if (ip && !targetByIp.has(ip)) targetByIp.set(ip, target);
    });

    const rank = { isp: 0, firewall: 1, core: 2, dist: 3, infrastructure: 4, server: 5 };
    return (projection.devices || []).map((device) => {
      const target = targetByIp.get(device.ip) || null;
      const kind = physicalTargetKind(target, device.kind);
      return {
        kind,
        name: target && target.displayName || device.sysname || device.ip,
        ip: device.ip,
        level: target ? topologyNodeLevel(target) : "none",
        latency: target ? target.latency : null,
        success: target ? target.success : undefined,
        physicalKind: device.kind
      };
    }).sort((a, b) => (
      (rank[a.kind] ?? rank.infrastructure) - (rank[b.kind] ?? rank.infrastructure) ||
      stableTopologyCompare(a.name, b.name) ||
      stableTopologyCompare(a.ip, b.ip)
    ));
  }

  function physicalEndpointLabel(endpoint) {
    if (!endpoint) return "";
    const aggregatePort = endpoint.aggregatePort || "";
    const port = endpoint.port || "";
    if (aggregatePort && port) return `${aggregatePort} / ${port}`;
    if (aggregatePort) return aggregatePort;
    return port;
  }

  function physicalTopologyLayout(projection, targets, canvasWidth, canvasHeight) {
    const NODE_W = 144;
    const NODE_H = 58;
    const H_GAP = 24;
    const X_PAD = 24;
    const Y_PAD = 24;
    const nodes = buildPhysicalTopologyNodes(projection, targets);
    const ranks = { isp: 0, firewall: 1, core: 2, dist: 3, infrastructure: 4, server: 5 };
    const rows = new Map();
    nodes.forEach((node) => {
      const row = ranks[node.kind] ?? ranks.infrastructure;
      if (!rows.has(row)) rows.set(row, []);
      rows.get(row).push(node);
    });
    const rowKeys = Array.from(rows.keys()).sort((a, b) => a - b);
    const maxRow = Math.max(1, ...Array.from(rows.values()).map((items) => items.length));
    const width = Math.max(Number(canvasWidth) || 1200, maxRow * (NODE_W + H_GAP) + X_PAD * 2);
    const minimumHeight = Math.max(420, Number(canvasHeight) || 680);
    const rowGap = rowKeys.length > 1
      ? Math.max(46, (minimumHeight - Y_PAD * 2 - NODE_H * rowKeys.length) / (rowKeys.length - 1))
      : 0;
    rowKeys.forEach((rowKey, rowIndex) => {
      const row = rows.get(rowKey).sort((a, b) => (
        stableTopologyCompare(a.name, b.name) || stableTopologyCompare(a.ip, b.ip)
      ));
      const rowWidth = row.length * NODE_W + Math.max(0, row.length - 1) * H_GAP;
      const startX = Math.max(X_PAD, (width - rowWidth) / 2);
      row.forEach((node, index) => {
        node.x = startX + index * (NODE_W + H_GAP);
        node.y = Y_PAD + rowIndex * (NODE_H + rowGap);
        node.w = NODE_W;
        node.h = NODE_H;
      });
    });

    const nodeByIp = new Map(nodes.map((node) => [node.ip, node]));
    const links = [];
    const addLink = (item, kind, aEndpoint, bEndpoint) => {
      const from = nodeByIp.get(aEndpoint.ip);
      const to = nodeByIp.get(bEndpoint.ip);
      if (!from || !to) return;
      links.push({
        id: item.id,
        kind,
        from,
        to,
        fromEndpoint: aEndpoint,
        toEndpoint: bEndpoint,
        labelLines: [physicalEndpointLabel(aEndpoint), physicalEndpointLabel(bEndpoint)],
        stale: item.stale === true ? true : (item.stale === false ? false : null),
        hasStaleMembers: item.hasStaleMembers === true,
        protocols: Array.isArray(item.protocols) ? item.protocols.slice() : [],
        lastSeen: item.lastSeen
      });
    };

    (projection.physicalLinks || []).forEach((link) => addLink(link, "physical", link.a, link.b));
    (projection.bundles || []).forEach((bundle) => addLink(bundle, "bundle", bundle.a, bundle.b));
    (projection.serverAttachments || []).forEach((attachment) => addLink(
      attachment,
      "attachment",
      attachment.switchEndpoint,
      attachment.serverEndpoint
    ));

    links.sort((a, b) => stableTopologyCompare(a.id, b.id));
    const parallelGroups = new Map();
    links.forEach((link) => {
      const pair = [link.from.ip, link.to.ip].sort(stableTopologyCompare).join("--");
      if (!parallelGroups.has(pair)) parallelGroups.set(pair, []);
      parallelGroups.get(pair).push(link);
    });
    parallelGroups.forEach((group) => {
      group.sort((a, b) => stableTopologyCompare(a.id, b.id));
      group.forEach((link, index) => {
        link.parallelIndex = index;
        link.parallelCount = group.length;
        link.parallelOffset = (index - (group.length - 1) / 2) * 12;
      });
    });

    return {
      nodes,
      links,
      width,
      height: Math.max(
        minimumHeight,
        nodes.reduce((maximum, node) => Math.max(maximum, node.y + node.h + Y_PAD), 0)
      ),
      compatibilityWarnings: (projection.compatibilityWarnings || []).slice()
    };
  }

  function physicalEndpointSignature(endpoint) {
    return [
      endpoint && endpoint.ip || "",
      endpoint && endpoint.ifindex === null ? "" : endpoint && endpoint.ifindex || "",
      endpoint && endpoint.port || "",
      endpoint && endpoint.aggregatePort || "",
      endpoint && Array.isArray(endpoint.memberPorts) ? endpoint.memberPorts.join(",") : ""
    ].join("|");
  }

  function physicalTopologySignature(layout, width) {
    const nodesSig = (layout.nodes || []).map((node) => (
      `${node.kind}|${node.ip || ""}|${node.name}|${node.level}`
    )).join("#");
    const linksSig = (layout.links || []).map((link) => [
      link.id,
      link.kind,
      physicalEndpointSignature(link.fromEndpoint),
      physicalEndpointSignature(link.toEndpoint),
      link.stale === true ? "stale" : (link.stale === false ? "fresh" : "unknown"),
      link.hasStaleMembers === true ? "partial-stale" : ""
    ].join("|")).join("#");
    return `${width}@${nodesSig}@@${linksSig}`;
  }

  function topologyTextWidth(text) {
    let width = 0;
    for (const character of String(text || "")) {
      width += /[　-鿿＀-￯]/.test(character) ? 13 : 7.3;
    }
    return width;
  }

  function renderTopologyNodes(nodes) {
    return (nodes || []).map((node, idx) => {
      const latencyText = Number.isFinite(node.latency)
        ? formatPingText(node.latency)
        : (node.kind === "isp" && node.success === true ? "在线" : "");
      const dataAttrs = `data-idx="${idx}" data-kind="${escapeHtml(node.kind)}" data-name="${escapeHtml(node.name)}" data-ip="${escapeHtml(node.ip || "")}" data-level="${escapeHtml(node.level)}"`;
      const subline = node.ip
        ? `<text class="topology-node-ip" x="14" y="${node.h - 8}">${escapeHtml(node.ip)}</text>`
        : "";
      const nodeName = String(node.name || "?");
      const nameMaxW = node.w - 42;
      const nameFitAttr = topologyTextWidth(nodeName) > nameMaxW
        ? ` textLength="${nameMaxW}" lengthAdjust="spacingAndGlyphs"`
        : "";
      const nodeKindLabel = node.kind === "server" && node.unlocated
        ? "服务器 · 未定位"
        : topologyNodeKindLabel(node.kind);
      return `
        <g class="topology-node node-${node.level}${node.unlocated ? " node-unlocated" : ""}" transform="translate(${node.x},${node.y})" ${dataAttrs} role="button" tabindex="0">
          <rect width="${node.w}" height="${node.h}" rx="10" />
          <text class="topology-node-icon" x="14" y="22">${topologyNodeIcon(node.kind)}</text>
          <text class="topology-node-name" x="34" y="22"${nameFitAttr}>${escapeHtml(nodeName)}</text>
          <text class="topology-node-kind" x="34" y="38">${escapeHtml(nodeKindLabel)}</text>
          <text class="topology-node-latency" x="${node.w - 10}" y="38" text-anchor="end">${escapeHtml(latencyText)}</text>
          ${subline}
        </g>
      `;
    }).join("");
  }

  function renderPhysicalTopologySvg(layout, canvasWidth) {
    if (!(layout.links || []).length) {
      return `<div class="topology-empty topology-physical-empty">No accepted physical topology</div>`;
    }
    const center = (node) => ({ x: node.x + node.w / 2, y: node.y + node.h / 2 });
    const linkPaths = layout.links.map((link) => {
      const from = center(link.from);
      const to = center(link.to);
      const dx = to.x - from.x;
      const dy = to.y - from.y;
      const length = Math.max(1, Math.sqrt(dx * dx + dy * dy));
      const normalX = -dy / length;
      const normalY = dx / length;
      const offset = link.parallelOffset || 0;
      const controlX = (from.x + to.x) / 2 + normalX * offset;
      const controlY = (from.y + to.y) / 2 + normalY * offset;
      const path = offset
        ? `M ${from.x} ${from.y} Q ${controlX} ${controlY} ${to.x} ${to.y}`
        : `M ${from.x} ${from.y} L ${to.x} ${to.y}`;
      const classes = ["topology-link", `topology-link--${link.kind}`];
      if (link.stale === true) classes.push("topology-link--stale");
      if (link.kind === "bundle" && link.stale !== true && link.hasStaleMembers) {
        classes.push("topology-link--partial-stale");
      }
      const labelAt = (ratio, text, endpoint) => {
        if (!text) return "";
        const x = from.x + dx * ratio + normalX * offset;
        const y = from.y + dy * ratio + normalY * offset - 7;
        return `<text class="topology-link-label topology-link-label--physical" data-endpoint="${endpoint}" x="${x}" y="${y}" text-anchor="middle">${escapeHtml(text)}</text>`;
      };
      return `
        <g class="topology-physical-link-group" data-link-id="${escapeHtml(link.id)}" data-link-kind="${link.kind}">
          <path class="${classes.join(" ")}" d="${path}" />
          ${labelAt(0.2, link.labelLines[0], "a")}
          ${labelAt(0.8, link.labelLines[1], "b")}
        </g>
      `;
    }).join("");
    const nodes = renderTopologyNodes(layout.nodes);
    return `
      <svg class="topology-svg topology-physical-svg" viewBox="0 0 ${canvasWidth} ${layout.height}" data-base-width="${canvasWidth}" data-base-height="${layout.height}" preserveAspectRatio="xMidYMid meet" focusable="false">
        ${linkPaths}
        ${nodes}
      </svg>
    `;
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
    const coreBus = layout.coreBus
      ? `<path class="topology-link topology-backbone link-${layout.coreBus.severity}" d="M ${layout.coreBus.coreX} ${layout.coreBus.coreY} L ${layout.coreBus.coreX} ${layout.coreBus.y} M ${layout.coreBus.x1} ${layout.coreBus.y} L ${layout.coreBus.x2} ${layout.coreBus.y}" />`
      : "";
    const branchBuses = (layout.branchBuses || []).map((bus) =>
      `<path class="topology-link topology-backbone topology-branch-backbone link-${bus.severity}" d="M ${bus.parentX} ${bus.parentY} L ${bus.parentX} ${bus.y} M ${bus.x1} ${bus.y} L ${bus.x2} ${bus.y}" />`
    ).join("");

    const linkPaths = layout.links.map((link) => {
      let labelX;
      let labelY;
      let labelAnchor = "middle";
      let labelPositions = null;
      let d;
      if (link.branchBus) {
        const childNode = link.from.y > link.to.y ? link.from : link.to;
        const x = nodeCenterX(childNode);
        const busY = link.branchBus.y;
        d = `M ${x} ${busY} L ${x} ${childNode.y}`;
        labelX = x + 14;
        labelY = Math.max(busY + 12, childNode.y - 34);
        labelAnchor = "start";
        if (Array.isArray(link.labelLines) && link.labelLines.length > 1) {
          labelPositions = [
            { text: link.labelLines[0], x: x + 14, y: busY - 8, anchor: "start" },
            { text: link.labelLines[1], x: x + 14, y: childNode.y - 5, anchor: "start" }
          ];
        }
      } else if (link.busLink && layout.coreBus) {
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
        const x1 = nodeCenterX(left);
        const x2 = nodeCenterX(right);
        const y1 = left.y + left.h;
        const y2 = right.y + right.h;
        const routeY = Math.max(y1, y2) + 18;
        d = `M ${x1} ${y1} L ${x1} ${routeY} L ${x2} ${routeY} L ${x2} ${y2}`;
        labelX = (x1 + x2) / 2;
        labelY = routeY + 13;
        if (Array.isArray(link.labelLines) && link.labelLines.length > 1) {
          const leftPort = left === link.from ? link.labelLines[0] : link.labelLines[1];
          const rightPort = left === link.from ? link.labelLines[1] : link.labelLines[0];
          labelPositions = [
            { text: leftPort, x: x1 + 9, y: y1 + 13, anchor: "start" },
            { text: rightPort, x: x2 + 9, y: y2 + 13, anchor: "start" }
          ];
        }
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
            {
              text: link.labelLines[0],
              x: x + 14,
              y: y1 + 13,
              anchor: "start"
            },
            {
              text: link.labelLines[1],
              x: x + 14,
              y: y2 - 5,
              anchor: "start"
            }
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
            {
              text: link.labelLines[0],
              // Spread parent-side labels along the fanned-out branch. They stay
              // on one horizontal row while long port names remain separated.
              x: x1 + (x2 - x1) * 0.45,
              y: y1 + 13,
              anchor: "middle"
            },
            {
              text: link.labelLines[1],
              x: x2,
              y: y2 - 5,
              anchor: "middle"
            }
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

    const nodes = renderTopologyNodes(layout.nodes);

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
        ${branchBuses}
        ${linkPaths}
        ${haBonds}
        ${nodes}
      </svg>
    `;
  }

  const ns = {
    topologyNodeLevel,
    topologyLatencyIp,
    buildTopologyLayers,
    topologyLayout,
    topologyNodeIcon,
    topologyNodeKindLabel,
    renderTopologySvg,
    buildPhysicalTopologyNodes,
    physicalTopologyLayout,
    physicalTopologySignature,
    renderPhysicalTopologySvg
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = ns;
  } else {
    window.BSTopology = ns;
  }
}());

