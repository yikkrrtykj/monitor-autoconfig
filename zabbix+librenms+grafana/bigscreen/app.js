(function () {
  const config = window.BIGSCREEN_CONFIG || {};
  const queries = window.BIGSCREEN_QUERIES || {};
  const pingTrendQuery = queries.pingTrend || "";
  const pingGaugeQuery = queries.pingGauge || "";
  const uptimeQuery = queries.uptime || "";
  const lossQuery = queries.loss || "";
  const infraPingJobs = queries.infraPingJobs || "infra-core-ping|infra-dist-ping|infra-fw-ping";
  const seriesColors = ["#73d17a", "#ffe32d", "#5b8ff9", "#ff9f43", "#ff4d66", "#b877db", "#40c4ff", "#b8e986", "#f8e71c"];
  const pages = window.BIGSCREEN_PAGES || [];
  let gaugeTimer = null;
  let chartTimer = null;
  let tournamentTimer = null;
  let opsTimer = null;
  let activePageId = "";
  let activeRoute = "";

  function setText(id, value) {
    const element = document.getElementById(id);
    if (element) {
      element.textContent = value || "";
    }
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, (char) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;"
    })[char]);
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

  function prometheusBaseUrl() {
    if (config.prometheusBaseUrl) {
      return config.prometheusBaseUrl.replace(/\/$/, "");
    }
    return "/prometheus";
  }

  function pageFromPath() {
    const path = window.location.pathname.replace(/\/+$/, "") || "/";
    if (path === "/index.html") return pages[0];
    if (path === "/evidence") return pages.find((page) => page.id === "evidence") || pages[0];
    return pages.find((page) => page.path === path) || pages[0];
  }

  function metricName(metric) {
    return metric.instance || metric.display_name || metric.ifAlias || metric.ifName || metric.ifDescr || "unknown";
  }

  function rangeWindow() {
    const end = Math.floor(Date.now() / 1000);
    const start = end - 30 * 60;
    return { start, end, step: 5 };
  }

  async function prometheusQuery(query) {
    const url = `${prometheusBaseUrl()}/api/v1/query?query=${encodeURIComponent(query)}`;
    const response = await fetch(url, { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`Prometheus HTTP ${response.status}`);
    }
    const payload = await response.json();
    if (payload.status !== "success") {
      throw new Error("Prometheus query failed");
    }
    return payload.data.result
      .map((item) => ({
        name: metricName(item.metric),
        value: Number(item.value[1])
      }))
      .filter((item) => Number.isFinite(item.value))
      .sort((a, b) => a.name.localeCompare(b.name, "zh-CN"));
  }

  async function prometheusInstant(query) {
    const url = `${prometheusBaseUrl()}/api/v1/query?query=${encodeURIComponent(query)}`;
    const response = await fetch(url, { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`Prometheus HTTP ${response.status}`);
    }
    const payload = await response.json();
    if (payload.status !== "success") {
      throw new Error("Prometheus query failed");
    }
    return payload.data.result
      .map((item) => ({
        metric: item.metric || {},
        value: Number(item.value[1])
      }))
      .filter((item) => Number.isFinite(item.value));
  }

  async function prometheusRangeFor(query, window, nameGetter = metricName) {
    const params = new URLSearchParams({
      query,
      ...Object.fromEntries(Object.entries(window).map(([key, value]) => [key, String(value)]))
    });
    const response = await fetch(`${prometheusBaseUrl()}/api/v1/query_range?${params.toString()}`, { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`Prometheus range HTTP ${response.status}`);
    }
    const payload = await response.json();
    if (payload.status !== "success") {
      throw new Error("Prometheus range query failed");
    }
    return payload.data.result
      .map((item) => ({
        name: nameGetter(item.metric || {}),
        metric: item.metric || {},
        values: item.values
          .map(([timestamp, value]) => ({ t: Number(timestamp), v: Number(value) }))
          .filter((point) => Number.isFinite(point.t) && Number.isFinite(point.v))
      }))
      .filter((item) => item.values.length)
      .sort((a, b) => a.name.localeCompare(b.name, "zh-CN"));
  }

  async function prometheusRange(query, nameGetter = metricName) {
    return prometheusRangeFor(query, rangeWindow(), nameGetter);
  }

  function activeInfraPingQuery() {
    return `up{job=~"${escapeLabel(infraPingJobs)}"}`;
  }

  function activeSeriesNames(items) {
    return new Set(items.map((item) => metricName(item.metric)).filter(Boolean));
  }

  function filterSeriesByNames(seriesList, names) {
    return seriesList.filter((item) => names.has(item.name));
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
    return new RegExp(stageDevicePattern(), "i").test(String(name || ""));
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

  function formatPing(seconds) {
    if (seconds < 0.001) {
      return { value: Math.round(seconds * 1000000), unit: "μs" };
    }
    return { value: (seconds * 1000).toFixed(2), unit: "ms" };
  }

  function formatPingText(seconds) {
    const formatted = formatPing(seconds);
    return `${formatted.value} ${formatted.unit}`;
  }

  function formatUptime(seconds) {
    if (seconds < 3600) {
      return { value: Math.max(1, Math.round(seconds / 60)), unit: "分钟" };
    }
    if (seconds < 86400) {
      return { value: (seconds / 3600).toFixed(2), unit: "小时" };
    }
    return { value: (seconds / 86400).toFixed(2), unit: "天" };
  }

  function formatBits(value) {
    const abs = Math.abs(value);
    if (abs >= 1000000000) return `${(value / 1000000000).toFixed(2)} Gb/s`;
    if (abs >= 1000000) return `${(value / 1000000).toFixed(1)} Mb/s`;
    if (abs >= 1000) return `${(value / 1000).toFixed(1)} kb/s`;
    return `${Math.round(value)} b/s`;
  }

  function networkLabel(network) {
    if (network === "wired") return "有线";
    if (network === "wireless") return "无线";
    if (network === "all") return "全部";
    return network || "-";
  }

  function seatLabel(seat) {
    return `S${seat}`;
  }

  function playerLabel(team, seat, network) {
    return `${teamName({ id: "" }, team)} ${seatLabel(seat)} ${networkLabel(network)}`;
  }

  function gaugeColor(kind, rawValue) {
    if (kind === "ping") {
      if (rawValue >= 0.02) return "#ff4d66";
      if (rawValue >= 0.01) return "#ffe32d";
      return "#73d17a";
    }
    return rawValue < 86400 ? "#ffe32d" : "#73d17a";
  }

  function gaugePercent(kind, rawValue) {
    const max = kind === "ping" ? 0.02 : 2592000;
    return Math.max(0.03, Math.min(1, rawValue / max));
  }

  function renderGaugeGrid(containerId, items, kind) {
    const container = document.getElementById(containerId);
    const formatter = kind === "ping" ? formatPing : formatUptime;
    const rows = Math.max(1, Math.min(items.length, items.length > 8 ? 3 : 2));
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

  function niceMax(value) {
    if (!Number.isFinite(value) || value <= 0) {
      return 1;
    }
    const exponent = Math.floor(Math.log10(value));
    const base = value / 10 ** exponent;
    const niceBase = base <= 1 ? 1 : base <= 2 ? 2 : base <= 5 ? 5 : 10;
    return niceBase * 10 ** exponent;
  }

  function average(values) {
    const usable = values.filter((value) => Number.isFinite(value));
    return usable.length ? usable.reduce((sum, value) => sum + value, 0) / usable.length : 0;
  }

  function formatTime(timestamp) {
    return new Intl.DateTimeFormat("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false
    }).format(new Date(timestamp * 1000));
  }

  function renderNoData(container, message) {
    container.innerHTML = `<div class="no-data">${message || "暂无数据"}</div>`;
  }

  function linePathFromPoints(points, smooth) {
    if (!points.length) return "";
    if (!smooth || points.length < 3) {
      return `M ${points.join(" L ")}`;
    }

    const coords = points.map((point) => {
      const [x, y] = point.split(",").map(Number);
      return { x, y };
    });
    const commands = [`M ${points[0]}`];
    for (let index = 0; index < coords.length - 1; index += 1) {
      const current = coords[index];
      const next = coords[index + 1];
      const previous = coords[index - 1] || current;
      const afterNext = coords[index + 2] || next;
      const cp1x = current.x + (next.x - previous.x) / 6;
      const cp1y = current.y + (next.y - previous.y) / 6;
      const cp2x = next.x - (afterNext.x - current.x) / 6;
      const cp2y = next.y - (afterNext.y - current.y) / 6;
      commands.push(`C ${cp1x.toFixed(1)},${cp1y.toFixed(1)} ${cp2x.toFixed(1)},${cp2y.toFixed(1)} ${next.x.toFixed(1)},${next.y.toFixed(1)}`);
    }
    return commands.join(" ");
  }

  function smoothValues(values, windowSize) {
    if (!windowSize || windowSize < 2 || values.length < 3) {
      return values;
    }

    return values.map((point, index) => {
      const start = Math.max(0, index - windowSize + 1);
      const window = values.slice(start, index + 1).map((item) => item.v);
      return { ...point, v: average(window) };
    });
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
    const maxV = niceMax(rawMax);
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
    const series = seriesList
      .filter((item) => item.values.length)
      .map((item) => ({ ...item, values: smoothValues(item.values, 5) }));
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

  function getIspNames() {
    return String(config.ispNames || "ISP1,ISP2")
      .split(",")
      .map((name) => name.trim())
      .filter(Boolean)
      .slice(0, 4);
  }

  function isIspAutoDiscoveryEnabled() {
    return ["1", "true", "yes", "on"].includes(String(config.ispAutoDiscovery || "").trim().toLowerCase());
  }

  function escapeRegex(value) {
    return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  function wanFilterPattern() {
    return String(config.wanIfFilter || "telecom,telcom,unicom,isp,wan")
      .split(",")
      .map((name) => name.trim())
      .filter(Boolean)
      .map(escapeRegex)
      .join("|") || "telecom|telcom|unicom|isp|wan";
  }

  function ispDiscoveryQuery() {
    const pattern = wanFilterPattern();
    return `group by (ifAlias) (ifHCInOctets{job="firewall-snmp",ifAlias=~".+",ifAlias=~"(?i).*(${pattern}).*"}) or group by (ifName) (ifHCInOctets{job="firewall-snmp",ifAlias="",ifName=~".+",ifName=~"(?i).*(${pattern}).*"}) or group by (ifDescr) (ifHCInOctets{job="firewall-snmp",ifAlias="",ifName="",ifDescr=~".+",ifDescr=~"(?i).*(${pattern}).*"})`;
  }

  function uniqueNames(names) {
    return Array.from(new Set(names.map((name) => String(name || "").trim()).filter(Boolean)));
  }

  async function fetchIspNames() {
    const configured = getIspNames();
    if (!isIspAutoDiscoveryEnabled()) {
      return configured;
    }

    try {
      const discovered = await prometheusInstant(ispDiscoveryQuery());
      const discoveredNames = uniqueNames(discovered.map((item) => item.metric.ifAlias || item.metric.ifName || item.metric.ifDescr));
      discoveredNames.sort((a, b) => a.localeCompare(b, "zh-CN", { numeric: true }));
      const names = configured.length ? uniqueNames([...configured, ...discoveredNames]) : discoveredNames;
      const limitedNames = names.slice(0, 4);
      return limitedNames.length ? limitedNames : configured;
    } catch (error) {
      console.warn("ISP discovery failed", error);
      return configured;
    }
  }

  function escapeLabel(value) {
    return String(value).replace(/\\/g, "\\\\").replace(/"/g, '\\"');
  }

  function ispTrafficQuery(metric, name) {
    const label = escapeLabel(name);
    return `sum(rate(${metric}{job="firewall-snmp",ifAlias="${label}"}[1m]) or rate(${metric}{job="firewall-snmp",ifAlias="",ifName="${label}"}[1m]) or rate(${metric}{job="firewall-snmp",ifAlias="",ifName="",ifDescr="${label}"}[1m])) * 8`;
  }

  async function fetchIspTraffic() {
    const names = await fetchIspNames();
    return Promise.all(names.map(async (name) => {
      const [download, upload] = await Promise.all([
        prometheusRange(ispTrafficQuery("ifHCInOctets", name)),
        prometheusRange(ispTrafficQuery("ifHCOutOctets", name))
      ]);
      return {
        name,
        download: { name: "下载", color: "#73d17a", values: download[0] ? download[0].values : [] },
        upload: { name: "上传", color: "#5b8ff9", values: upload[0] ? upload[0].values : [] }
      };
    }));
  }

  function renderIspPanels(results) {
    const ispGrid = document.getElementById("ispGrid");
    ispGrid.style.setProperty("--isp-count", String(Math.max(1, results.length)));
    ispGrid.innerHTML = "";
    if (!results.length) {
      renderNoData(ispGrid);
      return;
    }
    results.forEach((result, index) => {
      const panel = document.createElement("section");
      panel.className = "chart-panel isp-panel";
      panel.innerHTML = `<h2>${escapeHtml(result.name)}</h2><div class="chart-body" id="ispChart${index}"></div>`;
      ispGrid.appendChild(panel);
      renderLineChart(`ispChart${index}`, [result.download, result.upload], {
        axisFormatter: formatBits,
        valueFormatter: formatBits,
        axisPadLeft: 92,
        axisPadRight: 38,
        fill: true,
        legend: "bottom",
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
    return /\.(?:1|254)$/.test(String(ip || ""));
  }

  function preferPlayer(prev, candidate) {
    // Online wins over offline.
    if (candidate.success && !prev.success) return candidate;
    if (!candidate.success && prev.success) return prev;
    // Both online or both offline -- prefer the one with a finite latency,
    // then the lower latency (more reliable connection).
    const candFinite = Number.isFinite(candidate.latency);
    const prevFinite = Number.isFinite(prev.latency);
    if (candFinite && !prevFinite) return candidate;
    if (!candFinite && prevFinite) return prev;
    if (candFinite && prevFinite && candidate.latency < prev.latency) return candidate;
    return prev;
  }

  function dedupePlayersBySeat(players) {
    // A team-labeled port can show multiple (ip) targets when the switch MAC
    // table still holds a recently-aged entry alongside the live one. The
    // bigscreen only has one slot per (team, seat, network), so collapse
    // duplicates -- keep the online entry; if both online, keep the lower
    // latency one. Offline duplicates collapse silently.
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
    return `
      <a class="seat-slot ${level}" href="${escapeHtml(latencyUrlForPlayer(player))}" title="查看${escapeHtml(playerLabel(player.team, player.seat, player.network))}延迟">
        <span>${seatLabel(player.seat)}</span>
        <strong>${escapeHtml(latency)}</strong>
        <em>${escapeHtml(player.ip)}</em>
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
    return `avg by (team,seat) (avg_over_time(probe_icmp_duration_seconds{${selector},phase="rtt"}[3m]))`;
  }

  function lineChartOptions() {
    return {
      axisFormatter: formatPingText,
      valueFormatter: formatPingText,
      minMax: 0.005,
      smooth: true,
      smoothWindow: 5
    };
  }

  function renderTournamentTrend(page, trendSeries) {
    const container = document.getElementById("tournamentTrendChart");
    const teams = page.teams || [];
    container.innerHTML = `
      <div class="team-trend-grid" style="--trend-team-count:${teams.length}">
        ${teams.map((team) => {
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
        }).join("")}
      </div>
    `;
    teams.forEach((team) => {
      const teamSeries = trendSeries
        .filter((item) => String(item.metric.team || "") === String(team))
        .sort((a, b) => Number(a.metric.seat || 0) - Number(b.metric.seat || 0))
        .map((item) => ({ ...item, name: seatLabel(item.metric.seat || "?") }));
      renderSparkline(`teamTrend${team}`, teamSeries);
    });
  }

  async function refreshTournament(page) {
    try {
      const selector = tournamentSelector(page);
      const [latencyItems, successItems, trendSeries] = await Promise.all([
        prometheusInstant(`probe_icmp_duration_seconds{${selector},phase="rtt"}`),
        prometheusInstant(`probe_success{${selector}}`),
        prometheusRange(tournamentTrendQuery(page), (metric) => {
          return `${teamName(page, metric.team)} ${seatLabel(metric.seat || "?")}`;
        })
      ]);
      const players = buildPlayers(latencyItems, successItems)
        .filter((player) => !page.teamSize || player.seat <= page.teamSize);
      renderTournamentSummary(page, players);
      renderTournamentBoard(page, players);
      renderTournamentTrend(page, trendSeries);
    } catch (error) {
      renderNoData(document.getElementById("tournamentBoard"), "暂无选手数据");
      renderNoData(document.getElementById("tournamentTrendChart"));
      console.error(error);
    }
  }

  async function refreshGauges() {
    try {
      const [pingItems, uptimeItems] = await Promise.all([
        prometheusQuery(pingGaugeQuery),
        prometheusQuery(uptimeQuery)
      ]);
      renderGaugeGrid("pingGaugeGrid", visibleInfraItems(pingItems), "ping");
      renderGaugeGrid("uptimeGaugeGrid", visibleInfraItems(uptimeItems), "uptime");
    } catch (error) {
      renderGaugeGrid("pingGaugeGrid", [], "ping");
      renderGaugeGrid("uptimeGaugeGrid", [], "uptime");
      console.error(error);
    }
  }

  async function refreshCharts() {
    try {
      const [activeItems, pingSeries, lossSeries, ispTraffic] = await Promise.all([
        prometheusInstant(activeInfraPingQuery()),
        prometheusRange(pingTrendQuery),
        prometheusRange(lossQuery),
        fetchIspTraffic()
      ]);
      const activeNames = activeSeriesNames(visibleInfraItems(activeItems));
      const activePingSeries = visibleInfraSeries(filterSeriesByNames(pingSeries, activeNames));
      const activeLossSeries = visibleInfraSeries(filterSeriesByNames(lossSeries, activeNames));
      renderLineChart("pingTrendChart", activePingSeries, {
        axisFormatter: formatPingText,
        valueFormatter: formatPingText,
        minMax: 0.005,
        smooth: true,
        smoothWindow: 5
      });
      renderHeatmap("lossHeatmap", activeLossSeries);
      renderIspPanels(ispTraffic);
    } catch (error) {
      renderNoData(document.getElementById("pingTrendChart"));
      renderNoData(document.getElementById("lossHeatmap"));
      renderNoData(document.getElementById("ispGrid"));
      console.error(error);
    }
  }

  async function fetchPlayerSnapshot(selector) {
    const [latencyItems, successItems] = await Promise.all([
      prometheusInstant(`probe_icmp_duration_seconds{${selector},phase="rtt"}`),
      prometheusInstant(`probe_success{${selector}}`)
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
        { label: "疑似网关", value: gatewayIps.size, level: gatewayIps.size ? "bad" : "good", note: ".1 / .254" },
        { label: "最高延迟", value: Number.isFinite(maxLatency) ? formatPingText(maxLatency) : "-", level: maxLatency >= 0.08 ? "warn" : "good" }
      ]);
      renderWirelessBoard(players);
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
        smoothWindow: 5,
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
    refreshGauges();
    refreshCharts();
    gaugeTimer = window.setInterval(refreshGauges, 5000);
    chartTimer = window.setInterval(refreshCharts, 5000);
  }

  function startTournamentRefresh(page) {
    stopTournamentRefresh();
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
    screen.className = "screen home-mode";
    setVisible("homePanel", true);
    setVisible("panelGrid", false);
    setVisible("tournamentPanel", false);
    setVisible("evidencePanel", false);
    setVisible("opsPanel", false);
    renderHomeCards();
  }

  function showInfra() {
    const screen = document.querySelector(".screen");
    stopTournamentRefresh();
    stopOpsRefresh();
    screen.className = "screen infra-mode";
    setVisible("homePanel", false);
    setVisible("panelGrid", true);
    setVisible("tournamentPanel", false);
    setVisible("evidencePanel", false);
    setVisible("opsPanel", false);
    startInfraRefresh();
  }

  function showTournament(page) {
    const screen = document.querySelector(".screen");
    stopOpsRefresh();
    screen.className = `screen tournament-mode ${page.kind === "match" ? "match-mode" : "multi-team-mode"} ${page.id}`;
    setVisible("homePanel", false);
    setVisible("panelGrid", true);
    setVisible("tournamentPanel", true);
    setVisible("evidencePanel", false);
    setVisible("opsPanel", false);
    document.getElementById("tournamentPanel").className = `tournament-panel ${page.kind === "match" ? "match-panel" : "multi-team-panel"} ${page.id}`;
    startInfraRefresh();
    startTournamentRefresh(page);
  }

  function showEvidence() {
    const screen = document.querySelector(".screen");
    stopInfraRefresh();
    stopTournamentRefresh();
    stopOpsRefresh();
    screen.className = "screen evidence-mode";
    setVisible("homePanel", false);
    setVisible("panelGrid", false);
    setVisible("tournamentPanel", false);
    setVisible("evidencePanel", true);
    setVisible("opsPanel", false);
    setupEvidencePanel();
  }

  function showOps(page) {
    const screen = document.querySelector(".screen");
    stopInfraRefresh();
    stopTournamentRefresh();
    screen.className = `screen ops-mode ${page.id}-mode`;
    setVisible("homePanel", false);
    setVisible("panelGrid", false);
    setVisible("tournamentPanel", false);
    setVisible("evidencePanel", false);
    setVisible("opsPanel", true);
    startOpsRefresh(page);
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
    } else if (page.id === "wireless" || page.id === "seat-check") {
      showOps(page);
    } else if (page.kind) {
      showTournament(page);
    } else {
      showInfra();
    }
  }

  function tick() {
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
  }

  renderPage();
  tick();
  window.setInterval(tick, 1000);
  window.addEventListener("popstate", renderPage);
})();
