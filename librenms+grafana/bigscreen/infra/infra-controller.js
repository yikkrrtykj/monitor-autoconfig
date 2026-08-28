;(function (root, factory) {
  const ns = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = ns;
  } else {
    root.BSInfraController = ns;
  }
}(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";

  function createInfraController(dependencies) {
    const {
      document,
      window,
      console,
      queries,
      stageDeviceFilter,
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
      deleteRenderSignature,
      clearRenderSignatures,
      isStageFilterActive,
      onDataSuccess,
      createIspCarousel
    } = dependencies;

    const pingTrendQuery = queries.pingTrend || "";
    const pingSuccessTrendQuery = queries.pingSuccessTrend || "";
    const pingGaugeQuery = queries.pingGauge || "";
    const uptimeQuery = queries.uptime || "";
    const lossQuery = queries.loss || "";

    let gaugeTimer = null;
    let chartTimer = null;
    let seenUpTimer = null;
    let infraSeenUp = null;
    let infraCurrentTargets = null;
    let gaugeSeq = 0;
    let chartSeq = 0;
    let stageDeviceRegexCache = null;
    let ispTrafficResults = [];

    const ispCarousel = createIspCarousel({
      pageSize: 2,
      intervalMs: 10000,
      setIntervalFn: (callback, delay) => window.setInterval(callback, delay),
      clearIntervalFn: (handle) => window.clearInterval(handle),
      onPageChange: () => renderIspPanels(ispTrafficResults)
    });

    function stageDevicePattern() {
      const configured = String(stageDeviceFilter || "stage,wutai,舞台")
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

    function visibleInfraItems(items) {
      return isStageFilterActive() ? filterStageDeviceItems(items) : items;
    }

    function visibleInfraSeries(seriesList) {
      return isStageFilterActive() ? filterStageDeviceSeries(seriesList) : seriesList;
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
        onDataSuccess();
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
        onDataSuccess();
      } catch (error) {
        if (seq !== chartSeq) return;
        deleteRenderSignature("pingTrendChart");
        deleteRenderSignature("lossHeatmap");
        deleteRenderSignature("ispGrid");
        renderNoData(document.getElementById("pingTrendChart"));
        renderLossHeatmap("lossHeatmap", []);
        renderNoData(document.getElementById("ispGrid"));
        console.error(error);
      }
    }

    function start() {
      if (gaugeTimer || chartTimer) return;
      clearRenderSignatures();
      invalidateRangeCache();
      // Resolve the "deployed" set first so the first paint already hides
      // never-online targets; then keep it fresh on a slow timer.
      refreshInfraSeenUp().then(() => { refreshGauges(); refreshCharts(); });
      gaugeTimer = window.setInterval(refreshGauges, 5000);
      chartTimer = window.setInterval(refreshCharts, 5000);
      seenUpTimer = window.setInterval(refreshInfraSeenUp, 30000);
    }

    function stop() {
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

    function enterInfraMode() {
      ispCarousel.deactivate();
      renderIspPanels(ispTrafficResults);
    }

    function enterTournamentMode() {
      ispCarousel.activate({ reset: true });
      renderIspPanels(ispTrafficResults);
    }

    function hasScheduledRefresh() {
      return Boolean(gaugeTimer || chartTimer);
    }

    function refreshForResize() {
      if (chartTimer) refreshCharts();
    }

    return {
      start,
      stop,
      enterInfraMode,
      enterTournamentMode,
      hasScheduledRefresh,
      refreshForResize
    };
  }

  return { createInfraController };
}));
