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

  // The shared scale gives LAN jitter room while retaining WAN context.
  const wallTicks = [0, 25, 100, 500, 2000];
  const wallY = (value) => {
    const index = wallTicks.findIndex((tick) => value <= tick);
    if (index <= 0) return index === 0 ? 52 : 4;
    const from = wallTicks[index - 1];
    return 52 - 12 * (index - 1 + (value - from) / (wallTicks[index] - from));
  };

  function sparkline(points, start, end, interval) {
    const x = (t) => 1 + (t - start) * 298 / (end - start);
    const gap = interval * 1.5;
    const grid = wallTicks.map((tick) => `M1 ${wallY(tick)}H299`).join("");
    const events = { loss: "", scrape: "", missing: "", clipped: "" };
    let previous = start;
    for (const point of points) {
      const cx = x(point.t).toFixed(2);
      if (point.t - previous > gap) {
        events.missing += `M${x(previous + interval / 2).toFixed(2)} 60H${x(point.t - interval / 2).toFixed(2)}`;
      }
      if (point.scrape === 0) events.scrape += `M${cx} 55v5`;
      else if (point.success === 0) events.loss += `M${cx} 55v5`;
      else if (point.rtt === null) events.missing += `M${cx} 59h.5`;
      if (point.rtt > wallTicks.at(-1)) events.clipped += `M${cx} 4v4`;
      previous = point.t;
    }
    if (end - previous > gap) events.missing += `M${x(previous).toFixed(2)} 60H299`;
    const path = line(points, x, wallY, gap);
    const label = points.length ? "最近 5 分钟 RTT，共用刻度" : "最近 5 分钟无样本";
    const marks = Object.entries(events).filter(([, value]) => value)
      .map(([kind, value]) => `<path class="ping-mark-${kind}" d="${value}"/>`).join("");
    return `<svg viewBox="0 0 300 62" preserveAspectRatio="none" class="ping-sparkline" role="img" aria-label="${label}">
      <path class="ping-gridline" d="${grid}"/><path class="ping-trace" d="${path}"/>${marks}</svg>`;
  }

  function chart(series, start, end, cursor, { distribution = false, sampled = false, interactive = false } = {}) {
    const limit = series.reduce((max, item) => {
      const points = distribution ? item.buckets : item.points;
      return points.reduce((peak, value) => Math.max(peak, (distribution ? value.p95 : value.rtt) || 0), max);
    }, 0);
    if (!limit && distribution) return `<p class="ping-empty">样本不足，无法绘制分布带</p>`;
    const x = (t) => left + (t - start) / (end - start) * (right - left);
    const y = (value) => bottom - value / (limit || 1) * (bottom - top);
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
        const marks = { loss: "", scrape: "", missing: "" };
        if (!sampled) {
          for (const point of item.points) {
            const kind = point.scrape === 0 ? "scrape" : point.success === 0 ? "loss" : point.rtt === null ? "missing" : null;
            if (kind) marks[kind] += `M${x(point.t).toFixed(2)} ${bottom - index * 5}v-4`;
          }
        }
        return `<path d="${line(item.points, x, y, item.gap)}" fill="none" stroke="${color}" stroke-width="1.6" vector-effect="non-scaling-stroke" ${sampled ? 'stroke-dasharray="4 3"' : ""}/>${Object.entries(marks).filter(([, d]) => d).map(([kind, d]) => `<path class="ping-mark-${kind}" d="${d}" fill="none" />`).join("")}`;
      }
      return item.buckets.filter(({ p95 }) => p95 !== null).map(({ from, to, p50, p95 }) => `
        <rect x="${x(from)}" y="${y(p95)}" width="${x(to) - x(from)}" height="${y(p50) - y(p95)}" fill="${color}" opacity=".23"/>
        <path d="M${x(from)} ${y(p95)}H${x(to)}" stroke="${color}" stroke-opacity=".7" stroke-dasharray="4 3"/>
        <path d="M${x(from)} ${y(p50)}H${x(to)}" stroke="${color}" stroke-width="2"/>`).join("");
    }).join("");
    const marker = cursor >= start && cursor <= end ? `<path class="ping-cursor" d="M${x(cursor)} ${top}V${bottom}"/>` : "";
    const overlays = interactive ? `<rect class="ping-brush ping-plot-hidden" y="${top}" height="${bottom - top}"/>
      <path class="ping-pin ping-plot-hidden"/><path class="ping-hover-x ping-plot-hidden"/><path class="ping-hover-y ping-plot-hidden"/>` : "";
    const svg = `<svg class="ping-chart" viewBox="0 0 ${width} 186" data-chart-start="${start}" data-chart-end="${end}" data-chart-max="${limit || 1}"
      ${interactive ? 'tabindex="0" aria-describedby="pingChartInstructions" preserveAspectRatio="none"' : ""} role="img" aria-label="RTT 时间线（毫秒）">
      <text x="7" y="14">ms</text>${grid}${ticks}${paths}${marker}${overlays}</svg>`;
    return interactive ? `<div class="ping-plot">${svg}<div class="ping-tooltip" role="tooltip" hidden></div></div>` : svg;
  }

  function chartTime(svg, clientX) {
    const click = svg.createSVGPoint();
    click.x = clientX;
    const chartX = click.matrixTransform(svg.getScreenCTM().inverse()).x;
    const ratio = (chartX - left) / (right - left);
    const start = Number(svg.dataset.chartStart);
    const end = Number(svg.dataset.chartEnd);
    return start + Math.max(0, Math.min(1, ratio)) * (end - start);
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

  window.BSPingCharts = { colors, formatTime, fmt, sparkline, chart, chartTime, heatmap, plot: Object.freeze({ left, right, top, bottom }) };
})();
