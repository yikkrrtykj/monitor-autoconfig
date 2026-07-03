const assert = require("assert");
const path = require("path");

const {
  readinessScore,
  summarizePlayers,
  summarizeTargets,
  summarizeServices,
  buildReadinessChecks,
  lintSwitchConfig,
  lintSwitchPair,
  lintSwitchScene
} = require(path.resolve(__dirname, "../bigscreen/platform.js"));

const players = [
  { team: 1, seat: 1, ip: "10.1.1.11", success: true, latency: 0.012 },
  { team: 1, seat: 2, ip: "10.1.1.12", success: false, latency: null },
  { team: 1, seat: 2, ip: "10.1.1.13", success: true, latency: 0.091 }
];
const seatSummary = summarizePlayers(players, 4);
assert.strictEqual(seatSummary.seats, 2);
assert.strictEqual(seatSummary.missing, 2);
assert.strictEqual(seatSummary.duplicateSeats, 1);
assert.strictEqual(seatSummary.highLatency, 1);

const targetSummary = summarizeTargets([
  { job: "infra-core-ping", success: true, displayName: "core" },
  { job: "infra-fw-ping", success: true, displayName: "fw" },
  { job: "infra-dist-ping", success: false, displayName: "stage1" }
]);
const serviceSummary = summarizeServices([
  { metric: { job: "prometheus" }, value: 1 },
  { metric: { job: "player-ping" }, value: 0 }
]);
const checks = buildReadinessChecks({ seatSummary, targetSummary, serviceSummary, configRisks: [], topologyFindings: [] });
const score = readinessScore(checks);
assert.strictEqual(score.level, "bad");
assert.ok(score.score < 100);

const riskyConfig = `
logging host 192.168.41.253
interface GigabitEthernet1/0/1
 description player
 switchport access vlan 41
 switchport mode access
 spanning-tree portfast edge
`;
const riskyIssues = lintSwitchConfig(riskyConfig);
assert.ok(riskyIssues.some((item) => item.label.includes("BPDU Guard")), "missing BPDU Guard is flagged");
assert.ok(riskyIssues.some((item) => item.label.includes("广播风暴")), "missing broadcast storm-control is flagged");

const protectedConfig = `
no vstack
logging host 192.168.41.253
errdisable recovery cause bpduguard
errdisable recovery cause storm-control
errdisable recovery interval 60
interface GigabitEthernet1/0/1
 description player
 switchport access vlan 41
 switchport mode access
 storm-control broadcast level 1.00 0.50
 storm-control action shutdown
 spanning-tree portfast edge
 spanning-tree bpduguard enable
`;
const protectedIssues = lintSwitchConfig(protectedConfig);
assert.ok(!protectedIssues.some((item) => item.level === "bad"), "protected access port has no bad issues");

const badTrunk = `
no vstack
logging host 192.168.41.253
interface GigabitEthernet1/0/48
 description uplink
 switchport mode trunk
 spanning-tree bpduguard enable
`;
const trunkIssues = lintSwitchConfig(badTrunk);
assert.ok(trunkIssues.some((item) => item.level === "bad" && item.label.includes("BPDU Guard")), "trunk BPDU Guard is flagged");

// Many access ports failing the same check collapse into one ranged card.
let manyPorts = "no vstack\nlogging host 192.168.41.253\n";
for (let i = 1; i <= 10; i++) {
  manyPorts += `interface GigabitEthernet1/0/${i}\n switchport access vlan 41\n switchport mode access\n spanning-tree portfast edge\n storm-control broadcast level 1.0\n storm-control action shutdown\n`;
}
const manyIssues = lintSwitchConfig(manyPorts);
const bpduCards = manyIssues.filter((item) => item.label.includes("BPDU Guard"));
assert.strictEqual(bpduCards.length, 1, "10 ports missing BPDU Guard collapse into one card");
assert.strictEqual(bpduCards[0].label, "Gi1/0/1-10 BPDU Guard", "grouped card uses a compact interface range");

// Core/dist coordination: an access VLAN not allowed on the uplink trunk is flagged.
const vlanMismatch = `
no vstack
logging host 192.168.41.253
spanning-tree portfast bpduguard default
interface GigabitEthernet1/0/1
 switchport access vlan 41
 switchport mode access
 storm-control broadcast level 1.0
 storm-control action shutdown
interface GigabitEthernet1/0/48
 description uplink to-core
 switchport mode trunk
 switchport trunk allowed vlan 40
`;
const vlanIssues = lintSwitchConfig(vlanMismatch);
assert.ok(vlanIssues.some((item) => item.level === "bad" && item.label.includes("放行 VLAN")), "access VLAN missing from uplink trunk is flagged");

// A trunk that allows the access VLAN does not trip the coordination check.
const vlanOk = vlanMismatch.replace("allowed vlan 40", "allowed vlan 40,41");
assert.ok(!lintSwitchConfig(vlanOk).some((item) => item.label.includes("放行 VLAN")), "allowed access VLAN is not flagged");

