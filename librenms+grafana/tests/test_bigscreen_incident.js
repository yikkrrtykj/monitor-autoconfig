const assert = require("assert");
const path = require("path");

global.window = {
  BIGSCREEN_CONFIG: { ispNames: "ISP1", ispMaxBandwidthMbps: "ISP1:100" },
  BIGSCREEN_QUERIES: {}
};

const {
  seriesMaxValue,
  countOfflineRecoveries,
  analyzeIncident
} = require(path.resolve(__dirname, "../bigscreen/incident.js"));

const series = (metric, vs) => ({ metric, values: vs.map((v, i) => ({ t: 1000 + i * 5, v })) });
const emptyData = { playerLatency: [], playerSuccess: [], infraLatency: [], infraSuccess: [], isp: [] };

// ---- seriesMaxValue / countOfflineRecoveries ----
assert.strictEqual(seriesMaxValue(series({}, [0.01, 0.05, 0.02])), 0.05);
assert.strictEqual(seriesMaxValue(series({}, [])), null);
assert.strictEqual(countOfflineRecoveries([1, 0, 0, 1, 0, 1].map((v) => ({ v }))), 2);
assert.strictEqual(countOfflineRecoveries([0, 0, 0].map((v) => ({ v }))), 0, "stale all-offline entry has no recoveries");

// ---- all clear ----
let result = analyzeIncident(emptyData, 0.02);
assert.strictEqual(result.verdict.level, "good");

// ---- core-layer event dominates every other rule ----
result = analyzeIncident({
  ...emptyData,
  playerLatency: [series({ team: "1", seat: "1", switch: "sw-stage1", instance: "10.1.1.11" }, [0.09])],
  infraLatency: [series({ job: "infra-core-ping", instance: "core", target_ip: "10.0.0.1" }, [0.05])]
}, 0.02);
assert.strictEqual(result.verdict.text, "核心层异常");
assert.strictEqual(result.verdict.level, "bad");

// ---- >=3 players on one stage switch -> suspect that access switch ----
result = analyzeIncident({
  ...emptyData,
  playerLatency: [
    series({ team: "1", seat: "1", switch: "sw-stage1", instance: "10.1.1.11" }, [0.05]),
    series({ team: "1", seat: "2", switch: "sw-stage1", instance: "10.1.1.12" }, [0.06]),
    series({ team: "1", seat: "3", switch: "sw-stage1", instance: "10.1.1.13" }, [0.07])
  ]
}, 0.02);
assert.ok(result.verdict.text.includes("sw-stage1"), "verdict names the suspect switch");
assert.strictEqual(Object.keys(result.stageGroups).length, 1);
assert.strictEqual(result.stageGroups["sw-stage1"].players.length, 3);

// ---- saturated ISP + >=3 affected players spread over different switches ----
result = analyzeIncident({
  ...emptyData,
  playerLatency: [
    series({ team: "1", seat: "1", switch: "sw1", instance: "10.1.1.11" }, [0.05]),
    series({ team: "2", seat: "1", switch: "sw2", instance: "10.1.2.11" }, [0.05]),
    series({ team: "3", seat: "1", switch: "sw3", instance: "10.1.3.11" }, [0.05])
  ],
  isp: [{ metric: {}, _ispName: "ISP1", _direction: "in", values: [{ t: 1000, v: 90 * 1000 * 1000 }] }]
}, 0.02);
assert.strictEqual(result.verdict.text, "ISP 链路接近饱和");
assert.strictEqual(result.ispEvents[0].capacityBps, 100 * 1000 * 1000, "capacity from BIGSCREEN_ISP_MAX_BANDWIDTH");
assert.ok(result.ispEvents[0].utilization > 0.89 && result.ispEvents[0].utilization < 0.91);

// ---- single player -> single-point suspicion ----
result = analyzeIncident({
  ...emptyData,
  playerLatency: [series({ team: "4", seat: "2", switch: "sw1", instance: "10.1.4.12" }, [0.05])]
}, 0.02);
assert.strictEqual(result.verdict.text, "单选手问题");
assert.ok(result.verdict.detail.includes("Team 4 S2"));

// ---- disconnect detected via recovery edges on probe_success ----
result = analyzeIncident({
  ...emptyData,
  playerSuccess: [
    series({ team: "1", seat: "1", instance: "10.1.1.11" }, [1, 0, 1]),
    series({ team: "2", seat: "1", instance: "10.1.2.11" }, [1, 0, 1])
  ]
}, 0.02);
assert.strictEqual(result.offlinePlayers.length, 2);
assert.strictEqual(result.offlinePlayers[0].recoveryCount, 1);
assert.strictEqual(result.verdict.text, "多选手卡顿");

// ---- static/wireless-scan pseudo switches are excluded from stage grouping ----
result = analyzeIncident({
  ...emptyData,
  playerLatency: [
    series({ team: "1", seat: "1", switch: "static", instance: "10.1.1.11" }, [0.05]),
    series({ team: "1", seat: "2", switch: "wireless-scan", instance: "10.1.1.12" }, [0.05])
  ]
}, 0.02);
assert.strictEqual(Object.keys(result.stageGroups).length, 0);

console.log("bigscreen incident tests passed");
