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
    networkLabel, seatLabel, gaugeColor, gaugePercent, smoothValues,
    linePathFromPoints
  } = window.BSUtils;
  const {
    prometheusBaseUrl, prometheusQuery, prometheusInstant, prometheusRangeFor,
    prometheusRangeCached, invalidateRangeCache,
    activeInfraPingQuery, activeSeriesNames, filterSeriesByNames,
    fetchIspNames, ispTrafficQuery, fetchIspTraffic, ispCapacityBps, ispChartMaxBps,
    fetchTopologyTargets, fetchTopologyEdges
  } = window.BSApi;
  const {
    buildTopologyLayers, topologyLayout, renderTopologySvg, topologyNodeKindLabel
  } = window.BSTopology;
  let gaugeTimer = null;
  let chartTimer = null;
  let tournamentTimer = null;
  let opsTimer = null;
  let activePageId = "";
  let activeRoute = "";
  let gaugeSeq = 0;
  let chartSeq = 0;
  let tournamentSeq = 0;
  let topologySeq = 0;
  let stageDeviceRegexCache = null;
  const renderSignatures = new Map();
  let lastDataSuccessAt = 0;
  const DATA_STALE_AFTER_MS = 20000;

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
    const series = seriesList
      .filter((item) => item.values.length)
      .map((item) => ({
        ...item,
        values: smoothValues(item.values, options.smoothWindow)
      }));
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
    const rows = series.map((item) => {
      const cells = item.values.map((point) => {
        const level = point.v > 0.5 ? "bad" : point.v > 0 ? "warn" : "good";
        return `<span class="heatmap-cell ${level}"></span>`;
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
    ispGrid.style.setProperty("--isp-count", String(Math.max(1, results.length)));
    ispGrid.innerHTML = "";
    if (!results.length) {
      renderNoData(ispGrid);
      return;
    }
    const fragment = document.createDocumentFragment();
    results.forEach((result, index) => {
      const panel = document.createElement("section");
      panel.className = "chart-panel isp-panel";
      panel.innerHTML = `<h2>${escapeHtml(result.name)}</h2><div class="chart-body" id="ispChart${index}"></div>`;
      fragment.appendChild(panel);
    });
    ispGrid.appendChild(fragment);
    results.forEach((result, index) => {
      renderLineChart(`ispChart${index}`, [result.download, result.upload], {
        axisFormatter: formatBits,
        valueFormatter: formatBits,
        axisPadLeft: 92,
        axisPadRight: 38,
        fill: true,
        legend: "bottom",
        maxY: ispChartMaxBps(result.name),
        minMax: 1,
        calcs: ["last", "max"]
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

  function playerKey(metric) {
    return [metric.team || "", metric.seat || "", metric.instance || "", metric.network || ""].join("|");
  }

  function isGatewayAddress(ip) {
    return /\.254$/.test(String(ip || ""));
  }

  function preferPlayer(prev, candidate) {
    if (candidate.success && !prev.success) return candidate;
    if (!candidate.success && prev.success) return prev;
    const candFinite = Number.isFinite(candidate.latency);
    const prevFinite = Number.isFinite(prev.latency);
    if (candFinite && !prevFinite) return candidate;
    if (!candFinite && prevFinite) return prev;
    if (candFinite && prevFinite && candidate.latency < prev.latency) return candidate;
    return prev;
  }

  function dedupePlayersBySeat(players) {
    // Switch MAC table caches a recently-aged entry alongside the live one,
    // so the generator emits multiple (ip) targets per (team, seat, network).
    // The bigscreen has one slot per seat -- keep the online entry; if both
    // online, the lower-latency wins.
    const seen = new Map();
    for (const player of players) {
      const key = `${player.team}|${player.seat}|${player.network}`;
      const prev = seen.get(key);
      seen.set(key, prev ? preferPlayer(prev, player) : player);
    }
    return Array.from(seen.values());
  }

  function buildPlayers(latencyItems, successItems) {
    const byKey = new Map();
    successItems.forEach((item) => {
      if (isGatewayAddress(item.metric.instance)) return;
      byKey.set(playerKey(item.metric), {
        team: Number(item.metric.team || 0),
        seat: Number(item.metric.seat || 0),
        ip: item.metric.instance || "",
        network: item.metric.network || "",
        success: item.value >= 1,
        latency: null
      });
    });
    latencyItems.forEach((item) => {
      if (isGatewayAddress(item.metric.instance)) return;
      const key = playerKey(item.metric);
      const player = byKey.get(key) || {
        team: Number(item.metric.team || 0),
        seat: Number(item.metric.seat || 0),
        ip: item.metric.instance || "",
        network: item.metric.network || "",
        success: true,
        latency: null
      };
      player.latency = item.value;
      byKey.set(key, player);
    });
    const all = Array.from(byKey.values())
      .filter((player) => player.team > 0 && player.seat > 0 && player.ip);
    return dedupePlayersBySeat(all)
      .sort((a, b) => a.team - b.team || a.seat - b.seat || a.ip.localeCompare(b.ip));
  }

  function latencyLevel(player) {
    if (!player || !player.success) return "offline";
    if (!Number.isFinite(player.latency)) return "unknown";
    if (player.latency >= 0.08) return "bad";
    if (player.latency >= 0.04) return "warn";
    return "good";
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
    return `avg_over_time(probe_icmp_duration_seconds{${selector},phase="rtt"}[${playerSnapshotWindow}])`;
  }

  function playerSuccessSnapshotQuery(selector) {
    return `max_over_time(probe_success{${selector}}[${playerSnapshotWindow}])`;
  }

  function lineChartOptions() {
    return {
      axisFormatter: formatPingText,
      valueFormatter: formatPingText,
      minMax: 0.005,
      smooth: true
    };
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

  async function refreshGauges() {
    const seq = ++gaugeSeq;
    try {
      const [pingItems, uptimeItems] = await Promise.all([
        prometheusQuery(pingGaugeQuery),
        prometheusQuery(uptimeQuery)
      ]);
      if (seq !== gaugeSeq) return;
      const isServerItem = (item) => (item.metric && item.metric.job) === "infra-srv-ping";
      const networkPing = pingItems.filter((item) => !isServerItem(item));
      const serverPing = pingItems.filter(isServerItem);
      renderGaugeGrid("pingGaugeGrid", visibleInfraItems(networkPing), "ping");
      // Servers aren't stage devices (skip the stage filter); keep them on one row.
      renderGaugeGrid("pingServerGaugeGrid", serverPing, "ping", 1);
      renderGaugeGrid("uptimeGaugeGrid", visibleInfraItems(uptimeItems), "uptime");
      lastDataSuccessAt = Date.now();
    } catch (error) {
      if (seq !== gaugeSeq) return;
      renderGaugeGrid("pingGaugeGrid", [], "ping");
      renderGaugeGrid("pingServerGaugeGrid", [], "ping");
      renderGaugeGrid("uptimeGaugeGrid", [], "uptime");
      console.error(error);
    }
  }

  async function refreshCharts() {
    const seq = ++chartSeq;
    try {
      const [activeItems, pingSeries, lossSeries, ispTraffic] = await Promise.all([
        prometheusInstant(activeInfraPingQuery()),
        prometheusRangeCached(pingTrendQuery),
        prometheusRangeCached(lossQuery),
        fetchIspTraffic()
      ]);
      if (seq !== chartSeq) return;
      const activeNames = activeSeriesNames(visibleInfraItems(activeItems));
      const activePingSeries = visibleInfraSeries(filterSeriesByNames(pingSeries, activeNames));
      const activeLossSeries = visibleInfraSeries(filterSeriesByNames(lossSeries, activeNames));
      if (shouldRender("pingTrendChart", seriesSignature(activePingSeries))) {
        renderLineChart("pingTrendChart", activePingSeries, {
          axisFormatter: formatPingText,
          valueFormatter: formatPingText,
          minMax: 0.005
        });
      }
      if (shouldRender("lossHeatmap", seriesSignature(activeLossSeries))) {
        renderHeatmap("lossHeatmap", activeLossSeries);
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
      renderNoData(document.getElementById("lossHeatmap"));
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

  function renderOpsKpis(items) {
    document.getElementById("opsSummary").innerHTML = items.map((item) => `
      <div class="ops-kpi ${item.level || "info"}">
        <span>${escapeHtml(item.label)}</span>
        <strong>${escapeHtml(item.value)}</strong>
        <em>${escapeHtml(item.note || "")}</em>
      </div>
    `).join("");
  }

  function playerStatusText(player) {
    if (!player.success) return "离线";
    if (!Number.isFinite(player.latency)) return "暂无延迟";
    if (player.latency >= 0.08) return "高延迟";
    if (player.latency >= 0.04) return "轻微抖动";
    return "正常";
  }

  function renderWirelessControls() {
    const controls = document.getElementById("opsControls");
    if (controls.dataset.mode === "wireless") return;
    controls.dataset.mode = "wireless";
    controls.innerHTML = `
      <div class="ops-title">
        <strong>无线异常总览</strong>
        <span>只统计无线选手，用来确认是否有人连入 WiFi，以及是否出现高延迟或离线。</span>
      </div>
    `;
  }

  function renderWirelessBoard(players) {
    const board = document.getElementById("opsBoard");
    if (!players.length) {
      renderNoData(board, "当前没有无线选手");
      return;
    }
    const rows = players
      .slice()
      .sort((a, b) => Number(a.success) - Number(b.success) || (b.latency || 0) - (a.latency || 0) || a.team - b.team || a.seat - b.seat)
      .map((player) => `
        <a class="ops-table-row ${latencyLevel(player)}" href="${escapeHtml(latencyUrlForPlayer(player))}">
          <span>${escapeHtml(teamName({ id: "" }, player.team))}</span>
          <span>${escapeHtml(seatLabel(player.seat))}</span>
          <span>${escapeHtml(player.ip)}</span>
          <span>${escapeHtml(Number.isFinite(player.latency) ? formatPingText(player.latency) : "-")}</span>
          <span>${escapeHtml(playerStatusText(player))}</span>
        </a>
      `).join("");
    board.innerHTML = `
      <div class="ops-table">
        <div class="ops-table-head"><span>队伍</span><span>座位</span><span>IP</span><span>延迟</span><span>状态</span></div>
        ${rows}
      </div>
    `;
  }

  async function refreshWirelessOverview() {
    renderWirelessControls();
    try {
      const snapshot = await fetchPlayerSnapshot('role="player",network="wireless"');
      const rawItems = [...snapshot.latencyItems, ...snapshot.successItems];
      const gatewayIps = new Set(rawItems.map((item) => item.metric.instance).filter(isGatewayAddress));
      const players = snapshot.players;
      const online = players.filter((player) => player.success).length;
      const high = players.filter((player) => player.success && Number.isFinite(player.latency) && player.latency >= 0.08).length;
      const maxLatency = players
        .filter((player) => Number.isFinite(player.latency))
        .map((player) => player.latency)
        .sort((a, b) => b - a)[0];
      renderOpsKpis([
        { label: "无线目标", value: players.length, note: "当前识别到的无线选手" },
        { label: "在线", value: online, level: !players.length || online === players.length ? "good" : "warn", note: "当前可达" },
        { label: "高延迟", value: high, level: high ? "warn" : "good", note: ">= 80 ms" },
        { label: "疑似网关", value: gatewayIps.size, level: gatewayIps.size ? "bad" : "good", note: ".254" },
        { label: "最高延迟", value: Number.isFinite(maxLatency) ? formatPingText(maxLatency) : "-", level: maxLatency >= 0.08 ? "warn" : "good" }
      ]);
      renderWirelessBoard(players);
      lastDataSuccessAt = Date.now();
    } catch (error) {
      renderNoData(document.getElementById("opsSummary"), "查询失败");
      renderNoData(document.getElementById("opsBoard"));
      console.error(error);
    }
  }

  function seatCheckConfigFromUrl() {
    const params = new URLSearchParams(window.location.search);
    const layout = params.get("layout") || "match-5v5";
    const network = params.get("network") || "wired";
    return {
      page: pages.find((page) => page.id === layout && page.kind) || pages.find((page) => page.id === "match-5v5"),
      network: ["wired", "wireless", "all"].includes(network) ? network : "wired"
    };
  }

  function renderSeatCheckControls(page, network) {
    const matchPages = pages.filter((item) => item.kind);
    const controls = document.getElementById("opsControls");
    const modeKey = `seat-check:${page.id}:${network}`;
    if (controls.dataset.mode === modeKey) return;
    controls.dataset.mode = modeKey;
    controls.innerHTML = `
      <label>赛制
        <select id="seatCheckLayout">
          ${matchPages.map((item) => `<option value="${escapeHtml(item.id)}"${item.id === page.id ? " selected" : ""}>${escapeHtml(item.label)}</option>`).join("")}
        </select>
      </label>
      <label>网络
        <select id="seatCheckNetwork">
          ${["wired", "wireless", "all"].map((item) => `<option value="${item}"${item === network ? " selected" : ""}>${networkLabel(item)}</option>`).join("")}
        </select>
      </label>
      <div class="ops-title compact">
        <strong>赛前座位核对</strong>
        <span>缺失、重复、离线会直接标出。</span>
      </div>
    `;
    document.getElementById("seatCheckLayout").addEventListener("change", updateSeatCheckUrl);
    document.getElementById("seatCheckNetwork").addEventListener("change", updateSeatCheckUrl);
  }

  function updateSeatCheckUrl() {
    const layout = document.getElementById("seatCheckLayout").value;
    const network = document.getElementById("seatCheckNetwork").value;
    window.history.replaceState({}, "", `/seat-check?layout=${encodeURIComponent(layout)}&network=${encodeURIComponent(network)}`);
    activeRoute = `seat-check${window.location.search}`;
    refreshSeatCheck();
  }

  function groupPlayersBySeat(players) {
    const grouped = new Map();
    players.forEach((player) => {
      const key = `${player.team}|${player.seat}`;
      if (!grouped.has(key)) grouped.set(key, []);
      grouped.get(key).push(player);
    });
    return grouped;
  }

  function renderCheckSeat(team, seat, players) {
    if (!players.length) {
      return `<div class="check-seat missing"><span>${seatLabel(seat)}</span><strong>缺失</strong><em>-</em></div>`;
    }
    const player = players[0];
    const duplicate = players.length > 1;
    const level = duplicate ? "duplicate" : latencyLevel(player);
    const status = duplicate ? `重复 ${players.length}` : playerStatusText(player);
    return `
      <a class="check-seat ${level}" href="${escapeHtml(latencyUrlForPlayer(player))}">
        <span>${seatLabel(seat)}</span>
        <strong>${escapeHtml(status)}</strong>
        <em>${escapeHtml(player.ip)}</em>
      </a>
    `;
  }

  function renderSeatCheckBoard(page, players) {
    const grouped = groupPlayersBySeat(players);
    document.getElementById("opsBoard").innerHTML = `
      <div class="seat-check-grid">
        ${(page.teams || []).map((team) => `
          <article class="seat-check-card">
            <header><strong>${escapeHtml(teamName(page, team))}</strong><span>${page.teamSize} 座</span></header>
            <div>
              ${Array.from({ length: page.teamSize }, (_, index) => {
                const seat = index + 1;
                return renderCheckSeat(team, seat, grouped.get(`${team}|${seat}`) || []);
              }).join("")}
            </div>
          </article>
        `).join("")}
      </div>
    `;
  }

  async function refreshSeatCheck() {
    const { page, network } = seatCheckConfigFromUrl();
    renderSeatCheckControls(page, network);
    try {
      const snapshot = await fetchPlayerSnapshot(tournamentSelector(page, network));
      const players = snapshot.players.filter((player) => !page.teamSize || player.seat <= page.teamSize);
      const grouped = groupPlayersBySeat(players);
      const expected = (page.teams || []).length * page.teamSize;
      const missing = (page.teams || []).reduce((sum, team) => {
        return sum + Array.from({ length: page.teamSize }, (_, index) => index + 1)
          .filter((seat) => !(grouped.get(`${team}|${seat}`) || []).length).length;
      }, 0);
      const duplicateSeats = Array.from(grouped.values()).filter((items) => items.length > 1).length;
      const online = players.filter((player) => player.success).length;
      renderOpsKpis([
        { label: "应到座位", value: expected, note: `${page.label} · ${networkLabel(network)}` },
        { label: "已识别", value: grouped.size, level: grouped.size === expected ? "good" : "warn", note: "按队伍/座位去重" },
        { label: "在线", value: online, level: !players.length || online === players.length ? "good" : "warn", note: `${players.length} 个目标` },
        { label: "缺失", value: missing, level: missing ? "bad" : "good", note: "未发现 IP" },
        { label: "重复座位", value: duplicateSeats, level: duplicateSeats ? "bad" : "good", note: "同座位多个 IP" }
      ]);
      renderSeatCheckBoard(page, players);
      lastDataSuccessAt = Date.now();
    } catch (error) {
      renderNoData(document.getElementById("opsSummary"), "查询失败");
      renderNoData(document.getElementById("opsBoard"));
      console.error(error);
    }
  }

  function dateTimeInputValue(date) {
    const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
    return local.toISOString().slice(0, 16);
  }

  function evidenceWindow() {
    const atInput = document.getElementById("evidenceAt");
    const windowInput = document.getElementById("evidenceWindow");
    const centerDate = atInput && atInput.value ? new Date(atInput.value) : new Date();
    const center = Number.isFinite(centerDate.getTime()) ? centerDate.getTime() / 1000 : Date.now() / 1000;
    const minutes = Math.max(1, Number(windowInput && windowInput.value ? windowInput.value : 10));
    const now = Math.floor(Date.now() / 1000);
    const end = Math.min(Math.floor(center + minutes * 60), now);
    const start = Math.floor(center - minutes * 60);
    return {
      start: start <= end ? start : Math.max(0, end - minutes * 60),
      end,
      step: 5
    };
  }

  function evidencePlayerSelector(team, seat, network) {
    const networkFilter = network === "all" ? 'network=~".*"' : `network="${escapeLabel(network)}"`;
    return `role="player",team="${escapeLabel(team)}",seat="${escapeLabel(seat)}",${networkFilter}`;
  }

  function evidenceLatencyQuery(team, seat, network, ip) {
    if (ip) {
      const ipStr = escapeLabel(ip);
      return `probe_icmp_duration_seconds{instance="${ipStr}",phase="rtt"} or probe_icmp_duration_seconds{target_ip="${ipStr}",phase="rtt"}`;
    }
    return `probe_icmp_duration_seconds{${evidencePlayerSelector(team, seat, network)},phase="rtt"}`;
  }

  function evidenceSuccessQuery(team, seat, network, ip) {
    if (ip) {
      const ipStr = escapeLabel(ip);
      return `probe_success{instance="${ipStr}"} or probe_success{target_ip="${ipStr}"}`;
    }
    return `probe_success{${evidencePlayerSelector(team, seat, network)}}`;
  }

  function evidenceSeriesName(metric) {
    const seat = metric.seat ? `S${metric.seat}` : "";
    const ip = metric.instance || "";
    const network = metric.network ? ` ${networkLabel(metric.network)}` : "";
    return `${seat} ${ip}${network}`.trim() || "选手";
  }

  function flattenSeriesValues(seriesList) {
    return seriesList.flatMap((series) => series.values.map((point) => point.v)).filter((value) => Number.isFinite(value));
  }

  function estimateStepSeconds(seriesList) {
    const times = seriesList.flatMap((series) => series.values.map((point) => point.t)).sort((a, b) => a - b);
    const gaps = [];
    for (let index = 1; index < times.length; index += 1) {
      const gap = times[index] - times[index - 1];
      if (gap > 0 && gap < 300) gaps.push(gap);
    }
    return gaps.length ? Math.round(average(gaps)) : 5;
  }

  function evidenceVerdict(latencyValues, successValues) {
    const maxLatency = latencyValues.length ? Math.max(...latencyValues) : null;
    const avgLatency = latencyValues.length ? average(latencyValues) : null;
    const failCount = successValues.filter((value) => value < 0.5).length;

    if (!latencyValues.length && !successValues.length) {
      return { level: "unknown", text: "没有查到数据", detail: "这个时间窗口内 Prometheus 没有这名选手的采样。" };
    }
    if (failCount > 0) {
      return { level: "bad", text: "存在断线/探测失败", detail: "在线状态出现失败采样，可以直接截图给裁判确认。" };
    }
    if (avgLatency !== null && avgLatency >= 0.08) {
      return { level: "bad", text: "持续高延迟", detail: "平均延迟已经超过 80 ms，属于明显异常。" };
    }
    if (maxLatency !== null && maxLatency >= 0.1) {
      return { level: "warn", text: "有高延迟尖峰", detail: "最高延迟超过 100 ms，可能对应玩家反馈的延迟异常瞬间。" };
    }
    if (maxLatency !== null && maxLatency >= 0.04) {
      return { level: "warn", text: "有轻微抖动", detail: "有 40 ms 以上波动，建议结合现场体验判断。" };
    }
    return { level: "good", text: "未见明显网络异常", detail: "这个窗口内延迟和在线状态都比较稳定。" };
  }

  function renderEvidenceSummary(context, latencySeries, successSeries) {
    const container = document.getElementById("evidenceSummary");
    const latencyValues = flattenSeriesValues(latencySeries);
    const successValues = flattenSeriesValues(successSeries);
    const verdict = evidenceVerdict(latencyValues, successValues);
    const maxLatency = latencyValues.length ? formatPingText(Math.max(...latencyValues)) : "-";
    const avgLatency = latencyValues.length ? formatPingText(average(latencyValues)) : "-";
    const onlineRate = successValues.length ? `${(average(successValues) * 100).toFixed(1)}%` : "-";
    const failCount = successValues.filter((value) => value < 0.5).length;
    const offlineSeconds = failCount ? `${Math.round(failCount * estimateStepSeconds(successSeries))}s` : "0s";

    container.innerHTML = `
      <div class="evidence-verdict ${verdict.level}">
        <span>${escapeHtml(context.label)}</span>
        <strong>${escapeHtml(verdict.text)}</strong>
        <em>${escapeHtml(verdict.detail)}</em>
      </div>
      <div class="evidence-kpis">
        <div><span>平均延迟</span><strong>${escapeHtml(avgLatency)}</strong></div>
        <div><span>最高延迟</span><strong>${escapeHtml(maxLatency)}</strong></div>
        <div><span>在线率</span><strong>${escapeHtml(onlineRate)}</strong></div>
        <div><span>离线累计</span><strong>${escapeHtml(offlineSeconds)}</strong></div>
      </div>
    `;
  }

  async function queryEvidence() {
    const team = document.getElementById("evidenceTeam").value || "1";
    const seat = document.getElementById("evidenceSeat").value || "1";
    const network = document.getElementById("evidenceNetwork").value || "wired";
    const range = document.getElementById("evidenceWindow").value || "5";
    const at = document.getElementById("evidenceAt").value || "";
    const ip = (document.getElementById("evidenceIp").value || "").trim();
    const queryWindow = evidenceWindow();
    const latencyQuery = evidenceLatencyQuery(team, seat, network, ip);
    const successQuery = evidenceSuccessQuery(team, seat, network, ip);
    const label = ip
      ? `${ip} · ${formatTime(queryWindow.start)}-${formatTime(queryWindow.end)}`
      : `${playerLabel(team, seat, network)} · ${formatTime(queryWindow.start)}-${formatTime(queryWindow.end)}`;
    const params = new URLSearchParams({ team, seat, network, range });
    if (at) params.set("at", at);
    if (ip) params.set("ip", ip);
    window.history.replaceState({}, "", `/latency?${params.toString()}`);

    renderNoData(document.getElementById("evidenceLatencyChart"), "加载中");
    renderNoData(document.getElementById("evidenceSuccessChart"), "加载中");

    try {
      const [latencySeries, successSeries] = await Promise.all([
        prometheusRangeFor(latencyQuery, queryWindow, evidenceSeriesName),
        prometheusRangeFor(successQuery, queryWindow, evidenceSeriesName)
      ]);
      renderEvidenceSummary({ label }, latencySeries, successSeries);
      renderLineChart("evidenceLatencyChart", latencySeries, {
        axisFormatter: formatPingText,
        valueFormatter: formatPingText,
        minMax: 0.005,
        smooth: true,
        legend: "bottom"
      });
      renderLineChart("evidenceSuccessChart", successSeries.map((series) => ({ ...series, color: "#73d17a" })), {
        axisFormatter: (value) => `${Math.round(value * 100)}%`,
        valueFormatter: (value) => `${Math.round(value * 100)}%`,
        minMax: 1,
        smooth: false,
        fill: true,
        legend: "bottom"
      });
    } catch (error) {
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
    if (ip) document.getElementById("evidenceIp").value = ip;
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
      form.dataset.bound = "1";
    }
    queryEvidence();
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
    if (gaugeTimer) {
      window.clearInterval(gaugeTimer);
      gaugeTimer = null;
    }
    if (chartTimer) {
      window.clearInterval(chartTimer);
      chartTimer = null;
    }
  }

  function stopTournamentRefresh() {
    if (tournamentTimer) {
      window.clearInterval(tournamentTimer);
      tournamentTimer = null;
    }
  }

  function stopOpsRefresh() {
    if (opsTimer) {
      window.clearInterval(opsTimer);
      opsTimer = null;
    }
  }

  function startInfraRefresh() {
    if (gaugeTimer || chartTimer) return;
    renderSignatures.clear();
    invalidateRangeCache();
    refreshGauges();
    refreshCharts();
    gaugeTimer = window.setInterval(refreshGauges, 5000);
    chartTimer = window.setInterval(refreshCharts, 5000);
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

  function startOpsRefresh(page) {
    stopOpsRefresh();
    const refresh = page.id === "wireless" ? refreshWirelessOverview : refreshSeatCheck;
    refresh();
    opsTimer = window.setInterval(refresh, 5000);
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
    stopOpsRefresh();
    stopTopologyRefresh();
    screen.className = "screen home-mode";
    setVisible("homePanel", true);
    setVisible("panelGrid", false);
    setVisible("tournamentPanel", false);
    setVisible("evidencePanel", false);
    setVisible("opsPanel", false);
    setVisible("incidentPanel", false);
    setVisible("heatmapPanel", false);
    setVisible("topologyPanel", false);
    renderHomeCards();
  }

  function showInfra() {
    const screen = document.querySelector(".screen");
    stopTournamentRefresh();
    stopOpsRefresh();
    stopTopologyRefresh();
    screen.className = "screen infra-mode";
    setVisible("homePanel", false);
    setVisible("panelGrid", true);
    setVisible("tournamentPanel", false);
    setVisible("evidencePanel", false);
    setVisible("opsPanel", false);
    setVisible("incidentPanel", false);
    setVisible("heatmapPanel", false);
    setVisible("topologyPanel", false);
    startInfraRefresh();
  }

  function showTournament(page) {
    const screen = document.querySelector(".screen");
    stopOpsRefresh();
    stopTopologyRefresh();
    screen.className = `screen tournament-mode ${page.kind === "match" ? "match-mode" : "multi-team-mode"} ${page.id}`;
    setVisible("homePanel", false);
    setVisible("panelGrid", true);
    setVisible("tournamentPanel", true);
    setVisible("evidencePanel", false);
    setVisible("opsPanel", false);
    setVisible("incidentPanel", false);
    setVisible("heatmapPanel", false);
    setVisible("topologyPanel", false);
    document.getElementById("tournamentPanel").className = `tournament-panel ${page.kind === "match" ? "match-panel" : "multi-team-panel"} ${page.id}`;
    startInfraRefresh();
    startTournamentRefresh(page);
  }

  function showEvidence() {
    const screen = document.querySelector(".screen");
    stopInfraRefresh();
    stopTournamentRefresh();
    stopOpsRefresh();
    stopTopologyRefresh();
    screen.className = "screen evidence-mode";
    setVisible("homePanel", false);
    setVisible("panelGrid", false);
    setVisible("tournamentPanel", false);
    setVisible("evidencePanel", true);
    setVisible("opsPanel", false);
    setVisible("incidentPanel", false);
    setVisible("heatmapPanel", false);
    setVisible("topologyPanel", false);
    setupEvidencePanel();
  }

  function showOps(page) {
    const screen = document.querySelector(".screen");
    stopInfraRefresh();
    stopTournamentRefresh();
    stopTopologyRefresh();
    screen.className = `screen ops-mode ${page.id}-mode`;
    setVisible("homePanel", false);
    setVisible("panelGrid", false);
    setVisible("tournamentPanel", false);
    setVisible("evidencePanel", false);
    setVisible("opsPanel", true);
    setVisible("incidentPanel", false);
    setVisible("heatmapPanel", false);
    setVisible("topologyPanel", false);
    startOpsRefresh(page);
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
    const ispPromises = ispNames.flatMap((name) => [
      prometheusRangeFor(ispTrafficQuery("ifHCInOctets", name), win).then((series) => series.map((s) => ({ ...s, _ispName: name, _direction: "in" }))),
      prometheusRangeFor(ispTrafficQuery("ifHCOutOctets", name), win).then((series) => series.map((s) => ({ ...s, _ispName: name, _direction: "out" })))
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

  function seriesMaxValue(series) {
    if (!series || !series.values || !series.values.length) return null;
    let max = -Infinity;
    for (const point of series.values) {
      if (Number.isFinite(point.v) && point.v > max) max = point.v;
    }
    return max === -Infinity ? null : max;
  }

  function countOfflineRecoveries(values) {
    // Recovery edges (0 -> 1) = completed disconnect events. Stale entries
    // that stayed offline the whole window have no recoveries.
    let recoveries = 0;
    for (let i = 1; i < values.length; i += 1) {
      const prev = values[i - 1].v;
      const curr = values[i].v;
      if (prev < 0.5 && curr >= 0.5) recoveries += 1;
    }
    return recoveries;
  }

  function analyzeIncident(data, threshold) {
    const affectedPlayers = [];
    const offlinePlayers = [];

    data.playerLatency.forEach((series) => {
      const max = seriesMaxValue(series);
      if (max !== null && max >= threshold) {
        affectedPlayers.push({
          team: series.metric.team,
          seat: series.metric.seat,
          network: series.metric.network,
          switch: series.metric.switch,
          instance: series.metric.instance,
          maxLatency: max
        });
      }
    });

    data.playerSuccess.forEach((series) => {
      const recoveries = countOfflineRecoveries(series.values);
      if (recoveries > 0) {
        offlinePlayers.push({
          team: series.metric.team,
          seat: series.metric.seat,
          network: series.metric.network,
          instance: series.metric.instance,
          recoveryCount: recoveries
        });
      }
    });

    const infraEvents = [];
    data.infraLatency.forEach((series) => {
      const max = seriesMaxValue(series);
      if (max !== null && max >= threshold) {
        infraEvents.push({
          instance: series.metric.instance || series.metric.display_name,
          targetIp: series.metric.target_ip,
          job: series.metric.job,
          maxLatency: max
        });
      }
    });
    data.infraSuccess.forEach((series) => {
      const recoveries = countOfflineRecoveries(series.values);
      if (recoveries > 0) {
        infraEvents.push({
          instance: series.metric.instance || series.metric.display_name,
          targetIp: series.metric.target_ip,
          job: series.metric.job,
          offline: true,
          recoveryCount: recoveries
        });
      }
    });

    const ispEvents = [];
    data.isp.forEach((series) => {
      const max = seriesMaxValue(series);
      if (max === null) return;
      const ifAlias = series._ispName || series.metric.ifAlias;
      const direction = series._direction || (series.metric.direction || "in");
      const capacityBps = ispCapacityBps(ifAlias, direction);
      ispEvents.push({
        ifAlias,
        direction,
        maxBps: max,
        capacityBps,
        utilization: capacityBps > 0 ? max / capacityBps : 0
      });
    });

    const stageGroups = {};
    [...affectedPlayers, ...offlinePlayers].forEach((player) => {
      const sw = player.switch || "unknown";
      if (sw === "static" || sw === "wireless-scan" || sw === "unknown") return;
      if (!stageGroups[sw]) stageGroups[sw] = { switch: sw, players: new Map() };
      const key = `${player.team}-${player.seat}-${player.network}`;
      stageGroups[sw].players.set(key, player);
    });
    Object.values(stageGroups).forEach((group) => {
      group.players = Array.from(group.players.values());
    });

    const verdict = computeIncidentVerdict(affectedPlayers, offlinePlayers, infraEvents, ispEvents, stageGroups);
    return { affectedPlayers, offlinePlayers, infraEvents, ispEvents, stageGroups, verdict };
  }

  function computeIncidentVerdict(affected, offline, infra, isp, stageGroups) {
    const totalAffected = affected.length + offline.length;

    if (totalAffected === 0 && infra.length === 0) {
      return { level: "good", text: "未检测到异常", detail: "这个时间窗口内没有任何选手或基础设施超过阈值。" };
    }

    const coreEvent = infra.find((event) => event.job === "infra-core-ping");
    const fwOffline = infra.find((event) => event.job === "infra-fw-ping" && event.offline);
    if (coreEvent || fwOffline) {
      return {
        level: "bad",
        text: "核心层异常",
        detail: `${coreEvent ? "核心交换机" : "防火墙"}在该时间窗口内有延迟尖峰或离线 — 所有选手可能都会受影响。`
      };
    }

    const stageKeys = Object.keys(stageGroups);
    if (stageKeys.length === 1 && stageGroups[stageKeys[0]].players.length >= 3) {
      const sw = stageKeys[0];
      const stageInfra = infra.find((event) => event.targetIp === sw || event.instance === sw);
      return {
        level: "warn",
        text: `怀疑 ${sw} 接入交换机`,
        detail: `${stageGroups[sw].players.length} 个选手集中卡顿，都接在这台 stage。${stageInfra ? "该交换机自身 ping 也抖动 — 高度怀疑该交换机问题。" : "该交换机自身 ping 正常 — 可能是它的上行链路或 VLAN 配置问题。"}`
      };
    }

    const highIsp = isp.filter((event) => event.utilization >= 0.7);
    if (highIsp.length > 0 && totalAffected >= 3) {
      return {
        level: "warn",
        text: "ISP 链路接近饱和",
        detail: `${highIsp.map((event) => `${event.ifAlias} ${event.direction === "in" ? "下载" : "上传"}=${formatBits(event.maxBps)}（${Math.round(event.utilization * 100)}% / ${formatBits(event.capacityBps)}）`).join("、")}。多个选手同时卡顿 — 怀疑该 ISP 链路被打满。`
      };
    }

    if (totalAffected === 1) {
      const player = affected[0] || offline[0];
      return {
        level: "warn",
        text: "单选手问题",
        detail: `仅 Team ${player.team} S${player.seat} 出现卡顿/离线 — 基础设施正常，怀疑该选手 PC / 网线 / 无线干扰等单点问题。`
      };
    }

    if (totalAffected >= 2) {
      return {
        level: "warn",
        text: "多选手卡顿",
        detail: `${totalAffected} 个选手出现异常，但没有明显的基础设施 / ISP 关联 — 建议手工进 /latency 单独看每个选手的趋势。`
      };
    }

    return { level: "warn", text: "基础设施异常未影响选手", detail: "检测到基础设施抖动但选手 ping 正常。" };
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
    const threshold = Number(document.getElementById("incidentThreshold").value || 0.02);

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
    stopTopologyRefresh();
    screen.className = "screen incident-mode";
    setVisible("homePanel", false);
    setVisible("panelGrid", false);
    setVisible("tournamentPanel", false);
    setVisible("evidencePanel", false);
    setVisible("opsPanel", false);
    setVisible("incidentPanel", true);
    setVisible("heatmapPanel", false);
    setVisible("topologyPanel", false);
    setupIncidentPanel();
  }

  // ---- Connection-quality heatmap ----

  function heatmapWindow() {
    const startInput = document.getElementById("heatmapStart");
    const windowInput = document.getElementById("heatmapWindow");
    const hours = Math.max(0.1, Number(windowInput && windowInput.value ? windowInput.value : 6));
    const now = Math.floor(Date.now() / 1000);
    let end = now;
    let start = now - Math.floor(hours * 3600);
    if (startInput && startInput.value) {
      const parsed = new Date(startInput.value);
      if (Number.isFinite(parsed.getTime())) {
        start = Math.floor(parsed.getTime() / 1000);
        end = Math.min(start + Math.floor(hours * 3600), now);
      }
    }
    const span = Math.max(60, end - start);
    const step = Math.max(15, Math.floor(span / 480));
    return { start, end, step, span };
  }

  function heatmapColorForOffline(offlineFrac) {
    if (!Number.isFinite(offlineFrac) || offlineFrac < 0) return { bg: "rgba(60, 70, 90, 0.6)", level: "none" };
    if (offlineFrac === 0) return { bg: "rgba(58, 175, 90, 0.92)", level: "good" };
    if (offlineFrac < 0.01) return { bg: "rgba(120, 200, 70, 0.92)", level: "good" };
    if (offlineFrac < 0.05) return { bg: "rgba(255, 224, 64, 0.92)", level: "warn" };
    if (offlineFrac < 0.15) return { bg: "rgba(255, 150, 50, 0.92)", level: "warn" };
    return { bg: "rgba(239, 35, 60, 0.94)", level: "bad" };
  }

  function fmtPctText(value) {
    if (!Number.isFinite(value)) return "-";
    if (value < 0.0001) return "0.00%";
    if (value < 0.01) return `${(value * 100).toFixed(2)}%`;
    return `${(value * 100).toFixed(1)}%`;
  }

  async function queryHeatmapData(win, teamCount) {
    const teamFilter = `team=~"${Array.from({ length: teamCount }, (_, i) => i + 1).join("|")}"`;
    const offlineQ = `1 - avg_over_time(probe_success{role="player",network="wired",${teamFilter}}[${win.span}s])`;
    const latencyQ = `avg_over_time(probe_icmp_duration_seconds{role="player",network="wired",phase="rtt",${teamFilter}}[${win.span}s])`;

    const url = (query) => `${prometheusBaseUrl()}/api/v1/query?query=${encodeURIComponent(query)}&time=${win.end}`;
    const fetchOne = async (query) => {
      const resp = await fetch(url(query), { cache: "no-store" });
      if (!resp.ok) throw new Error(`Prometheus HTTP ${resp.status}`);
      const payload = await resp.json();
      if (payload.status !== "success") throw new Error("Prometheus query failed");
      return payload.data.result.map((item) => ({ metric: item.metric || {}, value: Number(item.value[1]) }));
    };

    const [offline, latency] = await Promise.all([fetchOne(offlineQ), fetchOne(latencyQ)]);
    return { offline, latency };
  }

  function buildHeatmapCells(data, teamCount, seatCount) {
    const offlineBy = new Map();
    data.offline.forEach((item) => {
      const key = `${item.metric.team}|${item.metric.seat}`;
      const prev = offlineBy.get(key);
      if (prev === undefined || item.value > prev) offlineBy.set(key, item.value);
    });
    const latencyBy = new Map();
    data.latency.forEach((item) => {
      const key = `${item.metric.team}|${item.metric.seat}`;
      const prev = latencyBy.get(key);
      if (prev === undefined || (Number.isFinite(item.value) && item.value < prev)) {
        latencyBy.set(key, item.value);
      }
    });

    const cells = [];
    for (let team = 1; team <= teamCount; team += 1) {
      for (let seat = 1; seat <= seatCount; seat += 1) {
        const key = `${team}|${seat}`;
        cells.push({
          team,
          seat,
          offline: offlineBy.has(key) ? offlineBy.get(key) : NaN,
          latency: latencyBy.has(key) ? latencyBy.get(key) : NaN
        });
      }
    }
    return cells;
  }

  function renderHeatmapSummary(cells, win) {
    const evaluated = cells.filter((cell) => Number.isFinite(cell.offline));
    const goodCount = evaluated.filter((cell) => cell.offline === 0).length;
    const warnCount = evaluated.filter((cell) => cell.offline > 0 && cell.offline < 0.05).length;
    const badCount = evaluated.filter((cell) => cell.offline >= 0.05).length;
    const noDataCount = cells.length - evaluated.length;
    const totalOffline = evaluated.reduce((sum, cell) => sum + cell.offline, 0);
    const avgOffline = evaluated.length ? totalOffline / evaluated.length : NaN;

    const startStr = new Date(win.start * 1000).toLocaleString("zh-CN", { hour12: false });
    const endStr = new Date(win.end * 1000).toLocaleString("zh-CN", { hour12: false });
    document.getElementById("heatmapSummary").innerHTML = `
      <div class="heatmap-kpi"><strong>${cells.length}</strong><span>总座位</span></div>
      <div class="heatmap-kpi good"><strong>${goodCount}</strong><span>全程在线</span></div>
      <div class="heatmap-kpi warn"><strong>${warnCount}</strong><span>偶尔抖动</span></div>
      <div class="heatmap-kpi bad"><strong>${badCount}</strong><span>频繁掉线</span></div>
      <div class="heatmap-kpi"><strong>${noDataCount}</strong><span>无数据</span></div>
      <div class="heatmap-kpi"><strong>${fmtPctText(avgOffline)}</strong><span>平均离线率</span></div>
      <div class="heatmap-window">${escapeHtml(startStr)} → ${escapeHtml(endStr)}</div>
    `;
  }

  function renderHeatmapGrid(cells, teamCount, seatCount) {
    const grid = document.getElementById("heatmapGrid");
    grid.style.setProperty("--heatmap-team-count", String(teamCount));
    grid.style.setProperty("--heatmap-seat-count", String(seatCount));
    grid.innerHTML = cells.map((cell) => {
      const color = heatmapColorForOffline(cell.offline);
      const offlineText = Number.isFinite(cell.offline) ? fmtPctText(cell.offline) : "—";
      const latencyText = Number.isFinite(cell.latency) ? formatPingText(cell.latency) : "—";
      const tooltip = `Team ${cell.team} Seat ${cell.seat}\n离线 ${offlineText}  平均 ${latencyText}`;
      return `
        <div class="heatmap-cell ${color.level}" style="background:${color.bg}" title="${escapeHtml(tooltip)}">
          <span class="heatmap-cell-pos">T${cell.team}·S${cell.seat}</span>
          <strong class="heatmap-cell-pct">${escapeHtml(offlineText)}</strong>
          <em class="heatmap-cell-rtt">${escapeHtml(latencyText)}</em>
        </div>
      `;
    }).join("");
  }

  function renderHeatmapLegend() {
    document.getElementById("heatmapLegend").innerHTML = `
      <span class="heatmap-key" style="background:rgba(58, 175, 90, 0.92)">0% (全程在线)</span>
      <span class="heatmap-key" style="background:rgba(120, 200, 70, 0.92)">&lt;1% (基本稳)</span>
      <span class="heatmap-key" style="background:rgba(255, 224, 64, 0.92)">1-5% (轻微抖)</span>
      <span class="heatmap-key" style="background:rgba(255, 150, 50, 0.92)">5-15% (明显问题)</span>
      <span class="heatmap-key" style="background:rgba(239, 35, 60, 0.94)">&gt;15% (严重掉线)</span>
      <span class="heatmap-key" style="background:rgba(60, 70, 90, 0.6)">无数据</span>
    `;
  }

  async function runHeatmap() {
    const win = heatmapWindow();
    const teamCount = Math.max(1, Number(document.getElementById("heatmapTeams").value || 16));
    const seatCount = teamCount === 2 ? 5 : 4;

    const params = new URLSearchParams();
    const startVal = document.getElementById("heatmapStart").value;
    if (startVal) params.set("start", startVal);
    params.set("window", document.getElementById("heatmapWindow").value);
    params.set("teams", String(teamCount));
    window.history.replaceState({}, "", `/heatmap?${params.toString()}`);

    document.getElementById("heatmapSummary").innerHTML = `<div class="heatmap-loading">加载中…</div>`;
    document.getElementById("heatmapGrid").innerHTML = "";

    try {
      const data = await queryHeatmapData(win, teamCount);
      const cells = buildHeatmapCells(data, teamCount, seatCount);
      renderHeatmapSummary(cells, win);
      renderHeatmapGrid(cells, teamCount, seatCount);
      renderHeatmapLegend();
    } catch (error) {
      console.error("Heatmap query failed:", error);
      document.getElementById("heatmapSummary").innerHTML = `<div class="heatmap-error">查询失败: ${escapeHtml(error.message || "")}</div>`;
    }
  }

  function setupHeatmapPanel() {
    const params = new URLSearchParams(window.location.search);
    const startVal = params.get("start");
    const windowVal = params.get("window");
    const teamsVal = params.get("teams");

    const startInput = document.getElementById("heatmapStart");
    if (startVal) startInput.value = startVal;
    if (windowVal) {
      const select = document.getElementById("heatmapWindow");
      if (Array.from(select.options).some((opt) => opt.value === windowVal)) select.value = windowVal;
    }
    if (teamsVal) {
      const select = document.getElementById("heatmapTeams");
      if (Array.from(select.options).some((opt) => opt.value === teamsVal)) select.value = teamsVal;
    }

    const form = document.getElementById("heatmapForm");
    if (form && !form.dataset.bound) {
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        runHeatmap();
      });
      form.dataset.bound = "1";
    }
    runHeatmap();
  }

  function showHeatmap() {
    const screen = document.querySelector(".screen");
    stopInfraRefresh();
    stopTournamentRefresh();
    stopOpsRefresh();
    stopTopologyRefresh();
    screen.className = "screen heatmap-mode";
    setVisible("homePanel", false);
    setVisible("panelGrid", false);
    setVisible("tournamentPanel", false);
    setVisible("evidencePanel", false);
    setVisible("opsPanel", false);
    setVisible("incidentPanel", false);
    setVisible("heatmapPanel", true);
    setVisible("topologyPanel", false);
    setupHeatmapPanel();
  }

  // ---- Network topology ----

  let topologyTimer = null;

  function stopTopologyRefresh() {
    if (topologyTimer) {
      window.clearInterval(topologyTimer);
      topologyTimer = null;
    }
  }

  function bindTopologyNodeEvents(nodes) {
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
        const node = nodes[idx];
        if (!node) return;
        detail.hidden = false;
        detail.innerHTML = `
          <header><strong>${escapeHtml(node.name)}</strong><span class="dot ${node.level}"></span></header>
          <dl>
            <dt>类型</dt><dd>${escapeHtml(topologyNodeKindLabel(node.kind))}</dd>
            <dt>IP</dt><dd>${escapeHtml(node.ip || "—")}</dd>
            <dt>状态</dt><dd>${node.success === undefined ? "无数据" : (node.success ? "在线" : "离线")}</dd>
            <dt>延迟</dt><dd>${Number.isFinite(node.latency) ? formatPingText(node.latency) : "—"}</dd>
          </dl>
          ${node.ip ? `<a class="detail-link" href="/latency?ip=${encodeURIComponent(node.ip)}">在 /latency 查这个 IP →</a>` : ""}
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
      const [targets, edges] = await Promise.all([
        fetchTopologyTargets(),
        fetchTopologyEdges()
      ]);
      if (seq !== topologySeq) return;
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
      canvas.innerHTML = renderTopologySvg(layout, width);
      bindTopologyNodeEvents(layout.nodes);
      setupTopoPanZoom();
      applyTopoView();
      document.getElementById("topologyUpdated").textContent = `刷新于 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })} · 拖动平移·滚轮缩放·双击复位${edges.length ? ` · LLDP ${edges.length} 条边` : " · LLDP 未发现邻居"}`;
      lastDataSuccessAt = Date.now();
    } catch (error) {
      if (seq !== topologySeq) return;
      console.error("Topology fetch failed:", error);
      canvas.innerHTML = `<div class="topology-error">拓扑数据拉取失败: ${escapeHtml(error.message || "")}</div>`;
    }
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
    screen.className = "screen topology-mode";
    setVisible("homePanel", false);
    setVisible("panelGrid", false);
    setVisible("tournamentPanel", false);
    setVisible("evidencePanel", false);
    setVisible("opsPanel", false);
    setVisible("incidentPanel", false);
    setVisible("heatmapPanel", false);
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
    } else if (page.id === "evidence") {
      showEvidence();
    } else if (page.id === "incident") {
      showIncident();
    } else if (page.id === "heatmap") {
      showHeatmap();
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
    return Boolean(gaugeTimer || chartTimer || tournamentTimer || opsTimer || topologyTimer);
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

  function tick() {
    try {
      const now = new Date();
      setText("dateText", new Intl.DateTimeFormat("zh-CN", {
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        weekday: "short"
      }).format(now));
      setText("timeText", new Intl.DateTimeFormat("zh-CN", {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false
      }).format(now));
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
  // even when the underlying data is unchanged.
  window.addEventListener("resize", () => renderSignatures.clear());
})();
