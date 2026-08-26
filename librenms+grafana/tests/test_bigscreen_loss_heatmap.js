const assert = require('assert');
const fs = require('fs');
const path = require('path');
const lossHeatmap = require('../bigscreen/charts/loss-heatmap.js');

assert.deepStrictEqual(
  Object.keys(lossHeatmap),
  ['createLossHeatmapRenderer'],
  'the Loss Heatmap module exposes only its dependency-injected renderer factory'
);

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

const containers = new Map();
const noDataCalls = [];
const document = {
  getElementById(id) {
    if (!containers.has(id)) containers.set(id, { innerHTML: '' });
    return containers.get(id);
  }
};
const renderLossHeatmap = lossHeatmap.createLossHeatmapRenderer({
  document,
  renderNoData: (container) => {
    noDataCalls.push(container);
    container.innerHTML = '<div class="no-data">暂无数据</div>';
  },
  formatTime: (timestamp) => `T${Number(timestamp).toFixed(1)}`,
  escapeHtml
});

function render(series) {
  const container = document.getElementById('lossHeatmap');
  container.innerHTML = '';
  renderLossHeatmap('lossHeatmap', series);
  return container.innerHTML;
}

function count(html, pattern) {
  return (html.match(pattern) || []).length;
}

let html = render([]);
assert.ok(html.includes('class="no-data"'));
assert.strictEqual(noDataCalls.length, 1, 'an empty list delegates to the existing no-data renderer');

html = render([{ name: 'empty', values: [] }]);
assert.ok(html.includes('class="no-data"'));
assert.strictEqual(noDataCalls.length, 2, 'series without timeline samples remain empty');

html = render([{
  name: 'switch <one>',
  values: [
    { t: 0, v: 0 },
    { t: 20, v: 0.02 },
    { t: 40, v: 1 },
    { t: 59, v: null }
  ]
}]);
assert.ok(html.includes('class="heatmap "'));
assert.ok(html.includes('class="heatmap-row"'));
assert.ok(html.includes('class="heatmap-name"'));
assert.ok(html.includes('class="heatmap-cells"'));
assert.ok(html.includes('class="heatmap-axis-times"'));
assert.ok(html.includes('switch &lt;one&gt;'), 'device labels retain the existing escaping');
assert.strictEqual(count(html, /class="heatmap-cell /g), 60, 'each row keeps exactly 60 time buckets');
assert.strictEqual(count(html, /heatmap-cell good/g), 1, 'zero percent loss is good');
assert.strictEqual(count(html, /heatmap-cell warn/g), 1, 'partial loss above one percent is warning');
assert.strictEqual(count(html, /heatmap-cell bad/g), 1, 'loss above fifty percent is bad');
assert.strictEqual(count(html, /heatmap-cell missing/g), 57, 'null and absent buckets remain missing');
assert.ok(html.includes('丢包 0.0%'));
assert.ok(html.includes('丢包 2.0%'));
assert.ok(html.includes('丢包 100.0%'));
assert.ok(html.includes('无数据'));

html = render([{
  name: 'bucket-max',
  values: [
    { t: 0, v: 0 },
    { t: 0.5, v: 0.02 },
    { t: 60, v: 0 }
  ]
}]);
assert.strictEqual(count(html, /heatmap-cell warn/g), 1, 'a bucket retains its maximum loss sample');
assert.strictEqual(count(html, /heatmap-cell good/g), 1, 'separate time buckets retain their own state');
assert.ok(html.includes('<span>T0.0</span><span>T30.0</span><span>T60.0</span>'));

html = render([{
  name: 'unknown-only',
  values: [{ t: 100, v: null }, { t: 102, v: null }]
}]);
assert.ok(html.includes('unknown-only'), 'all-unknown series remains visible');
assert.strictEqual(count(html, /heatmap-cell missing/g), 60);
assert.strictEqual(noDataCalls.length, 2, 'all-unknown is not collapsed into the empty state');

const orderedNames = Array.from({ length: 13 }, (_, index) => `device-${String(index).padStart(2, '0')}`);
html = render(orderedNames.map((name, index) => ({
  name,
  values: [{ t: 100, v: index % 3 === 0 ? 0.02 : 0 }]
})));
assert.ok(html.includes('dense-heatmap heatmap-split'));
assert.strictEqual(count(html, /class="heatmap-column"/g), 2, 'more than twelve devices use two columns');
assert.ok(html.includes('style="--heatmap-rows:7"'));
assert.ok(html.includes('style="--heatmap-rows:6"'));
for (let index = 1; index < orderedNames.length; index += 1) {
  assert.ok(
    html.indexOf(orderedNames[index - 1]) < html.indexOf(orderedNames[index]),
    'the renderer preserves the prepared device order'
  );
}

const source = fs.readFileSync(
  path.join(__dirname, '..', 'bigscreen', 'charts', 'loss-heatmap.js'),
  'utf8'
);
assert.ok(!source.includes('tournament-mode'), 'tournament visibility remains an unchanged CSS concern');
assert.ok(!source.includes('prometheus'), 'the renderer does not own data fetching or queries');

console.log('bigscreen Loss Heatmap tests passed');
