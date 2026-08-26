const assert = require('assert');
const pingTransform = require('../bigscreen/metrics/ping-transform.js');

assert.deepStrictEqual(
  Object.keys(pingTransform),
  ['buildInfrastructurePingPresentation'],
  'the module exposes only the presentation adapter'
);

const { buildInfrastructurePingPresentation } = pingTransform;

function series(values, name = 'switch-a', job = '') {
  const metric = { instance: name };
  if (job) metric.job = job;
  return [{ name, metric, values }];
}

function displayValues(values) {
  return buildInfrastructurePingPresentation(series(values))
    .displayLatencySeries[0].values.map((point) => point.v);
}

function successSeries(values, name = 'switch-a', job = '') {
  return series(values, name, job);
}

function buildV2(latencyValues, successValues, name = 'switch-a', job = '') {
  return buildInfrastructurePingPresentation({
    latencySeries: series(latencyValues, name, job),
    successSeries: successSeries(successValues, name, job)
  });
}

function v2Values(latencyValues, successValues, name = 'switch-a', job = '') {
  return buildV2(latencyValues, successValues, name, job)
    .displayLatencySeries[0].values;
}

function buildJobV2(job, latencyValues, successValues = onlineSuccessFor(latencyValues)) {
  return buildV2(latencyValues, successValues, 'switch-a', job);
}

function onlineSuccessFor(latencyValues) {
  return latencyValues.map((point) => ({ t: point.t, v: 1 }));
}

function timedValues(numbers, start = 0, step = 2) {
  return numbers.map((v, index) => ({ t: start + index * step, v }));
}

function warmBaseline(value = 0.002, start = 0, step = 2) {
  return timedValues(Array(6).fill(value), start, step);
}

function outputNumbers(latencyValues, successValues = onlineSuccessFor(latencyValues)) {
  return v2Values(latencyValues, successValues).map((point) => point.v);
}

function assertNumbers(actual, expected, message, epsilon = 1e-12) {
  assert.strictEqual(actual.length, expected.length, message);
  actual.forEach((value, index) => {
    if (value === null || expected[index] === null) {
      assert.strictEqual(value, expected[index], `${message} at index ${index}`);
      return;
    }
    assert.ok(
      Math.abs(value - expected[index]) <= epsilon,
      `${message} at index ${index}: expected ${expected[index]}, got ${value}`
    );
  });
}

// The dormant array-input adapter intentionally retains its legacy behavior.
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
  'legacy normal latency samples remain unchanged'
);

assert.deepStrictEqual(
  displayValues([
    { t: 100, v: 0.003 },
    { t: 102, v: 0.08 },
    { t: 108, v: 0.09 },
    { t: 110, v: 0.005 }
  ]),
  [0.003, 0.003, 0.005, 0.005],
  'legacy high samples separated by more than three seconds remain independent'
);

const legacyIncident = [
  { t: 100, v: 0.002 },
  { t: 102, v: 0.06 },
  { t: 104, v: 0.2 },
  { t: 106, v: 0.003 }
];
assert.deepStrictEqual(
  displayValues(legacyIncident),
  legacyIncident.map((point) => point.v),
  'legacy sustained high-latency incidents remain unchanged'
);

assert.deepStrictEqual(
  displayValues([
    { t: 100, v: 0.2 },
    { t: 102, v: 0.004 }
  ]),
  [0.004, 0.004],
  'the following normal sample remains available only to the legacy path'
);

assert.deepStrictEqual(
  displayValues([{ t: 100, v: 0.2 }]),
  [0.2],
  'legacy isolated spikes remain raw when no normal replacement exists'
);

assert.deepStrictEqual(
  displayValues([
    { t: 100, v: 0.003 },
    { t: 115, v: 0.2 }
  ]),
  [0.003, 0.003],
  'legacy replacement exactly fifteen seconds away remains usable'
);

