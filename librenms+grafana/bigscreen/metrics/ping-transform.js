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
    baselineWindowSeconds: 60,
    minimumBaselinePoints: 6,
    minimumThreshold: 0.008,
    medianMultiplier: 3,
    madScale: 1.4826,
    madMultiplier: 6,
    persistentRunSeconds: 4,
    fallbackNominalStepSeconds: 2,
    maximumCandidateGapSteps: 1.5,
    cadenceWindowPoints: 8,
    minimumCadenceDeltas: 2,
    smoothingWindowPoints: 3,
    emaAlpha: 0.5
  });

  const MANAGEMENT_RTT_DISPLAY_POLICY = Object.freeze({
    windowSeconds: 30,
    minimumWindowPoints: 3,
    quantile: 0.2,
    emaAlpha: 0.5
  });

  // These jobs probe a switch or firewall device-local/control-plane address.
  // Preserve their RTT semantics while estimating the lower network-delay
  // component underneath positive management scheduling delay.
  const MANAGEMENT_RTT_PING_JOBS = new Set([
    "infra-core-ping",
    "infra-dist-ping",
    "infra-fw-ping"
  ]);
  const MANAGEMENT_RTT_PRESENTATION_MODE = "management-rtt";
  const LATENCY_PRESENTATION_MODE = "latency";

  function median(numbers) {
    if (!numbers.length) return null;
    const sorted = [...numbers].sort((left, right) => left - right);
    const middle = Math.floor(sorted.length / 2);
    return sorted.length % 2
      ? sorted[middle]
      : (sorted[middle - 1] + sorted[middle]) / 2;
  }

  function nearestRankQuantile(numbers, quantile) {
    if (!numbers.length) return null;
    const sorted = [...numbers].sort((left, right) => left - right);
    const rank = Math.max(1, Math.ceil(Math.min(1, Math.max(0, quantile)) * sorted.length));
    return sorted[rank - 1];
  }

  function adaptiveThreshold(baseline) {
    if (baseline.length < SUCCESS_AWARE_DISPLAY_POLICY.minimumBaselinePoints) return null;
    const rawValues = baseline.map((point) => point.v);
    const baselineMedian = median(rawValues);
    const mad = median(rawValues.map((value) => Math.abs(value - baselineMedian)));
    return Math.max(
      SUCCESS_AWARE_DISPLAY_POLICY.minimumThreshold,
      baselineMedian * SUCCESS_AWARE_DISPLAY_POLICY.medianMultiplier,
      baselineMedian
        + SUCCESS_AWARE_DISPLAY_POLICY.madMultiplier
        * SUCCESS_AWARE_DISPLAY_POLICY.madScale
        * mad
    );
  }

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
          jobs: new Set(),
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
      const job = String((((series || {}).metric || {}).job) || "").trim();
      if (job) group.jobs.add(job);
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

  function presentationModeForGroup(group) {
    if (!group.jobs || !group.jobs.size) return LATENCY_PRESENTATION_MODE;
    return Array.from(group.jobs).every((job) => MANAGEMENT_RTT_PING_JOBS.has(job))
      ? MANAGEMENT_RTT_PRESENTATION_MODE
      : LATENCY_PRESENTATION_MODE;
  }

  function buildSuccessAwareSeries(group) {
    const latencyByTimestamp = pointsByTimestamp(group.latencyValues);
    const successByTimestamp = pointsByTimestamp(group.successValues);
    const timestamps = Array.from(new Set([
      ...latencyByTimestamp.keys(),
      ...successByTimestamp.keys()
    ])).sort((left, right) => left - right);
    const values = [];
    const stableRawBaseline = [];
    const cadenceTimestamps = [];
    const smoothableValues = [];
    const managementRawWindow = [];
    let abnormalRun = null;
    let previousSmooth = null;
    let previousManagementEstimate = null;
    let openGapStatus = null;
    let currentStatus = "unknown";
    const presentationMode = presentationModeForGroup(group);

    function resetSmoothing() {
      smoothableValues.length = 0;
      previousSmooth = null;
    }

    function resetPresentationState() {
      stableRawBaseline.length = 0;
      cadenceTimestamps.length = 0;
      managementRawWindow.length = 0;
      previousManagementEstimate = null;
      abnormalRun = null;
      resetSmoothing();
    }

    function appendGap(timestamp, status) {
      resetPresentationState();
      if (openGapStatus !== status) {
        values.push({ t: timestamp, v: null, status });
      }
      openGapStatus = status;
    }

    function expireBaseline(timestamp) {
      const oldestAllowed = timestamp - SUCCESS_AWARE_DISPLAY_POLICY.baselineWindowSeconds;
      while (stableRawBaseline.length && stableRawBaseline[0].t < oldestAllowed) {
        stableRawBaseline.shift();
      }
    }

    function inferNominalStep() {
      const deltas = [];
      for (let index = 1; index < cadenceTimestamps.length; index += 1) {
        const delta = cadenceTimestamps[index] - cadenceTimestamps[index - 1];
        if (Number.isFinite(delta) && delta > 0) deltas.push(delta);
      }
      if (deltas.length < SUCCESS_AWARE_DISPLAY_POLICY.minimumCadenceDeltas) {
        return SUCCESS_AWARE_DISPLAY_POLICY.fallbackNominalStepSeconds;
      }
      return median(deltas);
    }

    function rememberSuccessfulTimestamp(timestamp) {
      cadenceTimestamps.push(timestamp);
      while (cadenceTimestamps.length > SUCCESS_AWARE_DISPLAY_POLICY.cadenceWindowPoints) {
        cadenceTimestamps.shift();
      }
    }

    function rememberStableRaw(timestamp, value) {
      stableRawBaseline.push({ t: timestamp, v: value });
    }

    function recentStableRawMean() {
      const recent = stableRawBaseline.slice(-2).map((point) => point.v);
      if (!recent.length) return null;
      return recent.reduce((sum, value) => sum + value, 0) / recent.length;
    }

    function smoothPresentation(value) {
      smoothableValues.push(value);
      while (smoothableValues.length > SUCCESS_AWARE_DISPLAY_POLICY.smoothingWindowPoints) {
        smoothableValues.shift();
      }
      const currentMedian = median(smoothableValues);
      const smoothed = Number.isFinite(previousSmooth)
        ? SUCCESS_AWARE_DISPLAY_POLICY.emaAlpha * currentMedian
          + (1 - SUCCESS_AWARE_DISPLAY_POLICY.emaAlpha) * previousSmooth
        : currentMedian;
      previousSmooth = smoothed;
      return smoothed;
    }

    function managementRttPresentation(timestamp, value) {
      const oldestAllowed = timestamp - MANAGEMENT_RTT_DISPLAY_POLICY.windowSeconds;
      while (managementRawWindow.length && managementRawWindow[0].t < oldestAllowed) {
        managementRawWindow.shift();
      }
      if (!managementRawWindow.length) previousManagementEstimate = null;
      managementRawWindow.push({ t: timestamp, v: value });
      if (managementRawWindow.length < MANAGEMENT_RTT_DISPLAY_POLICY.minimumWindowPoints) {
        return null;
      }
      const lowQuantile = nearestRankQuantile(
        managementRawWindow.map((point) => point.v),
        MANAGEMENT_RTT_DISPLAY_POLICY.quantile
      );
      const estimate = Number.isFinite(previousManagementEstimate)
        ? MANAGEMENT_RTT_DISPLAY_POLICY.emaAlpha * lowQuantile
          + (1 - MANAGEMENT_RTT_DISPLAY_POLICY.emaAlpha) * previousManagementEstimate
        : lowQuantile;
      previousManagementEstimate = estimate;
      return estimate;
    }

    function startsOrContinuesCandidateRun(timestamp, threshold, nominalStep) {
      const maximumGap = nominalStep * SUCCESS_AWARE_DISPLAY_POLICY.maximumCandidateGapSteps;
      if (
        abnormalRun
        && timestamp - abnormalRun.lastT <= maximumGap
      ) {
        abnormalRun.lastT = timestamp;
        abnormalRun.threshold = threshold;
        return abnormalRun;
      }
      abnormalRun = {
        startT: timestamp,
        lastT: timestamp,
        threshold,
        confirmed: false
      };
      return abnormalRun;
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
        currentStatus = "unknown";
        appendGap(timestamp, "unknown");
        return;
      }

      openGapStatus = null;
      const rawValue = rawPoint.v;
      if (presentationMode === MANAGEMENT_RTT_PRESENTATION_MODE) {
        const presentationValue = managementRttPresentation(timestamp, rawValue);
        if (!Number.isFinite(presentationValue)) {
          values.push({ ...rawPoint, t: timestamp, v: null, status: "warming" });
          return;
        }
        values.push(successfulLatencyPoint(
          rawPoint,
          timestamp,
          presentationValue
        ));
        return;
      }

      expireBaseline(timestamp);
      const nominalStep = inferNominalStep();
      const currentThreshold = adaptiveThreshold(stableRawBaseline);
      const runThreshold = abnormalRun && abnormalRun.confirmed && Number.isFinite(abnormalRun.threshold)
        ? abnormalRun.threshold
        : null;
      const threshold = Number.isFinite(currentThreshold) ? currentThreshold : runThreshold;
      let candidate = Number.isFinite(threshold) && rawValue > threshold;

      if (candidate && abnormalRun) {
        const maximumGap = nominalStep * SUCCESS_AWARE_DISPLAY_POLICY.maximumCandidateGapSteps;
        if (timestamp - abnormalRun.lastT > maximumGap) {
          abnormalRun = null;
          candidate = Number.isFinite(currentThreshold) && rawValue > currentThreshold;
        }
      }

      if (candidate) {
        const run = startsOrContinuesCandidateRun(timestamp, threshold, nominalStep);
        rememberSuccessfulTimestamp(timestamp);
        if (timestamp - run.startT >= SUCCESS_AWARE_DISPLAY_POLICY.persistentRunSeconds) {
          if (!run.confirmed) {
            run.confirmed = true;
            resetSmoothing();
          }
          values.push(successfulLatencyPoint(rawPoint, timestamp, rawValue));
          return;
        }

        const replacement = recentStableRawMean();
        const presentationValue = Number.isFinite(replacement) ? replacement : rawValue;
        values.push(successfulLatencyPoint(
          rawPoint,
          timestamp,
          smoothPresentation(presentationValue)
        ));
        return;
      }

      const recoveringFromPersistent = abnormalRun && abnormalRun.confirmed;
      abnormalRun = null;
      if (recoveringFromPersistent) resetSmoothing();
      rememberStableRaw(timestamp, rawValue);
      rememberSuccessfulTimestamp(timestamp);
      values.push(successfulLatencyPoint(rawPoint, timestamp, smoothPresentation(rawValue)));
    });

    const source = group.source || {};
    const result = {
      ...source,
      presentationMode,
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
   * input is the production success-aware contract and intentionally returns
   * display data only.
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
