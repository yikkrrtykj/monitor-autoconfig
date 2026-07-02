const assert = require("assert");
const path = require("path");

const {
  readinessScore,
  summarizePlayers,
  summarizeTargets,
  summarizeServices,
  buildReadinessChecks,
  lintSwitchConfig
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

const badDhcpTrust = `
no vstack
logging host 192.168.41.253
interface GigabitEthernet1/0/9
 description player
 switchport access vlan 41
 switchport mode access
 ip dhcp snooping trust
 storm-control broadcast level 1.00 0.50
 storm-control action shutdown
 spanning-tree portfast edge
 spanning-tree bpduguard enable
`;
const trustIssues = lintSwitchConfig(badDhcpTrust);
assert.ok(trustIssues.some((item) => item.level === "bad" && item.label.includes("DHCP Trust")), "access DHCP trust is flagged");

console.log("bigscreen platform tests passed");
