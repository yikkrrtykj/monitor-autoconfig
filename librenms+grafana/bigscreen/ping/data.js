;(function () {
  'use strict';

  const common = typeof module !== "undefined" && module.exports;
  const model = common ? require("./model.js") : window.BSPingModel;
  const { escapeRegex } = common ? require("../utils.js") : window.BSUtils;
  const deviceJobs = Object.keys(model.jobs).filter((job) => job !== "player-ping" && job !== "infra-ap-ping");
  // Device names are display metadata resolved from the same SNMP sysName
  // series the network overview uses. A configured name always wins; this only
  // fills targets whose Prometheus label is still an IP placeholder.
  const nameLookback = 12 * 3600;
  const nameTtl = 60;
  const nameTimeout = 2000;
  const nameQuery = `max_over_time(sysName{job=~"infra-switch-snmp|infra-fw-snmp|infra-fw-unit-snmp"}[${nameLookback}s])`;
  const placeholderIp = /^\d{1,3}(?:\.\d{1,3}){3}$/;
  const genericSysName = /^(?:switch|router|firewall|amnesiac)$/i;

  function sysNameMap(rows) {
    const map = new Map();
    for (const { metric } of rows) {
      const name = String(metric?.sysName || "").trim();
      // An IP-shaped or factory-generic sysName is not a usable display name;
      // keep the target's configured label rather than a worse placeholder.
      if (!name || placeholderIp.test(name) || genericSysName.test(name)) continue;
      for (const key of [metric.target_ip, metric.instance, metric.display_name]) {
        const text = String(key || "").trim();
        if (text) map.set(text, name);
      }
    }
    return map;
  }

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

  function createPingData({ query, settings, inventoryMaxAge = 0, includeAp = true, resolveDeviceNames = false }) {
    const allowedJobs = new Set(includeAp ? [...deviceJobs, "infra-ap-ping"] : deviceJobs);
    const all = matcher("job", allowedJobs);
    const { inventorySeconds } = model;
    // Keep the preceding five minutes separate so an expiring target still
    // leaves a coverage notice, including on reload. Neither window proves intent.
    const inventoryFields = [
      tagged("target", `last_over_time(up{${all}}[${inventorySeconds}s])`),
      tagged("aged_out", `last_over_time(up{${all}}[300s] offset ${inventorySeconds}s)`)
    ];
    const liveFields = [
      ...currentFields(all),
      ...lossFields(matcher("job", deviceJobs), 60),
      ...(includeAp ? lossFields(matcher("job", ["infra-ap-ping"]), 300) : [])
    ];
    const liveQuery = liveFields.join(" or ");
    const summaryQuery = [...inventoryFields, ...liveFields].join(" or ");
    let inventory = null;
    let names = { at: -Infinity, map: new Map() };

    // One bounded query per minute at most. Names are display-only, so a slow
    // or failed lookup returns the previous map instead of delaying or failing
    // the monitoring snapshot.
    async function deviceNames(at, signal) {
      if (at >= names.at && at - names.at < nameTtl) return names.map;
      const timeout = new AbortController();
      const timer = setTimeout(() => timeout.abort(), nameTimeout);
      let map = names.map;
      try {
        const rows = await query(nameQuery, { time: at },
          signal ? AbortSignal.any([signal, timeout.signal]) : timeout.signal);
        map = sysNameMap(rows);
      } catch (error) {
        // A slow or failed lookup keeps the previous names and still records
        // the attempt, so a display-only query is retried at most once a minute
        // and never stalls or fails the monitoring snapshot.
        if (signal?.aborted) throw error;
      } finally {
        clearTimeout(timer);
      }
      if (at >= names.at) names = { at, map };
      return names.map;
    }

    async function summary(at, signal) {
      const cached = inventory && at >= inventory.at && at - inventory.at < inventoryMaxAge ? inventory : null;
      const [response, nameMap] = await Promise.all([
        query(cached ? liveQuery : summaryQuery, { time: at }, signal),
        resolveDeviceNames ? deviceNames(at, signal) : null
      ]);
      if (response.length > 10 * 3000) throw new Error("监控目标超出列表上限");
      const result = response.filter(({ metric }) => allowedJobs.has(metric.job));
      // The overview can reuse membership briefly, but always classifies
      // with new measurements and admits newly observed identities at once.
      const inventoryRows = cached?.rows || result.filter(({ metric }) =>
        metric.ping_field === "target" || metric.ping_field === "aged_out");
      const snapshotRows = cached ? [...inventoryRows, ...result] : result;
      const addedTargets = [];
      if (cached) {
        const known = new Set(inventoryRows.filter(({ metric }) => metric.ping_field === "target")
          .map(({ metric }) => model.identity(metric)));
        for (const { metric, value } of result) {
          if (metric.ping_field !== "source") continue;
          const key = model.identity(metric);
          if (!known.has(key)) {
            const target = { metric: { ...metric, ping_field: "target" }, value };
            known.add(key);
            addedTargets.push(target);
            snapshotRows.push(target);
          }
        }
      }
      const targets = model.snapshot(snapshotRows, at, settings, nameMap);
      const keys = new Set(targets.map(({ key }) => key));
      const agedOut = new Set();
      for (const { metric } of inventoryRows) {
        if (metric.ping_field !== "aged_out") continue;
        const key = model.identity(metric);
        if (!keys.has(key)) agedOut.add(key);
      }
      if (targets.length + agedOut.size > 3000) throw new Error("监控目标超出列表上限");
      if (inventoryMaxAge > 0 && !cached && !signal?.aborted) inventory = { at, rows: inventoryRows };
      if (cached && addedTargets.length && !signal?.aborted) inventory = {
        at: cached.at, rows: [...inventoryRows, ...addedTargets]
      };
      return { targets, agedOut: agedOut.size };
    }

    async function fixedCustom(at, signal) {
      const match = 'job="custom-ping"';
      const rows = await query([...currentFields(match), ...lossFields(match, 60)].join(" or "), { time: at }, signal);
      if (rows.length > 30000) throw new Error("自定义监控样本超出上限");
      const fields = rows.filter(({ metric }) => model.customMetric(metric));
      const inventory = fields.filter(({ metric }) => metric.ping_field === "source")
        .map((row) => ({ ...row, metric: { ...row.metric, ping_field: "target" } }));
      return new Map(model.snapshot([...inventory, ...fields], at, settings).map((target) => [target.key, target]));
    }

    async function custom(at, signal, endpoints = [], fixed = true) {
      const [samples, domainSamples] = await Promise.all([
        fixed ? fixedCustom(at, signal) : new Map(),
        endpoints.length ? dnsRaw(endpoints, at - 60, at, signal, true).catch((error) => {
          if (!fixed) throw error;
          return new Map();
        }) : new Map()
      ]);
      for (const target of endpoints) {
        const rows = (domainSamples.get(target.key) || []).filter((point) => point.t > target.sampleNotBefore);
        const last = rows.at(-1);
        if (!last || last.scrape !== 1 || ![0, 1].includes(last.success)) continue;
        samples.set(target.key, {
          ...target, sourceAt: last.t, status: last.success ? "UP" : "DOWN", rtt: last.rtt,
          loss: model.rawStats(rows, 60, target.interval)
        });
      }
      return samples;
    }

    function dnsMatcher(targets) {
      // Keep the URL bounded at 100 IDs. The returned physical series are
      // still matched by exact UUID and IP for live reads.
      return ['job="custom-ping"', matcher("custom_id", targets.map((target) => target.metric.custom_id))].join(",");
    }

    async function dnsRaw(targets, start, end, signal, live = false, queryOptions) {
      const span = Math.ceil(end - start);
      const limit = live ? 100 : model.maxCompared;
      if (!targets.length || targets.length > limit || span < 1 || span > model.maxRaw) {
        throw new Error("域名原始样本查询超出范围");
      }
      const match = dnsMatcher(targets);
      const expr = '{__name__=~"up|probe_success|probe_icmp_duration_seconds",' + match + ',phase=~"rtt|"}[' + span + 's]';
      const result = await query(expr, { time: end }, signal, queryOptions);
      const byId = new Map(targets.map((row) => [row.metric.custom_id, row]));
      const physical = new Map();
      let values = 0;
      for (const entry of result) {
        const { metric } = entry;
        const domain = byId.get(metric?.custom_id);
        if (!domain || !model.customMetric(metric) || live && domain.ip !== metric.target_ip) continue;
        const key = model.identity(metric);
        physical.set(key, { key, domain, ip: metric.target_ip });
        values += entry.values?.length ?? 0;
        if (values > 60000) throw new Error("域名原始样本过多，请缩短查询窗口");
      }
      const output = new Map(targets.map(({ key }) => [key, []]));
      for (const [key, points] of model.rawRows(result, [...physical.values()], start, end)) {
        const { domain, ip } = physical.get(key);
        const rows = output.get(domain.key);
        for (const point of points) rows.push({ ...point, ip, targetKey: key });
      }
      for (const rows of output.values()) rows.sort((a, b) => a.t - b.t || a.ip.localeCompare(b.ip));
      return output;
    }

    async function raw(targets, start, end, signal, queryOptions) {
      const duration = Math.ceil(end - start);
      if (!targets.length || targets.length > model.maxRawTargets || duration < 1 || duration > model.maxRaw) {
        throw new Error("原始样本查询超出范围");
      }
      const expected = targets.reduce((sum, target) => sum + duration / target.interval, 0);
      if (expected > 24000) throw new Error("原始样本过多，请缩短查询窗口");
      const match = targetMatchers(targets);
      const expr = `{__name__=~"up|probe_success|probe_icmp_duration_seconds",${match},phase=~"rtt|"}[${duration}s]`;
      const result = await query(expr, { time: end }, signal, queryOptions);
      return model.rawRows(result, targets, start, end);
    }

    async function overview(targets, start, end, signal, domains = false) {
      const span = end - start;
      if (!targets.length || targets.length > model.maxCompared || span <= model.maxRaw || span > model.maxHistory) {
        throw new Error("历史概览查询超出范围");
      }
      const step = Math.ceil(span / 240);
      // Anchor at the requested end. The first, potentially partial bucket is
      // read separately so its denominator and data never extend before start.
      const first = end - Math.floor(span / step) * step;
      const match = domains ? dnsMatcher(targets) : targetMatchers(targets);
      const range = { start: first + step, end, step };
      const [counts, current, partial] = await Promise.all([
        query(lossFields(match, step).join(" or "), range, signal),
        query(currentFields(match).join(" or "), range, signal),
        first > start ? query(lossFields(match, first - start).join(" or "), { time: first }, signal) : []
      ]);
      const results = [...counts, ...current, ...partial];
      if (domains && results.reduce((total, row) => total + (row.values?.length ?? 1), 0) > 180000) {
        throw new Error("域名聚合样本过多，请缩短查询窗口");
      }
      const ids = new Set(targets.map((target) => target.metric.custom_id));
      const physical = domains ? new Map(results.filter(({ metric }) =>
        model.customMetric(metric) && ids.has(metric.custom_id)).map(({ metric }) => {
          const target = model.targetFromMetric(metric, settings);
          return [target.key, target];
        })) : new Map(targets.map((target) => [target.key, target]));
      const records = new Map([...physical.keys()].map((key) => [key, new Map()]));
      for (const { metric, values, value } of results) {
        const record = records.get(model.identity(metric));
        if (!record) continue;
        for (const [t, rawValue] of values || [value]) {
          if (!record.has(t)) record.set(t, new Map());
          const fields = record.get(t);
          const name = metric.ping_field;
          fields.set(name, fields.has(name) ? null : model.number(rawValue));
        }
      }
      return new Map([...physical.values()].map((target) => {
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
        return [target.key, { buckets, points, step, ...(domains ? { target } : {}) }];
      }));
    }

    return { summary, custom, raw, overview, dnsRaw,
      dnsOverview: (targets, start, end, signal) => overview(targets, start, end, signal, true) };
  }

  const queryError = (error) => error?.name === "TimeoutError" ? "查询超时" : "查询失败";
  if (common) module.exports = { createPingData, queryError };
  else window.BSPingData = { createPingData, queryError };
})();
