const assert = require("assert");
const path = require("path");

const physicalTopology = require(path.resolve(
  __dirname,
  "../bigscreen/physical-topology.js"
));
const { projectPhysicalTopology } = physicalTopology;

assert.deepStrictEqual(
  Object.keys(physicalTopology),
  ["projectPhysicalTopology"],
  "Stage 3A exposes only the pure projection"
);

const clone = (value) => JSON.parse(JSON.stringify(value));

const physicalEdge = (overrides = {}) => ({
  edge_type: "physical",
  from_ip: "10.0.0.1",
  from_sysname: "Switch-A",
  from_port: "Gi1/0/1",
  from_ifindex: 101,
  to_ip: "10.0.0.2",
  to_sysname: "Switch-B",
  to_port: "Gi1/0/2",
  to_ifindex: 202,
  protocols: ["lldp"],
  stale: false,
  last_seen: 100,
  ...overrides
});

function reversePhysicalEdge(edge) {
  const reversed = {};
  Object.keys(edge).forEach((key) => {
    if (key.startsWith("from_")) {
      reversed[`to_${key.slice(5)}`] = edge[key];
    } else if (key.startsWith("to_")) {
      reversed[`from_${key.slice(3)}`] = edge[key];
    } else {
      reversed[key] = edge[key];
    }
  });
  return reversed;
}

function warningCodes(projection) {
  return projection.compatibilityWarnings.map((warning) => warning.code);
}

function assertUniqueIds(items) {
  const ids = items.map((item) => item.id);
  assert.strictEqual(new Set(ids).size, ids.length);
}

function objectKeysDeep(value, output = []) {
  if (!value || typeof value !== "object") return output;
  if (Array.isArray(value)) {
    value.forEach((item) => objectKeysDeep(item, output));
    return output;
  }
  Object.keys(value).forEach((key) => {
    output.push(key);
    objectKeysDeep(value[key], output);
  });
  return output;
}

// ---- Fixed output contract + ordinary physical adjacency ----
const ordinary = projectPhysicalTopology([physicalEdge()]);
assert.deepStrictEqual(Object.keys(ordinary), [
  "devices",
  "physicalLinks",
  "bundles",
  "serverAttachments",
  "compatibilityWarnings"
]);
assert.strictEqual(ordinary.physicalLinks.length, 1);
assert.strictEqual(ordinary.bundles.length, 0);
assert.strictEqual(ordinary.serverAttachments.length, 0);
assert.strictEqual(ordinary.physicalLinks[0].kind, "physical");
assert.deepStrictEqual(ordinary.physicalLinks[0].a, {
  ip: "10.0.0.1",
  sysname: "Switch-A",
  port: "Gi1/0/1",
  ifindex: 101,
  aggregatePort: null,
  memberPorts: []
});
assert.strictEqual(ordinary.physicalLinks[0].stale, false);
assert.strictEqual(ordinary.physicalLinks[0].lastSeen, 100);
assert.deepStrictEqual(
  ordinary.devices.map((device) => device.kind),
  ["infrastructure", "infrastructure"]
);

// IDENTITY_METADATA_INVARIANCE_PHYSICAL
const physicalIdentityBase = physicalEdge({
  from_sysname: "A-old",
  to_sysname: "B-old",
  protocols: ["cdp"],
  stale: false,
  last_seen: 100
});
const physicalIdentityChanged = physicalEdge({
  edge_type: " physical ",
  from_sysname: "A-new",
  to_sysname: "B-new",
  protocols: ["cdp", "lldp"],
  stale: true,
  last_seen: 200
});
assert.strictEqual(
  projectPhysicalTopology([physicalIdentityBase]).physicalLinks[0].id,
  projectPhysicalTopology([physicalIdentityChanged]).physicalLinks[0].id
);

// ---- Protocol provenance is normalized evidence, never cable count ----
for (const protocol of ["lldp", "cdp", "xdp"]) {
  const result = projectPhysicalTopology([physicalEdge({ protocols: [protocol] })]);
  assert.deepStrictEqual(result.physicalLinks[0].protocols, [protocol]);
  assert.strictEqual(result.physicalLinks.length, 1);
}

