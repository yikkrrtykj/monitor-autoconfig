;(function () {
  'use strict';

  const jobs = Object.freeze({
    "infra-core-ping": ["lan", "Core", "infra"],
    "infra-dist-ping": ["lan", "Dist", "infra"],
    "infra-fw-ping": ["lan", "FW", "infra"],
    "infra-fw-unit-ping": ["lan", "FW Unit", "infra"],
    "infra-isp-ping": ["isp", "ISP", "isp"],
    "infra-srv-ping": ["server", "Server", "infra"],
    "infra-ap-ping": ["ap", "AP", "ap"],
    "player-ping": ["player", "Player", "player"]
  });
  const jobOrder = Object.keys(jobs);
  const defaults = Object.freeze({ isp: 1, infra: 2, player: 5, ap: 5 });
  const minCoverage = 0.8;
  const minRttSamples = 30;
  const maxHistory = 7 * 86400;
  const maxRaw = 3600;
  const defaultStats = 300;
  const maxCompared = 4;
  const pageSize = 12;

  function durationSeconds(value) {
    const units = { y: 31536000, w: 604800, d: 86400, h: 3600, m: 60, s: 1, ms: 0.001 };
    if (!/^(?:\d+(?:ms|[ywdhms]))+$/.test(value || "")) return null;
    const seconds = [...value.matchAll(/(\d+)(ms|[ywdhms])/g)]
      .reduce((sum, [, count, unit]) => sum + Number(count) * units[unit], 0);
    return Number.isFinite(seconds) && seconds > 0 ? seconds : null;
  }

  function policy(config = {}) {
    const intervals = Object.fromEntries(Object.entries(defaults).map(([group, seconds]) => [
      group, durationSeconds(config.pingIntervals?.[group]) ?? seconds
    ]));
    const loss = Number(config.pingQualityLossPct);
    const rtt = Number(config.pingQualityRttMs);
    return {
      intervals,
      stageTerms: String(config.stageDeviceFilter || "stage,wutai,舞台").toLowerCase().split(",").map((term) => term.trim()).filter(Boolean),
      lossThreshold: loss > 0 && loss <= 100 ? loss : 5,
      rttThreshold: rtt > 0 && Number.isFinite(rtt) ? rtt : 50
    };
  }

  function identity(metric) {
    // Keep every target label. Removing a name or IP from the key would join
    // distinct Prometheus series across readdressing, renames, or seat changes.
    return JSON.stringify(Object.entries(metric)
      .filter(([key]) => !["__name__", "phase", "ping_field"].includes(key))
      .sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0));
  }

  function targetFromMetric(metric, settings) {
    const job = metric.job;
    if (!Object.hasOwn(jobs, job) || !metric.instance) return null;
    const [category, defaultGroup, intervalGroup] = jobs[job];
    const { team, seat, network } = metric;
    const seatLabel = [team && `队 ${team}`, seat && `座 ${seat}`, network].filter(Boolean).join(" · ");
    const name = metric.display_name || (category === "player" && seatLabel) || metric.instance;
    const group = job === "infra-dist-ping" && settings.stageTerms.some((term) => name.toLowerCase().includes(term)) ? "Stage" : defaultGroup;
    return {
      key: identity(metric),
      metric,
      job,
      category,
      group,
      interval: settings.intervals[intervalGroup],
      lossWindow: ["player", "ap"].includes(category) ? 300 : 60,
      name,
      ip: metric.target_ip || metric.instance,
      subtitle: category === "ap" ? "ICMP · 在线状态见无线总览" : name === seatLabel ? "" : seatLabel
    };
  }

  function number(value) {
    if (value === null || value === undefined || value === "") return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function lossStats(total, success, seconds, interval) {
    if (!Number.isInteger(total) || !Number.isInteger(success) || success < 0 || success > total) {
      return { total: 0, success: 0, failed: 0, coverage: 0, loss: null };
    }
    const coverage = Math.min(1, total * interval / seconds);
    return {
      total, success, failed: total - success, coverage,
      loss: total > 0 && coverage >= minCoverage ? (total - success) * 100 / total : null
    };
  }

  function rttMs(value) {
    const ms = value * 1000;
    return value > 0 && Number.isFinite(ms) ? ms : null;
  }

  function snapshot(result, at, settings) {
    const fields = new Map();
    for (const { metric, value } of result) {
      const key = identity(metric);
      if (!fields.has(key)) fields.set(key, new Map());
      const targetFields = fields.get(key);
      // Duplicate field/identity combinations are ambiguous and cannot
      // provide a status or be substituted with another target's result.
      targetFields.set(metric.ping_field, targetFields.has(metric.ping_field)
        ? { value: null } : { metric, value: number(value?.[1]) });
    }
    const targets = [];
    for (const record of fields.values()) {
      const inventory = record.get("target");
      if (inventory?.value == null) continue;
      const target = targetFromMetric(inventory.metric, settings);
      if (!target) continue;
      const get = (key) => record.get(key)?.value ?? null;
      const sourceAt = get("source_at");
      const fresh = sourceAt !== null && sourceAt <= at && at - sourceAt <= Math.max(10, target.interval * 3);
      const valid = fresh && get("source") === 1 && get("probe_at") === sourceAt && [0, 1].includes(get("probe"));
      const rtt = valid && get("probe") === 1 && get("rtt_at") === sourceAt ? rttMs(get("rtt")) : null;
      const loss = lossStats(get("count"), get("ok"), target.lossWindow, target.interval);
      let status = "UNKNOWN";
      if (valid) {
        if (get("probe") === 0) status = "DOWN";
        else if (loss.loss >= settings.lossThreshold || rtt >= settings.rttThreshold) status = "DEGRADED";
        else status = "UP";
      }
      targets.push({
        ...target, status, rtt, loss: valid ? loss : { ...loss, loss: null },
        reason: status !== "UNKNOWN" ? "" : !fresh ? "数据缺失或过期" : get("source") === 0 ? "Prometheus 抓取失败" : "探测结果缺失",
        sourceAt: valid ? sourceAt : null
      });
    }
    return targets.sort((a, b) => jobOrder.indexOf(a.job) - jobOrder.indexOf(b.job)
      || a.name.localeCompare(b.name, "zh-CN", { numeric: true }) || a.key.localeCompare(b.key));
  }

  function rawRows(result, targets, start, end) {
    const byKey = new Map(targets.map((target) => [target.key, new Map()]));
    for (const { metric, values } of result) {
      const rows = byKey.get(identity(metric));
      const field = { up: "scrape", probe_success: "probe", probe_icmp_duration_seconds: "rtt" }[metric.__name__];
      if (!rows || !field) continue;
      for (const [t, raw] of values) {
        if (!Number.isFinite(t) || t <= start || t > end) continue;
        if (!rows.has(t)) rows.set(t, { t });
        const row = rows.get(t);
        row[field] = Object.hasOwn(row, field) ? null : number(raw);
      }
    }
    return new Map([...byKey].map(([key, rows]) => [key,
      [...rows.values()].sort((a, b) => a.t - b.t).map(({ t, scrape, probe, rtt }) => {
        const success = scrape === 1 && [0, 1].includes(probe) ? probe : null;
        return { t, scrape: [0, 1].includes(scrape) ? scrape : null, success, rtt: success === 1 ? rttMs(rtt) : null };
      })
    ]));
  }

  function percentile(sorted, fraction) {
    const index = (sorted.length - 1) * fraction;
    const lower = Math.floor(index);
    return sorted[lower] + (sorted[Math.ceil(index)] - sorted[lower]) * (index - lower);
  }

  function rawStats(rows, seconds, interval) {
    let total = 0;
    let success = 0;
    const rtts = [];
    for (const row of rows) {
      if (row.success === null) continue;
      total += 1;
      success += row.success;
      if (row.rtt !== null) rtts.push(row.rtt);
    }
    const loss = lossStats(total, success, seconds, interval);
    const rttCoverage = Math.min(1, rtts.length * interval / seconds);
    const sufficient = rtts.length >= minRttSamples && rttCoverage >= minCoverage;
    const mean = rtts.length ? rtts.reduce((sum, value) => sum + value, 0) / rtts.length : null;
    if (sufficient) rtts.sort((a, b) => a - b);
    return {
      ...loss, rttSamples: rtts.length, rttCoverage,
      p50: sufficient ? percentile(rtts, 0.5) : null,
      p95: sufficient ? percentile(rtts, 0.95) : null,
      max: rtts.length ? rtts.reduce((max, value) => Math.max(max, value)) : null,
      stddev: sufficient ? Math.sqrt(rtts.reduce((sum, value) => sum + (value - mean) ** 2, 0) / rtts.length) : null
    };
  }

  function bucketRows(rows, start, end, seconds, interval) {
    const count = Math.ceil((end - start) / seconds);
    const bins = Array.from({ length: count }, () => []);
    for (const row of rows) {
      const index = Math.ceil((row.t - start) / seconds) - 1;
      if (index >= 0 && index < count && row.t <= end) bins[index].push(row);
    }
    return bins.map((samples, index) => {
      const from = start + index * seconds;
      const to = Math.min(end, from + seconds);
      return { from, to, ...rawStats(samples, to - from, interval) };
    });
  }

  const ns = { jobs, minCoverage, minRttSamples, maxHistory, maxRaw, defaultStats, maxCompared, pageSize,
    policy, identity, number, lossStats, rttMs, snapshot, rawRows, rawStats, bucketRows };
  if (typeof module !== "undefined" && module.exports) module.exports = ns;
  else window.BSPingModel = ns;
})();
