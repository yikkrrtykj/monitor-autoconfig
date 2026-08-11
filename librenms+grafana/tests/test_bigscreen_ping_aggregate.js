const assert = require('assert');
const {
  aggregateInfrastructurePingTrend,
  buildInfrastructurePingTrend,
  formatPingText,
  splitPointsOnGaps
} = require('../bigscreen/utils.js');
const {
  activeInfraPingQuery,
  activeInfrastructurePingTargetKeys,
  currentInfrastructurePingTargetKeys,
  deployedInfrastructurePingTargetKeys
} = require('../bigscreen/api.js');

const START = 1000;
const STEP = 2;

function target(name, values, metric = {}) {
  return {
    name,
    metric: {
      job: 'infra-dist-ping',
      target_ip: name,
      instance: name,
      ...metric
    },
    values
  };
}

function targets(count, valuesForIndex) {
  return Array.from({ length: count }, (_, index) => (
    target(`switch-${index + 1}`, valuesForIndex(index))
  ));
}

function constantValues(value, samples = 12) {
  return Array.from({ length: samples }, (_, index) => ({
    t: START + index * STEP,
    v: value
  }));
}

function seriesTargetKey(series) {
  return `${series.metric.job}|${series.metric.target_ip || series.metric.instance}`;
}

function expectedKeysFor(series, expected) {
  if (expected instanceof Set) return expected;
  const keys = new Set(series.map(seriesTargetKey));
  const count = Number.isFinite(expected) ? expected : series.length;
  for (let index = keys.size; index < count; index += 1) {
    keys.add(`infra-dist-ping|missing-${index + 1}`);
  }
  return keys;
}

function aggregate(series, expected = series.length, extra = {}) {
  return aggregateInfrastructurePingTrend(series, {
    expectedTargetKeys: expectedKeysFor(series, expected),
    stepSeconds: STEP,
    alignmentToleranceSeconds: 3,
    ...extra
  });
}

function valueAt(result, timestamp) {
  const point = result.series[0] && result.series[0].values.find((item) => item.t === timestamp);
  assert.ok(point, `missing aggregate point at ${timestamp}`);
  return point.v;
}

function coverageAt(result, timestamp) {
  const point = result.coverage.find((item) => item.t === timestamp);
  assert.ok(point, `missing coverage point at ${timestamp}`);
  return point;
}

const baselineTargets = targets(20, () => constantValues(0.001));
const baselineBefore = JSON.parse(JSON.stringify(baselineTargets));
const baselineAggregate = aggregate(baselineTargets, 20);
assert.strictEqual(baselineAggregate.series.length, 1);
assert.strictEqual(baselineAggregate.series[0].name, '典型设备中位数');
assert.strictEqual(baselineAggregate.expectedTargets, 20);
assert.strictEqual(baselineAggregate.quorum, 11);
assert.ok(
  baselineAggregate.series[0].values.every((point) => point.v === 0.001),
  'all targets near 1 ms must produce an aggregate near 1 ms'
);
assert.deepStrictEqual(baselineTargets, baselineBefore, 'aggregation must not mutate raw series');
assert.deepStrictEqual(
  aggregate(baselineTargets, 20),
  baselineAggregate,
  'recomputing after a browser refresh must be deterministic and stateless'
);

const minorityWave = [0.001, 0.004, 0.007, 0.004, 0.001]
  .map((v, index) => ({ t: START + index * STEP, v }));
const minorityTargets = targets(20, (index) => (
  index < 2 ? minorityWave.map((point) => ({ ...point })) : constantValues(0.001, 5)
));
assert.ok(
  aggregate(minorityTargets, 20).series[0].values.every((point) => point.v === 0.001),
  'two control-plane waves among twenty targets must not become a network wave'
);

function assertNetworkWideEventVisible(durationSeconds) {
  const values = constantValues(0.001, 14).map((point) => ({
    ...point,
    v: point.t >= START + 4 && point.t < START + 4 + durationSeconds ? 0.010 : 0.001
  }));
  const result = aggregate(targets(20, () => values.map((point) => ({ ...point }))), 20);
  assert.strictEqual(
    valueAt(result, START + 4),
    0.010,
    `${durationSeconds}s network-wide latency must be visible at its first sample`
  );
}