const multiProtocol = projectPhysicalTopology([physicalEdge({
  protocols: ["lldp", "cdp", "LLDP", " cdp "]
})]);
assert.deepStrictEqual(multiProtocol.physicalLinks[0].protocols, ["cdp", "lldp"]);
assert.strictEqual(multiProtocol.physicalLinks.length, 1, "two protocols still mean one adjacency");

const missingProtocols = physicalEdge();
delete missingProtocols.protocols;
assert.deepStrictEqual(
  projectPhysicalTopology([missingProtocols]).physicalLinks[0].protocols,
  [],
  "legacy protocol absence stays unknown"
);

const unknownProtocol = projectPhysicalTopology([physicalEdge({
  protocols: ["lldp", "hybrid", "garbage", ""]
})]);
assert.deepStrictEqual(unknownProtocol.physicalLinks[0].protocols, ["lldp"]);
assert.ok(warningCodes(unknownProtocol).every((code) => code === "unknown-protocol"));
assert.strictEqual(unknownProtocol.compatibilityWarnings.length, 3);

// ---- Phase 2 and legacy server attachments keep switch->server semantics ----
const phase2Attachment = {
  edge_type: "server_attachment",
  from_ip: "10.0.0.20",
  from_sysname: "Access-20",
  from_port: "Gi6/0/43",
  from_ifindex: 6043,
  to_ip: "192.168.42.203",
  to_sysname: "sdwan",
  to_port: null,
  to_ifindex: null,
  source: "fdb",
  server_mac: "fc:9d:05:1a:b5:41",
  server_vlan: 42
};
const phase2Server = projectPhysicalTopology([phase2Attachment]);
assert.strictEqual(phase2Server.serverAttachments.length, 1);
assert.deepStrictEqual(phase2Server.serverAttachments[0].switchEndpoint, {
  ip: "10.0.0.20",
  sysname: "Access-20",
  port: "Gi6/0/43",
  ifindex: 6043
});
assert.deepStrictEqual(phase2Server.serverAttachments[0].serverEndpoint, {
  ip: "192.168.42.203",
  sysname: "sdwan",
  port: null,
  ifindex: null
});
assert.strictEqual(phase2Server.serverAttachments[0].source, "fdb");
assert.strictEqual(phase2Server.serverAttachments[0].serverMac, "fc:9d:05:1a:b5:41");
assert.strictEqual(phase2Server.serverAttachments[0].serverVlan, 42);
assert.strictEqual(
  phase2Server.serverAttachments[0].serverEndpoint.port,
  null,
  "the projection never fabricates eth0 or copies the switch port"
);
assert.deepStrictEqual(phase2Server.devices, [
  { ip: "10.0.0.20", sysname: "Access-20", kind: "infrastructure" },
  { ip: "192.168.42.203", sysname: "sdwan", kind: "server" }
]);

const legacyAttachment = { ...phase2Attachment };
delete legacyAttachment.edge_type;
assert.deepStrictEqual(
  projectPhysicalTopology([legacyAttachment]),
  phase2Server,
  "legacy source=fdb uses the established server attachment contract"
);

// IDENTITY_METADATA_INVARIANCE_SERVER_ATTACHMENT
const changedAttachmentMetadata = {
  ...phase2Attachment,
  from_sysname: "Access-Renamed",
  to_sysname: "Server-Renamed",
  source: "FDB",
  server_mac: "00:11:22:33:44:55",
  server_vlan: 99
};
assert.strictEqual(
  projectPhysicalTopology([phase2Attachment]).serverAttachments[0].id,
  projectPhysicalTopology([changedAttachmentMetadata]).serverAttachments[0].id
);

// DUPLICATE_SERVER_ATTACHMENT_DEDUP
const duplicateServer = projectPhysicalTopology([
  phase2Attachment,
  { ...phase2Attachment }
]);
assert.strictEqual(duplicateServer.serverAttachments.length, 1);
assertUniqueIds(duplicateServer.serverAttachments);

