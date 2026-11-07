;(function () {
  'use strict';

  const common = typeof module !== "undefined" && module.exports;
  const ping = common ? require("../ping/model.js") : window.BSPingModel;
  const { parseIspBandwidthConfig } = common ? require("../utils.js") : window.BSUtils;
  const gatewayJobs = ["infra-core-ping", "infra-fw-ping", "infra-isp-ping", "infra-dist-ping"];
  const maxGateways = 12;
  const maxWans = 16;
  const wanFreshness = 35;
  const inventoryFreshness = 300;

  function countStates(targets) {
    const counts = { UP: 0, DOWN: 0, DEGRADED: 0, UNKNOWN: 0 };
    for (const { status } of targets) counts[status] += 1;
    return counts;
  }

  function gatewayTargets(targets) {
    const ordered = gatewayJobs.map((job) => targets.filter((target) => target.job === job));
    // Give the major groups a place before filling the table with more devices.
    const first = ordered.flatMap((group) => group.slice(0, 1));
    return first.concat(ordered.flatMap((group) => group.slice(1))).slice(0, maxGateways);
  }

  function pingHref(target) {
    return `/ping?target=${encodeURIComponent(target.key)}`;
  }

  function wanKey(instance, ifIndex) {
    return JSON.stringify([instance, ifIndex]);
  }

  function inventoryRows(payload) {
    if (!Array.isArray(payload) || payload.length > maxWans) throw new Error("WAN 清单超出范围");
    const identities = new Map();
    const names = new Map();
    const rows = payload.map(({ labels, targets }) => {
      if (!labels || !Array.isArray(targets) || targets.length > 1 ||
          targets.some((target) => typeof target !== "string")) throw new Error("WAN 清单格式无效");
      const name = String(labels.display_name || labels.metric_name || "").trim();
      const instance = String(labels.metric_target || "").trim();
      const index = String(labels.metric_ifindex || "").trim();
      if (!name || name.length > 160 || instance.length > 128) throw new Error("WAN 清单标签无效");
      const key = instance && /^[1-9]\d{0,9}$/.test(index) ? wanKey(instance, index) : null;
      const nameKey = name.toLocaleLowerCase();
      if (key) identities.set(key, (identities.get(key) || 0) + 1);
      names.set(nameKey, (names.get(nameKey) || 0) + 1);
      return { key, name, nameKey, instance, index, gateway: targets[0] || "",
        metadataConflict: String(labels.metadata_conflict).toLowerCase() === "true" };
    });
    return rows.map((row) => ({
      ...row,
      identityConflict: row.key !== null && identities.get(row.key) !== 1,
      ambiguousName: names.get(row.nameKey) !== 1
    }));
  }

  function capacityFor(row, raw) {
    // Global values include the legacy 1000 Mbps fallback. Only uniquely
    // named, explicit circuit capacities may become a utilization denominator.
    if (row.ambiguousName || row.metadataConflict || row.identityConflict || !row.key) return null;
    const entries = String(raw ?? "").split(",").filter((entry) => {
      const split = entry.lastIndexOf(":");
      return split > 0 && entry.slice(0, split).trim().toLocaleLowerCase() === row.nameKey;
    });
    if (entries.length !== 1) return null;
    const entry = Object.values(parseIspBandwidthConfig(entries[0]).perIsp)[0];
    if (!entry) return null;
    const { down, up } = entry;
    return down > 0 && up > 0 ? { down, up } : null;
  }

  function timelineFields(result, keyFor) {
    const byKey = new Map();
    for (const { metric, values } of result) {
      const key = keyFor(metric);
      if (!byKey.has(key)) byKey.set(key, new Map());
      const timeline = byKey.get(key);
      for (const [at, raw] of values) {
        if (!timeline.has(at)) timeline.set(at, new Map());
        const fields = timeline.get(at);
        const field = metric.overview_field;
        fields.set(field, fields.has(field) ? null : ping.number(raw));
      }
    }
    return byKey;
  }

  function directionAt(fields, source, direction, at) {
    const sampleAt = fields?.get(`${direction}_at`);
    const sourceAt = source?.get("up_at");
    if (source?.get("up") !== 1 || sampleAt == null || sampleAt !== sourceAt ||
        sampleAt > at || at - sampleAt > wanFreshness) {
      return { bps: null, sampleAt: null, reason: "抓取失败、缺样或数据过期" };
    }
    const bps = fields.get(direction);
    const speed = fields.get("speed");
    if (bps == null || bps < 0 || fields.get(`${direction}_resets`) !== 0 ||
        (speed > 0 && bps > speed * 1.05e6)) {
      return { bps: null, sampleAt: null, reason: "速率样本不足、重复或计数异常" };
    }
    return { bps, sampleAt, reason: "" };
  }

  function wanCards(rows, result, end, step, bandwidthConfig) {
    const sources = timelineFields(result.filter(({ metric }) => !metric.ifIndex), (metric) => metric.instance);
    const interfaces = timelineFields(result.filter(({ metric }) => metric.ifIndex),
      (metric) => wanKey(metric.instance, metric.ifIndex));
    return rows.map((row) => {
      const unavailable = row.identityConflict ? "接口身份重复" : row.metadataConflict ? "接口元数据冲突" : !row.key ? "接口身份未核实" : "";
      const capacity = capacityFor(row, bandwidthConfig);
      const device = sources.get(row.instance);
      const port = interfaces.get(row.key);
      const directions = Object.fromEntries([["in", "down"], ["out", "up"]].map(([direction, capKey]) => {
        const read = (at) => unavailable ? { bps: null, sampleAt: null, reason: unavailable }
          : directionAt(port?.get(at), device?.get(at), direction, at);
        const points = [];
        for (let at = end - 900; at <= end; at += step) points.push({ t: at, v: read(at).bps });
        return [direction, { ...read(end), points, capacity: capacity?.[capKey] ?? null }];
      }));
      return { ...row, ...directions };
    });
  }

  function freshTarget(target, ready, at) {
    if (ready && (target.status === "UNKNOWN" || (target.sourceAt != null &&
        at - target.sourceAt <= Math.max(10, target.interval * 3)))) return target;
    return { ...target, status: "UNKNOWN", rtt: null, loss: { ...target.loss, loss: null },
      reason: ready ? "数据过期" : "监控查询失败" };
  }

  function freshWan(card, ready, at) {
    const direction = (data) => ready && data.sampleAt != null && at - data.sampleAt <= wanFreshness ? data : {
      ...data, bps: null, reason: !ready ? "WAN 数据源不可用" : data.reason || "数据过期"
    };
    return { ...card, in: direction(card.in), out: direction(card.out) };
  }

  function wanProbeCoverage(wan, gateways, wans) {
    const matches = gateways.filter((target) => {
      const { metric } = target;
      const declaredIdentity = Boolean(metric.metric_target || metric.metric_ifindex);
      const sameInterface = Boolean(wan.key) &&
        metric.metric_target === wan.instance && metric.metric_ifindex === wan.index;
      // A published gateway can bind a manual ISP_PING target by address.
      // Without one, only an explicit canonical interface binding is evidence.
      return (wan.gateway ? target.ip === wan.gateway : sameInterface) &&
        (!declaredIdentity || sameInterface);
    });
    if (matches.length === 1) {
      const [target] = matches;
      if (!target.metric.metric_target && !target.metric.metric_ifindex) {
        // An address-only probe cannot prove which WAN interface owns its route.
        const owners = new Set(wans.filter((candidate) => candidate.gateway === target.ip)
          .map(({ key }) => key).filter(Boolean));
        if (owners.size > 1) return { state: "ambiguous" };
      }
      return { state: "matched", target };
    }
    if (matches.length > 1) return { state: "ambiguous" };
    return { state: wan.gateway ? "missing" : "unconfigured" };
  }

  function overviewState({ pingState, wanState, statsState }, at) {
    const targets = (pingState.data?.targets || []).map((target) => freshTarget(target, pingState.state === "ready", at));
    const infra = targets.filter((target) => target.category === "lan" || target.category === "server");
    const gateways = targets.filter((target) => target.category === "isp");
    const wans = (wanState.data || []).map((wan) => ({
      ...freshWan(wan, wanState.state === "ready", at), probeCoverage: wanProbeCoverage(wan, gateways, wanState.data)
    }));
    const critical = infra.concat(gateways);
    const infraCounts = countStates(infra);
    const ispCounts = countStates(gateways);
    const total = countStates(targets);
    const agedOut = pingState.data?.agedOut || 0;
    const uncovered = wans.filter(({ probeCoverage }) => probeCoverage.state === "missing").length;
    const unconfigured = wans.filter(({ probeCoverage }) => probeCoverage.state === "unconfigured").length;
    const ambiguous = wans.filter(({ probeCoverage }) => probeCoverage.state === "ambiguous").length;
    const incomplete = critical.some(({ status, rtt, loss }) => status === "UP" && (rtt == null || loss.loss == null));
    const missingGroups = !infra.some(({ job }) => job === "infra-core-ping") || !gateways.length;
    const wanUnavailable = wanState.state !== "ready" || !wans.length ||
      wans.some((wan) => wan.in.bps === null || wan.out.bps === null);
    const unavailable = pingState.state !== "ready" || !critical.some(({ status }) => status !== "UNKNOWN");
    const statsUnavailable = statsState.state === "error";
    const partial = total.UNKNOWN > 0 || agedOut > 0 || uncovered > 0 || unconfigured > 0 || ambiguous > 0 || incomplete ||
      missingGroups || wanUnavailable || statsUnavailable;
    const availability = unavailable ? "unavailable" : partial ? "partial" : "ready";
    const health = total.DOWN || total.DEGRADED ? "fault" : availability === "ready" ? "healthy" : availability;
    const faults = critical.filter(({ status }) => status !== "UP");
    const rank = { DOWN: 0, DEGRADED: 1, UNKNOWN: 2 };
    faults.sort((a, b) => rank[a.status] - rank[b.status]);
    const clients = ["player", "ap"].map((category) => ({
      category, ...countStates(targets.filter((target) => target.category === category))
    }));
    const gatewayRows = gatewayTargets(targets).map((target) => ({
      ...target, stddev: target.status !== "UNKNOWN" && statsState.state === "ready" &&
        at - statsState.at <= 45 ? statsState.data.get(target.key) ?? null : null
    }));
    return { health, availability, infraCounts, ispCounts, total, wans, faults, gatewayRows, clients,
      agedOut, uncovered, unconfigured, ambiguous, missingGroups, incomplete, wanUnavailable, statsUnavailable,
      moreGateways: targets.filter(({ job }) => gatewayJobs.includes(job)).length - gatewayRows.length };
  }

  const ns = { maxWans, inventoryFreshness, gatewayTargets, pingHref, inventoryRows, wanCards, overviewState };
  if (common) module.exports = ns;
  else window.BSOverviewModel = ns;
})();
