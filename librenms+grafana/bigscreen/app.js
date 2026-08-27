(function () {
  const config = window.BIGSCREEN_CONFIG || {};
  const queries = window.BIGSCREEN_QUERIES || {};
  const pingTrendQuery = queries.pingTrend || "";
  const pingSuccessTrendQuery = queries.pingSuccessTrend || "";
  const pingGaugeQuery = queries.pingGauge || "";
  const uptimeQuery = queries.uptime || "";
  const lossQuery = queries.loss || "";
  // Keep one scrape interval of tolerance without presenting a player who
  // disconnected a minute ago as still online.
  const playerSnapshotWindow = "15s";
  const playerOfflineGraceWindow = "5m";
  const seriesColors = ["#73d17a", "#ffe32d", "#5b8ff9", "#ff9f43", "#ff4d66", "#b877db", "#40c4ff", "#b8e986", "#f8e71c"];
  const pages = window.BIGSCREEN_PAGES || [];
  const teamLayouts = window.BIGSCREEN_TEAM_LAYOUTS || {};

  // Pure helpers live in utils.js, the Prometheus/data layer in api.js and the
  // topology layout/SVG pipeline in topology.js (all loaded before this file).
  const {
    escapeHtml, escapeRegex, escapeLabel, metricName, formatPing, formatPingText,
    formatUptime, formatBits, formatTime, niceMax, roundUpToStep, average,
    networkLabel, seatLabel, gaugeColor, gaugePercent,
    linePathFromPoints, stepPathFromPoints, splitPointsOnGaps,
    seriesSignature, lineSeriesStats, lineSeriesHasTimeline,
    lineSeriesCurrentDisplay, lineFailurePoints,
    buildCsv, formatTimestampFull, groupAddressesByCBlock
  } = window.BSUtils;
  const { createConfigEditor } = window.BSConfigEditor;
  const { createDhcpPanel } = window.BSDhcpPanel;
  const { createLineChartRenderer } = window.BSLineChart;
  const { createPingChartRenderer } = window.BSPingChart;
  const { createLossHeatmapRenderer } = window.BSLossHeatmap;
  const { createIspChartRenderer } = window.BSIspChart;
  const { createEvidenceChartRenderer } = window.BSEvidenceChart;
  const { createEvidencePanel } = window.BSEvidencePanel;
  const { createTopologyPanel } = window.BSTopologyPanel;
  const { buildInfrastructurePingPresentation } = window.BSPingTransform;
  const {
    prometheusBaseUrl, fetchWithTimeout,
    prometheusQuery, prometheusInstant, prometheusRangeFor,
    prometheusRangeCached, invalidateRangeCache,
    activeInfraPingQuery, activeSeriesNames, filterSeriesByNames,
    fetchIspNames, ispTrafficQuery, fetchIspTraffic, ispCapacityBps, ispChartMaxBps,
    fetchInfraDeviceNames, renameListWithInfraMap, partitionInfraPingItems,
    fetchTopologyTargets, fetchTopologyEdges, fetchRuntimeStatus,
    fetchPlatformAuthStatus, loginPlatformAuth, changePlatformPassword, logoutPlatformAuth,
    fetchPlatformConfig, fetchPlatformVersion, fetchApplyStatus, postPlatform, fetchIperfStatus, fetchIperfHistory, fetchRetirePending, patchPlatform, fetchIncidents,
    fetchDhcpDashboard, fetchDhcpBindings, testDhcpConnection, fetchDhcpSettings, saveDhcpSettings
  } = window.BSApi;
  const {
    buildTopologyLayers, topologyLayout, renderTopologySvg, topologyNodeKindLabel,
    topologyLatencyIp
  } = window.BSTopology;
  const {
    isGatewayAddress, buildPlayers, latencyLevel, playerStatusText
  } = window.BSPlayers;
  const { analyzeIncident } = window.BSIncident;
  const {
    readinessScore,
    summarizePlayers, summarizeTargets, summarizeServices,
    buildConfigRisks, buildTopologyFindings, buildReadinessChecks,
    lintSwitchScene,
    APPLY_REQUEST_TIMEOUT_MS,
    waitForApplyRecovery,
    applyRecoveryRenderPayload
  } = window.BSPlatform;
  const { createIspCarousel } = window.BSIspCarousel;
  const {
    DEFAULT_CUSTOM_PRESET,
    resultView: iperfResultView,
    historyHtml: iperfHistoryHtml,
    loadServerConfig,
    presetView: iperfPresetView
  } = window.BSIperf;
  let gaugeTimer = null;
  let chartTimer = null;
  let seenUpTimer = null;
  let infraSeenUp = null;  // Set of "deployed" (ever-online) infra instance names; null/empty = show all
  let infraCurrentTargets = null; // Current Prometheus targets; removes retired ISP/history series immediately
  let tournamentTimer = null;
  let wirelessTimer = null;
  let controlTimer = null;
  let activePageId = "";
  let activeRoute = "";
  let gaugeSeq = 0;
  let chartSeq = 0;
  let tournamentSeq = 0;
  let topologySeq = 0;
  let stageDeviceRegexCache = null;
  let ispTrafficResults = [];
  const renderSignatures = new Map();
  let lastDataSuccessAt = 0;
  let lastControlReport = null;
  let lastControlAuth = null;
  let lastIncidents = [];
  const DATA_STALE_AFTER_MS = 20000;
  const CONTROL_LAYOUT_STORAGE_KEY = "bigscreen.controlLayout.v1";
  const ispCarousel = createIspCarousel({
    pageSize: 2,
    intervalMs: 10000,
    setIntervalFn: (callback, delay) => window.setInterval(callback, delay),
    clearIntervalFn: (handle) => window.clearInterval(handle),
    onPageChange: () => renderIspPanels(ispTrafficResults)
  });

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
    const configured = String(config.stageDeviceFilter || "stage,wutai,舞台")
      .split(",")
      .map((name) => name.trim())
      .filter(Boolean)
      .map(escapeRegex)
      .join("|") || "stage|wutai|舞台";
    // Some event access switches are deliberately named Lan-Server/Server-SW.
    // At this point real servers have already been separated into serverPing,
    // so "server" safely widens only the network-device competition filter.
    return `${configured}|server`;
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

  function configuredTournamentPage(page) {
    if (!page || typeof teamLayouts.applyTeamOrder !== "function") return page;
    return teamLayouts.applyTeamOrder(page, config.teamOrders);
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

  function renderGaugeGrid(containerId, items, kind, forceRows, forceColumns) {
    const container = document.getElementById(containerId);
    const formatter = kind === "ping" ? formatPing : formatUptime;
    const rows = forceRows
      ? Math.max(1, Math.min(items.length, forceRows))
      : Math.max(1, Math.min(items.length, items.length > 8 ? 3 : 2));
    const columns = forceColumns
      ? Math.max(1, forceColumns)
      : Math.max(1, Math.ceil(items.length / rows));
    const layout = { rows, columns };
    container.dataset.rows = String(rows);
    container.style.setProperty("--gauge-columns", String(columns));
    container.style.setProperty("--gauge-rows", String(rows));
    container.innerHTML = "";

    if (!items.length) {
      container.innerHTML = '<div class="empty-state">暂无数据</div>';
      return layout;
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
    return layout;
  }

  function renderNoData(container, message) {
    container.innerHTML = `<div class="no-data">${message || "暂无数据"}</div>`;
  }

  const renderLineChart = createLineChartRenderer({
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
  });
  const renderPingChart = createPingChartRenderer({
    renderLineChart,
    estimateStepSeconds,
    formatPingText
  });
  const renderLossHeatmap = createLossHeatmapRenderer({
    document,
    renderNoData,
    formatTime,
    escapeHtml
  });
  const renderIspChart = createIspChartRenderer({
    renderLineChart,
    formatBits,
    ispChartMaxBps
  });
  const renderEvidenceCharts = createEvidenceChartRenderer({
    document,
    renderLineChart,
    formatPingText,
    estimateStepSeconds,
    average,
    escapeHtml
  });
  const evidencePanel = createEvidencePanel({
    document,
    window,
    Blob,
    URL,
    URLSearchParams,
    Date,
    console,
    escapeLabel,
    escapeRegex,
    networkLabel,
    formatTime,
    formatTimestampFull,
    buildCsv,
    dateTimeInputValue,
    playerLabel,
    renderNoData,
    prometheusInstant,
    prometheusRangeFor,
    renderEvidenceCharts
  });
  const topologyPanel = createTopologyPanel({
    document,
    location: window.location,
    buildTopologyLayers,
    topologyLayout,
    renderTopologySvg,
    topologyNodeKindLabel,
    topologyLatencyIp,
    escapeHtml,
    formatPingText
  });
  const dhcpPanel = createDhcpPanel({
    document,
    window,
    model: window.BSDhcpModel,
    escapeHtml,
    groupAddressesByCBlock,
    setText,
    fetchDhcpDashboard,
    fetchDhcpBindings,
    isPageActive: () => activePageId === "dhcp",
    onDataSuccess: () => { lastDataSuccessAt = Date.now(); }
  });
  const configEditor = createConfigEditor({
    document,
    window,
    HTMLInputElement: window.HTMLInputElement,
    pages,
    teamLayouts,
    escapeHtml,
    controlItemHtml,
    model: window.BSConfigModel,
    fetchPlatformConfig,
    fetchApplyStatus,
    postPlatform,
    saveDhcpSettings,
    testDhcpConnection,
    waitForApplyRecovery,
    applyRecoveryRenderPayload,
    applyRequestTimeoutMs: APPLY_REQUEST_TIMEOUT_MS,
    onRefresh: refreshControlPanel
  });

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

  function renderIspPanels(results) {
    const ispGrid = document.getElementById("ispGrid");
    const compactTournamentChart = document.querySelector(".screen")?.classList.contains("tournament-mode");
    ispTrafficResults = Array.isArray(results) ? results : [];
    const pageState = ispCarousel.updateTotal(ispTrafficResults.length);
    const visibleResults = compactTournamentChart
      ? ispCarousel.visibleItems(ispTrafficResults)
      : ispTrafficResults;
    const firstResultIndex = compactTournamentChart ? pageState.start : 0;

    ispGrid.classList.toggle("isp-paged", compactTournamentChart && pageState.pageCount > 1);
    ispGrid.style.setProperty("--isp-count", String(Math.max(1, visibleResults.length)));
    ispGrid.innerHTML = "";
    if (!ispTrafficResults.length) {
      renderNoData(ispGrid);
      return;
    }
    const fragment = document.createDocumentFragment();
    if (compactTournamentChart && pageState.pageCount > 1) {
      const pager = document.createElement("nav");
      pager.className = "isp-pager";
      pager.setAttribute("aria-label", "ISP 图表翻页");
      pager.innerHTML = `
        <button type="button" class="isp-page-button isp-page-previous" aria-label="上一页" ${pageState.canPrevious ? "" : "disabled"}>‹</button>
        <span class="isp-page-status">${pageState.pageNumber} / ${pageState.pageCount}</span>
        <button type="button" class="isp-page-button isp-page-next" aria-label="下一页" ${pageState.canNext ? "" : "disabled"}>›</button>
      `;
      pager.querySelector(".isp-page-previous").addEventListener("click", () => ispCarousel.move(-1));
      pager.querySelector(".isp-page-next").addEventListener("click", () => ispCarousel.move(1));
      fragment.appendChild(pager);
    }
    visibleResults.forEach((result, visibleIndex) => {
      const resultIndex = firstResultIndex + visibleIndex;
      const panel = document.createElement("section");
      panel.className = "chart-panel isp-panel";
      panel.innerHTML = `<h2>${escapeHtml(result.name)}</h2><div class="chart-body" id="ispChart${resultIndex}"></div>`;
      fragment.appendChild(panel);
    });
    ispGrid.appendChild(fragment);
    visibleResults.forEach((result, visibleIndex) => {
      const resultIndex = firstResultIndex + visibleIndex;
      renderIspChart({
        containerId: `ispChart${resultIndex}`,
        result,
        resultIndex,
        compactTournamentChart
      });
    });
  }

  function teamName(page, team) {
    const teamNumber = Number(team);
    if (page.id === "match-5v5") {
      if (teamNumber === 1) return "舞台左";
      if (teamNumber === 2) return "舞台右";
    }
    return `第 ${teamNumber} 队`;
  }

  function tournamentSelector(page, network = "wired") {
    const networkFilter = network === "all" ? 'network=~".*"' : `network="${escapeLabel(network)}"`;
    const teamRegex = (page.teams || []).join("|");
    const teamFilter = teamRegex ? `,team=~"${teamRegex}"` : "";
    const seatRegex = page.teamSize ? Array.from({ length: page.teamSize }, (_, index) => index + 1).join("|") : "";
    const seatFilter = seatRegex ? `,seat=~"${seatRegex}"` : "";
    return `role="player",${networkFilter}${teamFilter}${seatFilter}`;
  }

  function renderTournamentSummary(page, players) {
    const online = players.filter((player) => player.success).length;
    const high = players.filter((player) => player.success && Number.isFinite(player.latency) && player.latency >= 0.08).length;
    const total = players.length;
    const offline = Math.max(0, total - online);
    const values = [
      ["在线", online, "good"],
      ["离线", offline, offline ? "bad" : "good"],
      ["高延迟", high, high ? "warn" : "good"],
      ["总计", total, "info"]
    ];
    document.getElementById("tournamentSummary").innerHTML = values.map(([label, value, level]) => `
      <div class="tournament-kpi ${level}">
        <span>${escapeHtml(label)}</span>
        <strong>${escapeHtml(value)}</strong>
      </div>
    `).join("");
  }

  function playersByTeam(players) {
    const grouped = new Map();
    players.forEach((player) => {
      if (!grouped.has(player.team)) grouped.set(player.team, []);
      grouped.get(player.team).push(player);
    });
    return grouped;
  }

  function expectedSeats(page, teamPlayers) {
    if (page.teamSize) {
      return page.teamSize;
    }
    return Math.max(0, ...teamPlayers.map((player) => player.seat));
  }

  function latencyUrlForPlayer(player) {
    const params = new URLSearchParams({
      team: String(player.team),
      seat: String(player.seat),
      network: player.network || "wired"
    });
    if (player.ip) params.set("ip", player.ip);
    return `/latency?${params.toString()}`;
  }

  function renderSeatSlot(player, seat) {
    if (!player) {
      return `
        <div class="seat-slot empty">
          <span>${seatLabel(seat)}</span>
          <strong>-</strong>
          <em>未连接</em>
        </div>
      `;
    }
    const level = latencyLevel(player);
    const latency = Number.isFinite(player.latency) ? formatPingText(player.latency) : "-";
    const ipShort = player.ip ? "." + player.ip.split(".").pop() : "";
    return `
      <a class="seat-slot ${level}" href="${escapeHtml(latencyUrlForPlayer(player))}" title="${escapeHtml(playerLabel(player.team, player.seat, player.network))} ${escapeHtml(player.ip)}">
        <span>${seatLabel(player.seat)}</span>
        <strong>${escapeHtml(latency)}</strong>
        <em>${escapeHtml(ipShort)}</em>
      </a>
    `;
  }

  function renderTeamCard(page, team, teamPlayers) {
    const seatCount = expectedSeats(page, teamPlayers);
    const visiblePlayers = teamPlayers.filter((player) => player.seat >= 1 && player.seat <= seatCount);
    const bySeat = new Map(visiblePlayers.map((player) => [player.seat, player]));
    const seats = Array.from({ length: seatCount }, (_, index) => index + 1);
    const online = visiblePlayers.filter((player) => player.success).length;
    const latencies = visiblePlayers
      .filter((player) => player.success && Number.isFinite(player.latency))
      .map((player) => player.latency);
    const avg = latencies.length ? formatPingText(average(latencies)) : "-";
    return `
      <article class="team-card">
        <header>
          <h3>${escapeHtml(teamName(page, team))}</h3>
          <span>${online}/${seatCount}</span>
        </header>
        <div class="team-avg">${escapeHtml(avg)}</div>
        <div class="seat-grid">
          ${seats.map((seat) => renderSeatSlot(bySeat.get(seat), seat)).join("")}
        </div>
      </article>
    `;
  }

  function renderTournamentBoard(page, players) {
    const grouped = playersByTeam(players);
    const board = document.getElementById("tournamentBoard");
    if (page.kind === "match") {
      board.className = "tournament-board match-board";
      board.innerHTML = [1, 2].map((team) => renderTeamCard(page, team, grouped.get(team) || [])).join('<div class="versus">VS</div>');
      return;
    }

    board.className = `tournament-board team-board ${page.id}`;
    board.innerHTML = (page.groups || [page.teams || []]).map((group) => `
      <div class="team-row" style="--team-count:${group.length}">
        ${group.map((team) => renderTeamCard(page, team, grouped.get(team) || [])).join("")}
      </div>
    `).join("");
  }

  function tournamentTrendQuery(page) {
    const selector = tournamentSelector(page);
    return `avg by (team,seat) (probe_icmp_duration_seconds{${selector},phase="rtt"})`;
  }

  function playerLatencySnapshotQuery(selector) {
    const retained = `(max_over_time(probe_success{${selector}}[${playerOfflineGraceWindow}]) == 1)`;
    return `avg_over_time(probe_icmp_duration_seconds{${selector},phase="rtt"}[${playerSnapshotWindow}]) and on(instance,team,seat,network) ${retained}`;
  }

  function playerSuccessSnapshotQuery(selector) {
    const retained = `(max_over_time(probe_success{${selector}}[${playerOfflineGraceWindow}]) == 1)`;
    return `last_over_time(probe_success{${selector}}[${playerSnapshotWindow}]) and on(instance,team,seat,network) ${retained}`;
  }

  function renderTournamentTrend(page, trendSeries) {
    const container = document.getElementById("tournamentTrendChart");

    if (page.trendMode === "per-seat") {
      renderTournamentTrendPerSeat(page, trendSeries, container);
      return;
    }
    if (page.trendMode === "groups") {
      renderTournamentTrendByGroups(page, trendSeries, container);
      return;
    }
    renderTournamentTrendFlat(page, trendSeries, container);
  }

  function renderTeamTrendCard(page, team, trendSeries) {
    const teamSeries = trendSeries.filter((item) => String(item.metric.team || "") === String(team));
    const latestValues = teamSeries
      .map((item) => item.values[item.values.length - 1])
      .filter(Boolean)
      .map((point) => point.v);
    const latest = latestValues.length ? formatPingText(average(latestValues)) : "-";
    return `
      <section class="team-trend-card">
        <header><h3>${escapeHtml(teamName(page, team))}</h3><span>${escapeHtml(latest)}</span></header>
        <div class="team-trend-chart" id="teamTrend${team}"></div>
      </section>
    `;
  }

  function renderTeamSparklines(page, trendSeries) {
    (page.teams || []).forEach((team) => {
      const teamSeries = trendSeries
        .filter((item) => String(item.metric.team || "") === String(team))
        .sort((a, b) => Number(a.metric.seat || 0) - Number(b.metric.seat || 0))
        .map((item) => ({ ...item, name: seatLabel(item.metric.seat || "?") }));
      renderSparkline(`teamTrend${team}`, teamSeries);
    });
  }

  function renderTournamentTrendFlat(page, trendSeries, container) {
    const teams = page.teams || [];
    container.innerHTML = `
      <div class="team-trend-grid" style="--trend-team-count:${teams.length}">
        ${teams.map((team) => renderTeamTrendCard(page, team, trendSeries)).join("")}
      </div>
    `;
    renderTeamSparklines(page, trendSeries);
  }

  function renderTournamentTrendByGroups(page, trendSeries, container) {
    const groups = page.groups || [page.teams || []];
    container.innerHTML = `
      <div class="team-trend-stack">
        ${groups.map((group) => `
          <div class="team-trend-grid" style="--trend-team-count:${group.length}">
            ${group.map((team) => renderTeamTrendCard(page, team, trendSeries)).join("")}
          </div>
        `).join("")}
      </div>
    `;
    renderTeamSparklines(page, trendSeries);
  }

  function renderTournamentTrendPerSeat(page, trendSeries, container) {
    const teams = page.teams || [];
    const seatCount = page.teamSize || 1;
    const seats = Array.from({ length: seatCount }, (_, i) => i + 1);
    const cardId = (team, seat) => `seatTrend_${team}_${seat}`;
    container.innerHTML = `
      <div class="team-trend-stack-horizontal">
        ${teams.map((team) => `
          <div class="team-trend-grid team-trend-grid-vertical" style="--trend-seat-count:${seatCount}">
            ${seats.map((seat) => {
              const series = trendSeries.find(
                (item) =>
                  String(item.metric.team || "") === String(team) &&
                  String(item.metric.seat || "") === String(seat)
              );
              const latest = series && series.values.length
                ? formatPingText(series.values[series.values.length - 1].v)
                : "-";
              return `
                <section class="team-trend-card">
                  <header><h3>${escapeHtml(teamName(page, team))} ${escapeHtml(seatLabel(seat))}</h3><span>${escapeHtml(latest)}</span></header>
                  <div class="team-trend-chart" id="${cardId(team, seat)}"></div>
                </section>
              `;
            }).join("")}
          </div>
        `).join("")}
      </div>
    `;
    teams.forEach((team) => {
      seats.forEach((seat) => {
        const series = trendSeries
          .filter((item) =>
            String(item.metric.team || "") === String(team) &&
            String(item.metric.seat || "") === String(seat)
          )
          .map((item) => ({ ...item, name: seatLabel(seat) }));
        renderSparkline(cardId(team, seat), series);
      });
    });
  }

  async function refreshTournament(page) {
    page = configuredTournamentPage(page);
    const seq = ++tournamentSeq;
    try {
      const selector = tournamentSelector(page);
      const [latencyItems, successItems, trendSeries] = await Promise.all([
        prometheusInstant(playerLatencySnapshotQuery(selector)),
        prometheusInstant(playerSuccessSnapshotQuery(selector)),
        prometheusRangeCached(tournamentTrendQuery(page), (metric) => {
          return `${teamName(page, metric.team)} ${seatLabel(metric.seat || "?")}`;
        })
      ]);
      if (seq !== tournamentSeq) return;
      const players = buildPlayers(latencyItems, successItems)
        .filter((player) => !page.teamSize || player.seat <= page.teamSize);
      renderTournamentSummary(page, players);
      renderTournamentBoard(page, players);
      if (shouldRender("tournamentTrend", seriesSignature(trendSeries))) {
        renderTournamentTrend(page, trendSeries);
      }
      lastDataSuccessAt = Date.now();
    } catch (error) {
      if (seq !== tournamentSeq) return;
      renderSignatures.delete("tournamentTrend");
      renderNoData(document.getElementById("tournamentBoard"), "暂无选手数据");
      renderNoData(document.getElementById("tournamentTrendChart"));
      console.error(error);
    }
  }

  // Refresh the slowly-changing infrastructure "deployed" set on its own timer
  // so its long-window query does not run every 5s. Keep it on API failure.
  async function refreshInfraSeenUp() {
    try {
      const [seenItems, currentTargets] = await Promise.all([
        prometheusInstant(activeInfraPingQuery()),
        fetchTopologyTargets()
      ]);
      infraSeenUp = activeSeriesNames(seenItems);
      infraCurrentTargets = new Set();
      currentTargets.forEach((target) => {
        [target.instance, target.targetIp, target.displayName]
          .map((value) => String(value || "").trim())
          .filter(Boolean)
          .forEach((value) => infraCurrentTargets.add(value));
      });
    } catch (error) {
      // transient failure: keep the previous set
    }
  }

  // Drop infra items/series that have never been online (configured-but-absent
  // ping targets). Falls back to showing all until the set is known or empty.
  function filterDeployed(list, getName) {
    return list.filter((entry) => {
      const name = getName(entry);
      if (infraCurrentTargets && infraCurrentTargets.size && !infraCurrentTargets.has(name)) return false;
      if (infraSeenUp && infraSeenUp.size && !infraSeenUp.has(name)) return false;
      return true;
    });
  }

  async function refreshGauges() {
    const seq = ++gaugeSeq;
    try {
      const [pingItems, uptimeItems, switchSnmpItems] = await Promise.all([
        prometheusQuery(pingGaugeQuery),
        prometheusQuery(uptimeQuery),
        prometheusInstant('last_over_time(up{job="infra-switch-snmp"}[25m])').catch(() => [])
      ]);
      const nameMap = await fetchInfraDeviceNames();
      if (seq !== gaugeSeq) return;
      const deployed = filterDeployed(pingItems, (item) => item.name);
      const classified = partitionInfraPingItems(
        renameListWithInfraMap(deployed, nameMap),
        switchSnmpItems
      );
      const networkPing = dedupeInfraItems(classified.network, "max");
      const serverPing = dedupeInfraItems(classified.servers, "max");
      const visibleNetworkPing = visibleInfraItems(networkPing);
      const networkLayout = renderGaugeGrid("pingGaugeGrid", visibleNetworkPing, "ping");
      // Servers aren't stage devices (skip the stage filter). Keep their cells
      // the same width as the network-device cells instead of stretching one
      // server across the entire panel.
      renderGaugeGrid(
        "pingServerGaugeGrid",
        serverPing,
        "ping",
        1,
        Math.max(networkLayout.columns, serverPing.length)
      );
      // 没有服务器 ping 数据就整段隐藏，不显示"服务器 暂无数据"。
      setVisible("serverGaugesWrap", serverPing.length > 0);
      renderGaugeGrid("uptimeGaugeGrid", visibleInfraItems(dedupeInfraItems(renameListWithInfraMap(uptimeItems, nameMap), "max")), "uptime");
      lastDataSuccessAt = Date.now();
    } catch (error) {
      if (seq !== gaugeSeq) return;
      renderGaugeGrid("pingGaugeGrid", [], "ping");
      renderGaugeGrid("pingServerGaugeGrid", [], "ping");
      setVisible("serverGaugesWrap", false);
      renderGaugeGrid("uptimeGaugeGrid", [], "uptime");
      console.error(error);
    }
  }

  async function refreshCharts() {
    const seq = ++chartSeq;
    try {
      const [pingSeries, pingSuccessSeries, lossSeries, ispTraffic] = await Promise.all([
        // Fetch the raw 2-second infrastructure RTT and success probes.
        // prometheusRangeCached keys by query + step, so these remain
        // independent caches even though their evaluation grids are aligned.
        prometheusRangeCached(pingTrendQuery, metricName, 2),
        prometheusRangeCached(pingSuccessTrendQuery, metricName, 2),
        prometheusRangeCached(lossQuery),
        fetchIspTraffic()
      ]);
      const nameMap = await fetchInfraDeviceNames();
      if (seq !== chartSeq) return;
      const rawActivePingSeries = visibleInfraSeries(mergeInfraSeries(renameListWithInfraMap(filterDeployed(pingSeries, (s) => s.name), nameMap), "max"));
      const activePingSuccessSeries = visibleInfraSeries(mergeInfraSeries(renameListWithInfraMap(filterDeployed(pingSuccessSeries, (s) => s.name), nameMap), "max"));
      const { displayLatencySeries } = buildInfrastructurePingPresentation({
        latencySeries: rawActivePingSeries,
        successSeries: activePingSuccessSeries
      });
      const activeLossSeries = visibleInfraSeries(mergeInfraSeries(renameListWithInfraMap(filterDeployed(lossSeries, (s) => s.name), nameMap), "max"));
      if (shouldRender("pingTrendChart", seriesSignature(displayLatencySeries))) {
        renderPingChart({
          containerId: "pingTrendChart",
          series: displayLatencySeries,
          tournamentMode: Boolean(document.querySelector(".screen.tournament-mode"))
        });
      }
      if (shouldRender("lossHeatmap", seriesSignature(activeLossSeries))) {
        renderLossHeatmap("lossHeatmap", activeLossSeries);
      }
      const ispSignature = ispTraffic.map((result) => `${result.name}:${seriesSignature([result.download, result.upload])}`).join("||");
      if (shouldRender("ispGrid", ispSignature)) {
        renderIspPanels(ispTraffic);
      }
      lastDataSuccessAt = Date.now();
    } catch (error) {
      if (seq !== chartSeq) return;
      renderSignatures.delete("pingTrendChart");
      renderSignatures.delete("lossHeatmap");
      renderSignatures.delete("ispGrid");
      renderNoData(document.getElementById("pingTrendChart"));
      renderLossHeatmap("lossHeatmap", []);
      renderNoData(document.getElementById("ispGrid"));
      console.error(error);
    }
  }

  async function fetchPlayerSnapshot(selector) {
    const [latencyItems, successItems] = await Promise.all([
      prometheusInstant(playerLatencySnapshotQuery(selector)),
      prometheusInstant(playerSuccessSnapshotQuery(selector))
    ]);
    return {
      latencyItems,
      successItems,
      players: buildPlayers(latencyItems, successItems)
    };
  }

  function renderWirelessKpis(items) {
    document.getElementById("wirelessSummary").innerHTML = items.map((item) => `
      <div class="wireless-kpi ${item.level || "info"}">
        <span>${escapeHtml(item.label)}</span>
        <strong>${escapeHtml(item.value)}</strong>
        <em>${escapeHtml(item.note || "")}</em>
      </div>
    `).join("");
  }

  function triggerRescan(btn) {
    btn.disabled = true;
    btn.classList.add("spinning");
    fetch("/player-targets/rescan", { method: "POST" })
      .finally(() => {
        setTimeout(() => { btn.disabled = false; btn.classList.remove("spinning"); }, 3000);
      });
  }

  function renderWirelessControls() {
    const controls = document.getElementById("wirelessControls");
    if (controls.dataset.mode === "wireless") return;
    controls.dataset.mode = "wireless";
    controls.innerHTML = `
      <div class="wireless-title">
        <strong>无线异常总览</strong>
        <span>只统计无线选手，用来确认是否有人连入 WiFi，以及是否出现高延迟或离线。</span>
      </div>
    `;
  }

  function renderWirelessBoard(players) {
    const board = document.getElementById("wirelessBoard");
    if (!players.length) {
      renderNoData(board, "当前没有无线选手");
      return;
    }
    const rows = players
      .slice()
      .sort((a, b) => Number(a.success) - Number(b.success) || (b.latency || 0) - (a.latency || 0) || a.team - b.team || a.seat - b.seat)
      .map((player) => `
        <a class="wireless-table-row ${latencyLevel(player)}" href="${escapeHtml(latencyUrlForPlayer(player))}">
          <span data-label="队伍">${escapeHtml(teamName({ id: "" }, player.team))}</span>
          <span data-label="座位">${escapeHtml(seatLabel(player.seat))}</span>
          <span data-label="IP">${escapeHtml(player.ip)}</span>
          <span data-label="延迟">${escapeHtml(Number.isFinite(player.latency) ? formatPingText(player.latency) : "-")}</span>
          <span data-label="状态">${escapeHtml(playerStatusText(player))}</span>
        </a>
      `).join("");
    board.innerHTML = `
      <div class="wireless-table">
        <div class="wireless-table-head"><span>队伍</span><span>座位</span><span>IP</span><span>延迟</span><span>状态</span></div>
        ${rows}
      </div>
    `;
  }

  async function optionalPrometheusQuery(query) {
    try {
      return await prometheusQuery(query);
    } catch (error) {
      return [];
    }
  }

  function apOnlineFromLabels(metric) {
    const fields = ["state", "status", "stat", "connected", "up", "disabled"];
    for (const field of fields) {
      const raw = String(metric[field] || "").trim().toLowerCase();
      if (!raw) continue;
      if (field === "disabled" && ["1", "true", "yes"].includes(raw)) return false;
      if (/offline|disconnect|disconnected|down|unknown|false|^0$/.test(raw)) return false;
      if (/online|connected|active|adopted|true|^1$/.test(raw)) return true;
    }
    return null;
  }

  function mergeApOnlineMap(target, items) {
    items.forEach((item) => {
      const name = item.metric.name || item.name;
      if (!name) return;
      target.set(name, item.value > 0);
    });
  }

  // UniFi AP 状态（来自 unpoller / UniFi 控制器 API）。
  // device_info 可能包含离线 AP，所以不能把“有 info”直接当在线；优先看在线/uptime
  // 指标或状态 label，最后才兜底为在线，避免无 UniFi 状态指标时整段空掉。
  async function fetchApStatus() {
    let infos;
    let stations;
    try {
      [infos, stations] = await Promise.all([
        prometheusQuery('unpoller_device_info{type="uap"}'),
        prometheusQuery('sum by (name) (unpoller_device_stations{type="uap"})')
      ]);
    } catch (error) {
      return [];
    }
    const clients = {};
    stations.forEach((s) => { clients[s.metric.name] = s.value; });
    const onlineMaps = await Promise.all([
      optionalPrometheusQuery('max by (name) (unpoller_device_up{type="uap"})'),
      optionalPrometheusQuery('max by (name) (unpoller_device_connected{type="uap"})'),
      optionalPrometheusQuery('max by (name) (unpoller_device_state{type="uap"})'),
      optionalPrometheusQuery('max by (name) (unpoller_device_status{type="uap"})'),
      optionalPrometheusQuery('max by (name) (unpoller_device_uptime_seconds{type="uap"} > bool 0)'),
      optionalPrometheusQuery('max by (name) (unpoller_device_uptime{type="uap"} > bool 0)')
    ]);
    const onlineByName = new Map();
    onlineMaps.forEach((items) => mergeApOnlineMap(onlineByName, items));

    return infos
      .map((i) => {
        const name = i.metric.name || "?";
        const labelState = apOnlineFromLabels(i.metric);
        const online = onlineByName.has(name) ? onlineByName.get(name) : (labelState == null ? true : labelState);
        return {
          name,
          model: i.metric.model || "",
          online,
          clients: online && clients[name] != null ? clients[name] : 0
        };
      })
      .filter((ap) => ap.name && ap.name !== "?")
      .sort((a, b) => Number(b.online) - Number(a.online) || b.clients - a.clients || a.name.localeCompare(b.name, "zh-CN"));
  }

  function renderApStrip(aps) {
    const board = document.getElementById("wirelessBoard");
    if (!board || !aps.length) return;
    const onlineCount = aps.filter((ap) => ap.online).length;
    const totalClients = aps.reduce((sum, ap) => sum + (ap.online ? ap.clients : 0), 0);
    const chips = aps.map((ap) => `
      <div class="ap-chip ${ap.online ? "online" : "offline"}" title="${escapeHtml(`${ap.name} · ${ap.online ? "在线" : "离线"}${ap.model ? ` · ${ap.model}` : ""}`)}">
        <i class="dot"></i>
        <span class="ap-name">${escapeHtml(ap.name)}</span>
        <span class="ap-clients">${ap.online ? `<b>${ap.clients}</b> 人` : "离线"}</span>
      </div>
    `).join("");
    board.insertAdjacentHTML("afterbegin", `
      <div class="ap-strip">
        <div class="ap-strip-head">无线 AP：${onlineCount} 台在线 / ${aps.length} 台 · ${totalClients} 客户端</div>
        <div class="ap-grid">${chips}</div>
      </div>
    `);
  }

  async function refreshWirelessOverview() {
    renderWirelessControls();
    try {
      const [snapshot, aps] = await Promise.all([
        fetchPlayerSnapshot('role="player",network="wireless"'),
        fetchApStatus()
      ]);
      const rawItems = [...snapshot.latencyItems, ...snapshot.successItems];
      const gatewayIps = new Set(rawItems.map((item) => item.metric.instance).filter(isGatewayAddress));
      const players = snapshot.players;
      const online = players.filter((player) => player.success).length;
      const high = players.filter((player) => player.success && Number.isFinite(player.latency) && player.latency >= 0.08).length;
      const maxLatency = players
        .filter((player) => Number.isFinite(player.latency))
        .map((player) => player.latency)
        .sort((a, b) => b - a)[0];
      renderWirelessKpis([
        { label: "无线目标", value: players.length, note: "当前识别到的无线选手" },
        { label: "在线", value: online, level: !players.length || online === players.length ? "good" : "warn", note: "当前可达" },
        { label: "高延迟", value: high, level: high ? "warn" : "good", note: ">= 80 ms" },
        { label: "疑似网关", value: gatewayIps.size, level: gatewayIps.size ? "bad" : "good", note: ".254" },
        { label: "最高延迟", value: Number.isFinite(maxLatency) ? formatPingText(maxLatency) : "-", level: maxLatency >= 0.08 ? "warn" : "good" }
      ]);
      renderWirelessBoard(players);
      renderApStrip(aps);
      lastDataSuccessAt = Date.now();
    } catch (error) {
      renderNoData(document.getElementById("wirelessSummary"), "查询失败");
      renderNoData(document.getElementById("wirelessBoard"));
      console.error(error);
    }
  }

  function dateTimeInputValue(date) {
    const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
    return local.toISOString().slice(0, 16);
  }

  function estimateStepSeconds(seriesList) {
    const times = seriesList.flatMap((series) => series.values.map((point) => point.t)).sort((a, b) => a - b);
    const gaps = [];
    for (let index = 1; index < times.length; index += 1) {
      const gap = times[index] - times[index - 1];
      if (gap > 0 && gap < 300) gaps.push(gap);
    }
    if (!gaps.length) return 5;
    gaps.sort((a, b) => a - b);
    const middle = Math.floor(gaps.length / 2);
    const median = gaps.length % 2
      ? gaps[middle]
      : (gaps[middle - 1] + gaps[middle]) / 2;
    return Math.max(1, Math.round(median));
  }

  // ---- Event platform control ----

  function storedControlLayout() {
    const fallback = config.defaultLayout || "tournament-64-2layer";
    try {
      return window.localStorage.getItem(CONTROL_LAYOUT_STORAGE_KEY) || fallback;
    } catch (error) {
      return fallback;
    }
  }

  function controlPageAndNetwork() {
    const layout = storedControlLayout();
    const page = pages.find((item) => item.id === layout && item.kind) ||
      pages.find((item) => item.id === config.defaultLayout && item.kind) ||
      pages.find((item) => item.id === "tournament-64-2layer") ||
      pages.find((item) => item.kind);
    return {
      page,
      network: "wired"
    };
  }

  function controlItemHtml(item) {
    return `
      <div class="control-item ${item.level || "info"}">
        <span>${escapeHtml(item.section || "")}</span>
        <strong>${escapeHtml(item.label || "")}</strong>
        <b>${escapeHtml(item.value == null ? "" : item.value)}</b>
        <em>${escapeHtml(item.note || "")}</em>
      </div>
    `;
  }

  function renderControlReadiness(score, checks) {
    const missingHost = document.getElementById("controlReadinessMissing");
    const missing = (checks || [])
      .filter((item) => item.level === "bad" || item.level === "warn");
    if (!missingHost) return;
    missingHost.innerHTML = missing.length
      ? missing.map((item) => controlItemHtml({
          section: item.section || "待补",
          label: item.label || "检查项",
          level: item.level || "warn",
          value: item.value == null ? "" : item.value,
          note: item.note || ""
        })).join("")
      : `<div class="control-empty good">当前没有需要关注的问题</div>`;
  }

  function renderControlChecklist(checks) {
    const element = document.getElementById("controlChecklist");
    if (!element) return;
    const wanted = new Set(["赛前", "基础设施", "采集"]);
    const items = checks.filter((item) => wanted.has(item.section));
    element.innerHTML = items.map(controlItemHtml).join("") ||
      `<div class="control-empty">暂无检查项</div>`;
  }

  function renderControlTopology(targetSummary, topologyFindings, edges) {
    const rows = [
      { section: "拓扑", label: "设备目标", level: targetSummary.total ? "good" : "warn", value: String(targetSummary.total), note: `核心 ${targetSummary.byKind.core} / 接入 ${targetSummary.byKind.dist} / ISP ${targetSummary.byKind.isp}` },
      { section: "拓扑", label: "LLDP 边", level: edges.length ? "good" : "warn", value: String(edges.length), note: edges.length ? "已采集拓扑关系" : "未采集到拓扑关系" },
      ...topologyFindings
    ];
    document.getElementById("controlTopology").innerHTML = rows.map(controlItemHtml).join("");
  }

  function renderControlConfig(context) {
    const { runtimeStatus, configRisks, services, platformConfig, versionInfo } = context;
    const targetStatus = runtimeStatus && runtimeStatus.targets ? runtimeStatus.targets : null;
    const updated = runtimeStatus && runtimeStatus.updated_at ? formatTimestampFull(runtimeStatus.updated_at) : "-";
    const apiState = platformConfig && platformConfig.ok
      ? (platformConfig.writeEnabled === false ? "只读" : "可写")
      : "不可用";
    let schemaValue = "-";
    let schemaNote = "";
    if (versionInfo && versionInfo.config_schema_original != null) {
      schemaValue = String(versionInfo.config_schema_original);
      if (versionInfo.migration_required) {
        schemaValue = `${versionInfo.config_schema_original} → ${versionInfo.config_schema_current}`;
        schemaNote = "保存或应用时升级";
      } else if (versionInfo.config_too_new) {
        schemaNote = `当前软件最高支持 ${versionInfo.config_schema_supported}，请先升级平台`;
      }
    }
    const rows = [
      { label: "平台版本", value: versionInfo && versionInfo.platform_version ? versionInfo.platform_version : "unknown" },
      { label: "Git Commit", value: versionInfo && versionInfo.git_commit ? versionInfo.git_commit : "unknown" },
      { label: "配置版本", value: schemaValue, note: schemaNote },
      { label: "ISP", value: config.ispAutoDiscovery === "true" ? "自动发现" : (config.ispNames || "默认") },
      { label: "选手探测目标", value: targetStatus ? `${targetStatus.total} 个` : "-", note: targetStatus ? `player-targets 生成：有线 ${targetStatus.wired} / 无线 ${targetStatus.wireless} / ${updated}` : "" },
      { label: "采集任务", value: `${services.filter((item) => item.up === item.total).length}/${services.length}` },
      { label: "平台 API", value: apiState, note: platformConfig && platformConfig.error ? platformConfig.error : "" }
    ];
    const configRows = rows.map((row) => `
      <div class="config-row">
        <span>${escapeHtml(row.label)}</span>
        <strong>${escapeHtml(row.value)}</strong>
        ${row.note ? `<em>${escapeHtml(row.note)}</em>` : ""}
      </div>
    `).join("");
    const riskRows = configRisks.length
      ? `<div class="config-risk-list">${configRisks.map((item) => controlItemHtml({ section: "配置", ...item })).join("")}</div>`
      : `<div class="control-empty good">配置风险未触发</div>`;
    document.getElementById("controlConfig").innerHTML = `${configRows}${riskRows}`;
  }


  function renderIncidentList(payload) {
    const incidents = payload && payload.incidents ? payload.incidents : [];
    lastIncidents = incidents;
    const list = document.getElementById("controlIncidentList");
    if (!list) return;
    if (payload && payload.error) {
      list.innerHTML = `<div class="control-empty bad">${escapeHtml(payload.error)}</div>`;
      return;
    }
    if (!incidents.length) {
      list.innerHTML = `<div class="control-empty">暂无事故记录</div>`;
      return;
    }
    list.innerHTML = incidents.slice(0, 12).map((item) => {
      const started = item.startedAt ? formatTimestampFull(item.startedAt) : "-";
      const duration = item.recoveredAt && item.startedAt ? `${Math.max(0, Math.round((item.recoveredAt - item.startedAt) / 60))} 分钟` : "进行中";
      return `
        <div class="incident-record ${item.severity || "warn"}">
          <span>#${escapeHtml(item.id)} · ${escapeHtml(item.status || "open")}</span>
          <strong>${escapeHtml(item.title || "")}</strong>
          <em>${escapeHtml(started)} · ${escapeHtml(duration)} · ${escapeHtml(item.owner || "未分配")}</em>
          ${item.status === "resolved" ? "" : `<button type="button" data-resolve-incident="${escapeHtml(item.id)}">标记恢复</button>`}
        </div>
      `;
    }).join("");
    list.querySelectorAll("[data-resolve-incident]").forEach((button) => {
      button.addEventListener("click", async () => {
        try {
          await patchPlatform(`/incidents/${button.dataset.resolveIncident}`, {
            status: "resolved",
            recoveredAt: Math.floor(Date.now() / 1000),
            event: "标记恢复",
            eventType: "recovery"
          });
          renderIncidentList(await fetchIncidents());
        } catch (error) {
          renderIncidentList({ incidents: lastIncidents, error: error.message || "更新事故失败" });
        }
      });
    });
  }

  async function createControlIncident() {
    const input = document.getElementById("controlIncidentTitle");
    const title = (input && input.value.trim()) || "现场事故";
    const related = lastControlReport ? {
      readiness: lastControlReport.readiness,
      checks: lastControlReport.checks.filter((item) => item.level === "bad" || item.level === "warn").slice(0, 8)
    } : {};
    try {
      await postPlatform("/incidents", { title, severity: lastControlReport && lastControlReport.readiness.level === "bad" ? "bad" : "warn", related });
      if (input) input.value = "";
      renderIncidentList(await fetchIncidents());
    } catch (error) {
      renderIncidentList({ incidents: lastIncidents, error: error.message || "创建事故失败" });
    }
  }

  function renderDelivery() {
    const element = document.getElementById("controlDelivery");
    if (!element) return;
    // Render once so periodic status refreshes do not wipe manually entered
    // diagnostic settings or the result the operator is reading.
    if (element.dataset.built === "1") return;
    element.dataset.built = "1";
    element.innerHTML = `
      <div class="delivery-actions">
        <button type="button" class="delivery-test-alert" id="preCheckBtn">赛前体检</button>
        <button type="button" class="delivery-test-alert" id="testAlertBtn">发送测试告警</button>
        <span class="test-alert-result" id="testAlertResult"></span>
      </div>
      <div class="precheck-result" id="preCheckResult" hidden></div>
      <section class="network-tool" aria-labelledby="retirePendingTitle">
        <div class="network-tool-heading">
          <div>
            <h3 id="retirePendingTitle">待删除设备</h3>
            <p>离线满 48 小时的设备在这里等人工确认；不确认永远不会自动删除。飞书确认卡与此面板等效。</p>
          </div>
          <button type="button" class="delivery-test-alert" id="retirePendingRefreshBtn">刷新列表</button>
        </div>
        <div class="network-tool-result" id="retirePendingList" hidden></div>
      </section>
      <section class="network-tool" aria-labelledby="iperfToolTitle">
        <div class="network-tool-heading">
          <div>
            <h3 id="iperfToolTitle">iPerf3 出口测速</h3>
            <p>默认使用香港公共节点；公共节点繁忙时会自动尝试同组其他端口。</p>
          </div>
          <span class="network-tool-badge">主动占用带宽</span>
        </div>
        <div class="network-tool-grid iperf-tool-grid">
          <label>测速地区
            <select id="iperfPreset">
              <option value="hongkong" selected>中国香港（公共节点）</option>
              <option value="singapore">新加坡（公共节点）</option>
              <option value="istanbul">土耳其·伊斯坦布尔（公共节点）</option>
              <option value="indonesia">印度尼西亚（公共节点）</option>
              <option value="custom">自定义</option>
            </select>
          </label>
          <label>公共服务器
            <select id="iperfPublicServer"></select>
          </label>
          <label>服务器
            <input id="iperfServer" type="text" placeholder="正在加载版本化节点配置" spellcheck="false" readonly />
          </label>
          <label>端口或范围
            <input id="iperfPorts" type="text" inputmode="numeric" spellcheck="false" readonly />
          </label>
          <label>单向时长（秒）
            <input id="iperfDuration" type="text" inputmode="numeric" value="10" spellcheck="false" />
          </label>
          <label>并发连接
            <input id="iperfParallel" type="text" inputmode="numeric" value="10" spellcheck="false" />
          </label>
          <label>方向
            <select id="iperfDirection">
              <option value="both" selected>先上传，再下载</option>
              <option value="upload">仅上传</option>
              <option value="download">仅下载</option>
            </select>
          </label>
        </div>
        <p class="network-tool-hint" id="iperfPresetHint">香港 Leaseweb 公共节点；共享服务器繁忙时结果可能偏低。</p>
        <div class="network-tool-actions">
          <button type="button" class="delivery-test-alert" id="iperfRunBtn">开始测速</button>
          <button type="button" class="delivery-test-alert danger" id="iperfStopBtn" hidden>停止当前测速</button>
          <span>正常双向约 20 秒；节点繁忙时会重试，最长约 60 秒。</span>
        </div>
        <div class="iperf-confirm" id="iperfConfirm" hidden>
          <div class="iperf-confirm-copy">
            <strong>确认开始出口测速</strong>
            <span id="iperfConfirmSummary"></span>
          </div>
          <div class="iperf-confirm-actions">
            <button type="button" id="iperfCancelBtn">取消</button>
            <button type="button" class="primary" id="iperfConfirmBtn">确认并开始</button>
          </div>
        </div>
        <div class="iperf-progress" id="iperfProgress" hidden aria-live="polite">
          <div class="iperf-progress-heading">
            <strong id="iperfProgressPhase">准备测速</strong>
            <span id="iperfProgressElapsed">0.0 秒</span>
          </div>
          <div class="iperf-progress-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">
            <i id="iperfProgressFill"></i>
          </div>
          <span id="iperfProgressDetail">正在建立任务…</span>
        </div>
        <div class="network-tool-result" id="iperfResult" hidden></div>
        <div class="network-tool-history" id="iperfHistory" aria-live="polite"></div>
      </section>
    `;
    let iperfPresets = { custom: DEFAULT_CUSTOM_PRESET };
    const iperfPreset = document.getElementById("iperfPreset");
    const iperfPublicServer = document.getElementById("iperfPublicServer");
    const iperfServer = document.getElementById("iperfServer");
    const iperfPorts = document.getElementById("iperfPorts");
    const iperfHint = document.getElementById("iperfPresetHint");
    const applyIperfPublicServer = () => {
      const view = iperfPresetView(iperfPresets, iperfPreset.value, iperfPublicServer.value);
      if (!view.server) return;
      iperfServer.value = view.server;
      iperfPorts.value = view.ports;
    };
    const applyIperfPreset = () => {
      const view = iperfPresetView(iperfPresets, iperfPreset.value);
      iperfServer.placeholder = view.placeholder;
      iperfServer.readOnly = !view.isCustom;
      iperfPorts.readOnly = !view.isCustom;
      if (view.isCustom) {
        iperfPublicServer.innerHTML = '<option value="0">手工填写</option>';
        iperfPublicServer.disabled = true;
        iperfServer.value = "";
        iperfPorts.value = view.ports;
      } else {
        iperfPublicServer.disabled = false;
        iperfPublicServer.innerHTML = view.options.map((item) => (
          `<option value="${item.index}">${escapeHtml(item.label)}</option>`
        )).join("");
        iperfServer.value = view.server;
        iperfPorts.value = view.ports;
      }
      if (iperfHint) iperfHint.textContent = view.note;
    };
    if (iperfPreset) iperfPreset.addEventListener("change", applyIperfPreset);
    if (iperfPublicServer) iperfPublicServer.addEventListener("change", applyIperfPublicServer);
    applyIperfPreset();
    loadServerConfig(fetch)
      .then((payload) => {
        iperfPresets = payload.presets;
        applyIperfPreset();
        if (iperfHint && payload.verifiedAt) {
          iperfHint.textContent += ` · 节点核验 ${payload.verifiedAt}`;
        }
      })
      .catch((error) => {
        if (iperfHint) iperfHint.textContent = `公共节点配置加载失败：${error.message}；仍可使用自定义节点。`;
      });
    const preBtn = document.getElementById("preCheckBtn");
    if (preBtn) {
      preBtn.addEventListener("click", async () => {
        const box = document.getElementById("preCheckResult");
        preBtn.disabled = true;
        if (box) { box.hidden = false; box.className = "precheck-result"; box.textContent = "体检中…（最长约 2 分钟）"; }
        try {
          const res = await postPlatform("/pre-check", {});
          if (box) {
            if (!res || !res.ok) {
              box.className = "precheck-result bad";
              box.textContent = `体检失败：${(res && res.error) || "未知错误"}`;
            } else {
              const verdictText = { good: "✅ 可以开赛", warn: "⚠ 有警告，请确认", bad: "❌ 需要处理" }[res.verdict] || res.verdict;
              box.className = `precheck-result ${res.verdict}`;
              box.innerHTML = `<div class="precheck-verdict">${verdictText}　通过 ${res.pass} · 警告 ${res.warn} · 失败 ${res.fail}</div><pre>${escapeHtml(res.output || "")}</pre>`;
            }
          }
        } catch (error) {
          if (box) { box.className = "precheck-result bad"; box.textContent = `体检失败：${error.message}`; }
        } finally {
          preBtn.disabled = false;
        }
      });
    }
    const testBtn = document.getElementById("testAlertBtn");
    if (testBtn) {
      testBtn.addEventListener("click", async () => {
        const result = document.getElementById("testAlertResult");
        testBtn.disabled = true;
        if (result) { result.textContent = "发送中…"; result.className = "test-alert-result"; }
        try {
          const res = await postPlatform("/test-alert", {});
          const ok = Boolean(res && res.ok);
          if (result) {
            const channel = { app: "自建应用", webhook: "群机器人 Webhook", "dry-run": "DryRun" }[res && res.channel] || "未知通道";
            const fellBack = ok && res && res.channel === "webhook" && res.appError;
            result.textContent = ok
              ? (res.dryRun
                ? "已触发（DryRun 模式，未真正发送）"
                : fellBack
                  ? `已通过 Webhook 回退发送；自建应用失败：${res.appError}`
                  : `已通过${channel}发送，请到飞书群确认收到`)
              : `失败：${(res && (res.appError || res.error)) || "未知错误"}`;
            result.className = `test-alert-result ${fellBack ? "warn" : ok ? "good" : "bad"}`;
          }
        } catch (error) {
          if (result) { result.textContent = `失败：${error.message}`; result.className = "test-alert-result bad"; }
        } finally {
          testBtn.disabled = false;
        }
      });
    }

    const retireList = document.getElementById("retirePendingList");
    const retireRefreshBtn = document.getElementById("retirePendingRefreshBtn");

    const renderRetirePending = (payload) => {
      if (!retireList) return;
      retireList.hidden = false;
      const pending = (payload && payload.pending) || [];
      if (payload && payload.error) {
        retireList.className = "network-tool-result bad";
        retireList.textContent = payload.error;
        return;
      }
      if (!pending.length) {
        retireList.className = "network-tool-result good";
        retireList.textContent = "没有待删除设备。";
        return;
      }
      retireList.className = "network-tool-result warn";
      retireList.innerHTML = pending.map((item) => {
        const name = escapeHtml(item.name || item.ip || "?");
        const ip = escapeHtml(item.ip || "");
        const downSince = item.downSince
          ? new Date(item.downSince * 1000).toLocaleString("zh-CN", { hour12: false })
          : "未知";
        return `
          <div class="retire-pending-row" data-key="${escapeHtml(item.key)}" data-token="${escapeHtml(item.token)}">
            <span>${name}${ip && ip !== name ? ` (${ip})` : ""} · 离线自 ${escapeHtml(downSince)}</span>
            <button type="button" class="delivery-test-alert" data-retire-action="delete">确认删除</button>
            <button type="button" class="delivery-test-alert" data-retire-action="keep">保留设备</button>
          </div>`;
      }).join("");
    };

    const refreshRetirePending = async () => {
      if (!retireList) return;
      renderRetirePending(await fetchRetirePending());
    };

    if (retireRefreshBtn) retireRefreshBtn.addEventListener("click", refreshRetirePending);
    if (retireList) {
      retireList.addEventListener("click", async (event) => {
        const button = event.target.closest("button[data-retire-action]");
        if (!button) return;
        const row = button.closest(".retire-pending-row");
        if (!row) return;
        const action = button.dataset.retireAction;
        // 删除采用两段式按钮：第一次点击只是"武装"，再点一次才真正执行，
        // 与控制台其它危险操作一致（不使用浏览器弹窗）。
        if (action === "delete" && button.dataset.armed !== "1") {
          button.dataset.armed = "1";
          button.textContent = "再点一次确认删除";
          setTimeout(() => {
            button.dataset.armed = "";
            button.textContent = "确认删除";
          }, 5000);
          return;
        }
        button.disabled = true;
        try {
          const result = await postPlatform("/network/retire/resolve", {
            key: row.dataset.key,
            token: row.dataset.token,
            action,
          });
          if (!result || result.ok !== true) {
            renderRetirePending({ error: (result && result.error) || "操作失败" });
            setTimeout(refreshRetirePending, 1500);
            return;
          }
          await refreshRetirePending();
        } catch (error) {
          renderRetirePending({ error: `操作失败：${error.message}` });
        } finally {
          button.disabled = false;
        }
      });
      refreshRetirePending();
    }

    const iperfBtn = document.getElementById("iperfRunBtn");
    const iperfConfirm = document.getElementById("iperfConfirm");
    const iperfConfirmSummary = document.getElementById("iperfConfirmSummary");
    const iperfConfirmBtn = document.getElementById("iperfConfirmBtn");
    const iperfCancelBtn = document.getElementById("iperfCancelBtn");
    const iperfStopBtn = document.getElementById("iperfStopBtn");
    const iperfProgress = document.getElementById("iperfProgress");
    const iperfProgressPhase = document.getElementById("iperfProgressPhase");
    const iperfProgressElapsed = document.getElementById("iperfProgressElapsed");
    const iperfProgressFill = document.getElementById("iperfProgressFill");
    const iperfProgressDetail = document.getElementById("iperfProgressDetail");
    const iperfHistory = document.getElementById("iperfHistory");
    let pendingIperfRequest = null;
    let activeIperfTaskId = "";
    const iperfTaskStorageKey = "bigscreen.iperfTaskId";
    let iperfProgressTimer = null;
    let iperfProgressRefreshing = false;

    const hideIperfConfirmation = () => {
      pendingIperfRequest = null;
      if (iperfConfirm) iperfConfirm.hidden = true;
    };

    const renderIperfProgress = (status) => {
      if (!iperfProgress || !status || status.state === "unavailable") return;
      const elapsed = Math.max(0, Number(status.elapsedSeconds || 0));
      const maxSeconds = Math.max(1, Number(status.maxSeconds || 60));
      const reported = Math.max(0, Math.min(100, Number(status.percent || 0)));
      const timeFloor = status.state === "running" ? Math.min(95, (elapsed / maxSeconds) * 100) : 0;
      const percent = status.state === "complete" ? 100 : Math.max(reported, timeFloor);
      const phaseLabels = {
        preparing: "准备测速",
        upload: "上传测速",
        download: "下载测速",
        complete: "测速完成",
        failed: "测速失败",
        cancelled: "测速已停止"
      };
      iperfProgress.hidden = false;
      iperfProgress.className = `iperf-progress ${status.state || "running"}`;
      if (iperfProgressPhase) iperfProgressPhase.textContent = phaseLabels[status.phase] || "测速进行中";
      if (iperfProgressElapsed) iperfProgressElapsed.textContent = `${elapsed.toFixed(1)} 秒 / 最长 ${maxSeconds} 秒`;
      if (iperfProgressFill) iperfProgressFill.style.width = `${percent.toFixed(1)}%`;
      const track = iperfProgress.querySelector("[role=progressbar]");
      if (track) track.setAttribute("aria-valuenow", String(Math.round(percent)));
      if (iperfProgressDetail) iperfProgressDetail.textContent = status.message || "测速进行中";
    };

    const refreshIperfProgress = async () => {
      if (iperfProgressRefreshing) return;
      iperfProgressRefreshing = true;
      try {
        const status = await fetchIperfStatus(activeIperfTaskId);
        renderIperfProgress(status);
        if (status.state === "unavailable" && /不存在|过期/.test(status.error || "")) {
          if (iperfProgressTimer) window.clearInterval(iperfProgressTimer);
          iperfProgressTimer = null;
          window.sessionStorage.removeItem(iperfTaskStorageKey);
          activeIperfTaskId = "";
          iperfBtn.disabled = false;
          if (iperfStopBtn) iperfStopBtn.hidden = true;
          renderIperfTaskResult({ state: "failed", message: status.error });
          return;
        }
        if (["complete", "failed", "cancelled"].includes(status.state)) {
          if (iperfProgressTimer) window.clearInterval(iperfProgressTimer);
          iperfProgressTimer = null;
          window.sessionStorage.removeItem(iperfTaskStorageKey);
          activeIperfTaskId = "";
          iperfBtn.disabled = false;
          if (iperfStopBtn) iperfStopBtn.hidden = true;
          renderIperfTaskResult(status);
          refreshIperfHistory();
        }
      } finally {
        iperfProgressRefreshing = false;
      }
    };

    const startIperfProgress = () => {
      if (iperfProgressTimer) window.clearInterval(iperfProgressTimer);
      renderIperfProgress({
        state: "running",
        phase: "preparing",
        percent: 0,
        elapsedSeconds: 0,
        maxSeconds: 60,
        message: "正在连接测速服务…"
      });
      if (iperfStopBtn) iperfStopBtn.hidden = false;
      iperfProgressTimer = window.setInterval(refreshIperfProgress, 500);
    };

    const renderIperfTaskResult = (response) => {
      const result = document.getElementById("iperfResult");
      if (!result) return;
      result.hidden = false;
      const view = iperfResultView(response, escapeHtml);
      result.className = view.className;
      if (Object.prototype.hasOwnProperty.call(view, "html")) result.innerHTML = view.html;
      else result.textContent = view.text;
    };

    const renderIperfHistory = (payload) => {
      if (!iperfHistory) return;
      iperfHistory.innerHTML = iperfHistoryHtml(payload, escapeHtml);
    };

    const refreshIperfHistory = async () => renderIperfHistory(await fetchIperfHistory());

    const executeIperfTest = async (request) => {
      const result = document.getElementById("iperfResult");
      hideIperfConfirmation();
      iperfBtn.disabled = true;
      if (result) {
        result.hidden = false;
        result.className = "network-tool-result loading";
        result.textContent = "正在创建独立测速任务……";
      }
      try {
        const response = await postPlatform("/network/iperf3", request, { timeoutMs: 10000 });
        activeIperfTaskId = response.taskId || "";
        if (!activeIperfTaskId) throw new Error("后端没有返回任务编号");
        window.sessionStorage.setItem(iperfTaskStorageKey, activeIperfTaskId);
        if (result) result.textContent = `任务 ${activeIperfTaskId} 已开始，正在寻找可用端口……`;
        startIperfProgress();
        await refreshIperfProgress();
      } catch (error) {
        const runningTaskId = error && error.payload && error.payload.taskId;
        if (error.status === 409 && runningTaskId) {
          activeIperfTaskId = runningTaskId;
          window.sessionStorage.setItem(iperfTaskStorageKey, activeIperfTaskId);
          if (result) result.textContent = `任务 ${activeIperfTaskId} 正在运行，已连接到该任务。`;
          startIperfProgress();
          await refreshIperfProgress();
          return;
        }
        if (result) {
          result.className = "network-tool-result bad";
          result.textContent = `测速失败：${error.message}`;
        }
        iperfBtn.disabled = false;
        if (iperfStopBtn) iperfStopBtn.hidden = true;
      }
    };

    if (iperfBtn) {
      iperfBtn.addEventListener("click", () => {
        const result = document.getElementById("iperfResult");
        const direction = document.getElementById("iperfDirection").value;
        const seconds = Number(document.getElementById("iperfDuration").value || 10);
        const server = document.getElementById("iperfServer").value.trim();
        if (!server) {
          if (result) {
            result.hidden = false;
            result.className = "network-tool-result bad";
            result.textContent = "请先填写自定义 iPerf3 服务器。";
          }
          return;
        }
        pendingIperfRequest = {
          server,
          ports: document.getElementById("iperfPorts").value.trim(),
          duration: document.getElementById("iperfDuration").value.trim(),
          parallel: document.getElementById("iperfParallel").value.trim(),
          direction
        };
        const estimated = seconds * (direction === "both" ? 2 : 1);
        if (iperfConfirmSummary) {
          iperfConfirmSummary.textContent = `${server} · 正常约 ${estimated} 秒，节点忙时最长约 60 秒 · 期间会主动占用公网带宽`;
        }
        if (iperfConfirm) iperfConfirm.hidden = false;
        if (iperfConfirmBtn) iperfConfirmBtn.focus();
      });
    }
    if (iperfCancelBtn) iperfCancelBtn.addEventListener("click", hideIperfConfirmation);
    if (iperfConfirmBtn) {
      iperfConfirmBtn.addEventListener("click", () => {
        if (pendingIperfRequest) executeIperfTest(pendingIperfRequest);
      });
    }
    if (iperfStopBtn) {
      iperfStopBtn.addEventListener("click", async () => {
        if (!activeIperfTaskId) return;
        iperfStopBtn.disabled = true;
        try {
          await postPlatform("/network/iperf3/stop", { taskId: activeIperfTaskId }, { timeoutMs: 5000 });
          if (iperfProgressDetail) iperfProgressDetail.textContent = "正在停止测速进程……";
        } catch (error) {
          if (iperfProgressDetail) iperfProgressDetail.textContent = `停止失败：${error.message}`;
        } finally {
          iperfStopBtn.disabled = false;
        }
      });
    }
    if (iperfHistory) {
      iperfHistory.addEventListener("click", async (event) => {
        const button = event.target.closest("button[data-task-id]");
        if (!button) return;
        try {
          renderIperfTaskResult(await fetchIperfStatus(button.dataset.taskId));
        } catch (error) {
          renderIperfTaskResult({ state: "failed", message: error.message });
        }
      });
      refreshIperfHistory();
    }
    const rememberedIperfTaskId = window.sessionStorage.getItem(iperfTaskStorageKey) || "";
    if (rememberedIperfTaskId) {
      activeIperfTaskId = rememberedIperfTaskId;
      iperfBtn.disabled = true;
      startIperfProgress();
      refreshIperfProgress();
    }

  }

  function renderControlIncidentFlow(snapshot) {
    const nowValue = dateTimeInputValue(new Date());
    const worst = snapshot.readiness.level;
    const flow = [
      { label: "卡顿分析", href: `/incident?at=${encodeURIComponent(nowValue)}&window=5&threshold=0.05`, value: "当前时间" },
      { label: "比赛座位", href: snapshot.page ? snapshot.page.path : "/", value: `${snapshot.seatSummary.seats}/${snapshot.seatSummary.expectedSeats}` },
      { label: "拓扑", href: "/topology", value: `${snapshot.edges.length} 边` },
      { label: "网络总览", href: "/infra", value: snapshot.targetSummary.offline.length ? `${snapshot.targetSummary.offline.length} 离线` : "正常" }
    ];
    document.getElementById("controlIncidentFlow").innerHTML = `
      <div class="flow-state ${worst}">
        <strong>${worst === "bad" ? "需要处理" : worst === "warn" ? "需要关注" : "可比赛"}</strong>
        <span>${snapshot.checks.filter((item) => item.level === "bad" || item.level === "warn").slice(0, 2).map((item) => item.label).join("、") || "关键路径正常"}</span>
      </div>
      <div class="flow-links">
        ${flow.map((item) => `
          <a href="${escapeHtml(item.href)}">
            <span>${escapeHtml(item.label)}</span>
            <strong>${escapeHtml(item.value)}</strong>
          </a>
        `).join("")}
      </div>
    `;
  }

  function renderControlLint() {
    const coreInput = document.getElementById("controlCoreConfig");
    const input = document.getElementById("controlSwitchConfig");
    const result = document.getElementById("controlLintResult");
    const coreText = coreInput ? coreInput.value : "";
    const distText = input ? input.value : "";
    if (!coreText.trim() && !distText.trim()) {
      result.innerHTML = `<div class="control-empty">等待配置片段</div>`;
      return;
    }
    const issues = lintSwitchScene(coreText, distText);
    if (!issues.length) {
      result.innerHTML = `<div class="control-empty good">未发现明显风险</div>`;
      return;
    }
    result.innerHTML = issues.slice(0, 24).map((item) => controlItemHtml({
      section: item.source || (item.line ? `L${item.line}` : "全局"),
      label: item.label,
      level: item.level,
      value: item.level.toUpperCase(),
      note: item.note
    })).join("");
  }

  async function collectControlSnapshot() {
    const { page, network } = controlPageAndNetwork();
    const expectedSeats = page ? (page.teams || []).length * page.teamSize : 0;
    const selector = page ? tournamentSelector(page, network) : 'role="player"';
    const [snapshot, targets, edges, servicesRaw, runtimeStatus, platformConfig, versionInfo, incidents, dhcpSettings] = await Promise.all([
      fetchPlayerSnapshot(selector),
      fetchTopologyTargets(),
      fetchTopologyEdges(),
      prometheusInstant("up"),
      fetchRuntimeStatus(),
      fetchPlatformConfig(),
      fetchPlatformVersion(),
      fetchIncidents(),
      fetchDhcpSettings()
    ]);
    const players = page
      ? snapshot.players.filter((player) => !page.teamSize || player.seat <= page.teamSize)
      : snapshot.players;
    const seatSummary = summarizePlayers(players, expectedSeats);
    const targetSummary = summarizeTargets(targets);
    const serviceSummary = summarizeServices(servicesRaw);
    const configRisks = buildConfigRisks(config, runtimeStatus);
    const topologyFindings = buildTopologyFindings(targets, edges);
    const checks = buildReadinessChecks({ seatSummary, targetSummary, serviceSummary, configRisks, topologyFindings });
    const readiness = readinessScore(checks);
    return {
      mode: "monitor",
      page,
      network,
      players,
      seatSummary,
      targets,
      targetSummary,
      edges,
      services: serviceSummary,
      runtimeStatus,
      platformConfig,
      versionInfo,
      dhcpSettings,
      incidents,
      configRisks,
      topologyFindings,
      checks,
      readiness
    };
  }

  function renderControlPanel(snapshot) {
    renderControlReadiness(snapshot.readiness, snapshot.checks);
    renderControlTopology(snapshot.targetSummary, snapshot.topologyFindings, snapshot.edges);
    renderControlConfig(snapshot);
    configEditor.render(snapshot.platformConfig, snapshot.dhcpSettings);
    renderControlIncidentFlow(snapshot);
    renderIncidentList(snapshot.incidents);
    renderDelivery();
    lastControlReport = snapshot;
    lastDataSuccessAt = Date.now();
  }

  function setControlAuthMessage(message, level = "") {
    const element = document.getElementById("controlAuthMessage");
    if (!element) return;
    element.className = `auth-message ${level || ""}`.trim();
    element.textContent = message || "";
  }

  function renderControlAuth(status) {
    const authPanel = document.getElementById("controlAuth");
    const shell = document.getElementById("controlShell");
    const loginForm = document.getElementById("controlLoginForm");
    const passwordForm = document.getElementById("controlPasswordForm");
    const userInput = document.getElementById("controlLoginUser");
    const title = document.getElementById("controlAuthTitle");
    const hint = document.getElementById("controlAuthHint");
    const authenticated = status && status.authenticated;
    const mustChange = authenticated && status.mustChangePassword;

    if (!authPanel || !shell) return true;
    if (authenticated && !mustChange) {
      authPanel.hidden = true;
      shell.hidden = false;
      setControlAuthMessage("");
      return true;
    }

    shell.hidden = true;
    authPanel.hidden = false;
    if (loginForm) loginForm.hidden = Boolean(authenticated);
    if (passwordForm) passwordForm.hidden = !mustChange;
    if (userInput && status && status.defaultUser && !userInput.value) userInput.value = status.defaultUser;
    if (title) title.textContent = mustChange ? "首次登录需要修改密码" : "赛事控制台登录";
    if (hint) {
      hint.textContent = mustChange
        ? "默认密码只能用于首次进入，请设置一个新的控制台密码。"
        : "输入控制台账号密码后继续。";
    }
    if (status && status.error) {
      setControlAuthMessage(status.error, "bad");
    } else if (mustChange) {
      setControlAuthMessage("新密码至少 10 位，并包含字母和数字。", "");
    } else {
      setControlAuthMessage("");
    }
    return false;
  }

  async function ensureControlAuth() {
    const status = await fetchPlatformAuthStatus();
    // During a transient proxy outage (bigscreen restarting on 应用配置) the
    // auth probe fails with no HTTP status. If we were already authenticated,
    // hold the console rather than tearing it down to the login screen -- the
    // next poll will recover on its own.
    if (status && status.transient && lastControlAuth && lastControlAuth.authenticated) {
      return true;
    }
    lastControlAuth = status;
    return renderControlAuth(status);
  }

  async function refreshControlPanel() {
    // While 应用配置 is restarting services, its own flow drives the UI and waits
    // for recovery -- don't let the periodic refresh fight it with failed fetches.
    if (configEditor.isApplyInProgress()) return;
    if (!await ensureControlAuth()) {
      lastControlReport = null;
      return;
    }
    if (!lastControlReport) {
      ["controlReadinessMissing", "controlTopology", "controlConfig", "controlIncidentFlow", "controlIncidentList", "controlDelivery"].forEach((id) => {
        const element = document.getElementById(id);
        if (element) element.innerHTML = `<div class="control-empty">加载中</div>`;
      });
    }
    try {
      const snapshot = await collectControlSnapshot();
      renderControlPanel(snapshot);
    } catch (error) {
      console.error("Control panel failed:", error);
      const missingHost = document.getElementById("controlReadinessMissing");
      if (missingHost) missingHost.innerHTML = `<div class="control-empty bad">控制台加载失败</div>`;
    }
  }

  async function submitControlLogin(event) {
    event.preventDefault();
    const username = (document.getElementById("controlLoginUser") || {}).value || "";
    const passwordInput = document.getElementById("controlLoginPassword");
    const password = passwordInput ? passwordInput.value : "";
    setControlAuthMessage("正在登录...");
    try {
      lastControlAuth = await loginPlatformAuth(username.trim(), password);
      if (passwordInput) passwordInput.value = "";
      renderControlAuth(lastControlAuth);
      if (lastControlAuth.authenticated && !lastControlAuth.mustChangePassword) {
        refreshControlPanel();
      }
    } catch (error) {
      setControlAuthMessage(error.message || "登录失败", "bad");
    }
  }

  async function submitControlPasswordChange(event) {
    event.preventDefault();
    const currentInput = document.getElementById("controlCurrentPassword");
    const nextInput = document.getElementById("controlNewPassword");
    const confirmInput = document.getElementById("controlConfirmPassword");
    const currentPassword = currentInput ? currentInput.value : "";
    const newPassword = nextInput ? nextInput.value : "";
    const confirmPassword = confirmInput ? confirmInput.value : "";
    if (newPassword !== confirmPassword) {
      setControlAuthMessage("两次输入的新密码不一致", "bad");
      return;
    }
    setControlAuthMessage("正在修改密码...");
    try {
      lastControlAuth = await changePlatformPassword(currentPassword, newPassword, confirmPassword);
      [currentInput, nextInput, confirmInput].forEach((input) => { if (input) input.value = ""; });
      setControlAuthMessage("密码已修改", "good");
      renderControlAuth(lastControlAuth);
      refreshControlPanel();
    } catch (error) {
      setControlAuthMessage(error.message || "修改密码失败", "bad");
    }
  }

  async function logoutControl() {
    try {
      await logoutPlatformAuth();
    } catch (error) {
      // Logout is best effort; local UI should still return to the login screen.
    }
    lastControlAuth = { ok: true, enabled: true, authenticated: false };
    lastControlReport = null;
    renderControlAuth(lastControlAuth);
  }

  function setupControlPanel() {
    const loginForm = document.getElementById("controlLoginForm");
    if (loginForm && !loginForm.dataset.bound) {
      loginForm.addEventListener("submit", submitControlLogin);
      loginForm.dataset.bound = "1";
    }
    const passwordForm = document.getElementById("controlPasswordForm");
    if (passwordForm && !passwordForm.dataset.bound) {
      passwordForm.addEventListener("submit", submitControlPasswordChange);
      passwordForm.dataset.bound = "1";
    }
    const logoutBtn = document.getElementById("controlLogout");
    if (logoutBtn && !logoutBtn.dataset.bound) {
      logoutBtn.addEventListener("click", logoutControl);
      logoutBtn.dataset.bound = "1";
    }
    const refreshBtn = document.getElementById("controlRefresh");
    if (refreshBtn && !refreshBtn.dataset.bound) {
      refreshBtn.addEventListener("click", refreshControlPanel);
      refreshBtn.dataset.bound = "1";
    }
    const rescanBtn = document.getElementById("controlRescan");
    if (rescanBtn && !rescanBtn.dataset.bound) {
      rescanBtn.addEventListener("click", function () { triggerRescan(this); });
      rescanBtn.dataset.bound = "1";
    }
    ["controlSwitchConfig", "controlCoreConfig"].forEach((id) => {
      const lintInput = document.getElementById(id);
      if (lintInput && !lintInput.dataset.bound) {
        lintInput.addEventListener("input", renderControlLint);
        lintInput.dataset.bound = "1";
      }
    });
    configEditor.bind();
    const incidentCreate = document.getElementById("controlIncidentCreate");
    if (incidentCreate && !incidentCreate.dataset.bound) {
      incidentCreate.addEventListener("click", createControlIncident);
      incidentCreate.dataset.bound = "1";
    }
    renderControlLint();
  }

  function renderNav() {
    const nav = document.getElementById("screenNav");
    if (!nav) return;
    nav.hidden = true;
    nav.innerHTML = "";
  }

  function renderHeader(page) {
    const isHome = page && page.id === "home";
    const title = isHome ? page.title : titleText();
    const logoText = config.logoText || "";
    const brand = document.getElementById("brand");
    setText("screenTitle", title);
    setText("screenSubtitle", isHome ? page.description || "" : config.subtitle || "");
    setText("logoText", logoText);
    setText("brandMark", logoText ? logoText.slice(0, 1).toUpperCase() : "");
    brand.hidden = !logoText;
    document.title = title;
  }

  function stopInfraRefresh() {
    ispCarousel.deactivate();
    if (gaugeTimer) {
      window.clearInterval(gaugeTimer);
      gaugeTimer = null;
    }
    if (chartTimer) {
      window.clearInterval(chartTimer);
      chartTimer = null;
    }
    if (seenUpTimer) {
      window.clearInterval(seenUpTimer);
      seenUpTimer = null;
    }
  }

  function stopTournamentRefresh() {
    if (tournamentTimer) {
      window.clearInterval(tournamentTimer);
      tournamentTimer = null;
    }
  }

  function stopWirelessRefresh() {
    if (wirelessTimer) {
      window.clearInterval(wirelessTimer);
      wirelessTimer = null;
    }
  }

  function stopControlRefresh() {
    if (controlTimer) {
      window.clearInterval(controlTimer);
      controlTimer = null;
    }
  }

  function startInfraRefresh() {
    if (gaugeTimer || chartTimer) return;
    renderSignatures.clear();
    invalidateRangeCache();
    // Resolve the "deployed" set first so the first paint already hides
    // never-online targets; then keep it fresh on a slow timer.
    refreshInfraSeenUp().then(() => { refreshGauges(); refreshCharts(); });
    gaugeTimer = window.setInterval(refreshGauges, 5000);
    chartTimer = window.setInterval(refreshCharts, 5000);
    seenUpTimer = window.setInterval(refreshInfraSeenUp, 30000);
  }

  function startTournamentRefresh(page) {
    stopTournamentRefresh();
    renderSignatures.clear();
    invalidateRangeCache();
    refreshTournament(page);
    tournamentTimer = window.setInterval(() => refreshTournament(page), 5000);
    const refreshBtn = document.getElementById("tournamentRefresh");
    if (refreshBtn && !refreshBtn.dataset.bound) {
      refreshBtn.addEventListener("click", () => {
        const current = activePage();
        if (current && (current.kind === "match" || current.kind === "tournament")) {
          refreshTournament(current);
        }
      });
      refreshBtn.dataset.bound = "1";
    }
  }

  function startWirelessRefresh(page) {
    stopWirelessRefresh();
    const refresh = refreshWirelessOverview;
    refresh();
    wirelessTimer = window.setInterval(refresh, 5000);
    const rescanBtn = document.getElementById("wirelessRescan");
    if (rescanBtn) {
      rescanBtn.hidden = page.id === "wireless";
      if (!rescanBtn.dataset.bound) {
        rescanBtn.addEventListener("click", function () { triggerRescan(this); });
        rescanBtn.dataset.bound = "1";
      }
    }
  }

  function startControlRefresh() {
    stopControlRefresh();
    setupControlPanel();
    refreshControlPanel();
    controlTimer = window.setInterval(refreshControlPanel, 10000);
  }

  function setVisible(id, visible) {
    const element = document.getElementById(id);
    if (element) {
      element.hidden = !visible;
    }
  }

  function renderHomeCards() {
    const modeGrid = document.getElementById("modeGrid");
    modeGrid.innerHTML = pages
      .filter((page) => page.id !== "home")
      .map((page, index) => `
        <a class="mode-card ${page.kind ? "mode-card-match" : "mode-card-network"}" href="${page.path}">
          <span>${String(index + 1).padStart(2, "0")}</span>
          <strong>${escapeHtml(page.label)}</strong>
          <em>${escapeHtml(page.title)}</em>
          <b>${escapeHtml(page.description || "")}</b>
        </a>
      `).join("");
    modeGrid.querySelectorAll("a").forEach((link) => {
      link.addEventListener("click", (event) => {
        event.preventDefault();
        window.history.pushState({}, "", link.getAttribute("href"));
        renderPage();
      });
    });
  }

  function showHome() {
    const screen = document.querySelector(".screen");
    stopInfraRefresh();
    stopTournamentRefresh();
    stopWirelessRefresh();
    stopControlRefresh();
    dhcpPanel.stop();
    stopTopologyRefresh();
    screen.className = "screen home-mode";
    setVisible("homePanel", true);
    setVisible("panelGrid", false);
    setVisible("tournamentPanel", false);
    setVisible("evidencePanel", false);
    setVisible("wirelessPanel", false);
    setVisible("controlPanel", false);
    setVisible("dhcpPanel", false);
    setVisible("incidentPanel", false);
    setVisible("topologyPanel", false);
    renderHomeCards();
  }

  function showControl() {
    const screen = document.querySelector(".screen");
    stopInfraRefresh();
    stopTournamentRefresh();
    stopWirelessRefresh();
    dhcpPanel.stop();
    stopTopologyRefresh();
    screen.className = "screen control-mode";
    setVisible("homePanel", false);
    setVisible("panelGrid", false);
    setVisible("tournamentPanel", false);
    setVisible("evidencePanel", false);
    setVisible("wirelessPanel", false);
    setVisible("controlPanel", true);
    setVisible("dhcpPanel", false);
    setVisible("incidentPanel", false);
    setVisible("topologyPanel", false);
    startControlRefresh();
  }

  function showInfra() {
    const screen = document.querySelector(".screen");
    stopTournamentRefresh();
    stopWirelessRefresh();
    stopControlRefresh();
    dhcpPanel.stop();
    stopTopologyRefresh();
    screen.className = "screen infra-mode";
    ispCarousel.deactivate();
    renderIspPanels(ispTrafficResults);
    setVisible("homePanel", false);
    setVisible("panelGrid", true);
    setVisible("tournamentPanel", false);
    setVisible("evidencePanel", false);
    setVisible("wirelessPanel", false);
    setVisible("controlPanel", false);
    setVisible("dhcpPanel", false);
    setVisible("incidentPanel", false);
    setVisible("topologyPanel", false);
    startInfraRefresh();
  }

  function showTournament(page) {
    const screen = document.querySelector(".screen");
    stopWirelessRefresh();
    stopControlRefresh();
    dhcpPanel.stop();
    stopTopologyRefresh();
    screen.className = `screen tournament-mode ${page.kind === "match" ? "match-mode" : "multi-team-mode"} ${page.id}`;
    ispCarousel.activate({ reset: true });
    renderIspPanels(ispTrafficResults);
    setVisible("homePanel", false);
    setVisible("panelGrid", true);
    setVisible("tournamentPanel", true);
    setVisible("evidencePanel", false);
    setVisible("wirelessPanel", false);
    setVisible("controlPanel", false);
    setVisible("dhcpPanel", false);
    setVisible("incidentPanel", false);
    setVisible("topologyPanel", false);
    document.getElementById("tournamentPanel").className = `tournament-panel ${page.kind === "match" ? "match-panel" : "multi-team-panel"} ${page.id}`;
    startInfraRefresh();
    startTournamentRefresh(page);
  }

  function showEvidence() {
    const screen = document.querySelector(".screen");
    stopInfraRefresh();
    stopTournamentRefresh();
    stopWirelessRefresh();
    stopControlRefresh();
    dhcpPanel.stop();
    stopTopologyRefresh();
    screen.className = "screen evidence-mode";
    setVisible("homePanel", false);
    setVisible("panelGrid", false);
    setVisible("tournamentPanel", false);
    setVisible("evidencePanel", true);
    setVisible("wirelessPanel", false);
    setVisible("controlPanel", false);
    setVisible("dhcpPanel", false);
    setVisible("incidentPanel", false);
    setVisible("topologyPanel", false);
    evidencePanel.start();
  }

  function showWireless(page) {
    const screen = document.querySelector(".screen");
    stopInfraRefresh();
    stopTournamentRefresh();
    stopControlRefresh();
    dhcpPanel.stop();
    stopTopologyRefresh();
    screen.className = `screen wireless-mode ${page.id}-mode`;
    setVisible("homePanel", false);
    setVisible("panelGrid", false);
    setVisible("tournamentPanel", false);
    setVisible("evidencePanel", false);
    setVisible("wirelessPanel", true);
    setVisible("controlPanel", false);
    setVisible("dhcpPanel", false);
    setVisible("incidentPanel", false);
    setVisible("topologyPanel", false);
    startWirelessRefresh(page);
  }

  // ---- Incident root-cause analysis ----

  function incidentWindow() {
    const atInput = document.getElementById("incidentAt");
    const windowInput = document.getElementById("incidentWindow");
    const centerDate = atInput && atInput.value ? new Date(atInput.value) : new Date();
    const center = Number.isFinite(centerDate.getTime()) ? centerDate.getTime() / 1000 : Date.now() / 1000;
    const minutes = Math.max(1, Number(windowInput && windowInput.value ? windowInput.value : 5));
    const now = Math.floor(Date.now() / 1000);
    const end = Math.min(Math.floor(center + minutes * 60), now);
    const start = Math.floor(center - minutes * 60);
    return {
      start: start <= end ? start : Math.max(0, end - minutes * 60),
      end,
      step: 5,
      minutes
    };
  }

  async function queryIncidentData(win) {
    const playerLatencyQ = 'probe_icmp_duration_seconds{role="player",network="wired",phase="rtt"}';
    const playerSuccessQ = 'probe_success{role="player",network="wired"}';
    const infraLatencyQ = 'probe_icmp_duration_seconds{job=~"infra-isp-ping|infra-core-ping|infra-dist-ping|infra-fw-ping|infra-srv-ping",phase="rtt"}';
    const infraSuccessQ = 'probe_success{job=~"infra-isp-ping|infra-core-ping|infra-dist-ping|infra-fw-ping|infra-srv-ping"}';

    const ispNames = await fetchIspNames();
    const ispPromises = ispNames.flatMap((name, index) => [
      prometheusRangeFor(ispTrafficQuery("ifHCInOctets", name), win).then((series) => series.map((s) => ({ ...s, _ispName: name, _ispIndex: index, _direction: "in" }))),
      prometheusRangeFor(ispTrafficQuery("ifHCOutOctets", name), win).then((series) => series.map((s) => ({ ...s, _ispName: name, _ispIndex: index, _direction: "out" })))
    ]);

    const [playerLatency, playerSuccess, infraLatency, infraSuccess, ...ispArrays] = await Promise.all([
      prometheusRangeFor(playerLatencyQ, win),
      prometheusRangeFor(playerSuccessQ, win),
      prometheusRangeFor(infraLatencyQ, win),
      prometheusRangeFor(infraSuccessQ, win),
      ...ispPromises
    ]);
    const isp = ispArrays.flat();
    return { playerLatency, playerSuccess, infraLatency, infraSuccess, isp };
  }

  function renderIncidentVerdict(verdict) {
    const element = document.getElementById("incidentVerdict");
    element.className = `incident-verdict ${verdict.level}`;
    element.innerHTML = `
      <strong>${escapeHtml(verdict.text)}</strong>
      <span>${escapeHtml(verdict.detail)}</span>
    `;
  }

  function renderIncidentPlayers(result) {
    const element = document.getElementById("incidentPlayers");
    const items = [
      ...result.affectedPlayers.map((player) => ({
        type: "warn",
        label: `Team ${player.team} S${player.seat} (${networkLabel(player.network)})`,
        detail: `最高 ${formatPingText(player.maxLatency)}`,
        ip: player.instance
      })),
      ...result.offlinePlayers.map((player) => ({
        type: "bad",
        label: `Team ${player.team} S${player.seat} (${networkLabel(player.network)})`,
        detail: `${player.recoveryCount} 次断线后恢复`,
        ip: player.instance
      }))
    ];

    if (!items.length) {
      element.innerHTML = `<div class="incident-empty">没有选手超过阈值</div>`;
      return;
    }

    element.innerHTML = items.map((item) => `
      <div class="incident-item ${item.type}">
        <strong>${escapeHtml(item.label)}</strong>
        <em>${escapeHtml(item.ip || "")}</em>
        <span>${escapeHtml(item.detail)}</span>
      </div>
    `).join("");
  }

  function renderIncidentInfra(result) {
    const element = document.getElementById("incidentInfra");
    if (!result.infraEvents.length) {
      element.innerHTML = `<div class="incident-empty">基础设施正常</div>`;
      return;
    }

    element.innerHTML = result.infraEvents.map((event) => `
      <div class="incident-item ${event.offline ? "bad" : "warn"}">
        <strong>${escapeHtml(event.instance || event.targetIp || "?")}</strong>
        <em>${escapeHtml(event.job)}</em>
        <span>${event.offline ? `${event.recoveryCount} 次断线后恢复` : `最高 ${formatPingText(event.maxLatency)}`}</span>
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
    stopWirelessRefresh();
    stopControlRefresh();
    dhcpPanel.stop();
    stopTopologyRefresh();
    screen.className = "screen incident-mode";
    setVisible("homePanel", false);
    setVisible("panelGrid", false);
    setVisible("tournamentPanel", false);
    setVisible("evidencePanel", false);
    setVisible("wirelessPanel", false);
    setVisible("controlPanel", false);
    setVisible("dhcpPanel", false);
    setVisible("incidentPanel", true);
    setVisible("topologyPanel", false);
    setupIncidentPanel();
  }

  // ---- Network topology ----

  let topologyTimer = null;

  function stopTopologyRefresh() {
    if (topologyTimer) {
      window.clearInterval(topologyTimer);
      topologyTimer = null;
    }
  }

  async function refreshTopology() {
    if (!topologyPanel.isAvailable()) return;
    const seq = ++topologySeq;
    try {
      const [allTargets, edges, seenItems] = await Promise.all([
        fetchTopologyTargets(),
        fetchTopologyEdges(),
        prometheusInstant(activeInfraPingQuery()).catch(() => [])
      ]);
      if (seq !== topologySeq) return;
      // 与网络总览一致：隐藏从没上线过的设备（按 instance 名匹配 seen-up 集合）。
      const seenUp = activeSeriesNames(seenItems);
      const targets = seenUp.size
        ? allTargets.filter((t) => t.job === "infra-fw-unit-snmp" || t.job === "infra-isp-ping" || seenUp.has(t.instance))
        : allTargets;
      const { layout, width } = topologyPanel.prepare(targets, edges);
      if (shouldRender("topology", topologySignature(layout, width, edges))) {
        topologyPanel.render({ layout, width });
      } else {
        // Same structure and status levels: refresh only the latency readouts
        // in place, keeping the pan/zoom view and skipping the SVG rebuild.
        topologyPanel.updateLatency(layout.nodes);
      }
      topologyPanel.updateStatus(edges);
      lastDataSuccessAt = Date.now();
    } catch (error) {
      if (seq !== topologySeq) return;
      // The error message replaces the SVG, so the next success must rebuild
      // even when the data signature is unchanged.
      renderSignatures.delete("topology");
      console.error("Topology fetch failed:", error);
      topologyPanel.showError(error.message || "");
    }
  }

  // Skip the SVG rebuild when nothing the layout depends on changed: node set,
  // kinds, names, status levels, the LLDP edge list and the canvas width. Raw
  // latency is excluded on purpose -- it jitters every sample and is patched
  // into the existing DOM through the panel's incremental update instead.
  function topologySignature(layout, width, edges) {
    const nodesSig = layout.nodes.map((node) => `${node.kind}|${node.ip || ""}|${node.name}|${node.level}`).join("#");
    const edgesSig = (edges || []).map((edge) => [
      edge.from_ip, edge.from_port, (edge.from_member_ports || []).join(","), edge.from_aggregate_port,
      edge.to_ip, edge.to_port, (edge.to_member_ports || []).join(","), edge.to_aggregate_port,
      edge.stale === true ? "stale" : "live"
    ].join("|")).join("#");
    return `${width}@${nodesSig}@@${edgesSig}`;
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
    stopWirelessRefresh();
    stopControlRefresh();
    dhcpPanel.stop();
    screen.className = "screen topology-mode";
    setVisible("homePanel", false);
    setVisible("panelGrid", false);
    setVisible("tournamentPanel", false);
    setVisible("evidencePanel", false);
    setVisible("wirelessPanel", false);
    setVisible("controlPanel", false);
    setVisible("dhcpPanel", false);
    setVisible("incidentPanel", false);
    setVisible("topologyPanel", true);
    topologyPanel.clearDetail();
    topologyPanel.resetView();
    startTopologyRefresh();
  }

  function showDhcp() {
    const screen = document.querySelector(".screen");
    stopInfraRefresh();
    stopTournamentRefresh();
    stopWirelessRefresh();
    stopControlRefresh();
    stopTopologyRefresh();
    screen.className = "screen dhcp-mode";
    setVisible("homePanel", false);
    setVisible("panelGrid", false);
    setVisible("tournamentPanel", false);
    setVisible("evidencePanel", false);
    setVisible("wirelessPanel", false);
    setVisible("controlPanel", false);
    setVisible("dhcpPanel", true);
    setVisible("incidentPanel", false);
    setVisible("topologyPanel", false);
    dhcpPanel.start();
  }

  function renderPage() {
    const page = pageFromPath();
    renderHeader(page);
    renderNav(page);
    const routeKey = `${page.id}${window.location.search}`;
    if (routeKey === activeRoute) return;
    activePageId = page.id;
    activeRoute = routeKey;
    if (page.id !== "evidence") evidencePanel.stop();
    // SPA route changes otherwise preserve the long mobile home page's scroll
    // offset and can open a dashboard halfway down its content.
    window.scrollTo({ top: 0, left: 0, behavior: "auto" });
    if (page.id === "home") {
      showHome();
    } else if (page.id === "control") {
      showControl();
    } else if (page.id === "dhcp") {
      showDhcp();
    } else if (page.id === "evidence") {
      showEvidence();
    } else if (page.id === "incident") {
      showIncident();
    } else if (page.id === "topology") {
      showTopology();
    } else if (page.id === "wireless") {
      showWireless(page);
    } else if (page.kind) {
      showTournament(page);
    } else {
      showInfra();
    }
  }

  function anyRefreshActive() {
    return Boolean(gaugeTimer || chartTimer || tournamentTimer || wirelessTimer || controlTimer || dhcpPanel.hasScheduledRefresh() || topologyTimer);
  }

  // Warn when the active page's polling loop hasn't produced fresh data for a
  // while (network stall, Prometheus down, or a frozen refresh loop), so a
  // stale screen is never mistaken for live data.
  function updateFreshness() {
    const badge = document.getElementById("dataFreshness");
    if (!badge) return;
    const staleAfter = activePageId === "dhcp" ? 90000 : DATA_STALE_AFTER_MS;
    const stale = anyRefreshActive() && lastDataSuccessAt > 0 && (Date.now() - lastDataSuccessAt) > staleAfter;
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
