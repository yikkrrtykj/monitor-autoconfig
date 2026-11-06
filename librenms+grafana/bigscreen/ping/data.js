;(function () {
  'use strict';

  const common = typeof module !== "undefined" && module.exports;
  const model = common ? require("./model.js") : window.BSPingModel;
  const { escapeRegex } = common ? require("../utils.js") : window.BSUtils;
  const allJobs = Object.keys(model.jobs);
  const slowJobs = ["player-ping", "infra-ap-ping"];
  const fastJobs = allJobs.filter((job) => !slowJobs.includes(job));

  function matcher(label, values) {
    const terms = [...new Set(values)];
    if (terms.length === 1) return `${label}=${JSON.stringify(terms[0])}`;
    return `${label}=~${JSON.stringify(terms.map(escapeRegex).join("|"))}`;
  }

  function targetMatchers(targets) {
    return [
      matcher("job", targets.map(({ job }) => job)),
      matcher("instance", targets.map(({ metric }) => metric.instance)),
      matcher("target_ip", targets.map(({ metric }) => metric.target_ip || ""))
    ].join(",");
  }

  function tagged(field, expr) {
    return `label_replace(${expr}, "ping_field", "${field}", "job", ".+")`;
  }

  function currentFields(match) {
    const up = `up{${match}}`;
    const probe = `probe_success{${match}}`;
    const rtt = `probe_icmp_duration_seconds{${match},phase="rtt"}`;
    return [
      tagged("source", up), tagged("source_at", `timestamp(${up})`),
      tagged("probe", probe), tagged("probe_at", `timestamp(${probe})`),
      tagged("rtt", rtt), tagged("rtt_at", `timestamp(${rtt})`)
    ];
  }

  function lossFields(match, seconds) {
    // Blackbox emits one binary probe_success for each successful scrape.
    // A failed scrape emits up=0 but no probe_success sample.
    const selector = `probe_success{${match}}[${seconds}s]`;
    return [
      tagged("count", `count_over_time(${selector})`),
      tagged("ok", `sum_over_time(${selector})`)
    ];
  }

  function createPingData({ query, settings }) {
    const all = matcher("job", allJobs);
    const inventorySeconds = Math.min(1800, Math.max(300, Math.ceil(Math.max(...Object.values(settings.intervals)) * 3)));
    const summaryQuery = [
      tagged("target", `last_over_time(up{${all}}[${inventorySeconds}s])`),
      ...currentFields(all),
      ...lossFields(matcher("job", fastJobs), 60),
      ...lossFields(matcher("job", slowJobs), 300)
    ].join(" or ");

    async function summary(at, signal) {
      const result = await query(summaryQuery, { time: at }, signal);
      if (result.length > 9 * 3000) throw new Error("监控目标超出列表上限");
      return model.snapshot(result, at, settings);
    }

    async function raw(targets, start, end, signal) {
      const duration = Math.ceil(end - start);
      if (!targets.length || targets.length > model.pageSize || duration < 1 || duration > model.maxRaw) {
        throw new Error("原始样本查询超出范围");
      }
      const expected = targets.reduce((sum, target) => sum + duration / target.interval, 0);
      if (expected > 24000) throw new Error("原始样本过多，请缩短查询窗口");
      const match = targetMatchers(targets);
      const expr = `{__name__=~"up|probe_success|probe_icmp_duration_seconds",${match},phase=~"rtt|"}[${duration}s]`;
      const result = await query(expr, { time: end }, signal);
      return model.rawRows(result, targets, start, end);
    }

    async function overview(targets, start, end, signal) {
      const span = end - start;
      if (!targets.length || targets.length > model.maxCompared || span <= model.maxRaw || span > model.maxHistory) {
        throw new Error("历史概览查询超出范围");
      }
      const step = Math.ceil(span / 240);
      // Anchor at the requested end. The first, potentially partial bucket is
      // read separately so its denominator and data never extend before start.
      const first = end - Math.floor(span / step) * step;
      const match = targetMatchers(targets);
      const range = { start: first + step, end, step };
      const [counts, current, partial] = await Promise.all([
        query(lossFields(match, step).join(" or "), range, signal),
        query(currentFields(match).join(" or "), range, signal),
        first > start ? query(lossFields(match, first - start).join(" or "), { time: first }, signal) : []
      ]);
      const records = new Map(targets.map(({ key }) => [key, new Map()]));
      for (const { metric, values, value } of [...counts, ...current, ...partial]) {
        const record = records.get(model.identity(metric));
        if (!record) continue;
        for (const [t, rawValue] of values || [value]) {
          if (!record.has(t)) record.set(t, new Map());
          const fields = record.get(t);
          const name = metric.ping_field;
          fields.set(name, fields.has(name) ? null : model.number(rawValue));
        }
      }
      return new Map(targets.map((target) => {
        const record = records.get(target.key);
        const buckets = [];
        const points = [];
        for (let to = first > start ? first : first + step; to <= end; to += step) {
          const from = Math.max(start, to - step);
          const fields = record.get(to);
          const get = (name) => fields?.get(name) ?? null;
          buckets.push({ from, to, ...model.lossStats(get("count"), get("ok"), to - from, target.interval) });
          const at = get("source_at");
          const valid = at !== null && at > from && at <= to && to - at <= Math.max(10, target.interval * 3)
            && get("source") === 1 && get("probe") === 1 && get("probe_at") === at && get("rtt_at") === at;
          points.push({ t: valid ? at : to, rtt: valid ? model.rttMs(get("rtt")) : null, success: null });
        }
        return [target.key, { buckets, points, step }];
      }));
    }

    return { summary, raw, overview };
  }

  if (common) module.exports = { createPingData };
  else window.BSPingData = { createPingData };
})();
