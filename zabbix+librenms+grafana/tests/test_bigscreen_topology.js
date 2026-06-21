const assert = require("assert");
const path = require("path");

global.window = {
  BIGSCREEN_CONFIG: {
    ispNames: "ISP1,ISP2",
    ispIps: "ISP1:203.170.210.114,ISP2:202.133.189.82",
    ispMaxBandwidthMbps: "ISP1:500,ISP2:500,ISP3:300",
    ispAutoDiscovery: "false",
    serverTargets: "Server:172.25.12.252",
    stageDeviceFilter: "stage,aruba",
    wanIfFilter: "telecom,telcom,unicom,isp,WAN"
  },
  BIGSCREEN_QUERIES: {},
  BIGSCREEN_PAGES: []
};

const { parseIspBandwidthConfig } = require(path.resolve(__dirname, "../bigscreen/utils.js"));
const { ispChartMaxBps, getConfiguredIspNames, getIspNames } = require(path.resolve(__dirname, "../bigscreen/api.js"));
const {
  buildTopologyLayers,
  topologyLayout,
  renderTopologySvg
} = require(path.resolve(__dirname, "../bigscreen/topology.js"));

assert.deepStrictEqual(parseIspBandwidthConfig("ISP1:500,ISP2:500,ISP3:300").perIsp.ISP3, { down: 300, up: 300 });
assert.strictEqual(ispChartMaxBps("ISP1"), 500 * 1000 * 1000);
assert.strictEqual(ispChartMaxBps("ISP3"), 300 * 1000 * 1000);
window.BIGSCREEN_CONFIG.ispMaxBandwidthMbps = "ISP1:500/300,ISP2:800";
assert.strictEqual(ispChartMaxBps("ISP1"), 500 * 1000 * 1000);
assert.strictEqual(ispChartMaxBps("ISP2"), 800 * 1000 * 1000);
window.BIGSCREEN_CONFIG.ispMaxBandwidthMbps = "ISP1:500,ISP2:500,ISP3:300";

// ---- ISP name resolution: empty names must mean "pure auto-discover", not the
//      ISP1,ISP2 default, so portable setups don't grow empty placeholder panels ----
window.BIGSCREEN_CONFIG.ispNames = "";
assert.deepStrictEqual(getConfiguredIspNames(), [], "empty ISP names = no explicit names (auto-discover only)");
assert.deepStrictEqual(getIspNames(), ["ISP1", "ISP2"], "static/fallback path still defaults to ISP1,ISP2 when unset");
window.BIGSCREEN_CONFIG.ispNames = "telcom,unicom";
assert.deepStrictEqual(getConfiguredIspNames(), ["telcom", "unicom"], "explicit names are used verbatim");
assert.deepStrictEqual(getIspNames(), ["telcom", "unicom"], "explicit names override the default");
window.BIGSCREEN_CONFIG.ispNames = "ISP1,ISP2";

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

// ---- auto-discover ISP: empty names must NOT inject ISP1/ISP2 placeholder nodes ----
window.BIGSCREEN_CONFIG.ispNames = "";
window.BIGSCREEN_CONFIG.ispAutoDiscovery = "true";
window.BIGSCREEN_CONFIG.ispIps = "";
const autoIspLayers = buildTopologyLayers([
  target("infra-isp-ping", "telcom", "219.140.134.161"),
  target("infra-isp-ping", "unicom", "113.57.164.49"),
  target("infra-core-ping", "Core", "192.168.10.254")
]);
assert.deepStrictEqual(
  autoIspLayers.isps.map((node) => node.name).sort(),
  ["telcom", "unicom"],
  "auto-discover shows only discovered ISPs, no ISP1/ISP2 placeholders"
);
window.BIGSCREEN_CONFIG.ispNames = "ISP1,ISP2";
window.BIGSCREEN_CONFIG.ispAutoDiscovery = "false";
window.BIGSCREEN_CONFIG.ispIps = "ISP1:203.170.210.114,ISP2:202.133.189.82";

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
assert.deepStrictEqual(coreStageLinks[0].labelLines, [
  "Gi1/0/23, Gi1/0/24",
  "Gi1/0/23, Gi1/0/24"
]);
assert.strictEqual(coreStageLinks[0].severity, "warn");
assert.ok(coreStageLinks[0].busLink);
assert.ok(coreStageLinks[0].aggregated, "a multi-port bundle is flagged aggregated (drawn thicker)");
assert.strictEqual(layout.coreBus.severity, "good");