[4, 10, 15].forEach(assertNetworkWideEventVisible);

const oneHugeSpike = targets(20, (index) => (
  index === 0 ? [{ t: START, v: 0.2 }] : [{ t: START, v: 0.001 }]
));
assert.strictEqual(
  valueAt(aggregate(oneHugeSpike, 20), START),
  0.001,
  'one target at 200 ms must not lift the representative trend'
);

const duplicateDisplayName = aggregate([
  target('shared-switch-name', [{ t: START, v: 0.001 }], {
    job: 'infra-core-ping',
    target_ip: '192.0.2.10'
  }),
  target('shared-switch-name', [{ t: START, v: 0.003 }], {
    job: 'infra-dist-ping',
    target_ip: '192.0.2.20'
  })
], 2);
assert.strictEqual(coverageAt(duplicateDisplayName, START).contributors, 2);
assert.strictEqual(
  valueAt(duplicateDisplayName, START),
  0.002,
  'different job|target_ip identities must remain contributors even when display names collide'
);

const currentIdentity = 'infra-dist-ping|192.0.2.30';
const sameNameRetiredHistory = aggregate([
  target('same-display-name', [{ t: START, v: 0.001 }], {
    job: 'infra-dist-ping',
    target_ip: '192.0.2.30',
    instance: 'same-instance'
  }),
  target('same-display-name', [{ t: START, v: 0.009 }], {
    job: 'infra-dist-ping',
    target_ip: '192.0.2.31',
    instance: 'same-instance'
  })
], new Set([currentIdentity]));
assert.strictEqual(coverageAt(sameNameRetiredHistory, START).contributors, 1);
assert.strictEqual(sameNameRetiredHistory.expectedTargets, 1);
assert.strictEqual(
  valueAt(sameNameRetiredHistory, START),
  0.001,
  'same-name retired history outside expectedTargetKeys must not contribute or affect the median'
);

const tenOfTwenty = aggregate(targets(10, () => [{ t: START, v: 0.001 }]), 20);
assert.deepStrictEqual(tenOfTwenty.series, [], '10/20 contributors must fail strict-majority quorum');
assert.deepStrictEqual(coverageAt(tenOfTwenty, START), {
  t: START,
  contributors: 10,
  expectedTargets: 20,
  quorum: 11,
  quorumMet: false
});

const elevenOfTwenty = aggregate(targets(11, () => [{ t: START, v: 0.001 }]), 20);
assert.strictEqual(valueAt(elevenOfTwenty, START), 0.001, '11/20 contributors must meet quorum');
assert.strictEqual(coverageAt(elevenOfTwenty, START).quorumMet, true);

const dropAndRecovery = targets(20, (index) => {
  const values = [{ t: START, v: 0.001 }, { t: START + 4, v: 0.001 }];
  if (index < 10) values.splice(1, 0, { t: START + 2, v: 0.001 });
  return values;
});
const recoveryResult = aggregate(dropAndRecovery, 20);
assert.strictEqual(coverageAt(recoveryResult, START).expectedTargets, 20);
assert.strictEqual(coverageAt(recoveryResult, START + 2).expectedTargets, 20);
assert.strictEqual(coverageAt(recoveryResult, START + 4).expectedTargets, 20);
assert.strictEqual(coverageAt(recoveryResult, START + 2).quorumMet, false);
assert.deepStrictEqual(
  splitPointsOnGaps(recoveryResult.series[0].values, 3).map((segment) => segment.map((point) => point.t)),
  [[START], [START + 4]],
  'one 2-second quorum hole must split the rendered aggregate line'
);

const skewedOnce = aggregate([
  target('grid', [
    { t: START, v: 0.001 },
    { t: START + 2, v: 0.001 },
    { t: START + 4, v: 0.001 }
  ]),
  target('skewed', [{ t: START + 1, v: 0.003 }])
], 2);
assert.strictEqual(valueAt(skewedOnce, START), 0.002, 'nearest off-grid sample may align once');
assert.strictEqual(coverageAt(skewedOnce, START + 2).contributors, 1);
assert.strictEqual(coverageAt(skewedOnce, START + 4).contributors, 1);
assert.strictEqual(
  skewedOnce.series[0].values.length,
  1,
  'alignment tolerance must not reuse one old sample at later timestamps'
);

