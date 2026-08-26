const assert = require('assert');
const pingChart = require('../bigscreen/charts/ping-chart.js');

assert.deepStrictEqual(
  Object.keys(pingChart),
  ['createPingChartRenderer'],
  'the Ping chart module exposes only its dependency-injected renderer factory'
);

const calls = [];
const formatPingText = (value) => `${value * 1000}ms`;
const series = [{
  name: 'switch-a',
  values: [{ t: 100, v: 0.002 }, { t: 102, v: 0.003 }]
}];
const renderPingChart = pingChart.createPingChartRenderer({
  renderLineChart: (...args) => calls.push(args),
  estimateStepSeconds: (input) => {
    assert.strictEqual(input, series, 'gap estimation receives the presentation series unchanged');
    return 2;
  },
  formatPingText
});

renderPingChart({ containerId: 'pingTrendChart', series, tournamentMode: false });
assert.strictEqual(calls.length, 1);
assert.strictEqual(calls[0][0], 'pingTrendChart');
assert.strictEqual(calls[0][1], series);
assert.deepStrictEqual(calls[0][2], {
  axisFormatter: formatPingText,
  valueFormatter: formatPingText,
  minMax: 0.005,
  maxRoundStep: 0.01,
  breakGapSeconds: 6,
  currentStatusLegend: true,
  smooth: false,
  sortLegendByMax: true
}, 'the extracted facade preserves every production Ping chart option');

renderPingChart({ containerId: 'pingTrendChart', series, tournamentMode: true });
assert.deepStrictEqual(calls[1][2], {
  axisFormatter: formatPingText,
  valueFormatter: formatPingText,
  minMax: 0.005,
  maxRoundStep: 0.01,
  breakGapSeconds: 6,
  currentStatusLegend: true,
  smooth: false,
  sortLegendByMax: true,
  legend: 'bottom',
  legendNamesOnly: true,
  calcs: []
}, 'tournament mode retains its compact names-only Ping legend');

const renderShortCadence = pingChart.createPingChartRenderer({
  renderLineChart: (...args) => calls.push(args),
  estimateStepSeconds: () => 1,
  formatPingText
});
renderShortCadence({ containerId: 'pingTrendChart', series, tournamentMode: false });
assert.strictEqual(calls[2][2].breakGapSeconds, 5, 'the existing five-second minimum gap is unchanged');

console.log('bigscreen Ping chart tests passed');