assert.deepStrictEqual(
  displayValues([
    { t: 100, v: 0.003 },
    { t: 116, v: 0.2 }
  ]),
  [0.003, 0.2],
  'legacy replacement more than fifteen seconds away remains rejected'
);

assert.deepStrictEqual(
  displayValues([
    { t: 100, v: 0.2 },
    { t: 102, v: 0 }
  ]),
  [0, 0],
  'legacy zero remains a usable normal replacement'
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

const multipleLegacy = buildInfrastructurePingPresentation([
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
  multipleLegacy.displayLatencySeries.map((item) => item.values.map((point) => point.v)),
  [[0.002, 0.002, 0.004], [0.006, 0.007]],
  'legacy series remain transformed independently'
);

// Production object-input success-aware presentation.
const stableTwoMilliseconds = warmBaseline();
const normalV2 = buildV2(stableTwoMilliseconds, onlineSuccessFor(stableTwoMilliseconds));
assert.deepStrictEqual(
  Object.keys(normalV2),
  ['displayLatencySeries'],
  'the success-aware object API exposes presentation data only'
);
assert.deepStrictEqual(
  normalV2.displayLatencySeries[0].values.map((point) => point.v),
  Array(6).fill(0.002),
  'stable 2 ms RTT remains stable through causal smoothing'
);
assert.strictEqual(normalV2.displayLatencySeries[0].currentStatus, 'online');
assert.ok(!Object.prototype.hasOwnProperty.call(normalV2, 'rawLatencySeries'));
assert.strictEqual(normalV2.displayLatencySeries[0].presentationMode, 'latency');

const managementNormal = timedValues([0.001, 0.0014, 0.0018, 0.0022, 0.0026, 0.003]);
const managementNormalOutput = buildJobV2('infra-dist-ping', managementNormal);
const managementNormalValues = managementNormalOutput.displayLatencySeries[0].values;
assert.strictEqual(
  managementNormalOutput.displayLatencySeries[0].presentationMode,
  'management-rtt'
);
assert.deepStrictEqual(managementNormalValues.slice(0, 2), [
  { t: 0, v: null, status: 'warming' },
  { t: 2, v: null, status: 'warming' }
], 'management RTT uses a two-sample visual warm-up without inventing latency');
assert.ok(
  managementNormalValues.slice(2).every((point) => (
    Number.isFinite(point.v) && point.v >= 0.001 && point.v <= 0.003 && !point.status
  )),
  'management RTT becomes a finite millisecond trend after causal warm-up'
);
assert.ok(
  managementNormalValues.at(-1).v > managementNormalValues[2].v,
  'a normal 1-to-3 ms baseline trend remains visible at low amplitude'
);

const managementStableBaseline = timedValues(Array(31).fill(0.002));
const managementSlopeArtifact = [
  0.00926, 0.00856, 0.0073, 0.00667, 0.00532,
  0.00469, 0.00312, 0.00282, 0.00143
];
const managementSlope = [
  ...managementStableBaseline,
  ...timedValues(managementSlopeArtifact, 62)
];
const managementSlopeOutput = buildJobV2('infra-core-ping', managementSlope)
  .displayLatencySeries[0].values.map((point) => point.v);
assert.ok(
  Math.max(...managementSlopeOutput.slice(-managementSlopeArtifact.length)) <= 0.002,
  'the trailing low quantile removes the 9-to-1 ms management scheduling staircase'
);

const managementCpuSpikes = timedValues([0.002, 0.002, 0.15, 0.002, 0.002]);
assert.deepStrictEqual(
  buildJobV2('infra-dist-ping', managementCpuSpikes)
    .displayLatencySeries[0].values.map((point) => point.v),
  [null, null, 0.002, 0.002, 0.002],
  'an isolated 150 ms management CPU spike stays near the measured network floor'
);

const managementColdStartSpike = buildJobV2(
  'infra-dist-ping',
  timedValues([0.15, 0.002, 0.002, 0.002])
).displayLatencySeries[0];
assert.deepStrictEqual(managementColdStartSpike.values, [
  { t: 0, v: null, status: 'warming' },
  { t: 2, v: null, status: 'warming' },
  { t: 4, v: 0.002 },
  { t: 6, v: 0.002 }
], 'a cold-start 150 ms sample remains a warm-up gap instead of creating a decaying spike');
assert.strictEqual(managementColdStartSpike.currentStatus, 'online');

const firewallManagementOutput = buildJobV2('infra-fw-ping', managementCpuSpikes)
  .displayLatencySeries[0];
assert.strictEqual(firewallManagementOutput.presentationMode, 'management-rtt');
assert.ok(
  firewallManagementOutput.values.slice(2).every((point) => Number.isFinite(point.v)),
  'the firewall device-local Ping retains filtered RTT instead of a reachability-only lane'
);

const managementDomBurst = timedValues([0.002, 0.038, 0.171, 0.106, 0.06, 0.005, 0.002]);
const managementDomBurstOutput = buildJobV2('infra-dist-ping', managementDomBurst)
  .displayLatencySeries[0].values.map((point) => point.v);
assert.ok(
  Math.max(...managementDomBurstOutput) <= 0.0035,
  'a DOM-triggered 38-171 ms burst remains close to the causal low RTT estimate'
);

const managementRegimeShift = [
  ...timedValues(Array(31).fill(0.002)),
  ...timedValues(Array(31).fill(0.012), 62)
];
const managementRegimeShiftOutput = buildJobV2('infra-dist-ping', managementRegimeShift)
  .displayLatencySeries[0].values.map((point) => point.v);
const highRegimeOutput = managementRegimeShiftOutput.slice(31);
assert.deepStrictEqual(
  highRegimeOutput.slice(0, 12),
  Array(12).fill(0.002),
  'the 30-second low-quantile window does not mistake the first positive delay samples for a new baseline'
);
assert.ok(
  highRegimeOutput[12] > 0.002,
  'a sustained 12 ms regime begins to appear causally after 24 seconds'
);
assert.ok(
  highRegimeOutput.at(-1) > 0.0119 && highRegimeOutput.at(-1) <= 0.012,
  'a genuine 60-second 12 ms baseline shift converges to the new RTT instead of staying pinned at 2 ms'
);

const majorityHighRegime = [
  ...timedValues(Array(31).fill(0.002)),
  ...timedValues(
    Array.from({ length: 31 }, (_, index) => [0.012, 0.012, 0.012, 0.012, 0.002][index % 5]),
    62
  )
];
const majorityHighOutput = buildJobV2('infra-dist-ping', majorityHighRegime)
  .displayLatencySeries[0].values.map((point) => point.v).slice(31);
assert.ok(
  majorityHighOutput.at(-1) > 0.009 && majorityHighOutput.at(-1) < 0.012,
  'an 80%-high/20%-low regime remains clearly elevated after 60 seconds'
);
assert.ok(
  Math.max(...majorityHighOutput) > 0.011,
  'occasional low samples cannot permanently pin a majority-high regime to the old 2 ms floor'
);

const managementTimestampExpiry = [
  { t: 0, v: 0.002 },
  { t: 2, v: 0.002 },
  { t: 4, v: 0.002 },
  { t: 40, v: 0.012 },
  { t: 42, v: 0.012 },
  { t: 44, v: 0.012 }
];
assert.deepStrictEqual(buildJobV2('infra-dist-ping', managementTimestampExpiry)
  .displayLatencySeries[0].values, [
  { t: 0, v: null, status: 'warming' },
  { t: 2, v: null, status: 'warming' },
  { t: 4, v: 0.002 },
  { t: 40, v: null, status: 'warming' },
  { t: 42, v: null, status: 'warming' },
  { t: 44, v: 0.012 }
],
  'the management window expires by real timestamps and does not retain stale low samples or EMA state'
);

const managementRecovery = [
  ...timedValues(Array(31).fill(0.012)),
  ...timedValues(Array(31).fill(0.002), 62)
];
const managementRecoveryOutput = buildJobV2('infra-core-ping', managementRecovery)
  .displayLatencySeries[0].values.map((point) => point.v).slice(31);
assert.ok(
  managementRecoveryOutput.at(-1) >= 0.002 && managementRecoveryOutput.at(-1) < 0.0021,
  'a sustained recovery from 12 ms converges back to the 2 ms network floor'
);

const managementFailureLatency = timedValues([
  0.002, 0.002, 0.002, 0.002, 0.15, 0.002, 0.002, 0.002
], 100);
const managementFailureSuccess = onlineSuccessFor(managementFailureLatency);
managementFailureSuccess[3].v = 0;
const managementFailure = buildJobV2(
  'infra-dist-ping',
  managementFailureLatency,
  managementFailureSuccess
);
assert.deepStrictEqual(managementFailure.displayLatencySeries[0].values, [
  { t: 100, v: null, status: 'warming' },
  { t: 102, v: null, status: 'warming' },
  { t: 104, v: 0.002 },
  { t: 106, v: null, status: 'failure' },
  { t: 108, v: null, status: 'warming' },
  { t: 110, v: null, status: 'warming' },
  { t: 112, v: 0.002 },
  { t: 114, v: 0.002 }
], 'failure recovery suppresses a first-sample 150 ms spike with a fresh causal warm-up');
assert.strictEqual(managementFailure.displayLatencySeries[0].currentStatus, 'online');

const managementMissingRtt = buildInfrastructurePingPresentation({
  latencySeries: series([{ t: 100, v: 0.002 }], 'switch-a', 'infra-core-ping'),
  successSeries: successSeries([
    { t: 100, v: 1 },
    { t: 102, v: 1 }
  ], 'switch-a', 'infra-core-ping')
});
assert.deepStrictEqual(managementMissingRtt.displayLatencySeries[0].values, [
  { t: 100, v: null, status: 'warming' },
  { t: 102, v: null, status: 'unknown' }
]);
assert.strictEqual(
  managementMissingRtt.displayLatencySeries[0].currentStatus,
  'unknown',
  'management success without finite RTT remains unknown rather than online'
);

const managementMissingSuccess = buildV2(
  [{ t: 100, v: 0.002 }],
  [],
  'switch-a',
  'infra-core-ping'
);
assert.deepStrictEqual(managementMissingSuccess.displayLatencySeries[0].values, [
  { t: 100, v: null, status: 'unknown' }
]);
assert.strictEqual(
  managementMissingSuccess.displayLatencySeries[0].currentStatus,
  'unknown',
  'finite management RTT without probe_success remains unknown rather than online'
);

const managementInvalidRtt = buildV2(
  [{ t: 100, v: Number.NaN }],
  [{ t: 100, v: 1 }],
  'switch-a',
  'infra-dist-ping'
);
assert.deepStrictEqual(managementInvalidRtt.displayLatencySeries[0].values, [
  { t: 100, v: null, status: 'unknown' }
]);
assert.strictEqual(
  managementInvalidRtt.displayLatencySeries[0].currentStatus,
  'unknown',
  'management success with invalid RTT remains unknown rather than online'
);

const managementAllFailure = buildInfrastructurePingPresentation({
  latencySeries: [],
  successSeries: successSeries([
    { t: 100, v: 0 },
    { t: 102, v: 0 }
  ], 'switch-a', 'infra-dist-ping')
});
assert.strictEqual(
  managementAllFailure.displayLatencySeries[0].presentationMode,
  'management-rtt'
);
assert.deepStrictEqual(managementAllFailure.displayLatencySeries[0].values, [
  { t: 100, v: null, status: 'failure' }
]);
assert.strictEqual(managementAllFailure.displayLatencySeries[0].currentStatus, 'offline');

for (let length = 1; length <= managementSlope.length; length += 1) {
  assert.deepStrictEqual(
    buildJobV2('infra-core-ping', managementSlope.slice(0, length))
      .displayLatencySeries[0].values,
    buildJobV2('infra-core-ping', managementSlope)
      .displayLatencySeries[0].values.slice(0, length),
    `management RTT low-quantile presentation remains prefix-causal at length ${length}`
  );
}

const realLatencyProbe = [
  ...warmBaseline(),
  { t: 12, v: 0.1 },
  { t: 14, v: 0.11 },
  { t: 16, v: 0.12 },
  { t: 18, v: 0.13 }
];
for (const job of ['infra-srv-ping', 'infra-isp-ping', 'business-latency-probe']) {
  const latencyOutput = buildJobV2(job, realLatencyProbe).displayLatencySeries[0];
  assert.strictEqual(latencyOutput.presentationMode, 'latency');
  assert.deepStrictEqual(
    latencyOutput.values.slice(-2).map((point) => point.v),
    [0.12, 0.13],
    `${job} retains the latency path and exposes confirmed real high latency`
  );
}

const mixedJobMetadata = buildInfrastructurePingPresentation({
  latencySeries: series(realLatencyProbe, 'shared-target', 'infra-core-ping'),
  successSeries: successSeries(
    onlineSuccessFor(realLatencyProbe),
    'shared-target',
    'infra-srv-ping'
  )
});
assert.strictEqual(
  mixedJobMetadata.displayLatencySeries[0].presentationMode,
  'latency',
  'missing or conflicting job metadata defaults conservatively to real latency presentation'
);

const stableOneMillisecond = warmBaseline(0.001);
assert.deepStrictEqual(
  outputNumbers(stableOneMillisecond),
  Array(6).fill(0.001),
  'a stable 1 ms server is not over-smoothed or shifted'
);

const combJitter = timedValues([0.002, 0.003, 0.006, 0.002, 0.007, 0.003, 0.008, 0.002, 0.006, 0.003]);
assertNumbers(
  outputNumbers(combJitter),
  [0.002, 0.00225, 0.002625, 0.0028125, 0.00440625, 0.003703125,
    0.0053515625, 0.00417578125, 0.005087890625, 0.0040439453125],
  '2-8 ms comb jitter is reduced by trailing median plus EMA'
);

const singleTwenty = [...warmBaseline(), { t: 12, v: 0.02 }];
assert.deepStrictEqual(
  outputNumbers(singleTwenty),
  Array(7).fill(0.002),
  'a single 20 ms candidate is replaced from the past stable baseline'
);

const singleExtreme = [...warmBaseline(), { t: 12, v: 0.1 }];
assert.deepStrictEqual(
  outputNumbers(singleExtreme),
  Array(7).fill(0.002),
  'a single 100 ms candidate is suppressed without creating a high platform'
);

const threeSecondBurst = [
  ...warmBaseline(),
  { t: 12, v: 0.02 },
  { t: 15, v: 0.1 }
];
assert.deepStrictEqual(
  outputNumbers(threeSecondBurst),
  Array(8).fill(0.002),
  'a continuous three-second candidate burst remains provisional and suppressed'
);

const justUnderFourSeconds = [
  ...warmBaseline(),
  { t: 12, v: 0.012 },
  { t: 13.999, v: 0.015 },
  { t: 15.999, v: 0.018 }
];
assert.deepStrictEqual(
  outputNumbers(justUnderFourSeconds),
  Array(9).fill(0.002),
  'a continuous candidate run spanning 3.999 seconds remains provisional'
);

const exactPersistentRun = [
  ...warmBaseline(),
  { t: 12, v: 0.012 },
  { t: 14, v: 0.015 },
  { t: 16, v: 0.018 },
  { t: 18, v: 0.022 }
];
const exactPersistentOutput = outputNumbers(exactPersistentRun);
assertNumbers(
  exactPersistentOutput,
  [...Array(6).fill(0.002), 0.002, 0.002, 0.018, 0.022],
  'the four-second confirmation point and later persistent points stay raw'
);
assert.deepStrictEqual(
  exactPersistentOutput.slice(6, 8),
  [0.002, 0.002],
  'persistent confirmation never rewrites provisional history'
);

const persistentRecovery = [
  ...exactPersistentRun,
  { t: 20, v: 0.004 },
  { t: 22, v: 0.004 }
];
assertNumbers(
  outputNumbers(persistentRecovery).slice(-4),
  [0.018, 0.022, 0.004, 0.004],
  'persistent raw bypasses EMA and recovery reinitializes smoothing without a high tail'
);

const priorThresholdBaseline = timedValues([0.002, 0.002, 0.002, 0.007, 0.007, 0.007]);
const madThresholdSequence = [
  ...priorThresholdBaseline,
  { t: 12, v: 0.02 },
  { t: 14, v: 0.04 }
];
assertNumbers(
  outputNumbers(madThresholdSequence).slice(-2),
  [0.006375, 0.0131875],
  'scaled MAD raises the dynamic threshold above the floor and median multiplier'
);

const priorThresholdSequence = [
  ...priorThresholdBaseline,
  { t: 12, v: 0.04 },
  { t: 14, v: 0.05 }
];
assertNumbers(
  outputNumbers(priorThresholdSequence).slice(-2),
  [0.006375, 0.0066875],
  'threshold uses only the prior baseline and candidates never contaminate it'
);

const thresholdEquality = [
  ...warmBaseline(),
  { t: 12, v: 0.008 },
  { t: 14, v: 0.02 }
];
assertNumbers(
  outputNumbers(thresholdEquality).slice(-2),
  [0.002, 0.0035],
  'raw RTT equal to the 8 ms threshold is stable and enters the raw baseline'
);

const warmupBoundary = [
  ...timedValues(Array(5).fill(0.002)),
  { t: 10, v: 0.1 },
  { t: 12, v: 0.1 },
  { t: 14, v: 0.1 }
];
assertNumbers(
  outputNumbers(warmupBoundary).slice(-3),
  [0.002, 0.0265, 0.03875],
  'the sixth point remains warmup and adaptive candidate detection starts at the seventh'
);

const expiredBaseline = [
  ...warmBaseline(),
  { t: 71, v: 0.1 },
  { t: 73, v: 0.1 }
];
assertNumbers(
  outputNumbers(expiredBaseline).slice(-2),
  [0.002, 0.051],
  'baseline points expire by their real 60-second timestamps and warmup restarts'
);

const provisionalBaselineExpiry = [
  ...[-60, -59, -57, -55, -53, -51].map((t) => ({ t, v: 0.002 })),
  { t: 0, v: 0.02 },
  { t: 2, v: 0.02 },
  { t: 4, v: 0.02 }
];
assertNumbers(
  outputNumbers(provisionalBaselineExpiry).slice(-3),
  [0.002, 0.002, 0.011],
  'a provisional run ends and returns to warmup when baseline expiry leaves fewer than six points'
);

const longPersistentBaseline = warmBaseline();
const longPersistentCandidates = [
  { t: 12, v: 0.012 },
  { t: 14, v: 0.015 },
  { t: 16, v: 0.018 },
  ...Array.from({ length: 29 }, (_, index) => ({ t: 18 + index * 2, v: 0.02 }))
];
const longPersistentRecovery = [
  ...longPersistentBaseline,
  ...longPersistentCandidates,
  { t: 76, v: 0.002 },
  { t: 78, v: 0.1 }
];
const longPersistentOutput = outputNumbers(longPersistentRecovery);
assert.deepStrictEqual(
  longPersistentOutput.slice(8, -2),
  longPersistentCandidates.slice(2).map((point) => point.v),
  'a confirmed persistent run stays raw after every original baseline point expires'
);
assertNumbers(
  longPersistentOutput.slice(-2),
  [0.002, 0.0265],
  'persistent recovery resets smoothing and cached threshold before warmup restarts'
);

const maximumContinuousGap = [
  ...warmBaseline(),
  { t: 12, v: 0.012 },
  { t: 15, v: 0.015 },
  { t: 16, v: 0.018 }
];
assertNumbers(
  outputNumbers(maximumContinuousGap).slice(-3),
  [0.002, 0.002, 0.018],
  'a candidate gap equal to 1.5 nominal steps remains continuous'
);

const brokenCandidateRun = [
  ...warmBaseline(),
  { t: 12, v: 0.012 },
  { t: 15.001, v: 0.015 },
  { t: 17, v: 0.018 }
];
assert.deepStrictEqual(
  outputNumbers(brokenCandidateRun),
  Array(9).fill(0.002),
  'a candidate gap above 1.5 nominal steps starts a new provisional run'
);

const inferredOneSecondCadence = [
  ...warmBaseline(0.002, 0, 1),
  { t: 6, v: 0.012 },
  { t: 8, v: 0.015 },
  { t: 10, v: 0.018 }
];
assert.deepStrictEqual(
  outputNumbers(inferredOneSecondCadence),
  Array(9).fill(0.002),
  'nominal step inferred from timestamps prevents two-second gaps joining a one-second run'
);

const candidateCadenceChange = [
  ...warmBaseline(),
  { t: 12, v: 0.012 },
  { t: 13, v: 0.015 },
  { t: 14, v: 0.018 },
  { t: 15, v: 0.022 },
  { t: 16, v: 0.025 },
  { t: 18, v: 0.027 }
];
assert.strictEqual(
  outputNumbers(candidateCadenceChange).at(-1),
  0.002,
  'candidate timestamps participate in cadence inference and split the later two-second gap'
);

const prefixCandidate = [...warmBaseline(), { t: 12, v: 0.012 }, { t: 14, v: 0.015 }];
assert.deepStrictEqual(
  outputNumbers(prefixCandidate),
  exactPersistentOutput.slice(0, prefixCandidate.length),
  'adding a future confirmation point leaves the earlier presentation prefix unchanged'
);

const failureResetLatency = [
  ...warmBaseline(),
  { t: 12, v: 0.1 },
  { t: 14, v: 0 },
  { t: 16, v: 0.1 }
];
const failureResetSuccess = onlineSuccessFor(failureResetLatency);
failureResetSuccess[7].v = 0;
const failureReset = buildV2(failureResetLatency, failureResetSuccess);
assert.deepStrictEqual(failureReset.displayLatencySeries[0].values.slice(-3), [
  { t: 12, v: 0.002 },
  { t: 14, v: null, status: 'failure' },
  { t: 16, v: 0.1 }
], 'failure clears baseline, candidate run, cadence and smoothing before recovery warmup');
assert.strictEqual(failureReset.displayLatencySeries[0].currentStatus, 'online');

const unknownResetLatency = [
  ...warmBaseline(),
  { t: 12, v: 0.1 },
  { t: 14, v: 0.1 },
  { t: 16, v: 0.1 }
];
const unknownResetSuccess = onlineSuccessFor(unknownResetLatency)
  .filter((point) => point.t !== 14);
const unknownReset = buildV2(unknownResetLatency, unknownResetSuccess);
assert.deepStrictEqual(unknownReset.displayLatencySeries[0].values.slice(-3), [
  { t: 12, v: 0.002 },
  { t: 14, v: null, status: 'unknown' },
  { t: 16, v: 0.1 }
], 'unknown evidence clears all presentation state and recovery starts warmup');

const onlineWithoutRtt = buildInfrastructurePingPresentation({
  latencySeries: [],
  successSeries: successSeries([{ t: 100, v: 1 }])
});
assert.deepStrictEqual(onlineWithoutRtt.displayLatencySeries[0].values, [
  { t: 100, v: null, status: 'unknown' }
]);
assert.strictEqual(
  onlineWithoutRtt.displayLatencySeries[0].currentStatus,
  'unknown',
  'success without a finite RTT is unknown rather than online'
);

const repeatedFailures = Array.from({ length: 450 }, (_, index) => ({
  t: 100 + index * 2,
  v: 0
}));
const allFailurePresentation = buildInfrastructurePingPresentation({
  latencySeries: [],
  successSeries: successSeries(repeatedFailures)
});
assert.deepStrictEqual(allFailurePresentation.displayLatencySeries[0].values, [
  { t: 100, v: null, status: 'failure' }
], 'an all-failure series remains present with one compressed marker');
assert.strictEqual(allFailurePresentation.displayLatencySeries[0].currentStatus, 'offline');

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
  'newer unknown evidence replaces stale offline status'
);

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
], 'RTT and success are aligned by timestamp rather than array index');
assert.strictEqual(unequalLengths.displayLatencySeries[0].currentStatus, 'online');

