const assert = require("assert");
const path = require("path");

const iperf = require(path.resolve(__dirname, "../bigscreen/iperf.js"));
const escapeHtml = (value) => String(value == null ? "" : value)
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#39;");

assert.strictEqual(iperf.formatBytes(1024), "1.00 KB");
assert.strictEqual(iperf.formatBytes(1024 ** 3), "1.00 GB");

const presets = {
  hongkong: { note: "HK", servers: [
    { label: "primary", server: "one.example.test", ports: "5201-5210" },
    { label: "backup", server: "two.example.test", ports: "5201" }
  ] },
  custom: iperf.DEFAULT_CUSTOM_PRESET
};
assert.deepStrictEqual(iperf.presetView(presets, "hongkong", 1), {
  isCustom: false,
  placeholder: "iPerf3 服务器域名或 IP",
  note: "HK",
  options: [{ index: 0, label: "primary" }, { index: 1, label: "backup" }],
  server: "two.example.test",
  ports: "5201"
});
assert.strictEqual(iperf.presetView(presets, "custom").isCustom, true);
assert.strictEqual(iperf.presetView(presets, "custom").ports, "5201");

const complete = iperf.resultView({
  ok: true,
  state: "complete",
  taskId: "iperf-1-safe",
  protocol: "TCP",
  server: "speed.example.test",
  duration: 10,
  parallel: 4,
  results: [{
    direction: "upload",
    mbps: 950,
    bytes: 1_187_500_000,
    port: 5201,
    retransmits: 3,
    sender: { mbps: 1000, bytes: 1_250_000_000, retransmits: 3 },
    receiver: { mbps: 950, bytes: 1_187_500_000, seconds: 10 },
    intervals: [{ start: 0, end: 1, bytes: 118_750_000, mbps: 950, retransmits: 1 }]
  }]
}, escapeHtml);
assert.strictEqual(complete.className, "network-tool-result good");
assert.ok(complete.html.includes("iperf-1-safe"));
assert.ok(complete.html.includes("接收端全程平均 950.00 Mbps"));
assert.ok(complete.html.includes("TCP 重传"));

const cancelled = iperf.resultView({ state: "cancelled" }, escapeHtml);
assert.strictEqual(cancelled.className, "network-tool-result warn");
assert.strictEqual(cancelled.text, "测速已由操作员停止。");

const history = Array.from({ length: 5 }, (_, index) => ({
  taskId: `task-${index}`,
  server: `node-${index}.example.test`,
  state: "complete",
  results: [{ direction: "download", mbps: 100 + index }]
}));
const historyMarkup = iperf.historyHtml({ history }, escapeHtml);
assert.strictEqual((historyMarkup.match(/data-task-id=/g) || []).length, 5);
assert.ok(historyMarkup.includes("下载 104.00 Mbps"));

(async () => {
  const calls = [];
  const payload = await iperf.loadServerConfig(async (url, options) => {
    calls.push({ url, options });
    return {
      ok: true,
      json: async () => ({ version: 1, verifiedAt: "2026-08-03", presets: { public: { servers: [] } } })
    };
  });
  assert.deepStrictEqual(calls, [{ url: "/iperf-servers.json", options: { cache: "no-store" } }]);
  assert.strictEqual(payload.version, 1);
  assert.strictEqual(payload.presets.custom, iperf.DEFAULT_CUSTOM_PRESET);
  console.log("bigscreen iperf behavior tests passed");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
