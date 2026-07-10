(function () {
  const config = window.BIGSCREEN_CONFIG || {};
  const queries = window.BIGSCREEN_QUERIES || {};
  const pingTrendQuery = queries.pingTrend || "";
  const pingGaugeQuery = queries.pingGauge || "";
  const uptimeQuery = queries.uptime || "";
  const lossQuery = queries.loss || "";
  const playerSnapshotWindow = "90s";
  const seriesColors = ["#73d17a", "#ffe32d", "#5b8ff9", "#ff9f43", "#ff4d66", "#b877db", "#40c4ff", "#b8e986", "#f8e71c"];
  const pages = window.BIGSCREEN_PAGES || [];

  // Pure helpers live in utils.js, the Prometheus/data layer in api.js and the
  // topology layout/SVG pipeline in topology.js (all loaded before this file).
  const {
    escapeHtml, escapeRegex, escapeLabel, metricName, formatPing, formatPingText,
    formatUptime, formatBits, formatTime, niceMax, average,
    networkLabel, seatLabel, gaugeColor, gaugePercent,
    linePathFromPoints, buildCsv, formatTimestampFull
  } = window.BSUtils;
  const {
    prometheusBaseUrl, fetchWithTimeout,
    prometheusQuery, prometheusInstant, prometheusRangeFor,
    prometheusRangeCached, invalidateRangeCache,
    activeInfraPingQuery, activeSeriesNames, filterSeriesByNames,
    fetchIspNames, ispTrafficQuery, fetchIspTraffic, ispCapacityBps, ispChartMaxBps,
    fetchInfraDeviceNames, renameListWithInfraMap,
    fetchTopologyTargets, fetchTopologyEdges, fetchRuntimeStatus,
    fetchPlatformAuthStatus, loginPlatformAuth, changePlatformPassword, logoutPlatformAuth,
    fetchPlatformConfig, postPlatform, patchPlatform, fetchIncidents
  } = window.BSApi;
  const {
    buildTopologyLayers, topologyLayout, renderTopologySvg, topologyNodeKindLabel
  } = window.BSTopology;
  const {
    isGatewayAddress, buildPlayers, latencyLevel, playerStatusText, groupPlayersBySeat
  } = window.BSPlayers;
  const { analyzeIncident } = window.BSIncident;
  const {
    readinessScore,
    summarizePlayers, summarizeTargets, summarizeServices,
    buildConfigRisks, buildTopologyFindings, buildReadinessChecks,
    lintSwitchScene
  } = window.BSPlatform;
  let gaugeTimer = null;
  let chartTimer = null;
  let seenUpTimer = null;
  let infraSeenUp = null;  // Set of "deployed" (ever-online) infra instance names; null/empty = show all
  let tournamentTimer = null;
  let opsTimer = null;
  let controlTimer = null;
  let activePageId = "";
  let activeRoute = "";
  let gaugeSeq = 0;
  let chartSeq = 0;
  let tournamentSeq = 0;
  let topologySeq = 0;
  let stageDeviceRegexCache = null;
  const renderSignatures = new Map();
  let lastDataSuccessAt = 0;
  let lastControlReport = null;
  let lastControlAuth = null;
  let lastPlatformConfig = null;
  let lastEditableConfig = null;
  let lastIncidents = [];
  let configResultSticky = false;
  let applyInProgress = false;
  const DATA_STALE_AFTER_MS = 20000;
  const CONTROL_LAYOUT_STORAGE_KEY = "bigscreen.controlLayout.v1";

  // Skip re-rendering a chart when its data hasn't changed since last paint.
  // Historical Prometheus samples are immutable, so a cheap per-series digest
  // (count + first/last timestamp + last value) captures every real change.
  function seriesSignature(seriesList) {
    return seriesList.map((item) => {
      const values = item.values || [];
      const last = values.length ? values[values.length - 1] : null;
      return `${item.name}#${values.length}#${values.length ? values[0].t : ""}#${last ? `${last.t}=${last.v}` : ""}`;
    }).join("|");
  }

  function shouldRender(key, signature) {
    if (renderSignatures.get(key) === signature) {
      return false;
    }
    renderSignatures.set(key, signature);
    return true;
  }

  function setText(id, value) {
    const element = document.getElementById(id);
    if (element) {
      element.textContent = value || "";
    }
  }

  function titleText() {
    if (config.title) {
      return config.title;
    }
    if (config.eventName) {
      return `${config.eventName} 网络监控大屏`;
    }
    return "网络监控大屏";
  }

  function pageFromPath() {
    const path = window.location.pathname.replace(/\/+$/, "") || "/";
    if (path === "/index.html") return pages[0];
    if (path === "/evidence") return pages.find((page) => page.id === "evidence") || pages[0];
    return pages.find((page) => page.path === path) || pages[0];
  }

  function stageDevicePattern() {
    return String(config.stageDeviceFilter || "stage,wutai,舞台")
      .split(",")
      .map((name) => name.trim())
      .filter(Boolean)
      .map(escapeRegex)
      .join("|") || "stage|wutai|舞台";
  }

  function isStageDeviceName(name) {
    if (!stageDeviceRegexCache) {
      stageDeviceRegexCache = new RegExp(stageDevicePattern(), "i");
    }
    return stageDeviceRegexCache.test(String(name || ""));
  }

  function filterStageDeviceItems(items) {
    return items.filter((item) => isStageDeviceName(item.name || metricName(item.metric || {})));
  }

  function filterStageDeviceSeries(seriesList) {
    return seriesList.filter((item) => isStageDeviceName(item.name));
  }

  function activePage() {
    return pages.find((page) => page.id === activePageId) || {};
  }

  function shouldFilterStageDevices() {
    return Boolean(activePage().kind);
  }

  function visibleInfraItems(items) {
    return shouldFilterStageDevices() ? filterStageDeviceItems(items) : items;
  }

  function visibleInfraSeries(seriesList) {
    return shouldFilterStageDevices() ? filterStageDeviceSeries(seriesList) : seriesList;
  }

  function infraDisplayKey(item) {
    const name = String(item.name || metricName(item.metric || {}) || "").trim();
    if (name) return name;
    return JSON.stringify(item.metric || {});
  }

  function preferItem(previous, current, mode) {
    if (!previous) return current;
    const previousValue = Number(previous.value);
    const currentValue = Number(current.value);
    if (!Number.isFinite(previousValue)) return current;
    if (!Number.isFinite(currentValue)) return previous;
    if (mode === "min") return currentValue < previousValue ? current : previous;
    return currentValue > previousValue ? current : previous;
  }

  function dedupeInfraItems(items, mode) {
    const byName = new Map();
    items.forEach((item) => {
      const key = infraDisplayKey(item);
      byName.set(key, preferItem(byName.get(key), item, mode || "max"));
    });
    return Array.from(byName.values()).sort((a, b) => a.name.localeCompare(b.name, "zh-CN"));
  }

  function mergePointValues(left, right, mode) {
    const points = new Map();
    const choose = mode === "min"
      ? (a, b) => (b.v < a.v ? b : a)
      : (a, b) => (b.v > a.v ? b : a);
    const put = (point) => {
      const t = Number(point.t);
      const v = Number(point.v);
      if (!Number.isFinite(t) || !Number.isFinite(v)) return;
      const key = String(t);
      const normalized = { t, v };
      const existing = points.get(key);
      points.set(key, existing ? choose(existing, normalized) : normalized);
    };
    (left || []).forEach(put);
    (right || []).forEach(put);
    return Array.from(points.values()).sort((a, b) => a.t - b.t);
  }

  function mergeInfraSeries(seriesList, mode) {
    const byName = new Map();
    seriesList.forEach((item) => {
      const key = infraDisplayKey(item);
      const existing = byName.get(key);
      if (!existing) {
        byName.set(key, { ...item, values: [...(item.values || [])] });
        return;
      }
      byName.set(key, {
        ...existing,
        values: mergePointValues(existing.values, item.values, mode || "max")
      });
    });
    return Array.from(byName.values()).sort((a, b) => a.name.localeCompare(b.name, "zh-CN"));
  }

  function playerLabel(team, seat, network) {
    return `${teamName({ id: "" }, team)} ${seatLabel(seat)} ${networkLabel(network)}`;
  }

  function renderGaugeGrid(containerId, items, kind, forceRows) {
    const container = document.getElementById(containerId);
    const formatter = kind === "ping" ? formatPing : formatUptime;
    const rows = forceRows
      ? Math.max(1, Math.min(items.length, forceRows))
      : Math.max(1, Math.min(items.length, items.length > 8 ? 3 : 2));
    const columns = Math.max(1, Math.ceil(items.length / rows));
    container.dataset.rows = String(rows);
    container.style.setProperty("--gauge-columns", String(columns));
    container.style.setProperty("--gauge-rows", String(rows));
    container.innerHTML = "";

    if (!items.length) {
      container.innerHTML = '<div class="empty-state">暂无数据</div>';
      return;
    }

    items.forEach((item) => {
      const formatted = formatter(item.value);
      const card = document.createElement("article");
      card.className = `gauge-item gauge-${kind}`;
      card.title = item.name;
      card.style.setProperty("--gauge-color", gaugeColor(kind, item.value));
      card.style.setProperty("--gauge-fill", String(gaugePercent(kind, item.value) * 100));
      card.innerHTML = `
        <div class="gauge-visual" aria-hidden="true">
          <svg viewBox="0 0 220 150" focusable="false">
            <path class="threshold threshold-green" pathLength="100" d="M 25 127 A 88 88 0 1 1 195 127" />
            <path class="threshold threshold-yellow" pathLength="100" d="M 25 127 A 88 88 0 1 1 195 127" />
            <path class="threshold threshold-red" pathLength="100" d="M 25 127 A 88 88 0 1 1 195 127" />
            <path class="gauge-track" pathLength="100" d="M 48 121 A 64 64 0 1 1 172 121" />
            <path class="gauge-value-path" pathLength="100" d="M 48 121 A 64 64 0 1 1 172 121" />
          </svg>
          <div class="gauge-number"><strong>${formatted.value}</strong><span>${formatted.unit}</span></div>
        </div>
        <div class="gauge-name">${escapeHtml(item.name)}</div>
      `;
      container.appendChild(card);
    });
  }

  function renderNoData(container, message) {
    container.innerHTML = `<div class="no-data">${message || "暂无数据"}</div>`;
  }

  function renderLineChart(containerId, seriesList, options) {
    const container = document.getElementById(containerId);
    const series = seriesList.filter((item) => item.values.length);
    if (!series.length) {
      renderNoData(container);
      return;
    }

    const box = container.getBoundingClientRect();
    const width = Math.max(320, Math.round(box.width || container.clientWidth || 1000));
    const height = Math.max(150, Math.round(box.height || container.clientHeight || 260));
    const pad = {
      left: options.axisPadLeft || (width < 520 ? 64 : 76),
      right: options.axisPadRight || 38,
      top: 12,
      bottom: height < 190 ? 24 : 30
    };
    const plotWidth = width - pad.left - pad.right;
    const plotHeight = height - pad.top - pad.bottom;
    const times = series.flatMap((item) => item.values.map((point) => point.t));
    const minT = Math.min(...times);
    const maxT = Math.max(...times);
    const rawMax = Math.max(options.minMax || 0, ...series.flatMap((item) => item.values.map((point) => point.v)));
    const fixedMax = Number(options.maxY);
    const maxV = Number.isFinite(fixedMax) && fixedMax > 0 ? fixedMax : niceMax(rawMax);
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
      const points = item.values.map((point) => `${xOf(point.t).toFixed(1)},${yOf(point.v).toFixed(1)}`);
      const linePath = linePathFromPoints(points, options.smooth);
      const areaPath = options.fill
        ? `${linePath} L ${xOf(item.values[item.values.length - 1].t).toFixed(1)},${height - pad.bottom} L ${xOf(item.values[0].t).toFixed(1)},${height - pad.bottom} Z`
        : "";
      return `${areaPath ? `<path class="chart-area" d="${areaPath}" style="fill:${color}" />` : ""}<path class="chart-line" d="${linePath}" style="stroke:${color}" />`;
    }).join("");
    const calcs = options.calcs || ["mean", "max"];
    const calcsExplicit = !!options.calcs;
    const calcLabels = { last: "最近", max: "最高", mean: "平均", min: "最低" };
    const legend = series.map((item, index) => {
      const color = item.color || seriesColors[index % seriesColors.length];
      const values = item.values.map((point) => point.v);
      const stats = {
        last: values[values.length - 1],
        max: Math.max(...values),
        mean: average(values),
        min: Math.min(...values),
      };
      const cells = calcs.map((calc) => {
        const value = escapeHtml(valueFormatter(stats[calc]));
        if (calcsExplicit) {
          const label = escapeHtml(calcLabels[calc] || calc);
          return `<span><i class="legend-calc-label">${label}</i> ${value}</span>`;
        }
        return `<span>${value}</span>`;
      }).join("");
      return `
        <div class="legend-row">
          <span class="legend-swatch" style="background:${color}"></span>
          <span class="legend-name">${escapeHtml(item.name)}</span>
          ${cells}
        </div>
      `;
    }).join("");
    const headerCells = calcs.map((calc) => `<span>${escapeHtml(calcLabels[calc] || calc)}</span>`).join("");
    const legendHeader = `<div class="legend-row legend-head"><span></span><span>名称</span>${headerCells}</div>`;
    const legendClass = options.legend === "bottom" ? "chart-legend bottom-legend" : "chart-legend side-legend";
    const densityClass = series.length > 12 ? "compact-series" : series.length > 8 ? "dense-series" : "";

    container.innerHTML = `
      <div class="line-layout ${options.legend === "bottom" ? "bottom-layout" : "side-layout"} ${densityClass}" style="--series-count:${series.length}">
        <svg class="line-chart" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" focusable="false">
          ${timeGridLines}
          ${gridLines}
          ${paths}
          ${timeLabels}
        </svg>
        <div class="${legendClass}">${legendHeader}${legend}</div>
      </div>
    `;
  }

  function renderSparkline(containerId, seriesList) {
    const container = document.getElementById(containerId);
    const series = seriesList.filter((item) => item.values.length);
    if (!series.length) {
      renderNoData(container, "暂无趋势");
      return;
    }

    const box = container.getBoundingClientRect();
    const width = Math.max(120, Math.round(box.width || container.clientWidth || 180));
    const height = Math.max(44, Math.round(box.height || container.clientHeight || 72));
    const pad = { left: 4, right: 4, top: 6, bottom: 10 };
    const plotWidth = width - pad.left - pad.right;
    const plotHeight = height - pad.top - pad.bottom;
    const times = series.flatMap((item) => item.values.map((point) => point.t));
    const minT = Math.min(...times);
    const maxT = Math.max(...times);
    const rawMax = Math.max(0.005, ...series.flatMap((item) => item.values.map((point) => point.v)));
    const maxV = niceMax(rawMax);
    const xOf = (timestamp) => pad.left + ((timestamp - minT) / Math.max(1, maxT - minT)) * plotWidth;
    const yOf = (value) => pad.top + (1 - Math.min(1, Math.max(0, value / maxV))) * plotHeight;
    const paths = series.map((item, index) => {
      const color = item.color || seriesColors[index % seriesColors.length];
      const points = item.values.map((point) => `${xOf(point.t).toFixed(1)},${yOf(point.v).toFixed(1)}`);
      return `<path class="sparkline-path" d="${linePathFromPoints(points, true)}" style="stroke:${color}" />`;
    }).join("");
    const legend = series.slice(0, 5).map((item, index) => {
      const color = item.color || seriesColors[index % seriesColors.length];
      return `<span><i style="background:${color}"></i>${escapeHtml(item.name)}</span>`;
    }).join("");

    container.innerHTML = `
      <svg class="sparkline-chart" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" focusable="false">
        <line class="sparkline-grid" x1="${pad.left}" y1="${yOf(maxV * 0.5)}" x2="${width - pad.right}" y2="${yOf(maxV * 0.5)}" />
        ${paths}
      </svg>
      <div class="sparkline-legend">${legend}</div>
    `;
  }

  function renderHeatmap(containerId, seriesList) {
    const container = document.getElementById(containerId);
    const series = seriesList.filter((item) => item.values.length);
    if (!series.length) {
      renderNoData(container);
      return;
    }
    const allTimes = series.flatMap((item) => item.values.map((point) => point.t));
    const minT = Math.min(...allTimes);
    const maxT = Math.max(...allTimes);
    const densityClass = series.length > 12 ? "compact-heatmap" : series.length > 8 ? "dense-heatmap" : "";
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
    const rows = series.map((item) => {
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
    container.innerHTML = `
      <div class="heatmap ${densityClass}" style="--heatmap-rows:${series.length}">
        <div class="heatmap-rows">${rows}</div>
        <div class="heatmap-axis">
          <span aria-hidden="true"></span>
          <span class="heatmap-axis-times"><span>${formatTime(minT)}</span><span>${formatTime((minT + maxT) / 2)}</span><span>${formatTime(maxT)}</span></span>
        </div>
      </div>
    `;
  }

  function renderIspPanels(results) {
    const ispGrid = document.getElementById("ispGrid");
    ispGrid.style.setProperty("--isp-coun…26381 tokens truncated…event.offline ? `${event.recoveryCount} 次断线后恢复` : `最高 ${formatPingText(event.maxLatency)}`}</span>
      </div>
    `).join("");
  }

  function renderIncidentIsp(result) {
    const element = document.getElementById("incidentIsp");
    if (!result.ispEvents.length) {
      element.innerHTML = `<div class="incident-empty">ISP 流量数据不可用</div>`;
      return;
    }

    element.innerHTML = result.ispEvents
      .sort((a, b) => b.utilization - a.utilization)
      .map((event) => {
        const pct = Math.round(event.utilization * 100);
        const cls = event.utilization >= 0.7 ? "warn" : event.utilization >= 0.4 ? "info" : "info";
        return `
          <div class="incident-item ${cls}">
            <strong>${escapeHtml(event.ifAlias)}</strong>
            <em>${event.direction === "in" ? "下载" : "上传"} · 上限 ${escapeHtml(formatBits(event.capacityBps))}</em>
            <span>峰值 ${escapeHtml(formatBits(event.maxBps))}（${pct}%）</span>
          </div>
        `;
      }).join("");
  }

  function renderIncidentStage(result) {
    const element = document.getElementById("incidentStage");
    const stages = Object.values(result.stageGroups || {});
    if (!stages.length) {
      element.innerHTML = `<div class="incident-empty">没有 stage 受影响</div>`;
      return;
    }

    element.innerHTML = stages
      .sort((a, b) => b.players.length - a.players.length)
      .map((stage) => `
        <div class="incident-item ${stage.players.length >= 3 ? "warn" : "info"}">
          <strong>${escapeHtml(stage.switch)}</strong>
          <em>${stage.players.length} 个选手</em>
          <span>${stage.players.slice(0, 8).map((player) => `T${escapeHtml(player.team)}S${escapeHtml(player.seat)}`).join("、")}${stage.players.length > 8 ? "…" : ""}</span>
        </div>
      `).join("");
  }

  async function runIncidentAnalysis() {
    const win = incidentWindow();
    const threshold = Number(document.getElementById("incidentThreshold").value || 0.05);

    const params = new URLSearchParams();
    const at = document.getElementById("incidentAt").value;
    if (at) params.set("at", at);
    params.set("window", String(win.minutes));
    params.set("threshold", String(threshold));
    window.history.replaceState({}, "", `/incident?${params.toString()}`);

    ["incidentVerdict","incidentPlayers","incidentInfra","incidentIsp","incidentStage"].forEach((id) => {
      document.getElementById(id).innerHTML = `<div class="incident-empty">加载中...</div>`;
    });

    try {
      const data = await queryIncidentData(win);
      const result = analyzeIncident(data, threshold);
      renderIncidentVerdict(result.verdict);
      renderIncidentPlayers(result);
      renderIncidentInfra(result);
      renderIncidentIsp(result);
      renderIncidentStage(result);
    } catch (error) {
      console.error("Incident analysis failed:", error);
      document.getElementById("incidentVerdict").className = "incident-verdict bad";
      document.getElementById("incidentVerdict").innerHTML = `<strong>分析失败</strong><span>${escapeHtml(error.message || "")}</span>`;
    }
  }

  function setupIncidentPanel() {
    const atInput = document.getElementById("incidentAt");
    const form = document.getElementById("incidentForm");
    const params = new URLSearchParams(window.location.search);
    const at = params.get("at");
    const winVal = params.get("window");
    const threshold = params.get("threshold");

    if (at) atInput.value = at;
    else if (!atInput.value) atInput.value = dateTimeInputValue(new Date());

    if (winVal) {
      const winSelect = document.getElementById("incidentWindow");
      if (winSelect && Array.from(winSelect.options).some((opt) => opt.value === winVal)) {
        winSelect.value = winVal;
      }
    }
    if (threshold) {
      const thrSelect = document.getElementById("incidentThreshold");
      if (thrSelect && Array.from(thrSelect.options).some((opt) => opt.value === threshold)) {
        thrSelect.value = threshold;
      }
    }

    if (form && !form.dataset.bound) {
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        runIncidentAnalysis();
      });
      form.dataset.bound = "1";
    }

    runIncidentAnalysis();
  }

  function showIncident() {
    const screen = document.querySelector(".screen");
    stopInfraRefresh();
    stopTournamentRefresh();
    stopOpsRefresh();
    stopControlRefresh();
    stopTopologyRefresh();
    screen.className = "screen incident-mode";
    setVisible("homePanel", false);
    setVisible("panelGrid", false);
    setVisible("tournamentPanel", false);
    setVisible("evidencePanel", false);
    setVisible("opsPanel", false);
    setVisible("controlPanel", false);
    setVisible("incidentPanel", true);
    setVisible("topologyPanel", false);
    setupIncidentPanel();
  }

  // ---- Network topology ----

  let topologyTimer = null;
  // Latest laid-out nodes; the click handlers read from here so an in-place
  // latency update (render skipped) still shows fresh numbers in the detail
  // panel without rebinding events.
  let topologyNodes = [];

  function stopTopologyRefresh() {
    if (topologyTimer) {
      window.clearInterval(topologyTimer);
      topologyTimer = null;
    }
  }

  function bindTopologyNodeEvents() {
    const detail = document.getElementById("topologyDetail");
    const canvas = document.getElementById("topologyCanvas");
    if (canvas) {
      canvas.onclick = (event) => {
        if (event.target.closest && event.target.closest(".topology-node")) return;
        detail.hidden = true;
      };
    }
    document.querySelectorAll(".topology-node").forEach((el) => {
      const handler = (event) => {
        if (event && event.stopPropagation) event.stopPropagation();
        const idx = Number(el.dataset.idx);
        const node = topologyNodes[idx];
        if (!node) return;
        const syslogUrl = node.ip ? `${window.location.protocol}//${window.location.hostname}:3000/d/device-syslog?var-host=${encodeURIComponent(node.ip)}` : "";
        detail.hidden = false;
        detail.innerHTML = `
          <header><strong>${escapeHtml(node.name)}</strong><span class="dot ${node.level}"></span></header>
          <dl>
            <dt>类型</dt><dd>${escapeHtml(topologyNodeKindLabel(node.kind))}</dd>
            <dt>IP</dt><dd>${escapeHtml(node.ip || "—")}</dd>
            <dt>状态</dt><dd>${node.success === undefined ? "无数据" : (node.success ? "在线" : "离线")}</dd>
            <dt>延迟</dt><dd>${Number.isFinite(node.latency) ? formatPingText(node.latency) : "—"}</dd>
          </dl>
          <div class="topology-detail-actions">
            ${node.ip ? `<a class="detail-link" href="/latency?ip=${encodeURIComponent(node.ip)}">延迟证据</a>` : ""}
            <a class="detail-link" href="/incident?at=${encodeURIComponent(dateTimeInputValue(new Date()))}&window=5&threshold=0.05">事故分析</a>
            ${syslogUrl ? `<a class="detail-link" href="${escapeHtml(syslogUrl)}">Syslog</a>` : ""}
          </div>
        `;
      };
      el.addEventListener("click", handler);
      el.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          handler(event);
        }
      });
    });
  }

  const topoView = { scale: 1, x: 0, y: 0 };

  function applyTopoView() {
    const canvas = document.getElementById("topologyCanvas");
    const svg = canvas && canvas.querySelector(".topology-svg");
    if (!svg) return;
    const baseWidth = Number(svg.dataset.baseWidth || 0);
    const baseHeight = Number(svg.dataset.baseHeight || 0);
    if (!baseWidth || !baseHeight) return;
    const viewWidth = baseWidth / topoView.scale;
    const viewHeight = baseHeight / topoView.scale;
    svg.setAttribute("viewBox", `${topoView.x} ${topoView.y} ${viewWidth} ${viewHeight}`);
  }

  function resetTopoView() {
    topoView.scale = 1;
    topoView.x = 0;
    topoView.y = 0;
    applyTopoView();
  }

  // Drag to pan, wheel to zoom. Bound once on the canvas container so it
  // survives the 10s re-render; the transform itself is re-applied each refresh.
  function setupTopoPanZoom() {
    const canvas = document.getElementById("topologyCanvas");
    if (!canvas || canvas.dataset.panzoom === "1") return;
    canvas.dataset.panzoom = "1";

    let pointerDown = false;
    let dragging = false;
    let moved = false;
    let startX = 0;
    let startY = 0;
    let originX = 0;
    let originY = 0;
    let originScale = 1;
    let activePointer = null;

    canvas.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) return;
      pointerDown = true;
      dragging = false;
      moved = false;
      startX = event.clientX;
      startY = event.clientY;
      originX = topoView.x;
      originY = topoView.y;
      originScale = topoView.scale;
      activePointer = event.pointerId;
      // Don't capture or preventDefault yet — a plain click must still reach the node.
    });

    canvas.addEventListener("pointermove", (event) => {
      if (!pointerDown) return;
      const dx = event.clientX - startX;
      const dy = event.clientY - startY;
      if (!dragging && (Math.abs(dx) > 4 || Math.abs(dy) > 4)) {
        dragging = true;
        moved = true;
        canvas.classList.add("topology-grabbing");
        try { canvas.setPointerCapture(activePointer); } catch (e) {}
      }
      if (!dragging) return;
      const svg = canvas.querySelector(".topology-svg");
      const baseWidth = Number(svg && svg.dataset.baseWidth || 0);
      const baseHeight = Number(svg && svg.dataset.baseHeight || 0);
      const rect = canvas.getBoundingClientRect();
      if (!baseWidth || !baseHeight || !rect.width || !rect.height) return;
      topoView.x = originX - dx * (baseWidth / originScale) / rect.width;
      topoView.y = originY - dy * (baseHeight / originScale) / rect.height;
      applyTopoView();
    });

    const endDrag = () => {
      if (!pointerDown) return;
      pointerDown = false;
      if (dragging) {
        canvas.classList.remove("topology-grabbing");
        try { canvas.releasePointerCapture(activePointer); } catch (e) {}
      }
      dragging = false;
    };
    canvas.addEventListener("pointerup", endDrag);
    canvas.addEventListener("pointercancel", endDrag);

    // If the pointer actually dragged, swallow the trailing click so it neither
    // clears the detail panel nor opens a node.
    canvas.addEventListener("click", (event) => {
      if (moved) {
        event.stopPropagation();
        moved = false;
      }
    }, true);

    canvas.addEventListener("wheel", (event) => {
      event.preventDefault();
      const rect = canvas.getBoundingClientRect();
      const svg = canvas.querySelector(".topology-svg");
      const baseWidth = Number(svg && svg.dataset.baseWidth || 0);
      const baseHeight = Number(svg && svg.dataset.baseHeight || 0);
      if (!baseWidth || !baseHeight || !rect.width || !rect.height) return;
      const cx = event.clientX - rect.left;
      const cy = event.clientY - rect.top;
      const viewWidth = baseWidth / topoView.scale;
      const viewHeight = baseHeight / topoView.scale;
      const focusX = topoView.x + (cx / rect.width) * viewWidth;
      const focusY = topoView.y + (cy / rect.height) * viewHeight;
      const factor = event.deltaY < 0 ? 1.12 : 1 / 1.12;
      const next = Math.min(4, Math.max(0.3, topoView.scale * factor));
      topoView.scale = next;
      topoView.x = focusX - (cx / rect.width) * (baseWidth / topoView.scale);
      topoView.y = focusY - (cy / rect.height) * (baseHeight / topoView.scale);
      applyTopoView();
    }, { passive: false });

    canvas.addEventListener("dblclick", resetTopoView);
    // Belt-and-suspenders: stop the browser from drag-selecting the SVG labels.
    canvas.addEventListener("selectstart", (event) => event.preventDefault());
    canvas.addEventListener("dragstart", (event) => event.preventDefault());
  }

  async function refreshTopology() {
    const canvas = document.getElementById("topologyCanvas");
    if (!canvas) return;
    const seq = ++topologySeq;
    try {
      const [allTargets, edges, seenItems] = await Promise.all([
        fetchTopologyTargets(),
        fetchTopologyEdges(),
        prometheusInstant(activeInfraPingQuery()).catch(() => [])
      ]);
      if (seq !== topologySeq) return;
      // 与网络总览一致：隐藏从没上线过的设备（按 instance 名匹配 seen-up 集合）。
      // 物理防火墙成员由 SNMP up 提供，不属于 ping seen-up 集合，必须保留。
      const seenUp = activeSeriesNames(seenItems);
      const targets = seenUp.size
        ? allTargets.filter((t) => t.job === "infra-fw-unit-snmp" || seenUp.has(t.instance))
        : allTargets;
      const layers = buildTopologyLayers(targets);
      const containerWidth = Math.max(640, canvas.clientWidth || 1200);
      const height = Math.max(420, canvas.clientHeight || 680);
      // Lay the graph out at its natural width so a long row of access switches
      // doesn't get squeezed/overlapped; pan & zoom let you explore the rest.
      const maxRow = Math.max(
        layers.isps.length, layers.firewalls.length,
        layers.cores.length, layers.servers.length, layers.dists.length, 1
      );
      const width = Math.max(containerWidth, maxRow * 152 + 48);
      const layout = topologyLayout(layers, width, height, edges);
      topologyNodes = layout.nodes;
      if (shouldRender("topology", topologySignature(layout, width, edges))) {
        canvas.innerHTML = renderTopologySvg(layout, width);
        bindTopologyNodeEvents();
        setupTopoPanZoom();
        applyTopoView();
      } else {
        // Same structure and status levels: refresh only the latency readouts
        // in place, keeping the pan/zoom view and skipping the SVG rebuild.
        updateTopologyLatencyTexts(canvas);
      }
      document.getElementById("topologyUpdated").textContent = `刷新于 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })} · 拖动平移·滚轮缩放·双击复位${edges.length ? ` · LLDP ${edges.length} 条边` : " · LLDP 未发现邻居"}`;
      lastDataSuccessAt = Date.now();
    } catch (error) {
      if (seq !== topologySeq) return;
      // The error message replaces the SVG, so the next success must rebuild
      // even when the data signature is unchanged.
      renderSignatures.delete("topology");
      console.error("Topology fetch failed:", error);
      canvas.innerHTML = `<div class="topology-error">拓扑数据拉取失败: ${escapeHtml(error.message || "")}</div>`;
    }
  }

  // Skip the SVG rebuild when nothing the layout depends on changed: node set,
  // kinds, names, status levels, the LLDP edge list and the canvas width. Raw
  // latency is excluded on purpose -- it jitters every sample and is patched
  // into the existing DOM by updateTopologyLatencyTexts instead.
  function topologySignature(layout, width, edges) {
    const nodesSig = layout.nodes.map((node) => `${node.kind}|${node.ip || ""}|${node.name}|${node.level}`).join("#");
    const edgesSig = (edges || []).map((edge) => `${edge.from_ip}|${edge.from_port}|${edge.to_ip}|${edge.to_port}`).join("#");
    return `${width}@${nodesSig}@@${edgesSig}`;
  }

  function updateTopologyLatencyTexts(canvas) {
    canvas.querySelectorAll(".topology-node").forEach((el) => {
      const node = topologyNodes[Number(el.dataset.idx)];
      const text = el.querySelector(".topology-node-latency");
      if (!node || !text) return;
      text.textContent = Number.isFinite(node.latency)
        ? formatPingText(node.latency)
        : (node.kind === "isp" && node.success === true ? "在线" : "");
    });
  }

  function startTopologyRefresh() {
    stopTopologyRefresh();
    refreshTopology();
    topologyTimer = window.setInterval(refreshTopology, 10000);
  }

  function showTopology() {
    const screen = document.querySelector(".screen");
    stopInfraRefresh();
    stopTournamentRefresh();
    stopOpsRefresh();
    stopControlRefresh();
    screen.className = "screen topology-mode";
    setVisible("homePanel", false);
    setVisible("panelGrid", false);
    setVisible("tournamentPanel", false);
    setVisible("evidencePanel", false);
    setVisible("opsPanel", false);
    setVisible("controlPanel", false);
    setVisible("incidentPanel", false);
    setVisible("topologyPanel", true);
    const detail = document.getElementById("topologyDetail");
    detail.hidden = true;
    detail.innerHTML = `<div class="topology-empty">点击任意节点查看详情</div>`;
    resetTopoView();
    startTopologyRefresh();
  }

  function renderPage() {
    const page = pageFromPath();
    renderHeader(page);
    renderNav(page);
    const routeKey = `${page.id}${window.location.search}`;
    if (routeKey === activeRoute) return;
    activePageId = page.id;
    activeRoute = routeKey;
    if (page.id === "home") {
      showHome();
    } else if (page.id === "control") {
      showControl();
    } else if (page.id === "evidence") {
      showEvidence();
    } else if (page.id === "incident") {
      showIncident();
    } else if (page.id === "topology") {
      showTopology();
    } else if (page.id === "wireless" || page.id === "seat-check") {
      showOps(page);
    } else if (page.kind) {
      showTournament(page);
    } else {
      showInfra();
    }
  }

  function anyRefreshActive() {
    return Boolean(gaugeTimer || chartTimer || tournamentTimer || opsTimer || controlTimer || topologyTimer);
  }

  // Warn when the active page's polling loop hasn't produced fresh data for a
  // while (network stall, Prometheus down, or a frozen refresh loop), so a
  // stale screen is never mistaken for live data.
  function updateFreshness() {
    const badge = document.getElementById("dataFreshness");
    if (!badge) return;
    const stale = anyRefreshActive() && lastDataSuccessAt > 0 && (Date.now() - lastDataSuccessAt) > DATA_STALE_AFTER_MS;
    if (!stale) {
      badge.hidden = true;
      return;
    }
    const since = new Date(lastDataSuccessAt).toLocaleTimeString("zh-CN", { hour12: false });
    badge.textContent = `⚠ 数据可能过期 · 上次更新 ${since}`;
    badge.hidden = false;
  }

  // Intl.DateTimeFormat construction is comparatively heavy; build the clock
  // formatters once instead of twice a second.
  const clockDateFormat = new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    weekday: "short"
  });
  const clockTimeFormat = new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false
  });

  function tick() {
    try {
      const now = new Date();
      setText("dateText", clockDateFormat.format(now));
      setText("timeText", clockTimeFormat.format(now));
      updateFreshness();
    } catch (e) {
      // ignore — will retry next second
    }
  }

  renderPage();
  tick();
  window.setInterval(tick, 1000);
  window.addEventListener("popstate", renderPage);
  // Charts are sized from the container, so a resize must force a full repaint
  // even when the underlying data is unchanged. Repaint right after the drag
  // settles instead of waiting for the next 5s tick -- the range cache makes
  // the extra refresh nearly free.
  let resizeRepaintTimer = null;
  window.addEventListener("resize", () => {
    renderSignatures.clear();
    if (resizeRepaintTimer) window.clearTimeout(resizeRepaintTimer);
    resizeRepaintTimer = window.setTimeout(() => {
      resizeRepaintTimer = null;
      if (chartTimer) refreshCharts();
      if (tournamentTimer) {
        const current = activePage();
        if (current && current.kind) refreshTournament(current);
      }
      if (topologyTimer) refreshTopology();
    }, 200);
  });
})();