// Core/dist pair: the core must permit the VLANs a distribution switch serves.
const coreCfg = `
no vstack
interface GigabitEthernet1/0/1
 description to-dist
 switchport mode trunk
 switchport trunk allowed vlan 40
`;
const distCfg = `
no vstack
logging host 192.168.41.253
spanning-tree portfast bpduguard default
interface GigabitEthernet1/0/1
 switchport access vlan 41
 switchport mode access
 storm-control broadcast level 1.0
 storm-control action shutdown
interface GigabitEthernet1/0/48
 description uplink to-core
 switchport mode trunk
 switchport trunk allowed vlan 41
`;
const pairIssues = lintSwitchPair(coreCfg, distCfg);
assert.ok(pairIssues.some((item) => item.level === "bad" && item.label.includes("核心放行 VLAN")), "core missing a distribution VLAN is flagged");
// When the core permits the VLAN, no core-coordination bad is raised.
const coreOk = coreCfg.replace("allowed vlan 40", "allowed vlan 40,41");
assert.ok(!lintSwitchPair(coreOk, distCfg).some((item) => item.label.includes("核心放行 VLAN")), "core permitting the VLAN is not flagged");
// No core reference -> only the distribution's own checks run (no cross-check).
assert.ok(!lintSwitchPair("", distCfg).some((item) => item.label.includes("核心放行 VLAN")), "no core reference means no cross-check");

// Scene lint checks BOTH panes and tags each finding with its source.
const sceneNoLog = "no vstack\ninterface GigabitEthernet1/0/1\n switchport mode trunk\n switchport trunk allowed vlan 40,41\n";
const scene = lintSwitchScene(sceneNoLog, distCfg);
assert.ok(scene.some((item) => item.source === "核心"), "core pane is linted and tagged");
assert.ok(scene.some((item) => item.source === "分线"), "distribution pane is linted and tagged");
// Core alone is still checked (its own missing logging host shows up).
const coreOnly = lintSwitchScene(sceneNoLog, "");
assert.ok(coreOnly.length && coreOnly.every((item) => item.source === "核心"), "core-only scene lints just the core");

// Core role skips edge-only checks (BPDU Guard / uplink-VLAN) but keeps globals.
const coreWithAccessPorts = `
interface GigabitEthernet1/0/1
 switchport access vlan 20
 switchport mode access
interface GigabitEthernet1/0/2
 description to-dist
 switchport mode trunk
 switchport trunk allowed vlan 20
`;
const coreLint = lintSwitchConfig(coreWithAccessPorts, { role: "core" });
assert.ok(!coreLint.some((item) => item.label.includes("BPDU Guard")), "core role does not flag missing BPDU Guard");
assert.ok(!coreLint.some((item) => item.label.includes("放行 VLAN")), "core role does not apply the uplink-VLAN rule");
assert.ok(coreLint.some((item) => item.label.includes("no vstack")), "core role still runs global hygiene checks");
// Same config as an access switch DOES flag the edge issues.
const asAccess = lintSwitchConfig(coreWithAccessPorts);
assert.ok(asAccess.some((item) => item.label.includes("BPDU Guard")), "access role still flags BPDU Guard");

// Distribution switches should run DHCP snooping; missing it is flagged, but the
// core is never nagged about it.
const noSnoop = `
no vstack
logging host 192.168.41.253
spanning-tree portfast bpduguard default
interface GigabitEthernet1/0/1
 switchport access vlan 41
 switchport mode access
 storm-control broadcast level 1.0
 storm-control action shutdown
`;
assert.ok(lintSwitchConfig(noSnoop).some((item) => item.label.includes("DHCP Snooping")), "distribution missing DHCP snooping is flagged");
assert.ok(!lintSwitchConfig(noSnoop, { role: "core" }).some((item) => item.label.includes("DHCP")), "core is not asked for DHCP snooping");
// An access port wrongly set to trust is a bad finding.
const accessTrust = noSnoop.replace("switchport mode access", "switchport mode access\n ip dhcp snooping trust");
assert.ok(lintSwitchConfig(accessTrust).some((item) => item.level === "bad" && item.label.includes("DHCP Trust")), "access-port DHCP trust is flagged");
// Snooping enabled but no trust port anywhere breaks DHCP -> warn.
const snoopNoTrust = `
no vstack
logging host 192.168.41.253
ip dhcp snooping
ip dhcp snooping vlan 41
spanning-tree portfast bpduguard default
interface GigabitEthernet1/0/1
 switchport access vlan 41
 switchport mode access
 storm-control broadcast level 1.0
 storm-control action shutdown
`;
assert.ok(lintSwitchConfig(snoopNoTrust).some((item) => item.level === "warn" && item.label.includes("DHCP Trust")), "snooping without any trust port is flagged");

console.log("bigscreen platform tests passed");
