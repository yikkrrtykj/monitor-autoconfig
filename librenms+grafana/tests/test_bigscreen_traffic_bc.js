"use strict";
const assert = require("node:assert/strict");
const { render, rate, trace, escape } = require("../bigscreen/infra/traffic-bc.js");

assert.equal(rate(null), "N/A");
assert.equal(rate(0), "0.00 Mbps");
assert.equal(rate(1000), "<0.01 Mbps");
assert.equal(rate(4999), "<0.01 Mbps");
assert.equal(rate(10000), "0.01 Mbps");
assert.equal(rate(1000000), "1.00 Mbps");
assert.equal(escape("<img src=x>"), "&lt;img src=x&gt;");

function row(name = "OB & <A>") {
  return {
    name, interface: "tunnel1", interface_status: "UP",
    traffic_validity: "UNKNOWN",
    rx: { bps: null, sample_at: null,
      points: [{ t: 0, bps: 10 }, { t: 30, bps: null }, { t: 60, bps: 20 }] },
    tx: { bps: 0, sample_at: 60, points: [{ t: 0, bps: 0 }, { t: 30, bps: 0 }, { t: 60, bps: 0 }] },
    configured_isp: "<Configured only>"
  };
}

const output = render({ state: "ready", items: [row()] }, "ready", "remote_ob");
assert.match(output, /OB &amp; &lt;A&gt;/);
assert.doesNotMatch(output, /<A>|<Configured only>/);
assert.match(output, /SA: UNKNOWN/);
assert.match(output, /实际 ISP: UNKNOWN/);
assert.match(output, /N\/A/);
assert.match(output, /0.00 Mbps/);
assert.match(output, /M1.0 13.5/);
assert.match(output, /M199.0 2.0/); // gap in RX must create another M, never an L across UNKNOWN.
assert.ok(trace(row().rx.points).includes(" M199.0 "));
assert.match(render({ state: "ready", items: [row("VLAN20")] }, "ready", "business_vlan"), /不是 VLAN 互联网/);
assert.match(render({ state: "unconfigured", items: [] }, "ready", "business_vlan"), /未配置接口/);
assert.match(render(null, "error", "remote_ob"), /UNKNOWN/);
assert.match(render(null, "loading", "remote_ob"), /正在读取/);

const live = {
  ...row("Tunnel"), interface_sample_at: 120, traffic_validity: "VALID",
  rx: { ...row().rx, bps: 1000, sample_at: 120 },
  tx: { ...row().tx, sample_at: 120 }
};
const domain = { state: "ready", items: [live], max_age_seconds: 40 };
const boundary = render(domain, "ready", "remote_ob", 160);
assert.match(boundary, /data-validity="valid"/);
assert.match(boundary, /UP · tunnel1/);
assert.match(boundary, /&lt;0\.01 Mbps/);
const aged = render(domain, "ready", "remote_ob", 161);
assert.match(aged, /data-validity="unknown"/);
assert.match(aged, /UNKNOWN · tunnel1/);
assert.equal([...aged.matchAll(/<b>N\/A<\/b>/g)].length, 2);
assert.match(aged, /M199\.0 2\.0/, "expiry preserves the history chart");

async function testTrafficCancellation() {
  const { createOverviewController } = require("../bigscreen/infra/overview-controller.js");
  const pending = [];
  const observed = [];
  let timer;
  let now = 100;
  const suspend = () => new Promise(() => {});
  const controller = createOverviewController({
    data: {
      summary: suspend, bandwidth: suspend,
      businessTraffic: (signal) => new Promise((resolve) => pending.push({ signal, resolve }))
    },
    render: (_, meta) => observed.push(meta),
    clock: () => now,
    schedule: (callback) => { timer = callback; return callback; },
    cancel: (callback) => { assert.equal(timer, callback); timer = null; }
  });
  const flush = () => new Promise((resolve) => setImmediate(resolve));
  controller.start();
  assert.equal(pending.length, 1);
  controller.stop();
  assert.equal(timer, null);
  assert.equal(pending[0].signal.aborted, true);

  controller.start();
  const obsolete = { remote_ob: { state: "old" } };
  pending[0].resolve({ domains: obsolete });
  await flush();
  assert.equal(observed.some((meta) => meta.bcData === obsolete), false);

  const current = { remote_ob: { state: "ready", items: [] } };
  pending[1].resolve({ domains: current, served_at: 149 });
  await flush();
  assert.equal(observed.at(-1).bcData, current);
  assert.equal(observed.at(-1).bcNow, 149);
  now += 20;
  timer();
  assert.equal(observed.at(-1).bcNow, 169);
  assert.equal(pending.length, 2, "painting expiry does not add requests");
  controller.stop();
}

testTrafficCancellation().then(() => {
  console.log("B/C read-only UI presentation and request cancellation: PASS");
}).catch((error) => { console.error(error); process.exitCode = 1; });