const multiSeriesInput = {
  latencySeries: [
    ...series(warmBaseline(0.001), 'switch-a'),
    ...series([...warmBaseline(0.002), { t: 12, v: 0.1 }], 'switch-b')
  ],
  successSeries: [
    ...successSeries(onlineSuccessFor(warmBaseline(0.001)), 'switch-a'),
    ...successSeries(onlineSuccessFor([...warmBaseline(0.002), { t: 12, v: 0.1 }]), 'switch-b')
  ]
};
const multiSeriesOutput = buildInfrastructurePingPresentation(multiSeriesInput);
assert.deepStrictEqual(
  multiSeriesOutput.displayLatencySeries.map((item) => item.values.map((point) => point.v)),
  [Array(6).fill(0.001), Array(7).fill(0.002)],
  'adaptive and smoothing state remains independent per device'
);

const orderedMultiSeriesInput = {
  latencySeries: [
    ...series([{ t: 100, v: 0.002 }, { t: 102, v: 0.003 }], 'switch-a'),
    ...series([{ t: 100, v: 0.006 }, { t: 102, v: 0.007 }], 'switch-b')
  ],
  successSeries: [
    ...successSeries([{ t: 100, v: 1 }, { t: 102, v: 1 }], 'switch-a'),
    ...successSeries([{ t: 100, v: 1 }, { t: 102, v: 1 }], 'switch-b')
  ]
};
const shuffledMultiSeriesInput = {
  latencySeries: [
    ...series([{ t: 102, v: 0.003 }, { t: 100, v: 0.002 }], 'switch-a'),
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
    { t: 102, v: 0.08, note: 'warmup' }
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

const fullWindowLatency = Array.from({ length: 450 }, (_, index) => {
  let v = [0.002, 0.003, 0.006, 0.002, 0.007, 0.003][index % 6];
  if (index % 97 === 50) v = 0.1;
  if (index >= 200 && index <= 204) v = [0.012, 0.015, 0.018, 0.022, 0.025][index - 200];
  return { t: index * 2, v };
});
const fullWindowSuccess = onlineSuccessFor(fullWindowLatency);
const fullWindowOutput = v2Values(fullWindowLatency, fullWindowSuccess);
for (let length = 1; length <= fullWindowLatency.length; length += 1) {
  const prefixOutput = v2Values(
    fullWindowLatency.slice(0, length),
    fullWindowSuccess.slice(0, length)
  );
  assert.deepStrictEqual(
    prefixOutput,
    fullWindowOutput.slice(0, length),
    `15-minute full calculation remains prefix-causal at length ${length}`
  );
}

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
