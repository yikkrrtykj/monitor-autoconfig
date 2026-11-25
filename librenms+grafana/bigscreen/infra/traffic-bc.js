;(function () {
  "use strict";
  const common = typeof module !== "undefined" && module.exports;
  const escape = (text) => String(text ?? "").replace(/[&<>"']/g,
    (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  const numeric = (value) => typeof value === "number" && Number.isFinite(value) && value >= 0;
  function rate(value) {
    if (!numeric(value)) return "N/A";
    if (value > 0 && value < 10000) return "<0.01 Mbps";
    return (value / 1e6).toFixed(2) + " Mbps";
  }

  function trace(points) {
    if (!Array.isArray(points) || !points.length) return '<span class="overview-note">趋势无有效样本</span>';
    const data = points.slice(-31);
    const present = data.filter((p) => numeric(p?.bps));
    if (!present.length) return '<span class="overview-note">趋势暂无有效计数</span>';
    const first = Number(data[0].t), last = Number(data.at(-1).t);
    if (!Number.isFinite(first) || !Number.isFinite(last) || last <= first) return "";
    const top = Math.max(1, ...present.map((p) => p.bps));
    let connected = false;
    const commands = data.map((p) => {
      if (!numeric(p?.bps) || !Number.isFinite(Number(p.t))) { connected = false; return ""; }
      const x = 1 + (Number(p.t) - first) * 198 / (last - first);
      const y = 25 - p.bps / top * 23;
      const command = connected ? "L" : "M";
      connected = true;
      return command + x.toFixed(1) + " " + y.toFixed(1);
    }).join(" ");
    return '<svg viewBox="0 0 200 28" preserveAspectRatio="none" role="img" aria-label="15分钟接口流量趋势，有断点的区间无数据"><path class="overview-rx" d="' + commands + '"/></svg>';
  }

  function item(row, domain, now, maxAge) {
    const expired = (at) => numeric(at) && Number.isFinite(now) && Number.isFinite(maxAge) &&
      (now < at || now - at > maxAge);
    const rxAt = expired(row.rx?.sample_at) ? null : row.rx?.sample_at;
    const txAt = expired(row.tx?.sample_at) ? null : row.tx?.sample_at;
    const rx = rxAt === null ? null : row.rx?.bps;
    const tx = txAt === null ? null : row.tx?.bps;
    const status = expired(row.interface_sample_at) ? "UNKNOWN" : row.interface_status;
    const valid = row.traffic_validity === "VALID" && numeric(rx) && numeric(tx);
    const stamp = (numeric(rxAt) || numeric(txAt))
      ? new Date(Math.max(rxAt || 0, txAt || 0) * 1000)
          .toLocaleTimeString("zh-CN", { hour12: false }) : "未知";
    const hint = domain === "remote_ob"
      ? "SA: UNKNOWN · 实际 ISP: UNKNOWN" + (row.configured_isp ? " · 配置出口: " + escape(row.configured_isp) : "")
      : "仅 SVI 三层网关接口流量；不是 VLAN 互联网/全量二层流量";
    return '<article class="overview-bc-item" data-validity="' + (valid ? 'valid' : 'unknown') + '">' +
      '<div class="overview-bc-title"><strong>' + escape(row.name) + '</strong><small>' +
        escape(status || "UNKNOWN") + ' · ' + escape(row.interface || "") + '</small></div>' +
      '<div class="overview-bc-rates"><span>RX ↓ <b>' + escape(rate(rx)) +
      '</b></span><span>TX ↑ <b>' + escape(rate(tx)) + '</b></span></div>' +
      '<div class="overview-bc-trends">' + trace(row.rx?.points) + trace(row.tx?.points) + '</div>' +
      '<small class="overview-bc-note">' + (valid ? "计数连续" : "UNKNOWN · 缺样/重置/身份或采集异常") +
      ' · 更新: ' + escape(stamp) + '</small>' +
      '<small class="overview-bc-note">' + hint + '</small></article>';
  }

  function render(domainState, mode, domain, now) {
    if (mode === "loading") return '<p class="overview-empty">正在读取业务接口…</p>';
    if (mode === "error") return '<p class="overview-empty">UNKNOWN · 业务流量读取失败</p>';
    if (!domainState || domainState.state === "unconfigured") {
      return '<p class="overview-empty">未配置接口，不推断业务流量</p>';
    }
    if (domainState.state !== "ready" || !Array.isArray(domainState.items)) {
      return '<p class="overview-empty">UNKNOWN · 采集状态不可验证</p>';
    }
    return domainState.items.map((row) => item(row, domain, now, domainState.max_age_seconds)).join("");
  }

  const exported = { render, rate, trace, escape };
  if (common) module.exports = exported;
  else window.BSBusinessTraffic = exported;
})();
