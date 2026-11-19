;(function () {
  'use strict';

  const common = typeof module !== "undefined" && module.exports;
  const model = common ? require("./model.js") : window.BSPingModel;
  const dns = common ? require("./custom-dns.js") : window.BSPingCustomDNS;

  function validateSegments(rows) {
    for (const row of rows) {
      if (!dns.ipv4(row.ip) || typeof row.address !== "string" || row.address.length > 253 ||
          !dns.generation(row.generation) || !dns.stamp(row.start) ||
          row.end !== null && (!dns.stamp(row.end) || row.end < row.start) ||
          row.sampleNotBefore != null && (!dns.stamp(row.sampleNotBefore) || row.sampleNotBefore < row.start)) {
        throw new Error("解析历史格式无效");
      }
    }
    return [...rows].sort((a, b) => a.start - b.start || a.generation - b.generation);
  }

  function segmentAt(segments, time) {
    let lo = 0;
    let hi = segments.length;
    while (lo < hi) {
      const mid = (lo + hi) >>> 1;
      if (segments[mid].start <= time) lo = mid + 1;
      else hi = mid;
    }
    const row = segments[lo - 1];
    return row && (row.end === null || time < row.end) ? row : null;
  }

  function annotate(points, segments) {
    return points.map((point) => {
      const candidate = segmentAt(segments, point.t);
      const segment = candidate?.ip === point.ip ? candidate : null;
      return { ...point, segment, verified: segment?.sampleNotBefore != null && point.t > segment.sampleNotBefore };
    });
  }

  function ranges(target, start, end) {
    return (target.segments || []).filter((segment) => segment.start < end && (segment.end ?? end) > start);
  }

  function rawSeries(target, rows, start, end) {
    const input = rows.filter((point) => point.verified);
    const width = Math.max(10, Math.ceil((end - start) / 90));
    const bucketCount = Math.ceil((end - start) / width);
    // The plotted points use explicit breaks at a generation boundary. A
    // bucket spanning a boundary is blank instead of mixing two addresses.
    const points = [];
    const slices = new Map();
    let previous;
    for (const point of input) {
      if (previous && previous.segment.generation !== point.segment.generation) {
        points.push({ t: (previous.t + point.t) / 2, rtt: null });
      }
      points.push(point);
      previous = point;
      const index = Math.ceil((point.t - start) / width) - 1;
      if (!slices.has(index)) slices.set(index, []);
      slices.get(index).push(point);
    }
    const buckets = Array.from({ length: bucketCount }, (_, i) => {
      const from = start + i * width;
      const to = Math.min(end, from + width);
      const segment = segmentAt(target.segments, to);
      const verified = segment?.sampleNotBefore != null && from >= segment.sampleNotBefore;
      const samples = verified ? (slices.get(i) || []).filter((point) => point.segment.generation === segment.generation) : [];
      return { from, to, segment: verified ? segment : null, ...model.rawStats(samples, to - from, target.interval) };
    });
    return { target, points, samples: input, buckets, gap: target.interval * 1.5 };
  }

  function aggregate(target, physical, start, end) {
    const owner = target.metric.custom_id;
    const byIp = new Map([...physical.values()].filter(({ target }) => target.metric.custom_id === owner)
      .map((series) => [series.target.ip, series]));
    const step = Math.ceil((end - start) / 240);
    const first = end - Math.floor((end - start) / step) * step;
    const buckets = [];
    const points = [];
    let prev;
    for (let to = first > start ? first : first + step; to <= end; to += step) {
      const from = Math.max(start, to - step);
      const segment = segmentAt(target.segments, to);
      const ip = segment?.ip;
      const index = buckets.length;
      const data = segment?.sampleNotBefore != null && from >= segment.sampleNotBefore ? byIp.get(ip) : null;
      const bucket = data?.buckets[index];
      const point = data?.points[index];
      if (bucket && prev && prev.generation !== segment.generation) {
        points.push({ t: from, rtt: null });
      }
      buckets.push(bucket ? { ...bucket, segment } :
        { from, to, segment: null, ...model.lossStats(null, null, to - from, target.interval) });
      points.push(point ? { ...point, ip, segment } : { t: to, rtt: null });
      prev = bucket ? segment : null;
    }
    return { buckets, points, step };
  }

  function createHistoryData(client, data) {
    async function hydrate(targets, signal) {
      const domains = targets.filter((target) => target.dns);
      if (!domains.length) return targets;
      const [catalog, all] = await Promise.all([
        client.list(signal).catch(() => null),
        Promise.all(domains.map(async (target) => {
          const list = await client.resolutions(target.metric.custom_id, signal);
          return [target.key, validateSegments(list)];
        }))
      ]);
      const byKey = new Map(all);
      const byId = new Map((catalog?.targets || []).filter((row) => row.kind === "dns").map((row) => [row.id, row]));
      return targets.map((target) => {
        if (!target.dns) return target;
        const segments = byKey.get(target.key);
        const row = byId.get(target.metric.custom_id);
        const address = row?.address || target.address || segments.at(-1)?.address;
        return { ...target, ...(row ? dns.target(row) : {}), address, name: address || target.name, segments };
      });
    }

    async function raw(targets, start, end, signal) {
      const fixed = targets.filter((target) => !target.dns);
      const domains = targets.filter((target) => target.dns);
      const [plain, dynamic] = await Promise.all([
        fixed.length ? data.raw(fixed, start, end, signal) : new Map(),
        domains.length ? data.dnsRaw(domains, start, end, signal) : new Map()
      ]);
      for (const target of domains) plain.set(target.key, annotate(dynamic.get(target.key), target.segments));
      return plain;
    }

    async function overview(targets, start, end, signal) {
      const fixed = targets.filter((target) => !target.dns);
      const domains = targets.filter((target) => target.dns);
      const [plain, dynamic] = await Promise.all([
        fixed.length ? data.overview(fixed, start, end, signal) : new Map(),
        domains.length ? data.dnsOverview(domains, start, end, signal) : new Map()
      ]);
      for (const target of domains) plain.set(target.key, aggregate(target, dynamic, start, end));
      return plain;
    }

    return { hydrate, raw, overview };
  }

  function splitStatistics(series, start, end, aggregated) {
    if (!series.target.dns) return [{ ...series, span: end - start }];
    const byGeneration = new Map();
    for (const sample of aggregated ? (series.buckets || []) : series.points) {
      const segment = sample.segment;
      if (!segment || (!aggregated && !sample.verified)) continue;
      if (!byGeneration.has(segment.generation)) byGeneration.set(segment.generation, { segment, samples: [] });
      byGeneration.get(segment.generation).samples.push(sample);
    }
    return [...byGeneration.values()].map(({ segment, samples }) => ({
      target: { ...series.target, ip: segment.ip, name: segment.address, generation: segment.generation },
      span: Math.max(0.001, Math.min(end, segment.end ?? end) - Math.max(start, segment.sampleNotBefore)),
      [aggregated ? "buckets" : "points"]: samples
    }));
  }

  const api = { validateSegments, segmentAt, annotate, ranges, rawSeries, aggregate, createHistoryData, splitStatistics };
  if (common) module.exports = api;
  else window.BSPingCustomHistory = api;
})();
