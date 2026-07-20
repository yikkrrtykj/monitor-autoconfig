;(function () {
  'use strict';

  // Player snapshot logic: merging latency/success instant vectors into the
  // per-seat player list shown on the tournament/ops boards. Pure functions,
  // no DOM and no fetching -- the dedupe preference rules here decide which
  // entry wins a seat, so they are unit-tested in
  // tests/test_bigscreen_players.js.

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

  function playerStatusText(player) {
    if (!player.success) return "离线";
    if (!Number.isFinite(player.latency)) return "暂无延迟";
    if (player.latency >= 0.08) return "高延迟";
    if (player.latency >= 0.04) return "轻微抖动";
    return "正常";
  }

  const ns = {
    playerKey,
    isGatewayAddress,
    preferPlayer,
    dedupePlayersBySeat,
    buildPlayers,
    latencyLevel,
    playerStatusText
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = ns;
  } else {
    window.BSPlayers = ns;
  }
}());
