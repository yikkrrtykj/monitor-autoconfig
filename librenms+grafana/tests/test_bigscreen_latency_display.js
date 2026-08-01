const assert = require('assert');
const { suppressIsolatedLatencySpikes } = require('../bigscreen/utils.js');

function series(values) {
  return [{ name: 'switch-a', metric: { instance: 'switch-a' }, values }];
}

const isolatedInput = series([
  { t: 100, v: 0.002 },
  { t: 102, v: 0.2 },
  { t: 104, v: 0.004 }
]);
const isolatedOutput = suppressIsolatedLatencySpikes(isolatedInput);
assert.strictEqual(isolatedOutput[0].values[1].v, 0.003);
assert.strictEqual(isolatedInput[0].values[1].v, 0.2, 'input data must remain unchanged');

const sustainedInput = series([
  { t: 100, v: 0.002 },
  { t: 102, v: 0.05 },
  { t: 104, v: 0.2 },
  { t: 106, v: 0.004 }
]);
assert.deepStrictEqual(
  suppressIsolatedLatencySpikes(sustainedInput)[0].values.map((point) => point.v),
  sustainedInput[0].values.map((point) => point.v),
  'two consecutive samples at or above 50 ms must remain visible'
);

const gapInput = series([
  { t: 100, v: 0.003 },
  { t: 102, v: 0.08 },
  { t: 108, v: 0.09 },
  { t: 110, v: 0.005 }
]);
const gapOutput = suppressIsolatedLatencySpikes(gapInput)[0].values;
assert.ok(gapOutput[1].v < 0.05);
assert.ok(gapOutput[2].v < 0.05);

console.log('bigscreen latency display tests passed');
