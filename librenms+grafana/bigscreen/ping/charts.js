;(function () {
  'use strict';

  const { escapeHtml } = window.BSUtils;
  const colors = ["#68d9ee", "#ffd17a", "#c3a8ff", "#a3e47e"];
  const width = 900;
  const left = 53;
  const right = 882;
  const top = 16;
  const bottom = 153;
  const shortTime = new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  const dateTime = new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
  const formatTime = (t) => shortTime.format(new Date(t * 1000));
  const formatAxis = (t, span) => span >= 86400 ? dateTime.format(new Date(t * 1000)) : formatTime(t);
  const fmt = (value) => value == null ? "N/A" : value.toFixed(value >= 1 ? 1 : 3);

  function line(points, x, y, gap) {
    let previous = null;
    let path = "";
    for (const { t, rtt } of points) {
      if (rtt === null) {
        previous = null;
        continue;
      }
      const cx = x(t).toFixed(2);
      const cy = y(rtt).toFixed(2);
      path += previous === null || t - previous > gap
        ? `M${cx} ${cy}h.15` : `L${cx} ${cy}`;
      previous = t;
    }
    return path;
  }

  function sparkline(points, start, end, gap) {
    const max = points.reduce((limit, point) => Math.max(limit, point.rtt || 0), 0);
    if (!max) return "N/A";
    const path = line(points, (t) => (t - start) * 111 / (end - start), (value) => 27 - value * 23 / max, gap);
    return `<svg viewBox="0 0 112 30" class="ping-sparkline" role="img" aria-label="最近五分钟 RTT"><path d="${path}" /></svg>`;
  }

  function chart(series, start, end, cursor, { distribution = false, sampled = false } = {}) {
    const limit = series.reduce((max, item) => {
      const points = distribution ? item.buckets : item.points;
      return points.reduce((peak, value) => Math.max(peak, (distribution ? value.p95 : value.rtt) || 0), max);
    }, 0);
    if (!limit) return `<p class="ping-empty">${distribution ? "样本不足，无法绘制分布带" : "该时间段没有有效 RTT 样本"}</p>`;
    const x = (t) => left + (t - start) / (end - start) * (right - left);
    const y = (value) => bottom - value / limit * (bottom - top);
    const grid = Array.from({ length: 4 }, (_, index) => {
      const value = limit * index / 3;
      const row = y(value);
      return `<path class="ping-gridline" d="M${left} ${row}H${right}"/><text x="${left - 7}" y="${row + 4}" text-anchor="end">${fmt(value)}</text>`;
    }).join("");
    const ticks = [start, (start + end) / 2, end].map((t) =>
      `<text x="${x(t)}" y="178" text-anchor="${t === start ? "start" : t === end ? "end" : "middle"}">${formatAxis(t, end - start)}</text>`).join("");
    const paths = series.map((item, index) => {
      const color = colors[index];
      if (!distribution) {
        return `<path d="${line(item.points, x, y, item.gap)}" fill="none" stroke="${color}" stroke-width="1.6" vector-effect="non-scaling-stroke" ${sampled ? 'stroke-dasharray="4 3"' : ""}/>`;
      }
      return item.buckets.filter(({ p95 }) => p95 !== null).map(({ from, to, p50, p95 }) => `
        <rect x="${x(from)}" y="${y(p95)}" width="${x(to) - x(from)}" height="${y(p50) - y(p95)}" fill="${color}" opacity=".23"/>
        <path d="M${x(from)} ${y(p95)}H${x(to)}" stroke="${color}" stroke-opacity=".7" stroke-dasharray="4 3"/>
        <path d="M${x(from)} ${y(p50)}H${x(to)}" stroke="${color}" stroke-width="2"/>`).join("");
    }).join("");
    const marker = cursor >= start && cursor <= end ? `<path class="ping-cursor" d="M${x(cursor)} ${top}V${bottom}"/>` : "";
    return `<svg class="ping-chart" viewBox="0 0 ${width} 186" data-chart-start="${start}" data-chart-end="${end}" role="img" aria-label="RTT 时间线（毫秒）">
      <text x="7" y="14">ms</text>${grid}${ticks}${paths}${marker}</svg>`;
  }

  function chartTime(svg, clientX) {
    const click = svg.createSVGPoint();
    click.x = clientX;
    const chartX = click.matrixTransform(svg.getScreenCTM().inverse()).x;
    const ratio = (chartX - left) / (right - left);
    const start = Number(svg.dataset.chartStart);
    const end = Number(svg.dataset.chartEnd);
    return Math.round(start + Math.max(0, Math.min(1, ratio)) * (end - start));
  }

  function heatmap(series) {
    return series.map(({ target, buckets }, index) => `<div class="ping-heat-row">
      <span title="${escapeHtml(target.ip)}" style="--ping-series:${colors[index]}">${escapeHtml(target.name)}</span>
      <div class="ping-heat-track">${buckets.map((bucket) => {
        const state = bucket.loss === null ? "unknown" : bucket.failed === 0 ? "clear" : bucket.loss >= 50 ? "high" : "loss";
        const time = formatAxis(bucket.to, buckets.at(-1).to - buckets[0].from);
        const title = `${time} · ${bucket.loss === null ? "N/A" : bucket.loss.toFixed(1) + "%"} · ${bucket.failed} 失败 / ${bucket.total} 有效`;
        return `<button type="button" tabindex="-1" class="ping-heat-cell ${state}" title="${title}" aria-label="${title}" data-cursor="${bucket.to}" style="flex-grow:${bucket.to - bucket.from}"></button>`;
      }).join("")}</div></div>`).join("");
  }

  window.BSPingCharts = { colors, formatTime, fmt, sparkline, chart, chartTime, heatmap };
})();
