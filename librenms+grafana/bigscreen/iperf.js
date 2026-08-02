(function (root) {
  "use strict";

  const DEFAULT_CUSTOM_PRESET = Object.freeze({
    placeholder: "填写自有或其他公共 iPerf3 服务器",
    note: "使用手工填写的服务器和端口。"
  });

  function formatBytes(value) {
    const bytes = Math.max(0, Number(value || 0));
    if (bytes >= 1024 ** 3) return `${(bytes / (1024 ** 3)).toFixed(2)} GB`;
    if (bytes >= 1024 ** 2) return `${(bytes / (1024 ** 2)).toFixed(2)} MB`;
    if (bytes >= 1024) return `${(bytes / 1024).toFixed(2)} KB`;
    return `${Math.round(bytes)} B`;
  }

  function directionDetails(item, protocol, escapeHtml) {
    const labels = { upload: "上传", download: "下载" };
    const sender = item.sender || {};
    const receiver = item.receiver || {};
    const intervals = item.intervals || [];
    return `
      <section class="iperf-direction-detail">
        <header>
          <strong>${labels[item.direction] || escapeHtml(item.direction)}明细</strong>
          <span>${escapeHtml(protocol)} · 接收端全程平均 ${Number(item.mbps || 0).toFixed(2)} Mbps</span>
        </header>
        <div class="iperf-endpoints">
          <div><span>发送端总计</span><strong>${Number(sender.mbps || 0).toFixed(2)} Mbps</strong><small>${formatBytes(sender.bytes)} · 重传 ${Number(sender.retransmits || 0)}</small></div>
          <div><span>接收端总计</span><strong>${Number(receiver.mbps || item.mbps || 0).toFixed(2)} Mbps</strong><small>${formatBytes(receiver.bytes || item.bytes)} · ${Number(receiver.seconds || item.seconds || 0).toFixed(2)} 秒</small></div>
        </div>
        ${intervals.length ? `
          <div class="iperf-interval-table-wrap">
            <table class="iperf-interval-table">
              <thead><tr><th>区间</th><th>传输量</th><th>平均速率</th><th>TCP 重传</th></tr></thead>
              <tbody>
                ${intervals.map((interval) => `
                  <tr>
                    <td>${Number(interval.start || 0).toFixed(2)}–${Number(interval.end || 0).toFixed(2)} 秒</td>
                    <td>${formatBytes(interval.bytes)}</td>
                    <td>${Number(interval.mbps || 0).toFixed(2)} Mbps</td>
                    <td>${interval.retransmits == null ? "—" : Number(interval.retransmits)}</td>
                  </tr>
                `).join("")}
              </tbody>
              <tfoot><tr><th>全程</th><th>${formatBytes(receiver.bytes || item.bytes)}</th><th>${Number(receiver.mbps || item.mbps || 0).toFixed(2)} Mbps</th><th>${Number(sender.retransmits || item.retransmits || 0)}</th></tr></tfoot>
            </table>
          </div>
        ` : '<p class="network-result-note">本次服务器没有返回每秒区间明细。</p>'}
      </section>
    `;
  }

  function resultView(response, escapeHtml) {
    if (!response || response.state !== "complete" || response.ok === false) {
      const cancelled = response && response.state === "cancelled";
      return {
        className: `network-tool-result ${cancelled ? "warn" : "bad"}`,
        text: cancelled
          ? "测速已由操作员停止。"
          : `测速失败：${(response && response.message) || "未知错误"}`
      };
    }
    const labels = { upload: "上传", download: "下载" };
    const protocol = response.protocol || "TCP";
    return {
      className: "network-tool-result good",
      html: `
        <div class="network-result-summary">
          ${(response.results || []).map((item) => `
            <div><span>${labels[item.direction] || escapeHtml(item.direction)} · 接收端平均</span><strong>${Number(item.mbps || 0).toFixed(2)} Mbps</strong><small>${formatBytes(item.bytes)} · 端口 ${Number(item.port) || "?"} · 重传 ${Number(item.retransmits || 0)}</small></div>
          `).join("")}
        </div>
        <p class="network-result-note">任务 ${escapeHtml(response.taskId || "-")} · ${escapeHtml(protocol)} · 服务器 ${escapeHtml(response.server)} · ${Number(response.parallel) || "?"} 路并发 · 单向 ${Number(response.duration) || "?"} 秒</p>
        <div class="iperf-direction-details">
          ${(response.results || []).map((item) => directionDetails(item, protocol, escapeHtml)).join("")}
        </div>
      `
    };
  }

  function historyHtml(payload, escapeHtml) {
    const history = (payload && Array.isArray(payload.history)) ? payload.history : [];
    if (!history.length) {
      return '<p class="network-tool-hint">暂无测速历史；完成后最多保留 5 条。</p>';
    }
    return `
      <h4>最近测速（最多 5 条）</h4>
      <div class="iperf-history-list">
        ${history.map((item) => {
          const rates = (item.results || []).map((result) => `${result.direction === "download" ? "下载" : "上传"} ${Number(result.mbps || 0).toFixed(2)} Mbps`).join(" · ");
          const when = item.finishedAt ? new Date(item.finishedAt * 1000).toLocaleString("zh-CN", { hour12: false }) : "-";
          return `<button type="button" class="iperf-history-row" data-task-id="${escapeHtml(item.taskId || "")}"><span>${escapeHtml(when)} · ${escapeHtml(item.server || "-")}</span><strong>${escapeHtml(rates || item.message || item.state || "-")}</strong></button>`;
        }).join("")}
      </div>`;
  }

  async function loadServerConfig(fetchImpl, url = "/iperf-servers.json") {
    const response = await fetchImpl(url, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    if (!payload || !payload.presets || typeof payload.presets !== "object") {
      throw new Error("配置格式无效");
    }
    return {
      ...payload,
      presets: {
        ...payload.presets,
        custom: payload.presets.custom || DEFAULT_CUSTOM_PRESET
      }
    };
  }

  function presetView(presets, presetKey, serverIndex = 0) {
    const selectedPreset = presets[presetKey] || presets.custom || DEFAULT_CUSTOM_PRESET;
    const isCustom = presetKey === "custom" || !Array.isArray(selectedPreset.servers);
    const servers = isCustom ? [] : selectedPreset.servers;
    const selected = servers[Math.max(0, Number(serverIndex) || 0)] || servers[0] || null;
    return {
      isCustom,
      placeholder: selectedPreset.placeholder || "iPerf3 服务器域名或 IP",
      note: selectedPreset.note || "",
      options: servers.map((item, index) => ({ index, label: item.label })),
      server: selected ? selected.server : "",
      ports: selected ? selected.ports : (isCustom ? "5201" : "")
    };
  }

  const api = {
    DEFAULT_CUSTOM_PRESET,
    formatBytes,
    resultView,
    historyHtml,
    loadServerConfig,
    presetView
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.BSIperf = api;
})(typeof window !== "undefined" ? window : globalThis);