const conflictingServerMetadataRows = [
  phase2Attachment,
  {
    ...phase2Attachment,
    from_sysname: "Access-Z",
    to_sysname: "Server-Z",
    server_mac: "ff:ff:ff:ff:ff:ff",
    server_vlan: 99
  }
];
const conflictingServerMetadata = projectPhysicalTopology(conflictingServerMetadataRows);
assert.strictEqual(conflictingServerMetadata.serverAttachments.length, 1);
assert.strictEqual(
  conflictingServerMetadata.serverAttachments[0].serverMac,
  "fc:9d:05:1a:b5:41"
);
assert.strictEqual(conflictingServerMetadata.serverAttachments[0].serverVlan, 42);
assert.ok(warningCodes(conflictingServerMetadata).includes("conflicting-server-mac"));
assert.ok(warningCodes(conflictingServerMetadata).includes("conflicting-server-vlan"));
assert.deepStrictEqual(
  projectPhysicalTopology(conflictingServerMetadataRows.slice().reverse()),
  conflictingServerMetadata
);

// ONE_SIDED_LAG_FROM: partial aggregate evidence remains an ordinary fact.
const oneSidedLagFrom = projectPhysicalTopology([physicalEdge({
  from_aggregate_port: "Po11",
  from_member_ports: ["Gi1/0/1", "Gi1/0/3"],
  to_member_ports: ["Gi1/0/2"]
})]);
assert.strictEqual(oneSidedLagFrom.bundles.length, 0);
assert.strictEqual(oneSidedLagFrom.physicalLinks.length, 1);
assert.strictEqual(oneSidedLagFrom.physicalLinks[0].a.aggregatePort, "Po11");
assert.deepStrictEqual(oneSidedLagFrom.physicalLinks[0].a.memberPorts, ["Gi1/0/1", "Gi1/0/3"]);
assert.strictEqual(oneSidedLagFrom.physicalLinks[0].b.aggregatePort, null);
assert.deepStrictEqual(oneSidedLagFrom.physicalLinks[0].b.memberPorts, ["Gi1/0/2"]);

// ONE_SIDED_LAG_TO: the reverse partial shape is symmetric and never inferred.
const oneSidedLagTo = projectPhysicalTopology([physicalEdge({
  from_member_ports: ["Gi1/0/1"],
  to_aggregate_port: "Po22",
  to_member_ports: ["Gi1/0/2", "Gi1/0/4"]
})]);
assert.strictEqual(oneSidedLagTo.bundles.length, 0);
assert.strictEqual(oneSidedLagTo.physicalLinks.length, 1);
assert.strictEqual(oneSidedLagTo.physicalLinks[0].a.aggregatePort, null);
assert.deepStrictEqual(oneSidedLagTo.physicalLinks[0].a.memberPorts, ["Gi1/0/1"]);
assert.strictEqual(oneSidedLagTo.physicalLinks[0].b.aggregatePort, "Po22");
assert.deepStrictEqual(oneSidedLagTo.physicalLinks[0].b.memberPorts, ["Gi1/0/2", "Gi1/0/4"]);

