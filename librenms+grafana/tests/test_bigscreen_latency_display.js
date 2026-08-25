const assert = require('assert');
const {
  roundUpToStep,
  linePathFromPoints,
  stepPathFromPoints,
  splitPointsOnGaps,
  lineSeriesStats,
  lineFailurePoints,
  seriesSignature
} = require('../bigscreen/utils.js');

assert.ok(Math.abs(roundUpToStep(0.027, 0.01) - 0.03) < 1e-12, '27 ms gets a 30 ms ceiling');
assert.ok(Math.abs(roundUpToStep(0.03, 0.01) - 0.03) < 1e-12, 'an exact 30 ms peak stays at 30 ms');
assert.ok(Math.abs(roundUpToStep(0.031, 0.01) - 0.04) < 1e-12, '31 ms gets a 40 ms ceiling');

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

const explicitFailureValues = [
  { t: 100, v: 0.002 },
  { t: 102, v: null, status: 'failure' },
  { t: 104, v: 0.003 }
];
const explicitFailureSegments = splitPointsOnGaps(explicitFailureValues, 6);
assert.deepStrictEqual(
  explicitFailureSegments.map((segment) => segment.map((point) => point.t)),
  [[100], [104]],
  'an explicit failure breaks the line even when its finite neighbours are only four seconds apart'
);
assert.deepStrictEqual(
  explicitFailureSegments.map((segment) => linePathFromPoints(
    segment.map((point) => `${point.t},${point.v}`),
    true
  )),
  ['M 100,0.002', 'M 104,0.003'],
  'failure-separated points produce independent SVG paths and Bezier smoothing cannot cross the gap'
);

const explicitUnknownValues = [
  { t: 100, v: 0.002 },
  { t: 102, v: null, status: 'unknown' },
  { t: 104, v: 0.003 }
];
assert.deepStrictEqual(
  splitPointsOnGaps(explicitUnknownValues, 6).map((segment) => segment.map((point) => point.t)),
  [[100], [104]],
  'an explicit unknown point breaks the line'
);
assert.deepStrictEqual(
  lineFailurePoints([...explicitFailureValues, ...explicitUnknownValues]),
  [{ t: 102, v: null, status: 'failure' }],
  'only confirmed failures produce failure markers; unknown points only break the line'
);
assert.deepStrictEqual(
  splitPointsOnGaps([
    { t: 100, v: 0.002 },
    { t: 102, v: null },
    { t: 104, v: 0.003 },
    { t: 106, v: Number.NaN },
    { t: 108, v: 0.004 }
  ], 6).map((segment) => segment.map((point) => point.t)),
  [[100], [104], [108]],
  'null and non-finite latency values are never added to a drawable path'
);

assert.deepStrictEqual(
  lineSeriesStats([
    { t: 100, v: 0.002 },
    { t: 102, v: null, status: 'failure' },
    { t: 104, v: Number.NaN },
    { t: 106, v: 0.004 }
  ]),
  { last: 0.004, max: 0.004, mean: 0.003, min: 0.002 },
  'line latency statistics use finite drawable latency values only'
);
assert.deepStrictEqual(
  lineSeriesStats([{ t: 100, v: 0 }]),
  { last: 0, max: 0, mean: 0, min: 0 },
  'a real finite zero remains a valid latency value without implying failure'
);

const failureSignature = seriesSignature([{
  name: 'switch-a',
  values: [{ t: 100, v: null, status: 'failure' }]
}]);
const unknownSignature = seriesSignature([{
  name: 'switch-a',
  values: [{ t: 100, v: null, status: 'unknown' }]
}]);
assert.notStrictEqual(
  failureSignature,
  unknownSignature,
  'failure and unknown states must invalidate the render cache independently'
);
assert.strictEqual(
  seriesSignature([{
    name: 'switch-a',
    values: [
      { t: 100, v: 0.002 },
      { t: 102, v: 0.003 },
      { t: 104, v: 0.004 }
    ]
  }]),
  'switch-a#3#1763579967',
  'ordinary finite points keep the pre-failure-support signature'
);

const normalLatencyValues = [
  { t: 100, v: 0.002 },
  { t: 102, v: 0.003 },
  { t: 104, v: 0.004 }
];
assert.deepStrictEqual(
  splitPointsOnGaps(normalLatencyValues, 6),
  [normalLatencyValues],
  'ordinary finite latency points remain one unchanged drawable segment'
);

const visualPoints = ['10,20', '30,40', '50,30'];
const visualPointsBefore = [...visualPoints];
assert.ok(linePathFromPoints(visualPoints, true).includes(' C '), 'visual smoothing uses a curved SVG path');
assert.deepStrictEqual(visualPoints, visualPointsBefore, 'visual smoothing must not mutate sample coordinates');

const peakPoints = ['0,100', '10,98', '20,0', '30,98', '40,100'];
const peakPath = linePathFromPoints(peakPoints, true);
const cubicPattern = /C (-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?) (-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?) (-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)/g;
const cubicSegments = [...peakPath.matchAll(cubicPattern)];
assert.strictEqual(cubicSegments.length, peakPoints.length - 1);
cubicSegments.forEach((segment, index) => {
  const startY = Number(peakPoints[index].split(',')[1]);
  const endY = Number(peakPoints[index + 1].split(',')[1]);
  const minY = Math.min(startY, endY);
  const maxY = Math.max(startY, endY);
  [Number(segment[2]), Number(segment[4])].forEach((controlY) => {
    assert.ok(
      controlY >= minY && controlY <= maxY,
      'smoothed latency controls must stay inside the real endpoint range'
    );
  });
});

console.log('bigscreen latency display tests passed');
