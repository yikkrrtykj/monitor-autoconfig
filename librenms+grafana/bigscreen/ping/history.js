;(function () {
  'use strict';

  const common = typeof module !== "undefined" && module.exports;
  const model = common ? require("./model.js") : window.BSPingModel;
  const dns = common ? require("./custom-dns.js") : window.BSPingCustomDNS;
  const presets = Object.freeze({ "5m": 300, "15m": 900, "1h": 3600, "6h": 21600, "24h": 86400, "7d": model.maxHistory });
  const minZoom = 30;

  function parseTarget(key, settings) {
    // URLs contain canonical Prometheus labels, not a display name or IP shortcut.
    if (!key || key.length > 2048) return null;
    const domain = dns.parseTarget(key);
    if (domain) return domain;
    let entries;
    try { entries = JSON.parse(key); } catch { return null; }
    if (!Array.isArray(entries) || entries.length > 32 || entries.some((entry) =>
      !Array.isArray(entry) || entry.length !== 2 || typeof entry[0] !== "string"
      || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(entry[0]) || typeof entry[1] !== "string")) return null;
    const labels = Object.fromEntries(entries);
    if (labels.job === "player-ping" || model.identity(labels) !== key) return null;
    return model.targetFromMetric(labels, settings);
  }

  const isHistory = (params) => params.get("view") === "history" || params.has("target");

  function parseRoute(params, now, settings) {
    const atParam = params.get("at");
    const atValue = atParam ? (/^\d{10}$/.test(atParam) ? Number(atParam) : Math.floor(Date.parse(atParam) / 1000)) : NaN;
    const at = Number.isInteger(atValue) && atValue > 300 && atValue <= now ? atValue : null;
    const end = at ?? now;
    const range = validateRange(Number(params.get("from")), Number(params.get("to")), now) || { start: end - 300, end };
    const keys = [];
    for (const [name, value] of params) {
      if ((name === "target" || name === "compare") && !keys.includes(value)) keys.push(value);
      if (keys.length === model.maxCompared) break;
    }
    const targets = keys.map((key) => parseTarget(key, settings)).filter(Boolean);
    return { targets, invalid: keys.length !== targets.length, range, at };
  }

  function urlFor(targets, range, at) {
    const params = new URLSearchParams({ view: "history" });
    targets.forEach(({ key }, index) => params.append(index ? "compare" : "target", key));
    if (at !== null) params.set("at", String(at));
    params.set("from", String(range.start));
    params.set("to", String(range.end));
    return "/ping?" + params;
  }

  function validateRange(start, end, now) {
    return Number.isSafeInteger(start) && Number.isSafeInteger(end) && start > 0 && start < end
      && end <= now && end - start <= model.maxHistory ? { start, end } : null;
  }

  function shiftRange(range, seconds, now) {
    const span = range.end - range.start;
    const start = Math.max(1, Math.min(now - span, range.start + Math.round(seconds)));
    return { start, end: start + span };
  }

  function statisticsWindow(range, loaded, time, selected) {
    const requestedStart = time - selected;
    const historyStart = Math.max(range.start, requestedStart);
    const historyEnd = Math.min(range.end, time);
    const start = Math.max(historyStart, loaded.start);
    const end = Math.min(historyEnd, loaded.end);
    return { start, end, selected,
      historyClipped: historyStart > requestedStart || historyEnd < time,
      loadClipped: start > historyStart || end < historyEnd };
  }

  function lowerBound(rows, t, field) {
    let start = 0;
    let end = rows.length;
    while (start < end) {
      const middle = (start + end) >>> 1;
      if (rows[middle][field] < t) start = middle + 1;
      else end = middle;
    }
    return start;
  }

  function nearest(points, time, interval) {
    const index = lowerBound(points, time, "t");
    const left = points[index - 1];
    const right = points[index];
    const sample = !left || (right && right.t - time < time - left.t) ? right : left;
    return sample && Math.abs(sample.t - time) <= interval * 0.75 ? sample : null;
  }

  function sampleStatus(sample) {
    if (!sample) return "此处无原始样本";
    if (sample.scrape === 0) return "抓取失败";
    if (sample.success === 0) return "ICMP 丢包（DOWN）";
    if (sample.success === 1) return Number.isFinite(sample.rtt) ? "成功（UP）" : "探测成功 · RTT 缺样";
    return "缺样（UNKNOWN）";
  }

  function bucketSelection({ target, points, samples = points }, bucket, aggregated) {
    if (aggregated) {
      return { rows: [{ target, bucket }], time: bucket.to, rtt: null, canPin: true,
        aggregated: true, targetKey: target.key };
    }
    if (target.dns && !bucket.segment) return null;
    let sample = null;
    let rank = 0;
    for (let index = lowerBound(samples, bucket.from, "t"); index < samples.length; index++) {
      const point = samples[index];
      if (point.t > bucket.to) break;
      if (point.t <= bucket.from || point.scrape === undefined) continue;
      if (target.dns && (!point.verified || point.ip !== bucket.segment.ip ||
          point.segment?.generation !== bucket.segment.generation)) continue;
      // Within each class the newest record wins. A diagnostic record never
      // outranks a probe result or turns a failed scrape into packet loss.
      const priority = point.scrape === 1 && point.success === 0 ? 3 :
        point.scrape === 1 && point.success === 1 ? 2 : 1;
      if (priority >= rank) {
        sample = point;
        rank = priority;
      }
    }
    return sample && { rows: [{ target, sample }], time: sample.t, rtt: sample.rtt, canPin: true,
      aggregated: false, targetKey: target.key };
  }

  function selection(series, time, aggregated) {
    let closest = null;
    const rows = series.map(({ target, points, samples = points, buckets }) => {
      if (aggregated) {
        const candidate = buckets[lowerBound(buckets, time, "to")];
        const bucket = candidate && candidate.from <= time ? candidate : null;
        if (bucket && !closest) closest = { time: bucket.to, rtt: null };
        return { target, bucket };
      }
      const sample = nearest(samples, time, target.interval);
      if (sample && (!closest || Math.abs(sample.t - time) < Math.abs(closest.time - time))) {
        closest = { time: sample.t, rtt: sample.rtt };
      }
      return { target, sample };
    });
    return { rows, time: closest?.time ?? time, rtt: closest?.rtt ?? null, canPin: closest !== null, aggregated };
  }

  const api = { presets, minZoom, parseTarget, isHistory, parseRoute, urlFor, validateRange, shiftRange, statisticsWindow, nearest, sampleStatus, bucketSelection, selection };
  if (common) module.exports = api;
  else window.BSPingHistory = api;
})();