// ---- A real Po11 probe: one bundle, independent endpoint member facts ----
const lagRows = [
  physicalEdge({
    from_ip: "192.168.10.11",
    from_sysname: "Access-Stack",
    from_port: "Te1/0/2",
    from_ifindex: 102,
    from_aggregate_port: "Po11",
    from_member_ports: [" Te2/0/2 ", "Te1/0/2", "Te1/0/2", "", 42],
    to_ip: "192.168.10.254",
    to_sysname: "Core",
    to_port: "Te1/0/1",
    to_ifindex: 101,
    to_aggregate_port: "Po11",
    to_member_ports: ["Te2/0/1", "Te1/0/1"],
    protocols: ["lldp"],
    stale: true,
    last_seen: 100
  }),
  physicalEdge({
    from_ip: "192.168.10.11",
    from_sysname: "Access-Stack",
    from_port: "Te2/0/2",
    from_ifindex: 202,
    from_aggregate_port: "Po11",
    from_member_ports: ["Te1/0/2", "Te2/0/2"],
    to_ip: "192.168.10.254",
    to_sysname: "Core",
    to_port: "Te2/0/1",
    to_ifindex: 201,
    to_aggregate_port: "Po11",
    to_member_ports: ["Te1/0/1", "Te2/0/1"],
    protocols: ["cdp", "lldp"],
    stale: false,
    last_seen: 200
  })
];
const lag = projectPhysicalTopology(lagRows);
assert.strictEqual(lag.physicalLinks.length, 0);
assert.strictEqual(lag.bundles.length, 1);
assert.strictEqual(lag.bundles[0].kind, "bundle");
assert.strictEqual(lag.bundles[0].sourceEdgeCount, 2);
assert.deepStrictEqual(lag.bundles[0].a, {
  ip: "192.168.10.11",
  sysname: "Access-Stack",
  aggregatePort: "Po11",
  memberPorts: ["Te1/0/2", "Te2/0/2"]
});
assert.deepStrictEqual(lag.bundles[0].b, {
  ip: "192.168.10.254",
  sysname: "Core",
  aggregatePort: "Po11",
  memberPorts: ["Te1/0/1", "Te2/0/1"]
});
assert.deepStrictEqual(lag.bundles[0].protocols, ["cdp", "lldp"]);
assert.strictEqual(lag.bundles[0].stale, false, "one explicit fresh source makes the bundle fresh");
assert.strictEqual(lag.bundles[0].hasStaleMembers, true);
assert.strictEqual(lag.bundles[0].lastSeen, 200);

// IDENTITY_METADATA_INVARIANCE_BUNDLE
const changedBundleMetadata = projectPhysicalTopology([{
  ...lagRows[0],
  from_sysname: "Access-Renamed",
  to_sysname: "Core-Renamed",
  from_member_ports: ["Te9/0/9"],
  to_member_ports: ["Te8/0/8"],
  protocols: ["cdp", "lldp"],
  stale: false,
  last_seen: 999
}]);
assert.strictEqual(changedBundleMetadata.bundles[0].id, lag.bundles[0].id);

const forbiddenPairingKeys = new Set(["pairs", "memberPairs", "cables"]);
assert.ok(
  objectKeysDeep(lag.bundles[0]).every((key) => !forbiddenPairingKeys.has(key)),
  "exact member cable pairing is CANNOT INFER and is never generated"
);

const allStaleLag = projectPhysicalTopology(lagRows.map((edge) => ({ ...edge, stale: true })));
assert.strictEqual(allStaleLag.bundles[0].stale, true);
assert.strictEqual(allStaleLag.bundles[0].hasStaleMembers, true);

const unknownStaleLagRows = lagRows.map((edge) => {
  const copy = { ...edge };
  delete copy.stale;
  return copy;
});
const unknownStaleLag = projectPhysicalTopology(unknownStaleLagRows);
assert.strictEqual(unknownStaleLag.bundles[0].stale, null);
assert.strictEqual(unknownStaleLag.bundles[0].hasStaleMembers, false);

const partiallyKnownStaleLag = projectPhysicalTopology([
  { ...unknownStaleLagRows[0], stale: true },
  unknownStaleLagRows[1]
]);
assert.strictEqual(
  partiallyKnownStaleLag.bundles[0].stale,
  null,
  "one stale plus one unknown source cannot claim the entire bundle is stale"
);
assert.strictEqual(partiallyKnownStaleLag.bundles[0].hasStaleMembers, true);

const invalidTimestampLag = projectPhysicalTopology([
  lagRows[0],
  { ...lagRows[1], last_seen: "not-a-number" }
]);
assert.strictEqual(invalidTimestampLag.bundles[0].lastSeen, 100);
const noSafeTimestampLag = projectPhysicalTopology(lagRows.map((edge) => ({
  ...edge,
  last_seen: "not-a-number"
})));
assert.strictEqual(noSafeTimestampLag.bundles[0].lastSeen, null);