const serverNode = layout.nodes.find((node) => node.kind === "server");
const coreNode = layout.nodes.find((node) => node.kind === "core");
const distNode = layout.nodes.find((node) => node.kind === "dist");
const centerX = (node) => node.x + node.w / 2;
assert.ok(serverNode);
// Servers get their own row between the core and the access-switch (dist) row,
// instead of flanking the core on the same line.
assert.ok(serverNode.y > coreNode.y, "server row should sit below the core row");
assert.ok(serverNode.y < distNode.y, "server row should sit above the access-switch row");
assert.ok(serverNode.x > coreNode.x + coreNode.w, "a single server sits to the right of core, leaving the center trunk clear");
assert.ok(layout.links.some((link) => (
  [link.from.kind, link.to.kind].includes("core") &&
  [link.from.kind, link.to.kind].includes("server")
)));

window.BIGSCREEN_CONFIG.serverTargets = [
  "server5:192.168.141.100",
  "server4:192.168.141.18",
  "server2:192.168.141.16",
  "server3:192.168.141.17",
  "server1:192.168.141.15"
].join(",");
const multiServerTargets = [
  target("infra-core-ping", "PMGO-core", "192.168.10.254"),
  target("infra-dist-ping", "stage1", "192.168.10.3"),
  target("infra-srv-ping", "server5", "192.168.141.100"),
  target("infra-srv-ping", "server4", "192.168.141.18"),
  target("infra-srv-ping", "server2", "192.168.141.16"),
  target("infra-srv-ping", "server3", "192.168.141.17"),
  target("infra-srv-ping", "server1", "192.168.141.15")
];
const multiLayout = topologyLayout(buildTopologyLayers(multiServerTargets), 1365, 620, []);
const multiCore = multiLayout.nodes.find((node) => node.kind === "core");
const multiServers = multiLayout.nodes.filter((node) => node.kind === "server");
assert.strictEqual(multiServers.length, 5);
assert.ok(multiServers.some((node) => centerX(node) < multiCore.x), "server row uses the left side of core");
assert.ok(multiServers.some((node) => centerX(node) > multiCore.x + multiCore.w), "server row uses the right side of core");
assert.ok(multiServers.every((node) => (
  node.x + node.w < multiCore.x - 8 ||
  node.x > multiCore.x + multiCore.w + 8
)), "no server is centered under the core trunk");

const svg = renderTopologySvg(layout, 1365);
assert.ok(!svg.includes("topology-link-rate"));
assert.ok(!svg.includes("uplinks"));
assert.ok(!svg.includes("Core: Gi1/0/23"));
assert.ok(!svg.includes("stage1: Gi1/0/23"));
assert.ok(svg.includes(">Gi1/0/23, Gi1/0/24</text>"));
assert.strictEqual((svg.match(/>Gi1\/0\/23, Gi1\/0\/24<\/text>/g) || []).length, 2);
assert.ok(svg.includes('data-base-width="1365"'));
assert.ok(svg.includes("topology-backbone"));
assert.ok(!svg.includes("/topology/rates.json"));

// ---- dist hierarchy: a switch uplinked to ANOTHER switch sits in a layer below it ----
const hierTargets = [
  target("infra-core-ping", "PMGO-core", "172.25.10.254"),
  target("infra-dist-ping", "PMGO-FOH", "172.25.10.24"),
  target("infra-dist-ping", "PMGO-JIESHOU-RIGHT", "172.25.10.23"),
];
const hierEdges = [
  { from_ip: "172.25.10.24", from_port: "Gi0/9", to_ip: "172.25.10.254", to_port: "Te2/1/7" },
  { from_ip: "172.25.10.24", from_port: "Gi0/6", to_ip: "172.25.10.23", to_port: "Gi0/23" },
];
const hierLayout = topologyLayout(buildTopologyLayers(hierTargets), 1365, 620, hierEdges);
const fohNode = hierLayout.nodes.find((n) => n.ip === "172.25.10.24");
const jieNode = hierLayout.nodes.find((n) => n.ip === "172.25.10.23");
assert.ok(fohNode && jieNode, "both access switches are placed");
assert.ok(jieNode.y > fohNode.y, "child switch sits in a row below its parent");
assert.ok(hierLayout.coreBus, "core bus exists for direct core child links");
assert.strictEqual(
  Math.round(jieNode.y - (fohNode.y + fohNode.h)),
  Math.round(fohNode.y - hierLayout.coreBus.y),
  "child link gap matches the core-bus link gap"
);
const coreToChild = hierLayout.links.some((l) =>
  [l.from.ip, l.to.ip].includes("172.25.10.254") && [l.from.ip, l.to.ip].includes("172.25.10.23"));
assert.ok(!coreToChild, "no synthetic core->child link when a real uplink exists");

console.log("bigscreen topology tests passed");
