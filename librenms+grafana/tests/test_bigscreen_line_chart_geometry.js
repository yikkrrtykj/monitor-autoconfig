const assert = require('assert');
const { createLineChartRenderer } = require('../bigscreen/charts/line-chart.js');

class FakeSlot {
  constructor(width, height) {
    this.width = width;
    this.height = height;
    this.clientWidth = 0;
    this.clientHeight = 0;
    this.innerHTML = '';
  }

  getBoundingClientRect() {
    return { width: this.width, height: this.height };
  }
}

class FakeContainer {
  constructor(outerWidth, outerHeight, slotWidth, slotHeight) {
    this.outerWidth = outerWidth;
    this.outerHeight = outerHeight;
    this.clientWidth = outerWidth;
    this.clientHeight = outerHeight;
    this.slotWidth = slotWidth;
    this.slotHeight = slotHeight;
    this.layoutHtml = '';
    this.chartSlot = null;
    this.legendSlot = null;
  }

  set innerHTML(value) {
    this.layoutHtml = String(value);
    if (this.layoutHtml.includes('line-chart-slot')) {
      this.chartSlot = new FakeSlot(this.slotWidth, this.slotHeight);
      this.legendSlot = { innerHTML: '' };
    }
  }

  get innerHTML() {
    return this.layoutHtml;
  }

  getBoundingClientRect() {
    return { width: this.outerWidth, height: this.outerHeight };
  }

  querySelector(selector) {
    if (selector === '.line-chart-slot') return this.chartSlot;
    if (selector === '.side-legend' || selector === '.bottom-legend') return this.legendSlot;
    return null;
  }
}

const containers = new Map();
const cssPixels = {
  '--line-axis-pad-left': '80px',
  '--line-axis-pad-right': '40px',
  '--line-axis-pad-top': '16px',
  '--line-axis-pad-bottom': '32px'
};
const document = {
  defaultView: {
    getComputedStyle() {
      return { getPropertyValue: (name) => cssPixels[name] || '' };
    }
  },
  getElementById(id) {
    return containers.get(id);
  }
};

const values = [{ t: 100, v: 1 }, { t: 110, v: 2 }];
const renderLineChart = createLineChartRenderer({
  document,
  seriesColors: ['#58d68d'],
  renderNoData: (container) => { container.innerHTML = 'no data'; },
  escapeHtml: (value) => String(value),
  niceMax: (value) => Math.max(1, value),
  roundUpToStep: (value) => value,
  formatTime: (value) => String(value),
  linePathFromPoints: (points) => `M ${points.join(' L ')}`,
  stepPathFromPoints: (points) => `M ${points.join(' L ')}`,
  splitPointsOnGaps: (points) => [points],
  lineSeriesStats: (points) => ({ last: points.at(-1).v, max: 2, mean: 1.5, min: 1 }),
  lineSeriesHasTimeline: (item) => item.values.length > 0,
  lineSeriesCurrentDisplay: (_item, stats) => ({ currentStatus: 'online', label: null, value: stats.last }),
  lineFailurePoints: () => []
});

const sideContainer = new FakeContainer(1000, 300, 720, 260);
containers.set('sideChart', sideContainer);
renderLineChart('sideChart', [{ name: 'core-switch-with-a-long-name', values }], {});

assert.ok(sideContainer.layoutHtml.includes('line-layout side-layout'));
assert.ok(sideContainer.layoutHtml.includes('line-chart-slot'));
assert.match(sideContainer.chartSlot.innerHTML, /width="720" height="260" viewBox="0 0 720 260"/);
assert.ok(!sideContainer.chartSlot.innerHTML.includes('viewBox="0 0 1000 300"'));
assert.match(sideContainer.chartSlot.innerHTML, /class="chart-grid-line" x1="80"[^>]+x2="680"/);
assert.ok(sideContainer.legendSlot.innerHTML.includes('core-switch-with-a-long-name'));

const bottomContainer = new FakeContainer(900, 280, 900, 220);
containers.set('bottomChart', bottomContainer);
renderLineChart('bottomChart', [{ name: 'wan', values }], { legend: 'bottom' });

assert.ok(bottomContainer.layoutHtml.includes('line-layout bottom-layout'));
assert.match(bottomContainer.chartSlot.innerHTML, /width="900" height="220" viewBox="0 0 900 220"/);

const fallbackContainer = new FakeContainer(640, 220, 0, 0);
const fallbackDocument = {
  getElementById() {
    return fallbackContainer;
  }
};
const fallbackRenderer = createLineChartRenderer({
  document: fallbackDocument,
  seriesColors: ['#58d68d'],
  renderNoData: () => {},
  escapeHtml: (value) => String(value),
  niceMax: (value) => Math.max(1, value),
  roundUpToStep: (value) => value,
  formatTime: (value) => String(value),
  linePathFromPoints: (points) => `M ${points.join(' L ')}`,
  stepPathFromPoints: (points) => `M ${points.join(' L ')}`,
  splitPointsOnGaps: (points) => [points],
  lineSeriesStats: (points) => ({ last: points.at(-1).v, max: 2, mean: 1.5, min: 1 }),
  lineSeriesHasTimeline: (item) => item.values.length > 0,
  lineSeriesCurrentDisplay: (_item, stats) => ({ currentStatus: 'online', label: null, value: stats.last }),
  lineFailurePoints: () => []
});
fallbackRenderer('fallbackChart', [{ name: 'fallback', values }], {});
assert.match(fallbackContainer.chartSlot.innerHTML, /width="640" height="220" viewBox="0 0 640 220"/);
assert.match(fallbackContainer.chartSlot.innerHTML, /class="chart-grid-line" x1="76"[^>]+x2="602"/);

console.log('bigscreen line chart geometry tests passed');
