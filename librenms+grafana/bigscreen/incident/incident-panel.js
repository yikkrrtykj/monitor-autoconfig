;(function () {
  'use strict';

  function createIncidentPanel(dependencies) {
    const {
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
      analyzeIncident
    } = dependencies;

    let lifecycleGeneration = 0;
    let active = false;

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

    function isCurrent(generation) {
      return active && generation === lifecycleGeneration;
    }

    async function runIncidentAnalysis() {
      const generation = lifecycleGeneration;
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
        if (!isCurrent(generation)) return;
        const result = analyzeIncident(data, threshold);
        renderIncidentVerdict(result.verdict);
        renderIncidentPlayers(result);
        renderIncidentInfra(result);
        renderIncidentIsp(result);
        renderIncidentStage(result);
      } catch (error) {
        if (!isCurrent(generation)) return;
        console.error("Incident analysis failed:", error);
        document.getElementById("incidentVerdict").className = "incident-verdict bad";
        document.getElementById("incidentVerdict").innerHTML = `<strong>分析失败</strong><span>${escapeHtml(error.message || "")}</span>`;
      }
    }

    function readUrlIntoForm() {
      const atInput = document.getElementById("incidentAt");
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
    }

    function bind() {
      const form = document.getElementById("incidentForm");
      if (form && !form.dataset.bound) {
        form.addEventListener("submit", (event) => {
          event.preventDefault();
          runIncidentAnalysis();
        });
        form.dataset.bound = "1";
      }
    }

    function start() {
      active = true;
      readUrlIntoForm();
      bind();
      return runIncidentAnalysis();
    }

    function stop() {
      active = false;
      lifecycleGeneration += 1;
    }

    return { start, stop };
  }

  const ns = { createIncidentPanel };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = ns;
  } else {
    window.BSIncidentPanel = ns;
  }
}());