// DUPLICATE_PHYSICAL_DEDUP and REVERSE_DUPLICATE_PHYSICAL_DEDUP
const exactDuplicatePhysical = projectPhysicalTopology([
  physicalEdge(),
  physicalEdge()
]);
assert.strictEqual(exactDuplicatePhysical.physicalLinks.length, 1);
assertUniqueIds(exactDuplicatePhysical.physicalLinks);

const reverseDuplicatePhysical = projectPhysicalTopology([
  physicalEdge(),
  reversePhysicalEdge(physicalEdge())
]);
assert.strictEqual(reverseDuplicatePhysical.physicalLinks.length, 1);
assertUniqueIds(reverseDuplicatePhysical.physicalLinks);

const mergedDuplicatePhysical = projectPhysicalTopology([
  physicalEdge({ protocols: ["cdp"], stale: true, last_seen: 100 }),
  physicalEdge({ protocols: ["lldp"], stale: false, last_seen: 200 }),
  reversePhysicalEdge(physicalEdge({ protocols: ["xdp"], stale: true, last_seen: 150 }))
]);
assert.strictEqual(mergedDuplicatePhysical.physicalLinks.length, 1);
assert.deepStrictEqual(mergedDuplicatePhysical.physicalLinks[0].protocols, ["cdp", "lldp", "xdp"]);
assert.strictEqual(mergedDuplicatePhysical.physicalLinks[0].stale, false);
assert.strictEqual(mergedDuplicatePhysical.physicalLinks[0].lastSeen, 200);

const duplicateAllStale = projectPhysicalTopology([
  physicalEdge({ stale: true }),
  physicalEdge({ stale: true })
]);
assert.strictEqual(duplicateAllStale.physicalLinks[0].stale, true);
const duplicatePartiallyKnownStale = physicalEdge({ stale: true });
const duplicateUnknownStale = physicalEdge();
delete duplicateUnknownStale.stale;
assert.strictEqual(
  projectPhysicalTopology([duplicatePartiallyKnownStale, duplicateUnknownStale]).physicalLinks[0].stale,
  null
);

// ---- Parallel non-LAG links stay distinct ----
const parallelEdges = [
  physicalEdge(),
  physicalEdge({
    from_port: "Gi1/0/2",
    from_ifindex: 102,
    to_port: "Gi1/0/3",
    to_ifindex: 203
  })
];
const parallel = projectPhysicalTopology(parallelEdges);
assert.strictEqual(parallel.physicalLinks.length, 2);
assert.strictEqual(new Set(parallel.physicalLinks.map((link) => link.id)).size, 2);
assert.strictEqual(parallel.bundles.length, 0, "device pair alone is never bundle evidence");

// ---- Partial accepted adjacencies degrade field by field ----
const knownPortUnknownIndex = projectPhysicalTopology([physicalEdge({ from_ifindex: null })]);
assert.strictEqual(knownPortUnknownIndex.physicalLinks[0].a.port, "Gi1/0/1");
assert.strictEqual(knownPortUnknownIndex.physicalLinks[0].a.ifindex, null);

const knownDeviceUnknownPort = projectPhysicalTopology([physicalEdge({
  from_port: null,
  from_ifindex: null
})]);
assert.strictEqual(knownDeviceUnknownPort.physicalLinks[0].a.ip, "10.0.0.1");
assert.strictEqual(knownDeviceUnknownPort.physicalLinks[0].a.port, null);
assert.strictEqual(knownDeviceUnknownPort.physicalLinks[0].a.ifindex, null);

const stalePhysical = projectPhysicalTopology([physicalEdge({ stale: true, last_seen: 77 })]);
assert.strictEqual(stalePhysical.physicalLinks[0].stale, true);
assert.strictEqual(stalePhysical.physicalLinks[0].lastSeen, 77);
const missingStaleEdge = physicalEdge();
delete missingStaleEdge.stale;
assert.strictEqual(projectPhysicalTopology([missingStaleEdge]).physicalLinks[0].stale, null);

