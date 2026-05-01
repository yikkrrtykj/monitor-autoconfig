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
    teamH: 8,
    dotsH: 3,
    withTrend: true,
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
    teamH: 7,
    dotsH: 2,
    withTrend: false,
  },
  '6teams': {
    title: 'Tournament 6 队',
    uid: 'tournament-6teams',
    description: '6-team tournament (works for any team size — 3-per-team / 4-per-team / etc., auto-detected from data)',
    rows: [
      { left: [1, 2, 3], right: [4, 5, 6] },
    ],
    teamH: 12,
    dotsH: 4,
    withTrend: true,
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
    teamH: 7,
    dotsH: 2,
    withTrend: false,
  },
};

const INFRA_H = 3;
const HEADER_H = 4;
const TREND_H = 5;
const DS = { type: 'prometheus', uid: 'prometheus' };

function infraStatPanel(id, x, w, title, jobRegex) {
  return {
    datasource: DS,
    fieldConfig: {
      defaults: {
        color: { mode: "thresholds" },
        mappings: [],
        thresholds: {
          mode: "absolute",
          steps: [
            { color: "red", value: null },
            { color: "green", value: 1 },
          ],
        },
        unit: "none",
        noValue: "—",
      },
      overrides: [],
    },
    gridPos: { h: INFRA_H, w, x, y: 0 },
    id,
    options: {
      colorMode: "background",
      graphMode: "none",
      justifyMode: "center",
      orientation: "auto",
      reduceOptions: { calcs: ["lastNotNull"], fields: "", values: true },
      textMode: "name",
      wideLayout: false,
    },
    pluginVersion: "12.1.1",
    targets: [{
      datasource: DS,
      editorMode: "code",
      expr: "probe_success{job=~\"" + jobRegex + "\"}",
      legendFormat: "{{instance}}",
      refId: "A",
    }],
    title,
    type: "stat",
  };
}

function dotsPanel(team, gridPos, id) {
  return {
    datasource: DS,
    fieldConfig: {
      defaults: {
        color: { mode: 'thresholds' },
        mappings: [
          { type: 'value', options: { '0': { text: '●' }, '1': { text: '●' } } },
        ],
        thresholds: {
          mode: 'absolute',
          steps: [
            { color: 'red', value: null },
            { color: 'green', value: 1 },
          ],
        },
        unit: 'none',
        noValue: '—',
      },
      overrides: [],
    },
    gridPos,
    id,
    options: {
      colorMode: 'background',
      graphMode: 'none',
      justifyMode: 'center',
      orientation: 'horizontal',
      reduceOptions: { calcs: ['lastNotNull'], fields: '', values: true },
      textMode: 'value_and_name',
      wideLayout: false,
    },
    pluginVersion: '12.1.1',
    targets: [{
      datasource: DS,
      editorMode: 'code',
      expr: 'probe_success{role="player",team="' + team + '",network=~"$network"}',
      legendFormat: 'S{{seat}}',
      refId: 'A',
    }],
    title: '',
    transparent: true,
    type: 'stat',
  };
}

function rttPanel(team, gridPos, id) {
  return {
    datasource: DS,
    fieldConfig: {
      defaults: {
        color: { mode: 'thresholds' },
        mappings: [
          { type: 'special', options: { match: 'null', result: { text: '—', color: 'text' } } },
        ],
        thresholds: {
          mode: 'absolute',
          steps: [
            { color: 'green', value: null },
            { color: 'yellow', value: 0.03 },
            { color: 'red', value: 0.06 },
          ],
        },
        unit: 's',
        decimals: 1,
        noValue: '—',
      },
      overrides: [],
    },
    gridPos,
    id,
    options: {
      colorMode: 'background',
      graphMode: 'area',
      justifyMode: 'center',
      orientation: 'auto',
      reduceOptions: { calcs: ['lastNotNull'], fields: '', values: false },
      textMode: 'value',
      wideLayout: true,
    },
    pluginVersion: '12.1.1',
    targets: [{
      datasource: DS,
      editorMode: 'code',
      expr: 'avg(probe_icmp_duration_seconds{role="player",team="' + team + '",phase="rtt",network=~"$network"})',
      refId: 'A',
    }],
    title: 'Team ' + team,
    type: 'stat',
  };
}

