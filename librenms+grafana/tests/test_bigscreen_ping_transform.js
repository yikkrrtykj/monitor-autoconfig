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

function successSeries(values, name = 'switch-a') {
  return series(values, name);
}

function buildV2(latencyValues, successValues, name = 'switch-a') {
  return buildInfrastructurePingPresentation({
    latencySeries: series(latencyValues, name),
    successSeries: successSeries(successValues, name)
  });
}

function v2Values(latencyValues, successValues, name = 'switch-a') {
  return buildV2(latencyValues, successValues, name)
    .displayLatencySeries[0].values;
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

const normalV2 = buildV2([
  { t: 100, v: 0.002 },
  { t: 102, v: 0.003 },
  { t: 104, v: 0.019 }
], [
  { t: 100, v: 1 },
  { t: 102, v: 1 },
  { t: 104, v: 1 }
]);
assert.deepStrictEqual(
  Object.keys(normalV2),
  ['displayLatencySeries'],
  'the success-aware object API exposes presentation data only'
);
assert.deepStrictEqual(
  normalV2.displayLatencySeries[0].values.map((point) => point.v),
  [0.002, 0.003, 0.019],
  'successful latency below 20 ms remains exactly raw'
);
assert.strictEqual(normalV2.displayLatencySeries[0].currentStatus, 'online');
assert.ok(!Object.prototype.hasOwnProperty.call(normalV2, 'rawLatencySeries'));

assert.deepStrictEqual(
  v2Values([
    { t: 100, v: 0.002 },
    { t: 102, v: 0.004 },
    { t: 104, v: 0.02 }
  ], [
    { t: 100, v: 1 },
    { t: 102, v: 1 },
    { t: 104, v: 1 }
  ]).map((point) => point.v),
  [0.002, 0.004, 0.003],
  '20 ms is high latency and uses the mean of the previous two raw normal samples'
);

assert.deepStrictEqual(
  v2Values([
    { t: 0, v: 0.002 },
    { t: 2, v: 0.004 },
    { t: 10, v: 0.08 },
    { t: 13.999, v: 0.09 }
  ], [
    { t: 0, v: 1 },
    { t: 2, v: 1 },
    { t: 10, v: 1 },
    { t: 13.999, v: 1 }
  ]).map((point) => point.v),
  [0.002, 0.004, 0.003, 0.003],
  'a high run spanning 3.999 seconds remains provisional'
);

assert.deepStrictEqual(
  v2Values([
    { t: 0, v: 0.002 },
    { t: 2, v: 0.004 },
    { t: 10, v: 0.08 },
    { t: 14, v: 0.09 }
  ], [
    { t: 0, v: 1 },
    { t: 2, v: 1 },
    { t: 10, v: 1 },
    { t: 14, v: 1 }
  ]).map((point) => point.v),
  [0.002, 0.004, 0.08, 0.09],
  'a high run spanning exactly four seconds restores the complete raw run'
);

const oneSecondHighValues = [0.08, 0.09, 0.1, 0.11, 0.12];
assert.deepStrictEqual(
  v2Values([
    { t: 0, v: 0.002 },
    ...oneSecondHighValues.map((v, index) => ({ t: 10 + index, v }))
  ], [
    { t: 0, v: 1 },
    ...oneSecondHighValues.map((_v, index) => ({ t: 10 + index, v: 1 }))
  ]).map((point) => point.v),
  [0.002, ...oneSecondHighValues],
  'one-second samples still use a four-second persistence boundary'
);

const twoSecondHighValues = [0.08, 0.09, 0.1];
assert.deepStrictEqual(
  v2Values([
    { t: 0, v: 0.002 },
    ...twoSecondHighValues.map((v, index) => ({ t: 10 + index * 2, v }))
  ], [
    { t: 0, v: 1 },
    ...twoSecondHighValues.map((_v, index) => ({ t: 10 + index * 2, v: 1 }))
  ]).map((point) => point.v),
  [0.002, ...twoSecondHighValues],
  'two-second samples restore the run when the third high point reaches four seconds'
);

assert.deepStrictEqual(
  v2Values([
    { t: 0, v: 0.002 },
    { t: 10, v: 0.08 },
    { t: 11.4, v: 0.09 },
    { t: 14.2, v: 0.1 }
  ], [
    { t: 0, v: 1 },
    { t: 10, v: 1 },
    { t: 11.4, v: 1 },
    { t: 14.2, v: 1 }
  ]).map((point) => point.v),
  [0.002, 0.08, 0.09, 0.1],
  'non-uniform sample spacing is classified only by timestamp span'
);

assert.deepStrictEqual(
  v2Values([
    { t: 100, v: 0.006 },
    { t: 102, v: 0.08 }
  ], [
    { t: 100, v: 1 },
    { t: 102, v: 1 }
  ]).map((point) => point.v),
  [0.006, 0.006],
  'one prior normal raw sample is used directly'
);

assert.deepStrictEqual(
  v2Values([
    { t: 100, v: 0.08 },
    { t: 102, v: 0.09 }
  ], [
    { t: 100, v: 1 },
    { t: 102, v: 1 }
  ]).map((point) => point.v),
  [0.08, 0.09],
  'a provisional high run remains raw when no prior normal baseline exists'
);

assert.deepStrictEqual(
  v2Values([
    { t: 100, v: 0.08 },
    { t: 102, v: 0.004 }
  ], [
    { t: 100, v: 1 },
    { t: 102, v: 1 }
  ]).map((point) => point.v),
  [0.08, 0.004],
  'future normal samples are never used to replace an earlier high point'
);

const failureInterrupted = v2Values([
  { t: 0, v: 0.002 },
  { t: 2, v: 0.004 },
  { t: 4, v: 0.08 },
  { t: 6, v: 0 },
  { t: 8, v: 0.09 }
], [
  { t: 0, v: 1 },
  { t: 2, v: 1 },
  { t: 4, v: 1 },
  { t: 6, v: 0 },
  { t: 8, v: 1 }
]);
assert.deepStrictEqual(failureInterrupted, [
  { t: 0, v: 0.002 },
  { t: 2, v: 0.004 },
  { t: 4, v: 0.003 },
  { t: 6, v: null, status: 'failure' },
  { t: 8, v: 0.003 }
], 'failure interrupts the high run and neither failure zero nor presentation values enter the raw baseline');

assert.deepStrictEqual(
  v2Values([
    { t: 100, v: 0.002 },
    { t: 102, v: 0 },
    { t: 104, v: 0.003 }
  ], [
    { t: 100, v: 1 },
    { t: 102, v: 0 },
    { t: 104, v: 1 }
  ]),
  [
    { t: 100, v: 0.002 },
    { t: 102, v: null, status: 'failure' },
    { t: 104, v: 0.003 }
  ],
  'the production RTT/success fixture presents a failure sentinel instead of zero milliseconds'
);

const provisionalRun = buildV2([
  { t: 0, v: 0.002 },
  { t: 2, v: 0.004 },
  { t: 10, v: 0.08 },
  { t: 12, v: 0.09 }
], [
  { t: 0, v: 1 },
  { t: 2, v: 1 },
  { t: 10, v: 1 },
  { t: 12, v: 1 }
]);
assert.deepStrictEqual(
  provisionalRun.displayLatencySeries[0].values.map((point) => point.v),
  [0.002, 0.004, 0.003, 0.003]
);
const confirmedRun = buildV2([
  { t: 0, v: 0.002 },
  { t: 2, v: 0.004 },
  { t: 10, v: 0.08 },
  { t: 12, v: 0.09 },
  { t: 14, v: 0.1 }
], [
  { t: 0, v: 1 },
  { t: 2, v: 1 },
  { t: 10, v: 1 },
  { t: 12, v: 1 },
  { t: 14, v: 1 }
]);
assert.deepStrictEqual(
  confirmedRun.displayLatencySeries[0].values.map((point) => point.v),
  [0.002, 0.004, 0.08, 0.09, 0.1],
  'a later point reaching four seconds restores every earlier point in the high run'
);

const recoveredFailureRun = buildV2([
  { t: 106, v: 0.005 }
], [
  { t: 100, v: 0 },
  { t: 102, v: 0 },
  { t: 104, v: 0 },
  { t: 106, v: 1 }
]);
assert.deepStrictEqual(recoveredFailureRun.displayLatencySeries[0].values, [
  { t: 100, v: null, status: 'failure' },
  { t: 106, v: 0.005 }
], 'a consecutive failure run produces one marker and successful latency resumes afterwards');
assert.strictEqual(recoveredFailureRun.displayLatencySeries[0].currentStatus, 'online');

const overnightFailures = Array.from({ length: 450 }, (_, index) => ({
  t: 100 + index * 2,
  v: 0
}));
const overnightPresentation = buildInfrastructurePingPresentation({
  latencySeries: [],
  successSeries: successSeries(overnightFailures)
});
assert.deepStrictEqual(overnightPresentation.displayLatencySeries[0].values, [
  { t: 100, v: null, status: 'failure' }
], 'a long offline window is compressed to one failure marker');
assert.strictEqual(
  overnightPresentation.displayLatencySeries[0].currentStatus,
  'offline',
  'a series whose newest explicit success value is zero remains offline'
);

const missingSuccess = buildInfrastructurePingPresentation({
  latencySeries: series([
    { t: 100, v: 0.002 },
    { t: 102, v: 0.08 },
    { t: 104, v: 0.003 }
  ]),
  successSeries: []
});
assert.deepStrictEqual(missingSuccess.displayLatencySeries[0].values, [
  { t: 100, v: null, status: 'unknown' }
], 'consecutive latency points without success evidence collapse into one unknown gap');
assert.strictEqual(missingSuccess.displayLatencySeries[0].currentStatus, 'unknown');
assert.strictEqual(
  missingSuccess.displayLatencySeries[0].values.some((point) => point.status === 'failure'),
  false,
  'missing success never fabricates a failure marker'
);

const staleOfflineThenUnknown = buildV2([
  { t: 100, v: 0 },
  { t: 102, v: 0.003 }
], [
  { t: 100, v: 0 }
]);
assert.deepStrictEqual(staleOfflineThenUnknown.displayLatencySeries[0].values, [
  { t: 100, v: null, status: 'failure' },
  { t: 102, v: null, status: 'unknown' }
]);
assert.strictEqual(
  staleOfflineThenUnknown.displayLatencySeries[0].currentStatus,
  'unknown',
  'newer unknown evidence must not preserve a stale offline status'
);

const unknownInterrupted = v2Values([
  { t: 0, v: 0.002 },
  { t: 2, v: 0.004 },
  { t: 4, v: 0.08 },
  { t: 6, v: 0.09 },
  { t: 8, v: 0.1 }
], [
  { t: 0, v: 1 },
  { t: 2, v: 1 },
  { t: 4, v: 1 },
  { t: 8, v: 1 }
]);
assert.deepStrictEqual(unknownInterrupted, [
  { t: 0, v: 0.002 },
  { t: 2, v: 0.004 },
  { t: 4, v: 0.003 },
  { t: 6, v: null, status: 'unknown' },
  { t: 8, v: 0.003 }
], 'unknown evidence interrupts a high run without becoming a failure marker');

const unequalLengths = buildV2([
  { t: 100, v: 0.002 },
  { t: 102, v: 0.003 },
  { t: 106, v: 0.004 }
], [
  { t: 100, v: 1 },
  { t: 104, v: 0 },
  { t: 106, v: 1 }
]);
assert.deepStrictEqual(unequalLengths.displayLatencySeries[0].values, [
  { t: 100, v: 0.002 },
  { t: 102, v: null, status: 'unknown' },
  { t: 104, v: null, status: 'failure' },
  { t: 106, v: 0.004 }
], 'success-only and latency-only timestamps are aligned by timestamp rather than array index');
assert.strictEqual(unequalLengths.displayLatencySeries[0].currentStatus, 'online');

const orderedMultiSeriesInput = {
  latencySeries: [
    ...series([{ t: 100, v: 0.002 }, { t: 102, v: 0.08 }], 'switch-a'),
    ...series([{ t: 100, v: 0.006 }, { t: 102, v: 0.007 }], 'switch-b')
  ],
  successSeries: [
    ...successSeries([{ t: 100, v: 1 }, { t: 102, v: 1 }], 'switch-a'),
    ...successSeries([{ t: 100, v: 1 }, { t: 102, v: 1 }], 'switch-b')
  ]
};
const shuffledMultiSeriesInput = {
  latencySeries: [
    ...series([{ t: 102, v: 0.08 }, { t: 100, v: 0.002 }], 'switch-a'),
    ...series([{ t: 102, v: 0.007 }, { t: 100, v: 0.006 }], 'switch-b')
  ],
  successSeries: [
    ...successSeries([{ t: 102, v: 1 }, { t: 100, v: 1 }], 'switch-b'),
    ...successSeries([{ t: 102, v: 1 }, { t: 100, v: 1 }], 'switch-a')
  ]
};
assert.deepStrictEqual(
  buildInfrastructurePingPresentation(shuffledMultiSeriesInput),
  buildInfrastructurePingPresentation(orderedMultiSeriesInput),
  'series and point ordering do not affect identity-plus-timestamp alignment'
);

const immutableV2Input = {
  latencySeries: series([
    { t: 100, v: 0.002, note: 'normal' },
    { t: 102, v: 0.08, note: 'high' }
  ]),
  successSeries: successSeries([
    { t: 100, v: 1, note: 'online' },
    { t: 102, v: 1, note: 'online' }
  ])
};
const immutableV2Before = JSON.parse(JSON.stringify(immutableV2Input));
const immutableV2Output = buildInfrastructurePingPresentation(immutableV2Input);
assert.deepStrictEqual(immutableV2Input, immutableV2Before, 'v2 never mutates either input branch');
immutableV2Output.displayLatencySeries[0].name = 'changed-output';
immutableV2Output.displayLatencySeries[0].metric.instance = 'changed-instance';
immutableV2Output.displayLatencySeries[0].values[0].v = 0.9;
assert.deepStrictEqual(immutableV2Input, immutableV2Before, 'v2 output metadata and points do not alias input data');

function legacyReferenceValues(values) {
  const threshold = 0.02;
  const maxGapSeconds = 3;
  const replacementRadius = 5;
  const replacementWindowSeconds = 15;
  const source = values.map((point) => ({ ...point }));
  const output = source.map((point) => ({ ...point }));

  function nearestNormalSample(index) {
    const centerTime = source[index] && source[index].t;
    function usable(point) {
      return Boolean(point)
        && Number.isFinite(point.v)
        && point.v < threshold
        && (!Number.isFinite(centerTime)
          || !Number.isFinite(point.t)
          || Math.abs(point.t - centerTime) <= replacementWindowSeconds);
    }
    for (let distance = 1; distance <= replacementRadius; distance += 1) {
      if (usable(source[index - distance])) return source[index - distance].v;
      if (usable(source[index + distance])) return source[index + distance].v;
    }
    return null;
  }

  let start = 0;
  while (start < output.length) {
    if (!Number.isFinite(output[start].v) || output[start].v < threshold) {
      start += 1;
      continue;
    }
    let end = start + 1;
    while (
      end < output.length
      && Number.isFinite(output[end].v)
      && output[end].v >= threshold
      && Number.isFinite(output[end].t)
      && Number.isFinite(output[end - 1].t)
      && output[end].t - output[end - 1].t <= maxGapSeconds
    ) {
      end += 1;
    }
    if (end - start < 2) {
      for (let index = start; index < end; index += 1) {
        const replacement = nearestNormalSample(index);
        if (Number.isFinite(replacement)) output[index].v = replacement;
      }
    }
    start = end;
  }
  return output.map((point) => point.v);
}

let compatibilitySeed = 0x8e9db24;
function compatibilityRandom() {
  compatibilitySeed = (Math.imul(compatibilitySeed, 1664525) + 1013904223) >>> 0;
  return compatibilitySeed / 0x100000000;
}

for (let fixture = 0; fixture < 500; fixture += 1) {
  let timestamp = 100;
  const values = Array.from({ length: 1 + Math.floor(compatibilityRandom() * 30) }, () => {
    timestamp += 1 + Math.floor(compatibilityRandom() * 6);
    const roll = compatibilityRandom();
    const value = roll < 0.28
      ? 0.02 + compatibilityRandom() * 0.2
      : compatibilityRandom() * 0.0199;
    return { t: timestamp, v: value };
  });
  assert.deepStrictEqual(
    displayValues(values),
    legacyReferenceValues(values),
    `legacy randomized compatibility fixture ${fixture} must match 8e9db24 behavior`
  );
}

assert.deepStrictEqual(
  Object.keys(buildInfrastructurePingPresentation(series([{ t: 100, v: 0.002 }]))),
  ['rawLatencySeries', 'displayLatencySeries'],
  'legacy array input temporarily retains its complete production contract'
);

console.log('bigscreen ping transform tests passed');
