const assert = require('assert');
const {
  suppressIsolatedLatencySpikes,
  smoothLatencyJitter,
  stepPathFromPoints,
  splitPointsOnGaps
} = require('../bigscreen/utils.js');

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
assert.strictEqual(isolatedOutput[0].values[0].v, 0.002, 'the sample before an isolated spike must not change');
assert.strictEqual(isolatedOutput[0].values[2].v, 0.004, 'the sample after an isolated spike must not change');
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

assert.strictEqual(
  stepPathFromPoints(['10,20', '30,80', '60,20']),
  'M 10,20 H 30 V 80 H 60 V 20',
  'binary online state must change vertically instead of drawing diagonal ramps'
);

assert.deepStrictEqual(
  splitPointsOnGaps([
    { t: 100, v: 1 },
    { t: 102, v: 1 },
    { t: 180, v: 0 },
    { t: 182, v: 1 }
  ], 6).map((segment) => segment.map((point) => point.t)),
  [[100, 102], [180, 182]],
  'missing samples must produce a visible blank gap rather than a connecting line'
);

const jitterInput = series([
  { t: 100, v: 0.001 },
  { t: 102, v: 0.003 },
  { t: 104, v: 0.001 },
  { t: 106, v: 0.003 },
  { t: 108, v: 0.001 }
]);
const jitterOutput = smoothLatencyJitter(jitterInput, { preserveAbove: 0.05, radius: 2 });
assert.strictEqual(jitterOutput[0].values[1].v, 0.002);
assert.strictEqual(jitterOutput[0].values[2].v, 0.001);
assert.strictEqual(jitterInput[0].values[1].v, 0.003, 'jitter smoothing must not mutate input data');

const preservedHighInput = series([
  { t: 100, v: 0.001 },
  { t: 102, v: 0.06 },
  { t: 104, v: 0.07 },
  { t: 106, v: 0.002 }
]);
const preservedHighOutput = smoothLatencyJitter(preservedHighInput, { preserveAbove: 0.05, radius: 2 });
assert.strictEqual(preservedHighOutput[0].values[1].v, 0.06);
assert.strictEqual(preservedHighOutput[0].values[2].v, 0.07);

const gapInput = series([
  { t: 100, v: 0.003 },
  { t: 102, v: 0.08 },
  { t: 108, v: 0.09 },
  { t: 110, v: 0.005 }
]);
const gapOutput = suppressIsolatedLatencySpikes(gapInput)[0].values;
assert.ok(gapOutput[1].v < 0.05);
assert.ok(gapOutput[2].v < 0.05);

const incidentInput = series([
  { t: 100, v: 0.002 },
  { t: 102, v: 0.06 },
  { t: 104, v: 0.2 },
  { t: 106, v: 0.003 }
]);
assert.deepStrictEqual(
  suppressIsolatedLatencySpikes(incidentInput)[0].values.map((point) => point.v),
  incidentInput[0].values.map((point) => point.v),
  'sustained high-latency incidents must remain unchanged'
);

console.log('bigscreen latency display tests passed');
