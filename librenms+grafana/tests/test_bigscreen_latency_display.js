const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const {
  roundUpToStep,
  linePathFromPoints,
  stepPathFromPoints,
  splitPointsOnGaps,
  lineSeriesStats,
  lineSeriesHasTimeline,
  lineSeriesCurrentDisplay,
  lineFailurePoints,
  seriesSignature
} = require('../bigscreen/utils.js');
const {
  buildInfrastructurePingPresentation
} = require('../bigscreen/metrics/ping-transform.js');

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

function pingSeries(name, job, values) {
  return { name, metric: { instance: name, job }, values };
}

const managementDefinitions = [
  { name: 'core-1', job: 'infra-core-ping', success: [1, 1, 1, 1] },
  { name: 'stage1', job: 'infra-dist-ping', success: [1, 1, 1, 1] },
  { name: 'stage2', job: 'infra-dist-ping', success: [1, 1, 1, 1] },
  { name: 'stage3', job: 'infra-dist-ping', success: [1, 1, 0, 0] },
  { name: 'stage4', job: 'infra-dist-ping', success: [1, 1] },
  { name: 'firewall', job: 'infra-fw-ping', success: [1, 1, 1, 1] }
];
const managementTimes = [100, 102, 104, 106];
const mixedManagementLatency = managementDefinitions.map((definition, definitionIndex) => pingSeries(
  definition.name,
  definition.job,
  managementTimes.map((t, pointIndex) => ({
    t,
    v: [0.002, 0.009, 0.1, 0.2][(definitionIndex + pointIndex) % 4]
  }))
));
const mixedManagementSuccess = managementDefinitions.map((definition) => pingSeries(
  definition.name,
  definition.job,
  definition.success.map((v, index) => ({ t: managementTimes[index], v }))
));
const managementPresentation = buildInfrastructurePingPresentation({
  latencySeries: mixedManagementLatency,
  successSeries: mixedManagementSuccess
}).displayLatencySeries;
const serverPresentation = pingSeries('server', 'infra-srv-ping', [
  { t: 100, v: 0.002 },
  { t: 102, v: 0.003 },
  { t: 104, v: 0.008 },
  { t: 106, v: 0.004 }
]);
serverPresentation.presentationMode = 'latency';
const businessProbePresentation = pingSeries('business-probe', 'business-latency-probe', [
  { t: 100, v: 0.001 },
  { t: 102, v: 0.002 },
  { t: 104, v: 0.003 },
  { t: 106, v: 0.002 }
]);
businessProbePresentation.presentationMode = 'latency';
const realLatencyPresentation = [serverPresentation, businessProbePresentation];

function latencyDomainMax(seriesList, minimum = 0.005) {
  const latencyMaxima = seriesList
    .filter((item) => item.presentationMode !== 'management-reachability')
    .map((item) => lineSeriesStats(item.values).max)
    .filter((value) => Number.isFinite(value));
  return Math.max(minimum, ...latencyMaxima);
}

managementPresentation.forEach((item) => {
  assert.deepStrictEqual(
    lineSeriesStats(item.values),
    { last: null, max: null, mean: null, min: null },
    'management reachability has no finite latency statistic or Y-domain input'
  );
  assert.ok(
    item.values.every((point) => point.v === null),
    'management reachability carries categorical points without a zero RTT placeholder'
  );
});
assert.deepStrictEqual(
  Object.fromEntries(managementPresentation.map((item) => [item.name, item.currentStatus])),
  {
    'core-1': 'online',
    stage1: 'online',
    stage2: 'online',
    stage3: 'offline',
    stage4: 'unknown',
    firewall: 'online'
  },
  'each management device retains its own authoritative online/offline/unknown state'
);
assert.strictEqual(
  latencyDomainMax(realLatencyPresentation),
  latencyDomainMax([...managementPresentation, ...realLatencyPresentation]),
  'management status series cannot change the mixed chart latency Y-domain'
);
assert.strictEqual(
  latencyDomainMax([...managementPresentation, ...realLatencyPresentation]),
  0.008,
  'the mixed chart Y-domain is determined by the real server/business latency only'
);
assert.strictEqual(
  roundUpToStep(latencyDomainMax([...managementPresentation, ...realLatencyPresentation]), 0.01),
  roundUpToStep(latencyDomainMax(realLatencyPresentation), 0.01),
  'management status series cannot change the rounded real-latency axis scale'
);
assert.strictEqual(
  latencyDomainMax(managementPresentation),
  0.005,
  'an all-management input has no measured latency maximum'
);