// ORDINARY_LAST_SEEN_INVALID_TO_NULL
assert.strictEqual(
  projectPhysicalTopology([physicalEdge({ last_seen: 123.5 })]).physicalLinks[0].lastSeen,
  123.5
);
for (const invalidLastSeen of ["123.5", "not-a-number", Infinity, -Infinity, NaN, {}, [], true]) {
  assert.strictEqual(
    projectPhysicalTopology([physicalEdge({ last_seen: invalidLastSeen })]).physicalLinks[0].lastSeen,
    null
  );
}
const missingLastSeenEdge = physicalEdge();
delete missingLastSeenEdge.last_seen;
assert.strictEqual(projectPhysicalTopology([missingLastSeenEdge]).physicalLinks[0].lastSeen, null);
assert.strictEqual(projectPhysicalTopology([physicalEdge({ last_seen: null })]).physicalLinks[0].lastSeen, null);

// ---- Legacy, malformed, and unknown types are local compatibility concerns ----
const legacyPhysical = physicalEdge();
delete legacyPhysical.edge_type;
assert.strictEqual(projectPhysicalTopology([legacyPhysical]).physicalLinks.length, 1);

const malformedAndValid = projectPhysicalTopology([
  { edge_type: "physical", from_ip: "10.0.0.9", to_ip: "" },
  physicalEdge()
]);
assert.strictEqual(malformedAndValid.physicalLinks.length, 1);
assert.ok(warningCodes(malformedAndValid).includes("malformed-edge"));

const unknownType = projectPhysicalTopology([
  physicalEdge({ edge_type: "something-unknown" }),
  physicalEdge()
]);
assert.strictEqual(unknownType.physicalLinks.length, 1);
assert.ok(warningCodes(unknownType).includes("unknown-edge-type"));

const invalidRoot = projectPhysicalTopology({ edges: [] });
assert.deepStrictEqual(invalidRoot.devices, []);
assert.deepStrictEqual(warningCodes(invalidRoot), ["invalid-edges-root"]);

// ---- IDs, endpoint orientation, and the complete projection ignore input order ----
const forwardProjection = projectPhysicalTopology([physicalEdge()]);
const reverseProjection = projectPhysicalTopology([reversePhysicalEdge(physicalEdge())]);
assert.deepStrictEqual(reverseProjection, forwardProjection);
assert.strictEqual(
  reverseProjection.physicalLinks[0].id,
  forwardProjection.physicalLinks[0].id
);

const reorderedLag = projectPhysicalTopology(lagRows.slice().reverse());
assert.deepStrictEqual(reorderedLag, lag);
assert.strictEqual(reorderedLag.bundles[0].id, lag.bundles[0].id);

// ---- Devices dedupe, role conflicts, and display-name conflicts are deterministic ----
const conflictingNames = [
  physicalEdge({ from_sysname: "Zulu", to_ip: "10.0.0.3", to_sysname: "Third" }),
  physicalEdge({
    from_sysname: "Alpha",
    from_port: "Gi1/0/9",
    from_ifindex: 109,
    to_ip: "10.0.0.4",
    to_sysname: "Fourth",
    to_port: "Gi1/0/4",
    to_ifindex: 204
  })
];
const namesProjection = projectPhysicalTopology(conflictingNames);
assert.strictEqual(namesProjection.devices.filter((device) => device.ip === "10.0.0.1").length, 1);
assert.strictEqual(namesProjection.devices.find((device) => device.ip === "10.0.0.1").sysname, "Alpha");
assert.ok(warningCodes(namesProjection).includes("conflicting-device-sysname"));
assert.ok(namesProjection.physicalLinks.every((link) => (
  link.a.ip !== "10.0.0.1" || link.a.sysname === "Alpha"
) && (
  link.b.ip !== "10.0.0.1" || link.b.sysname === "Alpha"
)));

