const assert = require('assert');
const fs = require('fs');
const path = require('path');
const evidenceChart = require('../bigscreen/charts/evidence-chart.js');

assert.deepStrictEqual(
  Object.keys(evidenceChart),
  ['createEvidenceChartRenderer'],
  'the Evidence chart module exposes only its dependency-injected renderer factory'
);

function average(values) {
  return values.reduce((total, value) => total + value, 0) / values.length;
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

const containers = new Map();
const document = {
  getElementById(id) {
    if (!containers.has(id)) containers.set(id, { innerHTML: '' });
    return containers.get(id);
  }
};
const calls = [];
const stepCalls = [];
const formatPingText = (value) => `${(value * 1000).toFixed(1)}ms`;
const latencySeries = [
  { name: 'latency-a', values: [{ t: 100, v: 0.002 }, { t: 102, v: 0.004 }] },
  { name: 'latency-b', values: [{ t: 100, v: 0.006 }] }
];
const successSeries = [
  { name: 'success-a', values: [{ t: 100, v: 1 }, { t: 104, v: 0 }] },
  { name: 'success-b', values: [{ t: 100, v: 1 }] }
];
const renderEvidenceCharts = evidenceChart.createEvidenceChartRenderer({
  document,
  renderLineChart: (...args) => calls.push(args),
  formatPingText,
  estimateStepSeconds: (series) => {
    stepCalls.push(series);
    if (series === latencySeries) return 2;
    if (series === successSeries) return 4;
    return 5;
  },
  average,
  escapeHtml
});

renderEvidenceCharts({
  summaryContainerId: 'evidenceSummary',
  latencyContainerId: 'evidenceLatencyChart',
  successContainerId: 'evidenceSuccessChart',
  context: { label: 'T1S1 <wired>' },
  latencySeries,
  successSeries
});

const summary = document.getElementById('evidenceSummary').innerHTML;
assert.ok(summary.includes('evidence-verdict bad'));
assert.ok(summary.includes('存在断线/探测失败'));
assert.ok(summary.includes('T1S1 &lt;wired&gt;'));
assert.ok(summary.includes('<strong>4.0ms</strong>'), 'average latency formatting is unchanged');
assert.ok(summary.includes('<strong>6.0ms</strong>'), 'maximum latency formatting is unchanged');
assert.ok(summary.includes('<strong>66.7%</strong>'), 'online rate is unchanged');
assert.ok(summary.includes('<strong>4s</strong>'), 'offline duration uses the success cadence');

assert.strictEqual(calls.length, 2);
assert.strictEqual(calls[0][0], 'evidenceLatencyChart');
assert.strictEqual(calls[0][1], latencySeries, 'latency series order and identity are unchanged');
assert.deepStrictEqual(calls[0][2], {
  axisFormatter: formatPingText,
  valueFormatter: formatPingText,
  minMax: 0.005,
  smooth: true,
  breakGapSeconds: 6,
  legend: 'bottom'
}, 'Evidence latency options exactly match the pre-extraction renderer call');

assert.strictEqual(calls[1][0], 'evidenceSuccessChart');
assert.strictEqual(calls[1][1].length, successSeries.length);
calls[1][1].forEach((series, index) => {
  assert.notStrictEqual(series, successSeries[index]);
  assert.strictEqual(series.values, successSeries[index].values);
  assert.strictEqual(series.color, '#73d17a');
  assert.strictEqual(successSeries[index].color, undefined, 'presentation decoration does not mutate input');
});
const successOptions = calls[1][2];
assert.deepStrictEqual({
  calcs: successOptions.calcs,
  minMax: successOptions.minMax,
  smooth: successOptions.smooth,
  step: successOptions.step,
  breakGapSeconds: successOptions.breakGapSeconds,
  fill: successOptions.fill,
  legend: successOptions.legend
}, {
  calcs: ['last', 'min'],
  minMax: 1,
  smooth: false,
  step: true,
  breakGapSeconds: 12,
  fill: true,
  legend: 'bottom'
});
assert.strictEqual(successOptions.axisFormatter(0), '离线');
assert.strictEqual(successOptions.axisFormatter(0.5), '');
assert.strictEqual(successOptions.axisFormatter(1), '在线');
assert.strictEqual(successOptions.valueFormatter(0.49), '离线');
assert.strictEqual(successOptions.valueFormatter(0.5), '在线');
assert.deepStrictEqual(stepCalls, [successSeries, latencySeries, successSeries], 'summary and gap calculations keep their original cadence calls');

const emptyLatency = [];
const emptySuccess = [];
renderEvidenceCharts({
  summaryContainerId: 'emptySummary',
  latencyContainerId: 'emptyLatency',
  successContainerId: 'emptySuccess',
  context: { label: 'empty' },
  latencySeries: emptyLatency,
  successSeries: emptySuccess
});
assert.ok(document.getElementById('emptySummary').innerHTML.includes('evidence-verdict unknown'));
assert.ok(document.getElementById('emptySummary').innerHTML.includes('没有查到数据'));
assert.strictEqual(calls[2][1], emptyLatency, 'empty latency delegates to line-chart no-data handling');
assert.deepStrictEqual(calls[3][1], [], 'empty success delegates to line-chart no-data handling');

const unknownLatency = [{ name: 'latency-null', values: [{ t: 200, v: null }] }];
const unknownSuccess = [{ name: 'success-null', values: [{ t: 200, v: null }] }];
renderEvidenceCharts({
  summaryContainerId: 'unknownSummary',
  latencyContainerId: 'unknownLatency',
  successContainerId: 'unknownSuccess',
  context: { label: 'unknown' },
  latencySeries: unknownLatency,
  successSeries: unknownSuccess
});
assert.ok(document.getElementById('unknownSummary').innerHTML.includes('没有查到数据'));
assert.strictEqual(calls[4][1], unknownLatency, 'null latency samples pass through unchanged');
assert.strictEqual(calls[5][1][0].values, unknownSuccess[0].values, 'null success samples pass through unchanged');

function verdictFor(id, latencyValues) {
  renderEvidenceCharts({
    summaryContainerId: `${id}Summary`,
    latencyContainerId: `${id}Latency`,
    successContainerId: `${id}Success`,
    context: { label: id },
    latencySeries: [{ name: id, values: latencyValues.map((v, index) => ({ t: 300 + index * 2, v })) }],
    successSeries: [{ name: id, values: [{ t: 300, v: 1 }] }]
  });
  return document.getElementById(`${id}Summary`).innerHTML;
}

assert.ok(verdictFor('good', [0.002, 0.004]).includes('未见明显网络异常'));
assert.ok(verdictFor('light-jitter', [0, 0.04]).includes('有轻微抖动'));
assert.ok(verdictFor('high-spike', [0, 0.1]).includes('有高延迟尖峰'));
assert.ok(verdictFor('sustained-high', [0.08, 0.08]).includes('持续高延迟'));

const source = fs.readFileSync(
  path.join(__dirname, '..', 'bigscreen', 'charts', 'evidence-chart.js'),
  'utf8'
);
assert.ok(!source.includes('prometheus'), 'the Evidence facade does not fetch or query data');
assert.ok(!source.includes('tournament-mode'), 'Evidence has no tournament-specific renderer branch');

console.log('bigscreen Evidence chart tests passed');
