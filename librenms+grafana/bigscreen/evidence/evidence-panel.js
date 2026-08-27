;(function () {
  'use strict';

  function createEvidencePanel(dependencies) {
    const {
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
    } = dependencies;

    let evidenceSeq = 0;
    let lastEvidenceExport = null;
    let active = false;

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

    // CSV export for the operator query page (/latency) -- raw data to attach
    // to dispute reports alongside screenshots. Not wired to any TV-facing page.
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

    function isCurrent(seq) {
      return active && seq === evidenceSeq;
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
        if (!isCurrent(seq)) return;
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
        if (!isCurrent(seq)) return;
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
        if (!isCurrent(seq)) return;
        renderNoData(document.getElementById("evidenceSummary"), "查询失败");
        renderNoData(document.getElementById("evidenceLatencyChart"));
        renderNoData(document.getElementById("evidenceSuccessChart"));
        console.error(error);
      }
    }

    function bind() {
      const form = document.getElementById("evidenceForm");
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
    }

    function readUrlIntoForm() {
      const atInput = document.getElementById("evidenceAt");
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
    }

    function start() {
      active = true;
      readUrlIntoForm();
      bind();
      return queryEvidence();
    }

    function stop() {
      active = false;
      evidenceSeq += 1;
    }

    return { start, stop };
  }

  const ns = { createEvidencePanel };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = ns;
  } else {
    window.BSEvidencePanel = ns;
  }
}());
