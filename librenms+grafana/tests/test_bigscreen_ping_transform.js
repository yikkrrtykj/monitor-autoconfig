const assert = require('assert');
const pingTransform = require('../bigscreen/metrics/ping-transform.js');

assert.deepStrictEqual(
  Object.keys(pingTransform),
  ['buildInfrastructurePingPresentation'],
  'the module exposes only the presentation adapter'
);

const { buildInfrastructurePingPresentation } = pingTransform;

function series(values, name = 'switch-a') {
  return [{ name, metric: { instance: name }, values }];
}

function displayValues(values) {
  return buildInfrastructurePingPresentation(series(values))
    .displayLatencySeries[0].values.map((point) => point.v);
}

const isolatedInput = series([
  { t: 100, v: 0.002 },
  { t: 102, v: 0.2 },
  { t: 104, v: 0.004 }
]);
const isolatedPresentation = buildInfrastructurePingPresentation(isolatedInput);
assert.deepStrictEqual(
  isolatedPresentation.displayLatencySeries[0].values.map((point) => point.v),
  [0.002, 0.002, 0.004],
  'an isolated high sample uses the preceding real sample'
);
assert.deepStrictEqual(
  isolatedPresentation.rawLatencySeries,
  isolatedInput,
  'raw latency keeps the original values'
);
assert.strictEqual(isolatedInput[0].values[1].v, 0.2, 'the adapter does not mutate its input');

assert.deepStrictEqual(
  displayValues([
    { t: 100, v: 0.002 },
    { t: 102, v: 0.002 },
    { t: 104, v: 0.003 },
    { t: 106, v: 0.2 },
    { t: 108, v: 0.019 },
    { t: 110, v: 0.003 },
    { t: 112, v: 0.002 }
  ]),
  [0.002, 0.002, 0.003, 0.003, 0.019, 0.003, 0.002],
  'the preceding normal sample wins before the following sample at the same distance'
);

const sustained = [
  { t: 100, v: 0.002 },
  { t: 102, v: 0.02 },
  { t: 104, v: 0.2 },
  { t: 106, v: 0.004 }
];
assert.deepStrictEqual(
  displayValues(sustained),
  sustained.map((point) => point.v),
  'two consecutive samples at or above 20 ms remain visible'
);

assert.deepStrictEqual(
  displayValues([
    { t: 100, v: 0.002 },
    { t: 102, v: 0.02 },
    { t: 104, v: 0.004 }
  ]),
  [0.002, 0.002, 0.004],
  'one isolated sample exactly at 20 ms is suppressed'
);

const normalBaseline = [
  { t: 100, v: 0.001 },
  { t: 102, v: 0.003 },
  { t: 104, v: 0.001 },
  { t: 106, v: 0.004 },
  { t: 108, v: 0.002 }
];
assert.deepStrictEqual(
  buildInfrastructurePingPresentation(series(normalBaseline)).displayLatencySeries[0].values,
  normalBaseline,
  'normal latency samples remain unchanged without averaging or smoothing'
);

assert.deepStrictEqual(
  displayValues([
    { t: 100, v: 0.003 },
    { t: 102, v: 0.08 },
    { t: 108, v: 0.09 },
    { t: 110, v: 0.005 }
  ]),
  [0.003, 0.003, 0.005, 0.005],
  'high samples separated by more than three seconds are isolated independently'
);

const incident = [
  { t: 100, v: 0.002 },
  { t: 102, v: 0.06 },
  { t: 104, v: 0.2 },
  { t: 106, v: 0.003 }
];
assert.deepStrictEqual(
  displayValues(incident),
  incident.map((point) => point.v),
  'sustained high-latency incidents remain unchanged'
);

assert.deepStrictEqual(
  displayValues([
    { t: 100, v: 0.2 },
    { t: 102, v: 0.004 }
  ]),
  [0.004, 0.004],
  'the following normal sample is used when no preceding sample exists'
);

assert.deepStrictEqual(
  displayValues([{ t: 100, v: 0.2 }]),
  [0.2],
  'an isolated spike remains unchanged when no normal replacement exists'
);

assert.deepStrictEqual(
  displayValues([
    { t: 100, v: 0.003 },
    { t: 115, v: 0.2 }
  ]),
  [0.003, 0.003],
  'a replacement exactly fifteen seconds away remains usable'
);

assert.deepStrictEqual(
  displayValues([
    { t: 100, v: 0.003 },
    { t: 116, v: 0.2 }
  ]),
  [0.003, 0.2],
  'a replacement more than fifteen seconds away is rejected'
);

assert.deepStrictEqual(
  displayValues([
    { t: 100, v: 0.2 },
    { t: 102, v: 0 }
  ]),
  [0, 0],
  'zero remains a usable normal replacement under the current algorithm'
);

const independentInput = series([
  { t: 100, v: 0.002, note: 'first' },
  { t: 102, v: 0.004, note: 'second' }
]);
const independent = buildInfrastructurePingPresentation(independentInput);
independent.rawLatencySeries[0].name = 'raw-name';
independent.rawLatencySeries[0].metric.instance = 'raw-instance';
independent.rawLatencySeries[0].values[0].v = 0.9;
assert.strictEqual(independent.displayLatencySeries[0].name, 'switch-a');
assert.strictEqual(independent.displayLatencySeries[0].metric.instance, 'switch-a');
assert.strictEqual(independent.displayLatencySeries[0].values[0].v, 0.002);

independent.displayLatencySeries[0].name = 'display-name';
independent.displayLatencySeries[0].metric.instance = 'display-instance';
independent.displayLatencySeries[0].values[1].v = 0.8;
assert.strictEqual(independent.rawLatencySeries[0].name, 'raw-name');
assert.strictEqual(independent.rawLatencySeries[0].metric.instance, 'raw-instance');
assert.strictEqual(independent.rawLatencySeries[0].values[1].v, 0.004);
assert.strictEqual(independentInput[0].name, 'switch-a');
assert.strictEqual(independentInput[0].metric.instance, 'switch-a');
assert.strictEqual(independentInput[0].values[0].v, 0.002);

const multiple = buildInfrastructurePingPresentation([
  ...series([
    { t: 100, v: 0.002 },
    { t: 102, v: 0.2 },
    { t: 104, v: 0.004 }
  ], 'switch-a'),
  ...series([
    { t: 100, v: 0.006 },
    { t: 102, v: 0.007 }
  ], 'switch-b')
]);
assert.deepStrictEqual(
  multiple.displayLatencySeries.map((item) => item.values.map((point) => point.v)),
  [[0.002, 0.002, 0.004], [0.006, 0.007]],
  'each series is transformed independently'
);

console.log('bigscreen ping transform tests passed');
