;(function () {
  'use strict';

  /**
   * @typedef {{ t: number, v: number }} DataPoint
   * @typedef {{ name: string, metric: Record<string,string>, values: DataPoint[] }} Series
   * @typedef {{ name: string, value: number, metric: Record<string,string> }} InstantItem
   * @typedef {{ team: number, seat: number, ip: string, network: string, success: boolean, latency: number|null }} Player
   * @typedef {{ kind: string, name: string, ip: string, level: string, latency: number|null, success?: boolean }} TopologyNode
   */

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, (char) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;"
    })[char]);
  }

  function escapeRegex(value) {
    return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  function escapeLabel(value) {
    return String(value).replace(/\\/g, "\\\\").replace(/"/g, '\\"');
  }

  function metricName(metric) {
    return metric.instance || metric.display_name || metric.ifAlias || metric.ifName || metric.ifDescr || "unknown";
  }

  function formatPing(seconds) {
    if (seconds < 0.001) {
      return { value: Math.round(seconds * 1000000), unit: "μs" };
    }
    return { value: (seconds * 1000).toFixed(1), unit: "ms" };
  }

  function formatPingText(seconds) {
    const formatted = formatPing(seconds);
    return `${formatted.value} ${formatted.unit}`;
  }

  function formatUptime(seconds) {
    if (seconds < 3600) {
      return { value: Math.max(1, Math.round(seconds / 60)), unit: "分钟" };
    }
    if (seconds < 86400) {
      return { value: (seconds / 3600).toFixed(2), unit: "小时" };
    }
    return { value: (seconds / 86400).toFixed(2), unit: "天" };
  }

  function formatBits(value) {
    const abs = Math.abs(value);
    if (abs >= 1000000000) return `${(value / 1000000000).toFixed(2)} Gb/s`;
    if (abs >= 1000000) return `${(value / 1000000).toFixed(1)} Mb/s`;
    if (abs >= 1000) return `${(value / 1000).toFixed(1)} kb/s`;
    return `${Math.round(value)} b/s`;
  }

  // Intl.DateTimeFormat construction is comparatively heavy and formatTime
  // runs on every chart axis render -- build the two variants once.
  const timeOnlyFormat = new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false
  });
  const dateTimeFormat = new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false
  });

  function formatTime(timestamp) {
    const date = new Date(timestamp * 1000);
    const now = new Date();
    const sameDay = date.getFullYear() === now.getFullYear()
      && date.getMonth() === now.getMonth()
      && date.getDate() === now.getDate();
    return (sameDay ? timeOnlyFormat : dateTimeFormat).format(date);
  }

  function niceMax(value) {
    if (!Number.isFinite(value) || value <= 0) {
      return 1;
    }
    const exponent = Math.floor(Math.log10(value));
    const base = value / 10 ** exponent;
    const niceBase = base <= 1 ? 1 : base <= 2 ? 2 : base <= 5 ? 5 : 10;
    return niceBase * 10 ** exponent;
  }

  function average(values) {
    const usable = values.filter((value) => Number.isFinite(value));
    return usable.length ? usable.reduce((sum, value) => sum + value, 0) / usable.length : 0;
  }

  function uniqueNames(names) {
    return Array.from(new Set(names.map((name) => String(name || "").trim()).filter(Boolean)));
  }

  function networkLabel(network) {
    if (network === "wired") return "有线";
    if (network === "wireless") return "无线";
    if (network === "all") return "全部";
    return network || "-";
  }

  function seatLabel(seat) {
    return `S${seat}`;
  }

  function gaugeColor(kind, rawValue) {
    if (kind === "ping") {
      if (rawValue >= 0.02) return "#ff4d66";
      if (rawValue >= 0.01) return "#ffe32d";
      return "#73d17a";
    }
    return rawValue < 86400 ? "#ffe32d" : "#73d17a";
  }

  function gaugePercent(kind, rawValue) {
    const max = kind === "ping" ? 0.02 : 2592000;
    return Math.max(0.03, Math.min(1, rawValue / max));
  }

  function smoothValues(values, windowSize) {
    if (!windowSize || windowSize < 2 || values.length < 3) {
      return values;
    }

    return values.map((point, index) => {
      const start = Math.max(0, index - windowSize + 1);
      const window = values.slice(start, index + 1).map((item) => item.v);
      return { ...point, v: average(window) };
    });
  }

  function linePathFromPoints(points, smooth) {
    if (!points.length) return "";
    if (!smooth || points.length < 3) {
      return `M ${points.join(" L ")}`;
    }

    const coords = points.map((point) => {
      const [x, y] = point.split(",").map(Number);
      return { x, y };
    });
    const commands = [`M ${points[0]}`];
    for (let index = 0; index < coords.length - 1; index += 1) {
      const current = coords[index];
      const next = coords[index + 1];
      const previous = coords[index - 1] || current;
      const afterNext = coords[index + 2] || next;
      const cp1x = current.x + (next.x - previous.x) / 6;
      const cp1y = current.y + (next.y - previous.y) / 6;
      const cp2x = next.x - (afterNext.x - current.x) / 6;
      const cp2y = next.y - (afterNext.y - current.y) / 6;
      commands.push(`C ${cp1x.toFixed(1)},${cp1y.toFixed(1)} ${cp2x.toFixed(1)},${cp2y.toFixed(1)} ${next.x.toFixed(1)},${next.y.toFixed(1)}`);
    }
    return commands.join(" ");
  }

  function parseIspBandwidthConfig(raw) {
    const result = { default: { down: 1000, up: 1000 }, perIsp: {} };
    if (raw === undefined || raw === null) return result;
    const text = String(raw).trim();
    if (!text) return result;
    if (/^\d+(\.\d+)?$/.test(text)) {
      const value = Number(text);
      result.default = { down: value, up: value };
      return result;
    }
    text.split(",").forEach((item) => {
      const trimmed = item.trim();
      if (!trimmed) return;
      const colonIdx = trimmed.lastIndexOf(":");
      if (colonIdx <= 0) return;
      const name = trimmed.slice(0, colonIdx).trim();
      const bandwidth = trimmed.slice(colonIdx + 1).trim();
      const parts = bandwidth.split("/").map((part) => Number(part.trim()));
      const down = Number.isFinite(parts[0]) ? parts[0] : null;
      if (down === null) return;
      const up = Number.isFinite(parts[1]) ? parts[1] : down;
      result.perIsp[name] = { down, up };
    });
    return result;
  }

  function parseIspIps(raw) {
    const out = {};
    if (!raw) return out;
    String(raw).split(",").forEach((item) => {
      const idx = item.indexOf(":");
      if (idx <= 0) return;
      const name = item.slice(0, idx).trim();
      const ip = item.slice(idx + 1).trim();
      if (name && ip) out[name] = ip;
    });
    return out;
  }

  function parseConfiguredTargetIps(raw) {
    const ips = new Set();
    if (!raw) return ips;
    String(raw).split(",").forEach((item) => {
      const entry = item.trim();
      if (!entry) return;
      const value = entry.includes(":") ? entry.slice(entry.indexOf(":") + 1).trim() : entry;
      const ip = value.split("-", 1)[0].trim();
      if (/^(?:\d{1,3}\.){3}\d{1,3}$/.test(ip)) ips.add(ip);
    });
    return ips;
  }

  function compactPortLabel(port) {
    let text = String(port || "").trim();
    text = text
      .replace(/^GigabitEthernet/i, "Gi")
      .replace(/^TenGigabitEthernet/i, "Te")
      .replace(/^TwentyFiveGigE/i, "Twe")
      .replace(/^FortyGigabitEthernet/i, "Fo")
      .replace(/^HundredGigE/i, "Hu")
      .replace(/^Port[\s-]*channel/i, "Po")
      .replace(/^Bundle[\s-]*Ether/i, "BE")
      .replace(/^Ethernet[\s-]*Trunk/i, "Eth-Trunk")
      .replace(/\s+active$/i, "");
    return text.length > 18 ? `${text.slice(0, 15)}...` : text;
  }

  function isPortLikeLabel(port) {
    return (
      /^(?:gi|te|twe|fo|hu|xe|xge|sfp|qsfp|fa|eth|ge|xgei|port|po|lag|trk|ae|be|eth-trunk)/i.test(port) ||
      /^\d+(?:\/\d+)+$/.test(port)
    );
  }

  function isAggPortName(port) {
    return /^(?:po|lag|trk|ae|be|eth-trunk)\s*\d+/i.test(port);
  }

  // "YYYY-MM-DD HH:mm:ss" in local time -- Excel parses this directly.
  function formatTimestampFull(timestamp) {
    const date = new Date(timestamp * 1000);
    const pad = (value) => String(value).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
      `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  }

  function csvField(value) {
    const text = String(value == null ? "" : value);
    return /[",\n\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  }

  // Leading BOM so Excel opens the Chinese series names with the right encoding.
  function buildCsv(rows) {
    return "\uFEFF" + rows.map((row) => row.map(csvField).join(",")).join("\r\n");
  }

  const ns = {
    escapeHtml,
    escapeRegex,
    escapeLabel,
    metricName,
    formatPing,
    formatPingText,
    formatUptime,
    formatBits,
    formatTime,
    niceMax,
    average,
    uniqueNames,
    networkLabel,
    seatLabel,
    gaugeColor,
    gaugePercent,
    smoothValues,
    linePathFromPoints,
    parseIspBandwidthConfig,
    parseIspIps,
    parseConfiguredTargetIps,
    formatTimestampFull,
    csvField,
    buildCsv,
    compactPortLabel,
    isPortLikeLabel,
    isAggPortName
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = ns;
  } else {
    window.BSUtils = ns;
  }
}());
