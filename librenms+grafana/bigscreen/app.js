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
  const {
    cloneControlConfig, asConfigArray, configScalar, csvText, splitConfigList,
    controlConfigDefaults, configPathGet, configPathSet, expandIpRangeText
  } = window.BSConfigModel;
  const { createLineChartRenderer } = window.BSLineChart;
  const { createPingChartRenderer } = window.BSPingChart;
  const { createLossHeatmapRenderer } = window.BSLossHeatmap;
  const { createIspChartRenderer } = window.BSIspChart;
  const { createEvidenceChartRenderer } = window.BSEvidenceChart;
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
  let dhcpTimer = null;
  let dhcpSeq = 0;
  let dhcpRefreshing = false;
  let dhcpHasData = false;
  let dhcpLastPayload = null;
  let dhcpBindingPayload = null;
  let dhcpBindingsRefreshing = false;
  let dhcpSelectedPoolKey = "";
  let dhcpPoolSearchText = "";
  let dhcpPoolFilterValue = "all";
  let activePageId = "";
  let activeRoute = "";
  let gaugeSeq = 0;
  let chartSeq = 0;
  let tournamentSeq = 0;
  let evidenceSeq = 0;
  let topologySeq = 0;
  let stageDeviceRegexCache = null;
  let ispTrafficResults = [];
  const renderSignatures = new Map();
  let lastDataSuccessAt = 0;
  let lastControlReport = null;
  let lastControlAuth = null;
  let lastPlatformConfig = null;
  let lastDhcpSettings = null;
  let lastEditableConfig = null;
  let lastIncidents = [];
  let configResultSticky = false;
  let applyInProgress = false;
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

  // CSV export for the operator query pages (/latency) -- raw
  // data to attach to dispute reports alongside screenshots. Not wired to
  // any TV-facing page.
  function downloadCsv(filename, rows) {
    const blob = new Blob([buildCsv(rows)], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function csvStamp(timestamp) {
    return formatTimestampFull(timestamp).replace(/[: ]/g, "-");
  }

  function evidenceWindow() {
    const atInput = document.getElementById("evidenceAt");
    const windowInput = document.getElementById("evidenceWindow");
    const centerDate = atInput && atInput.value ? new Date(atInput.value) : new Date();
    const center = Number.isFinite(centerDate.getTime()) ? centerDate.getTime() / 1000 : Date.now() / 1000;
    // The dropdown value is the TOTAL window (minutes), centered on the query time.
    const minutes = Math.max(1, Number(windowInput && windowInput.value ? windowInput.value : 10));
    const half = (minutes * 60) / 2;
    const now = Math.floor(Date.now() / 1000);
    const end = Math.min(Math.floor(center + half), now);
    const start = Math.floor(center - half);
    return {
      start: start <= end ? start : Math.max(0, end - minutes * 60),
      end,
      // Evidence pages are used after a dispute, so keep short ISP flaps visible.
      step: 1
    };
  }

  function evidencePlayerSelector(team, seat, network) {
    const networkFilter = network === "all" ? 'network=~".*"' : `network="${escapeLabel(network)}"`;
    return `role="player",team="${escapeLabel(team)}",seat="${escapeLabel(seat)}",${networkFilter}`;
  }

  function evidenceIpMatchers(ips) {
    const values = (Array.isArray(ips) ? ips : [ips])
      .map((value) => String(value || "").trim())
      .filter(Boolean);
    if (!values.length) return null;
    if (values.length === 1) {
      const ip = escapeLabel(values[0]);
      return [`instance="${ip}"`, `target_ip="${ip}"`];
    }
    const regex = escapeLabel(`^(?:${values.map(escapeRegex).join("|")})$`);
    return [`instance=~"${regex}"`, `target_ip=~"${regex}"`];
  }

  function evidenceLatencyQuery(team, seat, network, ips) {
    const matchers = evidenceIpMatchers(ips);
    if (matchers) {
      return matchers.map((matcher) => `probe_icmp_duration_seconds{${matcher},phase="rtt"}`).join(" or ");
    }
    return `probe_icmp_duration_seconds{${evidencePlayerSelector(team, seat, network)},phase="rtt"}`;
  }

  function evidenceSuccessQuery(team, seat, network, ips) {
    const matchers = evidenceIpMatchers(ips);
    if (matchers) {
      return matchers.map((matcher) => `probe_success{${matcher}}`).join(" or ");
    }
    return `probe_success{${evidencePlayerSelector(team, seat, network)}}`;
  }

  async function resolveEvidenceCurrentIps(team, seat, network) {
    // An instant selector excludes Prometheus series made stale when the seat
    // moved to a new address. A look-back window here would briefly return
    // both the previous and current IP after target regeneration.
    const items = await prometheusInstant(`probe_success{${evidencePlayerSelector(team, seat, network)}}`);
    return [...new Set(items
      .map((item) => String(item.metric.target_ip || item.metric.instance || "").trim())
      .filter(Boolean))]
      .sort((left, right) => left.localeCompare(right, "zh-CN", { numeric: true }));
  }

  function evidenceSeriesName(metric) {
    const seat = metric.seat ? `S${metric.seat}` : "";
    const ip = metric.instance || "";
    const network = metric.network ? ` ${networkLabel(metric.network)}` : "";
    return `${seat} ${ip}${network}`.trim() || "选手";
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

  let lastEvidenceExport = null;

  function exportEvidenceCsv() {
    if (!lastEvidenceExport) return;
    const { latencySeries, successSeries, queryWindow, slug } = lastEvidenceExport;
    const rows = [["time", "series", "metric", "value"]];
    latencySeries.forEach((series) => {
      series.values.forEach((point) => {
        rows.push([formatTimestampFull(point.t), series.name, "latency_ms", (point.v * 1000).toFixed(2)]);
      });
    });
    successSeries.forEach((series) => {
      series.values.forEach((point) => {
        rows.push([formatTimestampFull(point.t), series.name, "online", String(point.v)]);
      });
    });
    downloadCsv(`latency_${slug}_${csvStamp(queryWindow.start)}_${csvStamp(queryWindow.end)}.csv`, rows);
  }

  async function queryEvidence() {
    const seq = ++evidenceSeq;
    const team = document.getElementById("evidenceTeam").value || "1";
    const seat = document.getElementById("evidenceSeat").value || "1";
    const network = document.getElementById("evidenceNetwork").value || "wired";
    const range = document.getElementById("evidenceWindow").value || "5";
    const at = document.getElementById("evidenceAt").value || "";
    const requestedIp = (document.getElementById("evidenceIp").value || "").trim();
    const queryWindow = evidenceWindow();
    const params = new URLSearchParams({ team, seat, network, range });
    if (at) params.set("at", at);
    if (requestedIp) params.set("ip", requestedIp);
    window.history.replaceState({}, "", `/latency?${params.toString()}`);

    renderNoData(document.getElementById("evidenceLatencyChart"), "加载中");
    renderNoData(document.getElementById("evidenceSuccessChart"), "加载中");

    try {
      const currentIps = requestedIp ? [requestedIp] : await resolveEvidenceCurrentIps(team, seat, network);
      if (seq !== evidenceSeq || activePageId !== "evidence") return;
      if (!currentIps.length) {
        lastEvidenceExport = null;
        renderNoData(document.getElementById("evidenceSummary"), `${playerLabel(team, seat, network)} 当前没有可查询的 IP`);
        renderNoData(document.getElementById("evidenceLatencyChart"), "当前座位未生成监控目标");
        renderNoData(document.getElementById("evidenceSuccessChart"), "当前座位未生成监控目标");
        return;
      }
      const latencyQuery = evidenceLatencyQuery(team, seat, network, currentIps);
      const successQuery = evidenceSuccessQuery(team, seat, network, currentIps);
      const ipLabel = currentIps.join("、");
      const label = requestedIp
        ? `${ipLabel} · 指定 IP · ${formatTime(queryWindow.start)}-${formatTime(queryWindow.end)}`
        : `${playerLabel(team, seat, network)} · 当前 IP ${ipLabel} · ${formatTime(queryWindow.start)}-${formatTime(queryWindow.end)}`;
      const [latencySeries, successSeries] = await Promise.all([
        prometheusRangeFor(latencyQuery, queryWindow, evidenceSeriesName),
        prometheusRangeFor(successQuery, queryWindow, evidenceSeriesName)
      ]);
      if (seq !== evidenceSeq || activePageId !== "evidence") return;
      lastEvidenceExport = {
        latencySeries,
        successSeries,
        queryWindow,
        slug: requestedIp || `T${team}S${seat}`
      };
      renderEvidenceCharts({
        summaryContainerId: "evidenceSummary",
        latencyContainerId: "evidenceLatencyChart",
        successContainerId: "evidenceSuccessChart",
        context: { label },
        latencySeries,
        successSeries
      });
    } catch (error) {
      if (seq !== evidenceSeq || activePageId !== "evidence") return;
      renderNoData(document.getElementById("evidenceSummary"), "查询失败");
      renderNoData(document.getElementById("evidenceLatencyChart"));
      renderNoData(document.getElementById("evidenceSuccessChart"));
      console.error(error);
    }
  }

  function setupEvidencePanel() {
    const atInput = document.getElementById("evidenceAt");
    const form = document.getElementById("evidenceForm");
    const params = new URLSearchParams(window.location.search);
    const team = params.get("team");
    const seat = params.get("seat");
    const network = params.get("network");
    const range = params.get("range") || params.get("window");
    const at = params.get("at");
    const ip = params.get("ip");
    if (team) document.getElementById("evidenceTeam").value = team;
    if (seat) document.getElementById("evidenceSeat").value = seat;
    if (["wired", "wireless", "all"].includes(network)) document.getElementById("evidenceNetwork").value = network;
    if (range) document.getElementById("evidenceWindow").value = range;
    document.getElementById("evidenceIp").value = ip || "";
    if (atInput && at) {
      atInput.value = at;
    } else if (atInput && !atInput.value) {
      atInput.value = dateTimeInputValue(new Date());
    }
    if (form && !form.dataset.bound) {
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        queryEvidence();
      });
      // Re-run as soon as a control changes (range/time/network dropdowns, team/seat)
      // so picking a range applies immediately -- no need to focus IP and press Enter.
      form.addEventListener("change", (event) => {
        if (["evidenceTeam", "evidenceSeat", "evidenceNetwork"].includes(event.target && event.target.id)) {
          document.getElementById("evidenceIp").value = "";
        }
        queryEvidence();
      });
      form.dataset.bound = "1";
    }
    const exportBtn = document.getElementById("evidenceExport");
    if (exportBtn && !exportBtn.dataset.bound) {
      exportBtn.addEventListener("click", exportEvidenceCsv);
      exportBtn.dataset.bound = "1";
    }
    queryEvidence();
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

  function renderControlDhcpSettings(settings) {
    lastDhcpSettings = settings;
    const editor = document.getElementById("controlConfigForm");
    const form = document.getElementById("controlDhcpSettingsForm");
    const username = document.getElementById("controlDhcpUsername");
    const port = document.getElementById("controlDhcpPort");
    const password = document.getElementById("controlDhcpPassword");
    const enablePassword = document.getElementById("controlDhcpEnablePassword");
    const state = document.getElementById("controlDhcpSavedState");
    if (!form) return;
    if (!settings || !settings.ok) {
      if (state) state.textContent = (settings && settings.error) || "无法读取 Telnet 配置";
      return;
    }
    if (!editor || !editor.dataset.telnetDirty) {
      if (username) username.value = settings.username || "";
      if (port) port.value = String(settings.port || 23);
      if (password) password.value = "";
      if (enablePassword) enablePassword.value = "";
    }
    if (password) {
      password.placeholder = settings.passwordConfigured
        ? "已保存；留空保留原密码"
        : "尚未设置登录密码";
    }
    if (enablePassword) {
      enablePassword.placeholder = settings.enablePasswordConfigured
        ? "已保存；留空保留原密码"
        : "没有 Enable 密码可留空";
    }
    if (state) {
      const passwordState = settings.passwordConfigured ? "登录密码已保存" : "登录密码未设置";
      const enableState = settings.enablePasswordConfigured ? "Enable 密码已保存" : "未设置 Enable 密码";
      state.textContent = `${passwordState} · ${enableState}`;
    }
  }

  async function saveAndTestControlDhcpSettings(event) {
    event.preventDefault();
    const form = document.getElementById("controlDhcpSettingsForm");
    const button = document.getElementById("controlDhcpSaveTest");
    const result = document.getElementById("controlDhcpSettingsResult");
    const username = document.getElementById("controlDhcpUsername");
    const password = document.getElementById("controlDhcpPassword");
    const enablePassword = document.getElementById("controlDhcpEnablePassword");
    const port = document.getElementById("controlDhcpPort");
    const editor = document.getElementById("controlConfigForm");
    if (!form || !result) return;
    const credentials = {
      username: username ? username.value.trim() : "",
      password: password ? password.value : "",
      enablePassword: enablePassword ? enablePassword.value : "",
      port: port ? port.value : "23"
    };
    let settingsSaved = false;
    if (button) button.disabled = true;
    result.hidden = false;
    result.className = "network-tool-result loading";
    result.textContent = "正在保存当前基础配置和 Telnet 信息…";
    try {
      const configPayload = collectControlConfigForm();
      const savedConfig = await postPlatform("/config/save", {
        text: JSON.stringify(configPayload, null, 2),
        actor: "web",
        note: "save core config before Telnet test"
      });
      if (!savedConfig || !savedConfig.ok) {
        if (savedConfig) renderConfigResult({ ...savedConfig, action: "save" });
        throw new Error((savedConfig && savedConfig.error) || "基础配置验证未通过");
      }
      lastPlatformConfig = savedConfig;
      lastEditableConfig = controlConfigDefaults(savedConfig.config || configPayload);
      if (editor) delete editor.dataset.dirty;
      configResultSticky = true;
      renderConfigResult({ ...savedConfig, action: "save" });

      const settings = await saveDhcpSettings(credentials);
      settingsSaved = true;
      if (password) password.value = "";
      if (enablePassword) enablePassword.value = "";
      if (editor) delete editor.dataset.telnetDirty;
      renderControlDhcpSettings(settings);
      result.className = "network-tool-result loading";
      result.textContent = "配置已保存，正在测试核心交换机连接…";
      const connection = await testDhcpConnection();
      result.className = `network-tool-result ${connection.privileged ? "good" : "warn"}`;
      result.textContent = `核心 IP 和 Telnet 信息已保存。${connection.message} · ${connection.host}:${connection.port}`;
    } catch (error) {
      result.className = "network-tool-result bad";
      result.textContent = settingsSaved
        ? `配置已保存，但连接测试失败：${error.message || "未知错误"}`
        : `保存失败：${error.message || "未知错误"}`;
    } finally {
      if (button) button.disabled = false;
    }
  }

  function renderConfigResult(payload) {
    const result = document.getElementById("controlConfigResult");
    if (!result) return;
    if (!payload || (payload.passive && !(payload.issues && payload.issues.length))) {
      result.innerHTML = `
        <div class="control-apply-next">
          <strong>配置流程</strong>
          <span>先点“验证”，确认无误后点“保存”或“应用配置”。</span>
        </div>
      `;
      return;
    }
    if (payload.pending) {
      result.innerHTML = `
        <div class="control-apply-next pending">
          <strong>正在${escapeHtml(payload.pendingLabel || "处理")}…</strong>
          <span>${escapeHtml(payload.pendingNote || "请稍候，不要重复点击或刷新页面。")}</span>
        </div>
      `;
      return;
    }
    if (!payload.ok && payload.error) {
      result.innerHTML = `
        <div class="control-apply-next bad">
          <strong>${escapeHtml(payload.errorTitle || "操作失败")}</strong>
          <span>${escapeHtml(payload.error)}</span>
        </div>
        ${payload.applyOutput ? `<pre class="control-apply-log">${escapeHtml(payload.applyOutput)}</pre>` : ""}
      `;
      return;
    }
    const issues = payload.issues || [];
    const issuesHtml = issues.map((item) => controlItemHtml({
      section: item.path || "配置",
      label: item.message || "配置项",
      level: item.level || "info",
      value: (item.level || "info").toUpperCase(),
      note: ""
    })).join("");
    let headline;
    if (payload.action === "rollback" && payload.applied) {
      headline = `
        <div class="control-apply-next good">
          <strong>↩ 回滚并应用完成</strong>
          <span>配置与 .env 已恢复到同一个历史版本，相关服务已重新验证。</span>
        </div>`;
    } else if (payload.applied) {
      headline = `
        <div class="control-apply-next good">
          <strong>🚀 应用完成</strong>
          <span>配置已写入 .env，相关容器已重启生效。</span>
        </div>`;
    } else if (payload.needsRedeploy) {
      headline = `
        <div class="control-apply-next warn">
          <strong>已保存，待应用</strong>
          <span>.env 已更新；点“应用配置”重启相关容器后才会生效。</span>
        </div>`;
    } else if (payload.action === "save") {
      headline = `
        <div class="control-apply-next good">
          <strong>💾 已保存</strong>
          <span>event-config.yml 已保存。点“应用配置”生成 .env 并让服务重启生效。</span>
        </div>`;
    } else if (payload.action === "rollback") {
      headline = `
        <div class="control-apply-next warn">
          <strong>↩ 已恢复文件，等待部署</strong>
          <span>配置与 .env 已成对恢复；当前环境关闭了自动应用，需要手动部署。</span>
        </div>`;
    } else if (issues.length) {
      headline = "";
    } else {
      headline = `
        <div class="control-apply-next good">
          <strong>✅ 验证通过</strong>
          <span>配置无误，可点“保存”或“应用配置”。</span>
        </div>`;
    }
    result.innerHTML = `${issuesHtml}${headline}`;
  }

  function configInput(path, label, options = {}) {
    const value = configPathGet(lastEditableConfig || {}, path);
    const id = `cfg-${path.replace(/[^a-z0-9]+/gi, "-")}`;
    const common = `id="${escapeHtml(id)}" data-config-path="${escapeHtml(path)}"${options.number ? ' data-config-number="1"' : ""}`;
    const fieldClasses = ["config-field"];
    if (options.compact) fieldClasses.push("config-field-compact");
    if (options.type === "checkbox") {
      const classes = ["config-field", "config-field-check"];
      if (options.compactCheck) classes.push("config-field-check-inline");
      return `
        <label class="${classes.join(" ")}" for="${escapeHtml(id)}">
          <input ${common} type="checkbox"${value ? " checked" : ""} />
          <span>${escapeHtml(label)}</span>
        </label>
      `;
    }
    if (options.type === "select") {
      return `
        <label class="${fieldClasses.join(" ")}" for="${escapeHtml(id)}">
          <span>${escapeHtml(label)}</span>
          <select ${common}>
            ${(options.choices || []).map((item) => `<option value="${escapeHtml(item.value)}"${String(value || "") === String(item.value) ? " selected" : ""}>${escapeHtml(item.label)}</option>`).join("")}
          </select>
        </label>
      `;
    }
    if (options.type === "textarea") {
      const textareaClasses = fieldClasses.slice();
      if (options.wide || !options.compact) textareaClasses.push("config-field-wide");
      return `
        <label class="${textareaClasses.join(" ")}" for="${escapeHtml(id)}">
          <span>${escapeHtml(label)}</span>
          <textarea ${common} rows="${options.rows || 2}" placeholder="${escapeHtml(options.placeholder || "")}">${escapeHtml(csvText(value))}</textarea>
        </label>
      `;
    }
    return `
      <label class="${fieldClasses.join(" ")}" for="${escapeHtml(id)}">
        <span>${escapeHtml(label)}</span>
        <input ${common} type="${escapeHtml(options.inputType || (options.number ? "number" : "text"))}" value="${escapeHtml(configScalar(value))}" placeholder="${escapeHtml(options.placeholder || "")}" />
      </label>
    `;
  }

  function teamOrderConfigMarkup() {
    const layoutId = String((lastEditableConfig.event || {}).default_layout || "");
    const page = pages.find((item) => item.id === layoutId);
    const configurable = Array.isArray(teamLayouts.configurableLayoutIds)
      && teamLayouts.configurableLayoutIds.includes(layoutId);
    if (!page || !configurable || typeof teamLayouts.teamOrderForPage !== "function") return "";
    const order = teamLayouts.teamOrderForPage(page, lastEditableConfig.event.team_orders);
    const groups = Array.isArray(page.groups) ? page.groups : [page.teams || []];
    let slotIndex = 0;
    return `
      <div class="team-order-editor" data-team-order-layout="${escapeHtml(layoutId)}">
        <div class="team-order-heading">
          <div>
            <strong>队号位置</strong>
            <span>按舞台从上到下、每排从左到右排列；选择重复队号时会自动互换。</span>
          </div>
          <button type="button" data-team-order-reset="${escapeHtml(layoutId)}">恢复默认顺序</button>
        </div>
        <div class="team-order-stage">
          ${groups.map((group, groupIndex) => {
            const sideSize = Math.ceil(group.length / 2);
            const rowLabels = groups.length === 3 ? ["上层", "中层", "下层"] : ["上层", "下层"];
            const rowLabel = rowLabels[groupIndex] || `第 ${groupIndex + 1} 层`;
            return `
            <div class="team-order-row" style="--team-order-side-count:${sideSize}">
              ${group.map((_team, groupSlotIndex) => {
                const currentIndex = slotIndex;
                const selectedTeam = order[slotIndex];
                const side = groupSlotIndex < sideSize ? "左侧" : "右侧";
                const sideSlot = groupSlotIndex < sideSize ? groupSlotIndex + 1 : groupSlotIndex - sideSize + 1;
                slotIndex += 1;
                return `
                  <label${groupSlotIndex === sideSize ? ` class="team-order-side-start" style="grid-column-start:${sideSize + 2}"` : ""}>
                    <span>${rowLabel} · ${side}第 ${sideSlot} 位</span>
                    <select data-team-order-slot="${currentIndex}" data-team-order-previous="${selectedTeam}">
                      ${(page.teams || []).map((team) => `<option value="${team}"${team === selectedTeam ? " selected" : ""}>第 ${team} 队</option>`).join("")}
                    </select>
                  </label>
                `;
              }).join("")}
            </div>
          `;
          }).join("")}
        </div>
      </div>
    `;
  }

  function configListRows(name, rows, columns) {
    const addLabels = {
      stage_switches: "赛事交换机",
      access_switches: "普通接入交换机",
      switches: "交换机",
      servers: "服务器",
      isp: "ISP"
    };
    const supportsRange = name === "stage_switches" || name === "access_switches";
    return `
      <div class="config-list" data-config-list="${escapeHtml(name)}">
        ${supportsRange ? `
          <div class="config-range-row">
            <input type="text" data-config-range-input="${escapeHtml(name)}" placeholder="范围或多个 IP" />
            <button type="button" data-config-add-range="${escapeHtml(name)}">添加范围</button>
          </div>
        ` : ""}
        ${rows.map((row, index) => `
          <div class="config-list-row" data-index="${index}">
            ${columns.map((column) => `
              <label>
                <span>${escapeHtml(column.label)}</span>
                <input data-config-key="${escapeHtml(column.key)}"${column.number ? ' data-config-number="1"' : ""} type="${column.number ? "number" : "text"}" value="${escapeHtml(configScalar(row[column.key]))}" placeholder="${escapeHtml(column.placeholder || "")}" />
              </label>
            `).join("")}
            <button type="button" data-config-remove="${escapeHtml(name)}" data-index="${index}">删除</button>
          </div>
        `).join("")}
        <button class="config-add-row" type="button" data-config-add="${escapeHtml(name)}">添加${escapeHtml(addLabels[name] || "条目")}</button>
      </div>
    `;
  }

  function controlDhcpSettingsMarkup() {
    return `
      <div class="config-private-section" id="core-telnet">
        <div class="network-tool-heading">
          <div>
            <h4>核心交换机 Telnet</h4>
            <p>用于只读 DHCP 查询；密码单独保存在本机，不随赛事配置导出。</p>
          </div>
          <span class="network-tool-badge">只读连接</span>
        </div>
        <div class="network-tool-grid telnet-settings-grid" id="controlDhcpSettingsForm">
          <label>Telnet 端口
            <input id="controlDhcpPort" type="number" min="1" max="65535" value="23" />
          </label>
          <label>用户名
            <input id="controlDhcpUsername" type="text" autocomplete="off" placeholder="按交换机实际配置填写" />
          </label>
          <label>登录密码
            <input id="controlDhcpPassword" type="password" autocomplete="new-password" placeholder="输入后保存；留空保留原密码" />
          </label>
          <label>Enable 密码
            <input id="controlDhcpEnablePassword" type="password" autocomplete="new-password" placeholder="没有可留空；留空保留原密码" />
          </label>
        </div>
        <div class="network-tool-actions">
          <button type="button" id="controlDhcpSaveTest">保存核心配置并测试</button>
          <span id="controlDhcpSavedState">等待读取配置</span>
        </div>
        <div class="network-tool-result" id="controlDhcpSettingsResult" hidden></div>
      </div>
    `;
  }

  function renderControlConfigForm(configValue) {
    const form = document.getElementById("controlConfigForm");
    if (!form) return;
    const telnetDraft = form.dataset.telnetDirty ? {
      username: (document.getElementById("controlDhcpUsername") || {}).value || "",
      password: (document.getElementById("controlDhcpPassword") || {}).value || "",
      enablePassword: (document.getElementById("controlDhcpEnablePassword") || {}).value || "",
      port: (document.getElementById("controlDhcpPort") || {}).value || "23"
    } : null;
    const matchPages = pages.filter((item) => item.kind);
    lastEditableConfig = controlConfigDefaults(configValue);
    form.innerHTML = `
      <section class="config-section">
        <h3>基础</h3>
        <div class="config-fields">
          ${configInput("event.name", "赛事名称", { placeholder: "可留空" })}
          ${configInput("event.default_layout", "默认赛制", { type: "select", choices: matchPages.map((item) => ({ value: item.id, label: item.label })) })}
        </div>
        ${teamOrderConfigMarkup()}
      </section>
      <section class="config-section">
        <h3>网络 / SNMP</h3>
        <div class="config-fields">
          ${configInput("snmp.community", "SNMP Community")}
          ${configInput("networks.player_vlan", "选手 VLAN", { number: true })}
          ${configInput("networks.wireless_vlan", "无线 VLAN", { number: true })}
          ${configInput("networks.player_subnets", "选手网段", { type: "textarea", compact: true, rows: 1, placeholder: "192.168.40.0/24" })}
          ${configInput("networks.wireless_subnets", "无线网段", { type: "textarea", compact: true, rows: 1, placeholder: "192.168.41.0/24" })}
          ${configInput("networks.player_gateways", "选手网关（可选）", { type: "textarea", compact: true, rows: 1, placeholder: "留空默认用核心交换机 IP" })}
          ${configInput("networks.switch_management_ranges", "普通交换机自动发现范围", { type: "textarea", compact: true, rows: 1, placeholder: "例如 192.168.10.1-100 或 192.168.10.0/24；只进入通用监控和拓扑，不参与赛事座位识别" })}
          ${configInput("networks.firewall_management_ranges", "防火墙管理网段", { type: "textarea", compact: true, rows: 1, placeholder: "默认 192.168.9.0/24；支持范围或单 IP" })}
        </div>
      </section>
      <section class="config-section">
        <h3>核心/防火墙</h3>
        <p class="config-section-note">防火墙 IP 同时用于 Ping 和 WAN 流量 SNMP；HA 物理机填物理防火墙 SNMP IP 后，单机会独立采集并有离线告警。</p>
        <div class="config-fields">
          ${configInput("devices.core.ip", "核心 IP")}
          ${configInput("devices.firewall.ip", "防火墙 IP", { type: "textarea", compact: true, rows: 1, placeholder: "可留空；多台逗号或换行分隔" })}
          ${configInput("devices.firewall.name", "防火墙名称（可选）", { placeholder: "大屏/拓扑显示名；留空用设备 SNMP sysName" })}
          ${configInput("devices.firewall.unit_snmp", "物理防火墙 SNMP IP", { type: "textarea", compact: true, rows: 1, placeholder: "两台物理防火墙，逗号或换行分隔" })}
        </div>
        ${controlDhcpSettingsMarkup()}
      </section>
      <div class="config-section-pair">
        <section class="config-section">
          <h3>赛事交换机（赛事项目必填）</h3>
          <p class="config-section-note">这里只填本项目承载选手电脑的交换机，例如 192.168.10.45、192.168.10.46。它们组成赛事白名单，用于座位识别和选手监控；普通管理网段里的其它交换机不会混入赛事控制台。</p>
          ${configListRows("stage_switches", lastEditableConfig.devices.stage_switches, [
            { key: "name", label: "名称", placeholder: "可留空，默认用 SNMP hostname" },
            { key: "ip", label: "管理地址", placeholder: "赛事交换机 IP" }
          ])}
        </section>
        <section class="config-section">
          <h3>固定普通交换机（选填）</h3>
          <p class="config-section-note">一般留空，系统会从“普通交换机自动发现范围”识别其它接入交换机。这里只在需要固定显示名称时填写；这些设备只进入通用大屏、拓扑和 LibreNMS，不参与选手座位识别。</p>
          ${configListRows("access_switches", lastEditableConfig.devices.access_switches, [
            { key: "name", label: "名称", placeholder: "可留空，默认用 SNMP hostname" },
            { key: "ip", label: "管理地址", placeholder: "可留空" }
          ])}
        </section>
      </div>
      <section class="config-section">
        <h3>服务器</h3>
        ${configListRows("servers", lastEditableConfig.devices.servers, [
          { key: "name", label: "名称", placeholder: "可留空" },
          { key: "ip", label: "地址", placeholder: "可留空" }
        ])}
      </section>
      <section class="config-section">
        <h3>ISP</h3>
        <p class="config-section-note">自动发现会从防火墙 SNMP 识别 WAN 接口，并从路由表发现网关。每条线路只需填写与防火墙一致的 WAN 口名/别名和带宽；不再要求公网 IP 或网关地址。对称线路填一个 Mbps 数值；不对称线路固定按“下载/上传”填写，例如 1000/100。</p>
        <div class="config-fields">
          ${configInput("isp.auto_discovery", "自动发现 ISP", { type: "checkbox", compactCheck: true })}
          ${configInput("isp.max_bandwidth_mbps", "默认带宽（下载/上传 Mbps）", { placeholder: "例如 1000 或 1000/100；留空默认 1000" })}
          ${configInput("isp.wan_if_filter", "WAN 口识别关键词", { placeholder: "telecom,telcom,unicom,isp,WAN" })}
        </div>
        ${configListRows("isp", lastEditableConfig.isp.links, [
          { key: "name", label: "WAN 口名/别名", placeholder: "例如 telecom、eth1 或电信" },
          { key: "bandwidth_mbps", label: "单线带宽（下载/上传 Mbps）", placeholder: "例如 1000 或 1000/100" }
        ])}
      </section>
      <section class="config-section">
        <h3>UniFi</h3>
        <div class="config-fields">
          ${configInput("unifi.enabled", "启用 UniFi", { type: "checkbox" })}
          ${configInput("unifi.controller_url", "UniFi 地址", { placeholder: "https://控制器IP" })}
          ${configInput("unifi.user", "UniFi 用户")}
          ${configInput("unifi.password", "UniFi 密码", { inputType: "password", placeholder: "留空则保留 .env 现有值" })}
          ${configInput("unifi.sites", "UniFi Sites", { placeholder: "all" })}
          ${configInput("unifi.verify_ssl", "校验 UniFi 证书", { type: "checkbox" })}
        </div>
      </section>
      <section class="config-section">
        <h3>告警</h3>
        <p class="config-section-note">所有监控可共用同一个飞书应用，但每台物理监控填写各自的“赛事名称”和“告警及巡检群名称”。现有长连接权限可继续使用；多套物理监控共用应用并要求严格按群处理时，再增加 im:chat、im:message:readonly 和 im:message.group_msg。</p>
        <div class="config-fields">
          ${configInput("alerts.feishu_robot_token", "飞书机器人 Token")}
          ${configInput("alerts.feishu_app_id", "飞书应用 App ID", { placeholder: "cli_ 开头" })}
          ${configInput("alerts.feishu_app_secret", "飞书应用 App Secret", { inputType: "password" })}
          ${configInput("alerts.feishu_chat_id", "告警及巡检群名称或 Chat ID", { placeholder: "唯一群名，或 oc_ 开头的 Chat ID" })}
          ${configInput("alerts.gateway_macs", "关键网关 MAC（逗号分隔）", { placeholder: "例如：0000.5e00.0101,0000.5e00.0201" })}
          ${configInput("alerts.gateway_uplink_ports", "网关正常上联接口（逗号分隔）", { placeholder: "例如：Po1,Po10" })}
          ${configInput("alerts.mac_flap_window_seconds", "MAC 漂移统计窗口（秒）", { number: true })}
          ${configInput("alerts.mac_flap_threshold", "普通 MAC 告警次数", { number: true })}
          ${configInput("alerts.cpu_alert_percent", "交换机 CPU 告警阈值（%）", { number: true })}
          ${configInput("alerts.memory_alert_percent", "交换机内存告警阈值（%）", { number: true })}
        </div>
        <p class="config-section-note">关键网关 MAC 在正常上联与其他接口间移动时立即告警；普通 MAC 默认 60 秒内出现 3 次才告警。Cisco 日志不提供可靠的原/新方向，因此未配置正常上联时只显示两个涉及接口。CPU 达到阈值持续 5 分钟才告警，内存持续 10 分钟才告警；分别低于阈值 10% 并稳定 2 分钟后恢复。默认 70% / 80%，40% 不会告警。</p>
      </section>
      <section class="config-section">
        <h3>安全</h3>
        <div class="config-fields">
          ${configInput("security.grafana_anonymous", "Grafana 匿名访问", { type: "checkbox" })}
        </div>
      </section>
    `;
    if (telnetDraft) {
      document.getElementById("controlDhcpUsername").value = telnetDraft.username;
      document.getElementById("controlDhcpPassword").value = telnetDraft.password;
      document.getElementById("controlDhcpEnablePassword").value = telnetDraft.enablePassword;
      document.getElementById("controlDhcpPort").value = telnetDraft.port;
    }
    if (lastDhcpSettings) renderControlDhcpSettings(lastDhcpSettings);
    if (window.location.hash === "#core-telnet") {
      window.requestAnimationFrame(() => {
        const target = document.getElementById("core-telnet");
        if (target) target.scrollIntoView({ block: "center" });
      });
    }
  }

  function collectControlConfigForm() {
    const form = document.getElementById("controlConfigForm");
    const value = controlConfigDefaults(lastEditableConfig);
    if (!form) return value;
    form.querySelectorAll("[data-config-path]").forEach((input) => {
      let nextValue;
      if (input.type === "checkbox") {
        nextValue = input.checked;
      } else if (input.tagName === "TEXTAREA") {
        nextValue = splitConfigList(input.value);
      } else if (input.dataset.configNumber) {
        nextValue = input.value === "" ? "" : Number(input.value);
      } else {
        nextValue = input.value.trim();
      }
      configPathSet(value, input.dataset.configPath, nextValue);
    });
    const listMappings = {
      stage_switches: ["devices", "stage_switches"],
      access_switches: ["devices", "access_switches"],
      servers: ["devices", "servers"],
      isp: ["isp", "links"]
    };
    Object.entries(listMappings).forEach(([name, path]) => {
      const list = form.querySelector(`[data-config-list="${name}"]`);
      const rows = [];
      if (list) {
        list.querySelectorAll(".config-list-row").forEach((row) => {
          const item = {};
          row.querySelectorAll("[data-config-key]").forEach((input) => {
            item[input.dataset.configKey] = input.dataset.configNumber
              ? (input.value === "" ? "" : Number(input.value))
              : input.value.trim();
          });
          if (Object.values(item).some((entry) => String(entry || "").trim())) rows.push(item);
        });
      }
      value[path[0]][path[1]] = rows;
    });
    const teamOrderEditor = form.querySelector("[data-team-order-layout]");
    if (teamOrderEditor) {
      const layoutId = teamOrderEditor.dataset.teamOrderLayout;
      const order = [...teamOrderEditor.querySelectorAll("[data-team-order-slot]")]
        .sort((left, right) => Number(left.dataset.teamOrderSlot) - Number(right.dataset.teamOrderSlot))
        .map((select) => Number(select.value));
      if (!value.event.team_orders || typeof value.event.team_orders !== "object" || Array.isArray(value.event.team_orders)) {
        value.event.team_orders = {};
      }
      value.event.team_orders[layoutId] = order;
    }
    if (value.devices) {
      value.devices.switches = [];
    }
    lastEditableConfig = value;
    return value;
  }

  function renderConfigEditor(platformConfig) {
    const form = document.getElementById("controlConfigForm");
    if (!form) return;
    if (platformConfig && platformConfig.ok && !form.dataset.dirty && !form.dataset.telnetDirty) {
      renderControlConfigForm(platformConfig.config || {});
    }
    const schemaBlocked = Boolean(platformConfig && platformConfig.configTooNew);
    ["controlConfigSave", "controlConfigApply", "controlConfigRollback", "controlConfigImport"].forEach((id) => {
      const button = document.getElementById(id);
      if (button) {
        button.disabled = schemaBlocked;
        button.title = schemaBlocked ? "配置版本高于当前软件，请先升级平台" : "";
      }
    });
    // Once the operator has run 验证/保存/应用配置, keep that result on screen --
    // don't let the periodic refresh overwrite it (that made the apply error
    // vanish into "验证通过" after a few seconds).
    if (configResultSticky) return;
    if (platformConfig && !platformConfig.ok) {
      renderConfigResult(platformConfig);
    } else if (platformConfig && platformConfig.ok) {
      renderConfigResult({ ok: true, passive: true, issues: platformConfig.issues || [] });
    }
  }

  const CONFIG_ACTION_LABELS = { validate: "验证", save: "保存", apply: "应用配置", rollback: "回滚" };

  function setConfigButtonsBusy(busy) {
    const schemaBlocked = Boolean(lastPlatformConfig && lastPlatformConfig.configTooNew);
    ["controlConfigValidate", "controlConfigSave", "controlConfigApply", "controlConfigRollback", "controlConfigImport"].forEach((id) => {
      const btn = document.getElementById(id);
      if (btn) btn.disabled = busy || (schemaBlocked && id !== "controlConfigValidate");
    });
  }

  function configOperationId(action) {
    const random = Math.random().toString(36).slice(2, 10);
    return `web-${action}-${Date.now()}-${random}`;
  }

  async function runConfigAction(action) {
    const form = document.getElementById("controlConfigForm");
    const label = CONFIG_ACTION_LABELS[action] || "处理";
    const configPayload = collectControlConfigForm();
    const payload = { text: JSON.stringify(configPayload, null, 2), actor: "web", note: action };
    const operationId = (action === "apply" || action === "rollback") ? configOperationId(action) : "";
    if (operationId) payload.operationId = operationId;
    configResultSticky = true;
    if (action === "apply") applyInProgress = true;
    setConfigButtonsBusy(true);
    renderConfigResult({
      pending: true,
      pendingLabel: action === "apply"
        ? "应用配置，重启服务中（页面可能短暂断开约 10-20 秒，请勿刷新或关闭）"
        : label
    });
    try {
      let result;
      if (action === "validate") {
        result = await postPlatform("/config/validate", payload);
      } else if (action === "save") {
        result = await postPlatform("/config/save", payload);
      } else if (action === "apply") {
        result = await postPlatform("/config/apply", payload, { timeoutMs: APPLY_REQUEST_TIMEOUT_MS });
      } else if (action === "rollback") {
        result = await postPlatform("/config/rollback", { actor: "web", note: "rollback from control", operationId }, { timeoutMs: APPLY_REQUEST_TIMEOUT_MS });
      }
      result.action = action;
      lastPlatformConfig = result;
      const shouldReloadSavedConfig = result && result.ok && action !== "validate";
      if (shouldReloadSavedConfig && result.config && form) {
        delete form.dataset.dirty;
        renderControlConfigForm(result.config);
      } else if (form) {
        form.dataset.dirty = "1";
      }
      renderConfigResult(result);
      if (shouldReloadSavedConfig) {
        applyInProgress = false;
        refreshControlPanel();
      }
    } catch (error) {
      if (action === "apply" || action === "rollback") {
        renderConfigResult({ pending: true, pendingLabel: "服务重启中，正在核对任务结果" });
        const recovered = await waitForApplyRecovery(operationId, {
          fetchConfig: fetchPlatformConfig,
          fetchStatus: fetchApplyStatus
        });
        const recoveryPayload = applyRecoveryRenderPayload(recovered, action);
        if (["succeeded", "pending", "failed"].includes(recovered.outcome)) {
          const recoveredConfig = recovered.config;
          if (recoveredConfig && recoveredConfig.ok) {
            lastPlatformConfig = recoveredConfig;
          }
          if (form && recoveredConfig && recoveredConfig.ok) {
            delete form.dataset.dirty;
            if (recoveredConfig.config) renderControlConfigForm(recoveredConfig.config);
          }
          renderConfigResult(recoveryPayload);
          applyInProgress = false;
          refreshControlPanel();
        } else {
          // A durable task that still says running is not an apply failure.
          // Unknown is reserved for a task record that cannot be recovered.
          renderConfigResult(recoveryPayload);
        }
      } else {
        renderConfigResult({ ok: false, errorTitle: `${label}失败`, error: error.message || "配置操作失败" });
      }
    } finally {
      applyInProgress = false;
      setConfigButtonsBusy(false);
    }
  }

  function importControlConfigFile() {
    const input = document.getElementById("controlConfigImportFile");
    if (input) input.click();
  }

  function bindConfigImportFile() {
    const fileInput = document.getElementById("controlConfigImportFile");
    const form = document.getElementById("controlConfigForm");
    if (!fileInput || !form || fileInput.dataset.bound) return;
    fileInput.addEventListener("change", async () => {
      const file = fileInput.files && fileInput.files[0];
      if (!file) return;
      const text = await file.text();
      fileInput.value = "";
      // Reject compressed archives before treating their binary content as YAML.
      if (/\.zip$/i.test(file.name) || text.slice(0, 2) === "PK") {
        renderConfigResult({
          ok: false,
          errorTitle: "不支持导入压缩包",
          error: "请选择 event-config.yml 配置文件。"
        });
        configResultSticky = true;
        return;
      }
      try {
        const result = await postPlatform("/config/validate", { text, actor: "web", note: "import" });
        lastPlatformConfig = result;
        if (result && result.ok && !result.configTooNew && result.config) {
          renderControlConfigForm(result.config);
          form.dataset.dirty = "1";
        }
        renderConfigResult(result);
      } catch (error) {
        renderConfigResult({ ok: false, error: error.message || "导入失败" });
      }
    });
    fileInput.dataset.bound = "1";
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
    renderConfigEditor(snapshot.platformConfig);
    renderControlDhcpSettings(snapshot.dhcpSettings);
    renderControlIncidentFlow(snapshot);
    renderIncidentList(snapshot.incidents);
    renderDelivery();
    lastControlReport = snapshot;
    lastPlatformConfig = snapshot.platformConfig;
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
    if (applyInProgress) return;
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
    const configForm = document.getElementById("controlConfigForm");
    if (configForm && !configForm.dataset.bound) {
      const markDirty = (event) => {
        if (event.target.closest("#controlDhcpSettingsForm")) configForm.dataset.telnetDirty = "1";
        else configForm.dataset.dirty = "1";
      };
      configForm.addEventListener("input", markDirty);
      configForm.addEventListener("change", (event) => {
        markDirty(event);
        const layoutSelect = event.target.closest('[data-config-path="event.default_layout"]');
        if (layoutSelect) {
          const next = collectControlConfigForm();
          renderControlConfigForm(next);
          configForm.dataset.dirty = "1";
          return;
        }
        const teamSelect = event.target.closest("[data-team-order-slot]");
        if (!teamSelect) return;
        const previous = String(teamSelect.dataset.teamOrderPrevious || "");
        const selected = String(teamSelect.value || "");
        const duplicate = [...configForm.querySelectorAll("[data-team-order-slot]")]
          .find((input) => input !== teamSelect && String(input.value) === selected);
        if (duplicate) {
          duplicate.value = previous;
          duplicate.dataset.teamOrderPrevious = previous;
        }
        teamSelect.dataset.teamOrderPrevious = selected;
        collectControlConfigForm();
      });
      // Browsers increment focused number inputs when the mouse wheel moves.
      // Remove focus before the native wheel action so scrolling the long
      // configuration form cannot silently change VLAN or bandwidth values.
      configForm.addEventListener("wheel", (event) => {
        const input = event.target instanceof HTMLInputElement ? event.target : null;
        if (input && input.type === "number" && document.activeElement === input) input.blur();
      }, { passive: true });
      configForm.addEventListener("click", (event) => {
        const dhcpSaveButton = event.target.closest("#controlDhcpSaveTest");
        if (dhcpSaveButton) {
          saveAndTestControlDhcpSettings(event);
          return;
        }
        const teamOrderReset = event.target.closest("[data-team-order-reset]");
        if (teamOrderReset) {
          const next = collectControlConfigForm();
          const layoutId = teamOrderReset.dataset.teamOrderReset;
          const page = pages.find((item) => item.id === layoutId);
          if (page && typeof teamLayouts.defaultTeamOrder === "function") {
            next.event.team_orders[layoutId] = teamLayouts.defaultTeamOrder(page);
            renderControlConfigForm(next);
            configForm.dataset.dirty = "1";
          }
          return;
        }
        const addButton = event.target.closest("[data-config-add]");
        const rangeButton = event.target.closest("[data-config-add-range]");
        const removeButton = event.target.closest("[data-config-remove]");
        if (!addButton && !rangeButton && !removeButton) return;
        const next = collectControlConfigForm();
        if (addButton) {
          const listName = addButton.dataset.configAdd;
          if (listName === "stage_switches") next.devices.stage_switches.push({ ip: "" });
          if (listName === "access_switches") next.devices.access_switches.push({ ip: "" });
          if (listName === "servers") next.devices.servers.push({ name: "", ip: "" });
          if (listName === "isp") next.isp.links.push({ name: "", ping: "", ip: "", bandwidth_mbps: "" });
        }
        if (rangeButton) {
          const listName = rangeButton.dataset.configAddRange;
          const input = configForm.querySelector(`[data-config-range-input="${listName}"]`);
          const values = expandIpRangeText(input ? input.value : "");
          const target = listName === "stage_switches" ? next.devices.stage_switches : next.devices.access_switches;
          const known = new Set(target.map((item) => String(item.ip || "").trim()).filter(Boolean));
          values.forEach((ip) => {
            if (!known.has(ip)) {
              target.push({ ip });
              known.add(ip);
            }
          });
        }
        if (removeButton) {
          const listName = removeButton.dataset.configRemove;
          const index = Number(removeButton.dataset.index);
          if (listName === "stage_switches") next.devices.stage_switches.splice(index, 1);
          if (listName === "access_switches") next.devices.access_switches.splice(index, 1);
          if (listName === "servers") next.devices.servers.splice(index, 1);
          if (listName === "isp") next.isp.links.splice(index, 1);
        }
        renderControlConfigForm(next);
        configForm.dataset.dirty = "1";
      });
      configForm.dataset.bound = "1";
    }
    [
      ["controlConfigValidate", "validate"],
      ["controlConfigSave", "save"],
      ["controlConfigApply", "apply"],
      ["controlConfigRollback", "rollback"]
    ].forEach(([id, action]) => {
      const button = document.getElementById(id);
      if (button && !button.dataset.bound) {
        button.addEventListener("click", () => runConfigAction(action));
        button.dataset.bound = "1";
      }
    });
    const importBtn = document.getElementById("controlConfigImport");
    if (importBtn && !importBtn.dataset.bound) {
      importBtn.addEventListener("click", importControlConfigFile);
      importBtn.dataset.bound = "1";
    }
    bindConfigImportFile();
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

  function stopDhcpRefresh() {
    if (dhcpTimer) {
      window.clearTimeout(dhcpTimer);
      dhcpTimer = null;
    }
    dhcpSeq += 1;
    dhcpRefreshing = false;
  }

  function dhcpSummaryCard(label, value, note, level = "") {
    return `
      <div class="dhcp-summary-card ${level}">
        <span>${escapeHtml(label)}</span>
        <strong>${escapeHtml(value)}</strong>
        <small>${escapeHtml(note || "")}</small>
      </div>
    `;
  }

  function dhcpRangeAddresses(rangeText, limit = 4096) {
    const match = String(rangeText || "").match(/^\s*(\d{1,3}(?:\.\d{1,3}){3})\s*-\s*(\d{1,3}(?:\.\d{1,3}){3})\s*$/);
    if (!match) return [];
    const toNumber = (value) => {
      const parts = value.split(".").map(Number);
      if (parts.length !== 4 || parts.some((part) => !Number.isInteger(part) || part < 0 || part > 255)) return null;
      return (((parts[0] * 256 + parts[1]) * 256 + parts[2]) * 256) + parts[3];
    };
    const toAddress = (value) => [24, 16, 8, 0].map((shift) => Math.floor(value / (2 ** shift)) % 256).join(".");
    const start = toNumber(match[1]);
    const end = toNumber(match[2]);
    if (start == null || end == null || end < start || end - start + 1 > limit) return [];
    return Array.from({ length: end - start + 1 }, (_item, index) => toAddress(start + index));
  }

  function compactDhcpAddresses(values) {
    const toNumber = (value) => {
      const parts = String(value || "").split(".").map(Number);
      if (parts.length !== 4 || parts.some((part) => !Number.isInteger(part) || part < 0 || part > 255)) return null;
      return (((parts[0] * 256 + parts[1]) * 256 + parts[2]) * 256) + parts[3];
    };
    const entries = [...new Set(values || [])]
      .map((ip) => ({ ip, number: toNumber(ip) }))
      .filter((item) => item.number != null)
      .sort((left, right) => left.number - right.number);
    const ranges = [];
    for (const entry of entries) {
      const current = ranges[ranges.length - 1];
      if (current && entry.number === current.endNumber + 1) {
        current.end = entry.ip;
        current.endNumber = entry.number;
      } else {
        ranges.push({ start: entry.ip, end: entry.ip, endNumber: entry.number });
      }
    }
    return ranges.map((range) => range.start === range.end ? range.start : `${range.start}–${range.end}`).join("、");
  }

  function dhcpPoolKey(pool) {
    return `${encodeURIComponent(String(pool.name || ""))}|${encodeURIComponent(String(pool.range || ""))}`;
  }

  function dhcpIpv4Number(value) {
    const parts = String(value || "").trim().split(".").map(Number);
    if (parts.length !== 4 || parts.some((part) => !Number.isInteger(part) || part < 0 || part > 255)) return null;
    return (((parts[0] * 256 + parts[1]) * 256 + parts[2]) * 256) + parts[3];
  }

  function dhcpPoolMatchesSearch(pool, query) {
    const needle = String(query || "").trim().toLowerCase();
    if (!needle) return true;
    const searchable = `${pool.name || ""} ${pool.range || ""}`.toLowerCase();
    if (searchable.includes(needle)) return true;
    const address = dhcpIpv4Number(needle);
    const rangeMatch = String(pool.range || "").match(/^\s*(\d{1,3}(?:\.\d{1,3}){3})\s*-\s*(\d{1,3}(?:\.\d{1,3}){3})\s*$/);
    if (address == null || !rangeMatch) return false;
    const start = dhcpIpv4Number(rangeMatch[1]);
    const end = dhcpIpv4Number(rangeMatch[2]);
    return start != null && end != null && address >= start && address <= end;
  }

  function dhcpPoolMatchesFilter(pool, conflicts) {
    if (dhcpPoolFilterValue === "active") return Number(pool.leased || 0) > 0;
    if (dhcpPoolFilterValue === "excluded") return Number(pool.excluded || 0) > 0;
    if (dhcpPoolFilterValue === "attention") {
      const poolAddresses = new Set(dhcpRangeAddresses(pool.range));
      return ["warn", "bad"].includes(String(pool.level || ""))
        || (conflicts || []).some((ip) => poolAddresses.has(ip));
    }
    return true;
  }

  function dhcpPoolSortValue(pool) {
    const match = String(pool.range || "").match(/^\s*(\d{1,3}(?:\.\d{1,3}){3})/);
    return match ? dhcpIpv4Number(match[1]) : null;
  }

  function dhcpPoolCard(pool, conflicts) {
    const pct = Math.max(0, Math.min(100, Number(pool.utilization || 0)));
    const addressBlockCount = groupAddressesByCBlock(dhcpRangeAddresses(pool.range)).length;
    return `
      <article class="dhcp-pool-card ${escapeHtml(pool.level || "good")}${addressBlockCount > 1 ? " multi-block" : ""}">
        <header>
          <div><strong>${escapeHtml(pool.name || "未命名地址池")}</strong><span>${escapeHtml(pool.range || "交换机未返回地址范围")}</span></div>
          <b>${pct.toFixed(1)}%</b>
        </header>
        <div class="dhcp-pool-bar"><i style="width:${pct}%"></i></div>
        <dl>
          <div><dt>总地址</dt><dd>${Number(pool.total || 0)}</dd></div>
          <div><dt>已租用</dt><dd>${Number(pool.leased || 0)}</dd></div>
          <div><dt>剩余</dt><dd>${Number(pool.available || 0)}</dd></div>
          <div><dt>排除</dt><dd>${Number(pool.excluded || 0)}</dd></div>
        </dl>
        ${dhcpAddressMap(pool, conflicts, dhcpBindingPayload)}
      </article>
    `;
  }

  function renderDhcpPoolBrowser(pools, conflicts) {
    const poolsElement = document.getElementById("dhcpPools");
    if (!poolsElement) return;
    const previousDirectory = poolsElement.querySelector(".dhcp-pool-directory");
    const previousDetail = poolsElement.querySelector(".dhcp-pool-detail");
    const directoryScrollTop = previousDirectory ? previousDirectory.scrollTop : 0;
    const detailScrollTop = previousDetail ? previousDetail.scrollTop : 0;
    const previousSelectedPoolKey = dhcpSelectedPoolKey;
    const sortedPools = [...pools].sort((left, right) => {
      const leftValue = dhcpPoolSortValue(left);
      const rightValue = dhcpPoolSortValue(right);
      if (leftValue != null && rightValue != null && leftValue !== rightValue) return leftValue - rightValue;
      if (leftValue != null && rightValue == null) return -1;
      if (leftValue == null && rightValue != null) return 1;
      return String(left.name || "").localeCompare(String(right.name || ""), "zh-CN", { numeric: true });
    });
    const visiblePools = sortedPools.filter((pool) =>
      dhcpPoolMatchesSearch(pool, dhcpPoolSearchText) && dhcpPoolMatchesFilter(pool, conflicts)
    );
    if (!visiblePools.some((pool) => dhcpPoolKey(pool) === dhcpSelectedPoolKey)) {
      dhcpSelectedPoolKey = visiblePools.length ? dhcpPoolKey(visiblePools[0]) : "";
    }
    const selectedPool = visiblePools.find((pool) => dhcpPoolKey(pool) === dhcpSelectedPoolKey);
    setText("dhcpPoolCount", `显示 ${visiblePools.length} / ${pools.length} 个网段`);
    if (!visiblePools.length) {
      poolsElement.innerHTML = `<div class="dhcp-empty">没有符合当前搜索或筛选条件的网段。</div>`;
      return;
    }
    poolsElement.innerHTML = `
      <aside class="dhcp-pool-directory" aria-label="DHCP 网段目录">
        ${visiblePools.map((pool) => {
          const key = dhcpPoolKey(pool);
          const pct = Math.max(0, Math.min(100, Number(pool.utilization || 0)));
          return `
            <button type="button" class="dhcp-pool-option ${escapeHtml(pool.level || "good")}${key === dhcpSelectedPoolKey ? " selected" : ""}" data-dhcp-pool="${escapeHtml(key)}">
              <span><strong>${escapeHtml(pool.name || "未命名地址池")}</strong><small>${escapeHtml(pool.range || "无地址范围")}</small></span>
              <span><b>${Number(pool.leased || 0)} 已用</b><small>${pct.toFixed(1)}%</small></span>
            </button>
          `;
        }).join("")}
      </aside>
      <div class="dhcp-pool-detail">
        ${selectedPool ? dhcpPoolCard(selectedPool, conflicts) : ""}
      </div>
    `;
    const selectionChanged = previousSelectedPoolKey !== dhcpSelectedPoolKey;
    const nextDirectory = poolsElement.querySelector(".dhcp-pool-directory");
    const nextDetail = poolsElement.querySelector(".dhcp-pool-detail");
    if (nextDirectory) nextDirectory.scrollTop = selectionChanged ? 0 : directoryScrollTop;
    if (nextDetail) nextDetail.scrollTop = selectionChanged ? 0 : detailScrollTop;
  }

  function dhcpAddressMap(pool, conflicts, bindingPayload) {
    const addresses = dhcpRangeAddresses(pool.range);
    if (!addresses.length) return '<div class="dhcp-address-note">交换机未返回可展开的地址范围。</div>';
    const addressBlocks = groupAddressesByCBlock(addresses);
    const excluded = new Set(pool.excludedAddresses || []);
    const conflictSet = new Set(conflicts || []);
    const bindingDetails = new Map((bindingPayload && bindingPayload.bindings || [])
      .map((item) => [String(item.ip || ""), String(item.detail || "")]));
    const arpDetails = new Map((bindingPayload && bindingPayload.arpEntries || [])
      .map((item) => [String(item.ip || ""), String(item.detail || "")]));
    const used = new Set(bindingPayload && bindingPayload.usedAddresses || []);
    const observed = new Set(bindingPayload && bindingPayload.observedAddresses || []);
    const reservedUsed = new Set([...excluded].filter((ip) => used.has(ip) || observed.has(ip)));
    const excludedList = [...excluded];
    const exclusionNote = excludedList.length
      ? `排除地址：${compactDhcpAddresses(excludedList)}`
      : (Number(pool.excluded || 0) ? "交换机返回了排除数量，但未返回具体排除配置" : "没有排除地址");
    return `
      <section class="dhcp-address-map" aria-label="${escapeHtml(pool.name || "地址池")} IP 地址格">
        <div class="dhcp-address-map-head">
          <div class="dhcp-address-legend">
            <span><i class="pool"></i>池内地址</span>
            <span><i class="used"></i>已用</span>
            <span><i class="excluded"></i>排除（未发现）</span>
            <span><i class="reserved-used"></i>排除且已发现</span>
            <span><i class="conflict"></i>冲突</span>
          </div>
          <span class="dhcp-exclusion-list">${escapeHtml(exclusionNote)}</span>
        </div>
        <div class="dhcp-address-blocks">
          ${addressBlocks.map((block) => `
            <div class="dhcp-address-block">
              <strong>${escapeHtml(`${block.prefix}.0/24`)}</strong>
              <div class="dhcp-address-grid">
                ${block.addresses.map((ip) => {
                  const status = conflictSet.has(ip) ? "conflict"
                    : reservedUsed.has(ip) ? "reserved-used"
                    : excluded.has(ip) ? "excluded"
                    : used.has(ip) ? "used"
                    : "pool";
                  const label = ip.slice(ip.lastIndexOf("."));
                  const statusText = status === "conflict" ? "冲突"
                    : status === "reserved-used"
                      ? `排除地址已发现${arpDetails.get(ip) ? ` · ${arpDetails.get(ip)}` : bindingDetails.get(ip) ? ` · ${bindingDetails.get(ip)}` : ""}`
                    : status === "excluded" ? "排除地址（当前租约和 ARP 表均未发现）" : status === "used"
                    ? `已租用${bindingDetails.get(ip) ? ` · ${bindingDetails.get(ip)}` : ""}`
                    : (bindingPayload ? "未在当前租约表中" : "池内（点击“查询已用 IP”后标色）");
                  return `<span class="dhcp-address-cell ${status}" title="${escapeHtml(`${ip} · ${statusText}`)}" aria-label="${escapeHtml(`${ip} ${statusText}`)}">${escapeHtml(label)}</span>`;
                }).join("")}
              </div>
            </div>
          `).join("")}
        </div>
      </section>
    `;
  }

  function renderDhcpDashboard(payload) {
    dhcpLastPayload = payload;
    const summary = payload.summary || {};
    const pools = payload.pools || [];
    const conflicts = payload.conflicts || [];
    const captured = payload.capturedAt
      ? new Date(payload.capturedAt * 1000).toLocaleTimeString("zh-CN", { hour12: false })
      : "—";
    const refreshSeconds = Number(payload.refreshSeconds || 60);
    setText("dhcpConnection", `${payload.host || "—"} · 读取自基础配置 · ${refreshSeconds} 秒刷新`);

    const status = document.getElementById("dhcpStatus");
    if (status) {
      status.className = "dhcp-status good";
      status.textContent = payload.refreshing
        ? `正在刷新，当前显示上次结果 · 采集于 ${captured}`
        : `${payload.cached ? `使用 ${Number(payload.cacheAgeSeconds || 0).toFixed(0)} 秒内缓存` : `已从核心交换机刷新（${Number(payload.collectionSeconds || 0).toFixed(2)} 秒）`} · 采集于 ${captured}`;
    }

    const utilization = Number(summary.utilization || 0);
    const utilizationLevel = utilization >= 90 ? "bad" : utilization >= 80 ? "warn" : "good";
    const summaryElement = document.getElementById("dhcpSummary");
    if (summaryElement) {
      summaryElement.innerHTML = [
        dhcpSummaryCard("地址池", String(summary.poolCount || 0), "核心交换机"),
        dhcpSummaryCard("可分配地址", String(summary.total || 0), `排除 ${summary.excluded || 0}`),
        dhcpSummaryCard("已租用", String(summary.leased || 0), "当前活动地址"),
        dhcpSummaryCard("剩余", String(summary.available || 0), "仍可分配"),
        dhcpSummaryCard("总体使用率", `${utilization.toFixed(1)}%`, "80% 提醒 / 90% 告警", utilizationLevel),
        dhcpSummaryCard("冲突地址", String(summary.conflictCount || 0), conflicts.slice(0, 3).join("、") || "未发现冲突", conflicts.length ? "bad" : "good")
      ].join("");
    }

    if (pools.length) renderDhcpPoolBrowser(pools, conflicts);
    else {
      const poolsElement = document.getElementById("dhcpPools");
      if (poolsElement) poolsElement.innerHTML = `<div class="dhcp-empty">核心交换机当前没有返回 DHCP 地址池。</div>`;
      setText("dhcpPoolCount", "0 个网段");
    }

    const warningText = (payload.warnings || []).join("；");
    setText(
      "dhcpFootnote",
      `${warningText ? `${warningText} · ` : ""}地址池数量自动刷新；进入页面时读取租约与 ARP 表。排除地址未被发现不代表设备一定离线（设备可能不响应或 ARP 已老化）。`
    );
    if (!dhcpBindingPayload && !dhcpBindingsRefreshing) {
      window.setTimeout(refreshDhcpBindings, 0);
    }
  }

  async function refreshDhcpBindings() {
    if (activePageId !== "dhcp" || dhcpBindingsRefreshing) return;
    const button = document.getElementById("dhcpBindings");
    const status = document.getElementById("dhcpBindingsStatus");
    dhcpBindingsRefreshing = true;
    if (button) button.disabled = true;
    if (status) status.textContent = "正在读取租约与 ARP 表…";
    try {
      const payload = await fetchDhcpBindings();
      if (activePageId !== "dhcp") return;
      dhcpBindingPayload = payload;
      if (status) {
        const captured = payload.capturedAt
          ? new Date(payload.capturedAt * 1000).toLocaleTimeString("zh-CN", { hour12: false })
          : "刚刚";
        const returned = Number((payload.usedAddresses || []).length);
        const exclusions = new Set((dhcpLastPayload && dhcpLastPayload.pools || [])
          .flatMap((pool) => pool.excludedAddresses || []));
        const discovered = new Set([
          ...(payload.usedAddresses || []),
          ...(payload.observedAddresses || [])
        ]);
        const reservedUsed = [...exclusions].filter((ip) => discovered.has(ip)).length;
        const expected = (dhcpLastPayload && dhcpLastPayload.pools || [])
          .reduce((sum, pool) => sum + Number(pool.leased || 0), 0);
        const statusText = returned === 0 && expected > 0
          ? `交换机统计已租用 ${expected} 个，但租约明细未解析；${payload.parserWarning || "请重试或检查命令输出"}`
          : `DHCP 租约（绿色）${returned} 个 · 排除且已发现（蓝色）${reservedUsed} 个 · ${captured}`;
        status.textContent = payload.arpWarning ? `${statusText} · ${payload.arpWarning}` : statusText;
      }
      if (dhcpLastPayload) renderDhcpDashboard(dhcpLastPayload);
    } catch (error) {
      if (status) status.textContent = `已用 IP 查询失败：${error.message || "未知错误"}`;
    } finally {
      dhcpBindingsRefreshing = false;
      if (button) button.disabled = false;
    }
  }

  function scheduleDhcpRefresh(seconds = 60) {
    if (dhcpTimer) window.clearTimeout(dhcpTimer);
    if (activePageId !== "dhcp" || document.visibilityState === "hidden") {
      dhcpTimer = null;
      return;
    }
    dhcpTimer = window.setTimeout(() => refreshDhcpDashboard(false), Math.max(30, Number(seconds || 60)) * 1000);
  }

  async function refreshDhcpDashboard(force = false) {
    if (activePageId !== "dhcp" || document.visibilityState === "hidden" || dhcpRefreshing) return;
    const seq = ++dhcpSeq;
    dhcpRefreshing = true;
    const refreshButton = document.getElementById("dhcpRefresh");
    if (refreshButton) refreshButton.disabled = true;
    const status = document.getElementById("dhcpStatus");
    if (status) {
      status.className = "dhcp-status loading";
      status.textContent = dhcpHasData ? "正在从核心交换机刷新…" : "正在连接核心交换机并读取 DHCP…";
    }
    let nextSeconds = 60;
    try {
      const payload = await fetchDhcpDashboard(force);
      if (seq !== dhcpSeq || activePageId !== "dhcp") return;
      dhcpHasData = true;
      nextSeconds = Number(payload.refreshSeconds || 60);
      renderDhcpDashboard(payload);
      lastDataSuccessAt = Date.now();
    } catch (error) {
      if (seq !== dhcpSeq || activePageId !== "dhcp") return;
      if (status) {
        status.className = "dhcp-status bad";
        status.textContent = `读取失败：${error.message || "未知错误"}`;
      }
      if (!dhcpHasData) {
        const poolsElement = document.getElementById("dhcpPools");
        const summaryElement = document.getElementById("dhcpSummary");
        if (summaryElement) summaryElement.innerHTML = "";
        if (poolsElement) poolsElement.innerHTML = `
          <div class="dhcp-empty bad">
            <span>请检查核心 IP、Telnet 登录信息和交换机连通性。</span>
            <a class="dhcp-config-link" href="/control#core-telnet">去赛事控制台配置</a>
          </div>
        `;
      }
    } finally {
      if (seq === dhcpSeq) {
        dhcpRefreshing = false;
        if (refreshButton) refreshButton.disabled = false;
        scheduleDhcpRefresh(nextSeconds);
      }
    }
  }

  function startDhcpRefresh() {
    stopDhcpRefresh();
    const refreshButton = document.getElementById("dhcpRefresh");
    if (refreshButton && !refreshButton.dataset.bound) {
      refreshButton.addEventListener("click", () => refreshDhcpDashboard(true));
      refreshButton.dataset.bound = "1";
    }
    const bindingsButton = document.getElementById("dhcpBindings");
    if (bindingsButton && !bindingsButton.dataset.bound) {
      bindingsButton.addEventListener("click", refreshDhcpBindings);
      bindingsButton.dataset.bound = "1";
    }
    const poolSearch = document.getElementById("dhcpPoolSearch");
    if (poolSearch && !poolSearch.dataset.bound) {
      poolSearch.addEventListener("input", () => {
        dhcpPoolSearchText = poolSearch.value;
        if (dhcpLastPayload) renderDhcpPoolBrowser(dhcpLastPayload.pools || [], dhcpLastPayload.conflicts || []);
      });
      poolSearch.dataset.bound = "1";
    }
    const poolFilter = document.getElementById("dhcpPoolFilter");
    if (poolFilter && !poolFilter.dataset.bound) {
      poolFilter.addEventListener("change", () => {
        dhcpPoolFilterValue = poolFilter.value;
        if (dhcpLastPayload) renderDhcpPoolBrowser(dhcpLastPayload.pools || [], dhcpLastPayload.conflicts || []);
      });
      poolFilter.dataset.bound = "1";
    }
    const poolsElement = document.getElementById("dhcpPools");
    if (poolsElement && !poolsElement.dataset.bound) {
      poolsElement.addEventListener("click", (event) => {
        const option = event.target.closest("[data-dhcp-pool]");
        if (!option) return;
        dhcpSelectedPoolKey = option.dataset.dhcpPool || "";
        if (dhcpLastPayload) {
          renderDhcpPoolBrowser(dhcpLastPayload.pools || [], dhcpLastPayload.conflicts || []);
          const detail = poolsElement.querySelector(".dhcp-pool-detail");
          if (detail) detail.scrollTop = 0;
        }
      });
      poolsElement.dataset.bound = "1";
    }
    refreshDhcpDashboard(false);
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
    stopDhcpRefresh();
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
    stopDhcpRefresh();
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
    stopDhcpRefresh();
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
    stopDhcpRefresh();
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
    stopDhcpRefresh();
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
    setupEvidencePanel();
  }

  function showWireless(page) {
    const screen = document.querySelector(".screen");
    stopInfraRefresh();
    stopTournamentRefresh();
    stopControlRefresh();
    stopDhcpRefresh();
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
    stopDhcpRefresh();
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
    stopDhcpRefresh();
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
    startDhcpRefresh();
  }

  function renderPage() {
    const page = pageFromPath();
    renderHeader(page);
    renderNav(page);
    const routeKey = `${page.id}${window.location.search}`;
    if (routeKey === activeRoute) return;
    activePageId = page.id;
    activeRoute = routeKey;
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
    return Boolean(gaugeTimer || chartTimer || tournamentTimer || wirelessTimer || controlTimer || dhcpTimer || topologyTimer);
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
  document.addEventListener("visibilitychange", () => {
    if (activePageId !== "dhcp") return;
    if (document.visibilityState === "hidden") stopDhcpRefresh();
    else startDhcpRefresh();
  });
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