const exactBeforeFallback = aggregate([
  target('grid', [
    { t: START, v: 0.001 },
    { t: START + 2, v: 0.001 }
  ]),
  target('mixed', [
    { t: START - 0.5, v: 0.009 },
    { t: START, v: 0.003 }
  ])
], 2);
assert.strictEqual(
  valueAt(exactBeforeFallback, START),
  0.002,
  'an exact timestamp must win before the nearest-sample fallback is considered'
);

const tournament = buildInfrastructurePingTrend([
  target('stage-switch-a', constantValues(0.001, 3)),
  target('stage-switch-b', constantValues(0.007, 3))
], { tournament: true });
assert.deepStrictEqual(
  tournament.series.map((series) => series.name),
  ['stage-switch-a', 'stage-switch-b'],
  'tournament mode must keep both paths as identifiable series'
);
assert.strictEqual(tournament.series[0].values[0].v, 0.001);
assert.strictEqual(tournament.series[1].values[0].v, 0.007);

assert.deepStrictEqual(
  aggregate([
    target('missing', []),
    target('failed-zero', [{ t: START, v: 0 }]),
    target('invalid', [{ t: Number.NaN, v: 0.001 }])
  ], 3),
  { series: [], coverage: [], expectedTargets: 3, quorum: 2 },
  'all targets without valid RTT data must preserve the no-data result'
);

assert.strictEqual(
  formatPingText(valueAt(aggregate([
    target('a', [{ t: START, v: 0.001 }]),
    target('b', [{ t: START, v: 0.003 }])
  ], 2), START)),
  '2.0 ms',
  'aggregation must keep RTT in seconds until the existing formatter converts it to milliseconds'
);

assert.ok(
  activeInfraPingQuery().includes('max by (instance, job, target_ip)'),
  'the deployed-target query must preserve stable target identity labels'
);
assert.deepStrictEqual(
  Array.from(activeInfrastructurePingTargetKeys([
    { metric: { job: 'infra-core-ping', target_ip: '192.0.2.1', instance: 'core' } },
    { metric: { job: 'infra-dist-ping', target_ip: '192.0.2.2', instance: 'dist' } },
    { metric: { job: 'infra-fw-ping', target_ip: '192.0.2.3', instance: 'fw' } },
    { metric: { job: 'infra-srv-ping', target_ip: '192.0.2.4', instance: 'server' } }
  ])).sort(),
  [
    'infra-core-ping|192.0.2.1',
    'infra-dist-ping|192.0.2.2',
    'infra-fw-ping|192.0.2.3'
  ],
  'expected targets come from stable deployed infrastructure identities, not timestamp contributors'
);

const historicalTargets = [
  { metric: { job: 'infra-core-ping', target_ip: '192.0.2.1', instance: 'core' } },
  { metric: { job: 'infra-dist-ping', target_ip: '192.0.2.2', instance: 'retired' } },
  { metric: { job: 'infra-fw-ping', target_ip: '192.0.2.3', instance: 'down' } }
];
const currentTargets = [
  {
    job: 'infra-core-ping', targetIp: '192.0.2.1', instance: 'core', success: true
  },
  {
    job: 'infra-fw-ping', targetIp: '192.0.2.3', instance: 'down', success: false
  },
  {
    job: 'infra-dist-ping', targetIp: '192.0.2.4', instance: 'never-online', success: false
  }
];
assert.deepStrictEqual(
  Array.from(currentInfrastructurePingTargetKeys(currentTargets)).sort(),
  [
    'infra-core-ping|192.0.2.1',
    'infra-dist-ping|192.0.2.4',
    'infra-fw-ping|192.0.2.3'
  ],
  'current configured targets remain present regardless of probe success'
);
assert.deepStrictEqual(
  Array.from(deployedInfrastructurePingTargetKeys(historicalTargets, currentTargets)).sort(),
  [
    'infra-core-ping|192.0.2.1',
    'infra-fw-ping|192.0.2.3'
  ],
  'retired history is removed, current down targets stay expected, and never-online placeholders stay excluded'
);

console.log('bigscreen aggregate ping trend tests passed');
