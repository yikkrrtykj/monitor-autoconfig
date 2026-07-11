const assert = require("assert");
const path = require("path");

global.window = { BIGSCREEN_CONFIG: { wanIfFilter: "telecom,WAN,eth0,eth1" }, BIGSCREEN_QUERIES: {} };

// Fake Prometheus：防火墙使用 HA 物理成员名 Member1，交换机使用 hostname。
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
  // Member1 是区分 HA 物理成员所必需的设备自带名称，应进入名字表。
  const nameMap = await api.fetchInfraDeviceNames();
  assert.strictEqual(nameMap.get("192.168.9.1"), "Member1", "HA 成员 sysName 应被保留");
  assert.strictEqual(nameMap.get("192.168.10.32"), "rts2", "真实 hostname 正常收录");

  // 改名结果：交换机 IP -> rts2；防火墙 IP/配置名 -> 当前设备 sysName。
  const renamed = api.renameListWithInfraMap([
    { name: "192.168.10.32", metric: { instance: "192.168.10.32", target_ip: "192.168.10.32" } },
    { name: "外网防火墙", metric: { instance: "外网防火墙", target_ip: "192.168.9.1" } }
  ], nameMap);
  assert.strictEqual(renamed[0].name, "rts2");
  assert.strictEqual(renamed[0].originalName, "192.168.10.32");
  assert.strictEqual(renamed[1].name, "Member1");

  // 运行时长：90 天以内保持"天"，超过换算成"月"（按 30 天）。
  assert.deepStrictEqual(utils.formatUptime(59.22 * 86400), { value: "59.22", unit: "天" });
  assert.deepStrictEqual(utils.formatUptime(89 * 86400), { value: "89.00", unit: "天" });
  assert.deepStrictEqual(utils.formatUptime(184.79 * 86400), { value: "6.2", unit: "月" });
  assert.deepStrictEqual(utils.formatUptime(30 * 86400 * 12), { value: "12.0", unit: "月" });

  // WAN 关键词：以数字结尾的按边界匹配（eth1 不命中 eth10），其它维持包含匹配。
  const wanRe = new RegExp(`.*(${api.wanFilterPattern()}).*`, "i");
  assert.ok(wanRe.test("eth0"), "eth0 应命中");
  assert.ok(wanRe.test("eth1"), "eth1 应命中");
  assert.ok(!wanRe.test("eth10"), "eth10 不应被 eth1 误配");
  assert.ok(!wanRe.test("eth15"), "eth15 不应被 eth1 误配");
  assert.ok(wanRe.test("WAN1"), "WAN 关键词维持包含匹配");
  assert.ok(wanRe.test("telecom-200M"), "telecom 维持包含匹配");
  assert.ok(!wanRe.test("lan-port"), "非 WAN 口不命中");

  console.log("bigscreen infra name/uptime tests passed");
})().catch((error) => { console.error(error); process.exit(1); });
