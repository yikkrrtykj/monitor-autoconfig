;(function () {
  'use strict';

  function createTournamentPanel(dependencies) {
    const {
      document,
      window,
      console,
      teamLayouts,
      getTeamOrders,
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
      deleteRenderSignature,
      clearRenderSignatures,
      invalidateRangeCache,
      onDataSuccess
    } = dependencies;

    let tournamentTimer = null;
    let tournamentSeq = 0;
    let currentPage = null;

    function configuredTournamentPage(page) {
      if (!page || typeof teamLayouts.applyTeamOrder !== "function") return page;
      return teamLayouts.applyTeamOrder(page, getTeamOrders());
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

    async function refresh(page) {
      page = configuredTournamentPage(page);
      const seq = ++tournamentSeq;
      try {
        const selector = tournamentSelector(page);
        const [snapshot, trendSeries] = await Promise.all([
          fetchPlayerSnapshot(selector),
          prometheusRangeCached(tournamentTrendQuery(page), (metric) => {
            return `${teamName(page, metric.team)} ${seatLabel(metric.seat || "?")}`;
          })
        ]);
        if (seq !== tournamentSeq) return;
        const players = buildPlayers(snapshot.latencyItems, snapshot.successItems)
          .filter((player) => !page.teamSize || player.seat <= page.teamSize);
        renderTournamentSummary(page, players);
        renderTournamentBoard(page, players);
        if (shouldRender("tournamentTrend", seriesSignature(trendSeries))) {
          renderTournamentTrend(page, trendSeries);
        }
        onDataSuccess();
      } catch (error) {
        if (seq !== tournamentSeq) return;
        deleteRenderSignature("tournamentTrend");
        renderNoData(document.getElementById("tournamentBoard"), "暂无选手数据");
        renderNoData(document.getElementById("tournamentTrendChart"));
        console.error(error);
      }
    }

    function stop() {
      if (tournamentTimer) {
        window.clearInterval(tournamentTimer);
        tournamentTimer = null;
      }
      currentPage = null;
    }

    function bind() {
      const refreshBtn = document.getElementById("tournamentRefresh");
      if (refreshBtn && !refreshBtn.dataset.bound) {
        refreshBtn.addEventListener("click", () => {
          if (currentPage && (currentPage.kind === "match" || currentPage.kind === "tournament")) {
            refresh(currentPage);
          }
        });
        refreshBtn.dataset.bound = "1";
      }
    }

    function start(page) {
      stop();
      currentPage = page;
      clearRenderSignatures();
      invalidateRangeCache();
      refresh(page);
      tournamentTimer = window.setInterval(() => refresh(page), 5000);
      bind();
    }

    function hasScheduledRefresh() {
      return Boolean(tournamentTimer);
    }

    return { start, stop, refresh, hasScheduledRefresh };
  }

  const ns = { createTournamentPanel };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = ns;
  } else {
    window.BSTournamentPanel = ns;
  }
}());
