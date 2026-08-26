;(function () {
  'use strict';

  function createLossHeatmapRenderer(dependencies) {
    const { document, renderNoData, formatTime, escapeHtml } = dependencies;

    return function renderLossHeatmap(containerId, seriesList) {
      const container = document.getElementById(containerId);
      const series = seriesList.filter((item) => item.values.length);
      if (!series.length) {
        renderNoData(container);
        return;
      }
      const allTimes = series.flatMap((item) => item.values.map((point) => point.t));
      const minT = Math.min(...allTimes);
      const maxT = Math.max(...allTimes);
      const splitColumns = series.length > 12;
      const densityClass = splitColumns ? "dense-heatmap heatmap-split" : series.length > 8 ? "dense-heatmap" : "";
      const bucketCount = 60;
      const bucketize = (values) => {
        const span = Math.max(1, maxT - minT);
        const bucketSize = span / bucketCount;
        const buckets = Array.from({ length: bucketCount }, (_, index) => ({
          t: minT + bucketSize * (index + 0.5),
          v: null,
          count: 0
        }));
        values.forEach((point) => {
          const index = Math.max(0, Math.min(bucketCount - 1, Math.floor((point.t - minT) / bucketSize)));
          const bucket = buckets[index];
          bucket.v = bucket.v === null ? point.v : Math.max(bucket.v, point.v);
          bucket.count += 1;
        });
        return buckets;
      };
      const renderRows = (items) => items.map((item) => {
        const cells = bucketize(item.values).map((point) => {
          const missing = point.count === 0 || point.v === null;
          const level = missing ? "missing" : point.v > 0.5 ? "bad" : point.v > 0.01 ? "warn" : "good";
          const title = missing ? `${formatTime(point.t)} 无数据` : `${formatTime(point.t)} 丢包 ${(point.v * 100).toFixed(1)}%`;
          return `<span class="heatmap-cell ${level}" title="${escapeHtml(title)}"></span>`;
        }).join("");
        return `
          <div class="heatmap-row">
            <span class="heatmap-name" title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</span>
            <span class="heatmap-cells">${cells}</span>
          </div>
        `;
      }).join("");
      const renderColumn = (items) => `
        <div class="heatmap-column" style="--heatmap-rows:${items.length}">
          <div class="heatmap-rows">${renderRows(items)}</div>
          <div class="heatmap-axis">
            <span aria-hidden="true"></span>
            <span class="heatmap-axis-times"><span>${formatTime(minT)}</span><span>${formatTime((minT + maxT) / 2)}</span><span>${formatTime(maxT)}</span></span>
          </div>
        </div>
      `;
      if (splitColumns) {
        const splitAt = Math.ceil(series.length / 2);
        container.innerHTML = `
          <div class="heatmap ${densityClass}">
            ${renderColumn(series.slice(0, splitAt))}
            ${renderColumn(series.slice(splitAt))}
          </div>
        `;
        return;
      }
      container.innerHTML = `
        <div class="heatmap ${densityClass}" style="--heatmap-rows:${series.length}">
          <div class="heatmap-rows">${renderRows(series)}</div>
          <div class="heatmap-axis">
            <span aria-hidden="true"></span>
            <span class="heatmap-axis-times"><span>${formatTime(minT)}</span><span>${formatTime((minT + maxT) / 2)}</span><span>${formatTime(maxT)}</span></span>
          </div>
        </div>
      `;
    };
  }

  const ns = { createLossHeatmapRenderer };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = ns;
  } else {
    window.BSLossHeatmap = ns;
  }
}());
