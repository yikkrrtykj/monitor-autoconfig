const assert = require("assert");
const fs = require("fs");
const path = require("path");

const {
  isGatewayAddress,
  preferPlayer,
  dedupePlayersBySeat,
  buildPlayers,
  latencyLevel,
  playerStatusText
} = require(path.resolve(__dirname, "../bigscreen/players.js"));

// ---- isGatewayAddress ----
assert.strictEqual(isGatewayAddress("10.0.0.254"), true);
assert.strictEqual(isGatewayAddress("10.0.0.25"), false);
assert.strictEqual(isGatewayAddress("10.0.0.2540"), false);
assert.strictEqual(isGatewayAddress(""), false);

// ---- preferPlayer: online beats offline; finite latency beats null; lower latency wins ----
const p = (over) => ({ team: 1, seat: 1, ip: "10.0.0.1", network: "wired", success: true, latency: null, ...over });
assert.strictEqual(preferPlayer(p({ success: false }), p({ success: true })).success, true);
assert.strictEqual(preferPlayer(p({ success: true }), p({ success: false })).success, true);
assert.strictEqual(preferPlayer(p({ latency: null }), p({ latency: 0.01 })).latency, 0.01);
assert.strictEqual(preferPlayer(p({ latency: 0.01 }), p({ latency: null })).latency, 0.01);
assert.strictEqual(preferPlayer(p({ latency: 0.02 }), p({ latency: 0.01 })).latency, 0.01);
assert.strictEqual(preferPlayer(p({ latency: 0.01 }), p({ latency: 0.02 })).latency, 0.01);

// ---- dedupePlayersBySeat: one slot per (team, seat, network) ----
const deduped = dedupePlayersBySeat([
  p({ ip: "10.0.0.1", success: false }),
  p({ ip: "10.0.0.2", success: true, latency: 0.003 }),
  p({ seat: 2, ip: "10.0.0.3" })
]);
assert.strictEqual(deduped.length, 2);
assert.strictEqual(deduped.find((x) => x.seat === 1).ip, "10.0.0.2", "online entry wins the seat");

// ---- buildPlayers: merge success+latency vectors, filter gateways and invalid keys ----
const item = (metric, value) => ({ metric, value });
const players = buildPlayers(
  [
    item({ team: "1", seat: "1", instance: "10.1.1.11", network: "wired" }, 0.005),
    item({ team: "1", seat: "2", instance: "10.1.1.254", network: "wired" }, 0.001),
    item({ team: "2", seat: "1", instance: "10.1.1.21", network: "wired" }, 0.012)
  ],
  [
    item({ team: "1", seat: "1", instance: "10.1.1.11", network: "wired" }, 1),
    item({ team: "1", seat: "3", instance: "10.1.1.13", network: "wired" }, 0),
    item({ team: "0", seat: "1", instance: "10.1.1.99", network: "wired" }, 1)
  ]
);
assert.strictEqual(players.length, 3, "gateway .254 and team=0 entries are dropped");
assert.deepStrictEqual(players.map((x) => `${x.team}-${x.seat}`), ["1-1", "1-3", "2-1"], "sorted by team then seat");
const t1s1 = players.find((x) => x.team === 1 && x.seat === 1);
assert.strictEqual(t1s1.success, true);
assert.strictEqual(t1s1.latency, 0.005, "latency merged onto the success entry");
const t1s3 = players.find((x) => x.team === 1 && x.seat === 3);
assert.strictEqual(t1s3.success, false, "probe_success=0 marks the player offline");
const t2s1 = players.find((x) => x.team === 2 && x.seat === 1);
assert.strictEqual(t2s1.success, true, "latency-only entry defaults to online");

// ---- latencyLevel thresholds ----
assert.strictEqual(latencyLevel(null), "offline");
assert.strictEqual(latencyLevel(p({ success: false })), "offline");
assert.strictEqual(latencyLevel(p({ latency: null })), "unknown");
assert.strictEqual(latencyLevel(p({ latency: 0.08 })), "bad");
assert.strictEqual(latencyLevel(p({ latency: 0.04 })), "warn");
assert.strictEqual(latencyLevel(p({ latency: 0.039 })), "good");

// ---- playerStatusText branches ----
assert.strictEqual(playerStatusText(p({ success: false })), "离线");
assert.strictEqual(playerStatusText(p({ latency: null })), "暂无延迟");
assert.strictEqual(playerStatusText(p({ latency: 0.09 })), "高延迟");
assert.strictEqual(playerStatusText(p({ latency: 0.05 })), "轻微抖动");
assert.strictEqual(playerStatusText(p({ latency: 0.005 })), "正常");

// Current status must reflect the most recent scrape, not "any success in the
// last 90 seconds", which kept disconnected players falsely online.
const appSource = fs.readFileSync(path.resolve(__dirname, "../bigscreen/app.js"), "utf8");
assert.ok(appSource.includes('const playerSnapshotWindow = "15s"'));
assert.ok(appSource.includes("last_over_time(probe_success"));
assert.ok(!appSource.includes("max_over_time(probe_success{${selector}}[${playerSnapshotWindow}])"));

console.log("bigscreen players tests passed");
