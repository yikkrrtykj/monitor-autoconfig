const assert = require("assert");
const path = require("path");

// localStorage stub so the infra-name cache can persist across calls the way
// a kiosk browser would; the rename logic must not depend on it being present.
const store = {};
global.window = {
  BIGSCREEN_CONFIG: {},
  BIGSCREEN_QUERIES: {},
  localStorage: {
    getItem: (key) => (key in store ? store[key] : null),
    setItem: (key, value) => { store[key] = String(value); }
  }
};

// Fake Prometheus routed by query string. Models the real label shape:
// the snmp-exporter `sysName` series carries the device hostname in its own
// `sysName` label, while ping series only know the configured display_name
// (e.g. "SW1") -- which is what the big screen used to show instead of the
// real hostname.
global.fetch = async (url) => {
  const query = new URL(String(url), "http://localhost").searchParams.get("query") || "";
  let result = [];
  if (query.includes("sysName")) {
    result = [
      { metric: { job: "infra-switch-snmp", instance: "CORE1", target_ip: "10.0.0.1", display_name: "CORE1", sysName: "core-sw-01" }, value: [0, "1"] },
      { metric: { job: "infra-switch-snmp", instance: "SW1", target_ip: "10.0.0.11", display_name: "SW1", sysName: "access-sw-11" }, value: [0, "1"] },
      // A device whose sysName is just its management IP must be rejected so the
      // screen keeps the friendlier configured name rather than an IP.
      { metric: { job: "infra-switch-snmp", instance: "SW2", target_ip: "10.0.0.12", display_name: "SW2", sysName: "10.0.0.12" }, value: [0, "1"] }
    ];
  } else if (query.includes("probe_success")) {
    result = [
      { metric: { job: "infra-core-ping", instance: "CORE1", target_ip: "10.0.0.1", display_name: "CORE1" }, value: [0, "1"] },
      { metric: { job: "infra-dist-ping", instance: "SW1", target_ip: "10.0.0.11", display_name: "SW1" }, value: [0, "1"] },
      { metric: { job: "infra-dist-ping", instance: "SW2", target_ip: "10.0.0.12", display_name: "SW2" }, value: [0, "1"] }
    ];
  } else if (query.includes("probe_icmp_duration_seconds")) {
    result = [
      { metric: { job: "infra-core-ping", instance: "CORE1", target_ip: "10.0.0.1" }, value: [0, "0.002"] },
      { metric: { job: "infra-dist-ping", instance: "SW1", target_ip: "10.0.0.11" }, value: [0, "0.004"] },
      { metric: { job: "infra-dist-ping", instance: "SW2", target_ip: "10.0.0.12" }, value: [0, "0.004"] }
    ];
  }
  return { ok: true, json: async () => ({ status: "success", data: { result } }) };
};

const api = require(path.resolve(__dirname, "../bigscreen/api.js"));

(async () => {
  // 1. The name map resolves switches to their SNMP hostname, keyed by both
  //    target IP and configured name; an IP-shaped sysName is dropped.
  const nameMap = await api.fetchInfraDeviceNames();
  assert.strictEqual(nameMap.get("10.0.0.1"), "core-sw-01", "core resolves by IP");
  assert.strictEqual(nameMap.get("CORE1"), "core-sw-01", "core resolves by configured name");
  assert.strictEqual(nameMap.get("10.0.0.11"), "access-sw-11", "dist resolves by IP");
  assert.ok(!nameMap.has("10.0.0.12"), "IP-only sysName is rejected");

  // 2. Gauge/chart lists get the configured name replaced with the hostname,
  //    keeping the original for traceability; devices without a valid sysName
  //    keep their configured name.
  const pingItems = [
    { name: "CORE1", metric: { instance: "CORE1", target_ip: "10.0.0.1" } },
    { name: "SW1", metric: { instance: "SW1", target_ip: "10.0.0.11" } },
    { name: "SW2", metric: { instance: "SW2", target_ip: "10.0.0.12" } }
  ];
  const renamed = api.renameListWithInfraMap(pingItems, nameMap);
  assert.strictEqual(renamed[0].name, "core-sw-01", "gauge core renamed to hostname");
  assert.strictEqual(renamed[1].name, "access-sw-11", "gauge dist renamed to hostname");
  assert.strictEqual(renamed[2].name, "SW2", "gauge keeps configured name without a valid sysName");
  assert.strictEqual(renamed[0].originalName, "CORE1", "original configured name preserved");

  // 3. Topology nodes carry the hostname through displayName so the diagram
  //    stops showing "SW1" once SNMP knows the real device name.
  const targets = await api.fetchTopologyTargets();
  const byIp = Object.fromEntries(targets.map((target) => [target.targetIp, target.displayName]));
  assert.strictEqual(byIp["10.0.0.1"], "core-sw-01", "topology core renamed to hostname");
  assert.strictEqual(byIp["10.0.0.11"], "access-sw-11", "topology dist renamed to hostname");
  assert.strictEqual(byIp["10.0.0.12"], "SW2", "topology keeps configured name without a valid sysName");

  console.log("bigscreen device name tests passed");
})().catch((error) => { console.error(error); process.exit(1); });
