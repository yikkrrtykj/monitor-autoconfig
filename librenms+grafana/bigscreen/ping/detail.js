;(function () {
  'use strict';

  const model = window.BSPingModel;
  const charts = window.BSPingCharts;
  const history = window.BSPingHistory;
  const timeline = window.BSPingTimeline;
  const { queryError } = window.BSPingData;
  const { escapeHtml: esc, buildCsv } = window.BSUtils;
  const now = () => Math.floor(Date.now() / 1000);
  const pct = (value) => value == null ? "N/A" : value.toFixed(1) + "%";
  const localInput = (t) => {
    const date = new Date(Math.floor(t) * 1000);
    return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 19);
  };
  const fromInput = (field) => Date.parse(field.value) / 1000;
  const csvText = (value) => /^[\t\r\n]|^\s*[=+\-@]/.test(value) ? "'" + value : value;
  const sameRange = (a, b) => a.start === b.start && a.end === b.end;
  const localDateTime = (t) => localInput(t).replace("T", " ");

  function duration(seconds) {
    return [[86400, "天"], [3600, "小时"], [60, "分钟"], [1, "秒"]].map(([unit, label]) => {
      const count = Math.floor(seconds / unit);
      seconds %= unit;
      return count ? count + " " + label : "";
    }).filter(Boolean).join(" ");
  }

  function createHistoryPanel({ byId, data, run, cancel, replaceUrl, back }) {
    const main = byId("OverviewChart");
    const raw = byId("Distribution");
    const pan = byId("Pan");
    const panTrack = byId("PanTrack");
    const earlier = byId("GoEarlier");
    const later = byId("GoLater");
    const statsField = byId("StatsWindow");
    const historyStack = [];
    let targets = [];
    let at = null;
    let range;
    let home;
    let overview = null;
    let drill = null;
    let pin = null;
    let plot = null;
    let rawPlot = null;
    let navStart = 0;
    let navEnd = 0;
    let navNow;
    let panGesture = null;
    let keyStart = null;
    let panCommit = null;

    function error(message = "") { byId("HistoryError").textContent = message; }

    function identity() {
      byId("Selected").innerHTML = targets.map((target, index) => `
        <div class="ping-history-target" style="--ping-series:${charts.colors[index]}">
          <strong>${esc(target.name)}</strong><small>${esc(target.ip)} · ${esc(model.groupLabel(target))}</small>
          <span class="ping-badge" data-state="${target.status ?? "UNKNOWN"}">${target.status ?? "UNKNOWN"}</span>
          ${targets.length > 1 ? `<button type="button" data-remove="${index}" aria-label="移除 ${esc(target.name)}">×</button>` : ""}
        </div>`).join("");
    }

    function snapshot(entries) {
      const byKey = new Map(entries.map((entry) => [entry.key, entry]));
      targets = targets.map((target) => byKey.get(target.key) || target);
      identity();
    }

    function updateUrl() { replaceUrl(history.urlFor(targets, range, at)); }

    function paintPan(next) {
      const domain = navEnd - navStart;
      const span = next.end - next.start;
      pan.style.left = (next.start - navStart) * 100 / domain + "%";
      pan.style.width = span * 100 / domain + "%";
      const state = sameRange(next, range) ? "当前窗口" : "预览窗口";
      const status = `${state}（${duration(span)}）`;
      const times = `${localDateTime(next.start)} — ${localDateTime(next.end)}`;
      byId("PanStatus").textContent = status;
      byId("PanTimes").textContent = times;
      pan.setAttribute("aria-valuenow", String(next.start));
      pan.setAttribute("aria-valuetext", status + " " + times);
    }

    function navigationBound(name, t) {
      const bound = byId(name);
      bound.dateTime = new Date(t * 1000).toISOString();
      bound.textContent = localDateTime(t);
    }

    function navigator(keepDomain = false) {
      const span = range.end - range.start;
      navNow = now();
      // Keep the preview's scale after release. A button can extend the rail past an edge.
      if (!keepDomain || range.start < navStart) navStart = Math.max(1, Math.min(home.start, range.start - span));
      if (!keepDomain || range.end > navEnd) navEnd = Math.min(navNow, Math.max(home.end, range.end + span));
      const disabled = navEnd - span <= navStart;
      pan.setAttribute("aria-valuemin", String(navStart));
      pan.setAttribute("aria-valuemax", String(navEnd - span));
      pan.setAttribute("aria-disabled", String(disabled));
      pan.tabIndex = disabled ? -1 : 0;
      navigationBound("PanStart", navStart);
      navigationBound("PanEnd", navEnd);
      byId("PrevRange").disabled = !historyStack.length;
      byId("ResetRange").disabled = sameRange(range, home) && !historyStack.length;
      const backSpan = Math.min(span, range.start - 1);
      const forwardSpan = Math.min(span, Math.max(0, navNow - range.end));
      earlier.disabled = !backSpan;
      later.disabled = !forwardSpan;
      earlier.textContent = "← 前 " + duration(backSpan || span);
      later.textContent = "后 " + duration(forwardSpan || span) + " →";
      byId("RangeStart").value = localInput(range.start);
      byId("RangeEnd").value = localInput(range.end);
      const mode = span > model.maxRaw ? "聚合 · RTT 为抽样趋势，丢包按完整探测分桶" : "原始样本 · 时间以浏览器本地时区显示";
      byId("HistoryCaption").textContent = `${timeline.time(range.start)} 至 ${timeline.time(range.end)} · ${mode}`;
      byId("Presets").querySelectorAll("[data-span]").forEach((button) => {
        button.setAttribute("aria-pressed", String(history.presets[button.dataset.span] === span));
      });
      paintPan(range);
    }

    function renderPins() {
      plot?.marker(pin?.time);
      rawPlot?.marker(pin?.time);
      byId("Pinned").innerHTML = pin ? `<h4>已固定 · ${timeline.time(pin.time)}</h4>${timeline.describe(pin)}` : "";
      byId("ClearPin").hidden = !pin;
      byId("Cursor").value = localInput(pin?.time ?? range.end);
    }

    function statistics(container, series, span, aggregated = false) {
      container.innerHTML = series.map(({ target, points, buckets }, index) => {
        const stats = aggregated ? model.lossStats(
          buckets.reduce((n, bucket) => n + bucket.total, 0),
          buckets.reduce((n, bucket) => n + bucket.success, 0), span, target.interval
        ) : model.rawStats(points, span, target.interval);
        const fields = [["P50", charts.fmt(stats.p50)], ["P95", charts.fmt(stats.p95)], ["Max", charts.fmt(stats.max)],
          ["STDDEV", charts.fmt(stats.stddev)], ["Loss", pct(stats.loss)]];
        return `<article class="ping-metrics-card" style="--ping-series:${charts.colors[index]}">
          <h4>${esc(target.name)} · ${esc(target.ip)}</h4>
          <dl>${fields.map(([label, value]) => `<div><dt>${label}</dt><dd>${value}</dd></div>`).join("")}</dl>
          <small>${stats.success} 成功 · ${stats.failed} 失败 · 覆盖 ${pct(stats.coverage * 100)}<br>
          ${aggregated ? "缩小至 1 小时内查看原始 RTT 统计" : `${stats.rttSamples} 个有效 RTT 样本`}</small>
        </article>`;
      }).join("");
    }

    function seriesFor(rows, start, end) {
      return targets.map((target) => {
        const points = rows.get(target.key);
        return { target, points, gap: target.interval * 1.5,
          buckets: model.bucketRows(points, start, end, Math.max(10, Math.ceil((end - start) / 90)), target.interval) };
      });
    }

    function graph(container, series, start, end, aggregated, isRaw) {
      container.innerHTML = charts.chart(series, start, end, null, { sampled: aggregated, interactive: true });
      return timeline.bind(container, series, { aggregated, onPin: (pick) => setPin(pick, isRaw), onZoom: zoom, onClear: clearPin });
    }

    function renderDrillSummary() {
      const show = Boolean(pin && drill?.data);
      byId("DrillStatistics").hidden = !show;
      if (!show) return;
      const end = Math.min(drill.end, Math.max(drill.start + 1, pin.time));
      const start = Math.max(drill.start, end - Number(statsField.value));
      const series = targets.map((target) => ({
        target,
        points: drill.data.get(target.key).filter(({ t }) => t > start && t <= end)
      }));
      byId("DrillStatistics").querySelector("h4").textContent =
        `固定点原始窗口统计 · ${timeline.time(start)} — ${timeline.time(end)}`;
      statistics(byId("DrillMetrics"), series, end - start);
    }

    function renderRaw() {
      rawPlot?.dispose();
      rawPlot = null;
      byId("Export").disabled = !drill?.data || ![...drill.data.values()].some((rows) => rows.length);
      raw.innerHTML = "";
      renderDrillSummary();
      byId("Events").innerHTML = '<tr><td colspan="4">该窗口没有探测记录</td></tr>';
      if (!drill) {
        byId("DrillCaption").textContent = "长区间仅显示聚合统计。固定一个聚合桶可下钻原始记录。";
        return;
      }
      const { start, end, data: rows, failed } = drill;
      const info = failed || (!rows ? "正在读取原始样本…" : "UTC CSV 使用此原始窗口");
      byId("DrillCaption").textContent = `${timeline.time(start)} — ${timeline.time(end)} · ${info}`;
      if (!rows) return;
      if (!overview?.raw && end > start) {
        const series = seriesFor(rows, start, end);
        rawPlot = graph(raw, series, start, end, false, true);
        rawPlot?.marker(pin?.time);
      }
      renderEvents();
    }

    function renderEvents() {
      if (!drill?.data) return;
      const failures = byId("Failures").checked;
      const events = targets.flatMap((target) => drill.data.get(target.key).map((point) => ({ target, point })))
        .filter(({ point }) => !failures || point.success !== 1)
        .sort((a, b) => pin ? Math.abs(a.point.t - pin.time) - Math.abs(b.point.t - pin.time) : b.point.t - a.point.t);
      byId("Events").innerHTML = events.length ? events.slice(0, 20).map(({ target, point }) => `
        <tr><td>${esc(timeline.time(point.t))}</td><td>${esc(target.name)} · ${esc(target.ip)}</td>
        <td>${history.sampleStatus(point)}</td><td>${charts.fmt(point.rtt)}</td></tr>`).join("") : '<tr><td colspan="4">该窗口没有探测记录</td></tr>';
    }

    function refreshDrill() {
      cancel("drill");
      if (!overview || (!overview.raw && !pin)) {
        drill = null;
        renderRaw();
        return;
      }
      const end = pin ? Math.min(range.end, Math.max(range.start + 1, pin.time)) : range.end;
      const start = pin ? Math.max(range.start, end - Number(statsField.value)) : range.start;
      if (overview.raw) {
        drill = { start, end, data: new Map([...overview.data].map(([key, rows]) =>
          [key, rows.filter(({ t }) => t > start && t <= end)])) };
        renderRaw();
        return;
      }
      drill = { start, end };
      renderRaw();
      run("drill", (signal) => data.raw(targets, start, end, signal), (rows) => {
        drill = { start, end, data: rows };
        renderRaw();
      }, (reason) => { drill = { start, end, failed: queryError(reason) }; renderRaw(); });
    }

    function clearPin() {
      if (!pin) return;
      pin = null;
      error();
      renderPins();
      refreshDrill();
    }

    function setPin(pick, isRaw = false) {
      if (pin?.time === pick.time && pin.aggregated === pick.aggregated) {
        clearPin();
        return;
      }
      pin = pick;
      error();
      renderPins();
      // Keep the existing drill graph and its samples under a click in the drill.
      if (isRaw) {
        renderDrillSummary();
        renderEvents();
      } else refreshDrill();
    }

    function locate(t) {
      if (!Number.isFinite(t) || t < range.start || t > range.end || !overview) {
        error("请选择当前图上的时间点。");
        return;
      }
      const pick = history.selection(overview.series, t, !overview.raw);
      if (pick.canPin) setPin(pick);
      else error("此处无原始样本。");
    }

    function refreshHistory() {
      cancel("history");
      cancel("drill");
      plot?.dispose();
      plot = null;
      overview = drill = pin = null;
      main.textContent = "正在读取历史数据…";
      byId("Metrics").innerHTML = byId("OverviewLoss").innerHTML = "";
      renderPins();
      renderRaw();
      const { start, end } = range;
      const isRaw = end - start <= model.maxRaw;
      run("history", (signal) => (isRaw ? data.raw : data.overview)(targets, start, end, signal), (rows) => {
        const series = isRaw ? seriesFor(rows, start, end) : targets.map((target) => {
          const result = rows.get(target.key);
          return { target, ...result, gap: result.step * 1.5 };
        });
        overview = { data: rows, series, raw: isRaw };
        plot = graph(main, series, start, end, !isRaw, false);
        byId("OverviewLoss").innerHTML = charts.heatmap(series);
        statistics(byId("Metrics"), series, end - start, !isRaw);
        refreshDrill();
      }, (reason) => { main.textContent = `历史${queryError(reason)}。可使用刷新重试。`; });
    }

    function clearPan() {
      clearTimeout(panCommit);
      panCommit = null;
      keyStart = null;
      panGesture = null;
    }

    function apply(next, action, keepDomain = false) {
      clearPan();
      if (action === "reset" && sameRange(next, range)) {
        historyStack.length = 0;
        navigator();
        return;
      }
      if (action === "remember") {
        if (sameRange(next, range)) { paintPan(range); return; }
        historyStack.push(range);
        if (historyStack.length > 24) historyStack.shift();
      }
      if (action === "reset") historyStack.length = 0;
      range = next;
      error();
      navigator(keepDomain);
      updateUrl();
      refreshHistory();
    }

    function zoom(start, end) {
      const next = history.validateRange(start, end, now());
      if (!next || end - start < history.minZoom) { error("拖选范围至少 30 秒。"); return; }
      apply(next, "remember");
    }

    function offset(seconds) { apply(history.shiftRange(range, seconds, navNow), "remember", true); }

    function panRange(start) {
      const span = range.end - range.start;
      start = Math.max(navStart, Math.min(navEnd - span, Math.round(start)));
      return { start, end: start + span };
    }

    function timeOnTrack(event) {
      const bounds = panTrack.getBoundingClientRect();
      return navStart + (event.clientX - bounds.left) * (navEnd - navStart) / bounds.width;
    }

    function download() {
      if (!drill?.data) return;
      const rows = [["timestamp_utc", "job", "name", "ip", "target_labels", "scrape_up", "probe_success", "rtt_ms"]];
      for (const target of targets) {
        for (const point of drill.data.get(target.key)) {
          rows.push([new Date(point.t * 1000).toISOString(), target.job, csvText(target.name), csvText(target.ip),
            target.key, point.scrape, point.success, point.rtt]);
        }
      }
      const url = URL.createObjectURL(new Blob([buildCsv(rows)], { type: "text/csv;charset=utf-8" }));
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `ping-${new Date(drill.end * 1000).toISOString().replace(/:/g, "-")}.csv`;
      anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 0);
    }

    byId("BackToWall").addEventListener("click", back);
    byId("HistoryRefresh").addEventListener("click", refreshHistory);
    byId("Presets").addEventListener("click", (event) => {
      const span = history.presets[event.target.closest("[data-span]")?.dataset.span];
      if (!span) return;
      const end = range.end;
      home = { start: Math.max(1, end - span), end };
      apply(home, "reset");
    });
    byId("HistoryApply").addEventListener("click", () => {
      const next = history.validateRange(fromInput(byId("RangeStart")), fromInput(byId("RangeEnd")), now());
      if (!next) { error("请输入有效的起止时间，范围最长 7 天，结束时间不能晚于当前时间。"); return; }
      home = next;
      apply(next, "reset");
    });
    byId("PrevRange").addEventListener("click", () => { if (historyStack.length) apply(historyStack.pop()); });
    byId("ResetRange").addEventListener("click", () => apply(home, "reset"));
    earlier.addEventListener("click", () => offset(-(range.end - range.start)));
    later.addEventListener("click", () => offset(range.end - range.start));
    panTrack.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || !event.isPrimary || panGesture || pan.tabIndex < 0) return;
      clearPan();
      const t = timeOnTrack(event);
      const grab = event.target === pan ? t - range.start : (range.end - range.start) / 2;
      panGesture = { id: event.pointerId, grab };
      panTrack.setPointerCapture(event.pointerId);
      pan.focus({ preventScroll: true });
      paintPan(panRange(t - grab));
    });
    panTrack.addEventListener("pointermove", (event) => {
      if (panGesture?.id === event.pointerId) paintPan(panRange(timeOnTrack(event) - panGesture.grab));
    });
    panTrack.addEventListener("pointerup", (event) => {
      if (panGesture?.id === event.pointerId) {
        apply(panRange(timeOnTrack(event) - panGesture.grab), "remember", true);
      }
    });
    function cancelPanGesture(event) {
      if (panGesture?.id !== event.pointerId) return;
      clearPan();
      paintPan(range);
    }
    panTrack.addEventListener("pointercancel", cancelPanGesture);
    panTrack.addEventListener("lostpointercapture", cancelPanGesture);
    pan.addEventListener("keydown", (event) => {
      if (pan.tabIndex < 0 || panGesture) return;
      if (event.key === "Escape" && keyStart !== null) {
        clearPan();
        paintPan(range);
        return;
      }
      const span = range.end - range.start;
      const step = Math.max(1, Math.round(span / 60));
      const current = keyStart ?? range.start;
      const steps = { ArrowLeft: -step, ArrowDown: -step, ArrowRight: step, ArrowUp: step,
        PageDown: -span, PageUp: span };
      let start;
      if (event.key === "Home") start = navStart;
      else if (event.key === "End") start = navEnd - span;
      else if (Object.hasOwn(steps, event.key)) start = current + steps[event.key];
      else return;
      event.preventDefault();
      const next = panRange(start);
      keyStart = next.start;
      paintPan(next);
      clearTimeout(panCommit);
      panCommit = setTimeout(() => apply(next, "remember", true), 150);
    });
    byId("Detail").addEventListener("keydown", (event) => {
      if (event.key === "Escape") clearPin();
    });
    byId("Locate").addEventListener("click", () => locate(fromInput(byId("Cursor"))));
    byId("ClearPin").addEventListener("click", clearPin);
    byId("OverviewLoss").addEventListener("click", (event) => {
      const button = event.target.closest("[data-cursor]");
      if (button) locate(Number(button.dataset.cursor));
    });
    statsField.addEventListener("change", refreshDrill);
    byId("Failures").addEventListener("change", renderEvents);
    byId("Export").addEventListener("click", download);
    byId("Selected").addEventListener("click", (event) => {
      const button = event.target.closest("[data-remove]");
      if (!button || targets.length <= 1) return;
      targets = targets.filter((_, index) => index !== Number(button.dataset.remove));
      identity();
      updateUrl();
      refreshHistory();
    });

    function start(route, known) {
      targets = route.targets;
      at = route.at;
      const visible = targets.length > 0;
      byId("HistoryBody").hidden = !visible;
      byId("HistoryEmpty").hidden = visible;
      byId("HistoryEmpty").textContent = route.invalid ? "无法识别链接中的设备身份。" : "请先在监控墙选择设备。";
      byId("HistoryTitle").focus({ preventScroll: true });
      if (!visible) return;
      const knownKeys = new Map(known.map((target) => [target.key, target]));
      targets = targets.map((target) => knownKeys.get(target.key) || target);
      identity();
      range = home = route.range;
      historyStack.length = 0;
      statsField.value = String(model.defaultStats);
      byId("Failures").checked = false;
      error(route.invalid ? "链接中有无法识别的设备，已显示其余设备。" : "");
      navigator();
      refreshHistory();
    }

    function stop() {
      clearPan();
      plot?.dispose();
      rawPlot?.dispose();
      plot = rawPlot = null;
      overview = drill = pin = null;
    }

    return { start, stop, snapshot };
  }

  window.BSPingDetail = { createHistoryPanel, localInput };
})();
