const assert = require('assert');
const fs = require('fs');
const path = require('path');
const ispChart = require('../bigscreen/charts/isp-chart.js');

assert.deepStrictEqual(
  Object.keys(ispChart),
  ['createIspChartRenderer'],
  'the ISP chart module exposes only its dependency-injected renderer factory'
);

const calls = [];
const capacityCalls = [];
const formatBits = (value) => `${value}bps`;
const renderIspChart = ispChart.createIspChartRenderer({
  renderLineChart: (...args) => calls.push(args),
  formatBits,
  ispChartMaxBps: (name, index) => {
    capacityCalls.push([name, index]);
    return 800000000;
  }
});

const download = {
  name: '下载',
  values: [{ t: 100, v: 200000000 }, { t: 105, v: null }]
};
const upload = {
  name: '上传',
  values: [{ t: 100, v: 100000000 }, { t: 105, v: 120000000 }]
};
const result = { name: 'ISP A', download, upload };

renderIspChart({
  containerId: 'ispChart2',
  result,
  resultIndex: 2,
  compactTournamentChart: false
});
assert.strictEqual(calls.length, 1);
assert.strictEqual(calls[0][0], 'ispChart2');
assert.strictEqual(calls[0][1][0], download, 'download remains the first series');
assert.strictEqual(calls[0][1][1], upload, 'upload remains the second series');
assert.deepStrictEqual(calls[0][2], {
  axisFormatter: formatBits,
  valueFormatter: formatBits,
  minWidth: 320,
  axisPadLeft: 92,
  axisPadRight: 38,
  axisPadTop: 12,
  axisPadBottom: undefined,
  fill: true,
  legend: 'bottom',
  maxY: 800000000,
  minMax: 1,
  calcs: ['last', 'max']
}, 'normal ISP rendering preserves every line-chart option');
assert.deepStrictEqual(capacityCalls, [['ISP A', 2]]);
assert.strictEqual(download.values[1].v, null, 'unknown samples pass through unchanged');

renderIspChart({
  containerId: 'ispChart0',
  result,
  resultIndex: 0,
  compactTournamentChart: true
});
assert.deepStrictEqual(calls[1][2], {
  axisFormatter: formatBits,
  valueFormatter: formatBits,
  minWidth: 120,
  axisPadLeft: 76,
  axisPadRight: 12,
  axisPadTop: 6,
  axisPadBottom: 20,
  fill: true,
  legend: 'bottom',
  maxY: 800000000,
  minMax: 1,
  calcs: ['last', 'max']
}, 'tournament mode preserves the compact ISP chart geometry');

const emptyDownload = { name: '下载', values: [] };
const emptyUpload = { name: '上传', values: [] };
renderIspChart({
  containerId: 'ispChartEmpty',
  result: { name: 'ISP Empty', download: emptyDownload, upload: emptyUpload },
  resultIndex: 3,
  compactTournamentChart: false
});
assert.deepStrictEqual(calls[2][1], [emptyDownload, emptyUpload], 'empty series remain delegated to the generic no-data behavior');

const source = fs.readFileSync(
  path.join(__dirname, '..', 'bigscreen', 'charts', 'isp-chart.js'),
  'utf8'
);
assert.ok(!source.includes('prometheus'), 'the ISP facade does not fetch or query data');
assert.ok(!source.includes('createIspCarousel'), 'carousel state remains app orchestration');

console.log('bigscreen ISP chart tests passed');
