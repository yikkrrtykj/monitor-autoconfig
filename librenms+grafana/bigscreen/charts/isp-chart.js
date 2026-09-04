;(function () {
  'use strict';

  function createIspChartRenderer(dependencies) {
    const { renderLineChart, formatBits, ispChartMaxBps } = dependencies;

    return function renderIspChart(input) {
      const { containerId, result, compactTournamentChart } = input;
      renderLineChart(containerId, [result.download, result.upload], {
        axisFormatter: formatBits,
        valueFormatter: formatBits,
        minWidth: compactTournamentChart ? 120 : 320,
        axisPadLeft: compactTournamentChart ? 76 : 92,
        axisPadRight: compactTournamentChart ? 12 : 38,
        axisPadTop: compactTournamentChart ? 6 : 12,
        axisPadBottom: compactTournamentChart ? 20 : undefined,
        fill: true,
        legend: "bottom",
        maxY: ispChartMaxBps(result.name),
        minMax: 1,
        calcs: ["last", "max"]
      });
    };
  }

  const ns = { createIspChartRenderer };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = ns;
  } else {
    window.BSIspChart = ns;
  }
}());
