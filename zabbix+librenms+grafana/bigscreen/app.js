(function () {
  const config = window.BIGSCREEN_CONFIG || {};
  const pingRawQuery = 'avg by (instance) (probe_icmp_duration_seconds{job=~"infra-core-ping|infra-dist-ping|infra-fw-ping",phase="rtt"})';
  const pingGaugeQuery = 'avg by (instance) (quantile_over_time(0.5, probe_icmp_duration_seconds{job=~"infra-core-ping|infra-dist-ping|infra-fw-ping",phase="rtt"}[1m]))';
  const uptimeQuery = 'max by (instance) (sysUpTime{job=~"infra-switch-snmp|infra-fw-snmp",instance!~"^(?:[0-9]{1,3}\\\\.){3}[0-9]{1,3}$"} / 100) or max by (instance) ((sysUpTime{job=~"infra-switch-snmp|infra-fw-snmp",instance=~"^(?:[0-9]{1,3}\\\\.){3}[0-9]{1,3}$"} / 100) unless on(target_ip) sysUpTime{job=~"infra-switch-snmp|infra-fw-snmp",instance!~"^(?:[0-9]{1,3}\\\\.){3}[0-9]{1,3}$"})';
  const lossQuery = 'max by (instance) (1 - probe_success{job=~"infra-core-ping|infra-dist-ping|infra-fw-ping"})';
  const seriesColors = ["#73d17a", "#ffe32d", "#5b8ff9", "#ff9f43", "#ff4d66", "#b877db", "#40c4ff", "#b8e986", "#f8e71c"];

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

  async function prometheusRange(query) {
    const params = new URLSearchParams({
      query,
      ...Object.fromEntries(Object.entries(rangeWindow()).map(([key, value]) => [key, String(value)]))
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
        name: metricName(item.metric),
        values: item.values
          .map(([timestamp, value]) => ({ t: Number(timestamp), v: Number(value) }))
          .filter((point) => Number.isFinite(point.t) && Number.isFinite(point.v))
      }))
      .filter((item) => item.values.length)
      .sort((a, b) => a.name.localeCompare(b.name, "zh-CN"));
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
      return { value: Math.max(1, Math.round(seconds / 60)), unit: "min" };
    }
    if (seconds < 86400) {
      return { value: (seconds / 3600).toFixed(2), unit: "hours" };
    }
    return { value: (seconds / 86400).toFixed(2), unit: "days" };
  }

  function formatBits(value) {
    const abs = Math.abs(value);
    if (abs >= 1000000000) return `${(value / 1000000000).toFixed(2)} Gb/s`;
    if (abs >= 1000000) return `${(value / 1000000).toFixed(1)} Mb/s`;
    if (abs >= 1000) return `${(value / 1000).toFixed(1)} kb/s`;
    return `${Math.round(value)} b/s`;
  }

  function gaugeColor(kind, rawValue) {
    if (kind === "ping") {
      if (rawValue >= 0.02) return "#ff4d66";
      if (rawValue >= 0.005) return "#ffe32d";
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
      container.innerHTML = '<div class="empty-state">No data</div>';
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
    container.innerHTML = `<div class="no-data">${message || "No data"}</div>`;
  }

  function renderLineChart(containerId, seriesList, options) {
    const container = document.getElementById(containerId);
    const series = seriesList.filter((item) => item.values.length);
    if (!series.length) {
      renderNoData(container);
      return;
    }

    const width = 1000;
    const height = 260;
    const pad = { left: 84, right: 16, top: 14, bottom: 30 };
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
    const timeLabels = [minT, (minT + maxT) / 2, maxT].map((timestamp) => {
      const x = xOf(timestamp);
      return `<text class="chart-axis" x="${x}" y="${height - 7}" text-anchor="middle">${formatTime(timestamp)}</text>`;
    }).join("");
    const paths = series.map((item, index) => {
      const color = item.color || seriesColors[index % seriesColors.length];
      const points = item.values.map((point) => `${xOf(point.t).toFixed(1)},${yOf(point.v).toFixed(1)}`);
      const linePath = `M ${points.join(" L ")}`;
      const areaPath = options.fill
        ? `${linePath} L ${xOf(item.values[item.values.length - 1].t).toFixed(1)},${height - pad.bottom} L ${xOf(item.values[0].t).toFixed(1)},${height - pad.bottom} Z`
        : "";
      return `${areaPath ? `<path class="chart-area" d="${areaPath}" style="fill:${color}" />` : ""}<path class="chart-line" d="${linePath}" style="stroke:${color}" />`;
    }).join("");
    const legend = series.map((item, index) => {
      const color = item.color || seriesColors[index % seriesColors.length];
      const values = item.values.map((point) => point.v);
      const max = Math.max(...values);
      const mean = average(values);
      return `
        <div class="legend-row">
          <span class="legend-swatch" style="background:${color}"></span>
          <span class="legend-name">${escapeHtml(item.name)}</span>
          <span>${escapeHtml(valueFormatter(mean))}</span>
          <span>${escapeHtml(valueFormatter(max))}</span>
        </div>
      `;
    }).join("");
    const legendHeader = '<div class="legend-row legend-head"><span></span><span>Name</span><span>Mean</span><span>Max</span></div>';
    const legendClass = options.legend === "bottom" ? "chart-legend bottom-legend" : "chart-legend side-legend";

    container.innerHTML = `
      <div class="line-layout ${options.legend === "bottom" ? "bottom-layout" : "side-layout"}">
        <svg class="line-chart" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" focusable="false">
          ${timeGridLines}
          ${gridLines}
          ${paths}
          ${timeLabels}
        </svg>
        <div class="${legendClass}">${legendHeader}${legend}</div>
      </div>
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
      <div class="heatmap">
        <div class="heatmap-rows">${rows}</div>
        <div class="heatmap-axis"><span>${formatTime(minT)}</span><span>${formatTime((minT + maxT) / 2)}</span><span>${formatTime(maxT)}</span></div>
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

  function escapeLabel(value) {
    return String(value).replace(/\\/g, "\\\\").replace(/"/g, '\\"');
  }

  function ispTrafficQuery(metric, name) {
    const label = escapeLabel(name);
    return `sum(rate(${metric}{job="firewall-snmp",ifAlias="${label}"}[1m]) or rate(${metric}{job="firewall-snmp",ifAlias="",ifName="${label}"}[1m]) or rate(${metric}{job="firewall-snmp",ifAlias="",ifName="",ifDescr="${label}"}[1m])) * 8`;
  }

  async function fetchIspTraffic() {
    const names = getIspNames();
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
        fill: true,
        legend: "bottom",
        minMax: 1
      });
    });
  }

  async function refreshGauges() {
    try {
      const [pingItems, uptimeItems] = await Promise.all([
        prometheusQuery(pingGaugeQuery),
        prometheusQuery(uptimeQuery)
      ]);
      renderGaugeGrid("pingGaugeGrid", pingItems, "ping");
      renderGaugeGrid("uptimeGaugeGrid", uptimeItems, "uptime");
    } catch (error) {
      renderGaugeGrid("pingGaugeGrid", [], "ping");
      renderGaugeGrid("uptimeGaugeGrid", [], "uptime");
      console.error(error);
    }
  }

  async function refreshCharts() {
    try {
      const [pingSeries, lossSeries, ispTraffic] = await Promise.all([
        prometheusRange(pingRawQuery),
        prometheusRange(lossQuery),
        fetchIspTraffic()
      ]);
      renderLineChart("pingTrendChart", pingSeries, {
        axisFormatter: formatPingText,
        valueFormatter: formatPingText,
        minMax: 0.005
      });
      renderHeatmap("lossHeatmap", lossSeries);
      renderIspPanels(ispTraffic);
    } catch (error) {
      renderNoData(document.getElementById("pingTrendChart"));
      renderNoData(document.getElementById("lossHeatmap"));
      renderNoData(document.getElementById("ispGrid"));
      console.error(error);
    }
  }

  function renderHeader() {
    const title = titleText();
    const logoText = config.logoText || "";
    const brand = document.getElementById("brand");
    setText("screenTitle", title);
    setText("screenSubtitle", config.subtitle || "");
    setText("logoText", logoText);
    setText("brandMark", logoText ? logoText.slice(0, 1).toUpperCase() : "");
    brand.hidden = !logoText;
    document.title = title;
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

  renderHeader();
  refreshGauges();
  refreshCharts();
  tick();
  window.setInterval(refreshGauges, 5000);
  window.setInterval(refreshCharts, 5000);
  window.setInterval(tick, 1000);
})();
