;(function () {
  'use strict';

  const model = window.BSPingModel;
  const charts = window.BSPingCharts;
  const { escapeHtml: esc, buildCsv } = window.BSUtils;
  const intervals = { "5m": 300, "1h": 3600, "6h": 21600, "24h": 86400, "7d": model.maxHistory };
  const states = { UP: "在线", DOWN: "离线", DEGRADED: "质量异常", UNKNOWN: "未知" };
  const fmt = charts.fmt;
  const time = (t) => new Date(t * 1000).toLocaleString("zh-CN", { hour12: false });
  const now = () => Math.floor(Date.now() / 1000);
  const percent = (value) => value === null ? "N/A" : `${value.toFixed(1)}%`;
  const localInput = (t) => {
    const date = new Date(t * 1000);
    return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 19);
  };
  const safeCsvText = (value) => /^[\t\r\n]|^\s*[=+\-@]/.test(value) ? "'" + value : value;

  function createPingPanel(root, data, settings) {
    const byId = (id) => root.querySelector(`#ping${id}`);
    const category = byId("Category");
    const status = byId("Status");
    const search = byId("Search");
    const rangeField = byId("Range");
    const customStart = byId("Start");
    const customEnd = byId("End");
    const cursorField = byId("Cursor");
    const statsField = byId("StatsWindow");
    const historical = byId("HistoricalTargets");
    const rows = byId("Rows");
    const detail = byId("Detail");
    const requests = new Map();
    const selected = new Map();
    let active = false;
    let timer = null;
    let targets = [];
    let agedOut = 0;
    let visible = [];
    let page = 0;
    let lastSuccess = null;
    let dataState = "loading";
    let snapshotAt = null;
    let routeTarget = null;
    let trends = null;
    let range = null;
    let cursor = null;
    let overview = null;
    let drill = null;

    function cancel(name) {
      requests.get(name)?.abort();
      requests.delete(name);
    }

    async function run(name, load, done, failed) {
      cancel(name);
      const controller = new AbortController();
      requests.set(name, controller);
      try {
        const result = await load(controller.signal);
        if (!active || requests.get(name) !== controller) return false;
        done(result);
        return true;
      } catch {
        if (!active || requests.get(name) !== controller) return false;
        failed();
        return true;
      } finally {
        if (requests.get(name) === controller) requests.delete(name);
      }
    }

    function list() {
      const term = search.value.trim().toLowerCase();
      const filtered = targets.filter((target) =>
        (!category.value || category.value === target.category) &&
        (!status.value || status.value === target.status) &&
        (!term || [target.name, target.ip, target.subtitle, target.group].some((value) => value.toLowerCase().includes(term))));
      const pages = Math.max(1, Math.ceil(filtered.length / model.pageSize));
      page = Math.min(page, pages - 1);
      visible = filtered.slice(page * model.pageSize, (page + 1) * model.pageSize);
      const focus = document.activeElement;
      const focusRow = rows.contains(focus) ? [visible.findIndex((target) => target.key === focus.dataset.key), focus.dataset.action] : null;
      rows.innerHTML = visible.map((target, index) => {
        const cache = dataState === "ready" && trends?.rows.get(target.key);
        const stats = cache && model.rawStats(cache, model.defaultStats, target.interval);
        const lossTitle = `${target.lossWindow / 60} 分钟 · 成功 ${target.loss.success} · 失败 ${target.loss.failed} · 总计 ${target.loss.total} · 覆盖 ${percent(target.loss.coverage * 100)}`;
        const picked = selected.has(target.key);
        return `<tr data-row="${index}">
          <th scope="row"><button type="button" class="ping-target" data-action="open" data-index="${index}" data-key="${esc(target.key)}">${esc(target.name)}</button>
            <small>${esc(target.ip)}${target.subtitle ? " · " + esc(target.subtitle) : ""}</small></th>
          <td>${esc(target.group)}</td>
          <td><span class="ping-badge" data-state="${target.status}" title="${esc(target.reason)}">${target.status}</span></td>
          <td>${fmt(target.rtt)}</td>
          <td title="${stats?.stddev == null ? "样本不足或查询不可用" : `${stats.rttSamples} 个有效 RTT 样本`}">${fmt(stats?.stddev)}</td>
          <td title="${lossTitle}">${percent(target.loss.loss)}</td>
          <td>${cache ? charts.sparkline(cache, trends.at - model.defaultStats, trends.at, target.interval * 2.5) : "N/A"}</td>
          <td><button type="button" data-action="compare" data-index="${index}" data-key="${esc(target.key)}" aria-pressed="${picked}" aria-label="${picked ? "移除" : "对比"} ${esc(target.name)}" ${!picked && selected.size >= model.maxCompared ? "disabled" : ""}>${picked ? "已选" : "+"}</button></td>
        </tr>`;
      }).join("");
      if (focusRow && focusRow[0] >= 0) {
        rows.querySelector(`[data-index="${focusRow[0]}"][data-action="${focusRow[1]}"]`)?.focus({ preventScroll: true });
      }
      const empty = category.value === "remote" ? "远端监控未配置" :
        dataState === "loading" ? "正在读取监控目标…" :
        dataState === "unavailable" ? "Monitoring Unavailable · 监控数据不可用" : "没有符合条件的目标";
      byId("Empty").textContent = visible.length ? "" : empty;
      byId("Prev").disabled = page === 0;
      byId("Next").disabled = page + 1 >= pages;
      byId("Page").textContent = `第 ${page + 1} / ${pages} 页 · ${filtered.length} 个目标`;
    }

    function renderSnapshot() {
      const counts = { UP: 0, DOWN: 0, DEGRADED: 0, UNKNOWN: 0 };
      for (const { status } of targets) counts[status] += 1;
      const available = dataState === "ready" && targets.length > counts.UNKNOWN;
      const partial = counts.UNKNOWN > 0 || agedOut > 0;
      const mode = snapshotAt === null ? "实时监控" : "历史快照";
      const source = byId("Source");
      source.dataset.state = !available ? "UNKNOWN" : partial ? "DEGRADED" : "UP";
      source.textContent = dataState === "loading" ? "正在读取" :
        !available ? "Monitoring Unavailable" : partial ? `${mode} · 部分不可用` : mode;
      byId("Counts").textContent = Object.entries(states).map(([key, label]) => `${label} ${counts[key]}`).join(" · ");
      const coverage = byId("Coverage");
      const coverageText = agedOut
        ? `${agedOut} 个目标已超出 ${model.inventorySeconds / 60} 分钟观察窗口并移出列表，消失原因未知。`
        : `清单依据最近 ${model.inventorySeconds / 60} 分钟的抓取记录。消失的目标显示 UNKNOWN，超出窗口后移出列表。`;
      if (coverage.textContent !== coverageText) coverage.textContent = coverageText;
      byId("Updated").textContent = `最后成功更新 ${lastSuccess === null ? "—" : charts.formatTime(lastSuccess)}`;
      list();
    }

    function refreshTrends() {
      if (!active || dataState !== "ready" || !visible.length) return;
      const at = snapshotAt ?? now();
      const current = visible.slice();
      const signature = JSON.stringify(current.map(({ key }) => key));
      if (requests.has("trends") && requests.get("trends").signature === signature) return;
      if (trends && trends.signature === signature && at - trends.at < 30) return;
      run("trends", (signal) => data.raw(current, at - model.defaultStats, at, signal), (result) => {
        trends = { at, rows: result, signature };
        list();
      }, () => {
        trends = null;
        list();
      });
      requests.get("trends").signature = signature;
    }

    function unavailable(reason) {
      dataState = "unavailable";
      cancel("trends");
      trends = null;
      targets = targets.map((target) => ({ ...target, status: "UNKNOWN", rtt: null, sourceAt: null,
        loss: { ...target.loss, total: 0, success: 0, failed: 0, coverage: 0, loss: null }, reason }));
      renderSnapshot();
    }

    async function refreshSnapshot() {
      clearTimeout(timer);
      const at = snapshotAt ?? now();
      const applied = await run("summary", (signal) => data.summary(at, signal), (result) => {
        ({ targets, agedOut } = result);
        dataState = targets.length ? "ready" : "unavailable";
        if (targets.some((target) => target.sourceAt !== null)) lastSuccess = now();
        renderSnapshot();
        refreshTrends();
        const deepTarget = targets.find((target) => target.key === routeTarget);
        if (deepTarget) {
          routeTarget = null;
          choose(deepTarget, false);
        }
      }, () => unavailable("监控查询失败"));
      if (applied && active && !requests.has("summary") && snapshotAt === null) {
        clearTimeout(timer);
        timer = setTimeout(refreshSnapshot, Math.max(500, 5000 - (now() - at) * 1000));
      }
    }

    function renderHistory() {
      detail.hidden = !selected.size;
      if (!selected.size) return;
      const chosen = [...selected.values()];
      const { start, end } = range;
      byId("Selected").innerHTML = chosen.map((target, index) =>
        `<button type="button" data-action="remove" data-index="${index}" style="--ping-series:${charts.colors[index]}" aria-label="移除 ${esc(target.name)}">${esc(target.name)} · ${esc(target.ip)} ×</button>`).join("");
      byId("HistoryCaption").textContent = `${time(start)} 至 ${time(end)} · ${end - start > model.maxRaw ? "抽样 RTT，丢包按原始探测分桶；点击时间线或色块查看原始记录" : "原始 RTT；点击时间线或色块定位时间点"}`;
      if (!overview?.data) {
        byId("OverviewChart").innerHTML = `<p class="ping-empty">${overview?.failed ? "历史查询失败，请刷新后重试。" : "正在读取历史数据…"}</p>`;
        byId("OverviewLoss").innerHTML = "";
      } else {
        const sampled = end - start > model.maxRaw;
        const series = chosen.map((target) => {
          const result = overview.data.get(target.key);
          if (sampled) return { target, ...result, gap: result.step * 1.5 };
          return { target, points: result, gap: target.interval * 2.5,
            buckets: model.bucketRows(result, start, end, Math.max(10, Math.ceil((end - start) / 90)), target.interval) };
        });
        byId("OverviewChart").innerHTML = charts.chart(series, start, end, cursor, { sampled });
        byId("OverviewLoss").innerHTML = charts.heatmap(series);
      }
      renderDrill();
    }

    function renderDrill() {
      const exportButton = byId("Export");
      exportButton.disabled = !drill?.data || ![...drill.data.values()].some((points) => points.length);
      const statsSeconds = Number(statsField.value);
      const start = cursor - statsSeconds;
      byId("DrillCaption").textContent = `${time(start)} 至 ${time(cursor)} · 统计及 CSV 使用此原始窗口`;
      if (!drill?.data) {
        byId("Metrics").innerHTML = `<p class="ping-empty">${drill?.failed ? "原始样本不可用，请重新查询。" : "正在读取原始样本…"}</p>`;
        byId("Distribution").innerHTML = "";
        byId("Events").innerHTML = "";
        return;
      }
      const series = [...selected.values()].map((target) => {
        const points = drill.data.get(target.key);
        return { target, points, gap: target.interval * 2.5,
          stats: model.rawStats(points, statsSeconds, target.interval),
          buckets: model.bucketRows(points, start, cursor, Math.max(model.defaultStats, Math.ceil(statsSeconds / 36)), target.interval) };
      });
      byId("Metrics").innerHTML = series.map(({ target, stats }, index) => `
        <article class="ping-metrics-card" style="--ping-series:${charts.colors[index]}">
          <h4>${esc(target.name)} · ${esc(target.ip)}</h4>
          <dl>${[["P50", fmt(stats.p50)], ["P95", fmt(stats.p95)], ["Max", fmt(stats.max)], ["STDDEV", fmt(stats.stddev)], ["Loss", percent(stats.loss)]]
            .map(([label, value]) => `<div><dt>${label}</dt><dd>${value}</dd></div>`).join("")}</dl>
          <small>成功 ${stats.success} · 失败 ${stats.failed} · 有效探测 ${stats.total} · 覆盖 ${percent(stats.coverage * 100)}<br>
            有效 RTT ${stats.rttSamples} · RTT 覆盖 ${percent(stats.rttCoverage * 100)}${stats.p95 === null ? " · 分位数与 STDDEV 样本不足" : ""}</small>
        </article>`).join("");
      const rawChart = range.end - range.start > model.maxRaw || start < range.start;
      byId("Distribution").innerHTML = `${rawChart ? '<h4>原始 RTT 时间线</h4>' + charts.chart(series, start, cursor, cursor) : ""}
        <h4>历史 RTT 分布带</h4><p>实线为 P50，虚线为 P95。此图每轮取一个 RTT，按时间窗口计算分布。SmokePing 烟雾图使用同轮多个探测包。</p>
        ${charts.chart(series, start, cursor, cursor, { distribution: true })}`;
      const failuresOnly = byId("Failures").checked;
      const events = series.flatMap(({ target, points }) => points.map((point) => ({ ...point, name: target.name, ip: target.ip })))
        .filter(({ success }) => !failuresOnly || success !== 1).sort((a, b) => b.t - a.t);
      byId("Events").innerHTML = events.length ? events.slice(0, 20).map((point) => `
        <tr><td>${time(point.t)}</td><td>${esc(point.name)} · ${esc(point.ip)}</td>
        <td>${point.success === 1 ? "成功" : point.success === 0 ? "失败" : point.scrape === 0 ? "抓取失败" : "未知"}</td>
        <td>${fmt(point.rtt)}</td></tr>`).join("") : '<tr><td colspan="4">该窗口没有符合条件的样本</td></tr>';
    }

    function refreshDrill() {
      cancel("drill");
      const chosen = [...selected.values()];
      if (!chosen.length) return;
      const end = cursor;
      const start = end - Number(statsField.value);
      if (overview?.raw && overview.data && start >= range.start && end <= range.end) {
        drill = { data: new Map([...overview.data].map(([key, points]) => [key, points.filter(({ t }) => t > start && t <= end)])) };
        renderDrill();
        return;
      }
      drill = null;
      renderDrill();
      run("drill", (signal) => data.raw(chosen, start, end, signal), (result) => {
        drill = { data: result };
        renderDrill();
      }, () => {
        drill = { failed: true };
        renderDrill();
      });
    }

    function refreshHistory() {
      cancel("history");
      cancel("drill");
      overview = null;
      drill = null;
      renderHistory();
      const chosen = [...selected.values()];
      if (!chosen.length) return;
      const { start, end } = range;
      const raw = end - start <= model.maxRaw;
      run("history", (signal) => (raw ? data.raw : data.overview)(chosen, start, end, signal), (result) => {
        overview = { data: result, raw };
        renderHistory();
        refreshDrill();
      }, () => {
        overview = { failed: true };
        renderHistory();
        refreshDrill();
      });
    }

    function setCursor(t) {
      if (!Number.isFinite(t) || t < range.start || t > range.end) {
        byId("Error").textContent = "请选择当前时间范围内的时间点。";
        return;
      }
      cursor = t;
      cursorField.value = localInput(t);
      byId("Error").textContent = "";
      renderHistory();
      refreshDrill();
    }

    function applyRange() {
      const custom = rangeField.value === "custom";
      byId("Custom").hidden = !custom;
      const end = custom ? Date.parse(customEnd.value) / 1000 : now();
      const start = custom ? Date.parse(customStart.value) / 1000 : end - intervals[rangeField.value];
      if (!Number.isFinite(start) || !Number.isFinite(end) || start >= end || end > now() || end - start > model.maxHistory) {
        byId("Error").textContent = "请输入有效的起止时间，范围最长 7 天，结束时间不能晚于当前时间。";
        return false;
      }
      byId("Error").textContent = "";
      range = { start, end };
      cursor = end;
      cursorField.value = localInput(end);
      if (!custom) historical.checked = false;
      if (historical.checked || snapshotAt !== null) switchSnapshot();
      refreshHistory();
      return true;
    }

    function switchSnapshot() {
      snapshotAt = historical.checked ? range.end : null;
      cancel("trends");
      trends = null;
      dataState = "loading";
      targets = [];
      agedOut = 0;
      page = 0;
      lastSuccess = null;
      renderSnapshot();
      refreshSnapshot();
    }

    function choose(target, toggle) {
      if (!target) return;
      const first = selected.size === 0;
      if (!toggle) selected.clear();
      if (toggle && selected.has(target.key)) selected.delete(target.key);
      else if (selected.size < model.maxCompared) selected.set(target.key, target);
      list();
      if (first && rangeField.value !== "custom") applyRange();
      else refreshHistory();
      if (!toggle) detail.scrollIntoView({ block: "start" });
    }

    function download() {
      if (!drill?.data) return;
      const rows = [["timestamp_utc", "job", "name", "ip", "target_labels", "scrape_up", "probe_success", "rtt_ms"]];
      for (const target of selected.values()) {
        for (const point of drill.data.get(target.key)) {
          rows.push([new Date(point.t * 1000).toISOString(), target.job, safeCsvText(target.name),
            safeCsvText(target.ip), target.key, point.scrape, point.success, point.rtt]);
        }
      }
      const url = URL.createObjectURL(new Blob([buildCsv(rows)], { type: "text/csv;charset=utf-8" }));
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `ping-${new Date(cursor * 1000).toISOString().replace(/:/g, "-")}.csv`;
      anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 0);
    }

    root.addEventListener("click", (event) => {
      const button = event.target.closest("button");
      const chart = event.target.closest("[data-chart-start]");
      if (chart && byId("OverviewChart").contains(chart)) setCursor(charts.chartTime(chart, event.clientX));
      if (!button) return;
      if (button.dataset.cursor) setCursor(Number(button.dataset.cursor));
      if (button.dataset.action === "open") choose(visible[Number(button.dataset.index)], false);
      if (button.dataset.action === "compare") choose(visible[Number(button.dataset.index)], true);
      if (button.dataset.action === "remove") choose([...selected.values()][Number(button.dataset.index)], true);
    });
    for (const field of [category, status, search]) field.addEventListener("input", () => {
      page = 0;
      list();
      refreshTrends();
    });
    for (const [id, delta] of [["Prev", -1], ["Next", 1]]) byId(id).addEventListener("click", () => {
      page += delta;
      list();
      refreshTrends();
    });
    rangeField.addEventListener("change", () => {
      if (rangeField.value === "custom") byId("Custom").hidden = false;
      else applyRange();
    });
    byId("Apply").addEventListener("click", applyRange);
    historical.addEventListener("change", switchSnapshot);
    byId("Refresh").addEventListener("click", () => {
      if (applyRange() && !historical.checked) refreshSnapshot();
    });
    byId("Locate").addEventListener("click", () => setCursor(Date.parse(cursorField.value) / 1000));
    statsField.addEventListener("change", refreshDrill);
    byId("Failures").addEventListener("change", renderDrill);
    byId("Export").addEventListener("click", download);
    byId("Policy").textContent = `质量异常阈值为 Loss ≥ ${settings.lossThreshold}% 或当前 RTT ≥ ${settings.rttThreshold} ms。统计覆盖至少 80%，P50 / P95 / STDDEV 至少 30 个有效 RTT。`;

    function stop() {
      active = false;
      clearTimeout(timer);
      for (const name of requests.keys()) cancel(name);
    }

    function start(params) {
      stop();
      active = true;
      selected.clear();
      snapshotAt = null;
      root.scrollTop = 0;
      category.value = status.value = search.value = "";
      historical.checked = false;
      rangeField.value = "5m";
      statsField.value = String(model.defaultStats);
      byId("Failures").checked = false;
      const at = now();
      customStart.value = localInput(at - 300);
      customEnd.value = localInput(at);
      routeTarget = params.get("target");
      const atParam = params.get("at");
      const past = atParam ? (/^\d{10}$/.test(atParam) ? Number(atParam) : Date.parse(atParam) / 1000) : null;
      if (Number.isInteger(past) && past > 300 && past <= at) {
        rangeField.value = "custom";
        customStart.value = localInput(past - 300);
        customEnd.value = localInput(past);
        historical.checked = Boolean(routeTarget);
      }
      applyRange();
      if (!historical.checked) switchSnapshot();
    }

    return { start, stop };
  }

  window.BSPingPanel = { createPingPanel };
})();
