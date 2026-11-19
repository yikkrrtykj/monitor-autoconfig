;(function () {
  'use strict';

  const charts = window.BSPingCharts;
  const history = window.BSPingHistory;
  const { escapeHtml: esc } = window.BSUtils;
  const timeOptions = {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23", timeZoneName: "short"
  };
  const localTime = new Intl.DateTimeFormat("zh-CN", timeOptions);
  const preciseTime = new Intl.DateTimeFormat("zh-CN", { ...timeOptions, fractionalSecondDigits: 3 });
  const time = (t) => {
    const ms = Math.round(t * 1000);
    return (ms % 1000 ? preciseTime : localTime).format(new Date(ms));
  };
  const subMillisecond = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 3 });
  const milliseconds = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 });
  const formatRtt = (value) => Number.isFinite(value) && value >= 0
    ? (value < 1 ? subMillisecond : milliseconds).format(value) + " ms" : "N/A";
  const percent = (value) => value == null ? "N/A" : value.toFixed(1) + "%";

  function describe({ rows, aggregated }) {
    return rows.map(({ target, sample, bucket }) => {
      const segment = sample?.segment || bucket?.segment;
      const ip = sample?.ip || segment?.ip || (target.dns ? "" : target.ip);
      const identity = `<strong>${esc(segment?.address || target.name)}${ip ? " · " + esc(ip) : ""}
        ${segment ? " · 代次 " + segment.generation : ""}</strong>`;
      if (aggregated) {
        return `<div>${identity}<span>${bucket ? `聚合桶 ${time(bucket.from)} — ${time(bucket.to)} · Loss ${percent(bucket.loss)} · ${bucket.failed}/${bucket.total} 失败` : "此处无聚合桶"}</span></div>`;
      }
      return `<div>${identity}<span>${sample ? `${time(sample.t)} · ` : ""}${history.sampleStatus(sample)} · RTT ${formatRtt(sample?.rtt)}</span></div>`;
    }).join("");
  }

  function bind(container, series, { aggregated, onPin, onZoom, onClear }) {
    const svg = container.querySelector("[data-chart-start]");
    if (!svg) return null;
    const tooltip = container.querySelector(".ping-tooltip");
    const vertical = svg.querySelector(".ping-hover-x");
    const horizontal = svg.querySelector(".ping-hover-y");
    const pinned = svg.querySelector(".ping-pin");
    const brush = svg.querySelector(".ping-brush");
    const { chartStart: startValue, chartEnd: endValue } = svg.dataset;
    const start = Number(startValue);
    const end = Number(endValue);
    const { left, right, top, bottom } = charts.plot;
    const x = (t) => left + (t - start) * (right - left) / (end - start);
    let gesture = null;
    let frame = null;
    let latest = null;
    let keyboard = null;
    const events = new AbortController();
    const listen = (type, callback) => svg.addEventListener(type, callback, { signal: events.signal });
    const at = (event) => charts.chartTime(svg, event.clientX);
    const inRange = (t) => t >= start && t <= end;

    function clearHover() {
      tooltip.hidden = true;
      vertical.classList.add("ping-plot-hidden");
      horizontal.classList.add("ping-plot-hidden");
      brush.classList.add("ping-plot-hidden");
    }

    function hover(t, relativeX) {
      const pick = history.selection(series, t, aggregated);
      const px = x(pick.time);
      vertical.setAttribute("d", `M${px} ${top}V${bottom}`);
      vertical.classList.remove("ping-plot-hidden");
      horizontal.classList.toggle("ping-plot-hidden", pick.rtt == null);
      if (pick.rtt != null) {
        const py = bottom - pick.rtt * (bottom - top) / Number(svg.dataset.chartMax);
        horizontal.setAttribute("d", `M${left} ${py}H${right}`);
      }
      tooltip.innerHTML = describe(pick);
      tooltip.hidden = false;
      tooltip.classList.toggle("ping-tooltip-left", relativeX > 0.58);
      return pick;
    }

    function preview(from, to) {
      const a = Math.min(from, to);
      const b = Math.max(from, to);
      brush.setAttribute("x", x(a));
      brush.setAttribute("width", Math.max(1, x(b) - x(a)));
      brush.classList.remove("ping-plot-hidden");
      vertical.classList.add("ping-plot-hidden");
      horizontal.classList.add("ping-plot-hidden");
      tooltip.textContent = `${time(Math.round(a))} — ${time(Math.round(b))}`;
      tooltip.hidden = false;
    }

    function cancelMove() {
      if (frame !== null) cancelAnimationFrame(frame);
      frame = null;
      latest = null;
    }

    listen("pointermove", (event) => {
      if (gesture && gesture.id !== event.pointerId) return;
      if (event.pointerType === "touch" && !gesture) return;
      latest = { clientX: event.clientX };
      if (frame !== null) return;
      frame = requestAnimationFrame(() => {
        frame = null;
        const t = at(latest);
        if (gesture && Math.abs(latest.clientX - gesture.x) >= 6) gesture.drag = true;
        if (gesture?.drag) preview(gesture.time, t);
        else hover(t, (t - start) / (end - start));
      });
    });
    listen("pointerleave", () => {
      if (!gesture) { cancelMove(); clearHover(); }
    });
    listen("pointerdown", (event) => {
      if (event.button !== 0 || !event.isPrimary || gesture) return;
      gesture = { id: event.pointerId, x: event.clientX, time: at(event), drag: false };
      svg.setPointerCapture(event.pointerId);
    });
    listen("pointerup", (event) => {
      if (gesture?.id !== event.pointerId) return;
      const t = at(event);
      const down = gesture;
      gesture = null;
      cancelMove();
      clearHover();
      if (down.drag || Math.abs(event.clientX - down.x) >= 6) {
        onZoom(Math.round(Math.min(t, down.time)), Math.round(Math.max(t, down.time)));
      } else {
        const pick = hover(t, (t - start) / (end - start));
        if (pick.canPin) onPin(pick);
      }
    });
    listen("pointercancel", () => { gesture = null; cancelMove(); clearHover(); });
    listen("lostpointercapture", () => {
      if (gesture) { gesture = null; cancelMove(); clearHover(); }
    });
    listen("keydown", (event) => {
      if (event.key === "Escape") { clearHover(); onClear(); return; }
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        const t = keyboard ?? end;
        const pick = hover(t, (t - start) / (end - start));
        if (pick.canPin) onPin(pick);
        return;
      }
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const step = aggregated ? series[0]?.step ?? Math.ceil((end - start) / 240)
        : Math.min(...series.map(({ target }) => target.interval));
      const next = (keyboard ?? end) + (event.key === "ArrowLeft" ? -step : step);
      keyboard = event.key === "Home" ? start : event.key === "End" ? end : Math.max(start, Math.min(end, next));
      hover(keyboard, (keyboard - start) / (end - start));
    });
    listen("blur", clearHover);
    return {
      marker(t) {
        pinned.classList.toggle("ping-plot-hidden", !Number.isFinite(t) || !inRange(t));
        if (Number.isFinite(t) && inRange(t)) pinned.setAttribute("d", `M${x(t)} ${top}V${bottom}`);
      },
      dispose() { events.abort(); cancelMove(); }
    };
  }

  window.BSPingTimeline = { bind, time, describe };
})();
