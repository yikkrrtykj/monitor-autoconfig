;(function () {
  'use strict';

  // Incident root-cause analysis: classifies a time window of player/infra/ISP
  // series into a verdict (core failure, suspect access switch, saturated ISP
  // link, single-player issue, ...). Pure analysis -- querying and rendering
  // stay in app.js -- so the verdict rules are unit-tested in
  // tests/test_bigscreen_incident.js.
  const isNode = (typeof module !== 'undefined' && module.exports);
  const utils = isNode ? require('./utils.js') : window.BSUtils;
  const api = isNode ? require('./api.js') : window.BSApi;
  const { formatBits } = utils;
  const { ispCapacityBps } = api;

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
      const capacityBps = ispCapacityBps(ifAlias, direction, series._ispIndex);
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

  const ns = {
    seriesMaxValue,
    countOfflineRecoveries,
    analyzeIncident,
    computeIncidentVerdict
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = ns;
  } else {
    window.BSIncident = ns;
  }
}());
