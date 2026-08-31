;(function () {
  'use strict';

  const ALLOWED_EDGE_TYPES = new Set(["physical", "server_attachment"]);
  const ALLOWED_PROTOCOLS = new Set(["cdp", "lldp", "xdp"]);
  const hasOwn = (value, key) => Object.prototype.hasOwnProperty.call(value, key);

  function stableCompare(left, right) {
    const a = String(left);
    const b = String(right);
    return a < b ? -1 : (a > b ? 1 : 0);
  }

  function stableValue(value) {
    if (value === null || typeof value === "string" || typeof value === "boolean") return value;
    if (typeof value === "number") return Number.isFinite(value) ? value : String(value);
    if (typeof value === "undefined") return "<undefined>";
    if (Array.isArray(value)) return value.map(stableValue);
    if (typeof value === "object") {
      const output = {};
      Object.keys(value).sort(stableCompare).forEach((key) => {
        output[key] = stableValue(value[key]);
      });
      return output;
    }
    return String(value);
  }

  function stableStringify(value) {
    return JSON.stringify(stableValue(value));
  }

  function nullableString(value) {
    if (typeof value !== "string") return null;
    const text = value.trim();
    return text || null;
  }

  function identityString(value) {
    return nullableString(value);
  }

  function normalizedIfindex(value) {
    if (value === null || typeof value === "undefined" || value === "") return null;
    if (typeof value === "number") return Number.isFinite(value) ? value : null;
    if (typeof value === "string") return value.trim() || null;
    return null;
  }

  function normalizedMembers(value) {
    if (!Array.isArray(value)) return [];
    return Array.from(new Set(value
      .filter((item) => typeof item === "string")
      .map((item) => item.trim())
      .filter(Boolean)))
      .sort(stableCompare);
  }

  function normalizeLastSeen(value) {
    return typeof value === "number" && Number.isFinite(value) ? value : null;
  }

  function aggregateStaleState(states) {
    if (states.some((value) => value === false)) return false;
    if (states.length > 0 && states.every((value) => value === true)) return true;
    return null;
  }

  function latestLastSeen(values) {
    const finiteValues = values
      .map(normalizeLastSeen)
      .filter((value) => value !== null);
    return finiteValues.length ? Math.max(...finiteValues) : null;
  }

  function stableNonNullChoice(values) {
    const byValue = new Map();
    values.filter((value) => value !== null && typeof value !== "undefined").forEach((value) => {
      const key = stableStringify(value);
      if (!byValue.has(key)) byValue.set(key, stableValue(value));
    });
    const keys = Array.from(byValue.keys()).sort(stableCompare);
    return keys.length ? byValue.get(keys[0]) : null;
  }

  function endpointFromEdge(edge, side) {
    return {
      ip: identityString(edge[`${side}_ip`]),
      sysname: nullableString(edge[`${side}_sysname`]),
      port: nullableString(edge[`${side}_port`]),
      ifindex: normalizedIfindex(edge[`${side}_ifindex`]),
      aggregatePort: nullableString(edge[`${side}_aggregate_port`]),
      memberPorts: normalizedMembers(edge[`${side}_member_ports`])
    };
  }

  function endpointIdentity(endpoint) {
    return stableStringify([
      endpoint.ip,
      endpoint.ifindex === null ? null : String(endpoint.ifindex),
      endpoint.port
    ]);
  }

  function bundleEndpointIdentity(endpoint) {
    return stableStringify([endpoint.ip, endpoint.aggregatePort]);
  }

  function canonicalEndpoints(fromEndpoint, toEndpoint, identity) {
    const fromKey = identity(fromEndpoint);
    const toKey = identity(toEndpoint);
    if (stableCompare(fromKey, toKey) <= 0) {
      return { a: fromEndpoint, b: toEndpoint, aKey: fromKey, bKey: toKey };
    }
    return { a: toEndpoint, b: fromEndpoint, aKey: toKey, bKey: fromKey };
  }

  function edgeFingerprint(edge) {
    if (!edge || typeof edge !== "object" || Array.isArray(edge)) {
      return stableStringify(edge);
    }
    const endpoints = ["from", "to"].map((side) => ({
      ip: identityString(edge[`${side}_ip`]),
      port: nullableString(edge[`${side}_port`]),
      ifindex: normalizedIfindex(edge[`${side}_ifindex`]),
      aggregatePort: nullableString(edge[`${side}_aggregate_port`])
    })).sort((a, b) => stableCompare(stableStringify(a), stableStringify(b)));
    return stableStringify({
      edgeType: hasOwn(edge, "edge_type") ? stableValue(edge.edge_type) : "<legacy>",
      endpoints,
      source: hasOwn(edge, "source") ? stableValue(edge.source) : null
    });
  }

  function addWarning(warnings, code, identity, details) {
    const warning = { code, identity };
    Object.keys(details || {}).sort(stableCompare).forEach((key) => {
      warning[key] = stableValue(details[key]);
    });
    warnings.push(warning);
  }

  function classifyEdge(edge, warnings) {
    if (!edge || typeof edge !== "object" || Array.isArray(edge)) {
      addWarning(warnings, "malformed-edge", edgeFingerprint(edge));
      return null;
    }
    if (!hasOwn(edge, "edge_type") || edge.edge_type === null || typeof edge.edge_type === "undefined") {
      return String(edge.source || "").trim().toLowerCase() === "fdb"
        ? "server_attachment"
        : "physical";
    }
    const edgeType = typeof edge.edge_type === "string" ? edge.edge_type.trim() : "";
    if (!ALLOWED_EDGE_TYPES.has(edgeType)) {
      addWarning(warnings, "unknown-edge-type", edgeFingerprint(edge), {
        value: stableStringify(edge.edge_type)
      });
      return null;
    }
    return edgeType;
  }

  function validEdgeEndpoints(edge, warnings) {
    const fromIp = identityString(edge.from_ip);
    const toIp = identityString(edge.to_ip);
    if (fromIp && toIp) return true;
    addWarning(warnings, "malformed-edge", edgeFingerprint(edge), {
      reason: "missing-endpoint-identity"
    });
    return false;
  }

  function normalizedProtocols(edge, warnings) {
    if (!hasOwn(edge, "protocols") || edge.protocols === null) return [];
    const rawValues = Array.isArray(edge.protocols) ? edge.protocols : [edge.protocols];
    const protocols = new Set();
    rawValues.forEach((rawValue) => {
      const value = typeof rawValue === "string" ? rawValue.trim().toLowerCase() : "";
      if (ALLOWED_PROTOCOLS.has(value)) {
        protocols.add(value);
        return;
      }
      addWarning(warnings, "unknown-protocol", edgeFingerprint(edge), {
        value: stableStringify(rawValue)
      });
    });
    return Array.from(protocols).sort(stableCompare);
  }

  function staleState(edge) {
    if (edge.stale === true) return true;
    if (edge.stale === false) return false;
    return null;
  }

  function addDeviceCandidate(candidates, endpoint, kind) {
    if (!endpoint.ip) return;
    if (!candidates.has(endpoint.ip)) {
      candidates.set(endpoint.ip, { kinds: new Set(), sysnames: new Set() });
    }
    const candidate = candidates.get(endpoint.ip);
    candidate.kinds.add(kind);
    if (endpoint.sysname) candidate.sysnames.add(endpoint.sysname);
  }

  function mergeEndpointFacts(current, incoming) {
    return {
      ip: current.ip,
      sysname: stableNonNullChoice([current.sysname, incoming.sysname]),
      port: current.port,
      ifindex: stableNonNullChoice([current.ifindex, incoming.ifindex]),
      aggregatePort: stableNonNullChoice([current.aggregatePort, incoming.aggregatePort]),
      memberPorts: Array.from(new Set([
        ...current.memberPorts,
        ...incoming.memberPorts
      ])).sort(stableCompare)
    };
  }

  function addOrdinarySource(groups, edge, fromEndpoint, toEndpoint, protocols) {
    const oriented = canonicalEndpoints(fromEndpoint, toEndpoint, endpointIdentity);
    const key = `${oriented.aKey}--${oriented.bKey}`;
    if (!groups.has(key)) {
      groups.set(key, {
        id: `physical:${key}`,
        kind: "physical",
        a: { ...oriented.a, memberPorts: oriented.a.memberPorts.slice() },
        b: { ...oriented.b, memberPorts: oriented.b.memberPorts.slice() },
        protocols: new Set(),
        staleStates: [],
        lastSeenValues: []
      });
    }
    const group = groups.get(key);
    group.a = mergeEndpointFacts(group.a, oriented.a);
    group.b = mergeEndpointFacts(group.b, oriented.b);
    protocols.forEach((protocol) => group.protocols.add(protocol));
    group.staleStates.push(staleState(edge));
    group.lastSeenValues.push(hasOwn(edge, "last_seen") ? edge.last_seen : null);
  }

  function finalizeOrdinaryLink(group) {
    return {
      id: group.id,
      kind: group.kind,
      a: group.a,
      b: group.b,
      protocols: Array.from(group.protocols).sort(stableCompare),
      stale: aggregateStaleState(group.staleStates),
      lastSeen: latestLastSeen(group.lastSeenValues)
    };
  }

  function bundleEndpoint(endpoint) {
    return {
      ip: endpoint.ip,
      sysname: endpoint.sysname,
      aggregatePort: endpoint.aggregatePort,
      memberPorts: endpoint.memberPorts.slice()
    };
  }

  function addBundleSource(groups, edge, fromEndpoint, toEndpoint, protocols) {
    const oriented = canonicalEndpoints(fromEndpoint, toEndpoint, bundleEndpointIdentity);
    const key = `${oriented.aKey}--${oriented.bKey}`;
    if (!groups.has(key)) {
      groups.set(key, {
        id: `bundle:${key}`,
        a: bundleEndpoint(oriented.a),
        b: bundleEndpoint(oriented.b),
        aMembers: new Set(),
        bMembers: new Set(),
        protocols: new Set(),
        sourceEdgeCount: 0,
        staleStates: [],
        lastSeenValues: []
      });
    }
    const group = groups.get(key);
    oriented.a.memberPorts.forEach((member) => group.aMembers.add(member));
    oriented.b.memberPorts.forEach((member) => group.bMembers.add(member));
    protocols.forEach((protocol) => group.protocols.add(protocol));
    group.sourceEdgeCount += 1;
    group.staleStates.push(staleState(edge));
    group.lastSeenValues.push(hasOwn(edge, "last_seen") ? edge.last_seen : null);
  }

  function finalizeBundle(group) {
    return {
      id: group.id,
      kind: "bundle",
      a: {
        ...group.a,
        memberPorts: Array.from(group.aMembers).sort(stableCompare)
      },
      b: {
        ...group.b,
        memberPorts: Array.from(group.bMembers).sort(stableCompare)
      },
      protocols: Array.from(group.protocols).sort(stableCompare),
      sourceEdgeCount: group.sourceEdgeCount,
      stale: aggregateStaleState(group.staleStates),
      hasStaleMembers: group.staleStates.some((value) => value === true),
      lastSeen: latestLastSeen(group.lastSeenValues)
    };
  }

  function serverEndpointShape(endpoint) {
    return {
      ip: endpoint.ip,
      sysname: endpoint.sysname,
      port: endpoint.port,
      ifindex: endpoint.ifindex
    };
  }

  function addServerAttachmentSource(groups, edge, switchEndpoint, serverEndpoint) {
    const switchKey = endpointIdentity(switchEndpoint);
    const serverKey = endpointIdentity(serverEndpoint);
    const key = `${switchKey}--${serverKey}`;
    if (!groups.has(key)) {
      groups.set(key, {
        id: `server_attachment:${key}`,
        kind: "server_attachment",
        switchEndpoint: serverEndpointShape(switchEndpoint),
        serverEndpoint: serverEndpointShape(serverEndpoint),
        serverMacValues: [],
        serverVlanValues: []
      });
    }
    const group = groups.get(key);
    group.switchEndpoint = mergeEndpointFacts(
      { ...group.switchEndpoint, aggregatePort: null, memberPorts: [] },
      { ...serverEndpointShape(switchEndpoint), aggregatePort: null, memberPorts: [] }
    );
    group.serverEndpoint = mergeEndpointFacts(
      { ...group.serverEndpoint, aggregatePort: null, memberPorts: [] },
      { ...serverEndpointShape(serverEndpoint), aggregatePort: null, memberPorts: [] }
    );
    delete group.switchEndpoint.aggregatePort;
    delete group.switchEndpoint.memberPorts;
    delete group.serverEndpoint.aggregatePort;
    delete group.serverEndpoint.memberPorts;
    if (hasOwn(edge, "server_mac")) group.serverMacValues.push(edge.server_mac);
    if (hasOwn(edge, "server_vlan")) group.serverVlanValues.push(edge.server_vlan);
  }

  function resolveServerMetadata(values, warnings, code, identity) {
    const candidates = values.filter((value) => (
      value !== null &&
      typeof value !== "undefined" &&
      !(typeof value === "string" && !value.trim())
    ));
    const byValue = new Map();
    candidates.forEach((value) => {
      const key = stableStringify(value);
      if (!byValue.has(key)) byValue.set(key, stableValue(value));
    });
    const keys = Array.from(byValue.keys()).sort(stableCompare);
    const canonicalValues = keys.map((key) => byValue.get(key));
    if (canonicalValues.length > 1) {
      addWarning(warnings, code, identity, { values: canonicalValues });
    }
    return canonicalValues.length ? canonicalValues[0] : null;
  }

  function finalizeServerAttachment(group, warnings) {
    return {
      id: group.id,
      kind: group.kind,
      switchEndpoint: group.switchEndpoint,
      serverEndpoint: group.serverEndpoint,
      source: "fdb",
      serverMac: resolveServerMetadata(
        group.serverMacValues,
        warnings,
        "conflicting-server-mac",
        group.id
      ),
      serverVlan: resolveServerMetadata(
        group.serverVlanValues,
        warnings,
        "conflicting-server-vlan",
        group.id
      )
    };
  }

  function finalizeDevices(candidates, warnings) {
    const devices = [];
    candidates.forEach((candidate, ip) => {
      const kinds = Array.from(candidate.kinds).sort(stableCompare);
      const sysnames = Array.from(candidate.sysnames).sort(stableCompare);
      if (kinds.length > 1) {
        addWarning(warnings, "conflicting-device-kind", ip, { values: kinds });
      }
      if (sysnames.length > 1) {
        addWarning(warnings, "conflicting-device-sysname", ip, { values: sysnames });
      }
      devices.push({
        ip,
        sysname: sysnames.length ? sysnames[0] : null,
        kind: kinds.length ? kinds[0] : "infrastructure"
      });
    });
    return devices.sort((a, b) => stableCompare(a.ip, b.ip));
  }

  function canonicalizeEndpointNames(items, devices) {
    const names = new Map(devices.map((device) => [device.ip, device.sysname]));
    const withName = (endpoint) => ({ ...endpoint, sysname: names.get(endpoint.ip) || null });
    items.physicalLinks = items.physicalLinks.map((link) => ({
      ...link,
      a: withName(link.a),
      b: withName(link.b)
    }));
    items.bundles = items.bundles.map((bundle) => ({
      ...bundle,
      a: withName(bundle.a),
      b: withName(bundle.b)
    }));
    items.serverAttachments = items.serverAttachments.map((attachment) => ({
      ...attachment,
      switchEndpoint: withName(attachment.switchEndpoint),
      serverEndpoint: withName(attachment.serverEndpoint)
    }));
  }

  function stableSorted(items) {
    return items.slice().sort((a, b) => {
      const byId = stableCompare(a.id || "", b.id || "");
      return byId || stableCompare(stableStringify(a), stableStringify(b));
    });
  }

  function finalizeWarnings(warnings) {
    const byValue = new Map();
    warnings.forEach((warning) => {
      const key = stableStringify(warning);
      if (!byValue.has(key)) byValue.set(key, warning);
    });
    return Array.from(byValue.values()).sort((a, b) =>
      stableCompare(stableStringify(a), stableStringify(b))
    );
  }

  function projectPhysicalTopology(edges) {
    const warnings = [];
    const physicalGroups = new Map();
    const serverAttachmentGroups = new Map();
    const bundleGroups = new Map();
    const deviceCandidates = new Map();

    if (!Array.isArray(edges)) {
      addWarning(warnings, "invalid-edges-root", "edges", {
        value: stableStringify(edges)
      });
      return {
        devices: [],
        physicalLinks: [],
        bundles: [],
        serverAttachments: [],
        compatibilityWarnings: finalizeWarnings(warnings)
      };
    }

    edges.forEach((edge) => {
      const edgeType = classifyEdge(edge, warnings);
      if (!edgeType || !validEdgeEndpoints(edge, warnings)) return;

      const fromEndpoint = endpointFromEdge(edge, "from");
      const toEndpoint = endpointFromEdge(edge, "to");

      if (edgeType === "server_attachment") {
        addDeviceCandidate(deviceCandidates, fromEndpoint, "infrastructure");
        addDeviceCandidate(deviceCandidates, toEndpoint, "server");
        addServerAttachmentSource(serverAttachmentGroups, edge, fromEndpoint, toEndpoint);
        return;
      }

      const protocols = normalizedProtocols(edge, warnings);
      addDeviceCandidate(deviceCandidates, fromEndpoint, "infrastructure");
      addDeviceCandidate(deviceCandidates, toEndpoint, "infrastructure");
      if (fromEndpoint.aggregatePort && toEndpoint.aggregatePort) {
        addBundleSource(bundleGroups, edge, fromEndpoint, toEndpoint, protocols);
      } else {
        addOrdinarySource(physicalGroups, edge, fromEndpoint, toEndpoint, protocols);
      }
    });

    const devices = finalizeDevices(deviceCandidates, warnings);
    const projected = {
      physicalLinks: Array.from(physicalGroups.values()).map(finalizeOrdinaryLink),
      bundles: Array.from(bundleGroups.values()).map(finalizeBundle),
      serverAttachments: Array.from(serverAttachmentGroups.values()).map((group) => (
        finalizeServerAttachment(group, warnings)
      ))
    };
    canonicalizeEndpointNames(projected, devices);

    return {
      devices,
      physicalLinks: stableSorted(projected.physicalLinks),
      bundles: stableSorted(projected.bundles),
      serverAttachments: stableSorted(projected.serverAttachments),
      compatibilityWarnings: finalizeWarnings(warnings)
    };
  }

  const ns = {
    projectPhysicalTopology
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = ns;
  } else {
    window.BSPhysicalTopology = ns;
  }
}());
