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
  const { createIncidentPanel } = window.BSIncidentPanel;
  const { createWirelessPanel } = window.BSWirelessPanel;
  const { createTournamentPanel } = window.BSTournamentPanel;
  const { createIperfController } = window.BSIperfController;
  const { createDeliveryPanel } = window.BSDeliveryPanel;
  const { createTopologyPanel } = window.BSTopologyPanel;
  const { buildInfrastructurePingPresentation } = window.BSPingTransform;
  const {
    prometheusQuery, prometheusInstant, prometheusRangeFor,
    prometheusRangeCached, invalidateRangeCache,
    activeInfraPingQuery, activeSeriesNames,
    fetchIspNames, ispTrafficQuery, fetchIspTraffic, ispChartMaxBps,
    fetchInfraDeviceNames, renameListWithInfraMap, partitionInfraPingItems,
    fetchTopologyTargets, fetchTopologyEdges, fetchRuntimeStatus,
    fetchPlatformAuthStatus, loginPlatformAuth, changePlatformPassword, logoutPlatformAuth,
    fetchPlatformConfig, fetchPlatformVersion, fetchApplyStatus, postPlatform, fetchRetirePending, patchPlatform, fetchIncidents,
    fetchDhcpDashboard, fetchDhcpBindings, testDhcpConnection, fetchDhcpSettings, saveDhcpSettings
  } = window.BSApi;
  const {
    buildTopologyLayers, topologyLayout, renderTopologySvg, topologyNodeKindLabel,
    topologyLatencyIp
  } = window.BSTopology;
  const {
    isGatewayAddress, buildPlayers, latencyLevel, playerStatusText
  } = window.BSPlayers;
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
  let gaugeTimer = null;
  let chartTimer = null;
  let seenUpTimer = null;
  let infraSeenUp = null;  // Set of "deployed" (ever-online) infra instance names; null/empty = show all
  let infraCurrentTargets = null; // Current Prometheus targets; removes retired ISP/history series immediately
  let controlTimer = null;
  let activePageId = "";
  let activeRoute = "";
  let gaugeSeq = 0;
  let chartSeq = 0;
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
  const incidentPanel = createIncidentPanel({
    document,
    window,
    URLSearchParams,
    Date,
    console,
    escapeHtml,
    networkLabel,
    formatPingText,
    formatBits,
    dateTimeInputValue,
    fetchIspNames,
    ispTrafficQuery,
    prometheusRangeFor,
    analyzeIncident: window.BSIncident.analyzeIncident
  });
  const wirelessPanel = createWirelessPanel({
    document,
    window,
    console,
    escapeHtml,
    seatLabel,
    formatPingText,
    isGatewayAddress,
    latencyLevel,
    playerStatusText,
    teamName,
    latencyUrlForPlayer,
    renderNoData,
    fetchPlayerSnapshot,
    prometheusQuery,
    triggerRescan,
    onDataSuccess: () => { lastDataSuccessAt = Date.now(); }
  });
  const tournamentPanel = createTournamentPanel({
    document,
    window,
    console,
    teamLayouts,
    getTeamOrders: () => config.teamOrders,
    seriesColors,
    escapeHtml,
    seatLabel,
    formatPingText,
    niceMax,
    average,
    linePathFromPoints,
    teamName,
    latencyUrlForPlayer,
    playerLabel,
    buildPlayers,
    latencyLevel,
    tournamentSelector,
    fetchPlayerSnapshot,
    prometheusRangeCached,
    renderNoData,
    shouldRender,
    seriesSignature,
    deleteRenderSignature: (key) => renderSignatures.delete(key),
    clearRenderSignatures: () => renderSignatures.clear(),
    invalidateRangeCache,
    onDataSuccess: () => { lastDataSuccessAt = Date.now(); }
  });
  const iperfController = createIperfController({
    document,
    window,
    fetch,
    escapeHtml,
    postPlatform,
    fetchIperfStatus: window.BSApi.fetchIperfStatus,
    fetchIperfHistory: window.BSApi.fetchIperfHistory,
    defaultCustomPreset: window.BSIperf.DEFAULT_CUSTOM_PRESET,
    resultView: window.BSIperf.resultView,
    historyHtml: window.BSIperf.historyHtml,
    loadServerConfig: window.BSIperf.loadServerConfig,
    presetView: window.BSIperf.presetView
  });
  const deliveryPanel = createDeliveryPanel({
    document,
    setTimeout: window.setTimeout.bind(window),
    escapeHtml,
    postPlatform,
    fetchRetirePending,
    iperfController
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

  function latencyUrlForPlayer(player) {
    const params = new URLSearchParams({
      team: String(player.team),
      seat: String(player.seat),
      network: player.network || "wired"
    });
    if (player.ip) params.set("ip", player.ip);
    return `/latency?${params.toString()}`;
  }

  function playerLatencySnapshotQuery(selector) {
    const retained = `(max_over_time(probe_success{${selector}}[${playerOfflineGraceWindow}]) == 1)`;
    return `avg_over_time(probe_icmp_duration_seconds{${selector},phase="rtt"}[${playerSnapshotWindow}]) and on(instance,team,seat,network) ${retained}`;
  }

  function playerSuccessSnapshotQuery(selector) {
    const retained = `(max_over_time(probe_success{${selector}}[${playerOfflineGraceWindow}]) == 1)`;
    return `last_over_time(probe_success{${selector}}[${playerSnapshotWindow}]) and on(instance,team,seat,network) ${retained}`;
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

  function triggerRescan(btn) {
    btn.disabled = true;
    btn.classList.add("spinning");
    fetch("/player-targets/rescan", { method: "POST" })
      .finally(() => {
        setTimeout(() => { btn.disabled = false; btn.classList.remove("spinning"); }, 3000);
      });
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
    deliveryPanel.render();
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
    tournamentPanel.stop();
    wirelessPanel.stop();
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
    tournamentPanel.stop();
    wirelessPanel.stop();
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
    tournamentPanel.stop();
    wirelessPanel.stop();
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
    wirelessPanel.stop();
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
    tournamentPanel.start(page);
  }

  function showEvidence() {
    const screen = document.querySelector(".screen");
    stopInfraRefresh();
    tournamentPanel.stop();
    wirelessPanel.stop();
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
    tournamentPanel.stop();
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
    wirelessPanel.start(page);
  }

  function showIncident() {
    const screen = document.querySelector(".screen");
    stopInfraRefresh();
    tournamentPanel.stop();
    wirelessPanel.stop();
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
    incidentPanel.start();
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
    tournamentPanel.stop();
    wirelessPanel.stop();
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
    tournamentPanel.stop();
    wirelessPanel.stop();
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
    if (page.id !== "incident") incidentPanel.stop();
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
    return Boolean(gaugeTimer || chartTimer || tournamentPanel.hasScheduledRefresh() || wirelessPanel.hasScheduledRefresh() || controlTimer || dhcpPanel.hasScheduledRefresh() || topologyTimer);
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
      if (tournamentPanel.hasScheduledRefresh()) {
        const current = activePage();
        if (current && current.kind) tournamentPanel.refresh(current);
      }
      if (topologyTimer) refreshTopology();
    }, 200);
  });
})();
