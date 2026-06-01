const assert = require("assert");
const path = require("path");

global.window = {
  BIGSCREEN_CONFIG: {
    ispNames: "ISP1,ISP2",
    ispIps: "ISP1:203.170.210.114,ISP2:202.133.189.82",
    ispAutoDiscovery: "false",
    serverTargets: "Server:172.25.12.252",
    stageDeviceFilter: "stage,aruba",
    wanIfFilter: "telecom,telcom,unicom,isp,WAN"
  },
  BIGSCREEN_QUERIES: {},
  BIGSCREEN_PAGES: [],
  __BIGSCREEN_TEST_MODE__: true
};

require(path.resolve(__dirname, "../bigscreen/app.js"));

const { buildTopologyLayers, topologyLayout, renderTopologySvg } = window.__BIGSCREEN_TOPOLOGY_TESTS__;

const target = (job, displayName, targetIp, success = true, latency = 0.002) => ({
  job,
  displayName,
  instance: displayName,
  targetIp,
  success,
  latency
});

const targets = [
  target("infra-isp-ping", "ISP1", "203.170.210.114"),
  target("infra-isp-ping", "ISP1-old", "203.170.201.114"),
  target("infra-isp-ping", "ISP2", "202.133.189.82"),
  target("infra-fw-ping", "FW1", "172.25.9.5"),
  target("infra-core-ping", "Core", "172.25.10.254"),
  target("infra-dist-ping", "stage1", "172.25.10.3", true, 0.04),
  target("infra-srv-ping", "Server-old", "172.25.10.254"),
  target("infra-srv-ping", "Server", "172.25.12.252")
];

const layers = buildTopologyLayers(targets);
assert.deepStrictEqual(layers.isps.map((node) => node.ip), ["203.170.210.114", "202.133.189.82"]);
assert.deepStrictEqual(layers.servers.map((node) => node.ip), ["172.25.12.252"]);

const noisyRawEdges = [
  { from_ip: "172.25.10.254", from_port: "Gi1/0/23", to_ip: "172.25.10.3", to_port: "Gi1/0/23" },
  { from_ip: "172.25.10.254", from_port: "Gi1/0/24", to_ip: "172.25.10.3", to_port: "Gi1/0/24" },
  { from_ip: "172.25.10.3", from_port: "Gi1/0/23", to_ip: "172.25.10.254", to_port: "Gi1/0/23" },
  { from_ip: "172.25.10.3", from_port: "Gi1/0/24", to_ip: "172.25.10.254", to_port: "Gi1/0/24" },
  { from_ip: "172.25.10.254", from_port: "Gi1/0/25", to_ip: "172.25.10.3", to_port: "to sw-core" },
  { from_ip: "172.25.10.3", from_port: "to sw-core", to_ip: "172.25.10.254", to_port: "Gi1/0/26" }
];

const layout = topologyLayout(layers, 1365, 620, noisyRawEdges);
const coreStageLinks = layout.links.filter((link) => (
  [link.from.kind, link.to.kind].includes("core") &&
  [link.from.kind, link.to.kind].includes("dist")
));
assert.strictEqual(coreStageLinks.length, 1);
assert.strictEqual(coreStageLinks[0].label, "2 uplinks");
assert.strictEqual(coreStageLinks[0].severity, "warn");
assert.ok(coreStageLinks[0].busLink);
assert.strictEqual(layout.coreBus.severity, "good");

const serverNode = layout.nodes.find((node) => node.kind === "server");
const coreNode = layout.nodes.find((node) => node.kind === "core");
const distNode = layout.nodes.find((node) => node.kind === "dist");
assert.ok(serverNode);
// Servers get their own row between the core and the access-switch (dist) row,
// instead of flanking the core on the same line.
assert.ok(serverNode.y > coreNode.y, "server row should sit below the core row");
assert.ok(serverNode.y < distNode.y, "server row should sit above the access-switch row");
assert.ok(layout.links.some((link) => (
  [link.from.kind, link.to.kind].includes("core") &&
  [link.from.kind, link.to.kind].includes("server")
)));

const svg = renderTopologySvg(layout, 1365);
assert.ok(!svg.includes("topology-link-rate"));
assert.ok(svg.includes("topology-backbone"));
assert.ok(!svg.includes("/topology/rates.json"));

console.log("bigscreen topology tests passed");
