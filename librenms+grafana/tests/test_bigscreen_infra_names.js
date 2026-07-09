const assert = require("assert");
const path = require("path");

global.window = { BIGSCREEN_CONFIG: {}, BIGSCREEN_QUERIES: {} };

// Fake Prometheus：防火墙的 sysName 是 HA 占位名 Member1，交换机是真 hostname。
global.fetch = async (url) => {
  const query = new URL(String(url), "http://localhost").searchParams.get("query") || "";
  let result = [];
  if (query.includes("sysName")) {
    result = [
      { metric: { job: "infra-fw-snmp", instance: "外网防火墙", target_ip: "192.168.9.1", display_name: "外网防火墙", sysName: "Member1" }, value: [0, "1"] },
      { metric: { job: "infra-switch-snmp", instance: "192.168.10.32", target_ip: "192.168.10.32", display_name: "192.168.10.32", sysName: "rts2" }, value: [0, "1"] }
    ];
  }
  return { ok: true, json: async () => ({ status: "success", data: { result } }) };
};

const utils = require(path.resolve(__dirname, "../bigscreen/utils.js"));
const api = require(path.resolve(__dirname, "../bigscreen/api.js"));

(async () => {
  // Member1 这类出厂占位/HA 成员 sysName 不进名字表；真 hostname 正常进。
  const nameMap = await api.fetchInfraDeviceNames();
  assert.ok(!nameMap.has("192.168.9.1"), "占位 sysName(Member1) 应被拒收");
  assert.strictEqual(nameMap.get("192.168.10.32"), "rts2", "真实 hostname 正常收录");

  // 改名结果：交换机 IP -> rts2；防火墙保持手填名，不被 Member1 顶掉。
  const renamed = api.renameListWithInfraMap([
    { name: "192.168.10.32", metric: { instance: "192.168.10.32", target_ip: "192.168.10.32" } },
    { name: "外网防火墙", metric: { instance: "外网防火墙", target_ip: "192.168.9.1" } }
  ], nameMap);
  assert.strictEqual(renamed[0].name, "rts2");
  assert.strictEqual(renamed[0].originalName, "192.168.10.32");
  assert.strictEqual(renamed[1].name, "外网防火墙");

  // 运行时长：90 天以内保持"天"，超过换算成"月"（按 30 天）。
  assert.deepStrictEqual(utils.formatUptime(59.22 * 86400), { value: "59.22", unit: "天" });
  assert.deepStrictEqual(utils.formatUptime(89 * 86400), { value: "89.00", unit: "天" });
  assert.deepStrictEqual(utils.formatUptime(184.79 * 86400), { value: "6.2", unit: "月" });
  assert.deepStrictEqual(utils.formatUptime(30 * 86400 * 12), { value: "12.0", unit: "月" });

  console.log("bigscreen infra name/uptime tests passed");
})().catch((error) => { console.error(error); process.exit(1); });
