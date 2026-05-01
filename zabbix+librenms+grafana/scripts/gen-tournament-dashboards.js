const fs = require('fs');

const layouts = {
  '2layer': {
    title: 'Tournament 64 (2 层)',
    uid: 'tournament-64-2layer',
    description: '64-Player Tournament — 2-layer stage (4-4 / 4-4)',
    rows: [
      { left: [9, 10, 11, 12], right: [13, 14, 15, 16] },
      { left: [1, 2, 3, 4],    right: [5, 6, 7, 8] },
    ],
  },
  '233': {
    title: 'Tournament 64 (3 层 233)',
    uid: 'tournament-64-233',
    description: '64-Player Tournament — 3-layer stage, bottom-up 2-3-3',
    rows: [
      { left: [11, 12, 13], right: [14, 15, 16] },
      { left: [5, 6, 7],    right: [8, 9, 10] },
      { left: [1, 2],       right: [3, 4] },
    ],
  },
  '332': {
    title: 'Tournament 64 (3 层 332)',
    uid: 'tournament-64-332',
    description: '64-Player Tournament — 3-layer stage, bottom-up 3-3-2',
    rows: [
      { left: [13, 14],     right: [15, 16] },
      { left: [7, 8, 9],    right: [10, 11, 12] },
      { left: [1, 2, 3],    right: [4, 5, 6] },
    ],
  },
};

const ROW_HEIGHT = 5;
const HEADER_H = 4;

function teamPanel(team, gridPos, idBase) {
  return {
    datasource: { type: 'prometheus', uid: 'prometheus' },
    fieldConfig: {
      defaults: {
        color: { mode: 'thresholds' },
        mappings: [],
        max: 0.1, min: 0,
        thresholds: {
          mode: 'absolute',
          steps: [
            { color: 'green', value: null },
            { color: 'yellow', value: 0.03 },
            { color: 'red', value: 0.06 },
          ],
        },
        unit: 's',
      },
      overrides: [],
    },
    gridPos,
    id: idBase + team,
    options: {
      displayMode: 'lcd',
      legend: { showLegend: false, displayMode: 'list', placement: 'bottom', calcs: [] },
      orientation: 'horizontal',
      reduceOptions: { calcs: ['lastNotNull'], fields: '', values: false },
      showUnfilled: true,
      valueMode: 'color',
    },
    pluginVersion: '12.1.1',
    targets: [{
      datasource: { type: 'prometheus', uid: 'prometheus' },
      editorMode: 'code',
      expr: 'probe_icmp_duration_seconds{role="player",team="' + team + '",network=~"$network",phase="rtt"}',
      format: 'table',
      instant: true,
      refId: 'A',
    }],
    transformations: [{
      id: 'organize',
      options: {
        excludeByName: {
          Time: true, job: true, network: true, phase: true, role: true,
          team: true, switch: true, instance: true, __name__: true,
        },
        indexByName: { seat: 0, Value: 1 },
        renameByName: { seat: 'S', Value: 'RTT' },
      },
    }],
    title: 'Team ' + team,
    type: 'bargauge',
  };
}

function statPanel(id, x, w, title, expr, color, thresholds) {
  return {
    datasource: { type: 'prometheus', uid: 'prometheus' },
    fieldConfig: {
      defaults: {
        color: { mode: 'thresholds' },
        mappings: [],
        thresholds: thresholds || { mode: 'absolute', steps: [{ color, value: null }] },
      },
      overrides: [],
    },
    gridPos: { h: HEADER_H, w, x, y: 0 },
    id,
    options: {
      colorMode: 'background',
      graphMode: 'area',
      justifyMode: 'center',
      orientation: 'auto',
      reduceOptions: { calcs: ['lastNotNull'], fields: '', values: false },
      textMode: 'auto',
    },
    pluginVersion: '12.1.1',
    targets: [{
      datasource: { type: 'prometheus', uid: 'prometheus' },
      editorMode: 'code',
      expr,
      refId: 'A',
    }],
    title,
    type: 'stat',
  };
}

