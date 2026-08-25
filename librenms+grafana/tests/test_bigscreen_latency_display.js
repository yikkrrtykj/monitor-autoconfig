const assert = require('assert');
const {
  roundUpToStep,
  linePathFromPoints,
  stepPathFromPoints,
  splitPointsOnGaps
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
