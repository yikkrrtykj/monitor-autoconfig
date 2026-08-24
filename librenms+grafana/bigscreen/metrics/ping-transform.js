;(function () {
  'use strict';

  // Presentation policy currently used by the infrastructure Ping trend.
  // Keep these effective values explicit while the old utils.js path remains
  // active so the later app wiring step cannot change display behavior.
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

  function buildInfrastructurePingPresentation(seriesList) {
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

  const ns = { buildInfrastructurePingPresentation };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = ns;
  } else {
    window.BSPingTransform = ns;
  }
}());
