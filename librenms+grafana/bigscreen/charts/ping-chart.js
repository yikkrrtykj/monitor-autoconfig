;(function () {
  'use strict';

  function createPingChartRenderer(dependencies) {
    const { renderLineChart, estimateStepSeconds, formatPingText } = dependencies;

    return function renderPingChart(input) {
      const { containerId, series, tournamentMode } = input;
      // Prometheus does not return placeholder samples while a target/series
      // is temporarily absent. Never join the two real samples surrounding
      // that hole: doing so draws a convincing but entirely synthetic ramp.
      const pingGap = Math.max(5, estimateStepSeconds(series) * 3);
      const tournamentPingLegend = tournamentMode
        ? { legend: "bottom", legendNamesOnly: true, calcs: [] }
        : {};
      renderLineChart(containerId, series, {
        axisFormatter: formatPingText,
        valueFormatter: formatPingText,
        // Keep network infrastructure devices in the combined Ping trend.
        // Servers remain in their dedicated gauges. A 5 ms floor avoids
        // exaggerating sub-millisecond jitter.
        minMax: 0.005,
        // Ping is easier to read in decimal milliseconds than the generic
        // 1/2/2.5/5 chart scale: e.g. a 27 ms peak gets a 30 ms ceiling.
        maxRoundStep: 0.01,
        breakGapSeconds: pingGap,
        currentStatusLegend: true,
        // The adapter already applies trailing causal smoothing. Keep the
        // Ping SVG linear so a future point cannot reshape an earlier segment.
        smooth: false,
        // When many switches are present, put the largest observed latency
        // first so the line responsible for the chart scale is immediately
        // identifiable instead of falling below the clipped viewport.
        sortLegendByMax: true,
        ...tournamentPingLegend
      });
    };
  }

  const ns = { createPingChartRenderer };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = ns;
  } else {
    window.BSPingChart = ns;
  }
}());