const roleConflictRows = [
  physicalEdge({ to_ip: "192.168.42.203", to_sysname: "Zulu-Infrastructure" }),
  phase2Attachment
];
const roleConflict = projectPhysicalTopology(roleConflictRows);
assert.ok(warningCodes(roleConflict).includes("conflicting-device-kind"));
assert.ok(warningCodes(roleConflict).includes("conflicting-device-sysname"));
assert.strictEqual(
  roleConflict.devices.find((device) => device.ip === "192.168.42.203").kind,
  "infrastructure",
  "a conflicting role uses a documented stable lexical choice and emits a warning"
);
assert.ok(roleConflict.devices.every((device) => ["infrastructure", "server"].includes(device.kind)));

// DEVICE_CONFLICT_FORWARD_REVERSE_DETERMINISM
const reversedRoleConflict = projectPhysicalTopology(roleConflictRows.slice().reverse());
assert.deepStrictEqual(roleConflict.devices, reversedRoleConflict.devices);
assert.deepStrictEqual(
  roleConflict.compatibilityWarnings,
  reversedRoleConflict.compatibilityWarnings
);
assert.deepStrictEqual(roleConflict, reversedRoleConflict);

// A mixed fixture locks complete reorder determinism, including warnings.
const mixedEdges = [
  ...parallelEdges,
  ...lagRows,
  phase2Attachment,
  physicalEdge({
    from_ip: "10.0.0.8",
    from_sysname: "Eight-Z",
    from_port: "Gi8",
    from_ifindex: 8,
    to_ip: "10.0.0.9",
    to_sysname: "Nine",
    to_port: "Gi9",
    to_ifindex: 9,
    protocols: ["lldp", "bad-protocol"]
  }),
  physicalEdge({
    from_ip: "10.0.0.8",
    from_sysname: "Eight-A",
    from_port: "Gi10",
    from_ifindex: 10,
    to_ip: "10.0.0.10",
    to_sysname: "Ten",
    to_port: "Gi10",
    to_ifindex: 10
  }),
  { edge_type: "physical", from_ip: "10.0.0.77", to_ip: null },
  physicalEdge({ edge_type: "future-link", from_ip: "10.0.0.88", to_ip: "10.0.0.89" })
];
const mixedBefore = clone(mixedEdges);
const mixedForward = projectPhysicalTopology(mixedEdges);
assert.deepStrictEqual(mixedEdges, mixedBefore, "projection never mutates raw edges");
const mixedReverse = projectPhysicalTopology(mixedEdges.slice().reverse());
assert.deepStrictEqual(mixedReverse, mixedForward, "complete output is independent of input order");
assert.deepStrictEqual(
  mixedForward.compatibilityWarnings,
  mixedForward.compatibilityWarnings.slice().sort((a, b) =>
    JSON.stringify(a).localeCompare(JSON.stringify(b))
  ),
  "compatibility warnings have a stable order"
);
assert.ok(mixedForward.compatibilityWarnings.every((warning) => !Object.hasOwn(warning, "inputIndex")));

// GLOBAL_ID_UNIQUENESS across duplicates, parallels, bundles, and attachments.
const secondBundleRow = {
  ...lagRows[0],
  from_aggregate_port: "Po12",
  to_aggregate_port: "Po12"
};
const globalIdentityRows = [
  physicalEdge(),
  physicalEdge(),
  reversePhysicalEdge(physicalEdge()),
  parallelEdges[1],
  ...lagRows,
  secondBundleRow,
  phase2Attachment,
  { ...phase2Attachment }
];
const globalIdentityProjection = projectPhysicalTopology(globalIdentityRows);
assert.strictEqual(globalIdentityProjection.physicalLinks.length, 2);
assert.strictEqual(globalIdentityProjection.bundles.length, 2);
assert.strictEqual(globalIdentityProjection.serverAttachments.length, 1);
assertUniqueIds(globalIdentityProjection.physicalLinks);
assertUniqueIds(globalIdentityProjection.bundles);
assertUniqueIds(globalIdentityProjection.serverAttachments);
assert.deepStrictEqual(
  projectPhysicalTopology(globalIdentityRows.slice().reverse()),
  globalIdentityProjection
);

console.log("bigscreen physical topology projection tests passed");
