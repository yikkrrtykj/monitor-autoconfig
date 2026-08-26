const assert = require('assert');
const fs = require('fs');
const path = require('path');
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
  { name: 'core-1', job: 'infra-core-ping', success: [1, 1, 1, 1, 1, 1, 1] },
  { name: 'stage1', job: 'infra-dist-ping', success: [1, 1, 1, 1, 1, 1, 1] },
  { name: 'stage2', job: 'infra-dist-ping', success: [1, 1, 1, 0, 1, 1, 1] },
  { name: 'stage4', job: 'infra-dist-ping', success: [1, 1, 1] },
  { name: 'firewall', job: 'infra-fw-ping', success: [1, 1, 1, 1, 1, 1, 1] }
];
const managementTimes = [100, 102, 104, 106, 108, 110, 112];
const mixedManagementLatency = managementDefinitions.map((definition) => pingSeries(
  definition.name,
  definition.job,
  managementTimes.map((t, pointIndex) => ({
    t,
    v: [0.002, 0.002, 0.002, 0.15, 0.002, 0.002, 0.002][pointIndex]
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
  { t: 106, v: 0.004 },
  { t: 108, v: 0.003 },
  { t: 110, v: 0.004 },
  { t: 112, v: 0.003 }
]);
serverPresentation.presentationMode = 'latency';
const businessProbePresentation = pingSeries('business-probe', 'business-latency-probe', [
  { t: 100, v: 0.001 },
  { t: 102, v: 0.002 },
  { t: 104, v: 0.003 },
  { t: 106, v: 0.002 },
  { t: 108, v: 0.001 },
  { t: 110, v: 0.002 },
  { t: 112, v: 0.001 }
]);
businessProbePresentation.presentationMode = 'latency';
const realLatencyPresentation = [serverPresentation, businessProbePresentation];

function latencyDomainMax(seriesList, minimum = 0.005) {
  const latencyMaxima = seriesList
    .map((item) => lineSeriesStats(item.values).max)
    .filter((value) => Number.isFinite(value));
  return Math.max(minimum, ...latencyMaxima);
}

managementPresentation.forEach((item) => {
  assert.strictEqual(item.presentationMode, 'management-rtt');
  assert.ok(
    item.values.some((point) => Number.isFinite(point.v) && point.v > 0),
    'every online management series contributes a real non-zero RTT curve'
  );
  assert.ok(
    lineSeriesStats(item.values).max <= 0.002,
    'the management CPU spike does not dominate RTT statistics or the mixed Y-domain'
  );
});
assert.deepStrictEqual(
  Object.fromEntries(managementPresentation.map((item) => [item.name, item.currentStatus])),
  {
    'core-1': 'online',
    stage1: 'online',
    stage2: 'online',
    stage4: 'unknown',
    firewall: 'online'
  },
  'management RTT retains authoritative online/offline/unknown state alongside numeric history'
);
assert.strictEqual(
  latencyDomainMax(realLatencyPresentation),
  latencyDomainMax([...managementPresentation, ...realLatencyPresentation]),
  'filtered management RTT below the real server peak keeps the mixed chart scale unchanged'
);
assert.strictEqual(
  latencyDomainMax([...managementPresentation, ...realLatencyPresentation]),
  0.008,
  'the mixed chart Y-domain includes all finite presentation RTT values'
);
assert.strictEqual(
  roundUpToStep(latencyDomainMax([...managementPresentation, ...realLatencyPresentation]), 0.01),
  roundUpToStep(latencyDomainMax(realLatencyPresentation), 0.01),
  'filtered management RTT below the real-latency maximum cannot change the rounded axis scale'
);
assert.strictEqual(
  latencyDomainMax(managementPresentation),
  0.005,
  'an all-management RTT input keeps the existing 5 ms presentation floor'
);

const appSource = fs.readFileSync(path.join(__dirname, '..', 'bigscreen', 'app.js'), 'utf8');
const lineChartSource = fs.readFileSync(
  path.join(__dirname, '..', 'bigscreen', 'charts', 'line-chart.js'),
  'utf8'
);
const rendererSources = `${appSource}\n${lineChartSource}`;
assert.ok(!rendererSources.includes('MANAGEMENT_REACHABILITY_MODE'));
assert.ok(!rendererSources.includes('reachabilityLaneLayout'));
assert.ok(!rendererSources.includes('chart-reachability-line'));
assert.ok(!rendererSources.includes('chart-reachability-separator'));
assert.ok(!rendererSources.includes('axisPadTop: 48'));
assert.ok(!rendererSources.includes('{ currentStatus: "online", label: "ONLINE", value: null }'));
assert.ok(
  lineChartSource.includes('const segments = splitPointsOnGaps(item.values, options.breakGapSeconds);'),
  'all Ping policies use the ordinary millisecond line renderer'
);
assert.ok(!appSource.includes('function renderLineChart('), 'app.js no longer owns the shared renderer');
assert.ok(
  appSource.includes('const renderLineChart = createLineChartRenderer({'),
  'app.js wires the extracted shared renderer through explicit dependencies'
);
const managementFailureItem = managementPresentation.find((item) => item.name === 'stage2');
assert.deepStrictEqual(
  lineFailurePoints(managementFailureItem.values),
  [{ t: 106, v: null, status: 'failure' }],
  'management failure remains a gap with an explicit failure marker'
);
assert.deepStrictEqual(
  lineSeriesCurrentDisplay(
    managementPresentation.find((item) => item.name === 'core-1'),
    lineSeriesStats(managementPresentation.find((item) => item.name === 'core-1').values)
  ),
  { currentStatus: 'online', label: null, value: 0.002 },
  'an online management legend exposes the latest RTT rather than ONLINE or --'
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
