;(function () {
  'use strict';

  function createWirelessPanel(dependencies) {
    const {
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
      onDataSuccess
    } = dependencies;

    let wirelessTimer = null;

    function renderWirelessKpis(items) {
      document.getElementById("wirelessSummary").innerHTML = items.map((item) => `
        <div class="wireless-kpi ${item.level || "info"}">
          <span>${escapeHtml(item.label)}</span>
          <strong>${escapeHtml(item.value)}</strong>
          <em>${escapeHtml(item.note || "")}</em>
        </div>
      `).join("");
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
        onDataSuccess();
      } catch (error) {
        renderNoData(document.getElementById("wirelessSummary"), "查询失败");
        renderNoData(document.getElementById("wirelessBoard"));
        console.error(error);
      }
    }

    function stop() {
      if (wirelessTimer) {
        window.clearInterval(wirelessTimer);
        wirelessTimer = null;
      }
    }

    function start(page) {
      stop();
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

    function hasScheduledRefresh() {
      return Boolean(wirelessTimer);
    }

    return { start, stop, hasScheduledRefresh };
  }

  const ns = { createWirelessPanel };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = ns;
  } else {
    window.BSWirelessPanel = ns;
  }
}());