function statSummary(id, x, w, title, expr, color, thresholds) {
  return {
    datasource: DS,
    fieldConfig: {
      defaults: {
        color: { mode: 'thresholds' },
        mappings: [],
        thresholds: thresholds || { mode: 'absolute', steps: [{ color, value: null }] },
      },
      overrides: [],
    },
    gridPos: { h: HEADER_H, w, x, y: INFRA_H },
    id,
    options: {
      colorMode: 'background',
      graphMode: 'area',
      justifyMode: 'center',
      orientation: 'auto',
      reduceOptions: { calcs: ['lastNotNull'], fields: '', values: false },
      textMode: 'value',
    },
    pluginVersion: '12.1.1',
    targets: [{
      datasource: DS,
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

  // Upstream infrastructure health row (always at top)
  panels.push(infraStatPanel(80, 0,  8, '核心交换机', 'infra-core-ping'));
  panels.push(infraStatPanel(81, 8,  8, '分线交换机', 'infra-dist-ping'));
  panels.push(infraStatPanel(82, 16, 8, '防火墙',     'infra-fw-ping'));

  panels.push(statSummary(1, 0,  6, '在线',   'count(probe_success{role="player",network=~"$network"} == 1) or vector(0)', 'green'));
  panels.push(statSummary(2, 6,  6, '离线',   'count(probe_success{role="player",network=~"$network"} == 0) or vector(0)', 'green', {
    mode: 'absolute', steps: [{ color: 'green', value: null }, { color: 'red', value: 1 }],
  }));
  panels.push(statSummary(3, 12, 6, '高延迟', 'count(probe_icmp_duration_seconds{role="player",network=~"$network",phase="rtt"} > 0.03) or vector(0)', 'green', {
    mode: 'absolute', steps: [{ color: 'green', value: null }, { color: 'orange', value: 1 }],
  }));
  panels.push(statSummary(4, 18, 6, '总计',   'count(probe_success{role="player",network=~"$network"}) or vector(0)', 'blue'));

  let y = INFRA_H + HEADER_H;
  let idBase = 100;
  const halfWidth = 12;

  for (const row of layout.rows) {
    const leftCount = row.left.length;
    const rightCount = row.right.length;
    const leftW = Math.floor(halfWidth / leftCount);
    const rightW = Math.floor(halfWidth / rightCount);

    let x = 0;
    for (const team of row.left) {
      panels.push(dotsPanel(team, { h: layout.dotsH, w: leftW, x, y },                                       idBase + team));
      panels.push(rttPanel (team, { h: layout.teamH - layout.dotsH, w: leftW, x, y: y + layout.dotsH },     idBase + team + 50));
      x += leftW;
    }

    x = 12;
    for (const team of row.right) {
      panels.push(dotsPanel(team, { h: layout.dotsH, w: rightW, x, y },                                      idBase + team));
      panels.push(rttPanel (team, { h: layout.teamH - layout.dotsH, w: rightW, x, y: y + layout.dotsH },    idBase + team + 50));
      x += rightW;
    }

    y += layout.teamH;
    idBase += 200;
  }

  if (layout.withTrend) {
    panels.push({
      datasource: DS,
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
          ]},
          unit: 's',
        },
        overrides: [],
      },
      gridPos: { h: TREND_H, w: 24, x: 0, y },
      id: 900,
      options: {
        legend: { calcs: ['mean', 'max'], displayMode: 'table', placement: 'right', showLegend: true },
        tooltip: { mode: 'multi', sort: 'none' },
      },
      pluginVersion: '12.1.1',
      targets: [{
        datasource: DS,
        editorMode: 'code',
        expr: 'avg by (team) (probe_icmp_duration_seconds{role="player",network=~"$network",phase="rtt"})',
        legendFormat: 'Team {{team}}',
        refId: 'A',
      }],
      title: '各队平均延迟趋势',
      type: 'timeseries',
    });
  }

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
    version: 2,
  };
}

const outDir = process.argv[2];
for (const [key, layout] of Object.entries(layouts)) {
  const dash = buildDashboard(layout);
  const path = outDir + '/' + layout.uid + '.json';
  fs.writeFileSync(path, JSON.stringify(dash, null, 2) + '\n');
  console.log('wrote ' + path + ' (' + dash.panels.length + ' panels)');
}
