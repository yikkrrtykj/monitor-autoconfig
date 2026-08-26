;(function () {
  'use strict';

  function createLineChartRenderer(dependencies) {
    const {
      document,
      seriesColors,
      renderNoData,
      escapeHtml,
      niceMax,
      roundUpToStep,
      formatTime,
      linePathFromPoints,
      stepPathFromPoints,
      splitPointsOnGaps,
      lineSeriesStats,
      lineSeriesHasTimeline,
      lineSeriesCurrentDisplay,
      lineFailurePoints
    } = dependencies;

    return function renderLineChart(containerId, seriesList, options) {
      const container = document.getElementById(containerId);
      const series = seriesList.filter(lineSeriesHasTimeline);
      if (!series.length) {
        renderNoData(container);
        return;
      }

      const box = container.getBoundingClientRect();
      const minWidth = Number(options.minWidth) > 0 ? Number(options.minWidth) : 320;
      const minHeight = Number(options.minHeight) > 0 ? Number(options.minHeight) : 150;
      const width = Math.max(minWidth, Math.round(box.width || container.clientWidth || 1000));
      const height = Math.max(minHeight, Math.round(box.height || container.clientHeight || 260));
      const pad = {
        left: options.axisPadLeft || (width < 520 ? 64 : 76),
        right: options.axisPadRight || 38,
        top: Number.isFinite(Number(options.axisPadTop)) ? Number(options.axisPadTop) : 12,
        bottom: Number.isFinite(Number(options.axisPadBottom))
          ? Number(options.axisPadBottom)
          : (height < 190 ? 24 : 30)
      };
      const plotWidth = width - pad.left - pad.right;
      const plotHeight = height - pad.top - pad.bottom;
      const times = series
        .flatMap((item) => item.values.map((point) => point.t))
        .filter((timestamp) => Number.isFinite(timestamp));
      const minT = Math.min(...times);
      const maxT = Math.max(...times);
      const statsBySeries = new Map(series.map((item) => [
        item,
        lineSeriesStats(item.values)
      ]));
      const rawMax = Math.max(
        options.minMax || 0,
        ...Array.from(statsBySeries.values())
          .map((stats) => stats.max)
          .filter((value) => Number.isFinite(value))
      );
      const fixedMax = Number(options.maxY);
      const maxRoundStep = Number(options.maxRoundStep);
      const roundedMax = Number.isFinite(maxRoundStep) && maxRoundStep > 0
        ? roundUpToStep(rawMax, maxRoundStep)
        : niceMax(rawMax);
      const maxV = Number.isFinite(fixedMax) && fixedMax > 0 ? fixedMax : roundedMax;
      const axisFormatter = options.axisFormatter || ((value) => String(value));
      const valueFormatter = options.valueFormatter || axisFormatter;

      const xOf = (timestamp) => pad.left + ((timestamp - minT) / Math.max(1, maxT - minT)) * plotWidth;
      const yOf = (value) => pad.top + (1 - Math.min(1, Math.max(0, value / maxV))) * plotHeight;
      const timeTicks = [minT, minT + (maxT - minT) * 0.25, minT + (maxT - minT) * 0.5, minT + (maxT - minT) * 0.75, maxT];
      const gridLines = [0, 0.25, 0.5, 0.75, 1].map((ratio) => {
        const y = pad.top + (1 - ratio) * plotHeight;
        return `<line class="chart-grid-line" x1="${pad.left}" y1="${y}" x2="${width - pad.right}" y2="${y}" /><text class="chart-axis" x="${pad.left - 10}" y="${y + 4}" text-anchor="end">${escapeHtml(axisFormatter(maxV * ratio))}</text>`;
      }).join("");
      const timeGridLines = timeTicks.map((timestamp) => {
        const x = xOf(timestamp);
        return `<line class="chart-time-line" x1="${x}" y1="${pad.top}" x2="${x}" y2="${height - pad.bottom}" />`;
      }).join("");
      const timeLabels = [
        { timestamp: minT, anchor: "start" },
        { timestamp: (minT + maxT) / 2, anchor: "middle" },
        { timestamp: maxT, anchor: "end" }
      ].map(({ timestamp, anchor }) => {
        const x = xOf(timestamp);
        return `<text class="chart-axis" x="${x}" y="${height - 7}" text-anchor="${anchor}">${formatTime(timestamp)}</text>`;
      }).join("");
      const paths = series.map((item, index) => {
        const color = item.color || seriesColors[index % seriesColors.length];
        const segments = splitPointsOnGaps(item.values, options.breakGapSeconds);
        return segments.map((values) => {
          const points = values.map((point) => `${xOf(point.t).toFixed(1)},${yOf(point.v).toFixed(1)}`);
          const linePath = options.step
            ? stepPathFromPoints(points)
            : linePathFromPoints(points, options.smooth);
          const areaPath = options.fill
            ? `${linePath} L ${xOf(values[values.length - 1].t).toFixed(1)},${height - pad.bottom} L ${xOf(values[0].t).toFixed(1)},${height - pad.bottom} Z`
            : "";
          return `${areaPath ? `<path class="chart-area" d="${areaPath}" style="fill:${color}" />` : ""}<path class="chart-line" d="${linePath}" style="stroke:${color}" />`;
        }).join("");
      }).join("");
      const failureMarkerY = height - pad.bottom - 6;
      const failureMarkers = series.map((item) => {
        return lineFailurePoints(item.values).map((point) => {
          const x = xOf(point.t);
          const arm = 4;
          const title = `${item.name} 探测失败 ${formatTime(point.t)}`;
          const markerStyle = "stroke:#ff4d66;stroke-width:2.4;stroke-linecap:round";
          return `
            <g class="chart-failure-marker" role="img" aria-label="${escapeHtml(title)}">
              <title>${escapeHtml(title)}</title>
              <line x1="${(x - arm).toFixed(1)}" y1="${(failureMarkerY - arm).toFixed(1)}" x2="${(x + arm).toFixed(1)}" y2="${(failureMarkerY + arm).toFixed(1)}" style="${markerStyle}" />
              <line x1="${(x + arm).toFixed(1)}" y1="${(failureMarkerY - arm).toFixed(1)}" x2="${(x - arm).toFixed(1)}" y2="${(failureMarkerY + arm).toFixed(1)}" style="${markerStyle}" />
            </g>
          `;
        }).join("");
      }).join("");
      const currentStatusLegend = !!options.currentStatusLegend;
      const calcs = options.calcs || (currentStatusLegend ? ["last", "max"] : ["mean", "max"]);
      const calcsExplicit = !!options.calcs;
      const calcLabels = { last: "最近", max: "最高", mean: "平均", min: "最低" };
      const seriesColor = new Map(series.map((item, index) => [
        item,
        item.color || seriesColors[index % seriesColors.length]
      ]));
      const legendSeries = options.sortLegendByMax
        ? [...series].sort((left, right) => {
          const leftMax = statsBySeries.get(left).max;
          const rightMax = statsBySeries.get(right).max;
          const comparableLeftMax = Number.isFinite(leftMax) ? leftMax : -Infinity;
          const comparableRightMax = Number.isFinite(rightMax) ? rightMax : -Infinity;
          return comparableRightMax - comparableLeftMax || left.name.localeCompare(right.name, "zh-CN");
        })
        : series;
      const legend = legendSeries.map((item) => {
        const color = seriesColor.get(item);
        const stats = statsBySeries.get(item);
        const currentDisplay = currentStatusLegend
          ? lineSeriesCurrentDisplay(item, stats)
          : null;
        const cells = calcs.map((calc) => {
          const stat = stats[calc];
          const isCurrentCell = currentStatusLegend && calc === "last";
          const displayValue = isCurrentCell && currentDisplay.label !== null
            ? currentDisplay.label
            : (isCurrentCell ? currentDisplay.value : stat);
          const value = escapeHtml(Number.isFinite(displayValue)
            ? valueFormatter(displayValue)
            : (typeof displayValue === "string" ? displayValue : (currentStatusLegend ? "--" : "-")));
          const statusClass = isCurrentCell && currentDisplay.currentStatus === "offline"
            ? ' class="legend-current-status legend-status-offline"'
            : "";
          if (calcsExplicit) {
            const label = escapeHtml(calcLabels[calc] || calc);
            return `<span${statusClass}><i class="legend-calc-label">${label}</i> ${value}</span>`;
          }
          return `<span${statusClass}>${value}</span>`;
        }).join("");
        const namesOnlyStatus = options.legendNamesOnly
          && currentDisplay
          && currentDisplay.currentStatus === "offline"
          ? '<span class="legend-current-status legend-status-offline">OFFLINE</span>'
          : "";
        return `
          <div class="legend-row" title="${escapeHtml(item.name)}">
            <span class="legend-swatch" style="background:${color}"></span>
            <span class="legend-name">${escapeHtml(item.name)}</span>
            ${namesOnlyStatus}
            ${cells}
          </div>
        `;
      }).join("");
      const headerCells = calcs.map((calc) => `<span>${escapeHtml(calcLabels[calc] || calc)}</span>`).join("");
      const legendHeader = options.legendNamesOnly
        ? ""
        : `<div class="legend-row legend-head"><span></span><span>名称</span>${headerCells}</div>`;
      const legendClass = options.legend === "bottom" ? "chart-legend bottom-legend" : "chart-legend side-legend";
      const legendModeClass = options.legendNamesOnly ? "names-only-legend" : "";
      const densityClass = series.length > 24
        ? "ultra-series"
        : series.length > 12
          ? "compact-series"
          : series.length > 8
            ? "dense-series"
            : "";

      container.innerHTML = `
        <div class="line-layout ${options.legend === "bottom" ? "bottom-layout" : "side-layout"} ${densityClass}" style="--series-count:${series.length}">
          <svg class="line-chart" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" focusable="false">
            ${timeGridLines}
            ${gridLines}
            ${paths}
            ${failureMarkers}
            ${timeLabels}
          </svg>
          <div class="${legendClass} ${legendModeClass}">${legendHeader}${legend}</div>
        </div>
      `;
    };
  }

  const ns = { createLineChartRenderer };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = ns;
  } else {
    window.BSLineChart = ns;
  }
}());
