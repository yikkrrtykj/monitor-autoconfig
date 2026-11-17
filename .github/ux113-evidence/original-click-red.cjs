const assert = require("node:assert/strict");
const { test } = require("node:test");
const { readFileSync } = require("node:fs");
const vm = require("node:vm");
test("clicking an original RTT chart invokes its time navigation", () => {
  const fields = new Map();
  let click;
  const root = {querySelector(id) {
    if (!fields.has(id)) fields.set(id, {addEventListener(){}, contains: () => false});
    return fields.get(id);
  },addEventListener(type,fn){if(type==="click")click=fn;}};
  let positioned = false;
  const window = {
    BSPingModel: { maxHistory: 604800, inventorySeconds: 1800 }, BSPingData: {},
    BSPingCharts: { chartTime(){ positioned=true; return 1700000001; } },
    BSUtils: {}, BSPingTrends: { createTrendLoader: () => ({}) },
    BSOverviewModel: { firewallExpectations: () => ({}) },
    BSPingWall: { createPingWall: () => ({}) }
  };
  vm.runInNewContext(readFileSync(process.argv[2],"utf8"),{window});
  window.BSPingPanel.createPingPanel(root, {}, {lossThreshold:5,rttThreshold:80});
  click({target:{closest: selector => selector === "[data-chart-start]" ? {dataset:{chartStart:"1700000000"}} : null},clientX:75});
  assert.ok(positioned,"RTT timeline in #pingDistribution should reach chartTime");
});