const appSource = fs.readFileSync(path.join(__dirname, '..', 'bigscreen', 'app.js'), 'utf8');
const laneFunctionStart = appSource.indexOf('  function reachabilityLaneLayout');
const laneFunctionEnd = appSource.indexOf('\n\n  function renderLineChart', laneFunctionStart);
assert.ok(laneFunctionStart >= 0 && laneFunctionEnd > laneFunctionStart, 'renderer exposes a local pure lane layout helper');
const reachabilityLaneLayout = vm.runInNewContext(
  `(${appSource.slice(laneFunctionStart, laneFunctionEnd).trim()})`
);
const sixLaneLayout = reachabilityLaneLayout(managementPresentation.length, 48);
assert.strictEqual(sixLaneLayout.positions.length, managementPresentation.length);
assert.strictEqual(
  new Set(sixLaneLayout.positions.map((value) => value.toFixed(6))).size,
  managementPresentation.length,
  'core, stage switches, and firewall receive independent status Y coordinates'
);
const laneGaps = sixLaneLayout.positions.slice(1).map((value, index) => (
  value - sixLaneLayout.positions[index]
));
assert.ok(
  laneGaps.every((gap) => gap > sixLaneLayout.strokeWidth),
  'adjacent online status strokes cannot cover each other'
);
assert.ok(
  laneGaps.every((gap) => gap > sixLaneLayout.markerArm * 2),
  'an online lane cannot cover the neighbouring device failure marker'
);
assert.ok(
  appSource.includes('axisPadTop: 48'),
  'the Ping chart reserves one fixed status band independent of management series count'
);

const statusHistoryValues = [
  { t: 100, v: 0.002 },
  { t: 102, v: 0.0087 },
  { t: 104, v: null, status: 'failure' }
];
const statusHistoryStats = lineSeriesStats(statusHistoryValues);
assert.deepStrictEqual(
  lineSeriesCurrentDisplay({ currentStatus: 'online' }, statusHistoryStats),
  { currentStatus: 'online', label: null, value: 0.0087 },
  'online legend state uses the latest finite RTT even when the final history point is a failure sentinel'
);
assert.deepStrictEqual(
  lineSeriesCurrentDisplay({ currentStatus: 'offline' }, statusHistoryStats),
  { currentStatus: 'offline', label: 'OFFLINE', value: null },
  'offline legend state comes only from the authoritative series currentStatus'
);
assert.strictEqual(statusHistoryStats.max, 0.0087, 'offline status preserves the finite historical maximum');
assert.deepStrictEqual(
  lineSeriesCurrentDisplay({ currentStatus: 'offline' }, lineSeriesStats([{ t: 100, v: 0 }])),
  { currentStatus: 'offline', label: 'OFFLINE', value: null },
  'offline legend state never presents a finite zero as zero milliseconds'
);
assert.deepStrictEqual(
  lineSeriesCurrentDisplay({ currentStatus: 'unknown' }, statusHistoryStats),
  { currentStatus: 'unknown', label: '--', value: null },
  'unknown legend state remains neutral instead of being inferred as offline'
);
assert.deepStrictEqual(
  lineSeriesCurrentDisplay({}, statusHistoryStats),
  { currentStatus: null, label: null, value: 0.0087 },
  'legacy series without currentStatus keep their existing latest finite value behavior'
);

const allFailureSeries = {
  name: 'switch-offline',
  currentStatus: 'offline',
  values: [{ t: 100, v: null, status: 'failure' }]
};
assert.strictEqual(lineSeriesHasTimeline(allFailureSeries), true, 'all-failure series remains eligible for chart and legend rendering');
assert.deepStrictEqual(
  lineSeriesStats(allFailureSeries.values),
  { last: null, max: null, mean: null, min: null },
  'offline status and failure sentinels do not enter latency statistics or Y-axis maximum inputs'
);
assert.deepStrictEqual(
  lineSeriesCurrentDisplay(allFailureSeries, lineSeriesStats(allFailureSeries.values)),
  { currentStatus: 'offline', label: 'OFFLINE', value: null },
  'an all-failure series still exposes OFFLINE while its historical maximum remains missing'
);
assert.deepStrictEqual(
  lineFailurePoints(allFailureSeries.values),
  [{ t: 100, v: null, status: 'failure' }],
  'the existing single failure marker remains available for an all-failure series'
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
const unchangedStatusPoints = [{ t: 100, v: 0.002 }];
const onlineSeriesSignature = seriesSignature([{ name: 'switch-a', currentStatus: 'online', values: unchangedStatusPoints }]);
const offlineSeriesSignature = seriesSignature([{ name: 'switch-a', currentStatus: 'offline', values: unchangedStatusPoints }]);
const unknownSeriesSignature = seriesSignature([{ name: 'switch-a', currentStatus: 'unknown', values: unchangedStatusPoints }]);
assert.notStrictEqual(onlineSeriesSignature, offlineSeriesSignature, 'online to offline invalidates the render cache without RTT changes');
assert.notStrictEqual(offlineSeriesSignature, unknownSeriesSignature, 'offline to unknown invalidates the render cache without RTT changes');
assert.notStrictEqual(offlineSeriesSignature, onlineSeriesSignature, 'offline to online invalidates the render cache without RTT changes');
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
