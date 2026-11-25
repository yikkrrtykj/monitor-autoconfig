"use strict";
const assert = require("node:assert/strict");
const { render, rate, trace, escape } = require("../bigscreen/infra/traffic-bc.js");

assert.equal(rate(null), "N/A");
assert.equal(rate(0), "0.00 Mbps");
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
assert.match(output, /M1.0 2/);
assert.match(output, /M199.0 2/); // gap in RX must create another M, never an L across UNKNOWN.
assert.ok(trace(row().rx.points).includes(" M199.0 "));
assert.match(render({ state: "ready", items: [row("VLAN20")] }, "ready", "business_vlan"), /不是 VLAN 互联网/);
assert.match(render({ state: "unconfigured", items: [] }, "ready", "business_vlan"), /未配置接口/);
assert.match(render(null, "error", "remote_ob"), /UNKNOWN/);
assert.match(render(null, "loading", "remote_ob"), /正在读取/);
console.log("B/C read-only UI presentation: PASS");
