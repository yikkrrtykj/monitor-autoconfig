(function () {
  const config = window.BIGSCREEN_CONFIG || {};
  const queries = window.BIGSCREEN_QUERIES || {};
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
  const { createAuthController } = window.BSAuthController;
  const { createIncidentRegistry } = window.BSIncidentRegistry;
  const { createTopologyPanel } = window.BSTopologyPanel;
  const { createInfraController } = window.BSInfraController;
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
  let controlTimer = null;
  let activePageId = "";
  let activeRoute = "";
  let topologySeq = 0;
  const renderSignatures = new Map();
  let lastDataSuccessAt = 0;
  let lastControlReport = null;
  const DATA_STALE_AFTER_MS = 20000;
  const CONTROL_LAYOUT_STORAGE_KEY = "bigscreen.controlLayout.v1";
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

  function activePage() {
    return pages.find((page) => page.id === activePageId) || {};
  }

  function playerLabel(team, seat, network) {
    return `${teamName({ id: "" }, team)} ${seatLabel(seat)} ${networkLabel(network)}`;
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
  const infraController = createInfraController({
    document,
    window,
    console,
    queries,
    stageDeviceFilter: config.stageDeviceFilter,
    escapeHtml,
    escapeRegex,
    metricName,
    formatPing,
    formatUptime,
    gaugeColor,
    gaugePercent,
    seriesSignature,
    activeInfraPingQuery,
    activeSeriesNames,
    prometheusQuery,
    prometheusInstant,
    prometheusRangeCached,
    invalidateRangeCache,
    fetchInfraDeviceNames,
    renameListWithInfraMap,
    partitionInfraPingItems,
    fetchTopologyTargets,
    fetchIspTraffic,
    buildInfrastructurePingPresentation,
    renderPingChart,
    renderLossHeatmap,
    renderIspChart,
    renderNoData,
    setVisible,
    shouldRender,
    deleteRenderSignature: (key) => renderSignatures.delete(key),
    clearRenderSignatures: () => renderSignatures.clear(),
    isStageFilterActive: () => Boolean(activePage().kind),
    onDataSuccess: () => { lastDataSuccessAt = Date.now(); },
    createIspCarousel
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
  const authController = createAuthController({
    document,
    fetchPlatformAuthStatus,
    loginPlatformAuth,
    changePlatformPassword,
    logoutPlatformAuth,
    onAuthenticated: () => refreshControlPanel(),
    onLoggedOut: () => { lastControlReport = null; }
  });
  const incidentRegistry = createIncidentRegistry({
    document,
    escapeHtml,
    formatTimestampFull,
    fetchIncidents,
    postPlatform,
    patchPlatform,
    getControlReport: () => lastControlReport,
    now: () => Date.now()
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
    incidentRegistry.render(snapshot.incidents);
    deliveryPanel.render();
    lastControlReport = snapshot;
    lastDataSuccessAt = Date.now();
  }

  async function refreshControlPanel() {
    // While 应用配置 is restarting services, its own flow drives the UI and waits
    // for recovery -- don't let the periodic refresh fight it with failed fetches.
    if (configEditor.isApplyInProgress()) return;
    if (!await authController.ensureAuthenticated()) {
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

  function setupControlPanel() {
    authController.bind();
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
    incidentRegistry.bind();
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

  function stopControlRefresh() {
    if (controlTimer) {
      window.clearInterval(controlTimer);
      controlTimer = null;
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
    infraController.stop();
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
    infraController.stop();
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
    infraController.enterInfraMode();
    setVisible("homePanel", false);
    setVisible("panelGrid", true);
    setVisible("tournamentPanel", false);
    setVisible("evidencePanel", false);
    setVisible("wirelessPanel", false);
    setVisible("controlPanel", false);
    setVisible("dhcpPanel", false);
    setVisible("incidentPanel", false);
    setVisible("topologyPanel", false);
    infraController.start();
  }

  function showTournament(page) {
    const screen = document.querySelector(".screen");
    wirelessPanel.stop();
    stopControlRefresh();
    dhcpPanel.stop();
    stopTopologyRefresh();
    screen.className = `screen tournament-mode ${page.kind === "match" ? "match-mode" : "multi-team-mode"} ${page.id}`;
    infraController.enterTournamentMode();
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
    infraController.start();
    tournamentPanel.start(page);
  }

  function showEvidence() {
    const screen = document.querySelector(".screen");
    infraController.stop();
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
    infraController.stop();
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
    infraController.stop();
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
      edge.edge_type === "server_attachment" || edge.source === "fdb" ? "attachment" : "",
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
    infraController.stop();
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
    infraController.stop();
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
    return Boolean(infraController.hasScheduledRefresh() || tournamentPanel.hasScheduledRefresh() || wirelessPanel.hasScheduledRefresh() || controlTimer || dhcpPanel.hasScheduledRefresh() || topologyTimer);
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
      infraController.refreshForResize();
      if (tournamentPanel.hasScheduledRefresh()) {
        const current = activePage();
        if (current && current.kind) tournamentPanel.refresh(current);
      }
      if (topologyTimer) refreshTopology();
    }, 200);
  });
})();