function buildPanels(layout) {
  const panels = [];

  panels.push(statPanel(1, 0,  6, '在线',   'count(probe_success{role="player",network=~"$network"} == 1) or vector(0)', 'green'));
  panels.push(statPanel(2, 6,  6, '离线',   'count(probe_success{role="player",network=~"$network"} == 0) or vector(0)', 'green', {
    mode: 'absolute', steps: [{ color: 'green', value: null }, { color: 'red', value: 1 }],
  }));
  panels.push(statPanel(3, 12, 6, '高延迟', 'count(probe_icmp_duration_seconds{role="player",network=~"$network",phase="rtt"} > 0.03) or vector(0)', 'green', {
    mode: 'absolute', steps: [{ color: 'green', value: null }, { color: 'orange', value: 1 }],
  }));
  panels.push(statPanel(4, 18, 6, '总计',   'count(probe_success{role="player",network=~"$network"}) or vector(0)', 'blue'));

  let y = HEADER_H;
  let idBase = 100;
  for (const row of layout.rows) {
    const leftCount = row.left.length;
    const rightCount = row.right.length;
    const halfWidth = 12;
    const leftW = Math.floor(halfWidth / leftCount);
    const rightW = Math.floor(halfWidth / rightCount);

    let x = 0;
    for (const team of row.left) {
      panels.push(teamPanel(team, { h: ROW_HEIGHT, w: leftW, x, y }, idBase));
      x += leftW;
    }
    x = 12;
    for (const team of row.right) {
      panels.push(teamPanel(team, { h: ROW_HEIGHT, w: rightW, x, y }, idBase));
      x += rightW;
    }
    y += ROW_HEIGHT;
    idBase += 100;
  }

  panels.push({
    datasource: { type: 'prometheus', uid: 'prometheus' },
    fieldConfig: {
      defaults: {
        color: { mode: 'palette-classic' },
        custom: {
          axisBorderShow: false, axisCenteredZero: false, axisColorMode: 'text', axisPlacement: 'auto',
          fillOpacity: 10, gradientMode: 'none',
          hideFrom: { legend: false, tooltip: false, viz: false },
          lineWidth: 1, pointSize: 5,
          scaleDistribution: { type: 'linear' },
          showPoints: 'never', spanNulls: true,
        },
        mappings: [],
        thresholds: { mode: 'absolute', steps: [
          { color: 'green', value: null },
          { color: 'yellow', value: 0.03 },
          { color: 'red', value: 0.06 },
        ] },
        unit: 's',
      },
      overrides: [],
    },
    gridPos: { h: 7, w: 24, x: 0, y },
    id: 900,
    options: {
      legend: { calcs: ['mean', 'max'], displayMode: 'table', placement: 'right', showLegend: true },
      tooltip: { mode: 'multi', sort: 'none' },
    },
    pluginVersion: '12.1.1',
    targets: [{
      datasource: { type: 'prometheus', uid: 'prometheus' },
      editorMode: 'code',
      expr: 'avg by (team) (probe_icmp_duration_seconds{role="player",network=~"$network",phase="rtt"})',
      legendFormat: 'Team {{team}}',
      refId: 'A',
    }],
    title: '各队平均延迟趋势',
    type: 'timeseries',
  });

  return panels;
}

function buildDashboard(layout) {
  return {
    annotations: {
      list: [{
        builtIn: 1,
        datasource: { type: 'grafana', uid: '-- Grafana --' },
        enable: true, hide: true,
        iconColor: 'rgba(0, 211, 255, 1)',
        name: 'Annotations & Alerts',
        type: 'dashboard',
      }],
    },
    description: layout.description,
    editable: true,
    fiscalYearStartMonth: 0,
    graphTooltip: 0,
    id: null,
    links: [],
    panels: buildPanels(layout),
    refresh: '5s',
    schemaVersion: 41,
    tags: ['tournament', 'ping', 'player'],
    templating: {
      list: [{
        current: { text: '有线', value: 'wired' },
        hide: 0,
        label: '网络（默认有线，无线为备用）',
        name: 'network',
        options: [
          { selected: true, text: '有线', value: 'wired' },
          { selected: false, text: '无线', value: 'wireless' },
          { selected: false, text: '全部', value: '.*' },
        ],
        query: 'wired,wireless,.*',
        type: 'custom',
      }],
    },
    time: { from: 'now-15m', to: 'now' },
    timepicker: {},
    timezone: 'browser',
    title: layout.title,
    uid: layout.uid,
    version: 1,
  };
}

const outDir = process.argv[2];
for (const [key, layout] of Object.entries(layouts)) {
  const dash = buildDashboard(layout);
  const path = outDir + '/tournament-64-' + key + '.json';
  fs.writeFileSync(path, JSON.stringify(dash, null, 2) + '\n');
  console.log('wrote ' + path + ' (' + dash.panels.length + ' panels)');
}
