;(function () {
  'use strict';

  const common = typeof module !== "undefined" && module.exports;
  const model = common ? require("./overview-model.js") : window.BSOverviewModel;
  const ping = common ? require("../ping/model.js") : window.BSPingModel;
  const { escapeRegex } = common ? require("../utils.js") : window.BSUtils;
  const wanStep = 15;

  function regexMatcher(label, values) {
    return `${label}=~${JSON.stringify([...new Set(values)].map(escapeRegex).join("|"))}`;
  }

  function wanQuery(rows) {
    const source = `job="firewall-snmp",${regexMatcher("instance", rows.map(({ instance }) => instance))}`;
    const port = `${source},${regexMatcher("ifIndex", rows.map(({ index }) => index))}`;
    const tag = (field, expr) => `label_replace(${expr}, "overview_field", "${field}", "job", ".+")`;
    const rates = [["in", "ifHCInOctets"], ["out", "ifHCOutOctets"]].flatMap(([direction, name]) => {
      const counter = `${name}{${port}}`;
      return [tag(direction, `rate(${counter}[1m]) * 8`), tag(`${direction}_at`, `timestamp(${counter})`),
        tag(`${direction}_resets`, `resets(${counter}[1m])`)];
    });
    return [...rates, tag("speed", `ifHighSpeed{${port}}`),
      tag("up", `up{${source}}`), tag("up_at", `timestamp(up{${source}})`)].join(" or ");
  }

  async function readTopologyJson(path, signal) {
    const response = await fetch(path, { cache: "no-store", signal });
    if (!response.ok) throw new Error("WAN 清单源不可用");
    const reader = response.body.getReader();
    const chunks = [];
    let size = 0;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.length;
      if (size > 128 * 1024) {
        await reader.cancel();
        throw new Error("WAN 清单超出大小限制");
      }
      chunks.push(value);
    }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) {
      bytes.set(chunk, offset);
      offset += chunk.length;
    }
    return { bytes, data: JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)) };
  }

  async function readDiscoveredInventory(at, signal, readJson) {
    const [file, state] = await Promise.all([
      readJson("/topology/isp_targets.json", signal), readJson("/topology/isp-discovery-state.json", signal)
    ]);
    const rows = model.inventoryRows(file.data);
    const meta = state.data;
    const observedAt = meta?.last_success_at;
    const disabled = meta?.status === "disabled" && rows.length === 0;
    const fresh = meta?.status === "ok" && Number.isFinite(observedAt) &&
      observedAt <= at + 5 && at - observedAt <= model.inventoryFreshness;
    if ((!disabled && !fresh) || meta.inventory_count !== rows.length ||
        !/^[0-9a-f]{64}$/.test(meta.inventory_sha256)) {
      throw new Error("WAN 发现清单不可用或已过期");
    }
    // Some on-site displays run over plain HTTP. On secure origins the
    // discovery checksum also detects a same-size snapshot replaced mid-read.
    if (globalThis.crypto?.subtle) {
      const hash = new Uint8Array(await globalThis.crypto.subtle.digest("SHA-256", file.bytes));
      const hex = Array.from(hash, (value) => value.toString(16).padStart(2, "0")).join("");
      if (hex !== meta.inventory_sha256) throw new Error("WAN 发现清单版本不一致");
    }
    return { rows, observedAt, disabled };
  }

  async function readManualInventory(names, at, signal, query) {
    if (!names.length) return [];
    if (names.length > model.maxWans) throw new Error("WAN 清单超出范围");
    const selector = (match) => `ifHCInOctets{job="firewall-snmp",${match}}`;
    const result = await query([
      selector(regexMatcher("ifAlias", names)),
      selector(`ifAlias="",${regexMatcher("ifName", names)}`),
      selector(`ifAlias="",ifName="",${regexMatcher("ifDescr", names)}`)
    ].join(" or "), { time: at }, signal);
    if (result.length > 256) throw new Error("手工 WAN 接口匹配超出范围");
    return model.inventoryRows(names.map((name) => {
      const matches = result.filter(({ metric }) => (metric.ifAlias || metric.ifName || metric.ifDescr) === name);
      const metric = matches.length === 1 ? matches[0].metric : {};
      return { targets: [], labels: { display_name: name, metric_target: metric.instance,
        metric_ifindex: metric.ifIndex, metadata_conflict: matches.length > 1 } };
    }));
  }

  function createOverviewData({ pingData, query, autoDiscovery, configuredNames, bandwidthConfig, readJson = readTopologyJson }) {
    let inventory = null;

    async function bandwidth(at, routeSignal) {
      const signal = AbortSignal.any([routeSignal, AbortSignal.timeout(12000)]);
      if (!inventory || at < inventory.at || at - inventory.at >= 30 || at - inventory.observedAt > model.inventoryFreshness) {
        const discovered = autoDiscovery ? await readDiscoveredInventory(at, signal, readJson) : null;
        const result = discovered && !discovered.disabled ? discovered :
          { rows: await readManualInventory(configuredNames, at, signal, query), observedAt: at };
        signal.throwIfAborted();
        inventory = { ...result, at };
      }
      const { rows } = inventory;
      const usable = rows.filter((row) => row.key && !row.identityConflict && !row.metadataConflict);
      let series = [];
      let failed = false;
      if (usable.length) {
        try {
          series = await query(wanQuery(usable), { start: at - 900, end: at, step: wanStep }, signal);
          if (series.length > 10 * model.maxWans ** 2) throw new Error("WAN 时序超出范围");
        } catch {
          signal.throwIfAborted();
          failed = true;
        }
      }
      return { cards: model.wanCards(rows, series, at, wanStep, bandwidthConfig), failed };
    }

    async function gatewayStats(targets, at, signal) {
      const samples = await pingData.raw(targets, at - ping.defaultStats, at, signal);
      return new Map(targets.map((target) => [
        target.key, ping.rawStats(samples.get(target.key), ping.defaultStats, target.interval).stddev
      ]));
    }

    return { summary: pingData.summary, bandwidth, gatewayStats };
  }

  if (common) module.exports = { createOverviewData };
  else window.BSOverviewData = { createOverviewData };
})();
