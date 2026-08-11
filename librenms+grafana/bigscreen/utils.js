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
    // 超过 90 天后 "184.79 天" 这种数不直观，先按 30 天换算成月；
    // 超过 12 个月再按年显示，避免出现 "16.6 月" 这类读数。
    if (seconds < 90 * 86400) {
      return { value: (seconds / 86400).toFixed(2), unit: "天" };
    }
    const months = seconds / (30 * 86400);
    if (months <= 12) {
      return { value: months.toFixed(1), unit: "月" };
    }
    return { value: (months / 12).toFixed(1), unit: "年" };
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
    // Include 2.5 so a 201–250 ms spike uses a 250 ms ceiling instead of
    // jumping all the way to 500 ms and leaving half the chart empty.
    const niceBase = base <= 1 ? 1 : base <= 2 ? 2 : base <= 2.5 ? 2.5 : base <= 5 ? 5 : 10;
    return niceBase * 10 ** exponent;
  }

  function roundUpToStep(value, step) {
    const numericValue = Number(value);
    const numericStep = Number(step);
    if (!Number.isFinite(numericValue) || !Number.isFinite(numericStep) || numericStep <= 0) {
      return numericValue;
    }
    // A tiny relative epsilon keeps an exact 30 ms value at 30 ms instead of
    // occasionally rounding to 40 ms due to binary floating-point noise.
    return Math.ceil(numericValue / numericStep - 1e-9) * numericStep;
  }

  function average(values) {
    const usable = values.filter((value) => Number.isFinite(value));
    return usable.length ? usable.reduce((sum, value) => sum + value, 0) / usable.length : 0;
  }

  function median(values) {
    const usable = values.filter((value) => Number.isFinite(value)).sort((a, b) => a - b);
    if (!usable.length) return null;
    const middle = Math.floor(usable.length / 2);
    return usable.length % 2
      ? usable[middle]
      : (usable[middle - 1] + usable[middle]) / 2;
  }

  /**
   * Build one representative infrastructure RTT series without changing the
   * source series. At each raw query timestamp, use the median across targets
   * only when a strict majority of the stable deployed target set contributed.
   *
   * Exact timestamps are preferred. A nearby off-grid sample may be matched
   * once as a scrape-alignment fallback; values are never interpolated and a
   * sample is never reused for multiple output timestamps.
   */
  function aggregateInfrastructurePingTrend(seriesList, options) {
    const settings = options || {};
    const expectedTargetKeys = settings.expectedTargetKeys instanceof Set
      ? new Set(settings.expectedTargetKeys)
      : new Set(Array.isArray(settings.expectedTargetKeys) ? settings.expectedTargetKeys : []);
    const expectedTargets = expectedTargetKeys.size;
    const quorum = expectedTargets ? Math.floor(expectedTargets / 2) + 1 : 0;
    const stepSeconds = Number.isFinite(settings.stepSeconds)
      ? Math.max(0.001, Number(settings.stepSeconds))
      : 2;
    const alignmentToleranceSeconds = Number.isFinite(settings.alignmentToleranceSeconds)
      ? Math.max(0, Number(settings.alignmentToleranceSeconds))
      : 3;
    const targetsByIdentity = new Map();
    (seriesList || []).forEach((series) => {
      const metric = series.metric || {};
      const job = String(metric.job || "").trim();
      const targetIp = String(metric.target_ip || metric.instance || "").trim();
      if (!job || !targetIp) return;
      const identity = `${job}|${targetIp}`;
      // Display-name filtering is intentionally not authoritative. Enforce the
      // current deployed identity set here so retired same-name history cannot
      // become a contributor or influence the representative median.
      if (!expectedTargetKeys.has(identity)) return;
      const byTimestamp = targetsByIdentity.get(identity) || new Map();
      (series.values || []).forEach((point) => {
        const t = Number(point.t);
        const v = Number(point.v);
        // A zero phase duration is not a measured RTT. Failed or absent probes
        // must not manufacture a zero-millisecond network baseline.
        if (Number.isFinite(t) && Number.isFinite(v) && v > 0) {
          const previous = byTimestamp.get(t);
          if (!previous || v > previous.v) byTimestamp.set(t, { t, v });
        }
      });
      targetsByIdentity.set(identity, byTimestamp);
    });
    const targets = Array.from(targetsByIdentity.values()).map((byTimestamp) => ({
      values: Array.from(byTimestamp.values()).sort((left, right) => left.t - right.t),
      usedFallbackSamples: new Set()
    })).filter((target) => target.values.length);

    if (!targets.length || !expectedTargets) {
      return { series: [], coverage: [], expectedTargets, quorum };
    }

    // The most complete target supplies the fixed query grid origin/range.
    // Missing points inside that range are reconstructed as coverage records,
    // not as latency values, so a quorum failure remains diagnosable.
    const reference = targets.reduce((best, target) => (
      !best || target.values.length > best.values.length ? target : best
    ), null);
    const firstTimestamp = reference.values[0].t;
    const lastTimestamp = reference.values[reference.values.length - 1].t;
    const timestamps = [];
    for (
      let timestamp = firstTimestamp;
      timestamp <= lastTimestamp + stepSeconds * 1e-6;
      timestamp += stepSeconds
    ) timestamps.push(Number(timestamp.toFixed(6)));

    const gridIndex = (timestamp) => Math.round((timestamp - firstTimestamp) / stepSeconds);
    const isGridTimestamp = (timestamp) => {
      const index = gridIndex(timestamp);
      if (index < 0 || index >= timestamps.length) return false;
      return Math.abs(firstTimestamp + index * stepSeconds - timestamp) <= 1e-6;
    };
    const exactValues = targets.map((target) => new Map(
      target.values.map((point, index) => [point.t, { point, index }])
    ));

    function valueForTimestamp(target, exact, timestamp) {
      const exactMatch = exact.get(timestamp);
      if (exactMatch) return exactMatch.point.v;
      let nearest = null;
      target.values.forEach((point, index) => {
        // Never steal a real sample from another grid timestamp. The tolerance
        // exists only for genuinely skewed timestamps returned by a backend.
        if (isGridTimestamp(point.t) || target.usedFallbackSamples.has(index)) return;
        const distance = Math.abs(point.t - timestamp);
        if (distance > alignmentToleranceSeconds) return;
        if (!nearest || distance < nearest.distance || (
          distance === nearest.distance && point.t < nearest.point.t
        )) nearest = { point, index, distance };
      });
      if (!nearest) return null;
      target.usedFallbackSamples.add(nearest.index);
      return nearest.point.v;
    }

    const values = [];
    const coverage = [];

    timestamps.forEach((timestamp) => {
      const targetValues = [];
      targets.forEach((target, index) => {
        const value = valueForTimestamp(target, exactValues[index], timestamp);
        if (Number.isFinite(value) && value > 0) targetValues.push(value);
      });
      const quorumMet = targetValues.length >= quorum;
      coverage.push({
        t: timestamp,
        contributors: targetValues.length,
        expectedTargets,
        quorum,
        quorumMet
      });
      const aggregateMedian = quorumMet ? median(targetValues) : null;
      if (quorumMet && Number.isFinite(aggregateMedian)) {
        values.push({ t: timestamp, v: aggregateMedian });
      }
    });

    return {
      series: values.length ? [{
        name: settings.name || "典型设备中位数",
        metric: {
          aggregate: "cross-target-median",
          expected_targets: String(expectedTargets),
          quorum: String(quorum)
        },
        values
      }] : [],
      coverage,
      expectedTargets,
      quorum
    };
  }

  function buildInfrastructurePingTrend(seriesList, options) {
    const settings = options || {};
    if (settings.tournament) {
      return {
        series: suppressIsolatedLatencySpikes(seriesList, {
          threshold: 0.02,
          minConsecutive: 2,
          maxGapSeconds: 3
        }),
        coverage: [],
        expectedTargets: (seriesList || []).length,
        quorum: null
      };
    }
    return aggregateInfrastructurePingTrend(seriesList, settings);
  }

  /**
   * Keep sustained latency incidents visible while removing isolated ICMP
   * response spikes from the infrastructure Ping trend. Prometheus
   * data is left untouched; this function returns copied series/points.
   */
  function suppressIsolatedLatencySpikes(seriesList, options) {
    const settings = options || {};
    const threshold = Number.isFinite(settings.threshold) ? settings.threshold : 0.02;
    const minConsecutive = Math.max(2, Math.floor(settings.minConsecutive || 2));
    const maxGapSeconds = Number.isFinite(settings.maxGapSeconds) ? settings.maxGapSeconds : 3;
    const replacementRadius = Math.max(1, Math.floor(settings.replacementRadius || 5));
    const replacementWindowSeconds = Number.isFinite(settings.replacementWindowSeconds)
      ? Math.max(maxGapSeconds, settings.replacementWindowSeconds)
      : Math.max(12, maxGapSeconds * replacementRadius);

    return (seriesList || []).map((series) => {
      const source = (series.values || []).map((point) => ({ ...point }));
      const values = source.map((point) => ({ ...point }));

      function nearestNormalSample(index) {
        // Replacement must be an actual observed sample, never an average or
        // synthesized baseline. Prefer the immediately preceding normal point;
        // if it is unavailable, use the following point at the same distance.
        const centerTime = source[index] && source[index].t;
        function usable(point) {
          if (!point || !Number.isFinite(point.v) || point.v >= threshold) return false;
          if (
            Number.isFinite(centerTime)
            && Number.isFinite(point.t)
            && Math.abs(point.t - centerTime) > replacementWindowSeconds
          ) return false;
          return true;
        }

        for (let distance = 1; distance <= replacementRadius; distance += 1) {
          const previous = source[index - distance];
          if (usable(previous)) return previous.v;
          const next = source[index + distance];
          if (usable(next)) return next.v;
        }
        return null;
      }

      let start = 0;
      while (start < values.length) {
        const point = values[start];
        if (!Number.isFinite(point.v) || point.v < threshold) {
          start += 1;
          continue;
        }

        let end = start + 1;
        while (
          end < values.length
          && Number.isFinite(values[end].v)
          && values[end].v >= threshold
          && Number.isFinite(values[end].t)
          && Number.isFinite(values[end - 1].t)
          && values[end].t - values[end - 1].t <= maxGapSeconds
        ) {
          end += 1;
        }

        if (end - start < minConsecutive) {
          for (let index = start; index < end; index += 1) {
            const replacement = nearestNormalSample(index);
            if (Number.isFinite(replacement)) values[index].v = replacement;
          }
        }
        start = end;
      }

      return { ...series, values };
    });
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
      const rawCp1y = current.y + (next.y - previous.y) / 6;
      const cp2x = next.x - (afterNext.x - current.x) / 6;
      const rawCp2y = next.y - (afterNext.y - current.y) / 6;
      // Catmull-Rom control points can overshoot after a sharp latency peak.
      // SVG's Y axis grows downward, so that overshoot appeared below the
      // zero-latency baseline even though every real sample was non-negative.
      // A cubic Bezier stays inside the convex hull of its control points;
      // clamping both controls to this segment's endpoint range therefore
      // preserves visual smoothing without inventing local minima or maxima.
      const minY = Math.min(current.y, next.y);
      const maxY = Math.max(current.y, next.y);
      const cp1y = Math.min(maxY, Math.max(minY, rawCp1y));
      const cp2y = Math.min(maxY, Math.max(minY, rawCp2y));
      commands.push(`C ${cp1x.toFixed(1)},${cp1y.toFixed(1)} ${cp2x.toFixed(1)},${cp2y.toFixed(1)} ${next.x.toFixed(1)},${next.y.toFixed(1)}`);
    }
    return commands.join(" ");
  }

  function stepPathFromPoints(points) {
    if (!points.length) return "";
    const commands = [`M ${points[0]}`];
    for (let index = 1; index < points.length; index += 1) {
      const [x, y] = points[index].split(",");
      commands.push(`H ${x} V ${y}`);
    }
    return commands.join(" ");
  }

  function splitPointsOnGaps(values, maxGapSeconds) {
    if (!values.length) return [];
    const maxGap = Number(maxGapSeconds);
    if (!Number.isFinite(maxGap) || maxGap <= 0) return [values.slice()];
    const segments = [[values[0]]];
    for (let index = 1; index < values.length; index += 1) {
      const point = values[index];
      const previous = values[index - 1];
      if (point.t - previous.t > maxGap) segments.push([]);
      segments[segments.length - 1].push(point);
    }
    return segments;
  }

  function parseIspBandwidthConfig(raw) {
    const result = { default: { down: 1000, up: 1000 }, perIsp: {}, ordered: [] };
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
      const entry = { down, up };
      if (name === "*") {
        result.default = entry;
        return;
      }
      result.perIsp[name] = entry;
      result.ordered.push(entry);
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

  function groupAddressesByCBlock(addresses) {
    const blocks = [];
    (addresses || []).forEach((ip) => {
      const prefix = String(ip || "").split(".").slice(0, 3).join(".");
      if (prefix.split(".").length !== 3) return;
      let block = blocks[blocks.length - 1];
      if (!block || block.prefix !== prefix) {
        block = { prefix, addresses: [] };
        blocks.push(block);
      }
      block.addresses.push(ip);
    });
    return blocks;
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
    roundUpToStep,
    average,
    aggregateInfrastructurePingTrend,
    buildInfrastructurePingTrend,
    suppressIsolatedLatencySpikes,
    uniqueNames,
    networkLabel,
    seatLabel,
    gaugeColor,
    gaugePercent,
    linePathFromPoints,
    stepPathFromPoints,
    splitPointsOnGaps,
    parseIspBandwidthConfig,
    parseIspIps,
    parseConfiguredTargetIps,
    formatTimestampFull,
    csvField,
    buildCsv,
    groupAddressesByCBlock,
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
