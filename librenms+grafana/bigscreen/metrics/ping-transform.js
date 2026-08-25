;(function () {
  'use strict';

  // Presentation policy used by the legacy array-input production path.
  // Keep these values explicit until production switches to the success-aware
  // object input and the compatibility path can be removed.
  const INFRASTRUCTURE_PING_DISPLAY_POLICY = Object.freeze({
    threshold: 0.02,
    minConsecutive: 2,
    maxGapSeconds: 3,
    replacementRadius: 5,
    replacementWindowSeconds: 15
  });

  function cloneLatencySeries(seriesList) {
    return (seriesList || []).map((series) => {
      const copy = {
        ...series,
        values: (series.values || []).map((point) => ({ ...point }))
      };
      if (series.metric && typeof series.metric === 'object') {
        copy.metric = { ...series.metric };
      }
      return copy;
    });
  }

  /**
   * Keep sustained latency incidents visible while removing isolated ICMP
   * response spikes from the infrastructure Ping trend. The input data is
   * left untouched; this function returns copied series/points.
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

  function buildLegacyInfrastructurePingPresentation(seriesList) {
    // Clone both branches independently. A consumer may inspect or mutate one
    // branch without changing the latency values, labels, or points in the other.
    const rawLatencySeries = cloneLatencySeries(seriesList);
    const displaySource = cloneLatencySeries(seriesList);
    const displayLatencySeries = suppressIsolatedLatencySpikes(
      displaySource,
      INFRASTRUCTURE_PING_DISPLAY_POLICY
    );
    return { rawLatencySeries, displayLatencySeries };
  }

  const SUCCESS_AWARE_DISPLAY_POLICY = Object.freeze({
    threshold: 0.02,
    persistentRunSeconds: 4
  });

  function seriesIdentity(series, index, sourceKind) {
    const metric = (series && series.metric) || {};
    const instance = String(metric.instance || "").trim();
    if (instance) return `instance:${instance}`;
    const name = String((series && series.name) || "").trim();
    if (name) return `name:${name}`;
    const targetIp = String(metric.target_ip || "").trim();
    if (targetIp) return `target:${targetIp}`;
    return `${sourceKind}:anonymous:${index}`;
  }

  function collectPresentationGroups(latencySeries, successSeries) {
    const groups = new Map();
    const ordered = [];

    function addSeries(series, index, sourceKind) {
      const key = seriesIdentity(series, index, sourceKind);
      let group = groups.get(key);
      if (!group) {
        group = {
          source: series,
          latencyValues: [],
          successValues: []
        };
        groups.set(key, group);
        ordered.push(group);
      } else if (sourceKind === "latency" && !group.hasLatencySource) {
        group.source = series;
      }
      if (sourceKind === "latency") {
        group.hasLatencySource = true;
        group.latencyValues.push(...((series && series.values) || []));
      } else {
        group.successValues.push(...((series && series.values) || []));
      }
    }

    (latencySeries || []).forEach((series, index) => addSeries(series, index, "latency"));
    (successSeries || []).forEach((series, index) => addSeries(series, index, "success"));
    return ordered;
  }

  function pointsByTimestamp(values) {
    const points = new Map();
    (values || []).forEach((point) => {
      if (!point || !Number.isFinite(point.t)) return;
      points.set(point.t, point);
    });
    return points;
  }

  function successState(point) {
    if (!point || !Number.isFinite(point.v)) return "unknown";
    if (point.v === 1) return "online";
    if (point.v === 0) return "offline";
    return "unknown";
  }

  function successfulLatencyPoint(source, timestamp, value) {
    const point = { ...(source || {}), t: timestamp, v: value };
    delete point.status;
    return point;
  }

  function buildSuccessAwareSeries(group) {
    const latencyByTimestamp = pointsByTimestamp(group.latencyValues);
    const successByTimestamp = pointsByTimestamp(group.successValues);
    const timestamps = Array.from(new Set([
      ...latencyByTimestamp.keys(),
      ...successByTimestamp.keys()
    ])).sort((left, right) => left - right);
    const values = [];
    const normalRawBaseline = [];
    let highRun = [];
    let highRunStart = null;
    let openGapStatus = null;
    let currentStatus = "unknown";

    function endHighRun() {
      highRun = [];
      highRunStart = null;
    }

    function appendGap(timestamp, status) {
      endHighRun();
      if (openGapStatus !== status) {
        values.push({ t: timestamp, v: null, status });
      }
      openGapStatus = status;
    }

    timestamps.forEach((timestamp) => {
      const state = successState(successByTimestamp.get(timestamp));
      currentStatus = state;

      if (state === "offline") {
        appendGap(timestamp, "failure");
        return;
      }
      if (state === "unknown") {
        appendGap(timestamp, "unknown");
        return;
      }

      const rawPoint = latencyByTimestamp.get(timestamp);
      if (!rawPoint || !Number.isFinite(rawPoint.v)) {
        appendGap(timestamp, "unknown");
        return;
      }

      openGapStatus = null;
      const rawValue = rawPoint.v;
      if (rawValue < SUCCESS_AWARE_DISPLAY_POLICY.threshold) {
        endHighRun();
        values.push(successfulLatencyPoint(rawPoint, timestamp, rawValue));
        normalRawBaseline.push(rawValue);
        if (normalRawBaseline.length > 2) normalRawBaseline.shift();
        return;
      }

      if (!highRun.length) highRunStart = timestamp;
      const replacement = normalRawBaseline.length
        ? normalRawBaseline.reduce((sum, value) => sum + value, 0) / normalRawBaseline.length
        : rawValue;
      const displayPoint = successfulLatencyPoint(rawPoint, timestamp, replacement);
      values.push(displayPoint);
      highRun.push({ point: displayPoint, rawValue });

      if (timestamp - highRunStart >= SUCCESS_AWARE_DISPLAY_POLICY.persistentRunSeconds) {
        highRun.forEach((entry) => {
          entry.point.v = entry.rawValue;
        });
      }
    });

    const source = group.source || {};
    const result = {
      ...source,
      currentStatus,
      values
    };
    if (source.metric && typeof source.metric === 'object') {
      result.metric = { ...source.metric };
    }
    return result;
  }

  function buildSuccessAwareInfrastructurePingPresentation(input) {
    const latencySeries = Array.isArray(input.latencySeries) ? input.latencySeries : [];
    const successSeries = Array.isArray(input.successSeries) ? input.successSeries : [];
    const groups = collectPresentationGroups(latencySeries, successSeries);
    return {
      displayLatencySeries: groups.map(buildSuccessAwareSeries)
    };
  }

  /**
   * Array input retains the deployed legacy presentation contract. Object
   * input is the success-aware v2 contract and intentionally returns display
   * data only; production will switch to it in a later wiring commit.
   */
  function buildInfrastructurePingPresentation(input) {
    if (Array.isArray(input) || input === undefined || input === null) {
      return buildLegacyInfrastructurePingPresentation(input);
    }
    return buildSuccessAwareInfrastructurePingPresentation(input);
  }

  const ns = { buildInfrastructurePingPresentation };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = ns;
  } else {
    window.BSPingTransform = ns;
  }
}());
